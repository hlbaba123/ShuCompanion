"""Immutable handoff from a verified single import to the battle runner."""
from contextlib import closing
from dataclasses import dataclass, replace
import hashlib
import ntpath
from pathlib import Path
import re
import sqlite3
import threading
from types import SimpleNamespace

from . import maa_queue_receipt as receipts
from .workflow_store import WorkflowStore


class ImportBindingError(RuntimeError):
    code='battle_import_binding_unverified'
    def __init__(self):
        super().__init__('导入与开战交接的作业凭据已变化或未核实，未开始作战。')


@dataclass(frozen=True)
class ImportJobPin:
    stage_code: str
    mystery_code: str
    difficulty: str
    maa_identity: tuple
    document_sha256: str | None
    source_task_id: str
    report_path: str | None
    report_sha256: str | None
    proof_path: str
    proof_sha256: str
    source_revision: int | None = None
    source_epoch: int | None = None
    import_dispatch_id: str | None = None


def bind_import_target(pin,target,identity,document_sha256):
    if pin is None:return target
    if (type(pin) is not ImportJobPin or not isinstance(target,dict)
            or target.get('stage_code')!=pin.stage_code or target.get('mystery_code')!=pin.mystery_code
            or tuple(identity.get(k) for k in ('hwnd','pid','creation_time'))!=pin.maa_identity[:3]
            or ntpath.normcase(ntpath.normpath(str(identity.get('executable'))))!=ntpath.normcase(ntpath.normpath(pin.maa_identity[3]))
            or pin.document_sha256 is not None and document_sha256!=pin.document_sha256):
        raise ImportBindingError()
    # The import's verified difficulty is authoritative, not a substring such
    # as "compatible with raid" in the author's human-readable title.
    return {**target,'difficulty':pin.difficulty}


def current_import_receipt(store, state, dispatch_id):
    """Read a current immutable single-import result for the commit gate."""
    try:
        dispatch = store.get_dispatch(dispatch_id)
        receipt = store.dispatch_result(dispatch_id)
        payload = dispatch['payload']
        # Ordinary single imports predate execution_step; their typed contract
        # identifies the import. Compound dispatches must still say "import".
        import_step = (payload.get('execution_step') == 'import'
                       or 'execution_step' not in payload
                       and state['contract'].get('current_deliverable') == 'maa_import'
                       and state['contract'].get('workflow_scope') != 'maa_import_then_battle')
        if (any(dispatch[key] != state[key] for key in ('task_id', 'revision', 'epoch'))
                or dispatch['role'] != 'maa' or dispatch['status'] != 'completed'
                or not import_step or payload.get('contract') != state['contract']
                or not receipt or receipt.get('status') != 'import_verified'
                or receipt.get('transport') != 'screenshot_keyboard'
                or receipt.get('battle_start_submitted') is not False
                or receipt.get('queue_receipt_ref') or receipt.get('single_document_plan_ref')
                or not isinstance(receipt.get('receipt_ref'), dict)
                or receipt['receipt_ref'] not in (receipt.get('evidence_refs') or [])):
            raise ValueError('missing current single import receipt')
        return receipt
    except (ValueError, TypeError, KeyError, RuntimeError):
        raise ImportBindingError() from None


def _pin_from_receipt(profile, task_id, receipt, verified, report_path=None, report_sha256=None):
    def artifact(ref):
        if ref not in receipt['evidence_refs']:
            raise ValueError('unregistered evidence')
        path = receipts._no_links(Path(ref['path']).absolute()).resolve(strict=True)
        if not path.is_relative_to(profile):
            raise ValueError('foreign evidence')
        body,_ = receipts._read_stable(path,maximum=32*1024*1024)
        if hashlib.sha256(body).hexdigest() != ref['sha256']:
            raise ValueError('changed evidence')
        return receipts._json(body)

    proof_ref = receipt['receipt_ref']
    proof = artifact(proof_ref)
    expected = proof['expected']
    if (proof.get('transport') != 'screenshot_ocr' or proof.get('start_clicked') is not False
            or not re.fullmatch(r'[A-Z0-9]+(?:-[A-Z0-9]+)+',expected['stage_code'])
            or not re.fullmatch(r'prts://[1-9][0-9]{0,11}',expected['code'])
            or expected['code'] != verified['mystery_code']
            or type(expected['difficulty']) is not int or expected['difficulty'] not in (1, 2)):
        raise ValueError('invalid imported target')
    before = next(frame for frame in map(artifact, proof['observation_refs'])
                  if frame.get('screenshot_id') == proof['before'])
    identity = receipts._process(before['window'])
    document_sha256 = None
    if proof.get('load_receipt_ref') is not None:
        # Use the MAA-normalized cache bytes. The downloaded station JSON
        # can have a different byte hash after MAA serialization.
        application = artifact(proof['load_receipt_ref'])
        document_sha256 = application.get('cache',{}).get('sha256')
        if not isinstance(document_sha256,str) or not re.fullmatch('[0-9a-f]{64}',document_sha256):
            raise ValueError('cache fingerprint')
    return ImportJobPin(expected['stage_code'],expected['code'],{1:'normal',2:'raid'}[expected['difficulty']],
        tuple(identity[k] for k in ('hwnd','pid','creation_time','executable')),document_sha256,
        task_id,report_path,report_sha256,proof_ref['path'],proof_ref['sha256'])


def load_current_import_binding(store, task_id, import_dispatch_id):
    """Verify the exact import dispatch without finishing or changing the task."""
    from .conversation_acceptance import import_verification
    try:
        state = store.current(task_id)
        receipt = current_import_receipt(store, state, import_dispatch_id)
        verified = import_verification(store, task_id, dispatch_id=import_dispatch_id)
        if verified.get('verified') is not True or verified.get('battle_start_submitted') is not False:
            raise ValueError('not a verified single import')
        pin = _pin_from_receipt(Path(store.engine.data_dir).resolve(), task_id, receipt, verified)
        current = store.current(task_id)
        if any(current[key] != state[key] for key in ('revision', 'epoch', 'control_seq', 'writes_enabled')):
            raise ValueError('import generation changed')
        return replace(pin,source_revision=state['revision'],source_epoch=state['epoch'],
                       import_dispatch_id=import_dispatch_id)
    except (OSError,ValueError,TypeError,KeyError,AttributeError,StopIteration,sqlite3.Error,RuntimeError):
        raise ImportBindingError() from None


def _check_requested_mode(contract, imported_mode):
    """Bound a pinned mode by this request; never infer a replacement mode."""
    requested, excluded = set(), set()
    text = contract.get('original_user_request', contract.get('user_request', ''))
    if not isinstance(text, str):
        raise ValueError('invalid request mode')
    for match in re.finditer(r'普通|突袭|\bnormal\b|\braid\b', text, re.I):
        mode = 'raid' if match.group().casefold() in ('突袭', 'raid') else 'normal'
        prefix = text[max(0, match.start()-16):match.start()]
        negated = re.search(r'(?:不是|并非|非|禁止|(?:不(?:要|用|必|需(?:要)?)?|别|无需)'
                            r'\s*(?:打|跑|选|运行|执行|使用)?)\s*$', prefix)
        (excluded if negated else requested).add(mode)
    scope = contract.get('import_scope') or {}
    difficulty = scope.get('difficulty')
    if difficulty is not None:
        if type(difficulty) is not int or difficulty not in (1, 2):
            raise ValueError('ambiguous requested mode')
        requested.add({1:'normal', 2:'raid'}[difficulty])
    if imported_mode in excluded or requested and requested != {imported_mode}:
        raise ValueError('requested difficulty differs from verified import')


def load_related_import_binding(store, context):
    """Resolve only gameplay's explicit completed single-import source task."""
    try:
        with store.engine._lock:
            target = store.current(context['task_id'])
            store.management.require_active(target['task_id'])
            source_id = target['contract'].get('source_task_id')
            if (any(target[key] != context[key] for key in ('task_id','revision','epoch','contract'))
                    or target['contract'].get('current_deliverable') != 'gameplay'
                    or not isinstance(source_id,str) or not source_id or source_id == target['task_id']):
                raise ValueError('missing explicit import source')
            battle = store.get_dispatch(context['dispatch_id'])
            if (any(battle[key] != target[key] for key in ('task_id','revision','epoch'))
                    or battle['role'] != 'maa' or battle['status'] != 'running'
                    or battle['payload'].get('contract') != target['contract']
                    or battle['payload'].get('execution_step') not in (None,'battle')):
                raise ValueError('not the current gameplay dispatch')
            store.management.require_active(source_id)
            source = store.current(source_id)
            if (source['contract'].get('current_deliverable') != 'maa_import'
                    or source['status'] != 'completed' or source['control_kind'] != 'import_verified'
                    or source['writes_enabled'] or source['epoch'] != source['task_epoch']):
                raise ValueError('source is not a completed current import')
            # Scope every row to the declared task and its current generation.
            # A different, newer task is never an implicit source candidate.
            row = store._db.execute('SELECT d.dispatch_id FROM workflow_dispatches d '
                "WHERE d.task_id=? AND d.revision=? AND d.epoch=? AND d.role='maa' "
                'ORDER BY d.rowid DESC LIMIT 1',
                (source_id,source['revision'],source['epoch'])).fetchone()
            if row is None:
                raise ValueError('source has no current import result')
        pin = load_current_import_binding(store,source_id,row['dispatch_id'])
        requested = (target['contract'].get('task_spec') or {}).get('stage_code')
        declared = target['contract'].get('target_stage_codes') or []
        if (requested and requested.upper() != pin.stage_code
                or declared and declared != [pin.stage_code]):
            raise ValueError('source is another target')
        _check_requested_mode(target['contract'], pin.difficulty)
        with store.engine._lock:
            for prior in (target,source):
                store.management.require_active(prior['task_id'])
                current = store.current(prior['task_id'])
                if any(current[key] != prior[key] for key in (
                        'revision','epoch','control_seq','writes_enabled','status','control_kind','contract')):
                    raise ValueError('source or gameplay generation changed')
        return pin
    except (OSError,ValueError,TypeError,KeyError,AttributeError,StopIteration,sqlite3.Error,RuntimeError):
        raise ImportBindingError() from None


def load_import_binding(report_path):
    from .conversation_acceptance import import_verification
    try:
        path=receipts._no_links(Path(report_path).absolute()).resolve(strict=True)
        raw,_=receipts._read_stable(path,maximum=32*1024*1024)
        report=receipts._json(raw)
        if (report.get('passed') is not True or type(report.get('verified_import_count')) is not int
                or report['verified_import_count']!=1 or type(report.get('battle_start_submissions')) is not int
                or report['battle_start_submissions']!=0 or len(report['turns'])!=1):
            raise ValueError('not a passed single import')
        turn=report['turns'][0]
        if turn['contract'].get('current_deliverable')!='maa_import':raise ValueError('wrong deliverable')
        task_id=turn['task'].get('id') or turn['task']['task_id']
        profile=receipts._no_links(path.parent/'data').resolve(strict=True)
        database=receipts._no_links(profile/'tasks.sqlite3')
        wal=receipts._no_links(profile/'tasks.sqlite3-wal')
        if wal.exists() and wal.stat().st_size:raise ValueError('source run is not closed')
        db_hash=hashlib.sha256(database.read_bytes()).hexdigest()
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
            db.row_factory=sqlite3.Row
            if db.execute('SELECT 1 FROM workflow_operations WHERE starts<>0 AND submitted_at IS NOT NULL LIMIT 1').fetchone():
                raise ValueError('source import profile already submitted a battle start')
            store=WorkflowStore.__new__(WorkflowStore)
            store._db=db
            store.engine=SimpleNamespace(_db=db,data_dir=profile,_lock=threading.RLock(),_ensure_open=lambda:None)
            actual=import_verification(store,task_id)
            if actual.get('verified') is not True or actual!=turn['import_verification']:
                raise ValueError('report differs from durable import receipt')
            state=store.current(task_id)
            if state['contract'].get('current_deliverable')!='maa_import':raise ValueError('wrong source contract')
            row=db.execute("SELECT d.dispatch_id FROM workflow_dispatches d JOIN workflow_results r "
                "ON d.dispatch_id=r.dispatch_id WHERE d.task_id=? AND d.revision=? AND d.epoch=? "
                "AND d.role='maa' ORDER BY d.rowid DESC LIMIT 1",(task_id,state['revision'],state['epoch'])).fetchone()
            receipt=store.dispatch_result(row['dispatch_id'])
            if receipt.get('queue_receipt_ref'):raise ValueError('single import only')

            pin=_pin_from_receipt(profile,task_id,receipt,actual,str(path),hashlib.sha256(raw).hexdigest())
        if hashlib.sha256(database.read_bytes()).hexdigest()!=db_hash:
            raise ValueError('source database changed')
        if receipts._read_stable(path,maximum=32*1024*1024)[0]!=raw:raise ValueError('source report changed')
        return pin
    except (OSError,ValueError,TypeError,KeyError,AttributeError,StopIteration,sqlite3.Error,RuntimeError):
        raise ImportBindingError() from None

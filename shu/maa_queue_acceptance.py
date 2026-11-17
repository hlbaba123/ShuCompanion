"""Isolated queue acceptance through the same natural-language product entry.

Reports are observations, not authority: a positive reply or an earlier successful
report cannot substitute for the current QueueStore and hashed queue receipt.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import sys
import time


_SECRET_FIELDS = {'api_key','apikey','authorization','password','secret','access_token',
                  'refresh_token','credentials','key_file','api_key_file'}


def _redact(value):
    if isinstance(value, dict):
        return {key:_redact(item) for key,item in value.items()
                if key.casefold().replace('-', '_') not in _SECRET_FIELDS}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return re.sub(r'(?i)\b(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+)', '[redacted]', value)
    return value


def read_request(path):
    """Only original user text is input; expected answers/codes are forbidden."""
    path = Path(path)
    if path.stat().st_size > 256_000:
        raise ValueError('Request file is too large')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if (not isinstance(value, dict) or set(value) != {'request'}
            or not isinstance(value['request'], str) or not value['request'].strip()
            or len(value['request']) > 65_536):
        raise ValueError('Request file must contain only the original request text')
    return value['request']


def snapshot_source(source, destination):
    """Read-only SQLite backup; the product can initialize only this private copy."""
    source, destination = Path(source).resolve(strict=True), Path(destination)
    destination.mkdir(parents=True)
    settings = json.loads((source/'settings.json').read_text(encoding='utf-8-sig'))
    allowed = ('model','base_url','game_model','game_thinking')
    (destination/'settings.json').write_text(json.dumps(
        {key:settings[key] for key in allowed if key in settings}, ensure_ascii=False), encoding='utf-8')
    appearance = source/'pet-appearance.json'
    if appearance.is_file():
        value = json.loads(appearance.read_text(encoding='utf-8-sig'))
        if isinstance(value.get('skin_id'), str):
            (destination/'pet-appearance.json').write_text(
                json.dumps({'skin_id':value['skin_id']}), encoding='utf-8')
    for name in ('tasks.sqlite3','conversation.sqlite3'):
        path = source/name
        if not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError('Source database cannot be a link')
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(destination/name)) as copied:
                original.backup(copied)


def _run_probe(output, project_root):
    from tools.maa_queue_probe import run_probe
    return run_probe(output, project_root=project_root, with_vision=False)


def _run_conversation(*args, timeout=900, **kwargs):
    from .conversation_acceptance import run_conversation_acceptance
    # Compatibility with older product runners is explicit in the report. The
    # current runner exposes its bounded per-turn wait as timeout_seconds.
    if 'timeout_seconds' in inspect.signature(run_conversation_acceptance).parameters:
        kwargs['timeout_seconds'] = timeout
    return run_conversation_acceptance(*args, **kwargs)


def _inspect_queue_evidence(data, original_request):
    """Re-evaluate current durable queue plans/receipts after the controller exits."""
    data = Path(data)
    database = data/'tasks.sqlite3'
    empty = {'verified':False,'code':'queue_evidence_missing','tasks':[],
             'requests':[],'operations':[],'battle_start_submitted':False,
             'forbidden_operations':[]}
    if not database.is_file():
        return empty
    # Locate the actual admitted request from the private run, never the text of
    # a generated reply or a report's claimed task identifier.
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        requests = [dict(row) for row in db.execute(
            'SELECT request_id,task_id,relation,status FROM workflow_messages WHERE text=? ORDER BY rowid',
            (original_request,))]
        operations = []
        for row in db.execute('SELECT operation_id,task_id,dispatch_id,epoch,status,starts,stop_only,action '
                              'FROM workflow_operations WHERE submitted_at IS NOT NULL ORDER BY rowid'):
            item = dict(row); action = json.loads(item.pop('action'))
            guard = action.get('queue_guard') or {}
            window_restore = action == {'kind':'maa_queue_window_restore','action':'focus'}
            item.update(kind=action.get('kind'), phase='focus' if window_restore else guard.get('phase'),
                        target_key=guard.get('target_key'), window_restore=window_restore)
            operations.append(item)
    forbidden = [item for item in operations if item['starts'] or item['stop_only']
                 or (item['kind'] != 'queue_gui' and not item['window_restore'])
                 or item['phase'] not in {'focus','mode','code','add','delete','scroll','options'}]
    result = {**empty, 'requests':requests, 'operations':operations,
              'battle_start_submitted':any(item['starts'] for item in operations),
              'forbidden_operations':forbidden}
    if not requests or any(not item['task_id'] for item in requests):
        return result
    from .core import TaskEngine
    from .workflow_store import WorkflowStore
    from .conversation_acceptance import import_verification
    from .maa_queue_store import QueueStore
    from .leader_plan import read_reference
    engine = TaskEngine(data)
    try:
        workflow = WorkflowStore(engine)
        journal = QueueStore(workflow)
        for task_id in dict.fromkeys(item['task_id'] for item in requests):
            state = workflow.current(task_id)
            run = journal.current(task_id)
            if not run:
                result['tasks'].append({'task_id':task_id,'status':state['status'],'verified':False})
                continue
            verification = import_verification(workflow,task_id)
            verified = (verification.get('verified') is True
                        and verification.get('schema') == 'maa_queue_receipt/v1')
            receipt_ref = verification.get('queue_receipt_ref')
            receipt = read_reference(receipt_ref,data) if receipt_ref else {}
            plan = run['plan']
            result['tasks'].append({'task_id':task_id,'revision':state['revision'],'epoch':state['epoch'],
                'status':state['status'],'verified':verified,'plan_ref':run['plan_ref'],
                'manifest':plan,'queue_receipt_ref':receipt_ref,'queue_receipt':receipt,
                'baseline':run['baseline'],'final_snapshot':run['snapshot'],
                'items':run['items'],'coverage':verification.get('coverage',{})})
        result['verified'] = (bool(result['tasks']) and all(task['verified'] for task in result['tasks'])
                              and not forbidden and result['battle_start_submitted'] is False)
        result['code'] = ('queue_import_verified' if result['verified'] else
                          'forbidden_input_submitted' if forbidden else 'queue_evidence_unverified')
        return result
    finally:
        engine.close()


def _usage(report):
    metrics = list(report.get('intent_metrics') or [])
    for values in (report.get('role_metrics') or {}).values():
        metrics.extend(values)
    numbers = {}
    for metric in metrics:
        for key,value in (metric.get('usage') or {}).items():
            if type(value) in (int,float):
                numbers[key] = numbers.get(key,0) + value
    return {'model_requests':len(metrics),'tokens':numbers,
            'cost':{'amount':None,'currency':None,'status':'provider_did_not_return_billing_amount'}}


def run_queue_acceptance(project_root: Path, resource_root: Path, output: Path,
                         key_file: Path, source_data: Path, *, request_file: Path | None=None,
                         mode='probe', allow_queue_import=False, executable: Path | None=None,
                         timeout=900, version: str) -> int:
    """Run one import-only request, or a default zero-input/no-model probe.

    To test a frozen build, invoke that EXE's --maa-queue-test entry. ``executable``
    is a provenance check, never a license to launch an unrelated old binary.
    """
    for part in (Path(output), *Path(output).parents):
        if part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction()):
            raise ValueError('Acceptance output cannot contain links')
    project_root, output = Path(project_root).resolve(), Path(output).resolve()
    artifacts = (project_root/'artifacts').resolve()
    if output == artifacts or not output.is_relative_to(artifacts):
        raise ValueError('Acceptance output must be inside project artifacts')
    if output.exists():
        raise FileExistsError('Acceptance output already exists')
    if mode not in {'probe','component','natural'} or type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError('Invalid acceptance mode or timeout')
    output.mkdir(parents=True)
    started = time.monotonic()
    report = {'schema':'maa_queue_acceptance/v1','mode':mode,'passed':False,
        'code':'acceptance_incomplete','queue_import_verified':False,
        'scope':'MAA queue import only; no start/stop/game input',
        'runtime':{'frozen':bool(getattr(sys,'frozen',False)), 'executable':sys.executable,
                   'version':version,'resource_root':str(resource_root)},
        'coverage':{'native_snapshot':'pending','natural_import':'pending',
                    'component_import':'not_implemented','multiple_targets':'pending',
                    'multiple_window_sizes':'pending',
                    'long_list':'pending','difficulty_3':'pending','owned_row_delete':'pending',
                    'final_exe':'pending'},
        'battle_start_submitted':False,'battle_start_checked':False,
        'key_copied':False,'user_data_modified':False,'gui_inputs':0,'operations':[]}
    try:
        # These gates precede importing Controller/providers and reading Key.
        if mode != 'probe' and allow_queue_import is not True:
            report['code'] = 'import_opt_in_required'
            return 2
        if executable is not None and Path(executable).resolve() != Path(sys.executable).resolve():
            report['code'] = 'invoke_requested_executable_directly'
            return 2
        if mode == 'component':
            report['code'] = 'component_harness_not_implemented'
            return 2
        if mode == 'probe':
            probe = _run_probe(output/'probe',project_root)
            report.update(probe=probe,passed=(probe.get('passed') is True
                and probe.get('gui_inputs') == 0 and probe.get('model_requests') == 0),
                code=probe.get('code','probe_incomplete'))
            report['coverage']['native_snapshot'] = 'passed' if report['passed'] else 'pending'
            return 0 if report['passed'] else 1
        if request_file is None:
            report['code'] = 'original_request_required'
            return 2
        question = read_request(request_file)
        report['request'] = {'text':question,'sha256':hashlib.sha256(question.encode('utf-8')).hexdigest()}
        source_copy = output/'source-snapshot'
        snapshot_source(source_data,source_copy)
        questions = output/'questions.json'
        questions.write_text(json.dumps([question], ensure_ascii=False), encoding='utf-8')
        conversation = output/'conversation'
        exit_code = _run_conversation(project_root,resource_root,conversation,questions,
            Path(key_file),source_copy,version=version,allow_maa_import=True,
            allow_maa_queue_import=True,timeout=timeout)
        observed = json.loads((conversation/'report.json').read_text(encoding='utf-8'))
        # Sanitize even diagnostic reports; exceptions and HTTP payloads are not
        # copied into the aggregate report. The key itself is never opened here.
        (conversation/'report.json').write_text(json.dumps(_redact(observed),ensure_ascii=False,indent=2),encoding='utf-8')
        durable = _inspect_queue_evidence(conversation/'data',question)
        report.update(code=durable['code'],queue_import_verified=durable['verified'],
            evidence=durable,operations=durable['operations'],
            battle_start_submitted=durable['battle_start_submitted'],
            battle_start_checked=bool((conversation/'data'/'tasks.sqlite3').is_file()),
            gui_inputs=len(durable['operations']),usage=_usage(observed),production_exit_code=exit_code)
        report['passed'] = bool(exit_code == 0 and durable['verified'])
        report['coverage']['natural_import'] = 'passed' if report['passed'] else 'pending'
        tasks = durable.get('tasks') or []
        targets = [target for task in tasks for target in (task.get('manifest') or {}).get('targets',[])]
        report['target_count'] = len(targets)
        report['target_modes'] = [{'target_key':target['target_key'],'stage_code':target['stage_code'],
            'difficulty':target['difficulty'],'source_difficulty':target['source_difficulty']}
            for target in targets]
        if report['passed']:
            if len(targets) > 1:
                report['coverage']['multiple_targets'] = 'passed'
            if all(task.get('baseline') and task.get('final_snapshot') for task in tasks):
                report['coverage']['native_snapshot'] = 'passed'
            dual_sources = {}
            for target in targets:
                if target['source_difficulty'] == 3:
                    key = (target['stage_id'],target['mystery_code'])
                    dual_sources.setdefault(key,set()).add(target['difficulty'])
            if any(modes == {1,2} for modes in dual_sources.values()):
                report['coverage']['difficulty_3'] = 'passed'
            if any(item.get('phase') == 'delete' and item.get('status') == 'verified'
                   for item in durable['operations']):
                report['coverage']['owned_row_delete'] = 'passed'
        if report['passed'] and report['runtime']['frozen']:
            report['coverage']['final_exe'] = 'passed'
        return 0 if report['passed'] else 1
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as error:
        report['code'] = 'acceptance_failed'
        report['error_type'] = type(error).__name__
        return 1
    finally:
        report['elapsed_seconds'] = round(time.monotonic()-started,3)
        report['timeout_seconds'] = timeout
        (output/'report.json').write_text(json.dumps(_redact(report),ensure_ascii=False,indent=2),encoding='utf-8')

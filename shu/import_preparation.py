"""Bind a verified code receipt to a current full-document candidate.

This read-only preparation boundary never starts MAA or a model. Public IO is
outside SQLite writers. Cached documents remain source/roster-bound; a new
dispatch receives new provenance instead of inheriting GUI authority.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import traceback
import uuid

from .code_delivery import CodeDeliveryStore
from .copilot_catalog import parse_candidate
from .copilot_pool import CopilotPool
from .core import ActionRejected, TaskNotFound
from .leader_plan import PlanStore, activity_query_matches, read_reference
from .maa_roster_tools import RosterTools
from .providers import ProviderConfig
from .research_tools import MAX_BYTES, ResearchTools
from .roster_match import requirements_hash, roster_ref
from .workflow_store import _encode


_PUBLIC_SOURCE_MAX_BYTES = 32 * 1024 * 1024


class ImportBoundaryTaskNotFound(TaskNotFound):
    """A task identity failure with a private, credential-free local trace."""
    code = 'import_boundary_task_not_found'

    def __init__(self, phase, diagnostic_ref):
        self.phase, self.diagnostic_ref = phase, diagnostic_ref
        detail = Path(diagnostic_ref['path']).name if diagnostic_ref else '诊断文件未能保存'
        super().__init__(f'导入交接阶段 {phase} 的任务身份检查失败；{detail}。已停止，未绕过检查。')


def _diagnostic_engine_scope(engine, task_id, source_task_id):
    """Inspect only fixed database identity/status fields, with a short lock wait."""
    result = {'available':False}
    lock = getattr(engine,'_lock',None)
    if lock is None or not lock.acquire(timeout=.2):
        return {**result,'reason':'database_lock_unavailable'}
    try:
        db = engine._db
        database = next((row[2] for row in db.execute('PRAGMA database_list') if row[1]=='main'),None)
        has_workflow = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_tasks'").fetchone() is not None
        def identity(identifier):
            row = db.execute('SELECT id,version,status FROM tasks WHERE id=?',(identifier,)).fetchone()
            workflow = (db.execute('SELECT revision,epoch,writes_enabled FROM workflow_tasks WHERE task_id=?',
                                   (identifier,)).fetchone() if has_workflow else None)
            return {'task_present':row is not None,'workflow_present':workflow is not None,
                    'version':row['version'] if row else None,'status':row['status'] if row else None,
                    'revision':workflow['revision'] if workflow else None,
                    'epoch':workflow['epoch'] if workflow else None}
        return {'available':True,'database':database,'data_directory':str(engine.data_dir),
                'workflow_table_present':has_workflow,
                'task':identity(task_id),'source':identity(source_task_id)}
    except Exception:
        return {**result,'reason':'database_identity_unavailable'}
    finally:
        lock.release()


def _publish_import_diagnostic(directory, diagnostic):
    raw,digest = _encode(diagnostic)
    target = Path(directory).resolve()/'evidence'/('import-boundary-'+digest+'.json')
    target.parent.mkdir(parents=True,exist_ok=True)
    raw = raw.encode('utf-8')
    if target.exists():
        if target.read_bytes() != raw:
            raise ValueError('已有导入诊断内容已变化')
    else:
        temporary=target.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        with temporary.open('xb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,target)
    return {'path':str(target),'sha256':digest}


@contextmanager
def import_boundary(workflow, directory, context, phase, *, source_task_id=None):
    """Trace TaskNotFound without logging raw exceptions, locals or transport data.

    This does not repair/recreate tasks, retry IO, relax a guard, or allow GUI
    continuation. Nested boundaries preserve the innermost failure receipt.
    """
    try:
        yield
    except ImportBoundaryTaskNotFound:
        raise
    except TaskNotFound as error:
        engine=workflow.engine
        lifecycle_engine=getattr(getattr(workflow,'management',None),'engine',None)
        source_task_id=(source_task_id or context.get('contract',{}).get('source_task_id')
                        or context['task_id'])
        diagnostic={'schema':'import_boundary_diagnostic/v1','phase':phase,
            'exception_type':type(error).__name__,'task_id':context['task_id'],
            'revision':context.get('revision'),'epoch':context.get('epoch'),
            'dispatch_id':context.get('dispatch_id'),'source_task_id':source_task_id,
            'source_is_current_task':source_task_id==context['task_id'],
            'execution_directory':str(Path(directory).resolve()),
            'workflow_scope':_diagnostic_engine_scope(engine,context['task_id'],source_task_id),
            'lifecycle_scope':_diagnostic_engine_scope(lifecycle_engine,context['task_id'],source_task_id),
            'scope_alignment':{'lifecycle_engine_is_workflow_engine':lifecycle_engine is engine,
                'workflow_connection_is_engine_connection':getattr(workflow,'_db',None) is engine._db,
                'execution_directory_matches_engine':Path(directory).resolve()==engine.data_dir.resolve()},
            'traceback':[{'file':Path(frame.filename).name,'function':frame.name,'line':frame.lineno}
                         for frame in traceback.extract_tb(error.__traceback__)[-24:]]}
        reference=None
        try:
            reference=_publish_import_diagnostic(engine.data_dir,diagnostic)
        except Exception:
            pass  # Diagnostics never replace the failure with permission to continue.
        raise ImportBoundaryTaskNotFound(phase,reference) from error


def ensure_preparation_ownership(engine):
    """Upgrade the old three-column cache without guessing task ownership.

    Old material has no owner field. Its download path supplies only a candidate;
    the immutable cache key must also match a real contract revision. Unprovable
    rows remain in place with an explicit error and cannot silently be purged.
    Callers already hold the engine's transaction/lock.
    """
    db = engine._db
    db.execute('CREATE TABLE IF NOT EXISTS import_preparations ('
               'cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, payload_hash TEXT NOT NULL)')
    columns = {row['name'] for row in db.execute('PRAGMA table_info(import_preparations)')}
    for name, kind in (('owner_task_id', 'TEXT'), ('source_task_id', 'TEXT'),
                       ('owner_revision', 'INTEGER'), ('ownership_error', 'TEXT')):
        if name not in columns:
            db.execute('ALTER TABLE import_preparations ADD COLUMN ' + name + ' ' + kind)
    root = engine.data_dir.resolve() / 'research'
    for row in db.execute('SELECT * FROM import_preparations').fetchall():
        owner, source, revision = row['owner_task_id'], row['source_task_id'], row['owner_revision']
        try:
            material = json.loads(row['payload'])
            if _encode(material)[1] != row['payload_hash']:
                raise ValueError('导入准备资料哈希不一致')
            binding, document = material['source_binding'], material['document_ref']
            actual_source = binding['source_task_id']
            if not isinstance(actual_source, str) or not actual_source:
                raise ValueError('导入准备缺少结构化来源任务')
            if source is not None and source != actual_source:
                raise ValueError('导入准备来源列与原资料不一致')
            source = actual_source
            if owner is None:
                relative = Path(document['path']).absolute().relative_to(root)
                if len(relative.parts) < 3:
                    raise ValueError('导入准备下载路径无法证明所有者')
                candidate = relative.parts[0]
                candidates = db.execute('SELECT revision FROM workflow_contracts WHERE task_id=?',
                                        (candidate,)).fetchall()
            else:
                candidate = owner
                candidates = [(revision,)]
            matches = [item[0] for item in candidates if type(item[0]) is int and _encode({
                'task_id': candidate, 'revision': item[0], 'source_task_id': source,
                'step_id': binding['step_id'], 'slot': binding['slot']})[1] == row['cache_key']]
            if len(matches) != 1 or not db.execute('SELECT 1 FROM tasks WHERE id=?', (candidate,)).fetchone():
                raise ValueError('导入准备缓存身份无法证明所属任务及版本')
            owner, revision = candidate, matches[0]
            error = None
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            error = '无法核实导入准备所有权：' + str(exc)
        db.execute('UPDATE import_preparations SET owner_task_id=?,source_task_id=?,owner_revision=?,ownership_error=? '
                   'WHERE cache_key=?', (owner, source, revision, error, row['cache_key']))


def _execution_stage(stage):
    # Full public-directory provenance is pinned in import-source and checked by
    # validate_use. Execution identity must not recursively adopt those large
    # catalog files as GUI result artifacts.
    return {key: copy.deepcopy(value) for key, value in stage.items()
            if key not in {'source_refs', 'opening_source_refs', 'difficulty_domain_source_refs'}}


class ImportPreparation:
    def __init__(self, workflow, directory):
        self.workflow, self.engine = workflow, workflow.engine
        self.directory = Path(directory).resolve()
        if self.directory != self.engine.data_dir.resolve():
            raise ValueError('执行候选必须使用当前工作流资料目录')
        self.plans = PlanStore(workflow)
        self.codes = CodeDeliveryStore(workflow, self.directory)
        self.rosters = RosterTools(workflow, self.directory)
        with self.engine._transaction():
            ensure_preparation_ownership(self.engine)

    def _guard(self, context, cancel=None, *, research_dispatch=True):
        if cancel is not None and cancel.is_set():
            raise ActionRejected('执行候选准备已取消')
        current = self.workflow.current(context['task_id'])
        if (current['revision'] != context['revision'] or current['epoch'] != context['epoch']
                or not current['writes_enabled'] or current['contract'] != context['contract']
                or current['status'] in {'paused', 'cancelled', 'completed', 'failed', 'needs_input'}):
            raise ActionRejected('当前任务、合同或执行代际已变化')
        contract = current['contract']
        if (contract.get('current_deliverable') not in {'maa_import', 'gameplay'}
                or contract.get('task_spec', {}).get('kind') == 'lookup'
                or contract.get('profile_id', 'local-default') != 'local-default'):
            raise ActionRejected('当前合同不允许准备导入候选或本地档案不一致')
        dispatch = self.workflow.get_dispatch(context['dispatch_id'])
        if (dispatch['task_id'] != current['task_id'] or dispatch['revision'] != current['revision']
                or dispatch['epoch'] != current['epoch'] or research_dispatch and dispatch['role'] != 'research'
                or dispatch['status'] != 'running'
                or ('contract' in dispatch['payload'] and dispatch['payload']['contract'] != contract)):
            raise ActionRejected('准备候选需要当前活跃的研究委派' if research_dispatch else '候选校验需要当前活跃委派')
        return current

    def _linked_plan(self, context, source_task_id=None):
        source_id = context['contract'].get('source_task_id')
        own_import = not source_id and context['contract'].get('current_deliverable') == 'maa_import'
        if own_import:
            source_id = context['task_id']
        if (not isinstance(source_id, str) or not source_id
                or (source_id == context['task_id'] and not own_import)
                or source_task_id is not None and source_task_id != source_id):
            raise ActionRejected('来源必须为当前合同明确关联的任务')
        self.workflow.management.require_active(source_id)
        source = self.workflow.current(source_id)
        plan = self.plans.current(source_id)
        if not plan or plan['revision'] != source['revision']:
            raise ActionRejected('关联来源当前版本没有真实计划，需重新核对来源')
        if context['contract'].get('source_revision', source['revision']) != source['revision']:
            raise ActionRejected('关联来源合同版本已变化')
        from .chapter_scope import resolved_contract
        structure = read_reference(plan['structure_ref'], self.directory)
        contract = resolved_contract(context['contract'], structure)
        requested_activity = contract.get('activity_name')
        if requested_activity and not activity_query_matches(requested_activity, plan.get('activity_name', '')):
            raise ActionRejected('当前活动与关联来源范围冲突')
        # Hash-check the original structure as well as the SQLite plan payload.
        if structure.get('stages') != plan['stages']:
            raise ValueError('来源计划与真实目录结构不一致')
        if (contract.get('import_scope') is not None or own_import
                and (len(plan['steps']) != 1 or plan.get('import_stage_keys') is not None)):
            from .maa_queue_plan import ordered_import_candidates, select_import_stages, select_research_stages
            ordered = ordered_import_candidates(structure)
            actual = [step['stage_key'] for step in plan['steps']]
            if not own_import and (contract.get('import_scope') or {}).get('kind') == 'all':
                ordered = [stage for stage in ordered if stage['stage_key'] in actual]
            expected = [stage['stage_key'] for stage in select_import_stages(ordered,contract)]
            if own_import:
                queried = [stage['stage_key'] for stage in select_research_stages(ordered,contract)]
                if plan.get('import_stage_keys') != expected or plan['ordered_stage_keys'] != queried or actual != queried:
                    raise ActionRejected('导入计划必须绑定本次合同的查询范围与导入目标')
            if set(expected) - set(actual):
                raise ActionRejected('导入计划必须完整绑定本次合同选择的关卡和难度')
            return source, plan, set(expected)
        targets = context['contract'].get('target_stage_codes') or []
        target = context['contract'].get('task_spec', {}).get('stage_code')
        if target:
            if targets and target.casefold() not in {code.casefold() for code in targets}:
                raise ActionRejected('当前合同的目标关卡范围冲突')
            targets = targets or [target]
        wanted = {code.casefold() for code in targets}
        available = {stage.get('display_code', '').casefold() for stage in plan['stages']}
        if wanted - available:
            raise ActionRejected('当前目标关卡超出关联来源计划范围')
        return source, plan, {stage['stage_key'] for stage in plan['stages']
                             if not wanted or stage.get('display_code','').casefold() in wanted}

    def _read_code(self, source, plan, step, slot):
        ref = (step.get('result') or {}).get(slot + '_ref')
        if not ref or step['status'] not in {'primary_only', 'two_verified'}:
            raise ActionRejected('所选步骤没有已核验的' + slot + '作业凭据')
        code = self.codes.read_code(ref, verify_current=True)
        if (code['task_id'] != source['task_id'] or code['revision'] != source['revision']
                or code['step_id'] != step['step_id']):
            raise ActionRejected('作业凭据不属于来源当前步骤')
        stage = next(stage for stage in plan['stages'] if stage['stage_key'] == step['stage_key'])
        if code['resolved_stage'] != stage:
            raise ActionRejected('作业凭据与来源关卡身份不一致')
        return ref, code, stage

    def options(self, context):
        """Expose actual selectable identities, retaining same-code mode variants."""
        with import_boundary(self.workflow,self.directory,context,'import_options'):
            return self._options(context)

    def _options(self, context):
        self._guard(context)
        source, plan, wanted = self._linked_plan(context)
        rows = []
        for step in plan['steps']:
            stage = next(stage for stage in plan['stages'] if stage['stage_key'] == step['stage_key'])
            if stage['stage_key'] not in wanted:
                continue
            item = {'step_id': step['step_id'], 'stage_code': stage.get('display_code'),
                    'group_id':stage.get('group_id'),
                    'group_label':next((g['label'] for g in plan['groups'] if g['group_id']==stage.get('group_id')),None),
                    'group_ordinal':next((g.get('catalog_ordinal') for g in plan['groups'] if g['group_id']==stage.get('group_id')),None),
                    'difficulty': stage.get('difficulty'), 'stage_key': stage['stage_key'],
                    'primary_code': None, 'backup_code': None, 'verified': False}
            for slot in ('primary', 'backup'):
                if (step.get('result') or {}).get(slot + '_ref'):
                    _, code, _ = self._read_code(source, plan, step, slot)
                    item[slot + '_code'] = code['code']
            item['verified'] = item['primary_code'] is not None
            rows.append(item)
        self._guard(context)
        if self.workflow.current(source['task_id'])['revision'] != source['revision']:
            raise ActionRejected('读取选项期间来源版本已变化')
        return {'source_task_id': source['task_id'], 'source_revision': source['revision'],
                'activity_name': plan.get('activity_name'), 'steps': rows,
                'scope_resolved': context['contract'].get('import_scope') is not None}

    def _source(self, context, source_task_id, step_id, slot):
        if slot not in {'primary', 'backup'}:
            raise ValueError('作业选择必须为 primary 或 backup')
        source, plan, wanted = self._linked_plan(context, source_task_id)
        steps = [step for step in plan['steps'] if step['step_id'] == step_id]
        if len(steps) != 1:
            raise ActionRejected('步骤不在关联来源当前计划中')
        step = steps[0]
        ref, code, stage = self._read_code(source, plan, step, slot)
        if stage['stage_key'] not in wanted:
            raise ActionRejected('所选步骤超出当前目标范围')
        self._verify_stage_sources(stage)
        with self.engine._lock:
            row = self.engine._db.execute('SELECT * FROM code_receipts WHERE id=?', (ref['receipt_id'],)).fetchone()
            payload = json.loads(row['payload'])
            if _encode(payload)[1] != ref['sha256']:
                raise ValueError('来源代码凭据已变化')
        screen = read_reference(payload['screen_ref'], self.directory, maximum=32 * 1024 * 1024)
        pool_ref = screen['pool_ref']
        pool_path = Path(pool_ref['path']).resolve()
        pool = CopilotPool(pool_path.parent.parent).read(pool_ref)
        candidates = [item for item in pool['candidates'] if item.get('candidate_locator') == payload['candidate_locator']]
        if len(candidates) != 1:
            raise ValueError('来源候选身份不唯一')
        binding = {'source_task_id': source_task_id, 'source_revision': source['revision'],
                   'source_contract_sha256': _encode(source['contract'])[1], 'step_id': step_id, 'slot': slot,
                   'code_ref': ref, 'resolved_stage': stage, 'structure_ref': plan['structure_ref'],
                   'step_result': step['result'], 'pool_ref': pool_ref, 'screen_ref': payload['screen_ref'],
                   'roster_ref': screen['roster_ref']}
        return binding, code, candidates[0]

    def _verify_stage_sources(self, stage):
        """Check projected public bytes without adopting large files as GUI artifacts."""
        checked = set()
        for field in ('source_refs', 'opening_source_refs', 'difficulty_domain_source_refs'):
            for ref in stage.get(field, []):
                if not isinstance(ref, dict) or not {'path', 'sha256'}.intersection(ref):
                    continue
                if not isinstance(ref.get('path'), str) or not isinstance(ref.get('sha256'), str):
                    raise ValueError('公开关卡来源文件引用无效')
                identity = (ref['path'], ref['sha256'])
                if identity in checked:
                    continue
                path = Path(ref['path']).resolve(strict=True)
                if (not path.is_relative_to(self.directory) or not path.is_file()
                        or path.stat().st_size > _PUBLIC_SOURCE_MAX_BYTES):
                    raise ValueError('公开关卡来源文件越界或超过大小限制')
                with path.open('rb') as stream:
                    raw = stream.read(_PUBLIC_SOURCE_MAX_BYTES + 1)
                if len(raw) > _PUBLIC_SOURCE_MAX_BYTES or hashlib.sha256(raw).hexdigest() != ref['sha256']:
                    raise ValueError('公开关卡来源文件内容已变化')
                checked.add(identity)

    def _publish(self, kind, value):
        raw, digest = _encode(value)
        raw = raw.encode('utf-8')
        target = (self.directory / 'evidence' / (kind + '-' + digest + '.json')).resolve()
        if not target.is_relative_to(self.directory):
            raise ValueError('执行候选证据越出资料目录')
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != raw:
                raise ValueError('已有执行候选证据已变化')
        else:
            temporary = target.with_suffix('.' + uuid.uuid4().hex + '.tmp')
            with temporary.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        return {'path': str(target), 'sha256': digest}

    def _document(self, ref):
        body = read_reference(ref, self.directory, maximum=MAX_BYTES)
        if (not isinstance(body, dict) or not isinstance(body.get('stage_name'), str)
                or not body['stage_name'].strip() or not isinstance(body.get('actions'), list)
                or not body['actions'] or any(not isinstance(action, dict) for action in body['actions'])):
            raise ValueError('完整作业缺少有效关卡或动作')
        return body

    def validate_use(self, context, record) -> None:
        """Revalidate a prepared receipt immediately before each GUI submission.

        Any current role may inspect the receipt; this grants no GUI authority.
        The original research dispatch may be complete. Its task/revision/epoch
        and all pinned source material must still belong to the current task.
        No download, model call or SQLite writer runs in this check.
        """
        self.submission_guard(context, record)

    def submission_guard(self, context, record):
        """Validate external bytes, then return facts for the atomic SQLite gate."""
        with self.engine._lock:
            self._guard(context, research_dispatch=False)
            result = self._verify_material(context, record)
            self._guard(context, research_dispatch=False)
            return result

    def _verify_material(self, context, record):
        """Read immutable preparation/source/Box facts without granting authority.

        Live submissions additionally require both guards in submission_guard.
        A stopped research generation may use this only for display verification.
        """
        record = copy.deepcopy(record)
        with self.engine._lock:
            if not isinstance(record, dict) or any(record.get(key) != context[key]
                    for key in ('task_id', 'revision', 'epoch')):
                raise ActionRejected('准备作业不属于当前任务版本或执行代际')
            execution = record.get('source_binding')
            if not isinstance(execution, dict):
                raise ValueError('准备作业缺少来源绑定')
            binding = read_reference(execution.get('evidence_ref'), self.directory)
            compact = {key: copy.deepcopy(binding[key]) for key in (
                'source_task_id', 'source_revision', 'step_id', 'slot', 'code_ref', 'roster_ref')}
            compact['evidence_ref'] = execution['evidence_ref']
            if execution != compact:
                raise ActionRejected('准备作业的来源绑定已替换')
            current, code, _ = self._source(context, binding['source_task_id'], binding['step_id'], binding['slot'])
            if current != binding:
                raise ActionRejected('使用前来源合同、步骤、代码凭据或本地 Box 已变化')
            document_ref = {key: record.get(key) for key in ('path', 'sha256')}
            cache_key = _encode({'task_id': context['task_id'], 'revision': context['revision'],
                'source_task_id': binding['source_task_id'], 'step_id': binding['step_id'], 'slot': binding['slot']})[1]
            cached = self.engine._db.execute('SELECT payload,payload_hash FROM import_preparations WHERE cache_key=?',
                                             (cache_key,)).fetchone()
            material = json.loads(cached['payload']) if cached else None
            if (not cached or _encode(material)[1] != cached['payload_hash']
                    or material != {'source_binding': binding, 'document_ref': document_ref}):
                raise ActionRejected('准备作业与已提交的原始来源及完整文档不一致')
            body = self._document(document_ref)
            stage = code['resolved_stage']
            goal_stage = record.get('goal_stage')
            expected_goal = {'task_id': context['task_id'], 'revision': context['revision'], 'epoch': context['epoch'],
                'goal': context['contract'].get('goal', ''), 'query': stage.get('query_key'),
                'stage_id': body['stage_name'], 'level_id': stage.get('level_id', stage.get('stage_id')),
                'code': stage.get('display_code'), 'difficulty': stage['difficulty'],
                'resolved_stage': _execution_stage(stage), 'source_binding': execution}
            if (not isinstance(goal_stage, dict)
                    or {key: value for key, value in goal_stage.items() if key != 'evidence_ref'} != expected_goal
                    or read_reference(goal_stage.get('evidence_ref'), self.directory) != expected_goal):
                raise ActionRejected('准备作业的当前目标证据已变化')
            if (record.get('copilot_id') != str(code['id'])
                    or record.get('source_url') != f"https://prts.maa.plus/copilot/get/{code['id']}"
                    or record.get('stage_name') != body['stage_name']
                    or record.get('action_count') != len(body['actions'])
                    or record.get('difficulty') != stage['difficulty']
                    or record.get('roster_ref') != binding['roster_ref']):
                raise ActionRejected('准备作业不再对应原步骤所选作业及关卡')
            assessment = self.rosters.assess('local-default', document_ref)
            if (assessment['classification'] != 'satisfied'
                    or assessment.get('roster_ref') != binding['roster_ref']):
                raise ActionRejected('使用前完整作业未通过原本地 Box 核验')
            source = self.workflow.current(binding['source_task_id'])
            if (source['revision'] != binding['source_revision']
                    or _encode(source['contract'])[1] != binding['source_contract_sha256']
                    or roster_ref(self.rosters.current('local-default') or {}) != binding['roster_ref']):
                raise ActionRejected('使用前来源合同或本地 Box 已变化')
            return {'task_id': context['task_id'], 'revision': context['revision'], 'epoch': context['epoch'],
                    'dispatch_id': context['dispatch_id'], 'contract_sha256': _encode(context['contract'])[1],
                    'source_binding': binding, 'cache_key': cache_key, 'cache_payload_hash': cached['payload_hash']}

    def prepare_many(self, context, selections, *, cancel_event=None):
        """Prepare a bounded ordered batch without sharing authority between items.

        Each entry still goes through the existing source, contract, file and
        current-Box checks. Results are evidence only, never GUI permission.
        """
        if not isinstance(selections, list) or not 1 <= len(selections) <= 100:
            raise ValueError('每次研究准备需要 1 至 100 个明确目标')
        identities = []
        for item in selections:
            if (not isinstance(item, dict) or set(item) != {'source_task_id','step_id','slot'}
                    or any(not isinstance(item[key], str) or not item[key].strip()
                           for key in ('source_task_id','step_id'))
                    or item['slot'] not in {'primary','backup'}):
                raise ValueError('多作业准备目标格式无效')
            identities.append((item['source_task_id'],item['step_id'],item['slot']))
        if len(identities) != len({(source,step) for source,step,_ in identities}):
            raise ValueError('多作业准备不能重复同一目标')
        self._guard(context, cancel_event)
        options = self.options(context)
        ordered = {step['step_id']:index for index,step in enumerate(options['steps'])}
        if (any(source != options['source_task_id'] or step not in ordered
                for source,step,_ in identities)
                or [ordered[step] for _,step,_ in identities]
                    != sorted(ordered[step] for _,step,_ in identities)):
            raise ActionRejected('准备目标必须属于当前关联来源，并遵循真实计划顺序')
        results = []
        for item in selections:
            self._guard(context, cancel_event)
            results.append(self.prepare(context, cancel_event=cancel_event, **item))
        self._guard(context, cancel_event)
        return results

    def prepare_queue_plan(self, context, selections, *, cancel_event=None):
        """Publish the complete ordered plan only after all selected jobs are prepared."""
        with import_boundary(self.workflow,self.directory,context,'prepare_queue_plan'):
            return self._prepare_queue_plan(context,selections,cancel_event=cancel_event)

    def _prepare_queue_plan(self, context, selections, *, cancel_event=None):
        from .maa_queue_plan import build_queue_plan

        self._guard(context, cancel_event)
        source, source_plan, _ = self._linked_plan(context)
        reference = source_plan['structure_ref']
        structure = read_reference(reference, self.directory)
        slots = {selection['slot'] for selection in selections}
        if len(slots) != 1:
            raise ValueError('批量准备需要明确的主选或备选策略')
        prepared = []
        for offset in range(0, len(selections), 100):
            prepared.extend(self.prepare_many(context, selections[offset:offset+100],
                                              cancel_event=cancel_event))
        contract = {**context['contract'], 'selected_slot': next(iter(slots)),
                    'source_revision': source['revision']}
        plan = build_queue_plan({**context, 'contract': contract}, reference, structure, prepared,
                                source_stage_keys=[step['stage_key'] for step in source_plan['steps']])
        self._guard(context, cancel_event)
        with self.engine._lock:
            self._guard(context, cancel_event)
            self.workflow.management.require_active(source['task_id'])
            if self.workflow.current(source['task_id'])['revision'] != source['revision']:
                raise ActionRejected('准备期间来源任务已修订')
            current_box = roster_ref(self.rosters.current('local-default') or {})
            if any(target['roster_ref'] != current_box for target in plan['targets']):
                raise ActionRejected('准备期间本地 Box 已变化')
            plan_ref = self._publish('maa-queue-plan', plan)
        return plan, plan_ref

    def prepare(self, context, *, source_task_id, step_id, slot='primary', research=None, cancel_event=None):
        with import_boundary(self.workflow,self.directory,context,'prepare_candidate',source_task_id=source_task_id):
            return self._prepare(context,source_task_id=source_task_id,step_id=step_id,slot=slot,
                                 research=research,cancel_event=cancel_event)

    def _prepare(self, context, *, source_task_id, step_id, slot='primary', research=None, cancel_event=None):
        self._guard(context, cancel_event)
        binding, code, summary = self._source(context, source_task_id, step_id, slot)
        cache_key = _encode({'task_id': context['task_id'], 'revision': context['revision'],
                             'source_task_id': source_task_id, 'step_id': step_id, 'slot': slot})[1]
        with self.engine._lock:
            cached = self.engine._db.execute('SELECT * FROM import_preparations WHERE cache_key=?', (cache_key,)).fetchone()
        expected_root = (self.directory / 'research' / context['task_id'] / context['dispatch_id']).resolve()
        if not expected_root.is_relative_to(self.directory):
            raise ValueError('当前委派下载目录越出资料目录')
        if cached:
            material = json.loads(cached['payload'])
            if _encode(material)[1] != cached['payload_hash'] or material['source_binding'] != binding:
                raise ActionRejected('已准备作业的来源版本、步骤或当前 Box 已变化')
            document_ref = material['document_ref']
            body = self._document(document_ref)
        else:
            research = research or ResearchTools(ProviderConfig(), expected_root,
                                                   public_cache_root=self.directory / 'public-cache')
            if Path(research.cache_dir).resolve() != expected_root:
                raise ValueError('下载工具必须绑定当前研究委派目录')
            research.set_pool_directory(Path(binding['pool_ref']['path']).resolve().parent.parent)
            research.register_pool_ref(binding['pool_ref'])
            self._guard(context, cancel_event)
            source_url = f"https://prts.maa.plus/copilot/get/{code['id']}"
            result = research.download_copilot(source_url)
            self._guard(context, cancel_event)
            document_ref = {'path': result.get('path'), 'sha256': result.get('sha256')}
            if (result.get('ok') is not True or result.get('tool') != 'download_copilot'
                    or result.get('source_url') != source_url or source_url not in research.discovered_copilot_urls
                    or not Path(document_ref['path']).resolve().is_relative_to(expected_root)):
                raise ValueError('下载结果缺少当前真实来源绑定')
            body = self._document(document_ref)
            # The immutable cache receipt must never outlive unflushed bytes.
            with Path(document_ref['path']).open('r+b') as stream:
                stream.flush()
                os.fsync(stream.fileno())
            material = {'source_binding': binding, 'document_ref': document_ref}
        full = parse_candidate({'id': code['id'], 'content': json.dumps(body, ensure_ascii=False)})
        if not full or full['stage_name'].casefold() != summary['stage_name'].casefold():
            raise ValueError('完整作业关卡与已登记候选不一致')
        source_difficulty = body.get('difficulty', 1)
        if type(source_difficulty) is not int or source_difficulty not in (1, 2, 3):
            raise ValueError('完整作业正文难度必须为普通、突袭或二者')
        for field in ('inferred_difficulty', 'difficulty_basis', 'difficulty_proof'):
            if field in summary:
                full[field] = copy.deepcopy(summary[field])
        if requirements_hash(full) != requirements_hash(summary):
            raise ValueError('完整作业难度或干员要求与代码凭据不一致')
        assessment = self.rosters.assess('local-default', document_ref)
        if assessment['classification'] != 'satisfied' or assessment['roster_ref'] != binding['roster_ref']:
            raise ActionRejected('完整作业未通过当前本地 Box 核验')
        # Recheck source files after IO; the writer below checks only DB facts.
        if self._source(context, source_task_id, step_id, slot)[0] != binding:
            raise ActionRejected('准备期间来源关卡或凭据已变化')
        self._guard(context, cancel_event)
        stage = code['resolved_stage']
        # Source pools/screens can exceed the legacy 2 MB adoption limit. Keep
        # their full binding in a separately hashed preparation receipt rather
        # than recursively promoting every large source file into a candidate.
        execution_binding = {key: copy.deepcopy(binding[key]) for key in (
            'source_task_id', 'source_revision', 'step_id', 'slot', 'code_ref', 'roster_ref')}
        execution_binding['evidence_ref'] = self._publish('import-source', binding)
        goal_stage = {'task_id': context['task_id'], 'revision': context['revision'], 'epoch': context['epoch'],
                      'goal': context['contract'].get('goal', ''), 'query': stage.get('query_key'),
                      'stage_id': body['stage_name'], 'level_id': stage.get('level_id', stage.get('stage_id')),
                      'code': stage.get('display_code'), 'difficulty': stage['difficulty'],
                      'resolved_stage': _execution_stage(stage), 'source_binding': execution_binding}
        goal_stage['evidence_ref'] = self._publish('goal-stage', goal_stage)
        record = {'task_id': context['task_id'], 'revision': context['revision'], 'epoch': context['epoch'],
                  'research_dispatch_id': context['dispatch_id'], 'source_url': f"https://prts.maa.plus/copilot/get/{code['id']}",
                  'copilot_id': str(code['id']), **document_ref, 'stage_name': body['stage_name'],
                  'title': str((body.get('doc') or {}).get('title', ''))[:500] if isinstance(body.get('doc'), dict) else '',
                  'action_count': len(body['actions']), 'goal_stage': goal_stage,
                  'difficulty': stage['difficulty'], 'source_binding': execution_binding, 'roster_ref': binding['roster_ref']}
        candidate_ref = self._publish('copilot-download', record)
        encoded, digest = _encode(material)
        with self.engine._transaction():
            self._guard(context, cancel_event)
            self.workflow.management.require_active(source_task_id)
            source = self.workflow.current(source_task_id)
            if source['revision'] != binding['source_revision'] or _encode(source['contract'])[1] != binding['source_contract_sha256']:
                raise ActionRejected('提交前来源合同已修订')
            if roster_ref(self.rosters.current('local-default') or {}) != binding['roster_ref']:
                raise ActionRejected('提交前本地 Box 已变化')
            row = self.engine._db.execute('SELECT hash FROM code_receipts WHERE id=?', (binding['code_ref']['receipt_id'],)).fetchone()
            step = self.engine._db.execute('SELECT result FROM leader_steps WHERE task_id=? AND revision=? AND step_id=?',
                (source_task_id, source['revision'], step_id)).fetchone()
            if (not row or row['hash'] != binding['code_ref']['sha256'] or not step
                    or json.loads(step['result']) != binding['step_result']):
                raise ActionRejected('提交前来源步骤或代码凭据已变化')
            previous = self.engine._db.execute('SELECT payload,payload_hash FROM import_preparations WHERE cache_key=?', (cache_key,)).fetchone()
            if previous and (previous['payload'] != encoded or previous['payload_hash'] != digest):
                raise ActionRejected('同一准备身份已有不同的完整作业')
            self.engine._db.execute('INSERT INTO import_preparations '
                '(cache_key,payload,payload_hash,owner_task_id,source_task_id,owner_revision,ownership_error) '
                'VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(cache_key) DO UPDATE SET '
                'owner_task_id=excluded.owner_task_id,source_task_id=excluded.source_task_id,'
                'owner_revision=excluded.owner_revision,ownership_error=NULL',
                (cache_key, encoded, digest, context['task_id'], source_task_id, context['revision']))
            self.workflow._publish_event(context['task_id'], 'import.candidate_prepared',
                {'dispatch_id': context['dispatch_id'], 'candidate_ref': candidate_ref},
                event_key='import-candidate:' + candidate_ref['sha256'])
        verified = {key: record[key] for key in ('copilot_id', 'path', 'sha256', 'stage_name', 'title', 'source_url')}
        verified.update(code=stage.get('display_code'), mystery_code=code['code'],
                        detail_url=f"https://prts.plus/?op={code['id']}", difficulty=stage['difficulty'])
        result = {'candidate_record': record, 'candidate_ref': candidate_ref, 'goal_stage': goal_stage,
                'verified_copilot': verified, 'assessment': assessment, 'cached': bool(cached),
                'source_difficulty': source_difficulty}
        # Optional conservative queue reuse identity. Legacy single-import
        # preparation may precede stricter queue/log canonical validation.
        from .maa_queue_receipt import expected_loaded_copilot_document, expected_queue_copilot_id, MaaQueueReceiptError
        try:
            result['source_semantic_sha256'] = _encode(expected_loaded_copilot_document(body))[1]
            result['expected_copilot_id'] = expected_queue_copilot_id(body, code['code'])
        except MaaQueueReceiptError:
            pass
        return result


def _readonly_preparation(workflow):
    """Construct read views without running their schema-migration constructors."""
    from .maa_facts import RosterStore
    preparation = object.__new__(ImportPreparation)
    preparation.workflow, preparation.engine = workflow, workflow.engine
    preparation.directory = workflow.engine.data_dir.resolve()
    plans = object.__new__(PlanStore)
    plans.workflow, plans.engine, plans.db = workflow, workflow.engine, workflow.engine._db
    facts = object.__new__(RosterStore)
    facts.workflow, facts.engine = workflow, workflow.engine
    codes = object.__new__(CodeDeliveryStore)
    codes.workflow, codes.engine, codes.directory = workflow, workflow.engine, preparation.directory
    codes.plans, codes.roster_store = plans, facts
    rosters = object.__new__(RosterTools)
    rosters.store, rosters.directory, rosters.facts = workflow, preparation.directory, facts
    preparation.plans, preparation.codes, preparation.rosters = plans, codes, rosters
    return preparation


def verify_import_preparation_result(workflow, task_id):
    """Independently inspect a completed research generation; never execute it.

    Accept an active same-generation review, or the explicit B0 stop receipt in
    its next revoked epoch. Paused/recycled/revised generations cannot reuse it.
    All reads are against real persisted dispatch, preparation, source and Box
    evidence. No constructor, download, model request or SQLite write runs here.
    """
    result = {'verified':False,'code':'import_preparation_unverified','items':[],
              'goal_complete':False,'maa_dispatches':0,'submitted_operations':0,
              'summary':'导入准备尚缺当前任务的完整独立核验凭据。'}
    try:
        engine=workflow.engine
        with engine._lock:
            state=workflow.current(task_id)
            workflow.management.require_active(task_id)
            result['maa_dispatches']=engine._db.execute(
                "SELECT COUNT(*) FROM workflow_dispatches WHERE task_id=? AND role='maa'",(task_id,)).fetchone()[0]
            result['submitted_operations']=engine._db.execute(
                'SELECT COUNT(*) FROM workflow_operations WHERE task_id=? AND submitted_at IS NOT NULL',
                (task_id,)).fetchone()[0]
            if result['maa_dispatches'] or result['submitted_operations']:
                return result
            row=engine._db.execute("SELECT dispatch_id FROM workflow_dispatches WHERE task_id=? "
                "AND revision=? AND role='research' ORDER BY rowid DESC LIMIT 1",(task_id,state['revision'])).fetchone()
            if not row:
                return result
            dispatch=workflow.get_dispatch(row['dispatch_id'])
            research=workflow.dispatch_result(dispatch['dispatch_id'],current_only=False)
            if (dispatch['status']!='completed' or not research or research.get('status')!='researched'
                    or dispatch['payload'].get('contract')!=state['contract']
                    or state['contract'].get('current_deliverable')!='maa_import'):
                return result
            same=(state['epoch']==dispatch['epoch'] and state['writes_enabled']
                  and state['status'] not in {'paused','cancelled','completed','failed','needs_input'})
            stopped=(state['epoch']==dispatch['epoch']+1 and not state['writes_enabled']
                     and state['status']=='needs_input' and state['control_kind'] in {'needs_input','blocked'})
            if not same and not stopped:
                return result
            if stopped:
                events=engine._db.execute("SELECT payload,payload_hash FROM workflow_outbox WHERE task_id=? "
                    "AND revision=? AND epoch=? AND type='import.preparation_verified'",
                    (task_id,state['revision'],state['epoch'])).fetchall()
                proofs=[json.loads(event['payload']) for event in events
                        if _encode(json.loads(event['payload']))[1]==event['payload_hash']]
                if not any(proof.get('research_dispatch_id')==dispatch['dispatch_id']
                           and proof.get('research_epoch')==dispatch['epoch']
                           and proof.get('candidate_refs')==research.get('candidate_refs')
                           and proof.get('queue_plan_ref')==research.get('queue_plan_ref') for proof in proofs):
                    return result
            context={**state,'epoch':dispatch['epoch'],'dispatch_id':dispatch['dispatch_id']}
            preparation=_readonly_preparation(workflow)
            source,source_plan,wanted=preparation._linked_plan(context)
            from .maa_queue_plan import select_import_stages, build_queue_plan
            from .planned_agents import _import_slot
            slot=_import_slot(state['contract'].get('user_request') or state['contract'].get('goal',''),
                contract=state['contract'],selected_slot=state['contract'].get('selected_slot'))
            stages={stage['stage_key']:stage for stage in source_plan['stages']}
            ordered=[{**stages[step['stage_key']],'stage_code':stages[step['stage_key']].get('display_code',''),
                      'step_id':step['step_id']} for step in source_plan['steps']]
            selected=([stage for stage in ordered if stage['stage_key'] in wanted]
                      if state['contract'].get('import_scope') is not None
                      else select_import_stages(ordered,state['contract']))
            references=research.get('candidate_refs')
            if not isinstance(references,list) or len(references)!=len(selected) or not references:
                return result
            prepared=[]
            for reference,stage in zip(references,selected,strict=True):
                record=read_reference(reference,engine.data_dir)
                if record.get('research_dispatch_id')!=dispatch['dispatch_id']:
                    return result
                facts=preparation._verify_material(context,record)
                binding=facts['source_binding']
                if (binding['step_id']!=stage['step_id'] or binding['slot']!=slot
                        or record['goal_stage']['resolved_stage']['stage_key']!=stage['stage_key']):
                    return result
                document_ref={key:record[key] for key in ('path','sha256')}
                body=preparation._document(document_ref)
                code='prts://'+record['copilot_id']
                assessment=preparation.rosters.assess('local-default',document_ref)
                verified={key:record[key] for key in ('copilot_id','path','sha256','stage_name','title','source_url')}
                verified.update(mystery_code=code,code=stage['stage_code'],difficulty=stage['difficulty'])
                item={'stage_code':stage['stage_code'],'difficulty':stage['difficulty'],'mystery_code':code,
                      'candidate_ref':reference,'document_ref':document_ref,'source_binding_ref':binding,
                      'roster_ref':record['roster_ref'],'source_task_id':source['task_id']}
                result['items'].append(item)
                preparation_result={'candidate_record':record,'candidate_ref':reference,'goal_stage':record['goal_stage'],
                    'verified_copilot':verified,'assessment':assessment,'source_difficulty':body.get('difficulty',1)}
                from .maa_queue_receipt import expected_loaded_copilot_document, expected_queue_copilot_id, MaaQueueReceiptError
                try:
                    preparation_result['source_semantic_sha256']=_encode(expected_loaded_copilot_document(body))[1]
                    preparation_result['expected_copilot_id']=expected_queue_copilot_id(body, code)
                except MaaQueueReceiptError:
                    if research.get('queue_plan_ref'):
                        raise
                prepared.append(preparation_result)
            plan_reference=research.get('queue_plan_ref') or research.get('single_document_plan_ref')
            if plan_reference:
                plan=read_reference(plan_reference,engine.data_dir)
                structure_ref=source_plan['structure_ref']
                structure=read_reference(structure_ref,engine.data_dir)
                rebuild=build_queue_plan({**context,'contract':{**context['contract'],
                    'selected_slot':slot,'source_revision':source['revision']}},structure_ref,structure,prepared,
                    source_stage_keys=[step['stage_key'] for step in source_plan['steps']])
                if plan!=rebuild:
                    return result
                if research.get('single_document_plan_ref'):
                    from .import_mode import single_document_plan
                    if not single_document_plan(plan):return result
                    result['single_document_plan_ref']=plan_reference
                else:
                    result['queue_plan_ref']=plan_reference
            elif len(selected)!=1:
                return result
            result.update(verified=True,code='import_preparation_verified',
                task_id=task_id,revision=state['revision'],research_epoch=dispatch['epoch'],
                research_dispatch_id=dispatch['dispatch_id'],candidate_refs=references,
                summary='已独立核验 '+str(len(selected))+' 个导入准备目标；尚未导入 MAA，不开始作战。')
            return result
    except (ActionRejected,AttributeError,KeyError,OSError,RuntimeError,TypeError,ValueError,sqlite3.Error):
        return result

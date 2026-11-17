"""Durable queue input fences on the existing workflow transaction.

Large evidence stays in task-private files. The database stores hashes,
row identities, and operation IDs; it never contains whole MAA settings.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from .core import ActionRejected
from .leader_plan import read_reference
from .maa_load_receipt import _PREFIX
from .maa_queue_receipt import (
    _check_snapshot, diff_queue,
    verify_queue_receipt, verify_queue_effect_receipt, verify_queue_recovery_receipt,
    verify_queue_absence_receipt, canonical_copilot_document,
    _delete_ownership,
)
from .task_cleanup import TaskCleanup
from .workflow_store import _encode


_IDENTITY = ('task_id', 'revision', 'epoch')
_PHASES = {'focus', 'mode', 'options', 'scroll', 'code', 'add', 'delete'}
_TARGET_PHASES = {'code', 'add'}
_ASYNC_PHASES = _TARGET_PHASES | {'delete'}
_ITEM_STATES = {'pending', 'reused', 'added', 'conflict', 'unknown', 'blocked'}
_MAX_EVIDENCE = 192 * 1024 * 1024
_RUN_SCHEMA = 'maa_queue_run/v1'
_ITEM_SCHEMA = 'maa_queue_item/v1'


def _key(context):
    return tuple(context[field] for field in _IDENTITY)


def _digest(value):
    return _encode(value)[1]


def _load(row):
    if row is None:
        return None
    payload = json.loads(row['payload'])
    if _digest(payload) != row['payload_hash']:
        raise ValueError('队列账本资料哈希已变化')
    return payload


def _run(db, context):
    return _load(db.execute(
        'SELECT payload,payload_hash FROM workflow_queue_runs WHERE task_id=? AND revision=? AND epoch=?',
        _key(context)).fetchone())


def _item(db, context, target_key):
    return _load(db.execute(
        'SELECT payload,payload_hash FROM workflow_queue_items WHERE task_id=? AND revision=? AND epoch=? AND target_key=?',
        (*_key(context), target_key)).fetchone())


def _active(workflow, context):
    state = workflow._fresh(context, writes=True)
    contract = state['contract']
    if (context.get('contract', contract) != contract
            or contract.get('current_deliverable') != 'maa_import'
            or contract.get('allow_start') is not False
            or type(contract.get('attempt_limit')) is not int or contract['attempt_limit'] != 0
            or (contract.get('task_spec') or {}).get('kind') != 'import'
            or state['status'] in {'paused','cancelled','completed','failed','needs_input'}):
        raise ActionRejected('当前合同不允许多作业导入')
    dispatch = workflow._dispatch(workflow._dispatch_row(context['dispatch_id']))
    if (_key(dispatch) != _key(context) or dispatch['role'] != 'maa'
            or dispatch['status'] != 'running'
            or dispatch['payload'].get('contract',contract) != contract):
        raise ActionRejected('队列操作需要当前活跃的 MAA 委派')
    return state


def _target(run, key):
    for target in run['plan']['targets']:
        if target['target_key'] == key:
            return target
    raise ActionRejected('队列目标不属于已登记计划')


def _guard_facts(workflow, guard):
    _active(workflow, guard)
    db = workflow._db
    run = _run(db, guard)
    if (not run or run['schema'] != _RUN_SCHEMA
            or guard['plan_sha256'] != run['plan']['plan_sha256']
            or not run['snapshot'] or guard['snapshot_sha256'] != run['snapshot']['ref']['sha256']):
        raise ActionRejected('队列计划或最新截图已经变化')
    key, phase = guard['target_key'], guard['phase']
    item = _item(db, guard, key) if key is not None else None
    if guard['item_version'] != (item['item_version'] if item else 0):
        raise ActionRejected('队列目标已经变化')
    if phase not in _PHASES or (phase in _TARGET_PHASES and not item) or (key is not None and not item):
        raise ActionRejected('队列动作与目标不匹配')
    if phase in _TARGET_PHASES and item['status'] != 'pending':
        raise ActionRejected('队列目标不是待导入项')
    if phase in _ASYNC_PHASES and run['snapshot']['visible_complete'] is not True:
        raise ActionRejected('缺少当前完整队列回读')
    if phase == 'add':
        prefix = f"queue:{guard['plan_sha256']}:{key}:code:"
        rows = db.execute('SELECT * FROM workflow_operations WHERE task_id=? AND revision=? AND epoch=? '
                          'AND substr(effect_key,1,?)=? AND status=?',
                          (*_key(guard),len(prefix),prefix,'verified'))
        if not any((receipt := workflow._operation(row)['result']) and
                   receipt.get('queue_effect') == 'parsed_no_row' and
                   receipt.get('projection_sha256') == run['snapshot']['projection_sha256'] for row in rows):
            raise ActionRejected('补加前须核实本次代码已解析且目标未入队')
    if phase == 'delete':
        row_id = guard['row_identity']
        owned = run['owned_rows'].get(row_id)
        if (not owned or not owned['operation_id']
                or run['snapshot']['row_ids'].count(row_id) != 1
                or row_id in run['baseline']['row_ids']):
            raise ActionRejected('只能删除唯一且已证实为本任务新增的行')
        operation = workflow._operation(workflow._operation_row(owned['operation_id']))
        if (_key(operation) != _key(guard) or operation['status'] != 'verified'
                or row_id not in (operation['result'] or {}).get('added_rows', [])):
            raise ActionRejected('缺少该行对应的已提交导入证据')
        claimed = db.execute(
            'SELECT payload,payload_hash FROM workflow_queue_items WHERE task_id=? AND revision=? AND epoch=?',
            _key(guard))
        if any(_load(row)['row_identity'] == row_id for row in claimed):
            raise ActionRejected('该行是已经分配的有效目标')
        candidate = next(row for row in run['snapshot']['rows'] if row['identity'] == row_id)
        if any(candidate['stage_id'] == target['stage_id']
               and candidate['is_raid'] == (target['difficulty'] == 2)
               for target in run['plan']['targets']):
            raise ActionRejected('该行对应计划中的有效关卡和难度')
    elif guard['row_identity'] is not None:
        raise ActionRejected('该动作不接受行身份')
    return run


def check_queue_guard_in_transaction(workflow, operation, guard, import_guard):
    """Last durable gate; file checks occurred before entering the writer."""
    if not workflow._db.in_transaction:
        raise RuntimeError('queue submission guard requires a writer transaction')
    action = json.loads(operation['action'])
    if (not isinstance(guard, dict) or action.get('queue_guard') != guard
            or any(operation[key] != guard.get(key) for key in (*_IDENTITY,'dispatch_id'))
            or not operation['effect_key'].startswith('queue:') or operation['slot'] != 'desktop-maa'
            or operation['starts'] or operation['stop_only']):
        raise ActionRejected('多作业提交缺少本动作登记的准入信息')
    run = _guard_facts(workflow, guard)
    if guard['phase'] == 'code' and not action.get('input_baseline_ref'):
        raise ActionRejected('代码输入缺少本动作登记的日志基线')
    if guard['phase'] in {'add','delete'}:
        if not action.get('effect_baseline_ref'):
            raise ActionRejected('队列效果输入缺少已登记基线')
        source_operation = _effect_source_operation(workflow, operation, action.get('effect_source'))
        if guard['phase'] == 'delete':
            owner = run['owned_rows'].get(guard['row_identity'])
            if (not owner or owner['operation_id'] != source_operation['operation_id']
                    or owner['evidence_ref'] != action['effect_source']['receipt_ref']):
                raise ActionRejected('删除行的来源所有权已经变化')
    if guard['phase'] not in _TARGET_PHASES:
        if import_guard is not None:
            raise ActionRejected('该队列动作不接受作业来源替换')
        return
    target = _target(run, guard['target_key'])
    if not isinstance(import_guard, dict):
        raise ActionRejected('导入动作必须同时复核完整作业及 Box')
    binding = import_guard.get('source_binding')
    if (not isinstance(binding, dict) or _digest(binding) != target['source_binding_ref']['sha256']
            or binding.get('slot') != target['slot']
            or binding.get('roster_ref') != target['roster_ref']
            or (binding.get('resolved_stage') or {}).get('stage_key') != target['stage_key']):
        raise ActionRejected('导入准备与当前队列目标不一致')
    cached = workflow._db.execute(
        'SELECT payload,payload_hash FROM import_preparations WHERE cache_key=?',
        (import_guard.get('cache_key'),)).fetchone()
    if (not cached or cached['payload_hash'] != import_guard.get('cache_payload_hash')
            or _load(cached).get('document_ref') != target['document_ref']):
        raise ActionRejected('当前队列目标的完整作业已经变化')
    receipt_id = (binding.get('code_ref') or {}).get('receipt_id')
    row = workflow._db.execute('SELECT payload,hash FROM code_receipts WHERE id=?',(receipt_id,)).fetchone()
    code_payload = json.loads(row['payload']) if row else {}
    if (not row or row['hash'] != binding['code_ref'].get('sha256')
            or _digest(code_payload) != row['hash']
            or 'prts://' + str((code_payload.get('candidate_locator') or {}).get('id')) != target['mystery_code']):
        raise ActionRejected('当前队列目标的作业码已经变化')


def _effect_source_operation(workflow, context, source):
    """Recheck immutable source operation facts using the existing DB lock."""
    if not isinstance(source,dict) or not source.get('operation_id'):
        raise ActionRejected('队列效果缺少来源输入')
    operation = workflow._operation(workflow._operation_row(source['operation_id']))
    result = operation['result'] or {}
    if (_key(operation) != _key(context) or operation['status'] != 'verified'
            or operation['action'].get('queue_guard',{}).get('phase') != 'code'
            or result.get('queue_verification') is not True
            or result.get('evidence_ref') != source.get('receipt_ref')
            or _digest(result) != source.get('result_sha256')
            or operation['payload_hash'] != source.get('action_sha256')):
        raise ActionRejected('队列来源输入凭据已经变化')
    return operation


class QueueStore:
    def __init__(self, workflow):
        self.workflow = workflow
        self.engine = workflow.engine
        self.db = workflow._db
        self.cleanup = TaskCleanup(workflow)
        with self.engine._transaction():
            self.db.execute('''CREATE TABLE IF NOT EXISTS workflow_queue_runs (
                task_id TEXT NOT NULL, revision INTEGER NOT NULL, epoch INTEGER NOT NULL,
                payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
                PRIMARY KEY(task_id,revision,epoch))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS workflow_queue_items (
                task_id TEXT NOT NULL, revision INTEGER NOT NULL, epoch INTEGER NOT NULL,
                target_key TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
                PRIMARY KEY(task_id,revision,epoch,target_key))''')

    def _private_ref(self, context, reference, *, decode=False):
        if (not isinstance(reference, dict) or set(reference) != {'path','sha256'}
                or not isinstance(reference['path'],str)
                or not isinstance(reference['sha256'],str)
                or not re.fullmatch(r'[0-9a-f]{64}',reference['sha256'])):
            raise ValueError('队列证据引用格式无效')
        task_id, _, epoch = _key(context)
        root = self.cleanup.task_root/task_id/'maa-queue'/str(epoch)
        path = self.cleanup._safe_path(task_id,reference['path'])
        if not path.is_relative_to(root.resolve()) or str(path) != reference['path']:
            raise ValueError('队列证据必须属于当前任务代际')
        if decode:
            return read_reference(reference, root, maximum=_MAX_EVIDENCE)
        with path.open('rb') as stream:
            if path.stat().st_size > _MAX_EVIDENCE or hashlib.file_digest(stream,'sha256').hexdigest() != reference['sha256']:
                raise ValueError('队列证据内容已变化或超过大小限制')
        return None

    def _own(self, task_id, refs, *, existing_only=False):
        for reference in refs:
            path = reference['path']
            previous = self.db.execute('SELECT sha256 FROM workflow_cleanup_files WHERE task_id=? AND path=?',
                                       (task_id,path)).fetchone()
            if previous:
                if previous['sha256'] != reference['sha256']:
                    raise ActionRejected('队列证据路径曾绑定不同内容')
            elif existing_only:
                raise ActionRejected('迟到回执只能引用已经登记的证据')
            else:
                self.db.execute('INSERT INTO workflow_cleanup_files(task_id,path,sha256) VALUES(?,?,?)',
                                (task_id,path,reference['sha256']))

    def _update_run(self, context, run):
        self.db.execute('UPDATE workflow_queue_runs SET payload=?,payload_hash=? WHERE task_id=? AND revision=? AND epoch=?',
                        (*_encode(run),*_key(context)))

    def register_evidence(self, context, refs):
        """Pin files from the current GUI recipe before a later read-only reconciliation."""
        for ref in refs:
            self._private_ref(context,ref)
        with self.engine._transaction():
            _active(self.workflow,context)
            if not _run(self.db,context):
                raise ActionRejected('尚未登记队列计划')
            self._own(context['task_id'],refs)

    def _items(self, context):
        rows = self.db.execute('SELECT payload,payload_hash FROM workflow_queue_items '
                               'WHERE task_id=? AND revision=? AND epoch=? ORDER BY rowid',_key(context))
        return [_load(row) for row in rows]

    def _view(self, context, run):
        return {**run, 'items':self._items(context)}

    def register_plan(self, context, plan, plan_ref):
        plan = json.loads(_encode(plan)[0])
        plan_file = self._private_ref(context,plan_ref,decode=True)
        if (plan_file != plan or plan.get('schema') != 'maa_queue_plan/v1'
                or _key(plan) != _key(context)
                or plan['plan_sha256'] != _digest({k:v for k,v in plan.items() if k != 'plan_sha256'})):
            raise ActionRejected('队列计划资料与当前任务不一致')
        keys = [target['target_key'] for target in plan['targets']]
        if (not keys or len(set(keys)) != len(keys)
                or any(not isinstance(key,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}',key) for key in keys)
                or [key for batch in plan['batches'] for key in batch['target_keys']] != keys):
            raise ValueError('队列目标范围不完整或含重复项')
        with self.engine._transaction():
            _active(self.workflow,context)
            existing = _run(self.db,context)
            if existing:
                if existing['plan'] != plan or existing['plan_ref'] != plan_ref:
                    raise ActionRejected('该执行代际已有不同的队列计划')
                return self._view(context,existing)
            self._own(context['task_id'],[plan_ref])
            run = {'schema':_RUN_SCHEMA,'status':'pending','plan':plan,'plan_ref':plan_ref,
                   'baseline':None,'snapshot':None,'owned_rows':{}}
            self.db.execute('INSERT INTO workflow_queue_runs VALUES(?,?,?,?,?)',(*_key(context),*_encode(run)))
            for key in keys:
                item = {'schema':_ITEM_SCHEMA,'target_key':key,'status':'pending','item_version':0,
                        'row_identity':None,'owned':False,'operation_ids':[],'evidence_refs':[],'code':'',
                        'match_basis':None}
                self.db.execute('INSERT INTO workflow_queue_items VALUES(?,?,?,?,?,?)',
                                (*_key(context),key,*_encode(item)))
            return self._view(context,run)

    def current(self, task_id):
        with self.engine._lock:
            state = self.workflow._state(task_id)
            run = _run(self.db,state)
            return self._view(state,run) if run else None

    def _snapshot_files(self, context, snapshot):
        _check_snapshot(snapshot)
        if snapshot['visible_complete'] is True and not snapshot['screenshot_refs']:
            raise ActionRejected('完整队列回读必须有当前截图')
        reference = snapshot['evidence_ref']
        if self._private_ref(context,reference,decode=True) != {
                key:value for key,value in snapshot.items() if key != 'evidence_ref'}:
            raise ActionRejected('截图和队列快照文件不一致')
        refs = snapshot['screenshot_refs'] + [item['ref'] for item in snapshot['body_evidence']]
        for extra in refs:
            self._private_ref(context,extra)
        return [reference,*refs]

    def record_snapshot(self, context, snapshot):
        refs = self._snapshot_files(context,snapshot)
        observation = {'ref':snapshot['evidence_ref'],'binding':snapshot['binding'],
                       'captured_at':snapshot['captured_at'],'visible_complete':snapshot['visible_complete'],
                       'projection_sha256':snapshot['projection_sha256'],
                       'rows':[{field:row[field] for field in ('identity','stage_id','is_raid')}
                               for row in snapshot['rows']],
                       'row_ids':[row['identity'] for row in snapshot['rows']]}
        with self.engine._transaction():
            _active(self.workflow,context)
            run = _run(self.db,context)
            if not run:
                raise ActionRejected('尚未登记队列计划')
            prior = run['snapshot']
            if prior and prior['ref'] == observation['ref']:
                return self._view(context,run)
            if prior and (prior['binding'] != observation['binding']
                          or prior['captured_at'] >= observation['captured_at']):
                raise ActionRejected('窗口、配置或快照代际已变化，需要重新对账')
            self._own(context['task_id'],refs)
            run['snapshot'] = observation
            if run['baseline'] is None and observation['visible_complete'] is True:
                run['baseline'] = observation
            self._update_run(context,run)
            return self._view(context,run)

    def prepare_item_action(self, context, target_key, *, phase, snapshot, row_identity=None,
                            input_baseline_ref=None, effect_baseline_ref=None):
        self._snapshot_files(context,snapshot)
        baseline = None
        if input_baseline_ref is not None:
            if phase != 'code':
                raise ActionRejected('仅代码输入可以登记日志基线')
            baseline = self._private_ref(context,input_baseline_ref,decode=True)
            if (baseline.get('schema') != 'maa_queue_input_baseline/v1'
                    or baseline.get('snapshot') != snapshot or baseline.get('binding') != snapshot['binding']):
                raise ActionRejected('日志基线与当前队列快照不一致')
            self.register_evidence(context,[input_baseline_ref])
        effect_baseline = None
        if phase in {'add','delete'}:
            if effect_baseline_ref is None:
                raise ActionRejected('队列效果输入必须登记独立基线')
            effect_baseline = self._private_ref(context,effect_baseline_ref,decode=True)
            if (effect_baseline.get('schema') != 'maa_queue_effect_baseline/v1'
                    or effect_baseline.get('phase') != phase
                    or effect_baseline.get('snapshot') != snapshot
                    or effect_baseline.get('binding') != snapshot['binding']
                    or effect_baseline.get('row_identity') != row_identity):
                raise ActionRejected('队列效果基线与本动作不一致')
            source = effect_baseline.get('source_input') or {}
            if self._private_ref(context,source.get('receipt_ref'),decode=True) != source.get('receipt'):
                raise ActionRejected('队列来源输入文件不一致')
        elif effect_baseline_ref is not None:
            raise ActionRejected('该动作不接受队列效果基线')
        with self.engine._lock:
            _active(self.workflow,context)
            run = _run(self.db,context)
            if not run or not run['snapshot']:
                raise ActionRejected('尚未登记队列快照')
            item = _item(self.db,context,target_key) if target_key is not None else None
            guard = {key:context[key] for key in (*_IDENTITY,'dispatch_id')}
            guard.update(plan_sha256=run['plan']['plan_sha256'],
                         snapshot_sha256=snapshot['evidence_ref']['sha256'],target_key=target_key,
                         phase=phase,row_identity=row_identity,item_version=item['item_version'] if item else 0)
            _guard_facts(self.workflow,guard)
            effect_source = None
            if effect_baseline is not None:
                source = effect_baseline['source_input']
                source_operation = self.workflow.get_operation(source['operation_id'])
                effect_source = {'operation_id':source['operation_id'],'receipt_ref':source['receipt_ref'],
                    'result_sha256':_digest(source_operation['result']),
                    'action_sha256':source_operation['payload_hash']}
                _effect_source_operation(self.workflow,context,effect_source)
                selected = _target(run,source_operation['action']['queue_guard']['target_key'])
                document = read_reference(selected['document_ref'],self.engine.data_dir,maximum=8*1024*1024)
                if (effect_baseline['planned_targets'] != run['plan']['targets']
                        or canonical_copilot_document(effect_baseline['source_document'])
                            != canonical_copilot_document(document)
                        or any(effect_baseline['target'].get(key) != selected[key]
                            for key in ('target_key','stage_id','difficulty','mystery_code'))):
                    raise ActionRejected('队列效果基线未绑定当前完整计划')
                if phase == 'delete':
                    owner = run['owned_rows'].get(row_identity)
                    if (not owner or owner['operation_id'] != source_operation['operation_id']
                            or owner['evidence_ref'] != source['receipt_ref']
                            or row_identity in run['baseline']['row_ids']):
                        raise ActionRejected('删除行不属于本任务来源输入')
                    _delete_ownership(effect_baseline)
                self.register_evidence(context,[effect_baseline_ref])
            if baseline:
                selected = _target(run,target_key)
                if any(baseline.get('target',{}).get(key) != selected[key] for key in
                       ('target_key','stage_id','difficulty','mystery_code')):
                    raise ActionRejected('日志基线与队列目标不一致')
            identity = target_key if target_key is not None else 'run'
            if phase == 'delete':
                identity = _digest({'target':identity,'row_identity':row_identity})
            prefix = f"queue:{guard['plan_sha256']}:{identity}:{phase}:"
            previous = self.db.execute(
                'SELECT * FROM workflow_operations WHERE task_id=? AND revision=? AND epoch=? '
                'AND substr(effect_key,1,?)=? ORDER BY rowid',
                (*_key(context),len(prefix),prefix)).fetchall()
            if previous:
                last = self.workflow._operation(previous[-1])
                if last['action'].get('queue_guard') == guard:
                    if (last['action'].get('input_baseline_ref') != input_baseline_ref
                            or last['action'].get('effect_baseline_ref') != effect_baseline_ref
                            or last['action'].get('effect_source') != effect_source):
                        raise ActionRejected('同一动作的输入日志基线不可替换')
                    return {**last,'queue_guard':guard}
                if last['status'] in {'planned','issued','submitted','unknown','failed'}:
                    raise ActionRejected('先对账旧的队列动作')
                if phase in _ASYNC_PHASES and last['status'] != 'not_issued':
                    raise ActionRejected('队列输入已经提交，不能重复')
            action = {'kind':'queue_gui','queue_guard':guard,'snapshot_ref':snapshot['evidence_ref']}
            if phase == 'scroll':
                parent_id = self.workflow.slot_state()['barrier_operation_id']
                if parent_id:
                    action['queue_observation_of'] = parent_id
            if input_baseline_ref:
                action['input_baseline_ref'] = input_baseline_ref
            if effect_baseline_ref:
                action['effect_baseline_ref'] = effect_baseline_ref
                action['effect_source'] = effect_source
            operation = self.workflow.prepare_operation(context['task_id'],context['dispatch_id'],
                prefix+str(len(previous)+1),action)
            return {**operation,'queue_guard':guard}

    def submission_guard(self, context, action):
        with self.engine._lock:
            row = self.workflow.get_operation(action['operation_id'])
            guard = row['action'].get('queue_guard')
            if (not guard or _key(guard) != _key(context)
                    or guard['dispatch_id'] != context['dispatch_id']
                    or row['status'] not in {'planned','issued'}):
                raise ActionRejected('队列动作不再可以提交')
            _guard_facts(self.workflow,guard)
            return guard

    def _read_snapshot(self, context, reference):
        return {**self._private_ref(context,reference,decode=True),'evidence_ref':reference}

    def record_item(self, context, result):
        key = result['target_key']
        status = result['status']
        if status not in _ITEM_STATES:
            raise ValueError('未知队列项目状态')
        with self.engine._lock:
            _active(self.workflow,context)
            run = _run(self.db,context)
            initial_item = _item(self.db,context,key)
            if not run or not initial_item:
                raise ActionRejected('队列目标尚未登记')
        reference = run['snapshot']['ref'] if run['snapshot'] else None
        row_id = result.get('row_identity')
        operation_ids = list(result.get('operation_ids',[]))
        refs = list(result.get('evidence_refs',[]))
        if status in {'reused','added'}:
            if not reference or not run['baseline']:
                raise ActionRejected('缺少完整队列快照')
            snapshot = self._read_snapshot(context,reference)
            baseline = self._read_snapshot(context,run['baseline']['ref'])
            target = _target(run,key)
            owned = [{'identity':identity,'owned':True,'operation_ids':[owner['operation_id']]}
                     for identity,owner in run['owned_rows'].items()]
            difference = diff_queue(baseline,snapshot,[target],owned)
            from .maa_queue_receipt import row_matches_target
            expected_rows = [row for row in snapshot['rows'] if row_matches_target(row, target)]
            if (key not in difference['reusable'] or difference['external_changes']
                    or len(expected_rows) != 1 or expected_rows[0]['identity'] != row_id):
                raise ActionRejected('当前队列无法核实所选作业')
            refs = list({ref['path']:ref for ref in [*refs,reference]}.values())
        elif row_id is not None or result.get('owned'):
            raise ActionRejected('未核实项目不能认领队列行')
        for ref in refs:
            self._private_ref(context,ref)
        with self.engine._transaction():
            _active(self.workflow,context)
            current = _run(self.db,context)
            item = _item(self.db,context,key)
            if current != run or item != initial_item:
                raise ActionRejected('核验期间队列证据已经变化')
            for operation_id in operation_ids:
                operation = self.workflow._operation(self.workflow._operation_row(operation_id))
                operation_key = operation['action'].get('queue_guard',{}).get('target_key')
                paired = False
                if status == 'added' and operation_key and operation_key != key:
                    target = _target(run,key)
                    other = _target(run,operation_key)
                    paired = (target['source_difficulty'] == other['source_difficulty'] == 3
                              and all(target[field] == other[field] for field in
                                  ('stage_id','mystery_code','source_semantic_sha256','roster_ref'))
                              and target['difficulty'] != other['difficulty'])
                if _key(operation) != _key(context) or (operation_key != key and not paired):
                    raise ActionRejected('项目引用的输入不属于当前目标')
            owner = run['owned_rows'].get(row_id)
            if status == 'added' and (not owner or owner['operation_id'] not in operation_ids):
                raise ActionRejected('新增项目缺少本任务已核实的输入效果')
            if status == 'reused' and (owner or row_id not in run['baseline']['row_ids']):
                raise ActionRejected('只有本轮基线已有的行可以记为复用')
            match_basis = None
            if status in {'reused','added'}:
                match_basis = ('normalized_content' if expected_rows[0]['copilot_id'] == 0
                               else 'station_id_and_content')
            fields = {'status':status,'row_identity':row_id,'owned':status == 'added',
                      'operation_ids':operation_ids,'evidence_refs':refs,'code':result.get('code',''),
                      'match_basis':match_basis}
            if all(item.get(field) == value for field,value in fields.items()):
                return item
            if item['status'] in {'reused','added'}:
                raise ActionRejected('已核实的项目结果不可改写')
            self._own(context['task_id'],refs)
            item.update(fields,item_version=item['item_version']+1)
            self.db.execute('UPDATE workflow_queue_items SET payload=?,payload_hash=? '
                            'WHERE task_id=? AND revision=? AND epoch=? AND target_key=?',
                            (*_encode(item),*_key(context),key))
            return item

    def record_reconciliation(self, task_id, *, original_epoch, operation_id, evidence_ref, result):
        """Reconcile an existing queue input using verified local application evidence."""
        operation = self.workflow.get_operation(operation_id)
        action = operation['action']
        guard = action.get('queue_guard') or {}
        if (operation['task_id'] != task_id or operation['epoch'] != original_epoch
                or not operation['submitted_at'] or guard.get('phase') not in _ASYNC_PHASES):
            raise ActionRejected('回执不属于已提交的队列输入')
        with self.engine._lock:
            lifecycle = self.workflow.management._lifecycle(task_id)['state']
            run = _run(self.db,operation)
            if lifecycle not in {'active','trashed'} or not run:
                raise ActionRejected('任务已进入清理，不能登记输入回执')
        proof = self._private_ref(operation,evidence_ref,decode=True)
        if proof.get('schema') == 'maa_queue_effect_receipt/v1' and guard['phase'] in {'add','delete'}:
            return self._reconcile_effect(operation,run,proof,evidence_ref,result)
        if proof.get('schema') not in {'maa_queue_input_receipt/v1','maa_queue_recovery_receipt/v1',
                                     'maa_queue_absence_receipt/v1'} or guard['phase'] != 'code':
            raise ActionRejected('此回执没有验证所提交的队列效果')
        target = _target(run,guard['target_key'])
        document = read_reference(target['document_ref'],self.engine.data_dir,maximum=8*1024*1024)
        baseline = proof.get('baseline') or {}
        current = proof.get('current_snapshot') or {}
        if (baseline.get('snapshot',{}).get('evidence_ref') != action['snapshot_ref']
                or baseline.get('evidence_ref') != action.get('input_baseline_ref')
                or not action.get('input_baseline_ref')
                or current.get('binding') != run['baseline']['binding']):
            raise ActionRejected('回执未绑定本次输入的队列基线')
        expected = {'binding':run['baseline']['binding'],'target':target,'source_document':document}
        if proof.get('schema') == 'maa_queue_recovery_receipt/v1':
            original_baseline = {**self._private_ref(operation,action['input_baseline_ref'],decode=True),
                                 'evidence_ref':action['input_baseline_ref']}
            committed = {key:operation[key] for key in
                         ('operation_id','task_id','revision','epoch','submitted_at','payload_hash')}
            committed.update(baseline=original_baseline,document_ref=target['document_ref'])
            verified = verify_queue_recovery_receipt(proof,{**expected,**committed})
        elif proof.get('schema') == 'maa_queue_absence_receipt/v1':
            verified = verify_queue_absence_receipt(proof,expected)
            if proof.get('submitted_at') != datetime.fromisoformat(operation['submitted_at']).timestamp():
                raise ActionRejected('解析证明不属于本次提交')
        else:
            verified = verify_queue_receipt(proof,expected)
        if verified['verified'] is not True or result.get('status') != 'verified':
            raise ActionRejected('队列效果尚未核实')
        try:
            submitted = datetime.fromisoformat(operation['submitted_at'])
            request = _PREFIX.fullmatch(proof['log_evidence']['events'][0])
            requested = datetime.strptime(request.group(1),'%Y-%m-%d %H:%M:%S.%f').timestamp()
            # MAA log precision is milliseconds. Compare it against the
            # persisted UTC submission, independently of the pre-input offset.
            if (submitted.tzinfo is None
                    or not baseline['started_at'] <= submitted.timestamp() <= proof['captured_at']
                    or requested < submitted.timestamp() - .002
                    or requested > submitted.timestamp() + 180):
                raise ValueError('outside submission interval')
        except (ValueError,TypeError,AttributeError,IndexError,KeyError):
            raise ActionRejected('回执中的请求不在本动作提交时间之后') from None
        refs = [evidence_ref, *self._snapshot_files(operation,current)]
        old_ref = baseline.get('evidence_ref')
        self._private_ref(operation,old_ref)
        refs.append(old_ref)
        receipt = {'evidence_ref':evidence_ref,'queue_verification':True,
                   'added_rows':verified['added_rows'],'matched_rows':verified['matched_rows'],
                   'target_key':target['target_key']}
        if verified.get('queue_effect') == 'parsed_no_row':
            receipt.update(queue_effect='parsed_no_row',projection_sha256=verified['projection_sha256'])
        encoded,digest = _encode(receipt)
        with self.engine._transaction():
            state = self.workflow.management._lifecycle(task_id)['state']
            row = self.workflow._operation_row(operation_id)
            if (state not in {'active','trashed'} or _run(self.db,operation) != run
                    or row['submitted_at'] != operation['submitted_at']
                    or row['payload_hash'] != operation['payload_hash']):
                raise ActionRejected('对账期间队列账本已变化')
            if row['status'] == 'verified' and row['result'] == encoded:
                return self.workflow._operation(row)
            if row['status'] not in {'submitted','unknown'} or not row['submitted_at']:
                raise ActionRejected('该输入没有可核实的未决效果')
            self._own(task_id,refs,existing_only=state == 'trashed')
            current_ids = Counter(item['identity'] for item in current['rows'])
            for row_id in verified['added_rows']:
                if current_ids[row_id] == 1 and row_id not in run['baseline']['row_ids']:
                    run['owned_rows'][row_id] = {'operation_id':operation_id,'evidence_ref':evidence_ref}
            self._update_run(operation,run)
            self.db.execute("UPDATE workflow_operations SET status='verified',result=? WHERE operation_id=?",
                            (encoded,operation_id))
            self.db.execute('UPDATE workflow_slots SET barrier_operation_id=NULL WHERE slot=? AND barrier_operation_id=?',
                            (row['slot'],operation_id))
            self.workflow._publish_event(task_id,'operation.result',
                {'operation_id':operation_id,'status':'verified','result':receipt},
                event_key=f'operation:{operation_id}:verified:{digest}',revision=row['revision'],epoch=row['epoch'])
            return self.workflow._operation(self.workflow._operation_row(operation_id))

    def _reconcile_effect(self, operation, run, proof, evidence_ref, result):
        action = operation['action']
        guard = action['queue_guard']
        with self.engine._lock:
            source = _effect_source_operation(self.workflow,operation,action.get('effect_source'))
        baseline = proof.get('baseline') or {}
        source_input = baseline.get('source_input') or {}
        expected_baseline = self._private_ref(operation,action.get('effect_baseline_ref'),decode=True)
        if (baseline.get('evidence_ref') != action.get('effect_baseline_ref')
                or {key:value for key,value in baseline.items() if key!='evidence_ref'} != expected_baseline
                or baseline.get('snapshot',{}).get('evidence_ref') != action['snapshot_ref']
                or baseline.get('phase') != guard['phase']
                or baseline.get('row_identity') != guard['row_identity']
                or baseline.get('planned_targets') != run['plan']['targets']
                or source_input.get('operation_id') != source['operation_id']
                or source_input.get('receipt_ref') != source['result']['evidence_ref']
                or self._private_ref(operation,source_input.get('receipt_ref'),decode=True)
                    != source_input.get('receipt')):
            raise ActionRejected('队列效果回执未绑定原动作与真实来源')
        target = _target(run,source['action']['queue_guard']['target_key'])
        document = read_reference(target['document_ref'],self.engine.data_dir,maximum=8*1024*1024)
        try:
            submitted = datetime.fromisoformat(operation['submitted_at'])
            if submitted.tzinfo is None:
                raise ValueError('timezone missing')
        except (ValueError,TypeError):
            raise ActionRejected('队列效果提交时间无法核实') from None
        expected = {'binding':run['baseline']['binding'],'phase':guard['phase'],
            'baseline_ref':action['effect_baseline_ref'],'effect_id':baseline['effect_id'],
            'operation_id':operation['operation_id'],'submitted_at':submitted.timestamp(),
            'source_operation_id':source['operation_id'],'target':target,'source_document':document,
            'row_identity':guard['row_identity']}
        verified = verify_queue_effect_receipt(proof,expected)
        if verified.get('verified') is not True or result.get('status') != 'verified':
            raise ActionRejected('队列新增或删除效果仍未核实')
        current = proof['current_snapshot']
        previous = proof['previous_snapshot']
        refs = [evidence_ref,action['effect_baseline_ref'],
                *self._snapshot_files(operation,previous),*self._snapshot_files(operation,current)]
        receipt = {'evidence_ref':evidence_ref,'queue_verification':True,
                   'queue_effect':verified['queue_effect'],'added_rows':verified['added_rows'],
                   'deleted_rows':verified.get('deleted_rows',[]),'matched_rows':verified['matched_rows'],
                   'target_key':guard['target_key'],'source_operation_id':source['operation_id']}
        encoded,digest = _encode(receipt)
        task_id,operation_id = operation['task_id'],operation['operation_id']
        with self.engine._transaction():
            lifecycle = self.workflow.management._lifecycle(task_id)['state']
            row = self.workflow._operation_row(operation_id)
            if (lifecycle not in {'active','trashed'} or _run(self.db,operation) != run
                    or row['submitted_at'] != operation['submitted_at']
                    or row['payload_hash'] != operation['payload_hash']):
                raise ActionRejected('队列效果对账期间状态已经变化')
            _effect_source_operation(self.workflow,operation,action['effect_source'])
            if row['status'] == 'verified' and row['result'] == encoded:
                return self.workflow._operation(row)
            if row['status'] not in {'submitted','unknown'} or not row['submitted_at']:
                raise ActionRejected('队列效果已经核对或未提交')
            for deleted in verified.get('deleted_rows',[]):
                owner = run['owned_rows'].get(deleted)
                if (not owner or owner['operation_id'] != source['operation_id']
                        or owner['evidence_ref'] != source_input['receipt_ref']
                        or deleted in run['baseline']['row_ids']):
                    raise ActionRejected('删除效果没有原本的独占所有权')
                del run['owned_rows'][deleted]
            self._own(task_id,refs,existing_only=lifecycle=='trashed')
            current_ids = Counter(item['identity'] for item in current['rows'])
            for added in verified['added_rows']:
                if current_ids[added] == 1 and added not in run['baseline']['row_ids']:
                    run['owned_rows'][added] = {'operation_id':operation_id,'evidence_ref':evidence_ref}
            self._update_run(operation,run)
            self.db.execute("UPDATE workflow_operations SET status='verified',result=? WHERE operation_id=?",
                            (encoded,operation_id))
            self.db.execute('UPDATE workflow_slots SET barrier_operation_id=NULL WHERE slot=? AND barrier_operation_id=?',
                            (row['slot'],operation_id))
            self.workflow._publish_event(task_id,'operation.result',
                {'operation_id':operation_id,'status':'verified','result':receipt},
                event_key=f'operation:{operation_id}:verified:{digest}',revision=row['revision'],epoch=row['epoch'])
            return self.workflow._operation(self.workflow._operation_row(operation_id))

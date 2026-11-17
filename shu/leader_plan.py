"""Versioned read-only plans in the TaskEngine transaction and outbox.

Leases fence late agents; budgets belong to stable task/step identities rather
than a model turn or graph checkpoint. No network, model or GUI runs in a writer.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import uuid
import unicodedata

from .core import ActionRejected, _utc_now
from .workflow_store import _encode


DEFAULT_POLICY = {'primary': {'pages': 4, 'milliseconds': 30000},
                  'backup': {'pages': 2, 'milliseconds': 30000}}
STEP_STATUSES = frozenset({'two_verified', 'primary_only', 'search_incomplete',
                          'no_box_match', 'no_jobs_yet', 'deferred'})


def _activity_label(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def activity_query_matches(query, canonical):
    """A base activity query may resolve to its explicitly labelled rerun.

    Keep the canonical edition on the plan. An explicitly requested edition is
    never stripped, and unrelated/partially matching activity names still fail.
    """
    left, right = _activity_label(query), _activity_label(canonical)
    return left == right or bool(left) and right == left + '复刻'


def read_reference(ref, root, *, maximum=4 * 1024 * 1024):
    if not isinstance(ref, dict) or not isinstance(ref.get('path'), str):
        raise ValueError('资料引用格式无效')
    path = Path(ref['path']).resolve(strict=True)
    if not path.is_relative_to(Path(root).resolve()) or not path.is_file():
        raise ValueError('资料引用越出任务资料目录')
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum or hashlib.sha256(raw).hexdigest() != ref.get('sha256'):
        raise ValueError('资料引用内容已变化或超过大小限制')
    return json.loads(raw)


def _policy(value):
    result = copy.deepcopy(DEFAULT_POLICY)
    if not isinstance(value, dict) or set(value) - set(result):
        raise ValueError('检索预算格式无效')
    for phase, changes in value.items():
        if not isinstance(changes, dict) or set(changes) - {'pages', 'milliseconds'}:
            raise ValueError('检索预算格式无效')
        for key, number in changes.items():
            if type(number) is not int or not 0 <= number <= DEFAULT_POLICY[phase][key]:
                raise ValueError('检索预算不能超过已授权上限')
            result[phase][key] = number
    return result


class PlanStore:
    def __init__(self, workflow):
        self.workflow, self.engine = workflow, workflow.engine
        self.db = self.engine._db
        statements = (
            """CREATE TABLE IF NOT EXISTS leader_plans (
              task_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
              payload_hash TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(task_id,revision),
              FOREIGN KEY(task_id,revision) REFERENCES workflow_contracts(task_id,revision))""",
            """CREATE TABLE IF NOT EXISTS leader_steps (
              task_id TEXT NOT NULL, revision INTEGER NOT NULL, step_id TEXT NOT NULL,
              ordinal INTEGER NOT NULL, stage_key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
              result TEXT, PRIMARY KEY(task_id,revision,step_id),
              FOREIGN KEY(task_id,revision) REFERENCES leader_plans(task_id,revision))""",
            """CREATE TABLE IF NOT EXISTS leader_leases (
              lease_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
              epoch INTEGER NOT NULL, step_id TEXT NOT NULL, dispatch_id TEXT NOT NULL
              REFERENCES workflow_dispatches(dispatch_id), status TEXT NOT NULL DEFAULT 'active')""",
            """CREATE TABLE IF NOT EXISTS leader_budget (
              reservation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, step_id TEXT NOT NULL,
              lease_id TEXT NOT NULL REFERENCES leader_leases(lease_id), phase TEXT NOT NULL,
              pages INTEGER NOT NULL, reserved_ms INTEGER NOT NULL, elapsed_ms INTEGER,
              received_ref TEXT, created_at TEXT NOT NULL)""",
        )
        with self.engine._transaction():
            for statement in statements:
                self.db.execute(statement)
            for table in ('leader_steps','leader_leases'):
                if 'attempt' not in {r['name'] for r in self.db.execute('PRAGMA table_info('+table+')')}:
                    self.db.execute('ALTER TABLE '+table+' ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1')
            self.db.execute('CREATE TABLE IF NOT EXISTS leader_step_attempts ('
                'task_id TEXT NOT NULL,revision INTEGER NOT NULL,step_id TEXT NOT NULL,attempt INTEGER NOT NULL,'
                'status TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(task_id,revision,step_id,attempt))')

    def _state(self, task_id, revision, epoch, *, active=False):
        state = self.workflow._state(task_id)
        if state['revision'] != revision or state['epoch'] != epoch:
            raise ActionRejected('任务版本或执行代际已变化')
        if active and (not state['writes_enabled'] or state['status'] in
                       {'paused', 'cancelled', 'completed', 'failed', 'needs_input'}):
            raise ActionRejected('任务已停止或暂停，不能继续检索')
        return state

    def _plan(self, task_id, revision):
        row = self.db.execute('SELECT * FROM leader_plans WHERE task_id=? AND revision=?',
                              (task_id, revision)).fetchone()
        if row is None:
            return None
        result = json.loads(row['payload'])
        if _encode(result)[1] != row['payload_hash']:
            raise ValueError('计划内容完整性校验失败')
        return result

    def current(self, task_id):
        with self.engine._lock:
            state = self.workflow._state(task_id)
            plan = self._plan(task_id, state['revision'])
            if plan is None:
                return None
            value = copy.deepcopy(plan)
            value['steps'] = [dict(row) for row in self.db.execute(
                'SELECT step_id,ordinal,stage_key,status,result,attempt FROM leader_steps '
                'WHERE task_id=? AND revision=? ORDER BY ordinal', (task_id, state['revision']))]
            for step in value['steps']:
                step['result'] = json.loads(step['result']) if step['result'] else None
            value['epoch'] = state['epoch']
            return value

    def _prepare(self, task_id, revision, structure_ref, ordered_steps, search_policy, *, single_import=False,
                 import_stage_keys=None, import_contract=None, research_contract=None):
        structure = read_reference(structure_ref, self.engine.data_dir)
        if (not isinstance(structure, dict) or structure.get('ok') is not True
                or structure.get('catalog_complete') is not True):
            raise ValueError('计划需要完整真实目录结构')
        stages = structure.get('stages', [])
        index = {s['stage_key']: s for s in stages}
        expected = [key for group in structure.get('groups', []) for key in group['stage_keys']
                    if key not in structure.get('excluded_stage_keys', [])]
        valid_order = (len(ordered_steps) == 1 and ordered_steps[0] in expected
                       if single_import else expected == ordered_steps)
        if research_contract and not single_import and import_stage_keys is None:
            from .chapter_scope import resolved_contract
            from .maa_queue_plan import ordered_import_candidates, select_research_stages
            scoped = resolved_contract(research_contract, structure)
            allowed = [r['stage_key'] for r in select_research_stages(ordered_import_candidates(structure), scoped)]
            valid_order = ordered_steps == allowed
        if import_stage_keys is not None:
            if single_import or not isinstance(import_stage_keys, list) or not import_contract:
                raise ValueError('多作业导入子集需要唯一的当前任务合同')
            from .maa_queue_plan import ordered_import_candidates, select_import_stages, select_research_stages
            candidates = ordered_import_candidates(structure)
            imported = [row['stage_key'] for row in select_import_stages(candidates,import_contract)]
            queried = [row['stage_key'] for row in select_research_stages(candidates,import_contract)]
            valid_order = (bool(imported) and import_stage_keys == imported and ordered_steps == queried
                           and not (set(imported)-set(queried)))
        if (len(index) != len(stages) or len(expected) != len(set(expected))
                or not valid_order
                or set(expected) != set(index) - set(structure.get('excluded_stage_keys', []))):
            raise ValueError('计划顺序与已审核的真实目录不一致，不能静默删关或换序')
        deferred = []
        for exclusion in structure.get('exclusions', []):
            if exclusion.get('opens_at'):
                member = next((stage for stage in stages if stage['stage_key'] == exclusion.get('stage_key')
                    or stage.get('stage_id') == exclusion.get('stage_id')), None)
                if member is not None:
                    deferred.append({'stage_key': member['stage_key'], 'stage_code': member['display_code'],
                        'opens_at': exclusion['opens_at'], 'source_refs': exclusion.get('source_refs', []),
                        'reason': exclusion.get('reason'), 'status': 'pending_unscheduled',
                        'action': '开放后重新核对作业上传情况，再询问是否执行'})
        result = dict(task_id=task_id, revision=revision, structure_ref=structure_ref,
                    activity_id=structure.get('activity_id'), activity_name=structure.get('activity_name'),
                    groups=structure['groups'], stages=stages, ordered_stage_keys=ordered_steps,
                    search_policy=_policy(search_policy), deferred_stages=deferred)
        if import_stage_keys is not None:
            result['import_stage_keys'] = list(import_stage_keys)
        return result

    def _insert(self, plan):
        encoded, digest = _encode(plan)
        self.db.execute('INSERT INTO leader_plans VALUES(?,?,?,?,?)',
                        (plan['task_id'], plan['revision'], encoded, digest, _utc_now()))
        for ordinal, key in enumerate(plan['ordered_stage_keys']):
            step_id = hashlib.sha256(('stage:' + key).encode()).hexdigest()
            self.db.execute('INSERT INTO leader_steps(task_id,revision,step_id,ordinal,stage_key) '
                            'VALUES(?,?,?,?,?)', (plan['task_id'], plan['revision'], step_id, ordinal, key))

    def register(self, task_id, *, expected_revision, expected_epoch, structure_ref,
                 ordered_steps, search_policy, single_import=False, import_stage_keys=None):
        with self.engine._lock:
            self.workflow.management.require_active(task_id)
            current = self._state(task_id, expected_revision, expected_epoch, active=True)
            research_contract = current['contract']
        import_contract = None
        if import_stage_keys is not None:
            with self.engine._lock:
                self.workflow.management.require_active(task_id)
                current = self._state(task_id, expected_revision, expected_epoch, active=True)
                from .chapter_scope import resolved_contract
                import_contract = resolved_contract(current['contract'], read_reference(structure_ref,self.engine.data_dir))
                if (import_contract.get('current_deliverable') != 'maa_import'
                        or import_contract.get('source_task_id')):
                    raise ActionRejected('多关导入计划需要当前独立的导入合同')
        plan = self._prepare(task_id, expected_revision, structure_ref, ordered_steps, search_policy,
                             single_import=single_import, import_stage_keys=import_stage_keys,
                             import_contract=import_contract, research_contract=research_contract)
        with self.engine._transaction():
            current = self._state(task_id, expected_revision, expected_epoch, active=True)
            if single_import and (current['contract'].get('current_deliverable') != 'maa_import'
                                  or current['contract'].get('source_task_id')):
                raise ActionRejected('单关导入计划需要当前独立的导入合同')
            from .chapter_scope import resolved_contract
            resolved = resolved_contract(current['contract'], read_reference(structure_ref,self.engine.data_dir))
            activity = resolved.get('activity_name')
            if activity and not activity_query_matches(activity, plan['activity_name']):
                raise ActionRejected('计划活动与本次任务指定活动不一致')
            previous = self._plan(task_id, expected_revision)
            if previous is not None:
                if previous != plan:
                    raise ActionRejected('已登记计划只能通过版本修订更改')
                return previous
            self._insert(plan)
            self.workflow._publish_event(task_id, 'plan.registered',
                {'plan_revision': expected_revision, 'structure_ref': structure_ref,
                 'stage_count': len(ordered_steps)}, event_key=f'plan:{task_id}:{expected_revision}')
        return plan

    def revise(self, task_id, *, expected_revision, expected_epoch, change, reason):
        if not isinstance(change, dict) or set(change) - {'contract', 'structure_ref', 'ordered_steps', 'search_policy'}:
            raise ValueError('计划修订字段无效')
        before = self.current(task_id)
        if before is None:
            raise ValueError('任务尚未登记计划')
        prepared = self._prepare(task_id, expected_revision + 1,
            change.get('structure_ref', before['structure_ref']),
            change.get('ordered_steps', before['ordered_stage_keys']),
            change.get('search_policy', before['search_policy']))
        with self.engine._transaction():
            state = self._state(task_id, expected_revision, expected_epoch)
            contract = copy.deepcopy(state['contract'])
            update = change.get('contract', {})
            if not isinstance(update, dict):
                raise ValueError('合同修订必须为对象')
            contract.update(update)
            if contract.get('activity_name') and not activity_query_matches(contract['activity_name'], prepared['activity_name']):
                raise ActionRejected('修订后的合同活动与目录活动不一致')
            encoded, digest = _encode(contract)
            command_id = uuid.uuid4().hex
            row = self.engine._invalidate(task_id, status=state['status'], goal=contract.get('goal', state['goal']))
            self.db.execute('INSERT INTO workflow_contracts VALUES(?,?,?,?,?,?,?)',
                (task_id, expected_revision + 1, command_id, row['goal'], encoded, digest, _utc_now()))
            self.db.execute('UPDATE workflow_tasks SET revision=?,epoch=?,control_seq=control_seq+1 '
                            'WHERE task_id=?', (expected_revision + 1, row['version'], task_id))
            self.workflow._revoke_operations(task_id)
            self.db.execute("UPDATE leader_leases SET status='revoked' WHERE task_id=? AND status='active'", (task_id,))
            self._insert(prepared)
            self.workflow._publish_event(task_id, 'plan.revised',
                {'reason': str(reason)[:1200], 'previous_revision': expected_revision},
                event_key='plan-revision:' + command_id)
        return prepared

    def _budget(self, task_id, step_id, policy, attempt=1):
        result = {}
        for phase, maximum in policy.items():
            used = self.db.execute('SELECT COALESCE(SUM(b.pages),0), '
                'COALESCE(SUM(COALESCE(b.elapsed_ms,b.reserved_ms)),0) FROM leader_budget b '
                'JOIN leader_leases l ON l.lease_id=b.lease_id '
                'WHERE b.task_id=? AND b.step_id=? AND b.phase=? AND l.attempt=?',
                (task_id, step_id, phase, attempt)).fetchone()
            result[phase] = dict(pages_used=used[0], milliseconds_used=used[1],
                pages_remaining=max(0, maximum['pages'] - used[0]),
                milliseconds_remaining=max(0, maximum['milliseconds'] - used[1]))
        return result

    def claim_next(self, task_id, *, expected_revision, expected_epoch, dispatch_id):
        with self.engine._transaction():
            self._state(task_id, expected_revision, expected_epoch, active=True)
            plan = self._plan(task_id, expected_revision)
            if plan is None:
                raise ActionRejected('Leader尚未登记有序计划')
            dispatch = self.workflow._dispatch_row(dispatch_id)
            if (dispatch['task_id'] != task_id or dispatch['revision'] != expected_revision
                    or dispatch['epoch'] != expected_epoch or dispatch['role'] != 'research'
                    or dispatch['status'] not in {'pending', 'running'}):
                raise ActionRejected('研究委派与计划版本不一致')
            step = self.db.execute("SELECT * FROM leader_steps WHERE task_id=? AND revision=? AND status='pending' "
                'ORDER BY ordinal LIMIT 1', (task_id, expected_revision)).fetchone()
            if step is None:
                return None
            old = self.db.execute("SELECT * FROM leader_leases WHERE task_id=? AND revision=? AND step_id=? "
                "AND status='active'", (task_id, expected_revision, step['step_id'])).fetchone()
            owner_active = (old and old['epoch'] == expected_epoch
                            and self.workflow._dispatch_row(old['dispatch_id'])['status'] in {'pending', 'running'})
            if owner_active:
                if old['dispatch_id'] != dispatch_id:
                    raise ActionRejected('当前关卡已由其他委派领取')
                lease_id = old['lease_id']
            else:
                if old:
                    self.db.execute("UPDATE leader_leases SET status='revoked' WHERE lease_id=?", (old['lease_id'],))
                lease_id = uuid.uuid4().hex
                self.db.execute('INSERT INTO leader_leases(lease_id,task_id,revision,epoch,step_id,dispatch_id,attempt) '
                    'VALUES(?,?,?,?,?,?,?)', (lease_id, task_id, expected_revision, expected_epoch, step['step_id'], dispatch_id,step['attempt']))
            return dict(task_id=task_id, revision=expected_revision, epoch=expected_epoch,
                step_id=step['step_id'], dispatch_id=dispatch_id, lease_id=lease_id,
                resolved_stage=next(s for s in plan['stages'] if s['stage_key'] == step['stage_key']),
                attempt=step['attempt'], search_policy=plan['search_policy'],
                budget=self._budget(task_id, step['step_id'], plan['search_policy'],step['attempt']))

    def _lease(self, lease):
        self._state(lease['task_id'], lease['revision'], lease['epoch'], active=True)
        row = self.db.execute('SELECT * FROM leader_leases WHERE lease_id=?', (lease['lease_id'],)).fetchone()
        fields = ('task_id', 'revision', 'epoch', 'step_id', 'dispatch_id')
        if row is None or row['status'] != 'active' or any(row[k] != lease[k] for k in fields):
            raise ActionRejected('步骤租约已失效或已封口')
        step=self.db.execute('SELECT attempt,status FROM leader_steps WHERE task_id=? AND revision=? AND step_id=?',
            (row['task_id'],row['revision'],row['step_id'])).fetchone()
        if (step is None or step['status']!='pending' or step['attempt']!=row['attempt']
                or lease.get('attempt',1)!=row['attempt']):
            raise ActionRejected('旧补查轮次不能推进当前步骤')
        dispatch = self.workflow._dispatch_row(row['dispatch_id'])
        if dispatch['status'] not in {'pending', 'running'}:
            raise ActionRejected('该委派已经结束，不能继续使用租约')
        return row

    def budget(self, lease):
        with self.engine._lock:
            self._lease(lease)
            plan = self._plan(lease['task_id'], lease['revision'])
            return self._budget(lease['task_id'], lease['step_id'], plan['search_policy'],lease.get('attempt',1))

    def reserve_budget(self, lease, *, phase, pages, milliseconds):
        if phase not in DEFAULT_POLICY or type(pages) is not int or pages < 0 or type(milliseconds) is not int or milliseconds <= 0:
            raise ValueError('预算预留参数无效')
        with self.engine._transaction():
            self._lease(lease)
            plan = self._plan(lease['task_id'], lease['revision'])
            budget = self._budget(lease['task_id'], lease['step_id'], plan['search_policy'],lease.get('attempt',1))[phase]
            if pages > budget['pages_remaining'] or milliseconds > budget['milliseconds_remaining']:
                raise ActionRejected('当前关卡补搜批次或时间预算已用尽')
            identifier = uuid.uuid4().hex
            self.db.execute('INSERT INTO leader_budget VALUES(?,?,?,?,?,?,?,?,?,?)',
                (identifier, lease['task_id'], lease['step_id'], lease['lease_id'], phase,
                 pages, milliseconds, None, None, _utc_now()))
            return dict(reservation_id=identifier, **{k: lease[k] for k in ('task_id', 'step_id', 'lease_id')})

    def settle_budget(self, reservation, *, elapsed_ms, received_ref=None):
        if type(elapsed_ms) is not int or elapsed_ms < 0:
            raise ValueError('耗时必须为非负整数毫秒')
        with self.engine._transaction():
            row = self.db.execute('SELECT * FROM leader_budget WHERE reservation_id=?',
                                  (reservation['reservation_id'],)).fetchone()
            if row is None or any(row[k] != reservation[k] for k in ('task_id', 'step_id', 'lease_id')):
                raise ActionRejected('预算回执与预留不一致')
            if row['elapsed_ms'] is not None:
                if row['elapsed_ms'] != elapsed_ms:
                    raise ActionRejected('预算回执不可重写')
                return
            encoded = _encode(received_ref)[0] if received_ref is not None else None
            self.db.execute('UPDATE leader_budget SET elapsed_ms=?,received_ref=? WHERE reservation_id=?',
                            (elapsed_ms, encoded, row['reservation_id']))

    def close_step(self, lease, result):
        if not isinstance(result, dict) or result.get('status') not in STEP_STATUSES:
            raise ValueError('步骤结果状态无效')
        status = result['status']
        if status in {'two_verified', 'primary_only'} and not result.get('primary_ref'):
            raise ValueError('已交付状态必须有真实主选凭据')
        if status == 'two_verified' and (not result.get('backup_ref') or result['backup_ref'] == result['primary_ref']):
            raise ValueError('两份作业必须有不同凭据')
        verified = []
        roster_snapshot = None
        if status in {'two_verified', 'primary_only'}:
            # Rehash bounded files outside the Stop writer. Inside it, recheck
            # immutable DB receipts, lease, revision and current roster version.
            from .code_delivery import CodeDeliveryStore
            from .roster_match import roster_ref
            deliveries = CodeDeliveryStore(self.workflow, self.engine.data_dir)
            roster_snapshot = roster_ref(deliveries.roster_store.current('local-default') or {})
            for kind in ('primary', 'backup'):
                if result.get(kind + '_ref'):
                    code = deliveries.read_code(result[kind + '_ref'])
                    if any(code[field] != lease[field] for field in ('task_id', 'revision', 'step_id')):
                        raise ActionRejected('代码凭据不属于当前任务步骤')
                    verified.append((result[kind + '_ref'], code['id']))
            if len({identifier for ref, identifier in verified}) != len(verified):
                raise ActionRejected('主备必须为两个不同的真实作业ID')
        encoded = _encode(result)[0]
        with self.engine._transaction():
            self._lease(lease)
            if verified:
                if roster_ref(deliveries.roster_store.current('local-default') or {}) != roster_snapshot:
                    raise ActionRejected('封口前本地阵容版本已更新')
                for ref, identifier in verified:
                    row = self.db.execute('SELECT * FROM code_receipts WHERE id=?', (ref['receipt_id'],)).fetchone()
                    if (row is None or row['hash'] != ref['sha256'] or row['task_id'] != lease['task_id']
                            or row['revision'] != lease['revision'] or row['step_id'] != lease['step_id']):
                        raise ActionRejected('代码登记已变化，不能封口')
            self.db.execute('UPDATE leader_steps SET status=?,result=? WHERE task_id=? AND revision=? AND step_id=?',
                (status, encoded, lease['task_id'], lease['revision'], lease['step_id']))
            self.db.execute('INSERT OR REPLACE INTO leader_step_attempts VALUES(?,?,?,?,?,?)',
                (lease['task_id'],lease['revision'],lease['step_id'],lease.get('attempt',1),status,encoded))
            self.db.execute("UPDATE leader_leases SET status='closed' WHERE lease_id=?", (lease['lease_id'],))
            self.workflow._publish_event(lease['task_id'], 'plan.step_closed',
                dict(step_id=lease['step_id'], status=status, dispatch_id=lease['dispatch_id']),
                event_key='step-closed:' + lease['lease_id'])
        return result

    def retry_failed(self, task_id, *, expected_revision, expected_epoch, dispatch_id):
        """One extra round for unresolved primaries; no successful step is erased."""
        with self.engine._transaction():
            self._state(task_id,expected_revision,expected_epoch,active=True)
            dispatch=self.workflow._dispatch_row(dispatch_id)
            if (dispatch['task_id']!=task_id or dispatch['revision']!=expected_revision
                    or dispatch['epoch']!=expected_epoch or dispatch['role']!='research'
                    or dispatch['status'] not in {'pending','running'}):
                raise ActionRejected('补查委派不属于当前计划')
            if self.db.execute("SELECT 1 FROM leader_steps WHERE task_id=? AND revision=? AND status='pending'",
                (task_id,expected_revision)).fetchone():
                return []
            rows=self.db.execute("SELECT * FROM leader_steps WHERE task_id=? AND revision=? AND attempt=1 "
                "AND status IN ('search_incomplete','no_jobs_yet') ORDER BY ordinal",(task_id,expected_revision)).fetchall()
            for row in rows:
                self.db.execute('INSERT OR IGNORE INTO leader_step_attempts VALUES(?,?,?,?,?,?)',
                    (task_id,expected_revision,row['step_id'],1,row['status'],row['result']))
                self.db.execute("UPDATE leader_steps SET attempt=2,status='pending',result=NULL "
                    'WHERE task_id=? AND revision=? AND step_id=?',(task_id,expected_revision,row['step_id']))
                self.db.execute("UPDATE leader_leases SET status='revoked' WHERE task_id=? AND revision=? AND step_id=?",
                    (task_id,expected_revision,row['step_id']))
                self.workflow._publish_event(task_id,'plan.retry_scheduled',{'step_id':row['step_id'],'attempt':2},
                    event_key=f"retry:{task_id}:{expected_revision}:{row['step_id']}:2")
            return [row['step_id'] for row in rows]

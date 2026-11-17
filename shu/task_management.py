"""Task-scoped dialogue and visibility authority in the workflow transaction."""
from __future__ import annotations

import uuid

from .core import ActionRejected, _nonempty, _utc_now
from .workflow_store import _encode


class TaskManagement:
    def __init__(self, store):
        self.store = store
        self.engine = store.engine
        self._db = store._db
        with self.engine._transaction():
            columns = {row['name'] for row in self._db.execute('PRAGMA table_info(workflow_tasks)')}
            if 'blocked_reason' not in columns:
                self._db.execute("ALTER TABLE workflow_tasks ADD COLUMN blocked_reason TEXT NOT NULL DEFAULT ''")
            for sql in (
                """CREATE TABLE IF NOT EXISTS workflow_lifecycle (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id), state TEXT NOT NULL,
                    updated_at TEXT NOT NULL, original_status TEXT, operation_id TEXT)""",
                """CREATE TABLE IF NOT EXISTS workflow_messages (
                    request_id TEXT PRIMARY KEY, text TEXT NOT NULL, mode TEXT NOT NULL,
                    target_task_id TEXT, task_id TEXT REFERENCES tasks(id), relation TEXT,
                    status TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS workflow_questions (
                    clarification_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    prompt TEXT NOT NULL, kind TEXT NOT NULL, revision INTEGER NOT NULL,
                    epoch INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                    answered_at TEXT, answer_request_id TEXT)""",
                "CREATE INDEX IF NOT EXISTS workflow_questions_task ON workflow_questions(task_id,status)",
            ):
                self._db.execute(sql)

    def _lifecycle(self, task_id):
        self.engine._task_row(task_id)
        row = self._db.execute('SELECT * FROM workflow_lifecycle WHERE task_id=?', (task_id,)).fetchone()
        return dict(row) if row else {'task_id': task_id, 'state': 'active', 'updated_at': None,
                                     'original_status': None, 'operation_id': None}

    def lifecycle(self, task_id):
        with self.engine._lock:
            self.engine._ensure_open()
            return self._lifecycle(task_id)

    def require_active(self, task_id):
        with self.engine._lock:
            self.engine._ensure_open()
            if self._lifecycle(task_id)['state'] != 'active':
                raise ActionRejected('任务已回收或正在删除，普通操作已禁用')

    def start_run(self, task_id, *, expected_epoch):
        """Serialize worker launch admission against recycle and trusted control."""
        with self.engine._transaction():
            self.require_active(task_id)
            state = self.store._state(task_id)
            if state['epoch'] != expected_epoch:
                raise ActionRejected('排队任务版本已经改变，旧运行不能启动')
            self.store._fresh(state, writes=True)
            if state['status'] in {'completed', 'failed', 'cancelled', 'paused', 'needs_input'}:
                raise ActionRejected('当前任务状态不能启动运行')
            self._db.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?", (_utc_now(), task_id))
            self.engine._append_event(self.engine._task_row(task_id), 'status_changed', 'MAA 工作流从实时证据推进', {'status': 'running'})
            return self.store._state(task_id)

    def accept_message(self, request_id, text, *, mode='auto', target_task_id=None):
        request_id = _nonempty(request_id, 'request id')
        text = _nonempty(text, 'message')
        if mode not in {'auto', 'new', 'chat', 'task'}:
            raise ValueError('invalid message mode')
        if mode == 'task' and not target_task_id:
            raise ValueError('task mode requires target_task_id')
        with self.engine._transaction():
            previous = self._db.execute('SELECT * FROM workflow_messages WHERE request_id=?', (request_id,)).fetchone()
            if previous:
                if (previous['text'], previous['mode'], previous['target_task_id']) != (text, mode, target_task_id):
                    raise ValueError('request id collision with different message')
                return dict(previous)
            if target_task_id:
                self.require_active(target_task_id)
            now = _utc_now()
            self._db.execute('INSERT INTO workflow_messages VALUES(?,?,?,?,?,?,?,?,?,?)',
                (request_id, text, mode, target_task_id, None, None, 'accepted', '', now, now))
            return dict(self._db.execute('SELECT * FROM workflow_messages WHERE request_id=?', (request_id,)).fetchone())

    def _finish_message(self, request_id, *, task_id=None, relation='new', status='applied', summary=''):
        row = self._db.execute('SELECT * FROM workflow_messages WHERE request_id=?', (request_id,)).fetchone()
        if row is None:
            raise KeyError(request_id)
        if task_id:
            self.require_active(task_id)
        if task_id and row['target_task_id'] and row['target_task_id'] != task_id:
            # Explicitly selecting a completed import chooses its evidence as
            # the source of a new gameplay request, not permission to rewrite
            # the old import's no-start contract or select an unrelated task.
            allowed=False
            if row['mode']=='task' and relation=='followup':
                source_id=row['target_task_id']
                self.require_active(source_id)
                source=self.store._state(source_id)
                target=self.store._state(task_id)
                contract=target['contract']
                allowed=(source['status']=='completed' and source['control_kind']=='import_verified'
                    and source['contract'].get('current_deliverable')=='maa_import'
                    and contract.get('current_deliverable')=='gameplay'
                    and contract.get('relation')=='followup'
                    and contract.get('source_task_id')==source_id
                    and contract.get('request_id')==request_id)
            if not allowed:
                raise ActionRejected('消息的显式目标不能更改')
        if row['task_id'] and row['task_id'] != task_id:
            raise ActionRejected('消息已经绑定其他任务')
        if row['status'] in {'applied', 'failed', 'cancelled'}:
            if (row['task_id'], row['relation'], row['status'], row['summary']) != (task_id, relation, status, summary):
                raise ActionRejected('消息处理结果已经固定')
            return dict(row)
        self._db.execute('UPDATE workflow_messages SET task_id=?,relation=?,status=?,summary=?,updated_at=? WHERE request_id=?',
                         (task_id, relation, status, summary, _utc_now(), request_id))
        return dict(self._db.execute('SELECT * FROM workflow_messages WHERE request_id=?', (request_id,)).fetchone())

    def finish_message(self, request_id, *, task_id=None, relation='new', status='applied', summary=''):
        with self.engine._transaction():
            return self._finish_message(request_id, task_id=task_id, relation=relation, status=status, summary=summary)

    def messages(self, *, task_id=None, pending_only=False):
        with self.engine._lock:
            self.engine._ensure_open()
            clauses, args = [], []
            if task_id is not None:
                clauses.append('task_id=?')
                args.append(task_id)
            if pending_only:
                clauses.append("status IN ('accepted','pending','processing')")
            where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
            return [dict(row) for row in self._db.execute('SELECT * FROM workflow_messages' + where + ' ORDER BY rowid', args)]

    def pending_continuations(self, task_id=None):
        """Current durable handoffs; startup must preserve their epoch until consumed.

        A blocked answer is retained too. Callers must inspect current authority
        rather than treating publication as permission to launch a GUI worker.
        """
        with self.engine._lock:
            events = []
            for event in self.store.pending_events(task_id):
                if event['type'] != 'command.continue':
                    continue
                try:
                    self.require_active(event['task_id'])
                    self.store._fresh(event)
                except (ActionRejected, KeyError):
                    continue
                events.append(event)
            return events

    def pending_launches(self):
        """Durable request admissions not yet covered by a graph checkpoint."""
        with self.engine._lock:
            events=[]
            for event in self.store.pending_events():
                if event['type'] not in {'command.submitted','command.modified','command.continue'}:
                    continue
                try:
                    self.require_active(event['task_id'])
                    state=self.store._fresh(event)
                except (ActionRejected,KeyError):
                    continue
                request_id=state['contract'].get('request_id')
                receipt=self.store.request_receipt(request_id) if request_id else None
                if receipt and (receipt['task_id'],receipt['revision'],receipt['epoch']) == (
                        event['task_id'],event['revision'],event['epoch']):
                    events.append(event)
            return events

    def ack_launch(self,event_id):
        """Caller has completed the synchronous graph checkpoint handoff."""
        with self.engine._transaction():
            row=self._db.execute('SELECT * FROM workflow_outbox WHERE event_id=?',(event_id,)).fetchone()
            if row is None or row['type'] not in {'command.submitted','command.modified','command.continue'}:
                raise KeyError(event_id)
            self.store._event(row)
            self._db.execute('UPDATE workflow_outbox SET acked=1 WHERE event_id=?',(event_id,))

    def ack_continuation(self, event_id):
        """ACK only after the consumer synchronously saves its graph checkpoint."""
        with self.engine._transaction():
            row = self._db.execute('SELECT * FROM workflow_outbox WHERE event_id=?', (event_id,)).fetchone()
            if row is None or row['type'] != 'command.continue':
                raise KeyError(event_id)
            self.store._event(row)
            self._db.execute('UPDATE workflow_outbox SET acked=1 WHERE event_id=?', (event_id,))

    def _open_question(self, task_id, prompt, *, kind='business'):
        self.require_active(task_id)
        state = self.store._state(task_id)
        prompt = _nonempty(prompt, 'question')
        self._db.execute("UPDATE workflow_questions SET status='withdrawn' WHERE task_id=? AND status='open'", (task_id,))
        identifier = uuid.uuid4().hex
        self._db.execute('INSERT INTO workflow_questions VALUES(?,?,?,?,?,?,?,?,?,?)',
            (identifier, task_id, prompt, kind, state['revision'], state['epoch'], 'open', _utc_now(), None, None))
        return dict(self._db.execute('SELECT * FROM workflow_questions WHERE clarification_id=?', (identifier,)).fetchone())

    def open_question(self, task_id, prompt, *, kind='business'):
        with self.engine._transaction():
            return self._open_question(task_id, prompt, kind=kind)

    def question(self, task_id):
        with self.engine._lock:
            self.engine._ensure_open()
            if self._lifecycle(task_id)['state'] != 'active':
                return None
            state = self.store._state(task_id)
            row = self._db.execute("SELECT * FROM workflow_questions WHERE task_id=? AND status='open' AND revision=? AND epoch=? ORDER BY rowid DESC LIMIT 1",
                                   (task_id, state['revision'], state['epoch'])).fetchone()
            return dict(row) if row else None

    def answer_question(self, task_id, clarification_id, contract, *, request_id, expected_revision, expected_epoch):
        if type(contract) is not dict:
            raise TypeError('contract must be a JSON object')
        request_id = _nonempty(request_id, 'request id')
        encoded, digest = _encode(contract)
        with self.engine._transaction():
            self.require_active(task_id)
            if self._db.execute('SELECT 1 FROM workflow_messages WHERE request_id=?', (request_id,)).fetchone() is None:
                raise KeyError(request_id)
            state = self.store._state(task_id)
            q = self._db.execute('SELECT * FROM workflow_questions WHERE clarification_id=?', (clarification_id,)).fetchone()
            if (not q or q['task_id'] != task_id or q['status'] != 'open' or q['kind'] != 'business'
                    or q['revision'] != expected_revision or q['epoch'] != expected_epoch
                    or state['revision'] != expected_revision or state['epoch'] != expected_epoch
                    or state['epoch'] != state['task_epoch'] or state['status'] != 'needs_input'
                    or state['control_kind'] != 'needs_input'):
                raise ActionRejected('澄清问题已失效或当前任务不能通过回答继续')
            unresolved = self._db.execute("SELECT 1 FROM workflow_operations WHERE task_id=? AND status IN ('unknown','submitted','issued') LIMIT 1", (task_id,)).fetchone()
            blocked_reason = '回答已采纳，仍等待未决输入对账' if unresolved else ''
            goal = _nonempty(contract.get('goal', state['goal']), '任务目标')
            row = self.engine._invalidate(task_id, status='needs_input' if unresolved else 'draft', goal=goal)
            revision, command_id = state['revision'] + 1, uuid.uuid4().hex
            self._db.execute('INSERT INTO workflow_contracts VALUES(?,?,?,?,?,?,?)',
                (task_id, revision, command_id, goal, encoded, digest, _utc_now()))
            self._db.execute("UPDATE workflow_tasks SET revision=?,epoch=?,writes_enabled=?,control_seq=control_seq+1,control_kind=?,blocked_reason=? WHERE task_id=?",
                             (revision, row['version'], int(not unresolved), 'blocked' if unresolved else 'continue', blocked_reason, task_id))
            self.store._revoke_operations(task_id)
            self._db.execute("UPDATE workflow_questions SET status='answered',answered_at=?,answer_request_id=? WHERE clarification_id=?",
                             (_utc_now(), request_id, clarification_id))
            current = self.store._state(task_id)
            request = self.store._request_identity(contract, 'answer', goal, task_id)
            # request_id is explicit protocol identity even for old minimal payloads.
            if request is None:
                request = self.store._request_identity({**contract, 'request_id': request_id}, 'answer', goal, task_id)
            if request[0] != request_id:
                raise ActionRejected('澄清回答的消息编号不一致')
            self.store._record_request(request, current, contract,
                relation='answer', summary='已采纳澄清回答')
            payload = {'command_id': command_id, 'request_id': request_id, 'clarification_id': clarification_id,
                       'blocked_reason': blocked_reason}
            self.store._command(current, command_id, 'continue', payload)
            event = self.store._publish_event(task_id, 'command.continue', payload, event_key='answer:' + clarification_id)
            return {**current, 'continuation_event_id': event['event_id']}

    def trash(self, task_id, *, expected_epoch=None):
        with self.engine._transaction():
            self._adopt_legacy_for_management(task_id)
            life = self._lifecycle(task_id)
            if life['state'] == 'trashed':
                return life
            self.require_active(task_id)
            state = self.store._state(task_id)
            if expected_epoch is not None and state['epoch'] != expected_epoch:
                raise ActionRejected('任务版本已变化，不能回收旧版本')
            now, operation_id = _utc_now(), uuid.uuid4().hex
            self._db.execute('INSERT INTO workflow_lifecycle VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at,original_status=excluded.original_status,operation_id=excluded.operation_id',
                             (task_id, 'trashed', now, state['status'], operation_id))
            row = self.engine._invalidate(task_id, status='paused')
            self._db.execute("UPDATE workflow_tasks SET epoch=?,writes_enabled=0,control_seq=control_seq+1,control_kind='pause' WHERE task_id=?", (row['version'], task_id))
            self.store._revoke_operations(task_id)
            self._db.execute("UPDATE workflow_questions SET status='withdrawn' WHERE task_id=? AND status='open'", (task_id,))
            self._db.execute("UPDATE workflow_messages SET status='cancelled',updated_at=? WHERE (task_id=? OR target_task_id=?) AND status IN ('accepted','pending','processing')", (now, task_id, task_id))
            self.store._publish_event(task_id, 'task.trashed', {'operation_id': operation_id}, event_key=operation_id)
            return self._lifecycle(task_id)

    def restore(self, task_id):
        with self.engine._transaction():
            life = self._lifecycle(task_id)
            if life['state'] == 'active':
                return life
            if life['state'] != 'trashed':
                raise ActionRejected('正在彻底删除的任务不能恢复')
            restored_status = life['original_status'] if life['original_status'] in {'completed','failed','cancelled'} else 'paused'
            row = self.engine._invalidate(task_id, status=restored_status)
            self._db.execute("UPDATE workflow_tasks SET epoch=?,writes_enabled=0,control_seq=control_seq+1,control_kind='pause' WHERE task_id=?", (row['version'], task_id))
            self._db.execute("UPDATE workflow_lifecycle SET state='active',updated_at=?,operation_id=? WHERE task_id=?", (_utc_now(), uuid.uuid4().hex, task_id))
            self.store._revoke_operations(task_id)
            return self._lifecycle(task_id)

    def _adopt_legacy_for_management(self,task_id):
        """Make old core-only tasks manageable, without an executable command."""
        if self._db.execute('SELECT 1 FROM workflow_tasks WHERE task_id=?',(task_id,)).fetchone():
            return
        from .workflow_store import _encode
        old=self.engine._task_row(task_id)
        contract={'goal':old['goal'],'current_deliverable':'chat','legacy_management_only':True,
                  'allow_start':False,'attempt_limit':0,'task_spec':{'kind':'unknown'}}
        encoded,digest=_encode(contract)
        self._db.execute('INSERT INTO workflow_contracts VALUES(?,?,?,?,?,?,?)',
            (task_id,1,'legacy-management:'+task_id,old['goal'],encoded,digest,_utc_now()))
        self._db.execute('INSERT INTO workflow_tasks(task_id,revision,epoch,writes_enabled) VALUES(?,?,?,0)',
                         (task_id,1,old['version']))

    def list_tasks(self, *, trashed=False):
        with self.engine._lock:
            self.engine._ensure_open()
            states = {'trashed', 'purge_pending'} if trashed else {'active'}
            result = []
            for row in self._db.execute('SELECT id AS task_id FROM tasks ORDER BY rowid DESC').fetchall():
                life = self._lifecycle(row['task_id'])
                if life['state'] in states:
                    if self._db.execute('SELECT 1 FROM workflow_tasks WHERE task_id=?',(row['task_id'],)).fetchone():
                        task=self.store._state(row['task_id'])
                        task['legacy_only']=task['contract'].get('legacy_management_only') is True
                    else:
                        old=dict(self.engine._task_row(row['task_id']))
                        task={**old,'task_id':old['id'],'revision':0,'epoch':old['version'],
                              'writes_enabled':False,'legacy_only':True,'contract':{}}
                    result.append({**task, 'lifecycle': life['state'], 'lifecycle_state': life,
                                   'question':None if task.get('legacy_only') else self.question(row['task_id'])})
            return result

"""Visibility and cleanup for proven unbound requests, never workflow tasks.

The request lifecycle is persisted beside workflow admissions. It grants no
execution authority and only owns the exact request's messages/display record.
"""
from __future__ import annotations

import threading
import uuid

from .activity_history import _identifier, safe_text
from .core import ActionRejected, _utc_now


class ChatHistoryManagement:
    def __init__(self, management, memory, history=None, *, progress=None, stop_event=None):
        self.management = management
        self.engine = management.engine
        self._db = management._db
        self.memory = memory
        self.history = history
        self.progress = progress or (lambda payload: None)
        self.stop_event = stop_event or threading.Event()
        with self.engine._transaction():
            self._db.execute('''CREATE TABLE IF NOT EXISTS workflow_chat_lifecycle (
                request_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL,
                operation_id TEXT, text TEXT NOT NULL, summary TEXT NOT NULL,
                original_status TEXT NOT NULL, created_at TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '')''')
        # Repair interruption between the durable visibility change and the
        # separate conversation database, without touching task-owned rows.
        with self.engine._lock:
            visibility = [(row['request_id'], row['state']) for row in self._db.execute(
                "SELECT request_id,state FROM workflow_chat_lifecycle WHERE state!='purged'")]
        for request_id, state in visibility:
            if not self.memory.request_deleted(request_id) and self.memory.request_task(request_id) is None:
                self.memory.hide_request(request_id, hidden=state != 'active')

    def _identity(self, request_id):
        if not _identifier(request_id):
            raise ValueError('Invalid chat request identity')
        return request_id

    def _lifecycle(self, request_id):
        row = self._db.execute('SELECT * FROM workflow_chat_lifecycle WHERE request_id=?', (request_id,)).fetchone()
        return dict(row) if row else None

    def _message(self, request_id):
        with self.engine._lock:
            row = self._db.execute('SELECT * FROM workflow_messages WHERE request_id=?', (request_id,)).fetchone()
            return dict(row) if row else None

    def _unbound(self, request_id, message):
        if message and (message['task_id'] or message['target_task_id'] or message['mode'] == 'task'):
            raise ActionRejected('任务关联消息不能通过普通聊天管理')
        if message and message['status'] not in ('applied', 'failed', 'cancelled'):
            raise ActionRejected('对话仍在处理，请等待归属确定后再管理')
        try:
            if self.memory.request_task(request_id) or (self.history and self.history.request_task(request_id)):
                raise ActionRejected('消息已有明确任务归属，不能作为普通聊天处理')
        except ValueError as error:
            raise ActionRejected('消息归属冲突，不能作为普通聊天处理') from error

    def _known(self, request_id):
        self._identity(request_id)
        with self.engine._lock:
            self.engine._ensure_open()
            message, life = self._message(request_id), self._lifecycle(request_id)
        self._unbound(request_id, message)
        record = self.history.load_request(request_id) if self.history else None
        if message is None and record is None and life is None:
            raise KeyError(request_id)
        return message, record, life

    def require_active(self, request_id):
        """Reject replay of a recycled identity before it can enter routing."""
        self._identity(request_id)
        with self.engine._lock:
            life = self._lifecycle(request_id)
        if life and life['state'] != 'active':
            raise ActionRejected('该对话已回收或删除，请使用新的对话请求')

    def list_chats(self, *, trashed=False):
        with self.engine._lock:
            messages = {row['request_id']: dict(row) for row in self._db.execute('SELECT * FROM workflow_messages')}
            lives = {row['request_id']: dict(row) for row in self._db.execute('SELECT * FROM workflow_chat_lifecycle')}
        records = {row['request_id']: row for row in self.history.records()} if self.history else {}
        rows = []
        for request_id in messages.keys() | records.keys() | lives.keys():
            life, message, record = lives.get(request_id), messages.get(request_id), records.get(request_id)
            state = life['state'] if life else 'active'
            if state == 'purged' or (state != 'active') != trashed:
                continue
            try:
                self._unbound(request_id, message)
            except ActionRejected:
                continue
            if not message and not record and not life:
                continue
            source = message or record or life
            rows.append({'request_id': request_id, 'task_id': None, 'kind': 'chat' if message else 'legacy',
                'text': safe_text(source.get('text'), 12000),
                'summary': safe_text(source.get('summary'), 1200),
                'status': source.get('status') or source.get('original_status') or 'completed',
                'created_at': source.get('created_at') or source.get('timestamp') or '',
                'lifecycle': state, 'trashed_at': life['updated_at'] if life else None,
                'stage': life['stage'] if life else '', 'error': life['error'] if life else ''})
        return sorted(rows, key=lambda row: (row['created_at'], row['request_id']), reverse=True)

    def trash(self, request_id):
        message, record, life = self._known(request_id)
        if life and life['state'] != 'active':
            if life['state'] == 'trashed':
                return {'request_id': request_id, 'status': 'trashed', 'namespace': 'request'}
            raise ActionRejected('对话正在删除或已经删除')
        source = message or record
        with self.engine._transaction():
            self._db.execute('''INSERT INTO workflow_chat_lifecycle
                (request_id,state,updated_at,operation_id,text,summary,original_status,created_at)
                VALUES(?,'trashed',?,NULL,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET
                state='trashed',updated_at=excluded.updated_at''',
                (request_id, _utc_now(), source.get('text', ''), source.get('summary', ''),
                 source.get('status', 'completed'), source.get('created_at') or source.get('timestamp') or _utc_now()))
        self.memory.hide_request(request_id)
        return {'request_id': request_id, 'status': 'trashed', 'namespace': 'request'}

    def restore(self, request_id):
        _, _, life = self._known(request_id)
        if not life or life['state'] not in ('active', 'trashed'):
            raise ActionRejected('正在彻底删除的对话不能恢复')
        with self.engine._transaction():
            self._db.execute("UPDATE workflow_chat_lifecycle SET state='active',updated_at=? WHERE request_id=? AND state='trashed'",
                (_utc_now(), request_id))
        self.memory.hide_request(request_id, hidden=False)
        return {'request_id': request_id, 'status': 'active', 'namespace': 'request'}

    def request_purge(self, request_id):
        _, _, life = self._known(request_id)
        if life and life['state'] == 'purged':
            return {'request_id': request_id, 'status': 'completed', 'namespace': 'request'}
        if not life or life['state'] not in ('trashed', 'purge_pending'):
            raise ActionRejected('请先将对话移入回收箱')
        with self.engine._transaction():
            self._db.execute("UPDATE workflow_chat_lifecycle SET state='purge_pending',stage='queued',error='',operation_id=COALESCE(operation_id,?),updated_at=? WHERE request_id=? AND state IN ('trashed','purge_pending')",
                (uuid.uuid4().hex, _utc_now(), request_id))
        if not self.memory.request_deleted(request_id):
            self.memory.hide_request(request_id)
        return {'request_id': request_id, 'status': 'queued', 'namespace': 'request'}

    def pending_purges(self):
        with self.engine._lock:
            return [row[0] for row in self._db.execute("SELECT request_id FROM workflow_chat_lifecycle WHERE state='purge_pending'")]

    def cleanup_state(self):
        with self.engine._lock:
            return [{**dict(row), 'namespace': 'request'} for row in self._db.execute(
                "SELECT request_id,stage,error,updated_at FROM workflow_chat_lifecycle WHERE state='purge_pending'")]

    def _current(self, request_id, operation_id):
        life=self._lifecycle(request_id)
        if not life or life['state']!='purge_pending' or life['operation_id']!=operation_id:
            raise ActionRejected('对话清理生命周期已改变，旧操作已停止')

    def _stage(self, request_id, stage, error='', *, operation_id):
        with self.engine._transaction():
            self._current(request_id,operation_id)
            self._db.execute('UPDATE workflow_chat_lifecycle SET stage=?,error=?,updated_at=? WHERE request_id=? AND state=\'purge_pending\'',
                (stage, safe_text(error, 1200), _utc_now(), request_id))
        self.progress({'namespace': 'request', 'request_id': request_id, 'stage': stage, 'error': error})

    def purge(self, request_id):
        _, _, life = self._known(request_id)
        if life and life['state'] == 'purged':
            return {'request_id': request_id, 'status': 'completed', 'namespace': 'request'}
        if not life or life['state'] != 'purge_pending':
            raise ActionRejected('聊天清理尚未提交')
        operation_id=life['operation_id']
        try:
            steps = [('memory', lambda: self.memory.delete_request_messages(request_id))]
            if self.history:
                steps.append(('history', lambda: self.history.delete_request(request_id)))
            for stage, action in steps:
                if self.stop_event.is_set():
                    self._stage(request_id, 'paused',operation_id=operation_id)
                    return {'request_id': request_id, 'status': 'paused', 'namespace': 'request'}
                self._unbound(request_id, self._message(request_id))
                self._stage(request_id, stage,operation_id=operation_id)
                action()
            # Preserve the admission/tombstone identity while removing private text.
            self._stage(request_id, 'business',operation_id=operation_id)
            with self.engine._transaction():
                self._current(request_id,operation_id)
                message=self._message(request_id)
                if message and (message['task_id'] or message['target_task_id'] or message['mode']=='task'):
                    raise ActionRejected('消息归属已改变，停止普通聊天清理')
                self._db.execute("UPDATE workflow_messages SET text='',summary='',status='cancelled',updated_at=? WHERE request_id=? AND task_id IS NULL AND target_task_id IS NULL",
                    (_utc_now(), request_id))
                self._db.execute("UPDATE workflow_chat_lifecycle SET state='purged',stage='completed',text='',summary='',error='',updated_at=? WHERE request_id=? AND state='purge_pending'",
                    (_utc_now(), request_id))
            return {'request_id': request_id, 'status': 'completed', 'namespace': 'request'}
        except ActionRejected as error:
            return {'request_id': request_id, 'status': 'blocked', 'reason': str(error), 'namespace': 'request'}
        except Exception as error:
            try:
                self._stage(request_id, 'error', str(error),operation_id=operation_id)
            except ActionRejected:
                return {'request_id': request_id, 'status': 'blocked', 'reason': str(error), 'namespace': 'request'}
            return {'request_id': request_id, 'status': 'error', 'reason': str(error), 'namespace': 'request'}

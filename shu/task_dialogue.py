"""Task message admission and lifecycle facade; no model or GUI IO."""
from collections import deque
import threading
from .core import ActionRejected


class TaskDialogue:
    def __init__(self, controller):
        self.c = controller
        self.target = {'mode': 'auto', 'task_id': None}
        self.pending = deque()
        self.message_ids = {}
        self._cleanup_lock=threading.Lock()
        self._cleanup_run_lock=threading.RLock()
        self._cleanup_queue=deque()
        self._cleanup_tasks=set()
        self._cleanup_thread=None
        self._cleanup_stop=threading.Event()
        self._cleanup_closed=False
        self._chat_management=None
        self._fallback_history=None
        from .task_cleanup import TaskCleanup
        TaskCleanup.ensure_schema(self.management.engine)

    @property
    def management(self):
        return self.c.execution.store.management

    @property
    def history(self):
        attached=getattr(self.c,'task_history',None)
        if attached is not None:return attached
        if self._fallback_history is None and getattr(self.c,'data_dir',None) is not None:
            from .activity_history import ActivityHistory
            self._fallback_history=ActivityHistory(self.c.data_dir/'activity-history.json')
        return self._fallback_history

    @property
    def chats(self):
        from .chat_history_management import ChatHistoryManagement
        if self._chat_management is None:
            self._chat_management=ChatHistoryManagement(self.management,self.c.memory,
                self.history,progress=self._cleanup_progress,stop_event=self._cleanup_stop)
        self._chat_management.history=self.history
        return self._chat_management

    def set_target(self, task_id=None, mode='auto'):
        if mode not in {'auto', 'new', 'chat', 'task'}:
            raise ValueError('输入归属模式无效')
        if mode == 'task':
            self.management.require_active(task_id)
        self.target = {'mode': mode, 'task_id': task_id if mode == 'task' else None}
        self.c.changed.emit()

    def state(self):
        return {'tasks': self.management.list_tasks(),
                'trashed': self.management.list_tasks(trashed=True),
                'chats': self.chats.list_chats(), 'trashed_chats': self.chats.list_chats(trashed=True),
                'requests': self.management.messages(), 'input_target': dict(self.target),
                'current_task_id': self.c._task_id,'cleanup':self._cleanup_state()}

    def _cleanup_state(self):
        engine=self.management.engine
        with engine._lock:
            rows=[dict(row) for row in engine._db.execute(
                'SELECT task_id,stage,error,completed,updated_at FROM workflow_cleanup_jobs')] if engine._db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='workflow_cleanup_jobs'").fetchone() else []
        return rows+self.chats.cleanup_state()

    def accept(self, request_id, text):
        self.chats.require_active(request_id)
        item = self.management.accept_message(request_id, text,
            mode=self.target['mode'], target_task_id=self.target['task_id'])
        self.pending.append(item)
        return item

    def routed(self, request_id, outcome, contract):
        identifier = outcome.get('task_id') or contract.selected_task_id
        if contract.intent == 'chat':
            identifier = None
        status = 'applied' if outcome.get('ok', True) else 'failed'
        existing = next((m for m in self.management.messages() if m['request_id']==request_id), None)
        relation = contract.relation or 'new'
        if existing and existing['status']=='applied':
            identifier, relation = existing['task_id'], existing['relation']
        else:
            self.management.finish_message(request_id, task_id=identifier,
                relation=relation, status=status,
                summary=(outcome.get('reason') or outcome.get('reply', ''))[:1200])
        if identifier:
            self.c.memory.bind_request(request_id, identifier, self.message_ids.get(request_id, []))
        self.c.message_routed.emit({'request_id': request_id, 'task_id': identifier,
            'relation': relation, 'status': status,
            'summary': outcome.get('reason') or ('已归入任务' if identifier else '普通对话')})
        return identifier

    def record_message(self, role, text, *, request_id=None, task_id=None):
        row = self.c.memory.append(role, text, request_id=request_id, task_id=task_id)
        if request_id:
            self.message_ids.setdefault(request_id, []).append(row['id'])
        return row

    def trash(self, task_id):
        result = self.c.execution.trash(task_id)
        if self.target['task_id'] == task_id:
            self.target = {'mode': 'auto', 'task_id': None}
        if self.c._task_id == task_id:
            rows = self.management.list_tasks()
            self.c._task_id = rows[0]['task_id'] if rows else None
            self.c.execution.current_task_id = self.c._task_id
        self.c.changed.emit()
        return result

    def restore(self, task_id):
        result = self.management.restore(task_id)
        self.c.changed.emit()
        return result

    def trash_chat(self,request_id):
        result=self.chats.trash(request_id)
        self.c.changed.emit()
        return result

    def restore_chat(self,request_id):
        result=self.chats.restore(request_id)
        self.c.changed.emit()
        return result

    def _cleanup(self):
        from .task_cleanup import TaskCleanup
        return TaskCleanup(self.c.execution.store,runtime=self.c.runtime,memory=self.c.memory,
            history=self.history,graph_directory=self.c.execution.directory,
            graph_lock=self.c.execution.graph_lock,progress=self._cleanup_progress,
            stop_event=self._cleanup_stop)

    def _cleanup_progress(self,payload):
        signal=getattr(self.c,'cleanup_progress',None)
        if signal is not None and not getattr(self.c,'_closed',False):signal.emit(payload)

    def start_purge(self,task_id):
        """Only durable admission happens here. One owned worker performs IO."""
        return self._submit_cleanup('task',task_id,lambda:self._cleanup().request(task_id))

    def start_chat_purge(self,request_id):
        return self._submit_cleanup('request',request_id,lambda:self.chats.request_purge(request_id))

    def _submit_cleanup(self,namespace,identifier,admit):
        key=(namespace,identifier)
        field='request_id' if namespace=='request' else 'task_id'
        with self._cleanup_lock:
            if self._cleanup_closed:raise RuntimeError('正在退出，清理将在下次启动后继续')
            if key in self._cleanup_tasks:return {field:identifier,'namespace':namespace,'status':'queued'}
            result=admit()
            if result['status']=='completed':return result
            self._cleanup_tasks.add(key);self._cleanup_queue.append(key)
            if self._cleanup_thread is None or not self._cleanup_thread.is_alive():
                self._cleanup_thread=threading.Thread(target=self._run_cleanup,name='shu-task-cleanup',daemon=False)
                self._cleanup_thread.start()
        return result

    def _run_cleanup(self):
        while True:
            with self._cleanup_lock:
                if self._cleanup_closed or not self._cleanup_queue:
                    self._cleanup_thread=None
                    return
                namespace,identifier=self._cleanup_queue.popleft()
            field='request_id' if namespace=='request' else 'task_id'
            try:
                if namespace=='request':
                    with self._cleanup_run_lock:
                        result=self.chats.purge(identifier)
                    self.c.changed.emit()
                else:
                    result=self.purge(identifier)
                self._cleanup_progress({**result,'stage':result['status']})
            except Exception as error:
                self._cleanup_progress({field:identifier,'namespace':namespace,'stage':'error','status':'error','reason':str(error)})
            finally:
                with self._cleanup_lock:self._cleanup_tasks.discard((namespace,identifier))

    def resume_cleanup(self):
        with self.management.engine._lock:
            pending=[r[0] for r in self.management._db.execute(
                "SELECT task_id FROM workflow_lifecycle WHERE state='purge_pending'")]
        return ([self.start_purge(task_id) for task_id in pending]+
                [self.start_chat_purge(request_id) for request_id in self.chats.pending_purges()])

    def close(self):
        # The caller joins this owner before closing any database/projection.
        # No timer/lambda can issue a late write after controller destruction.
        with self._cleanup_lock:
            self._cleanup_closed=True;self._cleanup_stop.set()
            thread=self._cleanup_thread
        if thread is not None and thread is not threading.current_thread():thread.join()

    def purge(self, task_id):
        with self._cleanup_run_lock:
            return self._purge(task_id)

    def _purge(self,task_id):
        self.c.memory.sync_request_bindings(self.management.messages())
        job = self.c.execution._job
        if job and job.task_id == task_id:
            raise ActionRejected('任务仍在退出执行，请稍后重试彻底删除。')
        lock=self.c.execution.graph_lock
        if not lock.acquire(blocking=False):
            return {'task_id':task_id,'status':'blocked','reason':'工作流仍在运行，请结束当前执行后再彻底删除。'}
        try:
            self.c.execution.store.audit_abandoned_dispatches(task_id)
            result = self._cleanup().purge(task_id)
        finally:
            lock.release()
        self.c.changed.emit()
        return result

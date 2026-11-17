"""Qt product entry for the durable import-only MAA workflow.

SQLite owns authority. RuntimeStore is a disposable UI projection. No model,
graph or blocking UIA call runs under the coordinator's short state lock.
"""
from __future__ import annotations

import copy
from collections import deque
import json
from dataclasses import dataclass, field
from pathlib import Path
import re
import threading

from PySide6.QtCore import QObject, Signal, QTimer

from .core import ActionRejected, TaskNotFound, TERMINAL_STATUSES
from .providers import ProviderCancelled, ProviderError
from .workflow_store import WorkflowStore
from .activity_events import make_activity_event
from .leader_policy import completion_decision
from .battle_contract import authorize as authorize_battle


@dataclass
class _Run:
    task_id: str
    epoch: int
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    roles: object = None
    controls: list = field(default_factory=list)
    needs_resume: bool = False


def _error_summary(exc):
    message = re.sub(r'(?i)Bearer\s+\S+', 'Bearer [redacted]', str(exc))
    message = re.sub(r'\bsk-[A-Za-z0-9_.\-]+', '[Key 已隐藏]', message)
    return message[:600]


class MaaCoordinator(QObject):
    changed = Signal()
    activity = Signal(bool)
    event = Signal(str, str, dict)
    finished = Signal(dict)

    def __init__(self, engine, windows, runtime, *, role_config, task_config,
                 permissions, directory=None, knowledge=None, store=None,
                 role_factory=None, workflow_factory=None, recover=True, single_import_enabled=False,
                 queue_import_enabled=False):
        super().__init__()
        self.engine, self.windows, self.runtime = engine, windows, runtime
        self.directory = Path(directory or engine.data_dir)
        self.knowledge = knowledge
        self.role_config, self.task_config = role_config, task_config
        self.permissions = permissions
        self.single_import_enabled = single_import_enabled
        self.queue_import_enabled = queue_import_enabled
        self.store = store or WorkflowStore(engine)
        self.role_factory, self.workflow_factory = role_factory, workflow_factory
        self.current_task_id = None
        self._lock = threading.RLock()
        self._resources = threading.Lock()
        self.graph_lock = threading.RLock()
        self._job = self._pending = None
        self._queue = deque()
        self._closed = False
        self._persistence_fault = None
        self._control_threads = []
        self.control_timeout_seconds = 45.0
        if recover:
            recoverable={event['task_id'] for event in self.store.management.pending_launches()}
            # Application-owner startup only. Reopening a passive WorkflowStore
            # never resumes or revokes work; product startup explicitly does.
            for task in engine.list_tasks():
                if self.store.management.lifecycle(task['id'])['state'] != 'active':
                    self.store.audit_abandoned_dispatches(task['id'])
                    continue
                try:
                    state = self.store.current(task['id'])
                except TaskNotFound:
                    continue
                if task['status'] not in TERMINAL_STATUSES and state['writes_enabled'] and task['id'] not in recoverable:
                    self.store.control(task['id'], 'pause')
                if self.current_task_id is None:
                    self.current_task_id = state['task_id']
            QTimer.singleShot(0,self.recover_launches)

    def recover_launches(self):
        if self._closed:return
        with self._lock:
            for event in self.store.management.pending_launches():
                state=self.store.current(event['task_id'])
                if not state['writes_enabled']:continue
                if self._job and self._job.task_id==state['task_id']:continue
                job=_Run(state['task_id'],state['epoch'])
                if self._job:self._enqueue(job)
                else:self._launch(job)

    @property
    def active(self):
        with self._lock:
            return self._job is not None or self._pending is not None or bool(self._queue)

    def contract(self, task_id=None):
        identifier = task_id or self.current_task_id
        if not identifier:
            return {}
        try:
            return copy.deepcopy(self.store.current(identifier)['contract'])
        except TaskNotFound:
            return {}

    def research_output(self,task_id=None):
        from .research_delivery import deliverables,render_delivery
        from .leader_policy import roster_required,research_stage_codes,research_goal_diagnostics
        identifier=task_id or self.current_task_id
        if not identifier:return {'items':[],'summary':'当前没有已登记的作业查询结果。'}
        try:
            state=self.store.current(identifier)
            if state['contract'].get('current_deliverable') in {'copilot_codes', 'maa_import'}:
                from .code_delivery import CodeDeliveryStore
                # A directory-to-code follow-up owns NEW code receipts. Only an
                # import explicitly consumes the source task's existing jobs.
                source_id = (state['contract'].get('source_task_id') or identifier
                             if state['contract'].get('current_deliverable')=='maa_import' else identifier)
                result = CodeDeliveryStore(self.store, self.directory).summary(source_id)
                from .code_presentation import render_code_summary
                if state['contract'].get('current_deliverable') == 'maa_import':
                    result.update(query_complete=result['complete'], complete=False, goal_complete=False,
                                  source_task_id=source_id)
                return {**result, 'task_id': identifier, 'status': state['status'],
                        'summary': render_code_summary(result)}
            receipts=self.store.research_display_results(identifier)
            entry=receipts[0] if receipts else {};receipt=entry.get('receipt',{})
            generation=entry.get('generation',{'task_id':identifier,'revision':state['revision'],
                'current_epoch':state['epoch'],'source_epoch':None,
                'display_only':True,'execution_eligible':False})
            # Display admission checks the same task/goal binding as execution,
            # while explicitly retaining its own read-only source generation.
            from .research_delivery import _read
            root=self.engine.data_dir.resolve();records={};references=[];source_dispatch_ids=[]
            for ref in receipt.get('candidate_refs',[])[:100]:
                try:
                    record=_read(root,ref);stage=record.get('goal_stage') or {}
                    source_id=record.get('research_dispatch_id')
                    if (record.get('task_id')!=identifier or stage.get('task_id')!=identifier
                            or record.get('epoch')!=generation['source_epoch']
                            or stage.get('epoch')!=record.get('epoch')
                            or stage.get('goal')!=state['contract'].get('goal')):
                        continue
                    source=self.store.get_dispatch(source_id)
                    source_receipt=self.store.dispatch_result(source_id,current_only=False)
                    if (source.get('role')!='research' or source.get('status')!='completed'
                            or source.get('task_id')!=identifier
                            or source.get('epoch')!=generation['source_epoch']
                            or source.get('revision')!=state['revision']
                            or source.get('payload',{}).get('contract')!=state['contract']
                            or not source_receipt or ref not in source_receipt.get('candidate_refs',[])):
                        continue
                    records[(record['copilot_id'],record['path'],record['sha256'])]=record
                    references.append(ref)
                    if source_id not in source_dispatch_ids:source_dispatch_ids.append(source_id)
                except (ValueError,OSError,KeyError,TypeError):
                    continue
            receipt={**receipt,'candidate_refs':references}
            generation={**generation,'source_dispatch_ids':source_dispatch_ids}
            items=deliverables(root,receipt)
            need=roster_required(state['contract'])
            if need and items:
                from .maa_roster_tools import RosterTools
                roster=RosterTools(self.store,self.engine.data_dir)
                for item in items:
                    current=roster.assess(state['contract'].get('profile_id','local-default'),
                        records[(item['copilot_id'],item['path'],item['sha256'])])
                    item['classification']=current['classification']
            actual={i['code'] for i in items if not need or i['classification']=='satisfied'}
            gaps=research_goal_diagnostics(state['contract'],receipt)
            return {'task_id':identifier,'status':state['status'],'items':items,'generation':generation,
                'structural_gaps':gaps,
                'summary':render_delivery(items,completed=state['status']=='completed',require_roster=need,
                    missing=[c for c in research_stage_codes(state['contract'],receipt) if c not in actual],
                    pending=[i['message'] for i in gaps.get('issues',[])])}
        except (ValueError,OSError,KeyError,TaskNotFound):
            return {'items':[],'summary':'已登记的作业产物无法通过完整性或当前干员核对；未将旧回复当作真实结果。'}

    def _allowed(self):
        screen, inputs = self.permissions()
        if not screen:
            raise ProviderError('请在桌宠设置中开启画面读取。')
        if not inputs:
            raise ProviderError('请在桌宠设置中开启本次 MAA 界面操作授权。')

    def _open(self, request_cancel=None):
        if self._closed:
            raise ProviderError('应用正在关闭。')
        if self._persistence_fault:
            raise ProviderError('任务存储异常尚未恢复，已禁止新的执行；请修复后重新启动。')
        if request_cancel is not None and request_cancel.is_set():
            raise ProviderCancelled('请求已停止。')

    def _project(self, state, phase, result=None):
        if not self._closed:
            self.runtime.save(state['task_id'], state['task_epoch'], phase=phase,
                              context={'maa_workflow': {'phase': phase,
                                       'summary': str((result or {}).get('summary', ''))[:1600]}})

    def submit(self, goal, contract=None, *, modify=False, request_cancel=None):
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError('目标不能为空')
        value = authorize_battle(contract or {'goal': goal})
        with self._lock:
            self._open(request_cancel)
            receipt=self.store.request_receipt(value.get('request_id')) if value.get('request_id') else None
            if receipt:
                state=self.store.current(receipt['task_id'])
                self.store.management.require_active(receipt['task_id'])
                if state['contract'] != value:
                    raise ActionRejected('同一消息编号不能执行不同要求。')
                return {'accepted':True,'task_id':state['task_id'],'request_replayed':True,
                        'phase':state['status']}
            old = self._job
            typed = value.get('relation') is not None
            modify = modify or value.get('relation') == 'modify'
            identifier = value.get('selected_task_id') if typed and modify else self.current_task_id
            if identifier:
                task = self.engine.get_task(identifier)
                if task['status'] in TERMINAL_STATUSES:
                    identifier = None
            if not modify:
                if identifier and self._job and self._job.task_id == identifier and not typed:
                    self._control(identifier, 'stop')
                identifier = None
            if typed and modify:
                if not identifier:
                    raise ProviderError('修改必须选择一个现有任务编号。')
                previous = self.store.current(identifier)
                state = self.store.replace_contract(identifier, goal, value,
                    expected_revision=previous['revision'], expected_epoch=previous['epoch'])
            else:
                state = self.store.submit(goal, value, task_id=identifier)
            self.current_task_id = state['task_id']
            if state.get('request_replayed'):
                return {'accepted':True,'task_id':state['task_id'],'request_replayed':True,'phase':state['status']}
            if typed and modify and not state['writes_enabled']:
                self._project(state, 'paused')
                self.changed.emit()
                return {'accepted': True, 'task_id': state['task_id'],
                        'task': self.engine.get_task(state['task_id']), 'phase': 'paused',
                        'game_input_sent': False, 'battle_start_submitted': False}
            job = _Run(state['task_id'], state['epoch'])
            if old:
                if old.task_id == job.task_id or not typed:
                    old.cancel.set()
                # Keep the replacement inert while its predecessor releases the
                # same GUI slot. A current pause also grants stop-only control.
                self._enqueue(job)
                if old.task_id == job.task_id:
                    state=self.store.control(job.task_id,'pause')
                    job.epoch,job.needs_resume=state['epoch'],True
                    self._stop_async(old, state)
            else:
                self._launch(job)
            self._project(state, 'queued')
        self.changed.emit()
        return {'accepted': True, 'task_id': job.task_id,
                'task': self.engine.get_task(job.task_id), 'phase': 'queued',
                'game_input_sent': False, 'battle_start_submitted': False}

    def _enqueue(self, job):
        """Retain every independent queued task; edits supersede their own entry."""
        if self._pending and self._pending.task_id == job.task_id:
            self._pending.cancel.set()
            self._pending = job
            return
        self._queue = deque(item for item in self._queue if item.task_id != job.task_id)
        if self._pending is None:
            self._pending = job
        else:
            self._queue.append(job)

    def continue_contract(self, task_id, contract, *, request_cancel=None, expected_state=None):
        """Apply a real clarification before launching, never silently resume old text."""
        with self._lock:
            self._open(request_cancel)
            previous = self.store.current(task_id)
            if expected_state is not None and tuple(expected_state)!=(previous['revision'],previous['epoch']):
                raise ActionRejected('理解期间任务已改变，请依据当前问题重新回答。')
            question = self.store.management.question(task_id)
            value = authorize_battle(contract)
            if question and question['kind'] == 'business':
                state = self.store.management.answer_question(task_id, question['clarification_id'], value,
                    request_id=value.get('request_id', ''), expected_revision=previous['revision'],
                    expected_epoch=previous['epoch'])
            else:
                state = self.store.replace_contract(task_id, value['goal'], value,
                    expected_revision=previous['revision'], expected_epoch=previous['epoch'])
                # Business edits alone must never recover user pauses or unknown effects.
                if not state['writes_enabled']:
                    return {'accepted': True, 'task_id': task_id, 'phase': 'paused',
                            'reason': '补充已保存；当前仍需明确继续或处理阻塞条件。'}
            self.current_task_id = task_id
            if not state['writes_enabled']:
                return {'accepted': True, 'task_id': task_id, 'phase': 'blocked',
                        'reason': state.get('blocked_reason') or '回答已采纳，等待当前操作核对。'}
            job = _Run(task_id, state['epoch'])
            if self._job:
                if self._job.task_id == task_id:
                    self._job.cancel.set()
                self._enqueue(job)
            else:
                self._launch(job)
            self._project(state, 'queued')
            self.changed.emit()
            return {'accepted': True, 'task_id': task_id, 'phase': 'queued',
                    'continuation_event_id': state.get('continuation_event_id')}

    def resume(self, task_id=None, *, request_cancel=None, request_id=None):
        with self._lock:
            self._open(request_cancel)
            identifier = task_id or self.current_task_id
            if not identifier:
                raise ProviderError('当前没有可继续的 MAA 任务。')
            try:
                state = self.store.current(identifier)
            except TaskNotFound:
                task = self.engine.get_task(identifier)
                if task['status'] in TERMINAL_STATUSES:
                    raise ProviderError('该任务已经结束，不能继续。')
                # A trusted Continue can adopt a pre-migration/no-Key draft.
                # Its durable goal preserves the full user boundary; RuntimeStore
                # is never consulted to manufacture a contract or permissions.
                state = self.store.submit(task['goal'], {'goal': task['goal'],
                    'allow_start': False, 'attempt_limit': 0,
                    'workflow_scope': 'gui_assist',
                    'execution_restrictions': {'battle_start':False,'direct_game_input':False}}, task_id=identifier)
            if state['status'] in TERMINAL_STATUSES:
                raise ProviderError('该任务已经结束，不能继续。')
            # Continue reuses existing authority. It must never upgrade an old
            # ungranted gameplay request solely because capabilities changed.
            from .battle_contract import authorized as battle_authorized
            existing = state['contract']
            derived = authorize_battle(existing)
            battle_scope = (existing.get('allow_start') is True
                            or existing.get('workflow_scope') == 'maa_battle_start')
            if ((derived.get('allow_start') is True and not battle_authorized(existing))
                    or battle_scope and not battle_authorized(existing)):
                raise ProviderError('任务权限与当前支持范围不一致，请发送新的明确要求；未自动增加开战权限。')
            if self._job and self._job.task_id == identifier and not self._job.cancel.is_set():
                return {'accepted': True, 'task_id': identifier, 'already_running': True}
            job = _Run(identifier, state['epoch'])
            self.current_task_id = identifier
            if self._job:
                job.needs_resume = True
                self._enqueue(job)
            else:
                state = self.store.control(identifier, 'resume')
                job.epoch = state['epoch']
                self._launch(job)
            self._project(state, 'queued')
        return {'accepted': True, 'task_id': identifier,
                'task': self.engine.get_task(identifier),
                'phase': 'resuming_with_fresh_observation'}

    def _control(self, identifier, kind):
        if not identifier:
            return None
        try:
            state = self.store.current(identifier)
        except TaskNotFound:
            # A pre-migration draft has no MAA contract and no write capability.
            return (self.engine.stop_task(identifier) if kind == 'stop'
                    else self.engine.pause_task(identifier))
        if self.store.management.lifecycle(identifier)['state'] != 'active':
            return self.engine.get_task(identifier)
        if state['status'] in TERMINAL_STATUSES:
            return self.engine.get_task(identifier)
        # This transaction commits BEFORE cancellation or blocking stop work.
        state = self.store.control(identifier, kind)
        self._project(state, state['status'])
        read_only = state['contract'].get('current_deliverable') in {'stage_catalog', 'copilot_codes'}
        with self.engine._lock:
            has_gui_effect = self.engine._db.execute(
                "SELECT 1 FROM workflow_operations o JOIN workflow_slots s "
                "ON s.barrier_operation_id=o.operation_id WHERE o.task_id=? "
                "AND o.status IN ('submitted','unknown') LIMIT 1", (identifier,)).fetchone() is not None
        owns_active_run = self._job is not None and self._job.task_id == identifier
        no_external_control = not has_gui_effect and (read_only or not owns_active_run)
        event_kind = 'task_stopped' if kind == 'stop' else 'task_paused'
        projection = make_activity_event(event_kind, role='leader' if no_external_control else 'maa', task_id=identifier,
            request_id=state['contract'].get('request_id', ''),
            epoch=state['epoch'], status=state['status'],
            summary='只读资料任务已暂停或停止，检查点和已取得资料保留。' if no_external_control else
                    '本地输入已撤销；正在核实 MAA 停止状态。')
        self.event.emit(event_kind, projection['summary'],
                        {**projection, 'goal_version': state['epoch']})
        job = self._job if self._job and self._job.task_id == identifier else None
        if job:
            job.cancel.set()
        if self._pending and self._pending.task_id == identifier:
            self._pending.cancel.set()
            self._pending = None
        self._queue = deque(item for item in self._queue if item.task_id != identifier)
        if not no_external_control:
            self._stop_async(job, state)
        return {**self.engine.get_task(identifier), 'external_stop_confirmed': False,
                'stop_status': 'not_required' if no_external_control else 'pending_confirmation'}

    def pause(self, task_id=None):
        with self._lock:
            result = self._control(task_id or self.current_task_id, 'pause')
        self.changed.emit()
        return result

    def stop(self, task_id=None):
        with self._lock:
            result = self._control(task_id or self.current_task_id, 'stop')
        self.changed.emit()
        return result

    def trash(self, task_id):
        """Revoke durably before asking only the matching worker to leave."""
        with self._lock:
            self._open()
            state = self.store.management.trash(task_id)
            job = self._job if self._job and self._job.task_id == task_id else None
            if job:
                job.cancel.set()
            if self._pending and self._pending.task_id == task_id:
                self._pending.cancel.set()
                self._pending = None
            self._queue = deque(i for i in self._queue if i.task_id != task_id)
            # A historical import cannot stop unrelated B. An active bounded
            # battle owns its start lease and must retain its stop-only path.
            if job and state.get('state') == 'trashed':
                current=self.store.current(task_id)
                from .battle_contract import authorized as battle_authorized
                if battle_authorized(current['contract']):
                    self._stop_async(job,current)
            self.changed.emit()
            return state

    def _roles_for(self, job):
        with self._resources:
            if job.roles is None:
                factory = self.role_factory
                if factory is None:
                    from .maa_agents import MaaRoleAgents
                    factory = MaaRoleAgents
                resolve = lambda config: config() if callable(config) else config
                job.roles = factory(self.store, self.directory,
                    role_config=resolve(self.role_config), task_config=resolve(self.task_config),
                    knowledge=self.knowledge,
                    screen_permission=lambda: bool(self.permissions()[0]),
                    input_permission=lambda: bool(self.permissions()[1]),
                    activity_callback=lambda kind, details: self._event(job, kind, details),
                    metrics_callback=lambda role,metrics,task_id:self.runtime.record_call(role,metrics,task_id))
                # Only an explicit local acceptance invocation supplies this
                # trusted recovery proof. No model output grants recovery.
                for name in ('focus_recovery_profile','focus_recovery_state_sha256','battle_expected_job'):
                    value=getattr(self,name,None)
                    if value is not None:setattr(job.roles,name,value)
            return job.roles

    def _stop_async(self, job, state):
        control_job = job or _Run(state['task_id'], state['epoch'])
        done = threading.Event()
        control_job.controls.append(done)

        def work():
            result = {'status': 'unknown', 'external_stop_confirmed': False,
                      'summary': '本地写入已撤销；MAA 停止状态尚未核实。'}
            try:
                roles = self._roles_for(control_job)
                result = roles.stop(state['task_id'], control_seq=state['control_seq'])
            except Exception:
                pass  # Never translate an unavailable stop path into success.
            finally:
                with self._lock:
                    if not self._closed:
                        self.event.emit('maa_control', str(result.get('summary') or
                            '本地写入已撤销；MAA 停止状态尚未核实。'),
                            {**result, 'task_id': state['task_id'],
                             'goal_version': state['epoch']})
                try:
                    if job is None and control_job.roles is not None:
                        control_job.roles.close()
                except Exception:
                    pass
                finally:
                    done.set()

        thread = threading.Thread(target=work, daemon=True, name='shu-maa-stop')
        self._control_threads.append(thread)
        thread.start()

    def _launch(self, job):
        self.store.management.start_run(job.task_id, expected_epoch=job.epoch)
        self._job = job
        self.activity.emit(True)
        self._event(job, 'task_started', {'role': 'leader', 'status': 'running',
                    'summary': '任务已交给 Leader，开始核实并分派工作。'})
        threading.Thread(target=self._run, args=(job,), daemon=True,
                         name='shu-maa-workflow').start()

    def _event(self, job, kind, details):
        with self._lock:
            if self._closed or job.cancel.is_set() or self._job is not job:
                return
            current = self.store.current(job.task_id)
            if current['epoch'] != job.epoch:
                return
            projection = make_activity_event(kind, details,
                            task_id=job.task_id, epoch=job.epoch,
                            request_id=current['contract'].get('request_id', ''))
            if projection is not None:
                self.event.emit(kind, projection['summary'],
                                {**projection, 'goal_version': job.epoch})

    def _paused_battle_failure(self, job):
        """Read the failure from this run's own pause without changing authority."""
        from .battle_contract import authorized

        with self.engine._lock:
            state = self.store.current(job.task_id)
            if (state['status'] != 'paused' or state['control_kind'] != 'pause'
                    or state['epoch'] != job.epoch + 1 or state['task_epoch'] != state['epoch']
                    or state['writes_enabled'] or not authorized(state['contract'])):
                return None
            self.store.management.require_active(job.task_id)
            starts = self.engine._db.execute(
                'SELECT operation_id FROM workflow_operations WHERE task_id=? AND epoch=? '
                'AND revision=? AND starts=1 AND stop_only=0 AND submitted_at IS NOT NULL LIMIT 2',
                (job.task_id, job.epoch, state['revision'])).fetchall()
            if len(starts) != 1:
                return None
            start = self.store.get_operation(starts[0]['operation_id'])
            if (start['slot'] != 'desktop-maa' or start['action'].get('kind') != 'start'
                    or start['control_seq'] + 1 != state['control_seq'] or not start['dispatch_id']):
                return None
            dispatch = self.store.get_dispatch(start['dispatch_id'])
            if (dispatch['task_id'] != job.task_id or dispatch['epoch'] != job.epoch
                    or dispatch['revision'] != state['revision'] or dispatch['role'] != 'maa'
                    or dispatch['status'] != 'completed' or dispatch['payload'].get('contract') != state['contract']):
                return None
            # The graph persisted this receipt before rejecting the old epoch.
            # Only a matching automatic-pause marker permits failure display.
            receipt = self.store.dispatch_result(start['dispatch_id'], current_only=False)
            marker = {**{key:state[key] for key in ('task_id','revision','epoch','control_seq')},
                      'from_epoch':job.epoch, 'from_revision':state['revision'],
                      'dispatch_id':start['dispatch_id'], 'start_operation_id':start['operation_id']}
            if (not receipt or receipt.get('status') != 'needs_input'
                    or receipt.get('automatic_pause_state') != marker
                    or receipt.get('start_operation_id') != start['operation_id']
                    or receipt.get('battle_start_submitted') is not True):
                return None

        settled = start.get('result') or {}
        idle = (settled.get('battle_idle_verified') is True
                and settled.get('external_idle_confirmed') is True
                and receipt.get('external_idle_confirmed') is True)
        reason = _error_summary(receipt.get('summary') or '运行状态核对失败。')
        stop_status = '已核实 MAA 恢复空闲。' if idle else 'MAA 停止状态尚未核实。'
        return state, {'status':'needs_input', 'goal_complete':False,
            'code':receipt.get('code') or 'battle_execution_unverified',
            'summary':f'MAA 已提交一次启动。{reason}任务已暂停，{stop_status}',
            'battle_start_submitted':True, 'external_idle_confirmed':idle,
            'start_operation_id':start['operation_id']}

    def _run(self, job):
        workflow = None
        result = {'status': 'cancelled', 'summary': '旧执行已撤销，保留检查点。'}
        try:
            if job.cancel.is_set():
                return
            roles = self._roles_for(job)
            factory = self.workflow_factory
            options = {}
            if factory is None:
                from .maa_graph import MaaWorkflow
                factory = MaaWorkflow
                # B2 enables one prepared code. Batch recipes have their own
                # acceptance gate and remain at the preparation boundary.
                options.update(allow_import_gui=self.single_import_enabled or self.queue_import_enabled,
                               single_import_only=not self.queue_import_enabled)
            workflow = factory(self.store, self.directory, roles.handlers,
                               emit=lambda kind, details: self._event(job, kind, details),
                               cancel_event=job.cancel, **options)
            with self.graph_lock:
                result = workflow.run(job.task_id)
                for event in self.store.management.pending_launches():
                    if (event['task_id'],event['epoch']) == (job.task_id,job.epoch):
                        self.store.management.ack_launch(event['event_id'])
        except Exception as exc:
            result = {'status': 'needs_input', 'summary': 'MAA 工作流需要处理：' + _error_summary(exc)}
        finally:
            resources_released = True
            for done in job.controls:
                if not done.wait(self.control_timeout_seconds):
                    resources_released = False
                    result = {'status': 'needs_input', 'summary': '停止回读尚未结束，已阻止后续任务接管。'}
            for resource in (workflow, job.roles):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        resources_released = False
                        result = {'status': 'needs_input',
                                  'summary': '工作流资源未能确认释放，普通写入已关闭，需要核实。'}
            try:
                if self.store.management.lifecycle(job.task_id)['state'] in {'trashed','purge_pending'}:
                    self.store.audit_abandoned_dispatches(job.task_id)
            except Exception as exc:
                resources_released=False
                self._persistence_fault=_error_summary(exc)
            accepted = False
            state = None
            # Hashing readback artifacts and committing a result are independent
            # of the short Qt scheduling lock. Stop can revoke while hashing;
            # the store's expected-epoch check then rejects the late completion.
            if not self._closed and not job.cancel.is_set():
                try:
                    state = self.store.current(job.task_id)
                    if state['epoch'] == job.epoch:
                        if result.get('status') == 'battle_finished':
                            roles = result.get('role_results', {})
                            receipt = roles.get('battle') or roles.get('maa', {})
                            try:
                                self.store.complete_battle_verified(job.task_id,
                                    receipt.get('evidence_refs', []), expected_epoch=job.epoch)
                            except (ActionRejected, ValueError, OSError):
                                result = {'status': 'needs_input', 'summary': 'MAA 运行结束尚缺少当前持久空闲回读凭据，未标记完成。'}
                        elif result.get('status') == 'import_verified':
                            receipt = result.get('role_results', {}).get('maa', {})
                            decision = completion_decision(state['contract'], receipt)
                            if not decision.get('goal_complete'):
                                result = {**result, **decision, 'status':'needs_input'}
                            else:
                                try:
                                    self.store.complete_import_verified(job.task_id,
                                        receipt.get('evidence_refs', []), expected_epoch=job.epoch)
                                except (ActionRejected, ValueError):
                                    result = {'status': 'needs_input', 'summary': '导入缺少当前持久 MAA 回读证据，未标记完成。'}
                        elif result.get('status') == 'planned_verified':
                            try:
                                self.store.complete_planned_verified(job.task_id, expected_epoch=job.epoch)
                            except (ActionRejected, ValueError, OSError):
                                result = {'status': 'needs_input', 'summary': '计划结果未通过当前目录、代码或阵容证据核验，未标记完成。'}
                        elif result.get('status') == 'research_verified':
                            receipt=result.get('role_results',{}).get('research',{})
                            try:
                                self.store.complete_research_verified(job.task_id,
                                    receipt.get('evidence_refs',[]),expected_epoch=job.epoch)
                            except (ActionRejected,ValueError,OSError):
                                result={'status':'needs_input','summary':'资料结果缺少当前持久工具证据，未标记完成。'}
                        if result.get('status') not in {'import_verified','research_verified','planned_verified','battle_finished'}:
                            preparation = {}
                            if result.get('code') == 'import_preparation_verified':
                                preparation['preparation_dispatch_id'] = result['prepared_dispatch_id']
                            self.store.suspend(job.task_id, expected_epoch=job.epoch,
                                               message=result.get('summary', '需要处理'),
                                               kind=result.get('waiting_kind','blocked'), **preparation)
                        from .leader_policy import copilot_research_required
                        if (copilot_research_required(state['contract'])
                                and result.get('waiting_kind')!='business'
                                and state['contract'].get('current_deliverable') not in {'maa_import', 'gameplay'}):
                            result['summary']=self.research_output(job.task_id)['summary']
                        if (state['contract'].get('current_deliverable') == 'maa_import'
                                and state['contract'].get('import_scope') is not None
                                and result.get('waiting_kind') != 'business'):
                            source_id = state['contract'].get('source_task_id') or job.task_id
                            delivery = self.research_output(source_id)
                            if delivery.get('items'):
                                result['query_delivery'] = delivery
                                result['summary'] = (result.get('summary', '')
                                    + '\n\n作业查询结果\n' + delivery['summary'])
                        state = self.store.current(job.task_id)
                        self._project(state, result.get('status', 'needs_input'), result)
                        accepted = True
                except ActionRejected:
                    pass  # A trusted control won the transaction race.
                except Exception as exc:
                    # Completion is outside workflow.run's exception boundary.
                    # A database/projection failure here must not strand _job,
                    # suppress the final failure, or leave current writes live.
                    result = {'status': 'needs_input', 'code': 'completion_persistence_error',
                              'summary': '任务结果核验或保存失败，未确认本轮完成（'
                                         + type(exc).__name__ + '）：' + _error_summary(exc)}
                    if not self._closed and not job.cancel.is_set():
                        try:
                            current = self.store.current(job.task_id)
                            if current['epoch'] == job.epoch:
                                state = (self.store.suspend(job.task_id, expected_epoch=job.epoch,
                                            message=result['summary'])
                                         if current['writes_enabled'] else current)
                                accepted = True
                            elif (state is not None and state['epoch'] == current['epoch']
                                  and current['status'] == 'needs_input'
                                  and current['control_kind'] in {'needs_input','blocked'}):
                                # The suspend already committed, but its UI
                                # projection failed. It remains an own result.
                                state, accepted = current, True
                        except ActionRejected:
                            pass  # Stop or a newer contract won recovery too.
                        except Exception as recovery_error:
                            # With unavailable storage no durable revocation can
                            # be asserted. Roles are closed above; fence future
                            # launches locally until application restart.
                            self._persistence_fault = _error_summary(recovery_error)
                            resources_released = False
                            result['summary'] += '；撤权持久化也未能确认，已禁止本地后续调度和新执行。'
                            state = state or {'epoch': job.epoch}
                            accepted = True
            with self._lock:
                if not self._closed and self._job is job:
                    if not accepted and resources_released and not job.cancel.is_set():
                        try:
                            paused = self._paused_battle_failure(job)
                            if paused is not None:
                                state, result = paused
                                accepted = True
                                self._project(state, 'paused', result)
                        except (ActionRejected, TaskNotFound):
                            pass  # Trusted controls and deletion keep precedence.
                        except Exception as exc:
                            # Even a failed display projection must release the job.
                            self._persistence_fault = _error_summary(exc)
                    if accepted and not job.cancel.is_set():
                        stage = 'task_finished' if result.get('status') in {'import_verified','research_verified','planned_verified','battle_finished'} else 'task_waiting'
                        projection = make_activity_event(stage, role='leader', task_id=job.task_id,
                            request_id=state.get('contract', {}).get('request_id', ''),
                            epoch=state['epoch'], status=result.get('status'), summary=result.get('summary'))
                        self.event.emit(stage, projection['summary'],
                                        {**projection, 'goal_version': state['epoch']})
                        self.finished.emit({**result, 'task_id': job.task_id, 'epoch':state['epoch'],
                                            'request_id':state.get('contract',{}).get('request_id','')})
                    self._job = None
                    self.activity.emit(False)
                    while self._job is None and (self._pending or self._queue):
                        pending, self._pending = self._pending, None
                        if pending is None:
                            pending=self._queue.popleft()
                        if not resources_released or self._persistence_fault:
                            pending.cancel.set()
                            try:
                                self.store.control(pending.task_id,'pause')
                            except Exception as exc:
                                self._persistence_fault=_error_summary(exc)
                        if pending.cancel.is_set():
                            pending.done.set()
                            continue
                        try:
                            state = self.store.current(pending.task_id)
                            self.store.management.require_active(pending.task_id)
                            if state['epoch'] != pending.epoch:
                                raise ActionRejected('排队期间任务已改变，不自动恢复新版本。')
                            if pending.needs_resume:
                                state = self.store.control(pending.task_id, 'resume')
                            pending.epoch = state['epoch']
                            self._launch(pending)
                        except (ActionRejected, TaskNotFound):
                            pending.cancel.set()
                            pending.done.set()
                        except Exception as exc:
                            self._persistence_fault = _error_summary(exc)
                            pending.cancel.set()
                            if self._job is pending:
                                self._job = None
                            pending.done.set()
                            try:
                                self.store.control(pending.task_id, 'pause')
                            except Exception:
                                pass  # Local fault fence remains until restart.
                            self.activity.emit(False)
                            self.changed.emit()
                    self.changed.emit()
                job.done.set()

    def close(self):
        with self._lock:
            if self._closed:
                return
            job = self._job
            waiting_ids=[waiting.task_id for waiting in self._queue]
            if self._pending:waiting_ids.append(self._pending.task_id)
            if self._pending:
                self._pending.cancel.set()
                self._pending = None
            for waiting in self._queue:
                waiting.cancel.set()
            self._queue.clear()
            # The UI may already point at a newer draft while an older run is
            # unwinding. Revoke both identities before closing shared stores.
            identifiers = dict.fromkeys(identifier for identifier in (
                job.task_id if job else None, self.current_task_id,*waiting_ids) if identifier)
            for identifier in identifiers:
                self._control(identifier, 'pause')
            self._closed = True
        if job:
            job.done.wait(2)
        for thread in self._control_threads:
            thread.join(.1)

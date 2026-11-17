"""Durable outer workflow. Nodes reuse journal results before any role call.

Only bounded JSON facts enter graph checkpoints. Model histories, credentials,
COM objects and input authority stay outside the graph.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import uuid
from typing import TypedDict

_RUNTIME = Path(__file__).resolve().parents[2] / '.agent-runtime'
if _RUNTIME.is_dir() and str(_RUNTIME) not in sys.path:
    sys.path.insert(0, str(_RUNTIME))

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langsmith import tracing_context

from .core import ActionRejected
from .activity_events import make_activity_event, ROLE_NAMES
from .leader_policy import completion_decision, research_satisfies_goal, copilot_research_required,roster_required,research_stage_codes
from .research_delivery import deliverables,render_delivery

ROLES = ('leader', 'maa', 'research', 'catalog', 'diagnosis')
_BLOCKED = frozenset({'api_key', 'authorization', 'reasoning_content', 'hidden_reasoning',
                      'raw_response', 'provider_payload', 'image_url', 'image_base64',
                      'audio_base64', 'image_data', 'pixels'})


class _CancellationScope:
    """Observe an owner's cancellation without mutating it during cleanup."""
    def __init__(self, parent=None):
        self.parent=parent
        self.local=threading.Event()
    def is_set(self):
        return self.local.is_set() or self.parent is not None and self.parent.is_set()
    def set(self):
        self.local.set()
    def wait(self, timeout=None):
        deadline=None if timeout is None else time.monotonic()+max(0,timeout)
        while not self.is_set():
            remaining=.02 if deadline is None else min(.02,deadline-time.monotonic())
            if remaining<=0:return self.is_set()
            self.local.wait(remaining)
        return True


def _json_facts(value, depth=0):
    if depth > 24:
        raise ValueError('工作流数据嵌套过深')
    if isinstance(value, dict):
        if any(not isinstance(k, str) or k.lower() in _BLOCKED for k in value):
            raise ValueError('工作流只接受无凭据/隐藏推理的事实字段')
        return {k: _json_facts(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_facts(v, depth + 1) for v in value]
    if value is None or type(value) in (bool, int, float, str):
        if isinstance(value, str) and (value.startswith('data:image/') or value.startswith('data:audio/')):
            raise ValueError('媒体只保存引用')
        return value
    raise TypeError('图状态不接受任意对象')


class JsonOnlySerializer:
    def dumps_typed(self, obj):
        raw = json.dumps(_json_facts(obj), ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(raw) > 512 * 1024:
            raise ValueError('工作流状态超出预算，请保存产物引用')
        return 'json', raw

    def loads_typed(self, data):
        kind, raw = data
        if kind != 'json':
            raise ValueError('仅允许 JSON checkpoint')
        return _json_facts(json.loads(raw))


class WorkflowState(TypedDict, total=False):
    task_id: str
    epoch: int
    revision: int
    phase: str
    turn: int
    role_results: dict
    import_dispatch_id: str
    completed_dispatch_ids: list
    next_role: str
    delegation: str
    last_role: str
    status: str
    summary: str
    source_dispatch_id: str
    handoff_history: list
    goal_complete: bool
    pending_objectives: list
    code: str
    waiting_kind: str
    prepared_dispatch_id: str


class MaaWorkflow:
    """Handlers are isolated role callables (context, cancel_event) -> JSON result."""
    def __init__(self, store, directory, handlers, *, emit=None, cancel_event=None,
                 allow_import_gui=True, single_import_only=False):
        self.store, self.handlers = store, dict(handlers)
        if type(allow_import_gui) is not bool:
            raise TypeError('import GUI capability must be a boolean')
        self.allow_import_gui = allow_import_gui
        self.single_import_only = single_import_only
        if set(handlers) - set(ROLES):
            raise ValueError('工作流角色不在注册表中')
        self.emit = emit or (lambda kind, details: None)
        self.cancel = _CancellationScope(cancel_event)
        self.owner = f'{os.getpid()}:{uuid.uuid4().hex}'
        self.store.register_runner(self.owner)
        self._lock = threading.Lock()
        self._closed = False
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(directory / 'maa-graph.sqlite3', check_same_thread=False)
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.saver = SqliteSaver(self.connection, serde=JsonOnlySerializer())
        builder = StateGraph(WorkflowState)
        builder.add_node('leader', self._leader)
        for role in ('maa', 'research', 'catalog', 'diagnosis'):
            builder.add_node(role, self._worker(role))
            builder.add_edge(role, 'leader')
        builder.add_node('adopt_research', self._adopt_research)
        builder.add_edge('adopt_research', 'leader')
        builder.add_edge(START, 'leader')
        builder.add_conditional_edges('leader', lambda state: state['next_role'],
                                      {**{r: r for r in ROLES if r != 'leader'},
                                       'adopt_research': 'adopt_research', 'end': END})
        self.graph = builder.compile(checkpointer=self.saver)

    def _current(self, state):
        current = self.store.current(state['task_id'])
        if self.cancel.is_set() or self._closed:
            raise ActionRejected('本次工作流已取消，不能继续委派')
        if current['epoch'] != state['epoch'] or not current['writes_enabled']:
            raise ActionRejected('旧执行代际失效，需通过可信继续入口重新观察')
        return current

    def _call(self, role, state):
        from .battle_contract import authorized as battle_authorized, compound_authorized
        current = self._current(state)
        compound = compound_authorized(current['contract'])
        battle_step = compound and role == 'maa' and state.get('phase') == 'battle'
        battle = battle_step or (not compound and battle_authorized(current['contract']))
        if role == 'maa' and self.single_import_only and not battle:
            research = state.get('role_results', {}).get('research', {})
            if research.get('queue_plan_ref') or len(research.get('candidate_refs', [])) != 1:
                raise ActionRejected('当前阶段仅允许一个已准备作业，批量导入仍需后续验收')
        if role == 'maa' and not self.allow_import_gui:
            raise ActionRejected('当前组件止于导入准备核验，尚未开放 MAA 界面操作。')
        typed = current['contract'].get('current_deliverable')
        if role == 'maa' and typed is not None and typed != 'maa_import' and not battle:
            raise ActionRejected('当前合同没有授权 MAA 导入委派')
        payload = {'task_id': state['task_id'], 'epoch': state['epoch'],
                   'contract': current['contract'], 'revision': current['revision'],
                   'request_id': current['contract'].get('request_id', ''),
                   'task': state.get('delegation', ''),
                   'last_role': state.get('last_role', ''),
                   'role_results': copy.deepcopy(state.get('role_results', {})),
                   'handoff_history': copy.deepcopy(state.get('handoff_history', []))[-12:]}
        if compound and role == 'maa':
            payload['execution_step'] = 'battle' if battle_step else 'import'
            if battle_step:
                payload['import_dispatch_id'] = state['import_dispatch_id']
        key = f"epoch:{state['epoch']}:turn:{state.get('turn', 0)}:role:{role}"
        def prepare(key, review_of=None):
            # Dynamic facts may change after a result commits but before its
            # checkpoint. Resolve the durable input before rebuilding context.
            existing = self.store.dispatch_by_key(state['task_id'], key)
            if existing is not None:
                old = existing['payload']
                if (existing['role'] != role or existing['epoch'] != state['epoch']
                        or existing['revision'] != current['revision']
                        or old.get('contract') != current['contract'] or old.get('task') != payload['task']
                        or old.get('execution_step') != payload.get('execution_step')
                        or old.get('import_dispatch_id') != payload.get('import_dispatch_id')):
                    raise ActionRejected('持久委派与当前合同或计划步骤不一致')
                return existing
            context = dict(payload)
            if role == 'leader':
                context['historical_research'] = self.store.historical_research(state['task_id'])
                context['interrupted_dispatches'] = self.store.interrupted_dispatches(state['task_id'])
            if role == 'research':
                context.update(self.store.research_account(state['task_id']))
            if review_of is not None:
                context['review_of_dispatch_id'] = review_of
            return self.store.ensure_dispatch(state['task_id'], role, key, context)

        dispatch = prepare(key)
        saved = self.store.dispatch_result(dispatch['dispatch_id'])
        if role == 'leader' and saved is not None and saved.get('code') == 'owner_exited':
            # One bounded new read-only Leader review, with an explicit link to
            # the interrupted attempt. The old dispatch is never replayed.
            dispatch = prepare(key + ':owner-exit-review', review_of=dispatch['dispatch_id'])
            saved = self.store.dispatch_result(dispatch['dispatch_id'])
        def progress(kind, result=None):
            status = ('running' if kind == 'dispatch_started' else
                      (result or {}).get('status') or ('reused' if kind == 'dispatch_reused' else 'completed'))
            fallback = ROLE_NAMES[role] + ('开始处理委派' if kind == 'dispatch_started' else
                       '复用已保存的结果' if kind == 'dispatch_reused' else '已返回委派结果')
            summary = fallback if kind == 'dispatch_started' else (result or {}).get('summary') or fallback
            if role=='leader' and kind!='dispatch_started':
                summary='Leader已返回调度决定；完成与交付以程序核验的产物为准。'
            if not isinstance(summary, str):
                summary = fallback
            self.emit(kind, make_activity_event(kind, role=role, status=status,
                task_id=state['task_id'], epoch=state['epoch'],
                request_id=dispatch['payload'].get('request_id'),
                dispatch_id=dispatch['dispatch_id'], summary=summary,
                step_title=(dispatch['payload'].get('task') or dispatch['payload'].get('contract',{}).get('goal') or fallback)[:120]))
        if saved is not None:
            progress('dispatch_reused', saved)
            return saved, dispatch['dispatch_id']
        claim = self.store.claim_dispatch(dispatch['dispatch_id'], self.owner)
        if claim is None:
            # An abandoned/unknown role is not permission to spawn its replacement.
            raise ActionRejected('委派正在执行或需要对账，未重复调用智能体')
        progress('dispatch_started')
        payload = dispatch['payload']
        handler = self.handlers.get(role)
        if handler is None:
            result = {'status': 'failed', 'code': 'role_unavailable',
                      'summary': f'{role} 工具尚未接通，不能假称执行成功'}
        else:
            try:
                result = handler({**payload, 'dispatch_id': dispatch['dispatch_id']}, self.cancel)
                result = _json_facts(result)
                if not isinstance(result, dict):
                    raise ValueError('角色结果必须是对象')
            except Exception as exc:
                result = {'status': 'failed', 'code': getattr(exc, 'code', 'role_error'),
                          'summary': str(exc)[:600]}
        if payload.get('review_of_dispatch_id'):
            result = {**result, 'review_of_dispatch_id': payload['review_of_dispatch_id']}
        # Persist the result and its outbox before allowing any graph advancement.
        self.store.finish_dispatch(dispatch['dispatch_id'], result)
        progress('dispatch_completed', result)
        self._current(state)  # Late results persist but cannot authorize a newer epoch.
        return result, dispatch['dispatch_id']

    def _import_route(self, state, contract):
        from .leader_policy import import_satisfies_goal
        from .planned_agents import register_import_plan, ImportSelectionRequired
        from .battle_contract import compound_authorized, import_then_start_requested

        def blocked(code, summary):
            return {'status': 'needs_input', 'code': code, 'goal_complete': False,
                    'summary': summary, 'phase': 'blocked', 'next_role': 'end'}

        compound = compound_authorized(contract)
        if not compound:
            if import_then_start_requested(contract.get('user_request')):
                return blocked('single_start_scope_unverified', '当前仅支持核验单个作业后启动一次。请确认要执行的关卡、难度和次数。')
            if (contract.get('allow_start') is not False or contract.get('attempt_limit', 0) != 0
                    or not import_satisfies_goal(contract)):
                return blocked('import_scope_rejected', '未能核实这条任务要求执行到哪一步。')
        results = state.get('role_results', {})
        if compound and not results:
            try:
                self.store.require_start_attempt_available(state['task_id'])
            except ActionRejected as exc:
                return blocked('start_attempt_used', str(exc))
        if 'maa' in results:
            receipt = results['maa']
            research = results.get('research', {})
            if research.get('single_document_plan_ref'):
                from .import_mode import verify_single_document_result
                if verify_single_document_result(self.store,state,research,receipt):
                    return {'status':'import_verified','goal_complete':True,'phase':'finished','next_role':'end',
                        'pending_objectives':[],'summary':receipt.get('summary') or '兼容作业已完成单作业加载核验。'}
                return blocked(receipt.get('code') or 'single_document_unverified',
                    receipt.get('summary') or '兼容作业加载凭据尚未核实。')
            if research.get('queue_plan_ref'):
                from .maa_queue_completion import verify_queue_result
                decision = verify_queue_result(self.store,state,contract,research,receipt)
                if decision['goal_complete']:
                    return {**decision,'phase':'finished','next_role':'end','pending_objectives':[]}
                return blocked(receipt.get('code') or decision['code'],
                               receipt.get('summary') if receipt.get('status') != 'import_verified'
                               and receipt.get('summary') else decision['summary'])
            if (receipt.get('status') == 'import_verified' and receipt.get('evidence_refs')
                    and receipt.get('battle_start_submitted') is False):
                if compound:
                    from .battle_import_binding import ImportBindingError, load_current_import_binding
                    try:
                        if 'battle' not in results:
                            load_current_import_binding(self.store, state['task_id'], state.get('import_dispatch_id'))
                    except ImportBindingError as exc:
                        return blocked(exc.code, str(exc))
                    return self._battle_route(state, contract, after_import=True)
                return {'status': 'import_verified', 'goal_complete': True,
                        'summary': receipt.get('summary') or 'MAA 已加载目标作业，回读核验通过。',
                        'phase': 'finished', 'next_role': 'end', 'pending_objectives': []}
            if receipt.get('transport') == 'screenshot_keyboard':
                return blocked(receipt.get('code') or 'import_unverified',
                               receipt.get('summary') or 'MAA 截图回读未核实。')
            # Historical UIA receipts can still be diagnosed once.
            # Diagnosis cannot route an uncertain import back to MAA.
            if 'diagnosis' not in results:
                return {'next_role': 'diagnosis', 'phase': 'diagnosis',
                        'delegation': '只读核对本次 MAA 导入失败回执和已登记证据，说明具体阻塞原因。'
                                      '保留待对账的操作，不重放输入，不开始作战。'}
            summary = receipt.get('summary') or 'MAA 加载结果仍需核验。'
            diagnosis = results['diagnosis'].get('summary')
            if isinstance(diagnosis, str) and diagnosis.strip():
                summary += '\n诊断结果：' + diagnosis.strip()[:1200]
            return blocked(receipt.get('code') or 'import_unverified', summary)
        if 'research' in results:
            receipt = results['research']
            if receipt.get('code') == 'import_selection_required' and receipt.get('waiting_kind') == 'business':
                return {**blocked('import_selection_required',receipt['summary']), 'waiting_kind':'business'}
            if compound and receipt.get('status') == 'researched' and (
                    receipt.get('queue_plan_ref') or receipt.get('single_document_plan_ref')
                    or len(receipt.get('candidate_refs', [])) != 1):
                return {**blocked('single_start_target_required', '单次启动需要一个明确的关卡及难度。请确认本次要执行哪一项。'),
                        'waiting_kind': 'business'}
            stage_gate = (not self.allow_import_gui
                          or self.single_import_only and (receipt.get('queue_plan_ref')
                              or len(receipt.get('candidate_refs',[])) != 1))
            if stage_gate and receipt.get('status') == 'researched' and receipt.get('candidate_refs'):
                from .import_preparation import verify_import_preparation_result
                try:
                    prepared = verify_import_preparation_result(self.store,state['task_id'])
                except (ActionRejected,OSError,ValueError,TypeError,KeyError) as error:
                    return blocked('import_preparation_unverified',
                        '导入准备尚未通过当前来源、完整作业与 Box 复核：'+str(error)[:500])
                if prepared.get('verified') is not True or not prepared.get('items'):
                    return blocked(prepared.get('code') or 'import_preparation_unverified',
                        prepared.get('summary') or '缺少当前已核验的导入准备；尚未导入 MAA。')
                lines = []
                for item in prepared['items']:
                    stage = item.get('stage_code') or item.get('code') or '未标明关卡'
                    code = item.get('mystery_code') or item.get('code') or ''
                    mode = item.get('difficulty',item.get('mode'))
                    label = {1:'普通',2:'突袭'}.get(mode,mode if isinstance(mode,str) else '')
                    lines.append(str(stage)+'：'+str(code)+('（'+label+'，准备已核实）' if label else '（准备已核实）'))
                return {**blocked('import_preparation_verified',
                    '已核验以下导入准备：\n'+'\n'.join(lines)+
                    '\n当前组件尚未导入 MAA；完整导入任务仍待后续执行。'),
                    'waiting_kind':'blocked','prepared_dispatch_id':prepared.get('research_dispatch_id','')}
            if receipt.get('queue_plan_ref'):
                if (receipt.get('status') == 'researched' and type(receipt.get('target_count')) is int
                        and receipt['target_count'] >= 2
                        and len(receipt.get('candidate_refs', [])) == receipt['target_count']):
                    return {'next_role':'maa','phase':'maa',
                            'delegation':'按已核验计划逐项导入 MAA 多作业队列，保护已有行并核对全部目标。'}
                return blocked('queue_plan_incomplete','多作业计划缺少完整目标凭据，暂不能输入。')
            if receipt.get('status') == 'researched' and receipt.get('candidate_refs'):
                return {'next_role': 'maa', 'phase': 'maa',
                        'delegation': '把本任务核验的神秘代码输入 MAA，并截图核对新预览。'}
            return blocked(receipt.get('code') or 'import_candidate_unavailable',
                           receipt.get('summary') or '缺少经过目录和 Box 核验的完整作业。')
        if not contract.get('source_task_id'):
            if 'catalog' not in results:
                return {'next_role': 'catalog', 'phase': 'catalog',
                        'delegation': '核对本任务目标关卡所属活动的真实完整目录、关卡标识与难度。'}
            try:
                register_import_plan(self.store, state, contract)
            except ImportSelectionRequired as exc:
                return {**blocked('import_selection_required', str(exc)), 'waiting_kind':'business'}
            except (ActionRejected, OSError, ValueError, TypeError, KeyError) as exc:
                return blocked('import_stage_unverified', str(exc)[:600])
        return {'next_role': 'research', 'phase': 'research',
                'delegation': '依据核验的关卡、难度与本地 Box，准备本次导入的完整作业。'}

    def _battle_route(self, state, contract, *, after_import=False):
        from .battle_contract import authorized
        if not authorized(contract):
            return {'status': 'needs_input', 'goal_complete': False,
                    'code': 'battle_authorization_required' if contract.get('workflow_scope')=='maa_battle_start' else 'execution_not_available',
                    'summary': '当前仅支持明确要求启动已导入单作业一次；多次作战、整活动和基建尚未开放。原目标已保留。',
                    'next_role': 'end', 'phase': 'blocked'}
        receipt = state.get('role_results', {}).get('battle' if after_import else 'maa')
        if receipt is None:
            return {'next_role': 'maa', 'phase': 'battle' if after_import else 'maa',
                    'delegation': '核对当前已导入的单作业及对应目标；截图导航到准确关卡详情且存在开始行动；'
                        '仅启动 MAA 一次并持续监控至明确停止或空闲，禁止购买、使用理智药/源石和直接部署。',
                    'summary': ('本任务作业已导入并核实。正在确认关卡页面，随后启动 MAA 一次并监控。' if after_import
                                else '已取得单次 MAA 启动授权，正在核对已导入作业、游戏目标页面并监控运行。')}
        if (receipt.get('status') == 'battle_finished' and receipt.get('battle_start_submitted') is True
                and receipt.get('external_idle_confirmed') is True and receipt.get('evidence_refs')
                and isinstance(receipt.get('start_operation_id'), str)):
            return {'status': 'battle_finished', 'goal_complete': True, 'next_role': 'end',
                    'phase': 'finished', 'pending_objectives': [],
                    'summary': receipt.get('summary') or '已确认 MAA 运行后恢复空闲；这不代表已核实关卡胜利。'}
        return {'status': 'needs_input', 'goal_complete': False, 'next_role': 'end', 'phase': 'blocked',
                'code': receipt.get('code') or 'battle_execution_unverified',
                'summary': receipt.get('summary') or 'MAA 启动或结束状态尚未核实，已停止重复启动。'}

    def _leader(self, state):
        contract = self._current(state)['contract']
        unresolved=(contract.get('task_spec') or {}).get('unresolved') or []
        if unresolved:
            return {'status':'needs_input','goal_complete':False,'code':'user_clarification_required',
                    'summary':'请补充：'+'；'.join(str(item) for item in unresolved),
                    'waiting_kind':'business','next_role':'end','phase':'needs_input'}
        typed = contract.get('current_deliverable')
        if typed == 'maa_import':
            return self._import_route(state, contract)
        if (contract.get('task_spec') or {}).get('kind') == 'base':
            return {'status': 'needs_input', 'goal_complete': False, 'code': 'execution_not_available',
                    'summary': '已保留执行目标。当前支持作业查询与导入，日常和作战执行待后续验收。',
                    'next_role': 'end', 'phase': 'blocked'}
        if typed == 'gameplay':
            return self._battle_route(state, contract)
        if state.get('code') == 'no_progress':
            return {'status':'needs_input','code':'no_progress','goal_complete':False,
                    'summary':state.get('summary') or '相同委派未取得新证据，已停止重复处理。',
                    'next_role':'end','phase':'blocked'}
        if state.get('turn', 0) >= 16:
            return {'status': 'needs_input', 'summary': '达到本任务委派预算，已保存进度',
                    'next_role': 'end'}
        result, dispatch_id = self._call('leader', state)
        ids = [*state.get('completed_dispatch_ids', []), dispatch_id]
        if result.get('review_of_dispatch_id'):
            ids.append(result['review_of_dispatch_id'])
        next_role = result.get('next_role', 'ask')
        typed = self._current(state)['contract'].get('current_deliverable')
        if ((typed in ('stage_catalog', 'copilot_codes') and next_role == 'adopt_research')
                or (typed in ('stage_catalog', 'copilot_codes', 'copilot_json') and next_role == 'maa')):
            return {'status':'needs_input', 'goal_complete':False, 'code':'typed_role_rejected',
                    'summary':'当前只读交付不能委派该角色。', 'next_role':'end', 'phase':'blocked',
                    'completed_dispatch_ids':ids}
        if typed == 'stage_catalog' and next_role == 'research':
            return {'status':'needs_input', 'goal_complete':False, 'code':'typed_role_rejected',
                    'summary':'目录交付不需要作业研究。', 'next_role':'end', 'phase':'blocked',
                    'completed_dispatch_ids':ids}
        if next_role in ('maa', 'research', 'catalog', 'diagnosis'):
            return {'next_role': next_role, 'delegation': str(result.get('task', ''))[:2000],
                    'phase': next_role, 'completed_dispatch_ids': ids,
                    'summary': str(result.get('summary', ''))[:1200]}
        if next_role == 'adopt_research' and isinstance(result.get('source_dispatch_id'), str):
            return {'next_role': next_role, 'source_dispatch_id': result['source_dispatch_id'],
                    'delegation': str(result.get('task', ''))[:2000],
                    'phase': next_role, 'completed_dispatch_ids': ids}
        # A real user choice is distinct from an evidence/tool failure. Preserve
        # only an explicit, fault-free Leader question; defaults stay blocked.
        if (next_role == 'ask' and result.get('ask_kind') == 'business'
                and not result.get('code') and str(result.get('summary') or '').strip()):
            return {'status':'needs_input', 'goal_complete':False,
                    'code':'user_clarification_required',
                    'summary':str(result['summary'])[:1600], 'waiting_kind':'business',
                    'next_role':'end', 'phase':'needs_input', 'completed_dispatch_ids':ids}
        if next_role == 'complete':
            contract = self._current(state)['contract']
            typed = contract.get('current_deliverable')
            if typed in ('stage_catalog', 'copilot_codes'):
                from .planned_agents import verify_planned_completion
                checked = verify_planned_completion(self.store, state, typed)
                if checked.get('complete'):
                    return {'status':'planned_verified', 'goal_complete':True,
                            'summary':checked['summary'], 'phase':'finished', 'next_role':'end',
                            'completed_dispatch_ids':ids, 'pending_objectives':[]}
                return {'status':'needs_input', 'goal_complete':False, 'code':'planned_incomplete',
                        'summary':checked.get('summary','计划证据尚不完整。'),
                        'phase':'blocked', 'next_role':'end', 'completed_dispatch_ids':ids}
            research=state.get('role_results',{}).get('research',{})
            if research_satisfies_goal(self._current(state)['contract'],research):
                return {'status':'research_verified','goal_complete':True,
                    'summary':render_delivery(deliverables(self.store.engine.data_dir,research),completed=True,
                        require_roster=roster_required(self._current(state)['contract'])) if copilot_research_required(self._current(state)['contract'])
                        else research.get('summary') or '只读资料查询已有工具来源；未操作 MAA。',
                    'phase':'finished','next_role':'end','completed_dispatch_ids':ids,'pending_objectives':[]}
            receipt = state.get('role_results', {}).get('maa', {})
            # Import receipt is generated by the adapter, never by Leader narration.
            if (receipt.get('status') == 'import_verified' and receipt.get('evidence_refs')
                    and receipt.get('battle_start_submitted') is False):
                decision = completion_decision(self._current(state)['contract'], receipt)
                if decision.get('goal_complete'):
                    return {'status':'import_verified','goal_complete':True,
                            'summary':receipt.get('summary','MAA 加载已核实'),
                            'phase':'finished','next_role':'end','completed_dispatch_ids':ids,
                            'pending_objectives':[]}
                return {'status':'needs_input','goal_complete':False,
                        'code':decision.get('code','goal_not_covered'),
                        'summary':decision.get('summary') or '导入已核实，但用户完整目标仍有未完成部分。',
                        'pending_objectives':decision.get('pending_objectives',[]),
                        'phase':'blocked','next_role':'end','completed_dispatch_ids':ids}
        contract=self._current(state)['contract']
        if contract.get('current_deliverable') in ('stage_catalog','copilot_codes'):
            from .planned_agents import verify_planned_completion
            checked = verify_planned_completion(self.store, state, contract['current_deliverable'])
            summary = checked.get('summary') or result.get('summary') or '计划证据尚不完整。'
            if result.get('code') == 'plan_closed':
                summary += '\n计划已封口；同一计划不能重复检索，需诊断或修订。'
            return {'status':'needs_input', 'code':str(result.get('code') or ''),
                    'summary':summary, 'next_role':'end', 'phase':'needs_input',
                    'completed_dispatch_ids':ids, 'waiting_kind':'blocked'}
        if copilot_research_required(contract):
            research=state.get('role_results',{}).get('research',{})
            items=deliverables(self.store.engine.data_dir,research)
            actual={i['code'] for i in items if not roster_required(contract) or i['classification']=='satisfied'}
            summary=render_delivery(items,completed=False,require_roster=roster_required(contract),
                missing=[c for c in research_stage_codes(contract,research) if c not in actual])
        else:summary=str(result.get('summary') or '缺少可核实的 MAA 结果，已保留进度')[:1600]
        return {'status': 'needs_input', 'code':str(result.get('code') or ''), 'summary': summary,
                'next_role': 'end',
                'phase': 'needs_input', 'completed_dispatch_ids': ids,
                'waiting_kind': 'blocked'}

    def _adopt_research(self, state):
        self._current(state)
        try:
            dispatch = self.store.adopt_research(state['task_id'], state['source_dispatch_id'],
                f"epoch:{state['epoch']}:turn:{state.get('turn', 0)}:adopt:{state['source_dispatch_id']}",
                input_task=state['delegation'])
            result = self.store.dispatch_result(dispatch['dispatch_id'])
        except (ActionRejected, ValueError, OSError, KeyError):
            # Invalid review is a bounded fact for Leader; it never launches a
            # second research request or applies unchecked historical material.
            result = {'status': 'failed', 'code': 'research_adoption_rejected',
                      'summary': '历史研究的合同、输入、新鲜度或已登记文件不再一致，未采用'}
            return {'role_results': {**state.get('role_results', {}), 'research': result},
                    'last_role': 'research', 'turn': state.get('turn', 0) + 1, 'phase': 'leader'}
        self._current(state)
        return {'role_results': {**state.get('role_results', {}), 'research': result},
                'last_role': 'research', 'turn': state.get('turn', 0) + 1, 'phase': 'leader',
                'completed_dispatch_ids': [*state.get('completed_dispatch_ids', []), dispatch['dispatch_id']]}

    def _worker(self, role):
        def work(state):
            result, dispatch_id = self._call(role, state)
            step = 'battle' if role == 'maa' and state.get('phase') == 'battle' else role
            results = {**state.get('role_results', {}), step: result}
            # Keep a compact, factual handoff trail, not each role's messages or
            # protocol reasoning. It survives a checkpoint and informs Leader.
            brief = {key:copy.deepcopy(result[key]) for key in
                     ('status','code','summary','evidence_refs','candidate_refs','unknowns','proposals')
                     if key in result}
            task = state.get('delegation','')
            signature = hashlib.sha256(json.dumps({'role':role,'task':task,'result':brief},
                ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')).hexdigest()
            history = [*state.get('handoff_history',[]),
                       {'role':role,'task':task,'dispatch_id':dispatch_id,
                        'result':brief,'result_hash':signature}][-12:]
            stalled = sum(item.get('result_hash') == signature for item in history[-8:]) >= 3
            update = {'role_results': results, 'last_role': role,
                    'turn': state.get('turn', 0) + 1,
                    'completed_dispatch_ids': [*state.get('completed_dispatch_ids', []), dispatch_id],
                    'phase': 'leader','handoff_history':history}
            if step == 'maa' and result.get('status') == 'import_verified':
                update['import_dispatch_id'] = dispatch_id
            if stalled:
                update.update(code='no_progress',status='needs_input',goal_complete=False,
                              summary=f'{ROLE_NAMES[role]} 的相同委派连续未产生新证据或结果，已保留进度并停止重复处理。')
            return update
        return work

    def run(self, task_id):
        with self._lock:
            current = self.store.current(task_id)
            if not current['writes_enabled'] or self.cancel.is_set():
                return {'status': 'paused', 'summary': '普通执行已撤销，尚未恢复输入权限'}
            self.store.audit_abandoned_dispatches(task_id)
            config = {'configurable': {'thread_id': task_id}, 'recursion_limit': 64,
                      'callbacks': []}
            snapshot = self.graph.get_state(config)
            if snapshot.values and snapshot.values.get('epoch') == current['epoch']:
                initial = None
                if not snapshot.next:
                    self._ack_completed(task_id, snapshot.values)
                    return dict(snapshot.values)
            else:
                initial = {'task_id': task_id, 'epoch': current['epoch'],
                           'revision': current['revision'], 'turn': 0, 'phase': 'leader',
                           'role_results': {}, 'completed_dispatch_ids': [],
                           'handoff_history': [], 'goal_complete': False, 'pending_objectives': [], 'code':'',
                           'last_role': '', 'source_dispatch_id': '', 'import_dispatch_id': '', 'waiting_kind':'blocked',
                           'status': 'running', 'delegation': current['contract'].get('goal', '')}
            # Local execution does not opt the user's account facts or task
            # traces into a cloud tracing destination via inherited env vars.
            with tracing_context(enabled=False):
                result = self.graph.invoke(initial, config=config, durability='sync')
            self._ack_completed(task_id, result)
            return dict(result)

    def _ack_completed(self, task_id, checkpoint_values):
        """An already terminal durable checkpoint can finish its missing ACKs."""
        completed = set(checkpoint_values.get('completed_dispatch_ids', []))
        for event in self.store.pending_events(task_id):
            if event.get('payload', {}).get('dispatch_id') in completed:
                self.store.accept_event('maa-graph', event['event_id'])
                self.store.ack_event(event['event_id'])

    def close(self):
        self.cancel.set()
        # Acquiring the same execution lock proves that this runner's handlers
        # have left, even if the enclosing application process remains alive.
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.store.release_runner(self.owner)
            self.connection.close()

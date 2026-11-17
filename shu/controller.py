"""Qt orchestration for conversation, real observation and authorized input.

Visual task completion records its model source; battle settlement counting
remains a separate trusted path.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace, field
import json
import math
from pathlib import Path
import re
import threading
import time
import uuid
from weakref import WeakKeyDictionary
from typing import Any, Callable
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Signal, Slot, QTimer

from .core import ActionRejected, TaskEngine, TERMINAL_STATUSES
from .providers import (DeepSeekClient, GameSession, HarnessBackend, PersonaSession,
                        ProviderCancelled, ProviderConfig, ProviderError)
from .windows import WindowController, WindowError
from .memory import ConversationMemory
from .game_executor import GameExecutor
from .coordinator import ExecutionCoordinator
from .maa_coordinator import MaaCoordinator
from .runtime import RuntimeStore
from .knowledge import KnowledgeStore
from .research_tools import ResearchTools
from .network_dialogue import run_network_dialogue
from .activity_events import make_activity_event
from .leader_policy import runtime_capabilities
from .task_contract import (ROUTE_USER_TOOL,ROUTE_TOOL_CHOICE,ROUTE_USER_PROMPT,
                           parse_task_contract,TaskContractError)


def _known_task_view(tasks, current_task_id, limit=12):
    """Show a bounded index; keep full contracts local for selected-ID validation."""
    selected = list(tasks[:limit])
    if current_task_id and not any(row.get('task_id') == current_task_id for row in selected):
        current = next((row for row in tasks if row.get('task_id') == current_task_id), None)
        if current is not None:
            selected.append(current)
    return [{'task_id': row['task_id'], 'status': row.get('status'),
             'goal': str(row.get('goal', ''))[:160],
             'activity_name': (row.get('contract') or {}).get('activity_name', ''),
             'chapter_scope': (row.get('contract') or {}).get('chapter_scope'),
             'current_deliverable': (row.get('contract') or {}).get('current_deliverable'),
             'target_stage_codes': (row.get('contract') or {}).get('target_stage_codes', []),
             'import_scope': (row.get('contract') or {}).get('import_scope'),
             'revision':row.get('revision'), 'epoch':row.get('epoch')}
            for row in selected]


def _function(name: str, description: str, properties: dict | None = None) -> dict:
    properties = properties or {}
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties), "additionalProperties": False}}}


ROLE_TOOLS = [
    _function("observe_game", "用户要求看看、描述或分析当前游戏画面时调用此工具。仅在本地截图权限已开启后获取真实窗口并交给独立游戏会话分析；不执行点击。单纯观察不需要新建游戏目标。"),
    _function("request_game_task", "保存用户明确要求的游戏目标。仅保存草稿，不执行游戏。",
              {"goal": {"type": "string", "description": "保留次数、关卡、资源禁用等用户约束的完整目标"}}),
    _function("execute_game_task","用户明确要求你现在实施游戏任务时调用。仅当本地游戏操作授权已开启才会执行；使用真实截图逐步操作与检查。",
              {"goal":{"type":"string","description":"完整目标和约束；遇到未确认的换班方案、资源花费等条件先询问，不臆造游戏已有某项功能"}}),
    _function("change_game_goal", "修改当前未结束任务的目标。原观察和旧动作立即失效。",
              {"goal": {"type": "string", "description": "修改后完整目标，保留未变更的用户约束"}}),
    _function("get_task_status", "获取应用已验证的任务事实，不能推断游戏进度。"),
    _function("pause_task", "暂停当前任务，停止本地输入。"),
    _function("resume_task", "恢复为等待新观察；不会开启输入或自动执行。"),
    _function("stop_task", "停止当前任务，使旧观察和动作失效。"),
]
GAME_TOOLS = [_function("propose_next_step", "仅提出一个有界建议；本工具不执行、不证明成功。", {
    "action": {"type": "string", "description": "建议的单步动作，无法确认时建议重新观察或询问"},
    "reason": {"type": "string", "description": "依据当前画面的理由及不确定性"},
    "expected_result": {"type": "string", "description": "未来应通过新观察验证的预期结果"},
})]


def _plain_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    return ""


def _current_delivery_repeat(text, contract):
    """Only a request to repeat the current job artifacts can skip routing."""
    if (contract or {}).get('current_deliverable') not in {'copilot_codes','copilot_json'}:
        return False
    if re.search(r'章节|第[零〇一二两三四五六七八九十百0-9]+[章篇节]|关卡目录|分区|导入|载入|MAA',text,re.I):
        return False
    positive_query=re.sub(r'(?:不要|不需要|无需|不用|不必|别)(?:再|重新|重复)?'
        r'(?:搜索|检索|查询|查找|查(?:一遍|一次|一下)?)','',text)
    if (not re.search(r'神秘代码|作业码|代码|JSON|作业(?:清单|结果)?',text,re.I)
            or not re.search(r'列|给|发|贴|展示|输出|重复|重说|再说|看看',text)
            or re.search(r'是什么|什么意思|怎么|如何|为什么|为何|含义|用法|用途|解释|原理',text)
            or re.search(r'重新|另一个|换|新活动|查找|搜索|查询|再查|查一|查下|历史|上一个|再.{0,12}找|没查到|没有合适|重试|补查',positive_query)):
        return False
    named={code.upper() for code in re.findall(
        r'(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+(?![A-Za-z0-9_-])',text)}
    if named:
        from .leader_policy import research_stage_codes
        known={str(code).upper() for code in research_stage_codes(contract)}
        if not named<=known:
            return False
    elif (not re.search(r'刚才|当前|之前|上面|这些|这份|查到|找到|再',text)
            and not re.fullmatch(r'(?:请|麻烦)?(?:给出|给我|发我|列出|展示|输出)(?:一下|你)?'
                r'(?:神秘代码|作业码|代码|JSON|作业清单)(?:吧|一下|。|！|!|\s)*',text,re.I)):
        # A named activity or other unresolved subject needs the model's scope
        # resolution; it must not silently inherit whichever task is current.
        return False
    return True


def _dialogue_history(messages):
    """Bound old conversational context; the complete local memory is untouched."""
    result=[]
    for message in messages:
        row=copy.deepcopy(message)
        if isinstance(row.get('content'),str):
            limit=900 if row.get('role')=='user' else 1200
            if len(row['content'])>limit:
                row['content']=row['content'][:limit]+'\n[历史显示摘要；完整记录保留本地，任务约束以当前结构化合同为准]'
        result.append(row)
    return result


def _record_persona_metrics(controller,client,request_id):
    seen=getattr(controller,'_metrics_seen',None)
    if seen is None:
        seen=controller._metrics_seen=WeakKeyDictionary()
    watermark=seen.get(client,0)
    for metric in list(getattr(client,'metrics_history',[])):
        number=metric.get('request_number',0)
        if type(number) is int and number>watermark:
            controller.runtime.record_call('persona',{**metric,'request_id':request_id},None)
            watermark=number
    seen[client]=watermark


@dataclass
class _Operation:
    number: int
    generation: int
    cancel: threading.Event
    task_id: str | None
    version: int | None
    execution_started: bool = False
    task_bound: bool = True
    request_id: str | None = None
    route_versions: dict = field(default_factory=dict)


class SessionController(QObject):
    ANALYSIS_MAX_AGE_SECONDS = 30.0
    request_started = Signal(str, str)
    message_routed = Signal(dict)
    context_message = Signal(dict)
    message = Signal(str, str)
    changed = Signal()
    observation_ready = Signal(dict)
    plan_ready = Signal(dict)
    notice = Signal(str)
    network_progress = Signal(dict)
    activity_progress = Signal(dict)
    cleanup_progress = Signal(dict)
    error = Signal(str)
    busy_changed = Signal(bool)
    execution_changed = Signal(bool)
    capture_preparing = Signal()
    _finished = Signal(object)

    def __init__(self, root: Path, data_dir: Path, key_path: Path | None = None, *,
                 legacy_tools: bool = False, execution_factory=None):
        super().__init__()
        self.root, self.data_dir = Path(root), Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.key_path = Path(key_path) if key_path is not None else self.root / "DeepSeek_API_Key.txt"
        self.engine = TaskEngine(self.data_dir)
        self.memory = ConversationMemory(self.data_dir)
        if legacy_tools or execution_factory is not None:
            self.engine.recover_interrupted_tasks()
        self.windows = WindowController(self.data_dir)
        tasks = self.engine.list_tasks()
        self._task_id = tasks[0]["id"] if tasks else None
        self._lock = threading.RLock()
        self._generation, self._sequence = 0, 0
        self._closed=False
        self._active: _Operation | None = None
        self._latest_observation: dict | None = None
        self.is_busy = False
        self.connection_verified = False
        self.last_network_report = {'sources': [], 'events': []}
        self.last_intent_contract = None
        self.game_connection_verified = False
        self.game_input_allowed=False
        self._executing=False
        self._game_execution_op=None
        self.legacy_tools=legacy_tools
        self.execution_max_steps=12
        self.execution_max_seconds=240
        self.config = self._load_config()
        self.game_config = replace(self.config, model=self._settings.get("game_model", self.config.model),
                                   thinking=self._settings.get("game_thinking", True),max_tokens=4096)
        self._rebuild_sessions()
        self.runtime=RuntimeStore(self.data_dir)
        self.knowledge=KnowledgeStore(self.data_dir)
        from .source_policy import install_user_source_preferences
        install_user_source_preferences(self.knowledge)
        if self.legacy_tools or execution_factory is not None:
            factory=execution_factory or ExecutionCoordinator
            self.execution=factory(self.engine,self.windows,self.runtime,
                lambda:DeepSeekClient(self.game_config),lambda:(self.screen_capture_allowed,self.game_input_allowed),knowledge=self.knowledge)
            from .wiki import WikiKnowledge
            self.execution.wiki=WikiKnowledge(self.knowledge)
        else:
            self.execution=MaaCoordinator(self.engine,self.windows,self.runtime,
                directory=self.data_dir,role_config=lambda:self.leader_config,
                task_config=lambda:self.game_config,
                permissions=lambda:(self.screen_capture_allowed,self.game_input_allowed),
                knowledge=self.knowledge,single_import_enabled=True,queue_import_enabled=True)
        self.execution.current_task_id=self._task_id
        if isinstance(self.execution, MaaCoordinator):
            tasks = self.execution.store.management.list_tasks()
            self._task_id = tasks[0]['task_id'] if tasks else None
            self.execution.current_task_id=self._task_id
        self.execution.activity.connect(self._execution_activity)
        self.execution.changed.connect(self._execution_changed)
        self.execution.event.connect(self._execution_event)
        self.execution.finished.connect(self._execution_finished)
        self._finished.connect(self._finish)
        self.message.connect(self._remember_message)
        self._message_request_id = self._message_task_id = None
        self.dialogue = None
        self._processing_request_id = None
        if isinstance(self.execution, MaaCoordinator):
            from .task_dialogue import TaskDialogue
            self.dialogue = TaskDialogue(self)
            self.memory.set_task_visibility(lambda tid:
                self.execution.store.management.lifecycle(tid)['state'] == 'active')
            self.runtime.set_task_writable(lambda tid:
                self.execution.store.management.lifecycle(tid)['state'] == 'active')
            QTimer.singleShot(0, self._recover_messages)

    def task_management_state(self):
        state = self.dialogue.state() if self.dialogue else {}
        return {**state, 'permissions': {'screen_capture': self.screen_capture_allowed is True,
                                        'maa_input': self.game_input_allowed is True}}

    def _emit_message(self, role, text):
        self.context_message.emit({'role':role, 'text':text,
            'request_id':getattr(self,'_message_request_id',None),
            'task_id':getattr(self,'_message_task_id',None)})
        self.message.emit(role,text)

    def set_input_target(self, task_id=None, mode='auto'):
        if self.dialogue:
            self.dialogue.set_target(task_id, mode)

    def trash_task(self, task_id):
        return self.dialogue.trash(task_id)

    def restore_task(self, task_id):
        return self.dialogue.restore(task_id)

    def purge_task(self, task_id):
        return self.dialogue.purge(task_id)

    def start_task_cleanup(self, task_id):
        return self.dialogue.start_purge(task_id)

    def trash_chat(self,request_id):
        return self.dialogue.trash_chat(request_id)

    def restore_chat(self,request_id):
        return self.dialogue.restore_chat(request_id)

    def start_chat_cleanup(self,request_id):
        return self.dialogue.start_chat_purge(request_id)

    def continue_task(self, task_id):
        self.execution.store.management.require_active(task_id)
        from .core import TaskNotFound
        try:
            state = self.execution.store.current(task_id)
        except TaskNotFound:
            state = {}  # Existing coordinator adopts legacy drafts safely.
        contract = state.get('contract', {})
        if (self._is_maa_import(contract)
                and not (self.screen_capture_allowed and self.game_input_allowed)):
            return {'accepted':False,'task_id':task_id,'status':'needs_input',
                    'code':'maa_permission_required',
                    'summary':'尚未开启 MAA 截图或本次输入权限。请点击任务页授权按钮，授权后继续原任务。'}
        outcome = self.execution.resume(task_id)
        self._task_id = task_id
        self.changed.emit()
        return outcome

    @staticmethod
    def _is_maa_import(contract):
        spec = contract.get('task_spec', {})
        return (isinstance(spec, dict) and spec.get('kind') == 'import'
                or contract.get('current_deliverable') == 'maa_import')

    def authorize_maa_import(self, task_id):
        """Explicit task-page click; never called by models or task recovery."""
        from .leader_policy import import_satisfies_goal
        self.execution.store.management.require_active(task_id)
        state = self.execution.store.current(task_id)
        contract = state.get('contract', {})
        if (state.get('status') not in {'needs_input','paused'}
                or not self._is_maa_import(contract) or not import_satisfies_goal(contract)
                or contract.get('allow_start') is not False or contract.get('attempt_limit',0) != 0):
            raise ValueError('只有仍在等待的仅导入任务可从此入口授权。')
        if not self.screen_capture_allowed:
            self.set_capture_permission(True)
        if not self.screen_capture_allowed:
            raise ValueError('画面读取权限未能开启，尚未继续任务。')
        self.set_game_input_permission(True)
        return self.continue_task(task_id)

    def cancel_task(self, task_id):
        outcome = self.execution.stop(task_id)
        self.changed.emit()
        return outcome

    def stop_task(self, task_id):
        return self.cancel_task(task_id)

    def _recover_messages(self):
        if self._closed or not self.dialogue:
            return
        self.memory.sync_request_bindings(self.dialogue.management.messages())
        # Resume only already-authorized cleanup jobs; this never launches tasks.
        self.dialogue.resume_cleanup()
        known = {i['request_id'] for i in self.dialogue.pending}
        if self._processing_request_id:
            known.add(self._processing_request_id)
        for item in self.dialogue.management.messages(pending_only=True):
            if item['request_id'] not in known:
                self.dialogue.pending.append(item)
        self._drain_messages()

    def _drain_messages(self):
        if self._closed or self.is_busy or not self.dialogue or not self.dialogue.pending:
            return
        item = self.dialogue.pending.popleft()
        try:
            current = next(m for m in self.dialogue.management.messages() if m['request_id']==item['request_id'])
            if current['status'] in {'applied','failed','cancelled'}:
                QTimer.singleShot(0,self._drain_messages)
                return
            if item.get('target_task_id'):
                self.dialogue.management.require_active(item['target_task_id'])
            self._processing_request_id = item['request_id']
            self._process_contract(item['text'], request_id=item['request_id'], input_target={
                'mode': item.get('mode', 'auto'), 'task_id': item.get('target_task_id')})
        except (ActionRejected, ValueError, KeyError, StopIteration) as exc:
            self._processing_request_id=None
            self.error.emit(self._safe_error(exc))
            QTimer.singleShot(0,self._drain_messages)

    def _load_config(self) -> ProviderConfig:
        saved: dict = {}
        try:
            candidate = json.loads((self.data_dir / "settings.json").read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                self._validate_settings(candidate.get("model", "deepseek-flash"),
                                        candidate.get("base_url", "https://api.deepseek.com"))
                self._validate_settings(candidate.get("game_model", candidate.get("model", "deepseek-flash")),
                                        candidate.get("base_url", "https://api.deepseek.com"))
                for name in ("game_thinking", "intent_thinking", "leader_thinking"):
                    if not isinstance(candidate.get(name, True), bool):
                        raise ValueError("Invalid thinking setting: " + name)
                saved = candidate
        except (OSError, ValueError, TypeError, ProviderError):
            pass
        self._settings = saved
        self.screen_capture_allowed = saved.get("screen_capture_allowed") is True
        return ProviderConfig(model=saved.get("model", "deepseek-flash"),
                              base_url=saved.get("base_url", "https://api.deepseek.com"),
                              api_key_file=self.key_path, dsh_home=self.data_dir / "harness")

    def _rebuild_sessions(self) -> None:
        # Routing and durable Leader decisions need their own reasoning budget;
        # ordinary character dialogue keeps the original non-thinking client.
        self.intent_config = replace(self.config,
            thinking=self._settings.get("intent_thinking", True),
            max_tokens=8192, timeout_seconds=120)
        self.leader_config = replace(self.config,
            thinking=self._settings.get("leader_thinking", True),
            max_tokens=8192, timeout_seconds=120)
        self.client = DeepSeekClient(self.config)
        self.intent_client = DeepSeekClient(self.intent_config)
        self.game_client = DeepSeekClient(self.game_config)
        self.persona = self._new_persona()
        self.game = GameSession(self.game_client)

    def _new_persona(self, *, routing: bool = True) -> PersonaSession:
        session = PersonaSession(self.intent_client if routing and not self.legacy_tools else self.client)
        session.response_language=getattr(self,'response_language','zh')
        base_prompt=session.messages[0]['content']
        session.messages[0]["content"] += (
            "\n用户只是想看看或分析当前游戏时，使用 observe_game 获取真实观察，"
            "不要把这种请求仅保存为游戏目标，也不要凭模型记忆描述尚未取得的画面。"
            "截图授权只能由用户在本地设置开启；工具未成功时说明实际障碍。"
            "是否能操作以本次摘要 game_input_allowed 为准。用户要求现在执行任务时调用 execute_game_task，"
            "不要指引用户寻找不存在的开关。只有实际执行结果才能说明已经操作。"
            "向用户用自然语言解释，不输出 input_enabled、needs_input 等内部字段，也不要把待验证标记说成模型完全不懂游戏。"
            "不要臆造明日方舟存在一键自动换班等按钮，先依据真实页面判断。")
        session.messages[0]['content'] += (
            '\n用户已授权且明确要求立即执行界面目标时，先调用 execute_game_task；执行器自己负责取得新截图。'
            '无需先把游戏语义或自主战斗标成已验证。空闲时底层输入关闭是正常状态，不代表本次授权关闭。'
            '没有调用 observe_game 或 execute_game_task 取得画面时，不得声称已经看到当前页面。')
        if not getattr(self,'legacy_tools',False):session.messages[0]['content']=base_prompt+('\n'+ROUTE_USER_PROMPT if routing else '')
        history=self.memory.model_messages(last_turns=4 if routing and not self.legacy_tools else 12)
        session.messages.extend(_dialogue_history(history) if routing and not self.legacy_tools else history)
        return session

    def set_response_language(self,language: str) -> None:
        if language not in ('zh','ja'):raise ValueError('Unsupported response language')
        self.response_language=language
        self.persona.response_language=language

    @Slot(str,str)
    def _remember_message(self,role: str,text: str) -> None:
        try:
            if getattr(self, 'dialogue', None):
                self.dialogue.record_message(role, text, request_id=self._message_request_id,
                                             task_id=self._message_task_id)
            else:
                self.memory.append(role,text)
        except Exception:
            self.notice.emit("这条对话未能保存到本地，当前会话仍可继续。")

    def close(self) -> None:
        if self._closed:return
        self.windows.set_stop()
        active=self._active
        if active:active.cancel.set()
        with self._lock:
            if self._closed:return
            self._closed=True
            self._cancel_pending()
            self._latest_observation=None
            self.execution.current_task_id=self._task_id
        # Closing (including an administrator restart) preserves a paused
        # checkpoint. Explicit stop() remains a terminal user cancellation.
        if self.dialogue:
            self.dialogue.close()
        self.execution.close()
        self.runtime.close()
        self.knowledge.close()
        self.memory.close()
        self.engine.close()

    @property
    def has_key(self) -> bool:
        try:
            return bool(self.config.resolve_key())
        except ProviderError:
            return False

    def current_task(self) -> dict | None:
        with self._lock:
            return self.engine.get_task(self._task_id) if self._task_id else None

    def harness_availability(self) -> dict:
        return HarnessBackend(self.config, self.root).availability()

    def _bind_operation_task(self, op: _Operation, task: dict | None) -> None:
        op.task_id = task["id"] if task else None
        op.version = task["version"] if task else None

    def _valid(self, op: _Operation) -> bool:
        if op.cancel.is_set() or op.generation != self._generation:
            return False
        if not op.task_bound:return True
        current = self.current_task()
        return ((current["id"], current["version"]) if current else (None, None)) == (op.task_id, op.version)

    def _require_valid(self, op: _Operation) -> None:
        if not self._valid(op):
            raise ProviderCancelled("目标或本地控制已改变，旧响应已丢弃。")

    def _cancel_pending(self) -> None:
        """Caller holds the state lock. Never waits on network or session locks."""
        self._generation += 1
        if self._active:
            self._active.cancel.set()
        if getattr(self,'dialogue',None):
            ids={item['request_id'] for item in self.dialogue.pending}
            if self._processing_request_id:ids.add(self._processing_request_id)
            if self._active and self._active.request_id:
                ids.add(self._active.request_id)
            for record in self.dialogue.management.messages(pending_only=True):
                if record['request_id'] in ids:
                    self.dialogue.management.finish_message(record['request_id'],
                        task_id=record.get('task_id'),status='cancelled',summary='用户已取消本条输入。')
            self.dialogue.pending.clear()
        self._active = None
        # Replace, rather than reset, sessions: a cancelled worker may still be
        # holding an old session lock while its socket unwinds.
        self.persona, self.game = self._new_persona(), GameSession(self.game_client)
        if self.is_busy:
            self.is_busy = False
            self.busy_changed.emit(False)

    def _start(self, work: Callable[[_Operation], dict], *, task_bound: bool = True) -> bool:
        with self._lock:
            if self.is_busy:
                self.notice.emit("正在处理上一条；暂停和停止仍可立即使用。")
                return False
            self._sequence += 1
            task = self.current_task()
            op = _Operation(self._sequence, self._generation, threading.Event(), None, None)
            op.task_bound=task_bound
            self._bind_operation_task(op, task)
            self._active, self.is_busy = op, True
            self.busy_changed.emit(True)

        def run() -> None:
            try:
                result, failure = work(op), None
            except ProviderCancelled:
                result, failure = {}, None
            except (ProviderError, WindowError, ActionRejected, ValueError) as exc:
                result, failure = {}, self._safe_error(exc)
            except Exception:
                result, failure = {}, "本次处理未完成，请检查连接或重新观察。"
            self._finished.emit((op, result, failure))

        threading.Thread(target=run, name=f"shu-controller-{op.number}", daemon=True).start()
        return True

    def _safe_error(self, exc: Exception) -> str:
        text = str(exc)
        try:
            key = self.config.resolve_key()
            if key:
                text = text.replace(key, "[Key 已隐藏]")
        except ProviderError:
            pass
        text = re.sub(r"\bsk-[A-Za-z0-9_.\-]{6,}", "[Key 已隐藏]", text)
        return re.sub(r"(?i)Bearer\s+\S+", "Bearer [已隐藏]", text)[:500]

    @Slot(object)
    def _finish(self, payload: tuple) -> None:
        try:
            self._finish_result(payload)
        finally:
            if self._processing_request_id==payload[0].request_id:
                self._processing_request_id = None
            QTimer.singleShot(0, self._drain_messages)

    def _finish_result(self, payload: tuple) -> None:
        op, result, failure = payload
        with self._lock:
            if self._active is not op:
                return
            valid = self._valid(op)
            self._active, self.is_busy = None, False
            self.busy_changed.emit(False)
            if not valid:
                self.notice.emit("目标已改变，旧响应已丢弃。")
                return
            if failure:
                if op.request_id and self.dialogue:
                    self.dialogue.management.finish_message(op.request_id, status='failed', summary=failure)
                    self.activity_progress.emit(make_activity_event('intent_failed', role='leader',
                        request_id=op.request_id, status='failed', summary=failure))
                self.error.emit(failure)
                self.changed.emit()
                return
            try:
                self._message_request_id = op.request_id
                self._message_task_id = result.get('task_id')
                self._apply_result(op, result)
            except (ProviderError, WindowError, ActionRejected, ValueError, KeyError) as exc:
                if op.request_id:
                    self.activity_progress.emit(make_activity_event('intent_failed',role='leader',
                        request_id=op.request_id,status='failed',summary=self._safe_error(exc)))
                self.error.emit(self._safe_error(exc))
            except Exception:
                if op.request_id:
                    self.activity_progress.emit(make_activity_event('intent_failed',role='leader',
                        request_id=op.request_id,status='failed',summary='本次结果无法接受。'))
                self.error.emit("本次结果无法接受，请重新获取观察。")
            finally:
                self._message_request_id = self._message_task_id = None
            self.changed.emit()

    def _apply_result(self, op: _Operation, result: dict) -> None:
        """Called on the Qt thread while the controller state lock is held."""
        kind = result.get("kind")
        if kind == "chat":
            if 'intent_contract' in result:
                self.last_intent_contract = copy.deepcopy(result['intent_contract'])
            if 'network_report' in result:
                self.last_network_report = result['network_report']
            self.connection_verified = True
            if result.get("content"):
                self._emit_message("assistant", result["content"])
            else:
                self._emit_message("system", "本次已达到工具处理上限；已记录的任务结果请查看轨迹。")
        elif kind == "connection":
            self.connection_verified = True
            self.notice.emit("DeepSeek 真实请求已通过；游戏识别与自动执行仍待验证。")
        elif kind == "capture":
            self._accept_observation(op, result["observation"])
            self.notice.emit("已取得真实截图；画面语义、游戏状态及作战结果尚未确认。")
        elif kind == "analysis":
            self._accept_plan(op, result["plan"])

    def _require_capture_permission(self) -> None:
        if not self.screen_capture_allowed:
            raise ProviderError("尚未授权获取游戏画面。请从桌宠设置中开启屏幕读取权限；绑定窗口不会自动授权。")

    def _accept_observation(self, op: _Operation, raw: dict) -> dict:
        self._require_valid(op)
        self._require_capture_permission()
        observation = copy.deepcopy(raw)
        observation["state"] = "unknown"
        task = self.current_task()
        if task and task["status"] not in TERMINAL_STATUSES | {"paused"}:
            if task["status"] == "draft":
                task = self.engine.set_status(task["id"], "observing", "已获取像素，等待观察分析", goal_version=op.version)
            self.engine.record_observation(task["id"], observation["id"], observation["captured_at"],
                                           observation["window_id"], "unknown", goal_version=op.version)
        observation["task_id"], observation["goal_version"] = op.task_id, op.version
        self._latest_observation = observation
        self.observation_ready.emit(copy.deepcopy(observation))
        return observation

    def _accept_plan(self, op: _Operation, plan: dict) -> None:
        self._require_valid(op)
        self._require_capture_permission()
        self._validate_observation(self._latest_observation)
        if self._latest_observation["id"] != plan["observation_id"]:
            raise ProviderError("观察已更新，旧分析不再展示，请重新分析。")
        task = self.current_task()
        if task and task["status"] not in TERMINAL_STATUSES | {"paused"}:
            self.engine.record_event(task["id"], "model_suggestion", "模型提出观察建议，尚未执行", {
                "observation_id": plan["observation_id"], "summary": plan["summary"], "executed": False,
            }, goal_version=op.version)
        self.plan_ready.emit(plan)
        self.game_connection_verified = True

    def submit(self, text: str, mode: int = 0) -> None:
        if mode==0 and not self.legacy_tools:
            self._submit_contract(text)
            return
        text = text.strip()
        if not text:
            return
        with self._lock:
            if self.is_busy:
                self._emit_message("system", "上一条仍在处理，本条尚未提交。可先停止操作后重新发送。")
                return
            self._emit_message("user", text)
            if mode == 2 or (mode == 0 and not self.has_key):
                self._save_draft(text)
                message = "游戏目标已保存为草稿，尚未执行。"
                if not self.has_key:
                    message += "尚未读取到 DeepSeek Key，本次没有调用模型或生成 AI 计划。"
                self._emit_message("system", message)
                self.changed.emit()
                return
            if not self.has_key:
                self._emit_message("system", "尚未读取到 DeepSeek Key，当前无法调用角色模型。请在本地文本填写后重新读取。")
                return
            persona = self.persona if self.legacy_tools or mode==0 else self._new_persona(routing=False)

        def work(op: _Operation) -> dict:
            if mode != 0:
                report = self._network_turn(persona, text, op, routing=False)
                response = report['response']
                return {'kind': 'chat', 'content': self._network_answer(_plain_text(response.get('content')), report),
                        'network_report': {k: report[k] for k in ('sources', 'events')}}
            for index in range(4):
                with self._lock:
                    self._require_valid(op)
                    summary = self._task_summary()
                response = persona.respond(text if index == 0 else "", task_summary=summary,
                                           tools=ROLE_TOOLS if mode == 0 else None, cancel_event=op.cancel)
                with self._lock:
                    self._require_valid(op)
                calls = response.get("tool_calls") or []
                if not calls:
                    return {"kind": "chat", "content": _plain_text(response.get("content"))}
                if mode != 0:
                    raise ProviderError("纯聊天会话未授权任务工具；未执行任何请求。")
                for call in calls:
                    result = self._execute_role_tool(op, call)
                    persona.record_tool_result(call["id"], result)
            return {"kind": "chat", "content": ""}

        self._start(work)

    def _network_turn(self, persona, text, op, *, routing=True, research=None, summary=None, route_only=False, request_id=None):
        with self._lock:
            self._require_valid(op)
            self.last_network_report = {'sources': [], 'events': []}
        def activity(stage, **facts):
            self._require_valid(op)
            event = make_activity_event(stage, task_id=None if request_id else op.task_id, epoch=op.version, request_id=request_id, **facts)
            if event is not None:
                self.activity_progress.emit(event)
        activity('intent_started', role='leader', status='running',
                 summary='Leader：正在理解本条要求并选择所需工具。')
        def progress(event):
            self._require_valid(op)
            self.network_progress.emit(dict(event))
            label = event.get('label') or event.get('summary')
            if not label:
                label = '研究 Agent：正在联网检索' if event.get('stage') == 'tool_started' else '研究 Agent：正在核对检索结果'
            self.notice.emit(str(label)[:240])
        try:
            report = run_network_dialogue(persona, text, task_summary=self._task_summary() if summary is None else summary,
                research_tools=research or ResearchTools(self.config, self.data_dir / 'network-cache'),
                cancel_event=op.cancel, progress=progress,
                guard=lambda: self._require_valid(op), routing=routing, route_only=route_only)
        except ProviderCancelled:
            raise
        except Exception:
            activity('intent_failed', role='leader', status='failed',
                     summary='本条要求的模型或工具处理未完成，请查看具体错误提示。')
            raise
        finally:
            _record_persona_metrics(self,persona.client,request_id)
        self._require_valid(op)
        if not routing:
            activity('intent_finished', role='leader', status='completed',
                     summary='本条对话或资料查询已返回；未提交 MAA 操作。')
        return report

    @staticmethod
    def _network_answer(answer, report):
        # Only real tool sources become the product's source list. No URLs are
        # manufactured from an activity name or a model-written citation.
        sources = report.get('sources', [])
        missing = [s for s in sources[:5] if s.get('url') and s['url'] not in answer]
        if missing:
            answer += '\n\n检索来源：\n' + '\n'.join(
                '- ' + str(s.get('title') or '来源')[:100] + '：' + s['url'] for s in missing)
        return answer

    def _submit_contract(self,text:str)->None:
        text = text.strip()
        if not text:
            return
        if not self.dialogue:
            return self._process_contract(text)
        request_id = uuid.uuid4().hex
        with self._lock:
            self.dialogue.accept(request_id, text)
            self.request_started.emit(request_id, text)
            self._message_request_id = request_id
            self._emit_message('user', text)
            self._message_request_id = None
        self._drain_messages()

    def _process_contract(self,text:str, *, request_id=None, input_target=None)->None:
        text=text.strip()
        if not text:return
        with self._lock:
            if self.is_busy:
                self._emit_message('system','正在理解上一条消息；停止按钮随时可用。');return
            if request_id is None:
                request_id=uuid.uuid4().hex
                self.request_started.emit(request_id,text)
                self._emit_message('user',text)
            input_target = input_target or {'mode': 'auto', 'task_id': None}
            if (not self.execution.active and self._task_id
                    and callable(getattr(self.execution,'research_output',None))
                    and input_target['mode'] == 'auto'
                    and _current_delivery_repeat(text,self.execution.contract(self._task_id))):
                self.last_intent_contract = {'intent':'chat', 'relation':'new', 'current_deliverable':'chat',
                    'user_request':text, 'request_id':request_id, 'reused_delivery_task_id':self._task_id}
                self.activity_progress.emit(make_activity_event('intent_finished', role='leader',
                    request_id=request_id, status='understood',
                    summary='重列已核验的作业代码；本条没有重新检索或修改原任务。'))
                self._message_request_id,self._message_task_id=request_id,self._task_id
                self._emit_message('assistant',self.execution.research_output(self._task_id)['summary'])
                self._message_request_id=self._message_task_id=None
                if self.dialogue:
                    self.dialogue.management.finish_message(request_id, task_id=self._task_id, status='applied')
                    QTimer.singleShot(0, self._drain_messages)
                return
            if not self.has_key:
                self._save_draft(text)
                self._message_request_id,self._message_task_id=request_id,self._task_id
                self._emit_message('system','目标已保存；请配置本地 API Key 后再执行。')
                self._message_request_id=self._message_task_id=None
                if self.dialogue:
                    self.dialogue.management.finish_message(request_id, task_id=self._task_id,
                        status='applied', summary='草稿已保存，等待本地 Key。')
                    QTimer.singleShot(0, self._drain_messages)
                return
            persona=self._new_persona()
            if input_target.get('mode') == 'task' and input_target.get('task_id'):
                persona.messages=persona.messages[:1]+_dialogue_history(
                    self.memory.model_messages(last_turns=12,task_id=input_target['task_id']))
            self.persona=persona
        def work(op):
            op.request_id = request_id
            previous=self.execution.contract(self._task_id)
            if not previous and self.current_task():previous={'goal':self.current_task()['goal']}
            known_tasks=[]
            visible_tasks = self.dialogue.management.list_tasks() if self.dialogue else self.engine.list_tasks()
            for task in visible_tasks:
                if task.get('legacy_only'):continue
                tid=task.get('task_id') or task['id']
                known_tasks.append({**task,'task_id':tid,'contract':self.execution.contract(tid) or {'goal':task['goal']}})
            op.route_versions={t['task_id']:(t.get('revision'),t.get('epoch')) for t in known_tasks}
            repair=''
            research=ResearchTools(self.config,self.data_dir/'network-cache')
            network_sources=[];network_events=[]
            for attempt in range(3):
                self._require_valid(op)
                summary=self._task_summary()
                task_index=_known_task_view(known_tasks,input_target.get('task_id') or self._task_id)
                summary+='\n近期任务索引known_tasks（仅资料，禁止自动合并到本次goal）：'+json.dumps(task_index,ensure_ascii=False)
                summary+='\n本条输入的用户显式归属（优先于模型建议）：'+json.dumps(input_target,ensure_ascii=False)
                from .task_context import task_context
                context_task=input_target.get('task_id') or (self._task_id if input_target.get('mode')=='auto' else None)
                if context_task and self.dialogue is not None:
                    from .tomc_adapter import TomcPreparer
                    prepare=(TomcPreparer.installed() if self._settings.get('task_context_tomc',True) else None)
                    summary+='\n所选任务独立上下文（历史资料，不授予权限；仅回答或续接该任务时使用）：'+json.dumps(
                        task_context(self.memory,self.execution.store,context_task,prepare=prepare),ensure_ascii=False)
                if self.dialogue:
                    visible_ids={item['task_id'] for item in task_index}
                    summary+='\n当前有效的待答问题：'+json.dumps([
                        {'task_id':t['task_id'], 'question':t.get('question'),
                         'constraints':t['contract'].get('constraints',[]),
                         'activity_name':t['contract'].get('activity_name',''),
                         'target_stage_codes':t['contract'].get('target_stage_codes',[]),
                         'import_scope':t['contract'].get('import_scope'),
                         'task_spec':t['contract'].get('task_spec',{})}
                        for t in known_tasks if t.get('question') and t['task_id'] in visible_ids],ensure_ascii=False)
                if repair:
                    summary+='\n上次合同校验未通过（未执行），只修复结构化路由：'+repair
                    summary+='\nremoved_constraints 必须逐字取所选任务索引中的 constraints；已回答待答问题是解决未决事实，不等于撤销用户限制。无须撤销时使用空数组，task_spec.unresolved可在答案已经补全时清空。'
                    summary+='\n本条真实用户原话（user_request必须逐字保留）：'+json.dumps(text,ensure_ascii=False)
                report=self._network_turn(persona,text if attempt==0 else '',op,research=research,
                    summary=summary,route_only=bool(repair),request_id=request_id)
                response=report['response']
                network_sources.extend(s for s in report['sources'] if s not in network_sources)
                network_events.extend(report['events'])
                calls=response.get('tool_calls') or []
                try:
                    if len(calls)!=1 or calls[0].get('function',{}).get('name')!='route_user_request':
                        raise TaskContractError('必须返回一次 route_user_request。')
                    arguments = calls[0]['function'].get('arguments','{}')
                    arguments = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
                    if input_target['mode']=='auto' and arguments.get('relation')=='followup':
                        source=next((t for t in known_tasks if t['task_id']==arguments.get('source_task_id')),None)
                        question=(source or {}).get('question') or {}
                        prior=(source or {}).get('contract') or {}
                        # Completing a genuinely unspecified stage is a question answer,
                        # while changed deliverables and explicit new tasks remain separate.
                        if (question.get('kind')=='business' and question.get('status')=='open'
                                and prior.get('current_deliverable')==arguments.get('current_deliverable')
                                and ((not prior.get('target_stage_codes') and arguments.get('target_stage_codes'))
                                     or ((prior.get('import_scope') or {}).get('kind') in {'ordinal','unspecified'}
                                         and (arguments.get('import_scope') or {}).get('kind') == 'stages'))):
                            arguments.update(relation='modify',selected_task_id=source['task_id'],source_task_id=None)
                    if input_target['mode'] == 'new':
                        arguments.update(relation='new', selected_task_id=None, source_task_id=None)
                        if arguments.get('intent') in {'modify', 'resume'}:
                            arguments['intent'] = 'execute'
                    elif input_target['mode'] == 'chat':
                        arguments.pop('import_scope',None)
                        arguments.update(intent='chat', relation='new', current_deliverable='chat',
                                         selected_task_id=None, source_task_id=None, removed_constraints=[])
                    elif input_target['mode'] == 'task' and arguments.get('intent') in {'execute','modify','resume'}:
                        explicit_source=next((t for t in known_tasks if t['task_id']==input_target['task_id']),None)
                        if (arguments.get('intent')=='execute' and arguments.get('current_deliverable')=='gameplay'
                                and explicit_source and explicit_source.get('status')=='completed'
                                and (explicit_source.get('contract') or {}).get('current_deliverable')=='maa_import'):
                            # The selected import is evidence for this newly
                            # requested run, not a mutable source of authority.
                            arguments.update(selected_task_id=None,source_task_id=input_target['task_id'],
                                             relation='followup')
                        else:
                            arguments.update(selected_task_id=input_target['task_id'], source_task_id=None,
                                relation='continue' if arguments.get('intent')=='resume' else 'modify')
                    elif input_target['mode'] == 'task' and arguments.get('intent') in {'pause','stop','status'}:
                        arguments.update(selected_task_id=input_target['task_id'], source_task_id=None,
                                         relation='modify')
                    contract=parse_task_contract(arguments,text,previous,
                        known_tasks=known_tasks,request_id=request_id,input_mode=input_target['mode'])
                    self._require_valid(op)
                    outcome=self._apply_contract(contract,op)
                    identifier = self.dialogue.routed(request_id, outcome, contract) if self.dialogue else outcome.get('task_id')
                    self.activity_progress.emit(make_activity_event('intent_finished', role='leader',
                        status='failed' if outcome.get('ok') is False else 'completed',
                        summary=outcome.get('reason') or '已核对本条要求：'+(contract.goal or contract.user_request),
                        task_id=identifier, epoch=op.version, request_id=request_id))
                    persona.record_tool_result(calls[0]['id'],outcome)
                    return {'kind':'chat','content':self._network_answer(outcome.get('reply') or contract.reply,
                                {'sources':network_sources}),
                            'network_report':{'sources':network_sources,'events':network_events},
                            'intent_contract':contract.as_dict(), 'task_id':identifier}
                except TaskContractError as exc:
                    repair=str(exc)
                    if len(calls)==1:
                        persona.record_tool_result(calls[0]['id'],{'ok':False,'error_code':'intent_contract_invalid',
                            'error':repair,'executed':False})
                        self.notice.emit('任务意图校验未通过，正在修复：'+repair)
                    elif calls:raise ProviderError('本次意图格式不完整，尚未执行；请重新发送。')
                    else:self.notice.emit('本条回复缺少结构化任务意图，正在补全；尚未执行。')
            raise ProviderError('本次任务意图校验未通过：'+repair+' 已完成两次格式修复，尚未执行。')
        self._start(work,task_bound=False)

    def _apply_contract(self,contract,op):
        self._require_valid(op)
        if contract.intent=='observe':return self._apply_contract_body(contract,op)
        with self._lock:
            self._require_valid(op)
            return self._apply_contract_body(contract,op)

    def _apply_contract_body(self,contract,op):
        self.execution.windows=self.windows
        selected=contract.selected_task_id or self._task_id
        intent=contract.intent
        if intent=='chat':return {'ok':True,'reply':contract.reply}
        self.execution.current_task_id=selected
        if intent=='remember':
            self.knowledge.add_preference('用户明确偏好',contract.user_request)
            return {'ok':True,'reply':'已保存这条偏好；以后执行时会参考，当前明确要求优先。'}
        if intent=='observe':
            if self.execution.active:
                saved=self.runtime.load(self._task_id) or {}
                state=(saved.get('context') or {}).get('model_state') or {}
                summary=state.get('state_summary')
                return {'ok':True,'reply':('执行仍在继续。最近一次画面分析：'+summary) if summary else '执行仍在继续，可从桌宠菜单查看最近画面；尚未取得的新信息不会猜测。'}
            result=self._observe_for_role(op)
            return {**result,'reply':result.get('analysis') or result.get('error','没有取得新的观察。')}
        if intent=='status':
            task=self.engine.get_task(selected) if selected else None
            return {'ok':True,'reply':('当前目标：'+task['goal']+'\n状态：'+{'running':'执行中','paused':'已暂停','needs_input':'等待处理','completed':'已完成','cancelled':'已停止','draft':'已记录'}.get(task['status'],task['status'])) if task else '当前没有任务。'}
        if intent in {'pause','stop'}:
            task=(self.execution.pause(selected) if intent=='pause' else self.execution.stop(selected))
            return {'ok':True,'reply':'已暂停，检查点会保留。' if intent=='pause' else '已停止本地任务。'}
        if intent=='draft':
            self.execution.stop(self._task_id)
            task=self._save_draft(contract.execution_goal())
            self.runtime.save(task['id'],task['version'],phase='draft',contract=contract.as_dict())
            return {'ok':True,'reply':'目标已记录，尚未执行。'}
        try:
            payload=contract.as_dict()
            from .leader_policy import copilot_research_required
            if copilot_research_required(payload):
                payload['match_roster']=not bool(re.search(r'不(?:做|进行)?(?:筛选|匹配|适配)',contract.user_request))
                payload['profile_id']='local-default'
            question = (self.dialogue.management.question(selected) if self.dialogue and selected else None)
            continuing = contract.relation in {'continue','modify'} and question and question['kind']=='business'
            if continuing:
                previous_contract = self.execution.contract(selected)
                if (previous_contract.get('source_task_id')
                        and payload.get('current_deliverable') == previous_contract.get('current_deliverable')
                        and payload.get('activity_name') == previous_contract.get('activity_name')):
                    payload['source_task_id'] = previous_contract['source_task_id']
                    if previous_contract.get('source_revision') is not None:
                        payload['source_revision'] = previous_contract['source_revision']
                outcome=self.execution.continue_contract(selected,payload,request_cancel=op.cancel,
                    expected_state=op.route_versions.get(selected))
            elif intent=='resume' or contract.relation=='continue':
                # Apply any supplied new facts first; explicit resume then releases a user pause.
                if contract.relation == 'continue' and isinstance(self.execution,MaaCoordinator):
                    old=self.execution.store.current(selected)
                    self.execution.store.replace_contract(selected, contract.goal, payload,
                        expected_revision=old['revision'], expected_epoch=old['epoch'])
                outcome=self.execution.resume(selected,request_cancel=op.cancel)
            else:
                outcome=self.execution.submit(contract.execution_goal(),payload,modify=intent=='modify',request_cancel=op.cancel)
            self._task_id=self.execution.current_task_id
            self.changed.emit()
            spec=contract.as_dict()['task_spec']
            details=[]
            if spec.get('stage_code'):details.append('关卡：'+spec['stage_code'])
            if spec.get('stop_after_current'):details.append('停止条件：当前局结束后停止，不开始下一局')
            elif spec.get('repeat_count') is not None:details.append('次数：'+str(spec['repeat_count']))
            if spec.get('medicine_limit') is not None:details.append('理智药上限：'+str(spec['medicine_limit']))
            if spec.get('premium_limit') is not None:details.append('源石上限：'+str(spec['premium_limit']))
            reply='当前目标：'+contract.goal
            if details:reply+='\n'+'；'.join(details)
            if contract.constraints:reply+='\n约束：'+'；'.join(contract.constraints)
            if outcome.get('phase') in {'paused','blocked'}:
                reply+='\n'+outcome.get('reason','补充已保存，任务仍暂停；需要明确继续或先处理阻塞条件。')
            else:
                reply+='\n已进入任务调度；实际执行结果会另外核实。'
            return {**outcome,'reply':reply}
        except (ProviderError,WindowError,ActionRejected) as exc:
            if intent=='execute' and isinstance(self.execution, ExecutionCoordinator):
                task=self._save_draft(contract.execution_goal())
                self.execution.current_task_id=task['id']
                self.runtime.save(task['id'],task['version'],phase='draft',contract=contract.as_dict())
            return {'ok':False,'reply':self._safe_error(exc)}

    @Slot(bool)
    def _execution_activity(self,active):
        if self._closed:return
        self._executing=bool(active);self.execution_changed.emit(active);self.changed.emit()

    @Slot()
    def _execution_changed(self):
        if self._closed:return
        if self.execution.current_task_id:self._task_id=self.execution.current_task_id
        self.changed.emit()

    @Slot(str,str,dict)
    def _execution_event(self,kind,message,details):
        if self._closed:return
        try:
            task=self.engine.get_task(details.get('task_id'))
            if self.dialogue:self.dialogue.management.require_active(task['id'])
        except (ActionRejected,KeyError,ValueError):return
        if task['version']!=details.get('goal_version'):return
        progress = make_activity_event(kind, details)
        if progress is not None:
            self.activity_progress.emit(progress)
        if kind=='observation':
            self._latest_observation=copy.deepcopy(details);self.observation_ready.emit(copy.deepcopy(details))
        elif kind=='foreground_required':self._emit_message('system',message)
        self.notice.emit(message)

    @Slot(dict)
    def _execution_finished(self,result):
        if self._closed:return
        try:
            task=self.engine.get_task(result.get('task_id'))
            if self.dialogue:self.dialogue.management.require_active(task['id'])
        except (ActionRejected,KeyError,ValueError):return
        if result.get('status')=='cancelled' or result.get('epoch',task['version'])!=task['version']:return
        summary=result.get('summary') or '本次执行已结束。'
        self._message_request_id=result.get('request_id')
        self._message_task_id=task['id']
        try:
            self._emit_message('assistant',summary)
        finally:
            self._message_request_id=self._message_task_id=None
        if result.get('model_goal_verified'):
            evidence=result.get('completion_evidence') or {}
            self.knowledge.add_experience(task['goal'],summary,evidence_id=evidence.get('observation_id',''),verified=False)
        self.changed.emit()
        if self.dialogue:QTimer.singleShot(0,self._recover_messages)

    def _save_draft(self, goal: str) -> dict:
        self.windows.set_stop()
        if not self.legacy_tools and hasattr(self,'execution'):
            self.execution.stop(self._task_id)
        current = self.current_task()
        if current and current["status"] not in TERMINAL_STATUSES:
            self.engine.stop_task(current["id"])
        task = self.engine.submit_goal(goal)
        self._task_id, self._latest_observation = task["id"], None
        if not self.legacy_tools and hasattr(self,'execution'):
            self.execution.current_task_id=task['id']
        return task

    def _task_summary(self) -> str:
        task = self.current_task()
        facts = {"task": task, "game_input_allowed":self.game_input_allowed,
                 "mode": "有界视觉执行" if self.game_input_allowed else "只读观察",
                 "screen_capture_allowed": self.screen_capture_allowed,
                 "window_bound": self.windows.bound_window() is not None,
                 "interface_execution_available":True,
                 "execution_entry":"execute_game_task 会先获取新截图再操作；无需预先已有截图或已验证语义",
                 "autonomous_battle_validated":False}
        if not self.legacy_tools:
            facts['assistant_role']='Arknights MAA supervisor'
            facts['mode']='理解完整目标并动态统筹 MAA 与资料工具'
            facts['execution_entry']='route_user_request 保留用户原始目标、关卡、次数及资源边界；Leader 根据现有事实决定研究、MAA 或诊断。能力限制不能用来缩减用户目标或冒充完成。'
            facts['capabilities']=runtime_capabilities(self.screen_capture_allowed,self.game_input_allowed)
            facts['requested_goal']=task['goal'] if task else None
            facts['execution_active']=self.execution.active if hasattr(self,'execution') else False
            facts['task_contract']=self.execution.contract(self._task_id) if hasattr(self,'execution') and self._task_id else {}
            from .battle_contract import authorized as battle_authorized
            facts['battle_start_allowed']=(self.screen_capture_allowed is True
                and self.game_input_allowed is True and battle_authorized(facts['task_contract']))
            facts['battle_start_authority']='current_task_only'
            facts['user_preferences']=self.knowledge.preferences(top_k=6,char_budget=1200,token_budget=900) if hasattr(self,'knowledge') else []
            if self._task_id and callable(getattr(self.execution,'research_output',None)):
                from .leader_plan import PlanStore
                plan=PlanStore(self.execution.store).current(self._task_id) if hasattr(self.execution,'store') else None
                if plan:
                    facts['registered_job_output']={'task_id':self._task_id,'plan_revision':plan['revision'],
                        'total_stages':len(plan['steps']),
                        'missing':[s['display_code'] for s in plan['stages'] if any(
                            item['stage_key']==s['stage_key'] and item['status'] not in {'two_verified','primary_only'} for item in plan['steps'])],
                        'note':'完整代码和凭据保存在本地；重列由已登记产物读取，不从旧聊天复制。'}
                else:
                    facts['registered_job_output']={'task_id':self._task_id,'plan_registered':False}
        return json.dumps(facts, ensure_ascii=False)

    def _execute_role_tool(self, op: _Operation, call: dict) -> dict:
        try:
            function = call["function"]
            args = json.loads(function.get("arguments", "{}"))
            name = function["name"]
            if not isinstance(args, dict):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "工具参数格式不正确，未执行。"}
        if name == "observe_game":
            return self._observe_for_role(op)
        if name=='execute_game_task':
            return self._execute_game_task(op,args.get('goal'))
        with self._lock:
            self._require_valid(op)
            task = self.current_task()
            if name in {"request_game_task", "change_game_goal"}:
                goal = args.get("goal")
                if not isinstance(goal, str) or not goal.strip() or len(goal) > 16000:
                    return {"ok": False, "error": "目标需要是有效文本。"}
                if name == "request_game_task":
                    task = self._save_draft(goal)
                elif not task or task["status"] in TERMINAL_STATUSES:
                    return {"ok": False, "error": "没有可修改的未结束任务，请新建目标。"}
                else:
                    self.windows.set_stop()
                    task = self.engine.update_goal(task["id"], goal)
                    self._latest_observation = None
                self._bind_operation_task(op, task)
            elif name == "get_task_status":
                pass
            elif name in {"pause_task", "resume_task", "stop_task"}:
                if name != "resume_task":
                    self.windows.set_stop()
                if not task:
                    return {"ok": False, "error": "当前没有任务。"}
                try:
                    task = {"pause_task": self.engine.pause_task, "resume_task": self.engine.resume_task,
                            "stop_task": self.engine.stop_task}[name](task["id"])
                except ActionRejected as exc:
                    return {"ok": False, "error": str(exc)}
                if name == "resume_task":
                    self.windows.reset_stop()
                self._latest_observation = None
                self._bind_operation_task(op, task)
            else:
                return {"ok": False, "error": "没有授权该工具；未执行。"}
            self.changed.emit()
            return {"ok": True, "task": task, "executed_game_action": False,
                    "game_input_allowed":self.game_input_allowed,"note": "任务管理结果，不能据此声称游戏已执行。"}

    def _observe_for_role(self, op: _Operation) -> dict:
        """Role tool: real pixels -> separate game session -> factual result.

        Capture and model latency never hold the controller lock, so a local
        stop or permission revocation can invalidate the entire chain.
        """
        try:
            with self._lock:
                self._require_valid(op)
                self._require_capture_permission()
                self._latest_observation = None
                game = self.game
            raw = self._capture_frame(op)
            with self._lock:
                observation = self._accept_observation(op, raw)
                task = self.current_task()
                goal = task["goal"] if task else "观察并描述当前游戏画面，说明不确定性，只提出一个有界建议"
            plan = self._analyze_step(op, game, goal, observation)
            with self._lock:
                self._accept_plan(op, plan)
                self.changed.emit()
            return {"ok": True, "observation_id": observation["id"],
                    "analysis": plan["summary"], "game_state": "unknown",
                    "executed_game_action": False, "input_enabled": False}
        except ProviderCancelled:
            raise
        except (ProviderError, WindowError, ActionRejected, ValueError) as exc:
            return {"ok": False, "error": self._safe_error(exc),
                    "executed_game_action": False, "input_enabled": False}

    def bind_window(self, hwnd: int):
        if not self.legacy_tools:self.execution.stop(self._task_id)
        with self._lock:
            self.windows.set_stop()
            self._cancel_pending()
            self._latest_observation = None
            result = self.windows.bind(hwnd)
            self.windows.reset_stop()
            self._invalidate_task("游戏窗口绑定已变化，需要新的观察")
            self.changed.emit()
            return result

    def capture(self) -> bool:
        with self._lock:
            self._latest_observation = None
            try:
                self._require_capture_permission()
            except ProviderError as exc:
                self.error.emit(str(exc))
                return False
        def work(op: _Operation) -> dict:
            observation = self._capture_frame(op)
            return {"kind": "capture", "observation": observation}
        return self._start(work)

    def _capture_frame(self, op: _Operation) -> dict:
        with self._lock:
            self._require_valid(op)
            self._require_capture_permission()
            needs_binding=self.windows.bound_window() is None
        if needs_binding:
            # Native title queries can synchronously contact a window thread.
            # Never hold the controller state lock across desktop enumeration.
            candidates=self.windows.list_windows(game_only=True)
            clients=[w for w in candidates if getattr(w,'is_game_client',False)]
            candidates=clients or [w for w in candidates if w.title.strip()=='明日方舟']
            if len(candidates)!=1:
                raise ProviderError("请选择唯一的明日方舟游戏窗口。可从桌宠的屏幕设置中绑定。")
            with self._lock:
                self._require_valid(op)
                self._require_capture_permission()
                if self.windows.bound_window() is None:
                    self.windows.bind(candidates[0].hwnd)
                    self.changed.emit()
        # HWND-bound WGC reads the target surface. Do not activate the game or
        # silently substitute a crop of the foreground desktop.
        with self._lock:
            self._require_valid(op)
            self._require_capture_permission()
        return self.windows.capture(background_only=True)

    def _validate_observation(self, observation: dict | None) -> None:
        if not observation:
            raise ProviderError("请先获取当前游戏窗口的真实截图。")
        try:
            age = time.time() - float(observation["captured_at"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ProviderError("截图时间无效，请重新观察。") from None
        if not math.isfinite(age) or age < -1 or age > self.ANALYSIS_MAX_AGE_SECONDS:
            raise ProviderError("截图已过期，请重新观察后再分析。")
        window = self.windows.bound_window()
        if (window is None or observation.get("window_id") != window.hwnd
                or (observation.get("window_pid") is not None and observation["window_pid"] != window.pid)):
            raise ProviderError("游戏窗口身份已改变，请重新绑定并观察。")
        if (getattr(window, "minimized", False)
                or any(getattr(window, key, observation.get(key)) != observation.get(key)
                       for key in ("width", "height"))):
            raise ProviderError("游戏窗口尺寸或状态已改变，请重新观察。")

    def analyze_observation(self) -> None:
        with self._lock:
            if not self.has_key:
                self.error.emit("尚未填写 DeepSeek Key，无法分析真实观察。")
                return
            try:
                self._require_capture_permission()
                self._validate_observation(self._latest_observation)
            except (ProviderError, WindowError) as exc:
                self._latest_observation = None
                self.error.emit(self._safe_error(exc))
                return
            observation = copy.deepcopy(self._latest_observation)
            task = self.current_task()
            if (observation.get("task_id"), observation.get("goal_version")) != (
                    (task["id"], task["version"]) if task else (None, None)):
                self.error.emit("目标已经变化，请先重新观察。")
                return
            goal = task["goal"] if task else "仅观察当前画面，描述不确定性并建议一个有界步骤"
            game = self.game

        def work(op: _Operation) -> dict:
            return {"kind": "analysis", "plan": self._analyze_step(op, game, goal, observation)}

        self._start(work)

    def _analyze_step(self, op: _Operation, game: GameSession, goal: str, observation: dict) -> dict:
        response = game.decide(goal, observation, GAME_TOOLS, history=[], cancel_event=op.cancel)
        with self._lock:
            self._require_valid(op)
            self._require_capture_permission()
        suggestions = []
        for call in response.get("tool_calls") or []:
            function = call.get("function", {})
            if function.get("name") != "propose_next_step":
                raise ProviderError("模型请求了未授权的游戏工具，未执行。")
            try:
                args = json.loads(function.get("arguments", "{}"))
                if not isinstance(args, dict) or not all(isinstance(args.get(k), str) and args[k].strip()
                                                       for k in ("action", "reason", "expected_result")):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ProviderError("模型建议格式不正确；未执行任何操作。") from None
            suggestions.append({key: args[key][:4000] for key in ("action", "reason", "expected_result")})
            game.record_tool_result(call["id"], {"accepted_as_proposal": True, "executed": False})
        parts = ["模型观察与建议 · 尚未执行，语义待验证"]
        if response.get("content"):
            parts.append(_plain_text(response["content"])[:12000])
        for suggestion in suggestions[:1]:
            parts.append(f"建议：{suggestion['action']}\n依据：{suggestion['reason']}\n待验证：{suggestion['expected_result']}")
        if len(parts) == 1:
            parts.append("模型未返回明确建议，请重新观察或补充目标。")
        return {"summary": "\n\n".join(parts), "proposals": suggestions[:1],
                "observation_id": observation["id"], "goal_version": op.version,
                "executed": False, "game_state": "unknown"}

    def pause(self) -> None:
        if not self.legacy_tools:
            with self._lock:
                # A late intent parser must not create a new job after Pause.
                self._cancel_pending()
                self._latest_observation = None
            try:self.execution.pause(self._task_id);self.notice.emit('已暂停，检查点已保留。')
            except (ActionRejected,ProviderError) as exc:self.error.emit(self._safe_error(exc))
            return
        with self._lock:
            self.windows.set_stop()
            self._cancel_pending()
            self._latest_observation = None
            task = self.current_task()
            try:
                if task:
                    self.engine.pause_task(task["id"])
                self.notice.emit("已暂停本地任务，旧动作已失效。")
            except ActionRejected as exc:
                self.error.emit(str(exc))
            self.changed.emit()

    def resume(self) -> None:
        if not self.legacy_tools:
            try:self.execution.resume(self._task_id);self.notice.emit('正在继续同一 MAA 目标，先核实当前界面。')
            except (ActionRejected,ProviderError) as exc:self.error.emit(self._safe_error(exc))
            return
        with self._lock:
            self._cancel_pending()
            self._latest_observation = None
            task = self.current_task()
            try:
                if task:
                    self.engine.resume_task(task["id"])
                    self.windows.reset_stop()
                    self.notice.emit("已恢复为等待观察；输入仍关闭，请重新截图。")
                else:
                    self.notice.emit("当前没有可继续的任务。")
            except ActionRejected as exc:
                self.error.emit(str(exc))
            self.changed.emit()

    def stop(self, silent: bool = False) -> None:
        # WindowController.set_stop does not wait for the capture lock.
        self.windows.set_stop()
        active=self._active
        if active:active.cancel.set()
        if hasattr(self,'execution') and not self.legacy_tools:self.execution.stop(self._task_id)
        with self._lock:
            self._cancel_pending()
            self._latest_observation = None
            task = self.current_task()
            if task and self.legacy_tools:
                self.engine.stop_task(task["id"])
            if not silent:
                self.notice.emit("已停止本地任务；晚到模型回复和旧观察将被丢弃。" if self.legacy_tools else
                                 "本地写入权限已撤销；MAA 停止状态将独立核实。")
            self.changed.emit()

    def set_game_input_permission(self,allowed:bool)->None:
        if not isinstance(allowed,bool):return
        if not allowed:
            self.windows.set_stop()
            if not self.legacy_tools:self.execution.pause(self._task_id)
        with self._lock:
            self.game_input_allowed=allowed
            if not allowed:self._cancel_pending()
            if self.legacy_tools:
                message = ('本次启动已允许游戏鼠标操作。任务执行时会切到游戏，可随时停止。' if allowed else '游戏操作授权已关闭。')
            else:
                message = ('本次启动已允许黍截图并操作 MAA 客户端；当前阶段仅导入作业，不开始作战。' if allowed else '本次 MAA 输入权限已关闭。')
            self.notice.emit(message)
            self.changed.emit()

    def _execute_game_task(self,op:_Operation,goal:Any)->dict:
        with self._lock:
            self._require_valid(op)
            if op.execution_started:
                return {'ok':False,'error':'本条请求已运行过执行循环，请依据返回结果答复；需要继续时由用户再发送请求。'}
            if not isinstance(goal,str) or not goal.strip() or len(goal)>16000:
                return {'ok':False,'error':'请提供完整有效的游戏目标。'}
            if not self.game_input_allowed:
                return {'ok':False,'error':'请从桌宠「设置 → 屏幕与游戏操作」开启本次启动的游戏操作授权，然后再告诉我执行目标。','executed_game_action':False}
            self._require_capture_permission()
            op.execution_started=True
            task=self._save_draft(goal)
            task=self.engine.set_status(task['id'],'running','开始依据真实画面执行目标',goal_version=task['version'])
            self._bind_operation_task(op,task)
            self.windows.reset_stop();self.windows.set_input_enabled(True)
            self._executing=True;self._game_execution_op=op;self.changed.emit()
        def guard():
            with self._lock:
                self._require_valid(op)
                self._require_capture_permission()
                if not self.game_input_allowed:raise ProviderCancelled('游戏操作授权已关闭。')
        def event(kind,message,details):
            with self._lock:
                guard()
                self.engine.record_event(task['id'],kind,message,details,goal_version=op.version)
                self.notice.emit(message)
                if kind=='foreground_required':self._emit_message('system',message)
                if kind=='observation' and isinstance(details,dict):
                    self._accept_observation(op,details)
                self.changed.emit()
        game=GameSession(self.game_client)
        try:
            # Ensure an unambiguous actual client is bound before entering the loop.
            self._capture_frame(op)
            if hasattr(self.windows,'require_input_permission'):
                self.windows.require_input_permission()
            with self._lock:
                guard()
                # Binding intentionally disables input; authorize only after it.
                self.windows.reset_stop();self.windows.set_input_enabled(True)
            executor=GameExecutor(self.windows,game.decide,guard,emit_event=event)
            result=executor.run(goal,op.cancel,max_steps=self.execution_max_steps,max_seconds=self.execution_max_seconds)
            with self._lock:
                guard()
                status=result.get('status','needs_input')
                if status not in {'completed','needs_input','failed'}:status='needs_input'
                if status=='completed':
                    evidence=result.get('completion_evidence') or {}
                    if result.get('model_goal_verified') is not True or evidence.get('source')!='model_visual_check':
                        raise ActionRejected('执行结果缺少画面检查依据。')
                    self.engine.complete_visual_goal(task['id'],goal_version=op.version,
                        observation_id=evidence.get('observation_id'),evidence=evidence.get('text'),
                        message=result.get('summary','目标画面已检查'))
                else:
                    self.engine.set_status(task['id'],status,result.get('summary','执行已结束'),goal_version=op.version)
                self._bind_operation_task(op,self.current_task())
                return {'ok':status=='completed',**result,'completed_runs':self.current_task()['completed_runs'],
                        'note':'完成结论来自执行后画面的模型检查；没有据此增加作战次数。'}
        except (ProviderError,WindowError,ActionRejected) as exc:
            with self._lock:
                self._require_valid(op)
                self.engine.set_status(task['id'],'needs_input','执行需要处理：'+self._safe_error(exc),goal_version=op.version)
                self._bind_operation_task(op,self.current_task())
            return {'ok':False,'error':self._safe_error(exc)}
        finally:
            with self._lock:
                if self._game_execution_op is op:
                    self.windows.set_stop();self._executing=False;self._game_execution_op=None
                self.changed.emit()

    def _invalidate_task(self, message: str) -> None:
        task = self.current_task()
        if task and task["status"] not in TERMINAL_STATUSES | {"paused"}:
            if not self.legacy_tools:self.execution.pause(task['id'])
            else:self.engine.set_status(task["id"], "needs_input", message, goal_version=task["version"])

    @staticmethod
    def _validate_settings(model: str, base_url: str) -> None:
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_./:\-]{1,128}", model):
            raise ProviderError("模型名称格式不正确。")
        if not isinstance(base_url, str):
            raise ProviderError("API 地址格式不正确。")
        try:
            parts = urlsplit(base_url)
            parts.port
            if (not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
                    or any(c.isspace() for c in base_url)
                    or (parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}))):
                raise ValueError()
        except ValueError:
            raise ProviderError("API 地址应为有效 HTTPS 地址；本机测试可用 HTTP。") from None

    def _write_settings(self, updates: dict) -> None:
        """Merge non-secret preferences rather than erasing unrelated settings."""
        saved = dict(self._settings)
        try:
            current = json.loads((self.data_dir / "settings.json").read_text(encoding="utf-8"))
            if isinstance(current, dict):
                saved.update(current)
        except (OSError, ValueError, TypeError):
            pass
        saved.update(updates)
        # Secrets belong exclusively to the external text file/environment.
        for name in ("api_key", "game_api_key", "DEEPSEEK_API_KEY"):
            saved.pop(name, None)
        temporary = self.data_dir / "settings.json.tmp"
        temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            temporary.replace(self.data_dir / "settings.json")
        except PermissionError:
            # Some Windows sandbox-backed directories permit file writes but
            # reject rename/replace. Preserve the merged content in that case.
            (self.data_dir / "settings.json").write_text(
                json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.unlink(missing_ok=True)
        self._settings = saved

    def set_capture_permission(self, allowed: bool) -> None:
        if not isinstance(allowed, bool):
            self.error.emit("截图权限必须为开启或关闭。")
            return
        try:
            if not allowed:
                # Stop the local input gate before touching storage or model state.
                self.windows.set_stop()
            with self._lock:
                self._write_settings({"screen_capture_allowed": allowed})
                self.screen_capture_allowed = allowed
                if not allowed:
                    self._cancel_pending()
                    self._latest_observation = None
                    self._invalidate_task("屏幕读取权限已关闭，需要重新授权与观察")
                self.notice.emit("已允许读取绑定游戏窗口；尚未允许游戏输入。" if allowed
                                 else "屏幕读取权限已关闭，旧观察与待处理回复已失效。")
                self.changed.emit()
        except OSError:
            # Revocation is effective even if its preference cannot be persisted.
            if not allowed:
                with self._lock:
                    self.screen_capture_allowed = False
                    self._cancel_pending()
                    self._latest_observation = None
                    self.changed.emit()
            self.error.emit("截图权限设置未能保存，请检查本地数据目录。")

    def save_settings(self, model: str, base_url: str, game_model: str | None = None,
                      game_thinking: bool | None = None, *,
                      intent_thinking: bool | None = None,
                      leader_thinking: bool | None = None) -> None:
        try:
            self._validate_settings(model, base_url)
            selected_game_model = self.game_config.model if game_model is None else game_model
            selected_game_thinking = self.game_config.thinking if game_thinking is None else game_thinking
            selected_intent_thinking = self.intent_config.thinking if intent_thinking is None else intent_thinking
            selected_leader_thinking = self.leader_config.thinking if leader_thinking is None else leader_thinking
            self._validate_settings(selected_game_model, base_url)
            if not isinstance(selected_game_thinking, bool):
                raise ProviderError("游戏思考模式必须为开启或关闭。")
            if not isinstance(selected_intent_thinking, bool):
                raise ProviderError("任务理解思考模式必须为开启或关闭。")
            if not isinstance(selected_leader_thinking, bool):
                raise ProviderError("Leader 思考模式必须为开启或关闭。")
            with self._lock:
                self._write_settings({"model": model, "base_url": base_url,
                                      "game_model": selected_game_model, "game_thinking": selected_game_thinking,
                                      "intent_thinking": selected_intent_thinking,
                                      "leader_thinking": selected_leader_thinking})
                self._cancel_pending()
                self.windows.set_stop()
                self.config = replace(self.config, model=model, base_url=base_url, api_key=None, thinking=False)
                self.game_config = replace(self.game_config, model=selected_game_model, base_url=base_url,
                                           api_key=None, thinking=selected_game_thinking)
                self.connection_verified = False
                self.game_connection_verified = False
                self._latest_observation = None
                self._invalidate_task("模型连接配置已更新，需要新的观察")
                self._rebuild_sessions()
                self.notice.emit("设置已保存；Key 仅从本地文本读取，请测试模型连接。")
                self.changed.emit()
        except (ProviderError, OSError) as exc:
            self.error.emit(self._safe_error(exc) if isinstance(exc, ProviderError) else "设置无法保存，请检查本地数据目录。")

    def test_connection(self) -> None:
        if not self.has_key:
            self.error.emit("尚未填写 DeepSeek Key，请保存文本后重新读取。")
            return
        config = replace(self.config, max_tokens=64, thinking=False)

        def work(op: _Operation) -> dict:
            probe = DeepSeekClient(config)
            probe.complete([{"role": "user", "content": "只回复 OK"}], cancel_event=op.cancel)
            return {"kind": "connection"}
        self._start(work)

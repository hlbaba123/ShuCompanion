"""Controller integration without a live API, native window, or paid request."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QImage,QColor

from shu.controller import SessionController
from shu.network_dialogue import WEB_TOOLS
from shu.providers import GameSession, PersonaSession
from shu.windows import WindowError


TEST_KEY = "sk-controller-test-fixture"
APP = QCoreApplication.instance() or QCoreApplication([])


def assistant(text):
    return {"role": "assistant", "content": text}


def tool(name, args=None, call_id="call-1"):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function":
            {"name": name, "arguments": json.dumps(args or {}, ensure_ascii=False)}}]}


class Model:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, tools=None, cancel_event=None, tool_choice=None):
        self.calls.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools),
                           "tool_choice": tool_choice,
                           "thread": threading.get_ident()})
        if not self.responses:
            raise AssertionError("Unexpected model request")
        return copy.deepcopy(self.responses.pop(0))


class BlockingModel(Model):
    def __init__(self, response):
        super().__init__(response)
        self.entered, self.release, self.returned = threading.Event(), threading.Event(), threading.Event()

    def complete(self, messages, tools=None, cancel_event=None, tool_choice=None):
        self.entered.set()
        self.release.wait(4)
        result = super().complete(messages, tools, cancel_event, tool_choice)
        self.returned.set()
        return result


class Windows:
    def __init__(self, directory):
        self.directory = directory
        self.stop_count = 0
        self.reset_count = 0
        self.input_enabled = False
        self.bound = SimpleNamespace(hwnd=55, pid=66, title="测试游戏")
        self.capture_count = 0
        self.capture_thread = None
        self.capture_background_flags = []

    def set_stop(self):
        self.stop_count += 1
        self.input_enabled = False

    def reset_stop(self):
        self.reset_count += 1
        self.input_enabled = False

    def set_input_enabled(self,enabled):
        self.input_enabled=bool(enabled)

    def bind(self, hwnd):
        self.bound.hwnd = hwnd
        return self.bound

    def bound_window(self):
        return self.bound

    def capture(self, *, background_only=False):
        self.capture_background_flags.append(background_only)
        if background_only is not True:
            raise AssertionError("Controller must require target-window background capture")
        self.capture_count += 1
        self.capture_thread = threading.get_ident()
        path = self.directory / f"frame-{self.capture_count}.png"
        fixture=QImage(20,30,QImage.Format.Format_RGB32);fixture.fill(QColor('#375a42'));fixture.save(str(path))
        return {"id": f"obs-{self.capture_count}", "captured_at": time.time(), "window_id": self.bound.hwnd,
                "window_pid": self.bound.pid,
                "path": str(path), "width": 20, "height": 30, "method": "mock",
                "state": "victory", "warnings": []}


class BlockingWindows(Windows):
    def __init__(self, directory):
        super().__init__(directory)
        self.entered, self.release, self.returned = threading.Event(), threading.Event(), threading.Event()

    def capture(self, *, background_only=False):
        self.entered.set()
        try:
            self.release.wait(4)
            return super().capture(background_only=background_only)
        finally:
            self.returned.set()


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.controller = SessionController(self.root, self.root / "data",legacy_tools=True)
        self.controller.windows = Windows(self.root)
        self.addCleanup(self.controller.close)
        self.messages, self.plans, self.observations, self.errors, self.notices = [], [], [], [], []
        self.controller.message.connect(lambda role, text: self.messages.append((role, text)))
        self.controller.plan_ready.connect(self.plans.append)
        self.controller.observation_ready.connect(self.observations.append)
        self.controller.error.connect(self.errors.append)
        self.controller.notice.connect(self.notices.append)

    def keyed(self):
        self.controller.key_path.write_text(TEST_KEY, encoding="utf-8")

    def install_model(self, model):
        self.controller.client = model
        self.controller.game_client = model
        self.controller.persona = PersonaSession(model)
        self.controller.game = GameSession(model)
        return model

    def wait_for(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(0.003)
        APP.processEvents()
        self.assertTrue(condition(), "Qt worker did not complete within the test bound")

    def idle(self):
        self.wait_for(lambda: not self.controller.is_busy)

    def capture(self):
        self.controller.set_capture_permission(True)
        self.assertTrue(self.controller.capture())
        self.idle()

    def draft(self, goal="刷六次，不使用理智药"):
        self.controller.submit(goal, 2)
        return self.controller.current_task()

    def test_execution_requires_session_permission_and_never_opens_input_implicitly(self):
        self.keyed()
        self.controller.set_capture_permission(True)
        self.install_model(Model(tool('execute_game_task', {'goal':'进入基建'}),assistant('需要本次操作授权。')))
        with patch('shu.controller.GameExecutor') as executor:
            self.controller.submit('现在进入基建')
            self.idle()
            executor.assert_not_called()
        self.assertFalse(self.controller.windows.input_enabled)
        self.assertIsNone(self.controller.current_task())

    def test_execution_records_new_frames_and_visual_completion_without_battle_count(self):
        self.keyed()
        self.controller.set_capture_permission(True)
        self.controller.set_game_input_permission(True)
        self.install_model(Model(tool('execute_game_task', {'goal':'进入基建'}),assistant('已检查基建页面。')))
        owner=self
        class Executor:
            def __init__(self,windows,decide,guard,emit_event):
                self.windows,self.guard,self.emit=windows,guard,emit_event
            def run(self,goal,cancel_event,**limits):
                owner.assertTrue(self.windows.input_enabled)
                self.guard()
                observation=self.windows.capture(background_only=True)
                self.emit('observation','新画面',observation)
                return {'status':'completed','summary':'已检查基建页面','model_goal_verified':True,
                        'completion_evidence':{'observation_id':observation['id'],'text':'基建标题可见','source':'model_visual_check'},
                        'actions':[{'kind':'click','verified':True}]}
        with patch('shu.controller.GameExecutor',Executor):
            self.controller.submit('现在进入基建')
            self.idle()
        task=self.controller.current_task()
        self.assertEqual(task['status'],'completed')
        self.assertEqual(task['completed_runs'],0)
        self.assertEqual(len(self.observations),1)
        self.assertFalse(self.controller.windows.input_enabled)
        self.assertTrue(any(event['type']=='visual_goal_checked' for event in self.controller.engine.events(task['id'])))
        self.assertEqual(self.errors,[])

    def test_autobind_reauthorizes_input_only_after_binding_clears_it(self):
        self.keyed();self.controller.set_capture_permission(True);self.controller.set_game_input_permission(True)
        candidate=SimpleNamespace(hwnd=55,pid=66,title='明日方舟',is_game_client=True)
        self.controller.windows.bound=None
        self.controller.windows.list_windows=lambda **kwargs:[candidate]
        def bind(hwnd):
            self.controller.windows.bound=candidate
            self.controller.windows.input_enabled=False
            return candidate
        self.controller.windows.bind=bind
        self.install_model(Model(tool('execute_game_task',{'goal':'进入基建'}),assistant('已暂停。')))
        owner=self
        class Executor:
            def __init__(self,windows,*args,**kwargs):self.windows=windows
            def run(self,*args,**kwargs):
                owner.assertTrue(self.windows.input_enabled)
                return {'status':'needs_input','summary':'测试暂停'}
        with patch('shu.controller.GameExecutor',Executor):
            self.controller.submit('现在进入基建');self.idle()
        self.assertEqual(self.errors,[])
        self.assertFalse(self.controller.windows.input_enabled)

    def test_blocked_desktop_enumeration_does_not_block_local_stop(self):
        entered,release,stopped=threading.Event(),threading.Event(),threading.Event()
        self.controller.set_capture_permission(True);self.controller.windows.bound=None
        def enumerate_windows(**kwargs):entered.set();release.wait(3);return []
        self.controller.windows.list_windows=enumerate_windows
        self.controller.capture();self.wait_for(entered.is_set)
        def stop():self.controller.stop(silent=True);stopped.set()
        worker=threading.Thread(target=stop);worker.start()
        try:self.wait_for(stopped.is_set,timeout=.5)
        finally:release.set();worker.join(2)
        self.assertFalse(self.controller.windows.input_enabled)

    def test_no_key_saves_explicit_draft_without_fake_ai_progress(self):
        model = self.install_model(Model())
        self.controller.submit("刷六次，不使用理智药", 0)
        task = self.controller.current_task()
        self.assertEqual((task["status"], task["completed_runs"]), ("draft", 0))
        self.assertEqual(model.calls, [])
        self.assertFalse(self.controller.connection_verified)
        self.assertTrue(any("没有调用模型" in text for role, text in self.messages if role == "system"))
        self.assertFalse(any(role == "assistant" for role, text in self.messages))

    def test_pure_chat_without_key_does_not_create_game_task(self):
        self.controller.submit("你好", 1)
        self.assertIsNone(self.controller.current_task())
        self.assertEqual(self.messages[0], ("user", "你好"))
        self.assertIn("无法调用角色模型", self.messages[-1][1])

    def test_explicit_draft_never_calls_model_and_replaces_unfinished_goal(self):
        self.keyed()
        model = self.install_model(Model())
        original = self.draft("旧目标")
        self.draft("新目标，不用源石")
        self.assertEqual(model.calls, [])
        self.assertEqual(self.controller.engine.get_task(original["id"])["status"], "cancelled")
        self.assertEqual(self.controller.current_task()["goal"], "新目标，不用源石")

    def test_chat_calls_model_on_worker_and_does_not_authorize_task_tools(self):
        self.keyed()
        model = self.install_model(Model(assistant("我在，慢慢说。")))
        self.controller.submit("今天有点累", 1)
        self.idle()
        self.assertIn(("assistant", "我在，慢慢说。"), self.messages)
        self.assertEqual(model.calls[0]["tools"], WEB_TOOLS)
        self.assertEqual(model.calls[0]["tool_choice"], 'auto')
        self.assertNotEqual(model.calls[0]["thread"], threading.get_ident())
        self.assertIsNone(self.controller.current_task())

    def test_auto_role_tool_submits_real_draft_and_returns_actual_result(self):
        self.keyed()
        model = self.install_model(Model(tool("request_game_task", {"goal": "刷六次，不使用理智药"}),
                                         assistant("目标已保存，等你获取画面。")))
        self.controller.submit("帮我刷六次，不吃药", 0)
        self.idle()
        task = self.controller.current_task()
        self.assertEqual(task["goal"], "刷六次，不使用理智药")
        self.assertEqual((task["status"], task["completed_runs"]), ("draft", 0))
        returned = json.loads(model.calls[1]["messages"][-2]["content"])
        # The transient verified summary follows the role=tool result.
        self.assertTrue(returned["ok"])
        self.assertFalse(returned["executed_game_action"])
        self.assertEqual(returned["task"]["id"], task["id"])
        self.assertTrue(any(event["type"] == "goal_created" for event in self.controller.engine.events(task["id"])))

    def test_role_change_goal_invalidates_prior_observation(self):
        self.keyed()
        original = self.draft("刷十次，不用源石")
        self.capture()
        self.install_model(Model(tool("change_game_goal", {"goal": "刷三次，不用源石"}), assistant("目标已更新。")))
        self.controller.submit("改成三次", 0)
        self.idle()
        task = self.controller.current_task()
        self.assertEqual(task["id"], original["id"])
        self.assertGreater(task["version"], original["version"])
        self.assertEqual(task["goal"], "刷三次，不用源石")
        self.assertIsNone(task["latest_observation_id"])
        self.assertIsNone(self.controller._latest_observation)

    def test_unknown_role_tool_does_not_reach_window_input(self):
        self.keyed()
        model = self.install_model(Model(tool("controlled_click", {"x": 5, "y": 6}), assistant("无法执行该工具。")))
        self.controller.submit("测试工具守卫", 0)
        self.idle()
        returned = next(m for m in model.calls[1]["messages"] if m["role"] == "tool")
        self.assertFalse(json.loads(returned["content"])["ok"])
        self.assertIsNone(self.controller.current_task())
        self.assertFalse(self.controller.windows.input_enabled)

    def test_capture_stores_unknown_pixels_not_backend_claimed_victory(self):
        task = self.draft()
        self.capture()
        current = self.controller.current_task()
        self.assertEqual(current["status"], "observing")
        self.assertEqual(current["latest_observation_state"], "unknown")
        self.assertEqual(current["completed_runs"], 0)
        self.assertEqual(self.observations[0]["state"], "unknown")
        self.assertEqual(self.observations[0]["task_id"], task["id"])
        self.assertNotEqual(self.controller.windows.capture_thread, threading.get_ident())
        self.assertEqual(self.controller.windows.capture_background_flags, [True])

    def test_analysis_finish_is_only_suggestion_and_attaches_real_observation_path(self):
        self.keyed()
        self.draft()
        self.capture()
        model = self.install_model(Model(tool("propose_next_step", {
            "action": "finish", "reason": "画面可能是结算，尚待确认", "expected_result": "下一次观察验证结算"})))
        self.controller.analyze_observation()
        self.idle()
        self.assertEqual(len(self.plans), 1)
        self.assertFalse(self.plans[0]["executed"])
        self.assertEqual(self.plans[0]["game_state"], "unknown")
        self.assertIn("尚未执行", self.plans[0]["summary"])
        self.assertEqual(self.controller.current_task()["status"], "observing")
        self.assertEqual(self.controller.current_task()["completed_runs"], 0)
        content = model.calls[0]["messages"][-1]["content"]
        self.assertEqual(content[1]["type"], "image_url")
        self.assertFalse(self.controller.windows.input_enabled)

    def test_pause_and_resume_invalidate_observations_without_enabling_input(self):
        self.draft()
        self.capture()
        before = self.controller.current_task()["version"]
        self.controller.pause()
        paused = self.controller.current_task()
        self.assertEqual(paused["status"], "paused")
        self.assertGreater(paused["version"], before)
        self.controller.resume()
        self.assertEqual(self.controller.current_task()["status"], "observing")
        self.assertGreater(self.controller.current_task()["version"], paused["version"])
        self.assertIsNone(self.controller._latest_observation)
        self.assertFalse(self.controller.windows.input_enabled)

    def test_stop_is_immediate_and_late_chat_cannot_change_new_task(self):
        self.keyed()
        original = self.draft("旧目标")
        model = self.install_model(BlockingModel(assistant("不应展示的晚到回复")))
        self.controller.submit("随便聊聊", 1)
        self.assertTrue(model.entered.wait(1))
        started = time.monotonic()
        self.controller.stop()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(self.controller.is_busy)
        self.assertEqual(self.controller.engine.get_task(original["id"])["status"], "cancelled")
        new = self.draft("新的安排")
        model.release.set()
        self.wait_for(model.returned.is_set)
        self.wait_for(lambda: not self.controller.is_busy)
        self.assertEqual(self.controller.current_task()["id"], new["id"])
        self.assertNotIn(("assistant", "不应展示的晚到回复"), self.messages)

    def test_external_goal_version_change_rejects_stale_model_tool(self):
        self.keyed()
        original = self.draft("原目标")
        model = self.install_model(BlockingModel(tool("change_game_goal", {"goal": "旧回复想改写目标"})))
        self.controller.submit("改一下", 0)
        self.assertTrue(model.entered.wait(1))
        self.controller.engine.update_goal(original["id"], "已经更新的新目标")
        model.release.set()
        self.idle()
        self.assertEqual(self.controller.current_task()["goal"], "已经更新的新目标")
        self.assertTrue(any("旧响应已丢弃" in text for text in self.notices))
        self.assertFalse(any(role == "assistant" for role, text in self.messages))

    def test_settings_save_contains_no_key_and_rebuilds_sessions(self):
        self.keyed()
        previous = self.controller.persona
        self.controller.connection_verified = True
        self.controller.save_settings("deepseek-flash", "https://api.deepseek.com/v1")
        raw = (self.controller.data_dir / "settings.json").read_text(encoding="utf-8")
        self.assertEqual(set(json.loads(raw)), {"model", "base_url", "game_model", "game_thinking",
                                               "intent_thinking", "leader_thinking"})
        self.assertNotIn(TEST_KEY, raw)
        self.assertIsNot(self.controller.persona, previous)
        self.assertFalse(self.controller.connection_verified)
        self.assertTrue(self.controller.has_key)
        self.controller.save_settings("bad model", "https://api.deepseek.com")
        self.assertEqual((self.controller.data_dir / "settings.json").read_text(encoding="utf-8"), raw)
        self.assertTrue(self.errors)

    def test_connection_is_only_probed_by_explicit_test_and_uses_worker(self):
        self.keyed()
        probe = Model(assistant("OK"))
        with patch("shu.controller.DeepSeekClient", return_value=probe) as constructor:
            self.assertEqual(probe.calls, [])
            self.controller.test_connection()
            self.idle()
        self.assertTrue(self.controller.connection_verified)
        self.assertEqual(probe.calls[0]["messages"], [{"role": "user", "content": "只回复 OK"}])
        self.assertEqual(constructor.call_args.args[0].max_tokens, 64)
        self.assertNotEqual(probe.calls[0]["thread"], threading.get_ident())
        self.assertNotIn(TEST_KEY, " ".join(self.notices + self.errors))

    def test_role_loop_is_bounded_to_four_model_calls(self):
        self.keyed()
        model = self.install_model(Model(*[tool("get_task_status", call_id=f"call-{i}") for i in range(4)]))
        self.controller.submit("看看任务状态", 0)
        self.idle()
        self.assertEqual(len(model.calls), 4)
        self.assertTrue(any("工具处理上限" in text for role, text in self.messages if role == "system"))

    def test_bind_invalidates_task_and_keeps_input_disabled(self):
        task = self.draft()
        bound = self.controller.bind_window(99)
        self.assertEqual(bound.hwnd, 99)
        self.assertEqual(self.controller.current_task()["status"], "needs_input")
        self.assertGreater(self.controller.current_task()["version"], task["version"])
        self.assertFalse(self.controller.windows.input_enabled)

    def test_failed_capture_clears_previous_observation_and_finishes_busy(self):
        self.draft()
        self.capture()
        busy = []
        self.controller.busy_changed.connect(busy.append)
        with patch.object(self.controller.windows, "capture", side_effect=WindowError("测试截图失败")):
            self.assertTrue(self.controller.capture())
            self.assertIsNone(self.controller._latest_observation)
            self.idle()
        self.assertEqual(busy, [True, False])
        self.assertIsNone(self.controller._latest_observation)
        self.assertIn("测试截图失败", self.errors)

    def test_analysis_rejects_expired_or_different_window_before_model_request(self):
        self.keyed()
        model = self.install_model(Model())
        self.capture()
        self.controller._latest_observation["captured_at"] = time.time() - 120
        self.controller.analyze_observation()
        self.assertIn("截图已过期", self.errors[-1])
        self.capture()
        self.controller.windows.bound.pid += 1
        self.controller.analyze_observation()
        self.assertIn("窗口身份已改变", self.errors[-1])
        self.assertEqual(model.calls, [])

    def test_failed_bind_also_invalidates_previous_observation(self):
        self.capture()
        with patch.object(self.controller.windows, "bind", side_effect=WindowError("窗口不存在")):
            with self.assertRaises(WindowError):
                self.controller.bind_window(999)
        self.assertIsNone(self.controller._latest_observation)

    def test_role_and_game_have_independent_clients_and_reasoning_configuration(self):
        self.assertIsNot(self.controller.client, self.controller.game_client)
        self.assertIs(self.controller.persona.client, self.controller.client)
        self.assertIs(self.controller.game.client, self.controller.game_client)
        self.assertFalse(self.controller.config.thinking)
        self.assertTrue(self.controller.game_config.thinking)
        self.assertEqual(self.controller.config.api_key_file, self.controller.game_config.api_key_file)

    def test_capture_requires_local_permission_and_binding_does_not_grant_it(self):
        self.assertFalse(self.controller.screen_capture_allowed)
        self.controller.bind_window(99)
        self.assertFalse(self.controller.screen_capture_allowed)
        self.assertFalse(self.controller.capture())
        self.assertFalse(self.controller.is_busy)
        self.assertEqual(self.controller.windows.capture_count, 0)
        self.assertIn("尚未授权", self.errors[-1])

    def test_capture_permission_merges_settings_and_persists_both_game_fields(self):
        self.controller.save_settings("role-model", "https://api.deepseek.com", "game-model", False)
        self.controller.set_capture_permission(True)
        saved = json.loads((self.controller.data_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["model"], "role-model")
        self.assertEqual(saved["game_model"], "game-model")
        self.assertFalse(saved["game_thinking"])
        self.assertTrue(saved["screen_capture_allowed"])
        restored = SessionController(self.root, self.controller.data_dir)
        try:
            self.assertTrue(restored.screen_capture_allowed)
            self.assertEqual(restored.game_config.model, "game-model")
            self.assertFalse(restored.game_config.thinking)
            self.assertFalse(restored.config.thinking)
            restored.save_settings("another-role", "https://api.deepseek.com/v1")
            self.assertEqual(restored.game_config.model, "game-model")
            self.assertFalse(restored.game_config.thinking)
            self.assertTrue(restored.screen_capture_allowed)
        finally:
            restored.close()

    def test_natural_role_observation_runs_real_capture_and_separate_game_model(self):
        self.keyed()
        self.controller.set_capture_permission(True)
        role_model = self.install_model(Model(tool("observe_game"), assistant("已取得画面，分析仍有不确定性。")))
        game_model = Model(tool("propose_next_step", {
            "action": "仅描述当前页面", "reason": "依据本次图像", "expected_result": "请用户核对"}))
        self.controller.game_client = game_model
        self.controller.game = GameSession(game_model)
        self.controller.submit("看一下我现在的游戏", 0)
        self.idle()
        self.assertEqual(self.controller.windows.capture_count, 1)
        self.assertEqual(self.controller.windows.capture_background_flags, [True])
        self.assertEqual(len(game_model.calls), 1)
        self.assertEqual(len(self.observations), 1)
        self.assertEqual(len(self.plans), 1)
        self.assertIsNone(self.controller.current_task())
        self.assertFalse(self.controller.windows.input_enabled)
        self.assertIn("observe_game", [t["function"]["name"] for t in role_model.calls[0]["tools"]])
        result = next(m for m in role_model.calls[1]["messages"] if m["role"] == "tool")
        returned = json.loads(result["content"])
        self.assertTrue(returned["ok"])
        self.assertFalse(returned["executed_game_action"])
        self.assertIn("本次图像", returned["analysis"])
        self.assertEqual(game_model.calls[0]["messages"][-1]["content"][1]["type"], "image_url")

    def test_role_cannot_grant_screen_permission_through_a_tool_call(self):
        self.keyed()
        model = self.install_model(Model(tool("observe_game"), assistant("请先开启本地截图权限。")))
        self.controller.submit("看看当前游戏", 0)
        self.idle()
        returned = next(m for m in model.calls[1]["messages"] if m["role"] == "tool")
        self.assertFalse(json.loads(returned["content"])["ok"])
        self.assertEqual(self.controller.windows.capture_count, 0)
        self.assertFalse(self.controller.screen_capture_allowed)

    def test_permission_revocation_discards_inflight_game_response(self):
        self.keyed()
        self.capture()
        game_model = BlockingModel(assistant("不应展示的过时观察"))
        self.controller.game_client = game_model
        self.controller.game = GameSession(game_model)
        self.controller.analyze_observation()
        self.assertTrue(game_model.entered.wait(1))
        began = time.monotonic()
        self.controller.set_capture_permission(False)
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertFalse(self.controller.is_busy)
        self.assertIsNone(self.controller._latest_observation)
        game_model.release.set()
        self.wait_for(game_model.returned.is_set)
        self.assertEqual(self.plans, [])
        self.assertFalse(self.controller.windows.input_enabled)

    def test_local_stop_discards_frame_returned_by_blocked_background_capture(self):
        blocked = BlockingWindows(self.root)
        self.controller.windows = blocked
        # Ensure even a failed assertion cannot leave a capture worker writing
        # into the temporary directory after the test has closed its stores.
        def drain_capture():
            blocked.release.set()
            blocked.returned.wait(2)
            APP.processEvents()
        self.addCleanup(drain_capture)
        self.controller.set_capture_permission(True)
        self.assertTrue(self.controller.capture())
        self.assertTrue(blocked.entered.wait(1))
        started = time.monotonic()
        self.controller.stop()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(self.controller.is_busy)
        blocked.release.set()
        self.wait_for(blocked.returned.is_set)
        self.assertEqual(blocked.capture_background_flags, [True])
        self.assertEqual(blocked.capture_count, 1)
        self.assertEqual(self.observations, [])
        self.assertIsNone(self.controller._latest_observation)

    def test_successful_chat_survives_stop_and_settings_rebuilding_without_hidden_payloads(self):
        self.keyed()
        response = assistant("记得，这次我们先检查游戏画面。")
        response["reasoning_content"] = "synthetic hidden reasoning must not be persisted"
        self.install_model(Model(response))
        self.controller.submit("请记住，我们先检查游戏画面。", 1)
        self.idle()
        expected = [
            {"role": "user", "content": "请记住，我们先检查游戏画面。"},
            {"role": "assistant", "content": "记得，这次我们先检查游戏画面。"},
        ]
        self.controller.message.emit("system", "synthetic diagnostic status, not model memory")
        self.assertEqual(self.controller.memory.model_messages(), expected)
        self.assertNotIn(response["reasoning_content"], str(self.controller.memory.load_messages()))
        self.controller.stop()
        restored = [m for m in self.controller.persona.messages if m["role"] != "system"]
        self.assertEqual(restored, expected)
        self.controller.save_settings("deepseek-flash", "https://api.deepseek.com/v1")
        rebuilt = [m for m in self.controller.persona.messages if m["role"] != "system"]
        self.assertEqual(rebuilt, expected)
        self.assertNotIn("synthetic diagnostic", str(self.controller.persona.messages))
        self.assertEqual(len(self.controller.memory.load_messages()), 3)

    def test_controller_restart_restores_visible_history_and_complete_context_for_next_reply(self):
        self.keyed()
        self.install_model(Model(assistant("第一轮回答。"), assistant("第二轮回答。")))
        self.controller.submit("第一轮问题。", 1)
        self.idle()
        self.controller.submit("第二轮问题。", 1)
        self.idle()
        self.controller.message.emit("system", "网络诊断：不作为模型对话")
        self.controller.close()
        next_model = Model(assistant("我保留了前两轮上下文。"))
        with patch("shu.controller.DeepSeekClient", side_effect=[next_model, Model(), Model()]):
            restored = SessionController(self.root, self.root / "data")
        self.addCleanup(restored.close)
        restored.windows = Windows(self.root)
        expected = [
            {"role": "user", "content": "第一轮问题。"},
            {"role": "assistant", "content": "第一轮回答。"},
            {"role": "user", "content": "第二轮问题。"},
            {"role": "assistant", "content": "第二轮回答。"},
        ]
        self.assertEqual(restored.memory.model_messages(), expected)
        self.assertEqual([m for m in restored.persona.messages if m["role"] != "system"], expected)
        self.assertEqual(len(restored.memory.load_messages()), 5)
        restored.submit("你还记得刚才的话吗？", 1)
        self.wait_for(lambda: not restored.is_busy)
        sent_dialogue = [m for m in next_model.calls[0]["messages"] if m["role"] != "system"]
        self.assertEqual(sent_dialogue, expected + [{"role": "user", "content": "你还记得刚才的话吗？"}])
        self.assertNotIn("网络诊断", str(next_model.calls[0]["messages"]))
        self.assertEqual(restored.memory.model_messages()[-1],
                         {"role": "assistant", "content": "我保留了前两轮上下文。"})


if __name__ == "__main__":
    unittest.main()

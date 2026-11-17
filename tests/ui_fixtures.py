from pathlib import Path
from types import SimpleNamespace
from PySide6.QtCore import QObject, Signal
from shu.core import TaskEngine

class FakeWindows:
    def __init__(self):
        self.window = None

    def bound_window(self):
        return self.window

    def discover_capabilities(self):
        return {"window_enumeration": True, "foreground_capture": True,
                "maafw_installed": False}


class FakeController(QObject):
    activity_progress = Signal(dict)
    network_progress = Signal(dict)
    message = Signal(str, str)
    changed = Signal()
    observation_ready = Signal(dict)
    plan_ready = Signal(dict)
    notice = Signal(str)
    error = Signal(str)
    busy_changed = Signal(bool)

    def __init__(self, data_dir):
        super().__init__()
        self.data_dir = data_dir
        self.key_path = data_dir / "not-a-real-key.txt"
        self.config = SimpleNamespace(model="test-model", base_url="https://example.invalid")
        self.has_key = False
        self.connection_verified = False
        self.is_busy = False
        self.windows = FakeWindows()
        self.engine = TaskEngine(data_dir)
        self.task = None
        self.submissions = []
        self.stops = 0
        self.pauses = 0
        self.closes = 0
        self.captures = 0
        self.screen_capture_allowed = False
        self.permission_changes = []
        self.response_language = 'zh'

    def current_task(self):
        return self.engine.get_task(self.task["id"]) if self.task else None

    def harness_availability(self):
        return {"message": "测试替身：未连接游戏工具"}

    def submit(self, text, mode):
        self.submissions.append((text, mode))

    def stop(self, silent=False):
        self.stops += 1

    def test_connection(self):
        raise AssertionError("A UI smoke must not test a real connection")

    def analyze_observation(self):
        pass

    def capture(self):
        self.captures += 1
        self.is_busy = True
        self.busy_changed.emit(True)

    def pause(self):
        self.pauses+=1
        task=self.current_task()
        if task and task['status'] not in {'cancelled','completed','failed'}:
            self.engine.pause_task(task['id'])

    def close(self):
        self.closes+=1
        task=self.current_task()
        if task and task['status'] in {'running','observing','needs_input'}:
            self.engine.pause_task(task['id'])

    def resume(self):
        pass

    def set_capture_permission(self, allowed):
        self.screen_capture_allowed = bool(allowed)
        self.permission_changes.append(bool(allowed))

    def set_game_input_permission(self,allowed):
        self.game_input_allowed=bool(allowed)

    def set_response_language(self,language):
        self.response_language=language



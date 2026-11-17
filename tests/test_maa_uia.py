"""UIA guard tests use an in-memory accessibility provider, never real OS input."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from shu.maa_uia import (MAA_EXECUTABLE, MAX_ELEMENTS, MaaUia, MaaUiaError,
                         WindowIdentity, _NativeSession)


def node(number, *, pid=202, **changes):
    data = {"runtime_id": [42, number], "pid": pid, "name": "control " + str(number),
            "automation_id": "control" + str(number), "control_type": "Button",
            "enabled": True, "offscreen": False,
            "rect": {"left": 10, "top": 10, "right": 110, "bottom": 40, "width": 100, "height": 30},
            "patterns": ["invoke", "toggle", "set_value", "select"],
            "password": False, "read_only": False, "value": "old", "toggle": 0, "selected": False}
    data.update(changes)
    return data


class FakeProvider:
    def __init__(self):
        self.identity = WindowIdentity(101, 202, MAA_EXECUTABLE, "MAA", creation_time=777)
        self.nodes = {0: node(0, patterns=[], control_type="Window"), 1: node(1)}
        self.edges = {0: [1]}
        self.top = [0]
        self.actions = []
        self.reads = []
        self.opened = self.closed = 0
        self.threads = []
        self.read_hook = None
        self.action_hook = None

    @contextmanager
    def session(self):
        self.opened += 1
        self.threads.append(threading.get_ident())
        try:
            yield self
        finally:
            self.closed += 1

    def window(self, hwnd):
        return self.identity

    def roots(self, hwnd, pid):
        return iter(self.top)

    def children(self, element):
        return iter(self.edges.get(element, []))

    def read(self, element):
        self.reads.append(element)
        if self.read_hook:
            self.read_hook(element)
        return deepcopy(self.nodes[element])

    def perform(self, element, action, value, *, guard):
        guard()
        self.actions.append((element, action, value))
        if action == "set_value":
            self.nodes[element]["value"] = value
        elif action == "toggle":
            self.nodes[element]["toggle"] = 1 - self.nodes[element]["toggle"]
        elif action == "select":
            self.nodes[element]["selected"] = True
        if self.action_hook:
            self.action_hook()


class UiaGuards(unittest.TestCase):
    def test_unreadable_node_marks_snapshot_incomplete(self):
        self.provider.nodes[2] = node(2)
        self.provider.edges[0] = [1, 2]
        def fail_one(element):
            if element == 1:
                raise RuntimeError('provider property read failed')
        self.provider.read_hook = fail_one
        result = self.uia.snapshot()
        self.assertTrue(result['possibly_truncated'])
        self.assertTrue(any(item['id'] == 'uia:42:2' for item in result['elements']))

    def test_native_provider_budget_allows_warmup_within_bounded_snapshot(self):
        from shu.maa_uia import NativeUiaBackend, SNAPSHOT_SECONDS
        ole = Mock()
        ole.CoInitializeEx.return_value = 0
        comtypes = Mock()
        client = Mock()
        automation = client.CreateObject.return_value
        native = Mock()
        with patch('ctypes.WinDLL', return_value=ole), \
                patch.dict(sys.modules, {'comtypes': comtypes}), \
                patch('shu.maa_uia._NativeSession', return_value=native), \
                patch('shu.maa_uia.importlib.import_module', side_effect=lambda name: comtypes if name == 'comtypes' else client):
            with NativeUiaBackend().session() as session:
                self.assertIs(session, native)
                self.assertEqual(automation.ConnectionTimeout, 1000)
                self.assertEqual(automation.TransactionTimeout, 1000)
                self.assertLess(automation.TransactionTimeout, SNAPSHOT_SECONDS * 1000)
        native.close.assert_called_once()

    def test_provider_timeout_has_specific_code(self):
        class ProviderTimeout(Exception):
            hresult = -2146233083

        def roots(*_args):
            raise ProviderTimeout('provider timed out')

        self.provider.roots = roots
        with self.assertRaises(MaaUiaError) as caught:
            self.uia.snapshot()
        self.assertEqual(caught.exception.code, 'uia_provider_timeout')

    def test_native_uses_control_view_to_skip_wpf_layout_without_losing_controls(self):
        class Automation:
            ControlViewWalker = object()

            @property
            def RawViewWalker(self):
                raise AssertionError('Raw layout must not consume the control snapshot budget')

        automation = Automation()
        with patch('ctypes.WinDLL'):
            native = _NativeSession(automation, SimpleNamespace())
        self.assertIs(native.walker, automation.ControlViewWalker)

    def setUp(self):
        self.provider = FakeProvider()
        self.now = 50.0
        self.uia = MaaUia(101, 202, backend=self.provider,
                          monotonic=lambda: self.now, clock=lambda: 1000 + self.now)

    def ready(self):
        snapshot = self.uia.snapshot()
        target = next(e for e in snapshot["elements"] if e["automation_id"] == "control1")
        return snapshot, target

    def rejected(self, code, callback):
        with self.assertRaises(MaaUiaError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.provider.actions, [])

    def test_snapshot_is_passive_bounded_and_in_its_own_session_thread(self):
        snapshot, target = self.ready()
        self.assertEqual(snapshot["snapshot_status"], "ok")
        self.assertEqual(target["id"], "uia:42:1")
        self.assertEqual(snapshot["window"]["hwnd"], 101)
        self.assertEqual(snapshot["timestamp"], 1050)
        self.assertEqual(self.provider.actions, [])
        self.assertEqual(self.provider.opened, self.provider.closed)
        self.assertNotIn(threading.get_ident(), self.provider.threads)

    def test_all_four_actions_return_fresh_snapshot_and_require_visual_verification(self):
        for action, value in [("invoke", None), ("toggle", None), ("set_value", "hello 世界"), ("select", None)]:
            snapshot, target = self.ready()
            result = self.uia.act(snapshot["snapshot_id"], target["id"], action, value)
            self.assertTrue(result["ok"])
            self.assertEqual(result["result"], "dispatched")
            self.assertTrue(result["verification_required"])
            after = result["after"]
            self.assertNotEqual(after["snapshot_id"], snapshot["snapshot_id"])
            after_target = next(e for e in after["elements"] if e["id"] == target["id"])
            if action == "set_value":
                self.assertEqual(after_target["value"], value)
            if action == "toggle":
                self.assertEqual(after_target["toggle"], 1)
            if action == "select":
                self.assertTrue(after_target["selected"])
            with self.assertRaises(MaaUiaError):
                self.uia.act(snapshot["snapshot_id"], target["id"], action, value)
        self.assertEqual(len(self.provider.actions), 4)
        self.assertEqual(self.provider.opened, self.provider.closed)

    def test_latest_snapshot_only_and_returned_data_cannot_tamper_with_evidence(self):
        old, target = self.ready()
        current, _ = self.ready()
        self.rejected("snapshot_mismatch", lambda: self.uia.act(old["snapshot_id"], target["id"], "invoke"))
        current["elements"][1]["name"] = "tampered by caller"
        self.assertTrue(self.uia.act(current["snapshot_id"], target["id"], "invoke")["ok"])

    def test_exact_executable_pid_hwnd_and_process_lifetime_are_required(self):
        variants = [{"executable": r"D:\other\MAA.exe"}, {"pid": 999}, {"hwnd": 999}]
        for change in variants:
            self.provider.identity = replace(WindowIdentity(101, 202, MAA_EXECUTABLE), **change)
            self.rejected("binding_mismatch", self.uia.snapshot)
        self.assertEqual(self.provider.reads, [])
        self.provider.identity = WindowIdentity(101, 202, MAA_EXECUTABLE, creation_time=777)
        snapshot, target = self.ready()
        self.provider.identity = replace(self.provider.identity, creation_time=888)
        self.rejected("binding_mismatch", lambda: self.uia.act(snapshot["snapshot_id"], target["id"], "invoke"))

    def test_snapshot_expires_at_ten_seconds(self):
        snapshot, target = self.ready()
        self.now += 10
        self.rejected("snapshot_stale", lambda: self.uia.act(snapshot["snapshot_id"], target["id"], "invoke"))

    def test_unknown_element_cannot_be_dispatched(self):
        snapshot, _ = self.ready()
        self.rejected("element_unknown", lambda: self.uia.act(snapshot["snapshot_id"], "uia:42:999", "invoke"))

    def test_changes_to_geometry_identity_or_properties_reject_stale_target(self):
        changes = {"name": "changed", "automation_id": "changed", "control_type": "Edit",
                   "enabled": False, "offscreen": True, "value": "changed", "toggle": 1,
                   "selected": True, "patterns": [], "password": True, "read_only": True,
                   "rect": {"left": 100, "top": 10, "right": 200, "bottom": 40, "width": 100, "height": 30}}
        for key, value in changes.items():
            with self.subTest(key=key):
                self.provider.nodes[1] = node(1)
                snapshot, target = self.ready()
                self.provider.nodes[1][key] = value
                self.rejected("element_changed", lambda: self.uia.act(snapshot["snapshot_id"], target["id"], "invoke"))

    def test_missing_element_or_foreign_pid_subtree_never_reacquires(self):
        snapshot, target = self.ready()
        self.provider.nodes[1]["pid"] = 303
        self.provider.nodes[2] = node(2)
        self.provider.edges[1] = [2]
        self.provider.reads.clear()
        self.rejected("element_unavailable", lambda: self.uia.act(snapshot["snapshot_id"], target["id"], "invoke"))
        self.assertNotIn(2, self.provider.reads)

    def test_disabled_offscreen_password_and_readonly_controls_are_rejected(self):
        for changes, action, code in [({"enabled": False}, "invoke", "element_not_actionable"),
                                      ({"offscreen": True}, "invoke", "element_not_actionable"),
                                      ({"password": True}, "set_value", "element_not_actionable"),
                                      ({"read_only": True}, "set_value", "pattern_not_supported"),
                                      ({"patterns": ["toggle"]}, "invoke", "pattern_not_supported")]:
            self.provider.nodes[1] = node(1, **changes)
            snapshot, target = self.ready()
            if changes.get("password"):
                self.assertIsNone(target["value"])
                self.assertEqual(target["patterns"], [])
            self.rejected(code, lambda: self.uia.act(snapshot["snapshot_id"], target["id"], action,
                                                    "new" if action == "set_value" else None))

    def test_action_and_value_allowlist(self):
        snapshot, target = self.ready()
        for action, value, code in [("click", None, "action_not_allowed"),
                                    ("set_value", 42, "invalid_value"),
                                    ("set_value", "x" * 4097, "invalid_value"),
                                    ("invoke", "text", "invalid_value")]:
            self.rejected(code, lambda: self.uia.act(snapshot["snapshot_id"], target["id"], action, value))

    def test_element_budget_is_lazy_and_depth_is_bounded(self):
        self.provider.nodes = {n: node(n) for n in range(1000)}
        self.provider.edges = {0: range(1, 1000)}
        snapshot = self.uia.snapshot()
        self.assertEqual(len(snapshot["elements"]), MAX_ELEMENTS)
        self.assertEqual(len(self.provider.reads), MAX_ELEMENTS)
        self.assertTrue(snapshot["possibly_truncated"])
        self.provider.edges = {n: [n + 1] for n in range(999)}
        self.provider.reads.clear()
        snapshot = self.uia.snapshot()
        self.assertEqual(len(snapshot["elements"]), 13)  # root depth zero through 12
        self.assertNotIn(13, self.provider.reads)
        self.assertTrue(snapshot["possibly_truncated"])

    def test_time_budget_returns_partial_snapshot_without_dispatch(self):
        self.provider.edges = {0: [1]}
        self.provider.read_hook = lambda _: setattr(self, "now", self.now + 1)
        snapshot = self.uia.snapshot()
        self.assertTrue(snapshot["possibly_truncated"])
        self.assertEqual(self.provider.actions, [])

    def test_stop_during_reacquisition_prevents_dispatch(self):
        snapshot, target = self.ready()
        stopped = threading.Event()
        self.provider.read_hook = lambda _: stopped.set()
        self.rejected("cancelled", lambda: self.uia.act(snapshot["snapshot_id"], target["id"], "invoke", cancel_event=stopped))

    def test_native_stop_guard_runs_after_pattern_acquisition(self):
        calls = []
        stopped = threading.Event()
        native = _NativeSession.__new__(_NativeSession)

        def pattern(element, action):
            stopped.set()  # STOP arrived while the provider was acquiring the pattern.
            return SimpleNamespace(Invoke=lambda: calls.append("invoke"))

        native._pattern = pattern
        element = SimpleNamespace(CurrentIsPassword=False, CurrentIsEnabled=True, CurrentIsOffscreen=False)

        def guard():
            if stopped.is_set():
                raise MaaUiaError("cancelled", "cancelled")

        self.rejected("cancelled", lambda: native.perform(element, "invoke", None, guard=guard))
        self.assertEqual(calls, [])

    def test_inaccessible_provider_and_post_dispatch_disappearance_fail_explicitly(self):
        self.provider.top = [0]
        self.provider.edges = {}
        snapshot = self.uia.snapshot()
        self.assertEqual(snapshot["snapshot_status"], "unsupported")
        self.provider.edges = {0: [1]}
        snapshot, target = self.ready()
        self.provider.action_hook = lambda: setattr(self.provider, "identity", replace(self.provider.identity, pid=999))
        result = self.uia.act(snapshot["snapshot_id"], target["id"], "invoke")
        self.assertTrue(result["ok"])
        self.assertIsNone(result["after"])
        with self.assertRaises(MaaUiaError):
            self.uia.act(snapshot["snapshot_id"], target["id"], "invoke")

    def test_native_password_read_does_not_query_any_patterns(self):
        class PasswordElement:
            CurrentIsPassword = True
            CurrentBoundingRectangle = SimpleNamespace(left=0, top=0, right=10, bottom=10)
            CurrentProcessId = 202
            CurrentName = "Password"
            CurrentAutomationId = "PasswordBox"
            CurrentControlType = 50004
            CurrentIsEnabled = True
            CurrentIsOffscreen = False

            def GetRuntimeId(self):
                return [42, 10]

            def GetCurrentPattern(self, number):
                raise AssertionError("Password values and patterns must not be queried")

        native = _NativeSession.__new__(_NativeSession)
        result = native.read(PasswordElement())
        self.assertIsNone(result["value"])
        self.assertEqual(result["patterns"], [])

    def test_import_and_construction_do_not_import_comtypes(self):
        script = "import sys; from shu.maa_uia import MaaUia; MaaUia(1, 1); assert 'comtypes' not in sys.modules"
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

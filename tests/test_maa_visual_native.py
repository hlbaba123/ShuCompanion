"""Transport invariants on a fake desktop. Never sends real mouse/keyboard input."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from shu.maa_visual_native import (
    MAA_EXECUTABLE, MaaVisualError, MaaVisualNative, _NativeMaaBackend, discover_maa_windows,
)
from shu.windows import WindowError, WindowInfo, _encode_png


class FakeBackend:
    def __init__(self):
        self.info = WindowInfo(111, 222, "MAA", 30, 40, 200, 100, True, False, MAA_EXECUTABLE, "WPF")
        self.birth = 333
        self.dpi_value = 96
        self.permission_value = {"known": True, "own_integrity": 8192, "target_integrity": 8192,
                                 "input_allowed": True, "requires_elevation": False}
        self.events = []
        self.capture_hook = None
        self.key_hook = None
        self.capture_method = "win32_printwindow"
        self.other_windows = []
        self.message_target_allowed = True

    def verify_message_target(self, hwnd):
        if not self.message_target_allowed:
            raise MaaVisualError('Owned popup receives messages', 'message_target_changed')

    def get_window(self, hwnd):
        return self.info if self.info and self.info.hwnd == hwnd else None

    def list_windows(self):
        return [self.info, *self.other_windows]

    def creation_time(self, pid):
        return self.birth

    def dpi(self, hwnd):
        return self.dpi_value

    def permission(self, pid):
        return self.permission_value

    def capture(self, info, path):
        pixels = bytes((40, 50, 60, 255)) * info.width * info.height
        path.write_bytes(_encode_png(info.width, info.height, pixels))
        if self.capture_hook:
            self.capture_hook()
        return {"method": self.capture_method, "width": info.width, "height": info.height,
                "requires_foreground": False}

    def focus(self, hwnd):
        self.events.append(("focus", hwnd))
        self.info = replace(self.info, minimized=False, foreground=True)

    def click(self, x, y, guard):
        guard()
        self.events.append(("click", x, y))
        return {"pointer_verified": True, "button_released": True}

    def click_battle(self, x, y, guard):
        return self.click(x, y, guard)

    def right_click(self, x, y, guard):
        guard()
        self.events.append(('right', x, y))
        return {'pointer_verified':True, 'button_released':True}

    def scroll_queue(self, x, y, notches, guard):
        guard()
        self.events.append(('scroll', x, y, notches))
        return {'pointer_verified':True}

    def send_keyboard(self, vk, scan, flags):
        self.events.append(("key", vk, scan, flags))
        if self.key_hook:
            self.key_hook(vk, scan, flags)


class MaaVisualNativeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.native = FakeBackend()
        self.now = 100.
        self.identity = {"hwnd": 111, "pid": 222, "creation_time": 333, "executable": MAA_EXECUTABLE}
        self.adapter = MaaVisualNative(self.identity, self.temporary.name, backend=self.native,
                                       monotonic=lambda: self.now, clock=lambda: self.now + 1000)

    def snap(self):
        return self.adapter.snapshot()["screenshot_id"]

    def rejects(self, code, operation):
        with self.assertRaises(WindowError) as failure:
            operation()
        self.assertEqual(failure.exception.code, code)
        return failure.exception

    def test_requires_positive_complete_identity_and_exact_installed_path(self):
        for key, value in (("hwnd", True), ("pid", 0), ("creation_time", 0),
                           ("executable", r"C:\other\MAA.exe"), ("executable", r"D:\game\Arknights.exe")):
            with self.assertRaises(ValueError):
                MaaVisualNative({**self.identity, key: value}, self.temporary.name, backend=self.native)

    def test_snapshot_has_real_file_hash_geometry_and_no_input(self):
        snapshot = self.adapter.snapshot()
        self.assertEqual(snapshot["window"]["client_rect"], {"left": 30, "top": 40, "width": 200, "height": 100})
        self.assertEqual(snapshot["window"]["creation_time"], 333)
        self.assertEqual(snapshot["window"]["dpi"], 96)
        self.assertTrue(Path(snapshot["image_path"]).is_file())
        self.assertEqual(len(snapshot["image_sha256"]), 64)
        self.assertEqual(self.native.events, [])
        self.assertTrue(snapshot["background_capture"])
        self.assertFalse(snapshot["recognition_verified"])

    def test_background_option_rejects_popup_before_dispatch(self):
        self.native.info = replace(self.native.info, foreground=False)
        self.native.message_target_allowed = False
        token = self.snap()
        transport = self.adapter._battle_input = Mock(act=Mock(return_value={}))
        self.rejects('message_target_changed', lambda: self.adapter.click_battle(
            30, 45, token, target='navigation_option', guard=lambda: None))
        transport.act.assert_not_called()

    def test_background_option_rechecks_popup_after_admission(self):
        self.native.info = replace(self.native.info, foreground=False)
        token = self.snap()
        dispatched = []
        def dispatch(*args, gate, **kwargs):
            gate()
            dispatched.append(True)
            return {}
        self.adapter._battle_input = Mock(act=dispatch)
        def popup_appeared():
            self.native.message_target_allowed = False
        self.rejects('message_target_changed', lambda: self.adapter.click_battle(
            30, 45, token, target='navigation_option', guard=popup_appeared))
        self.assertEqual(dispatched, [])

    def test_native_message_target_rejects_visible_owned_popup_or_disabled_window(self):
        native = _NativeMaaBackend.__new__(_NativeMaaBackend)
        user = Mock()
        native.native = Mock(user=user)
        user.IsWindow.return_value = True
        user.IsWindowEnabled.return_value = True
        user.GetAncestor.return_value = 111
        user.GetLastActivePopup.return_value = 777
        user.IsWindowVisible.return_value = True
        self.rejects('message_target_changed', lambda: native.verify_message_target(111))
        user.IsWindowVisible.return_value = False
        native.verify_message_target(111)
        user.GetLastActivePopup.return_value = 111
        native.verify_message_target(111)
        user.IsWindowEnabled.return_value = False
        self.rejects('message_target_changed', lambda: native.verify_message_target(111))

    def test_background_capture_does_not_focus_or_enable_background_input(self):
        self.native.info = replace(self.native.info, foreground=False)
        token = self.snap()
        self.rejects("not_foreground", lambda: self.adapter.click(10, 10, token))
        self.assertEqual(self.native.events, [])

    def test_background_start_and_stop_never_admit_or_use_postmessage(self):
        self.native.info = replace(self.native.info, foreground=False)
        for target in ('battle_start','battle_stop'):
            with self.subTest(target=target):
                token=self.snap();guard=Mock()
                self.adapter._battle_input=Mock(act=Mock(return_value={}))
                self.rejects('not_foreground',lambda:self.adapter.click_battle(
                    30,45,token,target=target,guard=guard))
                guard.assert_not_called();self.adapter._battle_input.act.assert_not_called()
        self.assertEqual(self.native.events,[])

    def test_start_and_stop_reject_focus_loss_during_admission(self):
        for target in ('battle_start','battle_stop'):
            self.native.info=replace(self.native.info,foreground=True)
            token=self.snap()
            def lose_focus():self.native.info=replace(self.native.info,foreground=False)
            self.rejects('not_foreground',lambda:self.adapter.click_battle(
                30,45,token,target=target,guard=lose_focus))
        self.assertEqual(self.native.events,[])

    def battle_pointer(self, sent=(2,)):
        backend = _NativeMaaBackend.__new__(_NativeMaaBackend)
        backend.native = Mock()
        backend.native.user.GetAsyncKeyState.return_value = 0
        backend.native.click_guarded.side_effect = lambda x,y,guard: guard()
        backend._send_mouse = Mock(side_effect=sent)
        self.native.click = backend.click
        self.native.click_battle = lambda x,y,guard: backend.click_battle(x,y,guard)
        return backend

    def test_battle_buttons_batch_press_and_release_without_game_dwell(self):
        for target in ('battle_start','battle_stop'):
            with self.subTest(target=target):
                backend = self.battle_pointer()
                with patch('shu.maa_visual_native.time.sleep') as sleep:
                    result = self.adapter.click_battle(30,45,self.snap(),target=target,guard=lambda:None)
                self.assertEqual([call.args for call in backend._send_mouse.call_args_list],
                                 [((2,0),(4,0))])
                backend.native.click_guarded.assert_not_called()
                self.assertTrue(result['native_evidence']['button_release_submitted'])
                self.assertEqual([call.args for call in sleep.call_args_list],[(.016,)])

    def test_partial_battle_press_only_releases_and_cannot_replay_screenshot(self):
        for released in (0,1):
            with self.subTest(released=released):
                backend = self.battle_pointer((1,released));token = self.snap()
                with patch('shu.maa_visual_native.time.sleep'):
                    error = self.rejects('partial_mouse_input', lambda:self.adapter.click_battle(
                        30,45,token,target='battle_start',guard=lambda:None))
                self.assertEqual([call.args for call in backend._send_mouse.call_args_list],
                                 [((2,0),(4,0)),((4,0),)])
                self.assertTrue(error.details['input_sent'])
                self.assertEqual(error.details['button_released'],bool(released))
                self.rejects('observation_mismatch', lambda:self.adapter.click_battle(
                    30,45,token,target='battle_start',guard=lambda:None))

    def test_battle_button_rejects_preexisting_left_press(self):
        backend = self.battle_pointer()
        backend.native.user.GetAsyncKeyState.return_value = 0x8000
        with patch('shu.maa_visual_native.time.sleep'):
            self.rejects('button_already_down',lambda:self.adapter.click_battle(
                30,45,self.snap(),target='battle_stop',guard=lambda:None))
        backend._send_mouse.assert_not_called()
        backend.native._pointer.assert_not_called()

    def test_battle_batch_rechecks_held_button_and_revocation_after_pointer_move(self):
        for failure in ('held','revoked','focus_lost'):
            with self.subTest(failure=failure):
                self.native.info=replace(self.native.info,foreground=True)
                backend=self.battle_pointer();calls=[]
                def admission():
                    calls.append(True)
                    if len(calls)==2:
                        if failure=='held':backend.native.user.GetAsyncKeyState.return_value=0x8000
                        elif failure=='focus_lost':self.native.info=replace(self.native.info,foreground=False)
                        else:raise MaaVisualError('revoked','stopped')
                code={'held':'button_already_down','revoked':'stopped','focus_lost':'not_foreground'}[failure]
                with patch('shu.maa_visual_native.time.sleep'):
                    self.rejects(code,lambda:self.adapter.click_battle(
                        30,45,self.snap(),target='battle_start',guard=admission))
                backend._send_mouse.assert_not_called()

    def test_battle_button_failed_batch_never_retries_press(self):
        backend = self.battle_pointer((0,))
        with patch('shu.maa_visual_native.time.sleep'):
            self.rejects('input_failed',lambda:self.adapter.click_battle(
                30,45,self.snap(),target='battle_start',guard=lambda:None))
        self.assertEqual([call.args for call in backend._send_mouse.call_args_list],
                         [((2,0),(4,0))])

    def test_battle_batch_rechecks_occlusion_after_native_admission(self):
        backend = self.battle_pointer()
        backend.native._verify_pointer_target.side_effect = [None,WindowError('occluded','target_occluded')]
        with patch('shu.maa_visual_native.time.sleep'):
            self.rejects('target_occluded',lambda:self.adapter.click_battle(
                30,45,self.snap(),target='battle_start',guard=lambda:None))
        backend._send_mouse.assert_not_called()

    def test_import_click_keeps_its_existing_native_path(self):
        backend = self.battle_pointer()
        self.adapter.click(30,45,self.snap(),target='code_input',guard=lambda:None)
        backend.native.click_guarded.assert_called_once()
        backend._send_mouse.assert_not_called()

    def test_option_on_background_maa_dispatches_to_bound_window_without_focusing(self):
        self.native.info = replace(self.native.info, foreground=False)
        events = []
        def dispatch(action, points, *, gate, expected_geometry):
            gate()
            events.append((action, points, expected_geometry))
            return {'input_dispatched': True, 'effect_verified': False}
        self.adapter._battle_input = Mock(act=dispatch)
        token = self.snap()
        result = self.adapter.click_battle(30, 45, token, target='navigation_option',
                                           guard=lambda: events.append('admitted'))
        self.assertEqual(events, ['admitted', ('click', [(30, 45)], {
            'left': 30, 'top': 40, 'width': 200, 'height': 100})])
        self.assertEqual(self.native.events, [])
        self.assertFalse(result['effect_verified'])
        self.rejects('observation_mismatch', lambda:
            self.adapter.click_battle(30, 45, token, target='navigation_option', guard=lambda: None))

    def test_background_option_rechecks_button_pixels_before_admission(self):
        self.native.info = replace(self.native.info, foreground=False)
        token = self.snap()
        original = self.native.capture
        def changed_capture(info, path):
            result = original(info, path)
            path.write_bytes(_encode_png(info.width, info.height, bytes((240, 240, 240, 255))*info.width*info.height))
            return result
        self.native.capture = changed_capture
        self.adapter._battle_input = Mock(act=lambda *a, gate, **k: gate())
        self.rejects('target_changed', lambda: self.adapter.click_battle(
            32, 41, token, target='navigation_option', guard=lambda: self.fail('stale option admitted')))
        self.assertEqual(self.native.events, [])

    def test_only_queue_add_allows_right_and_consumes_fresh_screenshot(self):
        token = self.snap()
        for target in (None, 'start', 'stop', 'queue_clear', 'code_input', 'row_delete:k'):
            self.rejects('target_not_allowed', lambda: self.adapter.click(3, 4, token,
                         button='right', target=target))
        result = self.adapter.click(3, 4, token, button='right', target='queue_add')
        self.assertEqual(self.native.events, [('right',33,44)])
        self.assertEqual(result['button'],'right')
        self.assertTrue(result['verification_required'])
        self.rejects('observation_mismatch', lambda: self.adapter.click(3,4,token,
                     button='right', target='queue_add'))

    def test_scroll_has_bounded_notches_and_rechecks_dpi_after_gate(self):
        token = self.snap()
        for notches in (0, 4, -4, True, 1.0):
            self.rejects('invalid_scroll', lambda: self.adapter.scroll_queue(3,4,notches,token))
        self.rejects('target_not_allowed', lambda: self.adapter.scroll_queue(3,4,1,token,target='start'))
        def gate():
            self.native.dpi_value = 144
        self.rejects('geometry_changed', lambda: self.adapter.scroll_queue(3,4,-2,token,guard=gate))
        self.assertEqual(self.native.events, [])
        result = self.adapter.scroll_queue(3,4,-2,self.snap())
        self.assertEqual(self.native.events, [('scroll',33,44,-2)])
        self.assertEqual(result['notches'],-2)

    def test_native_releases_right_button_if_the_down_up_batch_is_partial(self):
        native = _NativeMaaBackend.__new__(_NativeMaaBackend)
        native.native = Mock()
        native.native.user.GetAsyncKeyState.return_value = 0
        native._send_mouse = Mock(side_effect=[1,1])
        with patch('shu.maa_visual_native.time.sleep'):
            error = self.rejects('partial_mouse_input',lambda:native.right_click(11,12,lambda:None))
        self.assertTrue(error.details['partial_input'])
        self.assertTrue(error.details['button_released'])
        self.assertEqual([call.args for call in native._send_mouse.call_args_list],
                         [((8,0),(16,0)),((16,0),)])

    def test_native_wheel_checks_occlusion_again_after_gate(self):
        native = _NativeMaaBackend.__new__(_NativeMaaBackend)
        native.native = Mock()
        native.native._verify_pointer_target.side_effect = [None, WindowError('occluded','target_occluded')]
        native._send_wheel = Mock()
        with patch('shu.maa_visual_native.time.sleep'):
            self.rejects('target_occluded', lambda: native.scroll_queue(11,12,1,lambda:None))
        native._send_wheel.assert_not_called()

    def test_capture_rejects_minimized_and_does_not_automatically_restore(self):
        self.native.info = replace(self.native.info, minimized=True)
        self.rejects("minimized", self.adapter.snapshot)
        self.assertEqual(self.native.events, [])

    def test_explicit_focus_restores_without_maximizing_or_content_click(self):
        self.native.info = replace(self.native.info, minimized=True, foreground=False)
        result = self.adapter.focus(guard=lambda: None)
        self.assertEqual(self.native.events, [("focus", 111)])
        self.assertTrue(result["window"]["foreground"])
        self.assertEqual(result["window"]["client_rect"]["width"], 200)

    def test_focus_does_not_call_native_when_cancelled_or_permission_unknown(self):
        cancel = threading.Event(); cancel.set()
        self.rejects("stopped", lambda: self.adapter.focus(cancel_event=cancel))
        self.native.permission_value = {"known": False, "input_allowed": False}
        self.rejects("integrity_unknown", self.adapter.focus)

    def test_os_foreground_denial_is_a_known_focus_only_result(self):
        self.native.info = replace(self.native.info, foreground=False)
        def denied(hwnd):
            raise WindowError('Windows拒绝前台切换', 'focus_denied')
        self.native.focus = denied
        result = self.adapter.focus(guard=lambda: None)
        self.assertFalse(result['focus_acquired'])
        self.assertEqual(result['state'], 'awaiting_foreground')
        self.assertEqual(self.native.events, [])
        self.assertEqual(self.native.events, [])

    def test_process_path_pid_hwnd_and_creation_time_changes_reject(self):
        original = self.native.info
        for change in ({"pid": 999}, {"hwnd": 999}, {"executable": r"D:\game\Arknights.exe"}):
            self.native.info = replace(original, **change)
            self.rejects("binding_mismatch", self.adapter.snapshot)
        self.native.info = original
        self.native.birth += 1
        self.rejects("binding_mismatch", self.adapter.snapshot)
        self.assertEqual(self.native.events, [])

    def test_capture_geometry_change_invalidates_previous_evidence_and_removes_failed_file(self):
        old = self.snap()
        before = set(self.adapter.capture_dir.iterdir())
        self.native.capture_hook = lambda: setattr(self.native, "info", replace(self.native.info, width=210))
        self.rejects("geometry_changed", self.adapter.snapshot)
        self.assertEqual(set(self.adapter.capture_dir.iterdir()), before)
        self.rejects("observation_mismatch", lambda: self.adapter.key("TAB", old))

    def test_wrong_capture_method_cannot_be_reported_as_printwindow(self):
        self.native.capture_method = "desktop_crop"
        self.rejects("capture_failed", self.adapter.snapshot)
        self.assertEqual(list(self.adapter.capture_dir.iterdir()), [])

    def test_input_requires_same_or_higher_integrity(self):
        self.native.permission_value = {"known": True, "input_allowed": False, "requires_elevation": True,
                                        "own_integrity": 8192, "target_integrity": 12288}
        token = self.snap()
        self.rejects("integrity_mismatch", lambda: self.adapter.replace_code("prts://123456", token))
        self.assertEqual(self.native.events, [])

    def test_stale_geometry_dpi_expiry_and_focus_never_send_input(self):
        original = self.native.info
        token = self.snap(); self.now += 11
        self.rejects("observation_expired", lambda: self.adapter.click(10, 10, token))
        self.now = 100
        token = self.snap(); self.native.info = replace(original, left=99)
        self.rejects("geometry_changed", lambda: self.adapter.click(10, 10, token))
        self.native.info = original
        token = self.snap(); self.native.dpi_value = 144
        self.rejects("geometry_changed", lambda: self.adapter.key("TAB", token))
        self.native.dpi_value = 96
        token = self.snap(); self.native.info = replace(original, foreground=False)
        self.rejects("not_foreground", lambda: self.adapter.key("TAB", token))
        self.assertEqual(self.native.events, [])

    def test_click_uses_dynamic_client_origin_and_single_use_evidence(self):
        self.native.info = replace(self.native.info, left=400, top=80, width=100, height=75)
        token = self.snap()
        result = self.adapter.click(25, 30, token)
        self.assertEqual(self.native.events, [("click", 425, 110)])
        self.assertEqual(result["state"], "unverified")
        self.assertTrue(result["verification_required"])
        self.rejects("observation_mismatch", lambda: self.adapter.click(25, 30, token))

    def test_non_integer_or_outside_click_rejects(self):
        for x, y, code in ((True, 10, "invalid_coordinates"), (1.2, 2, "invalid_coordinates"),
                            (-1, 2, "out_of_bounds"), (200, 0, "out_of_bounds")):
            token = self.snap()
            self.rejects(code, lambda: self.adapter.click(x, y, token))
        self.assertEqual(self.native.events, [])

    def test_replace_accepts_only_mystery_code_and_never_sends_enter(self):
        for code in (r"D:\some\job.json", "prts://123\n", "prts://0", "prts://123;start", "prts://s/123", "https://prts.plus/?op=123"):
            self.rejects("invalid_copilot_code", lambda: self.adapter.replace_code(code, self.snap()))
        result = self.adapter.replace_code("prts://123456", self.snap())
        self.assertEqual(result["code"], "prts://123456")
        self.assertEqual([event[2] for event in self.native.events if event[0] == "key" and event[3] == 4],
                         list(map(ord, "prts://123456")))
        self.assertNotIn(("key", 13, 0, 0), self.native.events)
        self.assertEqual(self.native.events[:4], [("key", 17, 0, 0), ("key", 65, 0, 0),
                                                 ("key", 65, 0, 2), ("key", 17, 0, 2)])

    def test_unsafe_keyboard_names_reject(self):
        for key in ("CTRL+V", "ALT+F4", "WIN", "enter", None):
            self.rejects("key_not_allowed", lambda: self.adapter.key(key, self.snap()))
        self.assertEqual(self.native.events, [])

    def test_final_guard_rechecks_geometry_after_parent_gate_wait(self):
        token = self.snap()
        def moved():
            self.native.info = replace(self.native.info, left=123)
        self.rejects("geometry_changed", lambda: self.adapter.click(10, 10, token, guard=moved))
        self.assertEqual(self.native.events, [])

    def test_cancel_after_modifier_down_releases_ctrl_and_marks_partial(self):
        cancel = threading.Event()
        self.native.key_hook = lambda vk, scan, flags: cancel.set() if (vk, flags) == (17, 0) else None
        failure = self.rejects("stopped", lambda: self.adapter.replace_code("prts://123456", self.snap(), cancel_event=cancel))
        self.assertTrue(failure.details["partial_input"])
        self.assertEqual(self.native.events, [("key", 17, 0, 0), ("key", 17, 0, 2)])

    def test_parent_gate_wait_cannot_extend_screenshot_lifetime(self):
        token = self.snap()
        def delayed():
            self.now += 11
        self.rejects("observation_expired", lambda: self.adapter.click(10, 10, token, guard=delayed))
        self.assertEqual(self.native.events, [])

    def test_cancel_mid_text_releases_current_key_and_never_resumes_same_token(self):
        cancel = threading.Event()
        self.native.key_hook = lambda vk, scan, flags: cancel.set() if flags == 4 else None
        token = self.snap()
        self.rejects("stopped", lambda: self.adapter.replace_code("prts://123456", token, cancel_event=cancel))
        self.assertEqual(self.native.events[-2:], [("key", 0, ord("p"), 4), ("key", 0, ord("p"), 6)])
        self.rejects("observation_mismatch", lambda: self.adapter.replace_code("prts://123456", token))

    def test_parent_revocation_before_first_input_consumes_token(self):
        token = self.snap()
        def denied():
            raise MaaVisualError("revoked", "submission_rejected")
        self.rejects("submission_rejected", lambda: self.adapter.click(1, 1, token, guard=denied))
        self.rejects("observation_mismatch", lambda: self.adapter.click(1, 1, token))
        self.assertEqual(self.native.events, [])

    def test_discovery_only_returns_exact_maa_with_native_creation_time(self):
        self.native.other_windows = [replace(self.native.info, hwnd=999, executable=r"C:\other\MAA.exe"),
                                     replace(self.native.info, hwnd=998, executable=r"D:\Arknights.exe")]
        result = discover_maa_windows(backend=self.native)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["creation_time"], 333)
        self.assertEqual(self.native.events, [])


if __name__ == "__main__":
    unittest.main()

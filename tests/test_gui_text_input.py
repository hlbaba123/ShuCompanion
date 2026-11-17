"""MAA keyboard guards run against a fake desktop; no real input is sent."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest

from shu.windows import WindowController, WindowError, WindowInfo

try:
    from shu.gui_text_input import guarded_key, guarded_type, confirm_maa_dialog
except ImportError:
    guarded_key = guarded_type = confirm_maa_dialog = None


class FakeDesktop:
    def __init__(self):
        self.info = WindowInfo(101, 202, 'MAA', 10, 20, 900, 600,
                               True, False, r'D:\MAA\MAA.exe', 'MAA')
        self.windows = {}
        self.owners = {}

    def get_window(self, hwnd):
        return self.info if hwnd == self.info.hwnd else self.windows.get(hwnd)

    def capture(self, info, path):
        path.write_bytes(b'fake evidence')
        return {'method': 'fake', 'width': info.width, 'height': info.height}

    def get_owner(self, hwnd):
        return self.owners.get(hwnd, 0)


class FakeKeyboard:
    def __init__(self):
        self.events = []
        self.hook = None

    def send_keyboard(self, vk, scan, flags):
        self.events.append((vk, scan, flags))
        if self.hook:
            self.hook(vk, scan, flags)


class GuiTextInputGuards(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(guarded_type), 'MAA guarded keyboard module is missing')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.desktop = FakeDesktop()
        self.keyboard = FakeKeyboard()
        self.now = 100.
        self.controller = WindowController(Path(self.temp.name), backend=self.desktop,
            clock=lambda: self.now + 1000, monotonic=lambda: self.now, prefer_maafw=False)
        self.controller._gui_text_input_backend = self.keyboard
        self.controller.bind(101)

    def ready(self):
        observation = self.controller.capture()
        self.controller.set_input_enabled(True)
        return observation['id']

    def reject(self, code, operation):
        with self.assertRaises(WindowError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.keyboard.events, [])

    def type(self, text, observation_id):
        return guarded_type(self.controller, text, observation_id, allowed_texts={'TL-2', '1'})

    def test_input_disabled_rejects_whitelisted_text(self):
        observation = self.controller.capture()
        self.reject('input_disabled', lambda: self.type('TL-2', observation['id']))

    def test_non_whitelisted_text_is_rejected_before_any_input(self):
        observation_id = self.ready()
        self.reject('text_not_allowed', lambda: self.type('anything else', observation_id))

    def test_oversized_and_control_text_cannot_be_whitelisted(self):
        observation_id = self.ready()
        for text in ('a' * 513, 'TL-2\n', ''):
            self.reject('invalid_text', lambda: guarded_type(self.controller, text,
                observation_id, allowed_texts={text}))

    def test_expired_observation_rejects_keyboard(self):
        observation_id = self.ready()
        self.now += 6
        self.reject('observation_expired', lambda: self.type('TL-2', observation_id))

    def test_arknights_executable_is_rejected(self):
        self.desktop.info = replace(self.desktop.info, executable=r'D:\Arknights.exe')
        observation_id = self.ready()
        self.reject('maa_scope_required', lambda: self.type('TL-2', observation_id))

    def test_stopped_and_cancelled_operations_never_send_input(self):
        observation_id = self.ready()
        cancel = threading.Event(); cancel.set()
        self.reject('stopped', lambda: guarded_key(self.controller, 'ENTER', observation_id, cancel))
        self.controller.set_stop()
        self.reject('stopped', lambda: self.type('TL-2', observation_id))

    def test_stale_identity_geometry_and_focus_are_rejected(self):
        for updates, code in (({'pid': 999}, 'window_missing'),
                              ({'left': 50}, 'geometry_changed'),
                              ({'foreground': False}, 'not_foreground')):
            original = self.desktop.info
            observation_id = self.ready()
            self.desktop.info = replace(original, **updates)
            self.reject(code, lambda: self.type('TL-2', observation_id))
            self.desktop.info = original
            self.controller.bind(original.hwnd)

    def test_successful_text_consumes_evidence_and_requests_visual_verification(self):
        observation_id = self.ready()
        result = self.type('TL-2', observation_id)
        self.assertTrue(result['verification_required'])
        self.assertEqual(result['characters'], 4)
        self.assertEqual([scan for vk, scan, flags in self.keyboard.events if flags == 4],
                         list(map(ord, 'TL-2')))
        self.keyboard.events.clear()
        self.reject('observation_mismatch', lambda: self.type('1', observation_id))

    def test_only_fixed_key_names_are_accepted(self):
        observation_id = self.ready()
        for key in ('CTRL+V', 'WIN', 'ALT+F4', 'enter'):
            self.reject('key_not_allowed', lambda: guarded_key(self.controller, key, observation_id))

    def test_ctrl_modifier_is_released_if_cancelled_after_modifier_down(self):
        observation_id = self.ready()
        self.keyboard.hook = lambda vk, scan, flags: self.controller.set_stop() if (vk, flags) == (0x11, 0) else None
        with self.assertRaises(WindowError) as caught:
            guarded_key(self.controller, 'CTRL+A', observation_id)
        self.assertEqual(caught.exception.code, 'stopped')
        self.assertEqual(self.keyboard.events, [(0x11, 0, 0), (0x11, 0, 2)])

    def test_permission_rejection_precedes_send_input(self):
        observation_id = self.ready()
        def no_permission(info):
            raise WindowError('fixture', 'integrity_mismatch')
        self.controller._require_input_permission = no_permission
        self.reject('integrity_mismatch', lambda: guarded_key(self.controller, 'ENTER', observation_id))

    def test_text_cancellation_stops_before_next_character_and_releases_current(self):
        observation_id = self.ready()
        self.keyboard.hook = lambda vk, scan, flags: self.controller.set_stop() if flags == 4 else None
        with self.assertRaises(WindowError):
            self.type('TL-2', observation_id)
        self.assertEqual(self.keyboard.events, [(0, ord('T'), 4), (0, ord('T'), 6)])

    def test_only_confirmed_same_pid_owned_dialog_can_use_inaccessible_executable(self):
        main = self.desktop.info
        dialog = replace(main, hwnd=303, executable='', window_class='#32770')
        self.desktop.windows[303] = dialog
        self.desktop.owners[303] = main.hwnd
        self.controller.bind(303)
        observation_id = self.ready()
        self.reject('maa_scope_required', lambda: self.type('1', observation_id))
        confirm_maa_dialog(self.controller, 303, maa_hwnd=101)
        result = self.type('1', observation_id)
        self.assertEqual(result['window_id'], 303)

    def test_same_pid_without_owner_link_is_not_a_confirmed_dialog(self):
        self.desktop.windows[303] = replace(self.desktop.info, hwnd=303, executable='')
        self.reject('maa_dialog_unconfirmed', lambda: confirm_maa_dialog(self.controller, 303, maa_hwnd=101))


if __name__ == '__main__':
    unittest.main()

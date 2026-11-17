"""Guard tests use a fake desktop; they never send real OS input."""

from dataclasses import replace
from contextlib import nullcontext
import ctypes
import os
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace
import zlib

from shu.windows import WindowController, WindowError, WindowInfo, _WgcCapture, _crop_bgra, _encode_png, _NativeWindows


class FakeDesktop:
    def __init__(self):
        self.info = WindowInfo(101, 202, "明日方舟", -500, 150, 1280, 720, True, False)
        self.clicks = []
        self.capture_hook = None
        self.get_hook = None
        self.get_calls = 0
        self.focused = []
        self.drags = []
        self.drag_hook = None

    def focus_window(self, hwnd):
        self.focused.append(hwnd)
        self.info = replace(self.info, foreground=True, minimized=False)

    def list_windows(self):
        return [self.info, replace(self.info, hwnd=102, title="Arknights Launcher"),
                replace(self.info, hwnd=103, title="文档")]

    def get_window(self, hwnd):
        self.get_calls += 1
        if self.get_hook:
            self.get_hook(self)
        return self.info if self.info and hwnd == self.info.hwnd else None

    def capture(self, info, path):
        # The guard tests only need an artifact; PNG encoding is tested separately.
        path.write_bytes(b"fake-window-capture")
        if self.capture_hook:
            self.capture_hook(self)
        return {"method": "fake_foreground", "warnings": [],
                "width": info.width, "height": info.height}

    def click(self, x, y):
        self.clicks.append((x, y))

    def drag(self,x1,y1,x2,y2,duration,guard):
        guard()
        if self.drag_hook:self.drag_hook(self)
        guard()
        self.drags.append((x1,y1,x2,y2,duration))


class WindowGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.desktop = FakeDesktop()
        self.now = 100.0
        self.controller = WindowController(Path(self.temp.name), backend=self.desktop,
                                           clock=lambda: 1000 + self.now,
                                           monotonic=lambda: self.now,
                                           prefer_maafw=False)
        self.controller.bind(101)

    def ready(self):
        observation = self.controller.capture()
        self.controller.set_input_enabled(True)
        return observation

    def assert_rejected(self, code, operation):
        with self.assertRaises(WindowError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.desktop.clicks, [])

    def test_drag_uses_bound_client_coordinates_and_consumes_observation(self):
        frame=self.ready()
        result=self.controller.controlled_drag(10,20,30,40,.2,frame['id'])
        self.assertEqual(self.desktop.drags,[(-490,170,-470,190,.2)])
        self.assertTrue(result['verification_required'])
        self.assert_rejected('observation_mismatch',lambda:self.controller.controlled_drag(10,20,30,40,.2,frame['id']))

    def test_drag_checks_stop_and_focus_during_motion(self):
        for hook,code in ((lambda desktop:self.controller.set_stop(),'stopped'),
                          (lambda desktop:setattr(desktop,'info',replace(desktop.info,foreground=False)),'not_foreground')):
            self.desktop.info=replace(self.desktop.info,foreground=True)
            self.controller.reset_stop();frame=self.ready();self.desktop.drag_hook=hook
            self.assert_rejected(code,lambda:self.controller.controlled_drag(10,20,30,40,.2,frame['id']))
            self.assertEqual(self.desktop.drags,[])

    def test_native_enumeration_skips_own_gui_before_synchronous_title_queries(self):
        native=object.__new__(_NativeWindows)
        native.w=SimpleNamespace(DWORD=ctypes.c_ulong)
        native._physical_pixels=lambda:nullcontext()
        def identify(hwnd,pointer):pointer._obj.value=os.getpid()
        def forbidden_title(hwnd):raise AssertionError('Own GUI title query can deadlock')
        native.user=SimpleNamespace(IsWindow=lambda hwnd:True,IsWindowVisible=lambda hwnd:True,
            GetWindowThreadProcessId=identify,GetWindowTextLengthW=forbidden_title)
        self.assertIsNone(native.get_window(999))

    def test_native_click_spans_game_frames_and_always_releases_button(self):
        native=object.__new__(_NativeWindows);events=[]
        native._pointer=lambda x,y,flags:events.append(('input',flags))
        native._verify_pointer_target=lambda x,y:None
        native._mouse_button_down=lambda:False
        with patch('shu.windows.time.sleep',side_effect=lambda delay:events.append(('wait',delay))):
            native.click(1,2)
        self.assertEqual(events,[('input',0),('wait',.016),('input',2),('wait',.060),('input',4)])
        def fail_on_down(x,y,flags):
            events.append(('input',flags))
            if flags==2:raise WindowError('fixture','input_failed')
        events.clear();native._pointer=fail_on_down
        with patch('shu.windows.time.sleep'),self.assertRaises(WindowError):native.click(1,2)
        self.assertEqual(events[-1],('input',4))

    def test_native_pointer_uses_physical_position_and_button_only_events(self):
        native=object.__new__(_NativeWindows)
        native._physical_pixels=lambda:nullcontext()
        native._virtual_rect=lambda:(0,0,1920,1080)
        native.user=SimpleNamespace(SetCursorPos=Mock(return_value=1),SendInput=Mock(return_value=1))
        native._pointer(100,200,0)
        native.user.SetCursorPos.assert_called_once_with(100,200)
        native.user.SendInput.assert_not_called()
        native._pointer(100,200,2)
        event=native.user.SendInput.call_args.args[1]._obj
        self.assertEqual((event.type,event.value.mi.dx,event.value.mi.dy,event.value.mi.dwFlags),(0,0,0,2))

    def test_native_click_does_not_press_when_position_or_local_guard_is_invalid(self):
        native=object.__new__(_NativeWindows);events=[]
        native._pointer=lambda x,y,flags:events.append(flags)
        native._verify_pointer_target=Mock(side_effect=WindowError('fixture','pointer_displaced'))
        with patch('shu.windows.time.sleep'),self.assertRaises(WindowError):native.click(1,2)
        self.assertEqual(events,[0])
        events.clear();native._verify_pointer_target=lambda x,y:None
        def stopped():raise WindowError('fixture','stopped')
        with patch('shu.windows.time.sleep'),self.assertRaises(WindowError):native.click_guarded(1,2,stopped)
        self.assertEqual(events,[0])

    def test_title_filter_is_candidate_only(self):
        self.assertEqual([w.hwnd for w in self.controller.list_windows()], [101, 102])
        self.assertEqual(len(self.controller.list_windows(False)), 3)
        observation = self.controller.capture()
        self.assertEqual(observation["state"], "unknown")
        self.assertEqual(observation["coordinate_space"], "client_physical_pixels")
        self.assertTrue(Path(observation["path"]).is_file())

    def test_focus_for_capture_only_activates_bound_window_and_never_enables_input(self):
        self.desktop.info = replace(self.desktop.info, foreground=False, minimized=True)
        focused = self.controller.focus_for_capture()
        self.assertTrue(focused.foreground)
        self.assertFalse(focused.minimized)
        self.assertEqual(self.desktop.focused, [101])
        self.assertEqual(self.desktop.clicks, [])
        self.assertFalse(self.controller.input_enabled)

    def test_background_window_capture_does_not_authorize_background_input(self):
        self.desktop.supports_background_capture = True
        self.desktop.info = replace(self.desktop.info, foreground=False)
        observation = self.controller.capture()
        self.assertEqual(observation["window_id"], 101)
        self.controller.set_input_enabled(True)
        self.assert_rejected("not_foreground", lambda: self.controller.controlled_click(1, 2, observation["id"]))

    def test_strict_background_uses_hwnd_backend_without_focus(self):
        self.desktop.supports_background_capture = True
        self.desktop.info = replace(self.desktop.info, foreground=False)
        calls = []
        def capture_background(info, path):
            calls.append(info.hwnd);path.write_bytes(b'window surface')
            return {'method':'windows_graphics_capture','requires_foreground':False}
        self.desktop.capture_background = capture_background
        observation = self.controller.capture(background_only=True)
        self.assertEqual(calls,[101]);self.assertEqual(self.desktop.focused,[])
        self.assertTrue(observation['background_capture'])
        self.assertTrue(self.controller.discover_capabilities()['background_capture_verified'])
        self.assertFalse(self.controller.input_enabled)

    def test_strict_background_failure_never_falls_back_to_screen_crop(self):
        self.desktop.supports_background_capture = True
        self.desktop.info = replace(self.desktop.info, foreground=False)
        def failed_background(info, path):
            raise WindowError('WGC denied','wgc_session_failed')
        self.desktop.capture_background = failed_background
        self.assert_rejected('wgc_session_failed',lambda:self.controller.capture(background_only=True))
        self.assertEqual(self.desktop.focused,[])
        self.assertEqual(list((Path(self.temp.name)/'captures').glob('*.png')),[])

    def test_strict_background_rejects_foreground_only_backend(self):
        self.assert_rejected('background_capture_unavailable',lambda:self.controller.capture(background_only=True))
        self.desktop.supports_background_capture=True
        self.desktop.capture_background=lambda info,path:(path.write_bytes(b'wrong pixels') or {})
        def wrong_mode(info,path):
            path.write_bytes(b'wrong pixels');return {'method':'gdi','requires_foreground':True}
        self.desktop.capture_background=wrong_mode
        self.assert_rejected('background_capture_unavailable',lambda:self.controller.capture(background_only=True))

    def test_game_identity_prefers_executable_or_unity_class_over_browser_title(self):
        actual=replace(self.desktop.info, executable=r'D:\Arknights\Arknights.exe',window_class='UnityWndClass')
        browser=replace(self.desktop.info,hwnd=999,executable=r'C:\Chrome\chrome.exe',window_class='Chrome_WidgetWin_1')
        self.desktop.list_windows=lambda:[browser,actual]
        self.assertTrue(actual.is_game_client);self.assertFalse(browser.is_game_client)
        self.assertEqual(self.controller.list_windows(),[actual])

    def test_capture_does_not_hold_state_lock_and_rebind_invalidates_frame(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def hold(_desktop):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test capture was not released")
        self.desktop.capture_hook = hold
        def capture():
            try:
                self.controller.capture()
            except WindowError as exc:
                errors.append(exc.code)
        worker = threading.Thread(target=capture)
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertIsNotNone(self.controller.bound_window())
        self.controller.bind(101)
        self.controller.set_stop()
        self.assertFalse(self.controller.input_enabled)
        release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ["binding_changed"])
        self.assertEqual(list((Path(self.temp.name) / "captures").glob("*.png")), [])

    def test_concurrent_capture_is_rejected_without_waiting(self):
        self.controller._capture_lock.acquire()
        try:
            self.assert_rejected("capture_in_progress", self.controller.capture)
        finally:
            self.controller._capture_lock.release()

    def test_input_disabled_by_default(self):
        observation = self.controller.capture()
        self.assert_rejected("input_disabled", lambda: self.controller.controlled_click(1, 2, observation["id"]))

    def test_negative_monitor_origin_maps_client_coordinates(self):
        observation = self.ready()
        result = self.controller.controlled_click(100, 200, observation["id"])
        self.assertEqual(self.desktop.clicks, [(-400, 350)])
        self.assertTrue(result["verification_required"])
        self.assertEqual(result["state"], "unverified")
        with self.assertRaises(WindowError) as caught:
            self.controller.controlled_click(1, 2, observation["id"])
        self.assertEqual(caught.exception.code, "observation_mismatch")
        self.assertEqual(len(self.desktop.clicks), 1)

    def test_bounds_and_coordinate_types(self):
        observation = self.ready()
        for x, y, code in [(-1, 1, "out_of_bounds"), (1280, 1, "out_of_bounds"),
                           (1, 720, "out_of_bounds"), (1.0, 2, "invalid_coordinates"),
                           (True, 2, "invalid_coordinates")]:
            with self.subTest(x=x, y=y):
                self.assert_rejected(code, lambda: self.controller.controlled_click(x, y, observation["id"]))

    def test_observation_expires_on_monotonic_clock(self):
        observation = self.ready()
        self.now += 5.001
        self.assert_rejected("observation_expired", lambda: self.controller.controlled_click(1, 2, observation["id"]))

    def test_capture_duration_counts_against_evidence_age(self):
        def slow_capture(_desktop):
            self.now += 5.1

        self.desktop.capture_hook = slow_capture
        observation = self.ready()
        self.assert_rejected("observation_expired", lambda: self.controller.controlled_click(1, 2, observation["id"]))

    def test_new_observation_invalidates_old_id(self):
        old = self.ready()
        self.controller.capture()
        self.assert_rejected("observation_mismatch", lambda: self.controller.controlled_click(1, 2, old["id"]))

    def test_window_move_requires_new_capture(self):
        observation = self.ready()
        self.desktop.info = replace(self.desktop.info, left=-400)
        self.assert_rejected("geometry_changed", lambda: self.controller.controlled_click(1, 2, observation["id"]))

    def test_final_window_check_rejects_resize(self):
        observation = self.ready()
        calls = self.desktop.get_calls

        def resize_on_final_check(desktop):
            if desktop.get_calls == calls + 2:
                desktop.info = replace(desktop.info, width=640)

        self.desktop.get_hook = resize_on_final_check
        self.assert_rejected("geometry_changed", lambda: self.controller.controlled_click(1, 2, observation["id"]))

    def test_focus_loss_blocks_capture_and_input(self):
        observation = self.ready()
        self.desktop.info = replace(self.desktop.info, foreground=False)
        self.assert_rejected("not_foreground", lambda: self.controller.controlled_click(1, 2, observation["id"]))
        self.assert_rejected("not_foreground", self.controller.capture)
        self.desktop.info = replace(self.desktop.info, foreground=True)
        self.assert_rejected("observation_mismatch", lambda: self.controller.controlled_click(1, 2, observation["id"]))

    def test_minimized_window_rejected(self):
        self.desktop.info = replace(self.desktop.info, minimized=True)
        self.assert_rejected("minimized", self.controller.capture)

    def test_capture_rechecks_focus_and_removes_failed_artifact(self):
        self.desktop.capture_hook = lambda desktop: setattr(desktop, "info", replace(desktop.info, foreground=False))
        self.assert_rejected("not_foreground", self.controller.capture)
        self.assertEqual(list((Path(self.temp.name) / "captures").glob("*.png")), [])

    def test_reused_hwnd_does_not_authorize_different_process(self):
        observation = self.ready()
        self.desktop.info = replace(self.desktop.info, pid=999)
        self.assert_rejected("window_missing", lambda: self.controller.controlled_click(1, 2, observation["id"]))
        self.assertFalse(self.controller.input_enabled)

    def test_stop_and_reset_require_explicit_reenable(self):
        observation = self.ready()
        self.controller.set_stop()
        self.assert_rejected("stopped", lambda: self.controller.controlled_click(1, 2, observation["id"]))
        self.assert_rejected("stopped", lambda: self.controller.set_input_enabled(True))
        self.controller.reset_stop()
        self.assert_rejected("input_disabled", lambda: self.controller.controlled_click(1, 2, observation["id"]))

    def test_external_cancel_blocks_input(self):
        observation = self.ready()
        cancelled = threading.Event()
        cancelled.set()
        self.assert_rejected("stopped", lambda: self.controller.controlled_click(1, 2, observation["id"], cancelled))

    def test_rebind_disables_input_and_invalidates_observation(self):
        observation = self.ready()
        self.controller.bind(101)
        self.assertFalse(self.controller.input_enabled)
        self.controller.set_input_enabled(True)
        self.assert_rejected("observation_mismatch", lambda: self.controller.controlled_click(1, 2, observation["id"]))


class PngEncoding(unittest.TestCase):
    def test_wgc_client_crop_preserves_row_order_and_checks_bounds(self):
        pixels=bytes(range(48)) # 4x3 BGRA
        self.assertEqual(_crop_bgra(pixels,4,3,1,1,2,2),pixels[20:28]+pixels[36:44])
        with self.assertRaises(WindowError):_crop_bgra(pixels,4,3,-1,0,2,2)
        with self.assertRaises(WindowError):_crop_bgra(pixels,4,3,3,0,2,2)

    def test_wgc_callback_copies_frame_and_targets_exact_hwnd(self):
        captured={}
        class Control:
            def stop(self):captured['stopped']=True
            def wait(self):captured['waited']=True
        class Capture:
            def __init__(self,**kwargs):captured['kwargs']=kwargs
            def event(self,fn):setattr(self,fn.__name__,fn);return fn
            def start_free_threaded(self):
                frame=SimpleNamespace(width=2,height=1,frame_buffer=SimpleNamespace(tobytes=lambda:bytes([0,0,255,255,0,255,0,255])))
                self.on_frame_arrived(frame,Control());return Control()
        info=WindowInfo(456,789,'game',0,0,2,1,False,False)
        with tempfile.TemporaryDirectory() as directory, patch.dict('sys.modules',{'windows_capture':SimpleNamespace(WindowsCapture=Capture)}):
            output=Path(directory)/'frame.png'
            result=_WgcCapture().capture(info,output,lambda *_:(0,0))
            self.assertTrue(output.read_bytes().startswith(b'\x89PNG'))
        self.assertEqual(captured['kwargs'],{'window_hwnd':456,'cursor_capture':False,'draw_border':None})
        self.assertTrue(captured['stopped']);self.assertTrue(captured['waited'])
        self.assertFalse(result['requires_foreground'])

    def test_top_down_bgra_becomes_png_rgb(self):
        image = _encode_png(2, 1, bytes([0, 0, 255, 255, 0, 255, 0, 0]))
        self.assertEqual(image[:8], b"\x89PNG\r\n\x1a\n")
        offset = 8
        compressed = b""
        while offset < len(image):
            length = struct.unpack(">I", image[offset:offset + 4])[0]
            kind = image[offset + 4:offset + 8]
            payload = image[offset + 8:offset + 8 + length]
            if kind == b"IHDR":
                self.assertEqual(struct.unpack(">II", payload[:8]), (2, 1))
            if kind == b"IDAT":
                compressed += payload
            offset += length + 12
        self.assertEqual(zlib.decompress(compressed), bytes([0, 255, 0, 0, 0, 255, 0]))

    def test_short_image_rejected(self):
        with self.assertRaises(WindowError):
            _encode_png(2, 2, b"\x00")


if __name__ == "__main__":
    unittest.main()

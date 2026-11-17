"""Only verified nonclient caption input may acquire MAA foreground."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
import unittest
from shu.maa_visual_native import _NativeMaaBackend
from shu.windows import WindowError


class CaptionActivationTests(unittest.TestCase):
    def setUp(self):
        self.native=SimpleNamespace()
        self.native.focus_window=Mock(side_effect=WindowError('denied','focus_denied'))
        self.native._physical_pixels=nullcontext
        self.user=SimpleNamespace(**{key:Mock() for key in ('SetWindowPos','SendMessageTimeoutW',
            'WindowFromPoint','GetAncestor','GetAsyncKeyState','GetWindowRect','GetForegroundWindow','GetPhysicalCursorPos')})
        self.native.user=self.user
        self.user.SetWindowPos.return_value=True
        self.user.GetAsyncKeyState.return_value=0
        self.user.GetForegroundWindow.return_value=111
        self.user.WindowFromPoint.return_value=111
        self.user.GetAncestor.return_value=111
        def rectangle(hwnd,out):
            r=out._obj;r.left=20;r.top=40;r.right=1020;r.bottom=840;return True
        self.user.GetWindowRect.side_effect=rectangle
        self.hit=2
        def hit_test(*args):args[-1]._obj.value=self.hit;return 1
        self.user.SendMessageTimeoutW.side_effect=hit_test
        self.point=None
        self.native._pointer=Mock(side_effect=lambda x,y,flags:setattr(self,'point',(x,y)))
        def cursor(out):out._obj.x,out._obj.y=self.point;return True
        self.user.GetPhysicalCursorPos.side_effect=cursor
        self.backend=object.__new__(_NativeMaaBackend);self.backend.native=self.native
        self.backend._send_mouse=Mock(return_value=2)

    def test_denial_uses_one_guarded_caption_click_not_content_or_keys(self):
        guard=Mock()
        result=self.backend.focus_guarded(111,guard)
        self.assertEqual(result['focus_method'],'verified_caption')
        self.assertEqual(self.point,(320,52))
        self.backend._send_mouse.assert_called_once_with((2,0),(4,0))
        self.assertGreaterEqual(guard.call_count,2)
        self.user.SetWindowPos.assert_called_once_with(111,0,0,0,0,0,0x0253)

    def test_noncaption_or_other_window_never_receives_click(self):
        for mode in ('client','covered'):
            with self.subTest(mode=mode):
                self.hit=1 if mode=='client' else 2
                self.user.GetAncestor.return_value=222 if mode=='covered' else 111
                with self.assertRaises(WindowError):self.backend.focus_guarded(111,Mock())
                self.backend._send_mouse.assert_not_called()

    def test_cancel_after_placement_and_active_mouse_or_modifier_never_press(self):
        guard=Mock(side_effect=[None,RuntimeError('cancelled')])
        with self.assertRaises(RuntimeError):self.backend.focus_guarded(111,guard)
        self.backend._send_mouse.assert_not_called()
        self.user.GetAsyncKeyState.return_value=0x8000
        with self.assertRaises(WindowError):self.backend.focus_guarded(111,Mock())
        self.backend._send_mouse.assert_not_called()

    def test_existing_foreground_uses_no_pointer_input(self):
        self.native.focus_window.side_effect=None
        self.assertEqual(self.backend.focus_guarded(111,Mock())['focus_method'],'set_foreground')
        self.backend._send_mouse.assert_not_called()
        self.native._pointer.assert_not_called()

    def test_displaced_caption_cursor_never_clicks_even_within_three_pixels(self):
        def displaced(out):out._obj.x,out._obj.y=self.point[0]+3,self.point[1];return True
        self.user.GetPhysicalCursorPos.side_effect=displaced
        with self.assertRaises(WindowError):self.backend.focus_guarded(111,Mock())
        self.backend._send_mouse.assert_not_called()


if __name__=='__main__':unittest.main()

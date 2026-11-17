"""Release-only cleanup tests: injected Win32 facade, never real input."""
import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from shu.drag_button_cleanup import ReleaseOnlyLeftButton, DragCleanupError


class User:
    def __init__(self):
        self.names={10:'Default',20:'Default',30:'WinSta0'}
        self.input_desktop=10;self.thread_desktop=20;self.station=30
        self.foreground=101;self.pid=202;self.down=False;self.known=True
        self.events=[];self.closed=[];self.accepted=1;self.release_effect=True
        self.swapped=False;self.on_state=None
    def OpenInputDesktop(self,flags,inherit,access):
        self.events.append(('open_desktop',flags,inherit,access));return self.input_desktop
    def CloseDesktop(self,handle):self.closed.append(handle);return True
    def GetThreadDesktop(self,thread):return self.thread_desktop
    def GetProcessWindowStation(self):return self.station
    def GetUserObjectInformationW(self,handle,index,buffer,size,needed):
        if not self.known:return False
        name=self.names.get(handle)
        if name is None:return False
        needed._obj.value=(len(name)+1)*ctypes.sizeof(ctypes.c_wchar)
        if buffer is None:return False
        buffer.value=name;return True
    def GetForegroundWindow(self):return self.foreground
    def GetWindowThreadProcessId(self,hwnd,pid):pid._obj.value=self.pid;return 303
    def GetSystemMetrics(self,index):
        assert index==23
        return int(self.swapped)
    def GetAsyncKeyState(self,key):
        assert key==1
        if self.on_state:self.on_state()
        return 0x8000 if self.down else 0
    def SendInput(self,count,events,size):
        event=events._obj
        self.events.append(('send',count,event.type,event.mi.dx,event.mi.dy,
            event.mi.mouseData,event.mi.dwFlags,event.mi.time,event.mi.dwExtraInfo,size))
        if self.accepted and self.release_effect:self.down=False
        return self.accepted


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.user=User();self.now=0.
        self.permission=Mock(return_value={'known':True,'input_allowed':True,
            'own_integrity':12288,'target_integrity':8192})
        self.native=SimpleNamespace(native=SimpleNamespace(user=self.user,
            kernel=SimpleNamespace(GetCurrentThreadId=lambda:5)),permission=self.permission)
        self.cleanup=ReleaseOnlyLeftButton(native=self.native,clock=lambda:self.now,sleep=self.sleep)
    def sleep(self,value):self.now+=value
    def sent(self):return [e for e in self.user.events if e[0]=='send']
    def test_known_active_desktop_can_report_up_without_any_input(self):
        self.assertIs(self.cleanup.is_down(),False)
        self.assertEqual(self.sent(),[])
        self.assertEqual(self.user.closed,[10,10])
    def test_release_is_one_left_up_event_without_motion_focus_or_key(self):
        self.user.down=True
        result=self.cleanup.release_after_exit()
        self.assertEqual(result,{'button_released':True,'release_only':True,'input_sent':True})
        self.assertEqual(len(self.sent()),1)
        self.assertEqual(self.sent()[0][1:9],(1,0,0,0,0,4,0,0))
    def test_already_up_does_not_send_left_up(self):
        self.assertEqual(self.cleanup.release_after_exit(),
            {'button_released':True,'release_only':True,'input_sent':False})
        self.assertEqual(self.sent(),[])
    def test_getasynckeystate_zero_on_inaccessible_desktop_is_not_up_proof(self):
        self.user.input_desktop=0
        with self.assertRaises(DragCleanupError):self.cleanup.is_down()
        self.assertEqual(self.sent(),[])
    def test_secure_or_noninteractive_desktop_never_releases(self):
        for handle,name in ((10,'Winlogon'),(20,'Other'),(30,'Service-0x1')):
            with self.subTest(name=name):
                old=self.user.names[handle];self.user.names[handle]=name
                with self.assertRaises(DragCleanupError):self.cleanup.release_after_exit()
                self.user.names[handle]=old
        self.assertEqual(self.sent(),[])
    def test_unknown_object_name_closes_handle_and_fails_closed(self):
        self.user.known=False
        with self.assertRaises(DragCleanupError):self.cleanup.is_down()
        self.assertEqual(self.user.closed,[10]);self.assertEqual(self.sent(),[])
    def test_unknown_or_higher_foreground_integrity_never_reports_up(self):
        for report in ({'known':False,'input_allowed':False},
                       {'known':True,'input_allowed':False,'own_integrity':8192,'target_integrity':12288}):
            with self.subTest(report=report):
                self.permission.return_value=report
                with self.assertRaises(DragCleanupError):self.cleanup.is_down()
        self.assertEqual(self.sent(),[])
    def test_missing_foreground_cannot_prove_desktop_input_access(self):
        self.user.foreground=0
        with self.assertRaises(DragCleanupError):self.cleanup.is_down()
        self.assertEqual(self.sent(),[])
    def test_foreground_change_during_query_is_unverified(self):
        self.user.on_state=lambda:setattr(self.user,'foreground',404)
        with self.assertRaises(DragCleanupError):self.cleanup.is_down()
        self.assertEqual(self.sent(),[])
    def test_rejected_release_is_not_claimed_success(self):
        self.user.down=True;self.user.accepted=0
        self.assertEqual(self.cleanup.release_after_exit(),
            {'button_released':False,'release_only':True,'input_sent':False})
        self.assertEqual(len(self.sent()),1)
    def test_still_down_after_release_keeps_unverified_and_never_retries(self):
        self.user.down=True;self.user.release_effect=False
        result=self.cleanup.release_after_exit()
        self.assertFalse(result['button_released']);self.assertTrue(result['input_sent'])
        self.assertEqual(len(self.sent()),1);self.assertLessEqual(self.now,.21)
    def test_swapped_buttons_fail_closed_without_releasing_wrong_physical_button(self):
        self.user.swapped=True
        with self.assertRaises(DragCleanupError):self.cleanup.is_down()
        self.assertEqual(self.sent(),[])


if __name__=='__main__':unittest.main()

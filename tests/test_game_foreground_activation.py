"""Official activation sequence only; no actual Windows input in these tests."""
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from shu.game_foreground_activation import activate_game
from shu.windows import WindowError


class User:
    def __init__(self):
        self.foreground=999;self.topmost=False;self.minimized=False
        self.events=[];self.alive=True;self.pid=202;self.tid=303
        self.acquire_on=None;self.fail_position=False;self.position_hook=None
    def IsWindow(self,hwnd):return self.alive
    def IsIconic(self,hwnd):return self.minimized
    def GetWindowLongPtrW(self,hwnd,index):
        assert index==-20
        return 8 if self.topmost else 0
    def GetWindowThreadProcessId(self,hwnd,pid):pid._obj.value=self.pid;return self.tid
    def GetForegroundWindow(self):return self.foreground
    def ShowWindow(self,hwnd,mode):
        self.events.append(('restore',hwnd,mode));self.minimized=False;return False
    def SetForegroundWindow(self,hwnd):
        self.events.append(('foreground',hwnd))
        if self.acquire_on=='foreground':self.foreground=hwnd;return True
        return False
    def SetWindowPos(self,hwnd,z,x,y,w,h,flags):
        self.events.append(('position',hwnd,z,x,y,w,h,flags))
        if self.fail_position:return False
        if z==-1:self.topmost=True
        elif z==-2:self.topmost=False
        if self.acquire_on==z:self.foreground=hwnd
        if self.position_hook:self.position_hook(z)
        return True


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.user=User();self.native=SimpleNamespace(user=self.user,_physical_pixels=nullcontext)
        self.guards=0
        self.sleep=patch('shu.game_foreground_activation.time.sleep').start()
        self.addCleanup(patch.stopall)
    def guard(self):self.guards+=1
    def activate(self,guard=None):return activate_game(self.native,101,guard or self.guard)
    def test_existing_foreground_does_not_change_z_order_or_send_input(self):
        self.user.foreground=101
        result=self.activate()
        self.assertIs(result['focus_acquired'],True)
        self.assertEqual(result['focus_method'],'maafw_official_activation')
        self.assertIs(result['caption_click'],False)
        self.assertEqual(self.user.events,[])
    def test_official_top_then_foreground_sequence_with_actual_readback(self):
        self.user.acquire_on='foreground'
        result=self.activate()
        self.assertIs(result['focus_acquired'],True)
        self.assertEqual(self.user.events,[('position',101,0,0,0,0,0,0x43),('foreground',101)])
        self.assertGreaterEqual(self.guards,4)
    def test_window_z_order_alone_is_not_claimed_as_foreground(self):
        result=self.activate()
        self.assertIs(result['focus_acquired'],False)
        self.assertEqual([e[2] for e in self.user.events if e[0]=='position'],[0,-1,-2])
        self.assertFalse(self.user.topmost)
    def test_temporary_topmost_fallback_can_acquire_focus_and_restores_style(self):
        self.user.acquire_on=-1
        result=self.activate()
        self.assertIs(result['focus_acquired'],True)
        self.assertFalse(self.user.topmost)
        self.assertEqual(self.sleep.call_args_list[-1].args,(.005,))
    def test_original_topmost_is_not_automatically_removed(self):
        self.user.topmost=True
        result=self.activate()
        self.assertIs(result['focus_acquired'],False)
        self.assertTrue(self.user.topmost)
        self.assertNotIn(-2,[e[2] for e in self.user.events if e[0]=='position'])
    def test_every_mutation_has_guard_before_and_after(self):
        order=[]
        def guard():order.append('guard')
        original=self.user.SetWindowPos
        def position(*args):order.append('mutation');return original(*args)
        self.user.SetWindowPos=position
        original_foreground=self.user.SetForegroundWindow
        def foreground(*args):order.append('mutation');return original_foreground(*args)
        self.user.SetForegroundWindow=foreground
        self.activate(guard)
        for i,event in enumerate(order):
            if event=='mutation':self.assertEqual((order[i-1],order[i+1]),('guard','guard'))
    def test_revocation_after_temporary_topmost_restores_without_content_input(self):
        revoked=False
        def hook(z):
            nonlocal revoked
            if z==-1:revoked=True
        self.user.position_hook=hook
        def guard():
            if revoked:raise RuntimeError('revoked')
        with self.assertRaisesRegex(RuntimeError,'revoked'):self.activate(guard)
        self.assertFalse(self.user.topmost)
        self.assertEqual(self.user.events[-1],('position',101,-2,0,0,0,0,0x13))
    def test_revocation_before_mutation_never_touches_window(self):
        def guard():raise RuntimeError('revoked')
        with self.assertRaisesRegex(RuntimeError,'revoked'):self.activate(guard)
        self.assertEqual(self.user.events,[])
    def test_changed_pid_during_topmost_skips_cleanup_of_new_process_window(self):
        def hook(z):
            if z==-1:self.user.pid=404
        self.user.position_hook=hook
        with self.assertRaises(WindowError) as got:self.activate()
        self.assertEqual(got.exception.code,'game_binding_changed')
        self.assertNotIn(-2,[e[2] for e in self.user.events if e[0]=='position'])
    def test_invalid_hwnd_never_mutates(self):
        self.user.alive=False
        with self.assertRaises(WindowError):self.activate()
        self.assertEqual(self.user.events,[])
    def test_minimized_window_is_restored_before_official_activation(self):
        self.user.minimized=True;self.user.acquire_on='foreground'
        self.assertTrue(self.activate()['focus_acquired'])
        self.assertEqual(self.user.events[0],('restore',101,9))
    def test_os_position_failure_is_not_reported_as_success(self):
        self.user.fail_position=True
        with self.assertRaises(WindowError) as got:self.activate()
        self.assertEqual(got.exception.code,'focus_activation_failed')
        self.assertFalse(got.exception.details['input_sent'])


if __name__=='__main__':unittest.main()

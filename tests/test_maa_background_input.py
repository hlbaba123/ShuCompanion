"""Fixed SDK adapter tests. These never import the native runtime or send input."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest

from shu.maa_background_input import MaaBackgroundInput, MaaBackgroundInputError, MaaClientButtonInput


IDENTITY = dict(hwnd=101, pid=202, creation_time=303,
                executable=r'D:\Games\Arknights.exe')
GEOMETRY = dict(left=-600, top=20, width=1920, height=1080)


class Job:
    def __init__(self, done=True, succeeded=True, result=None):
        self.done, self.succeeded, self.result = done, succeeded, result
    def get(self): return self.result


class Image:
    shape = (1080, 1920, 3)


class Controller:
    def __init__(self):
        self.events=[]; self.resolution=(1920,1080); self.raw=True
        self.input_job=Job(); self.capture_job=Job(result=Image())
        self.connect_job=Job(); self.inactive_job=Job()
        self.on_capture=None; self.on_input=None
    def set_screenshot_use_raw_size(self, enabled):
        self.events.append(('raw',enabled)); return self.raw
    def post_connection(self): self.events.append(('connect',)); return self.connect_job
    def post_screencap(self):
        self.events.append(('capture',))
        if self.on_capture: self.on_capture()
        return self.capture_job
    def post_click(self,*args):
        self.events.append(('click',*args))
        if self.on_input: self.on_input()
        return self.input_job
    def post_swipe(self,*args): self.events.append(('swipe',*args)); return self.input_job
    def post_inactive(self): self.events.append(('inactive',)); return self.inactive_job


class BackgroundInputTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.c=Controller(); self.now=0.; self.gates=0; self.factory_calls=[]
        self.window={**IDENTITY,'client_rect':deepcopy(GEOMETRY),'minimized':False}
        def factory(**kwargs): self.factory_calls.append(kwargs); return self.c
        self.adapter=MaaBackgroundInput(IDENTITY,Path(self.temp.name),
            controller_factory=factory,check_window=lambda:deepcopy(self.window),
            clock=lambda:self.now,sleep=self.sleep)
    def sleep(self,seconds): self.now+=seconds
    def gate(self): self.gates+=1
    def click(self,**kwargs):
        return self.adapter.act('click',[(1437,831)],gate=self.gate,
            expected_geometry=GEOMETRY,**kwargs)
    def inputs(self): return [e for e in self.c.events if e[0] in ('click','swipe')]
    def test_raw_physical_client_coordinates_do_not_add_origin_or_scale(self):
        result=self.click()
        self.assertEqual(self.factory_calls,[dict(hWnd=101,screencap_method=16,
            mouse_method=1024,keyboard_method=2)])
        self.assertEqual(result['input_method'],'maafw_anchored_touch')
        self.assertEqual(self.c.events,[('raw',True),('connect',),('capture',),
            ('click',1437,831),('inactive',)])
        self.assertTrue(result['input_dispatched'])
        self.assertFalse(result['effect_verified'])
        self.assertGreater(self.gates,0)
    def test_second_click_recaptures_raw_dimensions(self):
        self.click(); self.click()
        self.assertEqual(len(self.factory_calls),1)
        self.assertEqual(self.c.events.count(('capture',)),2)
    def test_swipe_duration_seconds_becomes_exact_milliseconds(self):
        self.adapter.act('drag',[(100,200),(1200,200)],duration_seconds=.4,
            gate=self.gate,expected_geometry=GEOMETRY)
        self.assertEqual(self.inputs(),[('swipe',100,200,1200,200,400)])
    def test_raw_option_failure_never_falls_back_to_default_scale_or_seize(self):
        self.c.raw=False
        with self.assertRaises(MaaBackgroundInputError) as got:self.click()
        self.assertEqual(got.exception.code,'background_raw_size_unavailable')
        self.assertFalse(got.exception.details['input_sent']); self.assertEqual(self.inputs(),[])
        with self.assertRaises(MaaBackgroundInputError):self.click()
        self.assertEqual(self.inputs(),[])
    def test_capture_shape_and_resolution_must_both_match_window(self):
        self.c.resolution=(1280,720)
        with self.assertRaises(MaaBackgroundInputError) as got:self.click()
        self.assertEqual(got.exception.code,'background_capture_geometry')
        self.assertEqual(self.inputs(),[])
    def test_changed_geometry_during_capture_rejected_before_gate(self):
        self.c.on_capture=lambda:self.window['client_rect'].update(left=0)
        with self.assertRaises(MaaBackgroundInputError):self.click()
        self.assertEqual(self.inputs(),[]);self.assertEqual(self.gates,0)
    def test_same_pid_new_process_birth_never_rebinds(self):
        self.window['creation_time']=304
        with self.assertRaises(MaaBackgroundInputError):self.click()
        self.assertEqual(self.factory_calls,[])
    def test_gate_can_revoke_after_capture_without_input(self):
        def reject():raise RuntimeError('revoked')
        with self.assertRaisesRegex(RuntimeError,'revoked'):
            self.adapter.act('click',[(1,2)],gate=reject,expected_geometry=GEOMETRY)
        self.assertEqual(self.inputs(),[])
    def test_geometry_change_inside_gate_is_rechecked(self):
        def gate():self.window['client_rect']['width']=1280
        with self.assertRaises(MaaBackgroundInputError):
            self.adapter.act('click',[(1,2)],gate=gate,expected_geometry=GEOMETRY)
        self.assertEqual(self.inputs(),[])
    def test_cancel_before_dispatch_does_not_enqueue(self):
        cancel=threading.Event();self.c.on_capture=cancel.set
        with self.assertRaises(MaaBackgroundInputError) as got:self.click(cancel_event=cancel)
        self.assertFalse(got.exception.details['input_sent']);self.assertEqual(self.inputs(),[])
    def test_timeout_before_dispatch_is_bounded_and_known_no_input(self):
        self.c.connect_job=Job(done=False)
        with self.assertRaises(MaaBackgroundInputError) as got:self.click()
        self.assertEqual(got.exception.code,'background_timeout')
        self.assertFalse(got.exception.details['input_sent'])
        self.assertLess(self.now,8.1);self.assertEqual(self.inputs(),[])
    def test_timeout_after_enqueue_is_unknown_and_poisoned_no_replay(self):
        self.c.input_job=Job(done=False)
        with self.assertRaises(MaaBackgroundInputError) as got:self.click()
        self.assertTrue(got.exception.details['input_sent'])
        self.assertTrue(got.exception.details['partial_input'])
        with self.assertRaises(MaaBackgroundInputError):self.click()
        self.assertEqual(len(self.inputs()),1)
    def test_failed_sdk_input_is_unknown_not_effect_success(self):
        self.c.input_job=Job(succeeded=False)
        with self.assertRaises(MaaBackgroundInputError) as got:self.click()
        self.assertTrue(got.exception.details['input_sent'])
        self.assertIn(('inactive',),self.c.events)
    def test_cancel_after_enqueue_is_unknown_and_attempts_bounded_restore(self):
        cancel=threading.Event();self.c.on_input=cancel.set
        with self.assertRaises(MaaBackgroundInputError) as got:self.click(cancel_event=cancel)
        self.assertTrue(got.exception.details['input_sent'])
        self.assertIn(('inactive',),self.c.events)
    def test_window_not_restored_cannot_be_reported_success(self):
        self.c.on_input=lambda:self.window['client_rect'].update(left=0)
        with self.assertRaises(MaaBackgroundInputError) as got:self.click()
        self.assertEqual(got.exception.code,'background_geometry_changed')
        self.assertTrue(got.exception.details['input_sent'])
    def test_invalid_points_actions_and_durations_never_init_native_runtime(self):
        cases=[('click',[(True,1)],None),('click',[(1920,2)],None),
            ('click',[(1,-1)],None),('drag',[(1,2)],.5),('drag',[(1,2),(3,4)],0),
            ('drag',[(1,2),(3,4)],float('nan')),('drag',[(1,2),(3,4)],10),
            ('key',[(1,2)],None)]
        for action,points,duration in cases:
            with self.subTest(action=action,points=points,duration=duration),self.assertRaises(MaaBackgroundInputError):
                self.adapter.act(action,points,duration_seconds=duration,
                    gate=self.gate,expected_geometry=GEOMETRY)
        self.assertEqual(self.factory_calls,[])
    def test_minimized_game_rejected_and_never_silently_restored(self):
        self.window['minimized']=True
        with self.assertRaises(MaaBackgroundInputError):self.click()
        self.assertEqual(self.factory_calls,[])


class MaaClientButtonTests(unittest.TestCase):
    def setUp(self):
        from shu.maa_visual_native import MAA_EXECUTABLE
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.identity={**IDENTITY,'executable':str(MAA_EXECUTABLE)}
        self.window={**self.identity,'client_rect':deepcopy(GEOMETRY),'minimized':False}
        self.controller=Controller();self.options=[];self.now=0.
        def factory(**options):self.options.append(options);return self.controller
        def sleep(seconds):self.now+=seconds
        self.adapter=MaaClientButtonInput(self.identity,Path(self.temp.name),
            controller_factory=factory,check_window=lambda:deepcopy(self.window),
            clock=lambda:self.now,sleep=sleep)

    def click(self,gate=lambda:None):
        return self.adapter.act('click',[(265,241)],gate=gate,expected_geometry=GEOMETRY)

    def test_maa_uses_plain_postmessage_without_touch_window_or_cursor_repositioning(self):
        result=self.click()
        self.assertEqual(self.options,[dict(hWnd=101,screencap_method=16,
            mouse_method=4,keyboard_method=2)])
        self.assertEqual(self.controller.events,[('raw',True),('connect',),('capture',),
            ('click',265,241),('inactive',)])
        self.assertEqual(result['input_method'],'maafw_post_message')
        self.assertTrue(result['input_dispatched'])
        self.assertFalse(result['effect_verified'])
        self.assertTrue(result['verification_required'])

    def test_maa_failed_sdk_job_cannot_fall_back_or_retry_start(self):
        self.controller.input_job=Job(succeeded=False)
        with self.assertRaises(MaaBackgroundInputError) as caught:self.click()
        self.assertTrue(caught.exception.details['input_sent'])
        with self.assertRaises(MaaBackgroundInputError):self.click()
        self.assertEqual(self.controller.events.count(('click',265,241)),1)
        self.assertEqual(len(self.options),1)

    def test_maa_permission_revoked_at_gate_never_sends(self):
        def revoked():raise RuntimeError('revoked')
        with self.assertRaisesRegex(RuntimeError,'revoked'):self.click(revoked)
        self.assertNotIn(('click',265,241),self.controller.events)

    def test_maa_adapter_rejects_game_identity(self):
        with self.assertRaises(ValueError):
            MaaClientButtonInput(IDENTITY,Path(self.temp.name),check_window=lambda:self.window)


if __name__=='__main__': unittest.main()

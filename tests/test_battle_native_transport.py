"""Dual surface helper tests; no real desktop or native input."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shu.maa_battle_native import MaaBattleHandler, BattleTransportError, validate_game_identity
from shu.maa_visual_native import MAA_EXECUTABLE
from shu.windows import WindowController, WindowInfo, WindowError, _encode_png
from shu.gui_supervisor import GuiSupervisor, _MaaVisualHandler


GAME = {'hwnd':101,'pid':202,'creation_time':303,'executable':r'D:\Games\Arknights\Arknights.exe'}
MAA = {'hwnd':111,'pid':222,'creation_time':333,'executable':MAA_EXECUTABLE}


class Desktop:
    supports_background_capture = True
    def __init__(self):
        self.info=WindowInfo(101,202,'明日方舟',-200,50,160,100,True,False,GAME['executable'],'UnityWndClass')
        self.birth=303; self.events=[]; self.gate=lambda:None; self.capture_hook=None
        self.allowed=True
    def get_window(self,hwnd):
        return self.info if self.info and self.info.hwnd==hwnd else None
    def creation_time(self,pid):return self.birth
    def capture_background(self,info,path):
        path.write_bytes(_encode_png(info.width,info.height,bytes((40,50,60,255))*info.width*info.height))
        if self.capture_hook:self.capture_hook()
        return {'method':'win32_printwindow','width':info.width,'height':info.height,'requires_foreground':False}
    def capture(self,info,path):return self.capture_background(info,path)
    def permission(self,pid):return {'known':True,'input_allowed':self.allowed}
    def focus_window(self,hwnd):
        self.gate();self.info=replace(self.info,foreground=True,minimized=False);self.events.append(('focus',hwnd))
    def presentation_state(self, game, maa):
        return {'game_visible':game.foreground, 'maa_minimized':maa.minimized,
                'maa_topmost':False,'game_topmost':False,'maa_obstructs_game':not game.foreground,
                'focus_acquired':game.foreground}
    def restore_window(self, hwnd):
        self.info = replace(self.info, minimized=False)
        self.events.append(('restore', hwnd))
    def click_guarded(self,x,y,guard):
        guard();self.gate();self.events.append(('click',x,y));return {'pointer_verified':True}
    def drag(self,*args):
        *coords,duration,guard=args;guard();self.gate();self.events.append(('drag',*coords,duration))


class Maa:
    def __init__(self):self.events=[];self.sequence=0
    def _current(self, *, permit_minimized):
        return WindowInfo(111,222,'MAA',-200,50,80,100,False,False,MAA_EXECUTABLE)
    def _permission(self, *, required):return {'input_allowed':True}
    def snapshot(self):
        self.sequence+=1;return {'screenshot_id':f'maa-{self.sequence}','window':MAA,'captured_at':100.}
    def focus(self,*,guard):guard();self.events.append(('focus',));return {'ok':True}
    def click_battle(self,x,y,screenshot_id,*,target,guard):
        guard();self.events.append(('click',x,y,screenshot_id,target));return {'ok':True}


class BackgroundInput:
    """Fake SDK boundary; geometry/admission are still the real handler's."""
    def __init__(self):self.calls=[];self.before_gate=None;self.failure=None
    def act(self,action,points,*,duration_seconds=None,gate,expected_geometry,**_):
        if self.before_gate:self.before_gate()
        gate()
        if self.failure:raise self.failure
        self.calls.append((action,points,duration_seconds,expected_geometry))
        return {'ok':True,'input_dispatched':True,'effect_verified':False,
                'method':'maafw_postmessage_with_window_pos'}


class BattleTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.desktop=Desktop();self.maa=Maa()
        self.windows=WindowController(Path(self.temp.name),backend=self.desktop,prefer_maafw=False)
        self.windows.bind(GAME['hwnd'])
        self.background_input=BackgroundInput()
        self.handler=MaaBattleHandler({**MAA,'_game':GAME},self.temp.name,
            maa_adapter=self.maa,game_windows=self.windows,game_backend=self.desktop,
            game_input=self.background_input)
        self.admitted=0
    def admit(self):self.admitted+=1
    def call(self,op='snapshot',**args):
        return self.handler({'op':op,'args':args},self.admit)
    def snapshot(self):return self.call(surface='game')
    def click(self,frame,**extra):
        return self.call('act',surface='game',action='click',screenshot_id=frame['screenshot_id'],
                         x=35,y=40,forbidden_regions=[],**extra)
    def test_game_snapshot_is_bound_and_reads_without_admission(self):
        frame=self.snapshot()
        self.assertEqual(frame['creation_time'],303);self.assertEqual(frame['window']['pid'],202)
        self.assertEqual(frame['screenshot_id'],frame['id']);self.assertEqual(self.admitted,0)
    def test_minimized_game_restored_without_focus_or_content_input(self):
        self.desktop.info = replace(self.desktop.info, minimized=True, foreground=False)
        result = self.call('act', surface='game', action='restore')
        self.assertTrue(result['restored'])
        self.assertFalse(self.desktop.info.foreground)
        self.assertEqual(self.desktop.events, [('restore', 101)])
        self.assertEqual(self.admitted, 1)

    def test_game_click_refreshes_pixels_then_uses_only_current_token(self):
        frame=self.snapshot();self.click(frame)
        self.assertEqual(self.background_input.calls[-1][:3],('click',[(35,40)],None))
        self.assertEqual(self.desktop.events,[])
        self.assertGreater(self.admitted,0)
        with self.assertRaises(BattleTransportError):self.click(frame)
    def test_same_pid_with_new_birth_cannot_rebind(self):
        self.desktop.birth=304
        with self.assertRaises(BattleTransportError):self.snapshot()
        self.assertEqual(self.desktop.events,[])
    def test_changed_executable_cannot_rebind(self):
        self.desktop.info=replace(self.desktop.info,executable=r'D:\Other\Arknights.exe')
        with self.assertRaises(BattleTransportError):self.snapshot()
    def test_changed_geometry_is_rejected_even_with_same_pixels(self):
        frame=self.snapshot();self.desktop.info=replace(self.desktop.info,left=-150)
        with self.assertRaises(BattleTransportError):self.click(frame)
        self.assertEqual(self.desktop.events,[])
    def test_patch_change_rejects_before_admission(self):
        frame=self.snapshot();self.handler.patch_compare=lambda *args:False
        with self.assertRaises(BattleTransportError):self.click(frame)
        self.assertEqual(self.admitted,0)
    def test_forbidden_start_action_rect_blocks_click(self):
        frame=self.snapshot()
        args={'surface':'game','action':'click','x':35,'y':40,'screenshot_id':frame['screenshot_id'],
              'forbidden_regions':[{'kind':'start_action','rect':{'x':20,'y':20,'width':40,'height':40}}]}
        with self.assertRaises(BattleTransportError):self.handler({'op':'act','args':args},self.admit)
        self.assertEqual(self.admitted,0)
    def test_drag_segment_crossing_forbidden_rect_is_rejected(self):
        frame=self.snapshot()
        with self.assertRaises(BattleTransportError):
            self.call('act',surface='game',action='drag',x=10,y=50,to_x=150,to_y=50,duration_seconds=.3,
                screenshot_id=frame['screenshot_id'],forbidden_regions=[
                    {'kind':'start_action','rect':{'x':70,'y':30,'width':20,'height':40}}])
        self.assertEqual(self.desktop.events,[])
    def test_missing_forbidden_regions_is_not_an_empty_verified_list(self):
        frame=self.snapshot()
        with self.assertRaises(BattleTransportError):
            self.call('act',surface='game',action='click',x=35,y=40,screenshot_id=frame['screenshot_id'])
    def test_game_battle_start_label_is_never_accepted(self):
        frame=self.snapshot()
        with self.assertRaises(BattleTransportError):self.click(frame,target='开始行动')
    def test_rejected_parent_admission_never_clicks(self):
        frame=self.snapshot()
        def reject():raise RuntimeError('revoked')
        with self.assertRaises(RuntimeError):
            self.handler({'op':'act','args':{'surface':'game','action':'click','x':35,'y':40,
                'screenshot_id':frame['screenshot_id'],'forbidden_regions':[]}},reject)
        self.assertEqual(self.desktop.events,[])
    def test_maa_start_is_distinct_from_ordinary_import_clicks(self):
        frame=self.call(surface='maa')
        result=self.call('act',surface='maa',action='click',x=50,y=60,screenshot_id=frame['screenshot_id'],target='battle_start')
        self.assertEqual(self.maa.events[-1][-1],'battle_start')
        self.assertTrue(result['game_presentation']['game_visible'])
        self.assertEqual(self.desktop.events,[('focus',GAME['hwnd'])])
        self.assertGreater(self.admitted,1)
        with self.assertRaises(BattleTransportError):
            self.call('act',surface='maa',action='click',x=50,y=60,screenshot_id='fresh',target='start')

    def test_revocation_after_start_skips_window_change_and_preserves_click(self):
        frame=self.call(surface='maa')
        def stop_on_handoff():
            if self.maa.events:raise RuntimeError('revoked')
            self.admit()
        result=self.handler({'op':'act','args':{'surface':'maa','action':'click','target':'battle_start',
            'x':50,'y':60,'screenshot_id':frame['screenshot_id']}},stop_on_handoff)
        self.assertTrue(result['ok'])
        self.assertFalse(result['game_presentation']['game_visible'])
        self.assertEqual(self.desktop.events,[])
        self.assertEqual(len(self.maa.events),1)

    def test_topmost_maa_failure_is_reported_without_replaying_start(self):
        frame=self.call(surface='maa')
        self.desktop.presentation_state=lambda game,maa: {'game_visible':False,'maa_obstructs_game':True,
            'maa_topmost':True,'game_topmost':False,'maa_minimized':False,'focus_acquired':True}
        result=self.call('act',surface='maa',action='click',x=50,y=60,screenshot_id=frame['screenshot_id'],target='battle_start')
        self.assertTrue(result['ok'])
        self.assertEqual(result['game_presentation']['reason'],'maa_topmost_occlusion')
        self.assertEqual(len(self.maa.events),1)

    def test_normal_maa_that_still_covers_game_is_placed_behind_and_rechecked(self):
        states=iter([False,True])
        def observe(game,maa):
            visible=next(states)
            return {'game_visible':visible,'maa_obstructs_game':not visible,'maa_topmost':False,
                    'game_topmost':False,'maa_minimized':False,'focus_acquired':game.foreground}
        self.desktop.presentation_state=observe
        self.desktop.place_maa_behind=lambda game,maa:self.desktop.events.append(('lower',maa.hwnd,game.hwnd))
        result=self.call('act',surface='game',action='focus',target='show_battle_game')
        self.assertEqual(self.desktop.events,[('focus',GAME['hwnd']),('lower',MAA['hwnd'],GAME['hwnd'])])
        self.assertTrue(result['game_visible'])
        self.assertFalse(self.maa.events)

    def test_failed_start_or_stop_click_never_brings_game_forward(self):
        for target in ('battle_stop','battle_start'):
            frame=self.call(surface='maa')
            if target=='battle_start':
                def reject(*args,**kwargs):raise WindowError('focus lost','not_foreground')
                self.maa.click_battle=reject
                with self.assertRaises(WindowError):
                    self.call('act',surface='maa',action='click',x=50,y=60,screenshot_id=frame['screenshot_id'],target=target)
            else:
                result=self.call('act',surface='maa',action='click',x=50,y=60,screenshot_id=frame['screenshot_id'],target=target)
                self.assertNotIn('game_presentation',result)
        self.assertEqual(self.desktop.events,[])
    def test_stop_only_can_never_start_or_navigate(self):
        for surface,target in [('maa','battle_start'),('game','battle_stop'),('maa','navigation_option')]:
            with self.subTest(surface=surface,target=target),self.assertRaises(BattleTransportError):
                self.handler({'op':'act','stop_only':True,'args':{'surface':surface,'action':'click',
                    'target':target,'x':5,'y':5,'screenshot_id':'fresh'}},self.admit)
        self.assertEqual(self.maa.events,[])

    def background(self):
        self.background_input=BackgroundInput()
        self.handler=MaaBattleHandler({**MAA,'_game':GAME},self.temp.name,
            maa_adapter=self.maa,game_windows=self.windows,game_backend=self.desktop,
            game_input=self.background_input)
        return self.background_input

    def test_background_click_does_not_require_foreground_or_move_physical_pointer(self):
        sdk=self.background();self.desktop.info=replace(self.desktop.info,foreground=False)
        frame=self.snapshot();result=self.click(frame)
        self.assertEqual(sdk.calls,[('click',[(35,40)],None,frame['window']['client_rect'])])
        self.assertEqual(self.desktop.events,[])
        self.assertTrue(result['input_dispatched']);self.assertFalse(result['effect_verified'])
        self.assertTrue(result['verification_required']);self.assertEqual(self.admitted,1)

    def test_background_foreground_change_alone_does_not_invalidate_target(self):
        sdk=self.background();frame=self.snapshot()
        self.desktop.info=replace(self.desktop.info,foreground=False)
        self.click(frame)
        self.assertEqual(len(sdk.calls),1)

    def test_background_permission_loss_at_sdk_dispatch_blocks_admission(self):
        sdk=self.background();frame=self.snapshot()
        sdk.before_gate=lambda:setattr(self.desktop,'allowed',False)
        with self.assertRaises(BattleTransportError):self.click(frame)
        self.assertEqual(self.admitted,0);self.assertEqual(sdk.calls,[])

    def test_background_geometry_change_at_sdk_dispatch_blocks_admission(self):
        sdk=self.background();frame=self.snapshot()
        sdk.before_gate=lambda:setattr(self.desktop,'info',replace(self.desktop.info,left=-170))
        with self.assertRaises(BattleTransportError):self.click(frame)
        self.assertEqual(self.admitted,0);self.assertEqual(sdk.calls,[])

    def test_background_new_pixels_before_enqueue_are_rechecked(self):
        sdk=self.background();frame=self.snapshot()
        sdk.before_gate=lambda:setattr(self.handler,'patch_compare',lambda *a:False)
        with self.assertRaises(BattleTransportError):self.click(frame)
        self.assertEqual(self.admitted,0);self.assertEqual(sdk.calls,[])

    def test_background_sdk_failure_is_not_reported_as_success_or_replayed(self):
        sdk=self.background();frame=self.snapshot();sdk.failure=WindowError('SDK failed','framework_job_failed')
        with self.assertRaises(WindowError):self.click(frame)
        self.assertEqual(self.admitted,1)
        with self.assertRaises(BattleTransportError):self.click(frame)

    def test_background_drag_passes_raw_client_coordinates_and_duration(self):
        sdk=self.background();self.desktop.info=replace(self.desktop.info,foreground=False)
        frame=self.snapshot()
        self.call('act',surface='game',action='drag',x=10,y=15,to_x=100,to_y=15,duration_seconds=.25,
                  screenshot_id=frame['screenshot_id'],forbidden_regions=[])
        self.assertEqual(sdk.calls[0][:3],('drag',[(10,15),(100,15)],.25))
        self.assertEqual(self.desktop.events,[])

    def test_background_drag_invalid_duration_is_rejected_before_sdk(self):
        sdk=self.background();frame=self.snapshot()
        for duration in (None,True,.01,2,float('nan')):
            with self.subTest(duration=duration),self.assertRaises(BattleTransportError):
                self.call('act',surface='game',action='drag',x=10,y=15,to_x=100,to_y=15,duration_seconds=duration,
                          screenshot_id=frame['screenshot_id'],forbidden_regions=[])
        self.assertEqual(sdk.calls,[]);self.assertEqual(self.admitted,0)
    def test_stop_only_accepts_bound_maa_stop(self):
        frame=self.call(surface='maa')
        self.handler({'op':'act','stop_only':True,'args':{'surface':'maa','action':'click',
            'target':'battle_stop','x':5,'y':5,'screenshot_id':frame['screenshot_id']}},self.admit)
        self.assertEqual(self.maa.events[-1][-1],'battle_stop')
    def test_no_game_identity_does_not_guess_a_window(self):
        handler=MaaBattleHandler(MAA,self.temp.name,maa_adapter=self.maa)
        with self.assertRaises(BattleTransportError):handler({'op':'snapshot','args':{'surface':'game'}},self.admit)
    def test_identity_validation_rejects_invalid_pid_or_executable(self):
        for changes in ({'pid':True},{'creation_time':0},{'executable':r'D:\Games\launcher.exe'},
                        {'executable':'Arknights.exe'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):validate_game_identity({**GAME,**changes})
    def test_maa_stop_rejects_missing_old_or_consumed_frame(self):
        args={'surface':'maa','action':'click','target':'battle_stop','x':5,'y':5,'screenshot_id':'not-captured'}
        with self.assertRaises(BattleTransportError):
            self.handler({'op':'act','stop_only':True,'args':args},self.admit)
        frame=self.call(surface='maa');args['screenshot_id']=frame['screenshot_id']
        self.handler({'op':'act','stop_only':True,'args':args},self.admit)
        with self.assertRaises(BattleTransportError):
            self.handler({'op':'act','stop_only':True,'args':args},self.admit)
    def test_expired_maa_frame_does_not_call_admission(self):
        frame=self.call(surface='maa');self.handler._last_maa_at-=9
        with self.assertRaises(BattleTransportError):
            self.call('act',surface='maa',action='click',x=5,y=5,target='battle_start',screenshot_id=frame['screenshot_id'])
        self.assertEqual(self.admitted,0)
    def test_game_source_expiry_is_not_silently_renewed(self):
        frame=self.snapshot();self.handler._last_game_at-=121
        with self.assertRaises(BattleTransportError):self.click(frame)
        self.assertEqual(self.admitted,0)
    def test_game_permission_loss_rejects_before_input(self):
        frame=self.snapshot();self.desktop.allowed=False
        with self.assertRaises(BattleTransportError):self.click(frame)
        self.assertEqual(self.desktop.events,[])
    def test_game_drag_is_guarded_and_uses_fresh_observation(self):
        frame=self.snapshot()
        self.call('act',surface='game',action='drag',x=10,y=15,to_x=100,to_y=15,duration_seconds=.25,
                  screenshot_id=frame['screenshot_id'],forbidden_regions=[])
        self.assertEqual(self.background_input.calls[-1][:3],('drag',[(10,15),(100,15)],.25))
        self.assertEqual(self.desktop.events,[])
        self.assertGreater(self.admitted,0)
    def test_game_focus_runs_admission_and_drops_previous_action_token(self):
        frame=self.snapshot();self.desktop.info=replace(self.desktop.info,foreground=False)
        self.call('act',surface='game',action='focus')
        self.assertTrue(self.desktop.info.foreground);self.assertGreater(self.admitted,0)
        with self.assertRaises(BattleTransportError):self.click(frame)
    def test_ordinary_visual_import_backend_still_forbids_start_and_stop(self):
        with patch('shu.maa_visual_native.MaaVisualNative',return_value=self.maa):
            handler=_MaaVisualHandler(MAA,self.temp.name)
        for target in ('start','stop'):
            with self.subTest(target=target),self.assertRaises(RuntimeError):
                handler({'op':'act','args':{'action':'click','target':target,'x':5,'y':5,'screenshot_id':'x'}},self.admit)
        self.assertEqual(self.maa.events,[])
    def test_supervisor_pins_two_identities_without_changing_maa_public_identity(self):
        supervisor=GuiSupervisor(MAA,backend='battle',directory=self.temp.name,
            navigation_identity=GAME,lock_path=Path(self.temp.name)/'test.lock')
        try:
            self.assertEqual(supervisor.target_identity,MAA)
            self.assertEqual(json.loads(supervisor._identity_json)['_game'],GAME)
        finally:supervisor.close()
    def test_supervisor_does_not_accept_navigation_identity_on_old_backends(self):
        for backend in ('uia','visual'):
            with self.subTest(backend=backend),self.assertRaises(ValueError):
                GuiSupervisor(MAA,backend=backend,directory=self.temp.name,navigation_identity=GAME)

    def foreground(self):
        self.handler=MaaBattleHandler({**MAA,'_game':GAME,'_navigation_mode':'foreground'},self.temp.name,
            maa_adapter=self.maa,game_windows=self.windows,game_backend=self.desktop)
        return self.handler

    def test_foreground_click_uses_existing_physical_path_and_no_background_sdk(self):
        with patch('shu.maa_background_input.MaaBackgroundInput',side_effect=AssertionError('No implicit SDK fallback')):
            self.foreground();frame=self.snapshot();result=self.click(frame)
        self.assertEqual(self.desktop.events,[('click',-165,90)])
        self.assertTrue(result['input_dispatched']);self.assertFalse(result['effect_verified'])
        self.assertEqual(result['method'],'win32_foreground_guarded')
        self.assertEqual(result['source_screenshot_id'],frame['screenshot_id'])
        self.assertEqual(self.background_input.calls,[])
        self.assertFalse(self.windows.input_enabled)
        with self.assertRaises(BattleTransportError):self.click(frame)

    def test_foreground_drag_uses_bounded_physical_client_translation(self):
        self.foreground();frame=self.snapshot()
        result=self.call('act',surface='game',action='drag',x=10,y=15,to_x=100,to_y=15,duration_seconds=.25,
            screenshot_id=frame['screenshot_id'],forbidden_regions=[])
        self.assertEqual(self.desktop.events,[('drag',-190,65,-100,65,.25)])
        self.assertTrue(result['input_dispatched']);self.assertFalse(self.windows.input_enabled)

    def test_foreground_loss_minimize_stale_or_permission_blocks_before_input(self):
        for change in ('foreground','minimized','permission','stale','covered'):
            with self.subTest(change=change):
                self.desktop.info=replace(self.desktop.info,foreground=True,minimized=False)
                self.desktop.allowed=True;self.desktop.events.clear();self.foreground();frame=self.snapshot()
                if change=='foreground':self.desktop.info=replace(self.desktop.info,foreground=False)
                elif change=='minimized':self.desktop.info=replace(self.desktop.info,minimized=True)
                elif change=='permission':self.desktop.allowed=False
                elif change=='stale':self.handler._last_game_at-=121
                else:
                    def covered(*args):raise WindowError('covered','target_occluded')
                    self.desktop.click_guarded=covered
                with self.assertRaises(WindowError):self.click(frame)
                self.assertFalse(self.desktop.events);self.assertFalse(self.windows.input_enabled)

    def test_foreground_revocation_or_focus_loss_at_admission_never_clicks(self):
        self.foreground();frame=self.snapshot()
        def lose_focus():self.desktop.info=replace(self.desktop.info,foreground=False)
        with self.assertRaises(WindowError):
            self.handler({'op':'act','args':{'surface':'game','action':'click','x':35,'y':40,
                'screenshot_id':frame['screenshot_id'],'forbidden_regions':[]}},lose_focus)
        self.assertFalse(self.desktop.events);self.assertFalse(self.windows.input_enabled)
        self.desktop.info=replace(self.desktop.info,foreground=True)
        frame=self.snapshot()
        def revoked():raise WindowError('cancelled','stopped')
        with self.assertRaises(WindowError):
            self.handler({'op':'act','args':{'surface':'game','action':'click','x':35,'y':40,
                'screenshot_id':frame['screenshot_id'],'forbidden_regions':[]}},revoked)
        self.assertFalse(self.desktop.events)

    def test_denied_game_focus_returns_known_false_without_require_foreground_throw(self):
        self.foreground();self.desktop.info=replace(self.desktop.info,foreground=False)
        def deny(hwnd):
            self.desktop.gate();self.desktop.focus_result={'focus_acquired':False,'input_sent':False,'caption_click':False}
        self.desktop.focus_window=deny
        result=self.call('act',surface='game',action='focus')
        self.assertFalse(result['focus_acquired']);self.assertFalse(self.desktop.events)
        self.assertGreater(self.admitted,0)

    def test_game_focus_does_not_accept_false_cached_success(self):
        self.foreground();self.desktop.info=replace(self.desktop.info,foreground=False)
        def stale_success(hwnd):
            self.desktop.gate();self.desktop.focus_result={'focus_acquired':True,'input_sent':False}
        self.desktop.focus_window=stale_success
        result=self.call('act',surface='game',action='focus')
        self.assertFalse(result['focus_acquired'])

    def test_navigation_mode_is_fixed_constructor_scope_and_removed_from_maa_identity(self):
        self.foreground();self.assertEqual(self.handler.maa_identity,MAA)
        self.assertEqual(self.handler.navigation_mode,'foreground')
        with self.assertRaises(ValueError):
            MaaBattleHandler({**MAA,'_game':GAME,'_navigation_mode':'seize'},self.temp.name,maa_adapter=self.maa)
        for backend in ('uia','visual'):
            with self.subTest(backend=backend),self.assertRaises(ValueError):
                GuiSupervisor(MAA,backend=backend,directory=self.temp.name,navigation_mode='foreground')

    def test_supervisor_serializes_explicit_mode_only_for_bound_battle(self):
        supervisor=GuiSupervisor(MAA,backend='battle',directory=self.temp.name,navigation_identity=GAME,
            navigation_mode='foreground',lock_path=Path(self.temp.name)/'foreground.lock')
        try:
            self.assertEqual(supervisor.target_identity,MAA)
            self.assertEqual(json.loads(supervisor._identity_json)['_navigation_mode'],'foreground')
        finally:supervisor.close()
        with self.assertRaises(ValueError):
            GuiSupervisor({**MAA,'_navigation_mode':'foreground'},backend='battle',directory=self.temp.name,
                          navigation_identity=GAME)


if __name__=='__main__':unittest.main()

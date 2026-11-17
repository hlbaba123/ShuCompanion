import copy
import unittest
import tempfile
import threading
from unittest.mock import MagicMock, patch
from pathlib import Path
from types import SimpleNamespace

from shu.maa_battle import BattleError, verified_ready, loaded_target


class BattleRunnerPolicyTests(unittest.TestCase):
    def test_maa_itself_must_have_game_input_integrity_not_just_shu(self):
        from shu.maa_battle import verify_execution_integrity
        maa={'known':True,'input_allowed':True,'own_integrity':12288,'target_integrity':8192}
        game={'known':True,'input_allowed':True,'own_integrity':12288,'target_integrity':12288}
        with self.assertRaises(BattleError) as caught:verify_execution_integrity(maa,game)
        self.assertEqual(caught.exception.code,'maa_game_integrity_mismatch')
        verify_execution_integrity(maa,{**game,'target_integrity':8192})
        with self.assertRaises(BattleError):verify_execution_integrity(maa,{**game,'known':False})

    def setUp(self):
        self.frame={'screenshot_id':'a'*32,'image_sha256':'b'*64,
                    'window':{'client_rect':{'width':1920,'height':1080}}}
        self.target={'stage_code':'UT-EX-1','difficulty':'normal'}
        self.view={'action':'ready','page':'stage_detail','selected_stage_code':'UT-EX-1',
                   'difficulty':'normal','start_action_box':{'x':.82,'y':.87,'width':.15,'height':.08},
                   'image_sha256':'b'*64,'screenshot_id':'a'*32,'resource_sensitive':False,
                   'target_label':'','target_box':None,'to_x':None,'to_y':None}

    def test_exact_current_selected_stage_and_difficulty_ready(self):
        self.assertTrue(verified_ready(self.frame,self.view,self.target))

    def test_map_label_wrong_difficulty_or_unbound_image_never_ready(self):
        for change in ({'page':'stage_map'},{'selected_stage_code':'UT-EX-10'},
                       {'difficulty':'raid'},{'difficulty':'unknown'},
                       {'image_sha256':'0'*64},{'screenshot_id':'z'*32},
                       {'resource_sensitive':True},{'start_action_box':None}):
            with self.subTest(change=change):
                self.assertFalse(verified_ready(self.frame,{**self.view,**change},self.target))

    def test_start_action_must_be_measured_bottom_right(self):
        view=copy.deepcopy(self.view);view['start_action_box']['x']=.1
        self.assertFalse(verified_ready(self.frame,view,self.target))

    def test_current_loaded_document_must_match_visible_stage(self):
        view={'page':'copilot','running':False,'multi_mode':False,'mystery_code':'prts://123',
              'stage_code':'UT-EX-1','preview_title':'Example UT-EX-1',
              'start_box':{'x':.1,'y':.1,'width':.2,'height':.1}}
        doc={'stage_name':'internal-stage','doc':{'title':'Example UT-EX-1'},'actions':[{'type':'SpeedUp'}]}
        self.assertEqual(loaded_target(view,doc,{})['stage_code'],'UT-EX-1')
        for change in ({'multi_mode':True},{'running':True},{'stage_code':'UT-EX-10'},
                       {'start_box':None},{'mystery_code':''}):
            with self.subTest(change=change),self.assertRaises(BattleError):
                loaded_target({**view,**change},doc,{})

    def test_explicit_user_stage_cannot_be_replaced_by_loaded_other_stage(self):
        view={'page':'copilot','running':False,'multi_mode':False,'mystery_code':'prts://123',
              'stage_code':'UT-1','preview_title':'UT-1','start_box':{'x':.1,'y':.1,'width':.2,'height':.1}}
        doc={'stage_name':'s','doc':{'title':'UT-1'},'actions':[{}]}
        with self.assertRaises(BattleError):
            loaded_target(view,doc,{'task_spec':{'stage_code':'OTHER-1'}})

    def test_visible_title_can_be_exact_stage_subtitle_instead_of_full_prefix(self):
        view={'page':'copilot','running':False,'multi_mode':False,
              'mystery_code':'prts://123','stage_code':'UT-EX-1','preview_title':'Visible subtitle……',
              'start_box':{'x':.1,'y':.1,'width':.2,'height':.1}}
        doc={'stage_name':'stage','doc':{'title':'Activity - UT-EX-1 - Visible subtitle……'},'actions':[{}]}
        self.assertEqual(loaded_target(view,doc,{})['stage_code'],'UT-EX-1')
        with self.assertRaises(BattleError):
            loaded_target({**view,'preview_title':'Wrong subtitle'},doc,{})

    def test_empty_live_page_is_not_replaced_by_old_loaded_cache(self):
        view={'page':'copilot','running':False,'multi_mode':False,'mystery_code':'',
              'stage_code':'','preview_title':'','start_box':{'x':.1,'y':.1,'width':.2,'height':.1}}
        old={'stage_name':'old-stage','doc':{'title':'Old UT-EX-1'},'actions':[{}]}
        with self.assertRaises(BattleError) as caught:loaded_target(view,old,{})
        self.assertEqual(caught.exception.code,'loaded_job_missing')

    def test_first_battle_creates_its_independent_vision_client(self):
        from shu.maa_battle import BattleRunner
        from shu.providers import ProviderConfig
        with tempfile.TemporaryDirectory() as folder:
            maa=SimpleNamespace(config=ProviderConfig())
            roles=SimpleNamespace(directory=Path(folder),store=object(),clients={'maa':maa})
            runner=BattleRunner(roles,{'task_id':'test-task','epoch':1},threading.Event())
            self.assertIs(roles.clients['vision'],runner.vision.client)
            self.assertEqual(runner.navigation_mode,'background')
            self.assertIsNot(roles.clients['vision'],maa)

    def test_large_roster_is_fingerprinted_outside_workflow_envelope(self):
        from shu.maa_battle import _content_hash
        box={'operators':[{'name':'example-'+str(n),'source':'s'*300} for n in range(400)]}
        self.assertEqual(len(_content_hash(box)),64)
        self.assertNotEqual(_content_hash(box),_content_hash({**box,'box_revision':2}))

    def test_background_navigation_does_not_require_game_foreground(self):
        ready={**self.view,'evidence':'selected detail'}
        runner=self.navigation_runner([self.navigation_view('home'),ready,ready])
        runner.focus.side_effect=AssertionError('foreground denied')
        runner._navigate()
        runner.focus.assert_not_called();runner.input.assert_not_called()
        self.assertEqual(runner.vision.navigate.call_count,3)
        self.assertEqual(runner.ready_frame['screenshot_id'],'2')

    def test_single_mode_requires_same_frame_local_checkbox_evidence(self):
        from shu.maa_battle import BattleRunner
        runner=BattleRunner.__new__(BattleRunner)
        local={'image_sha256':self.frame['image_sha256'],'page':'copilot',
               'options':{'multiple_jobs':{'state':False,'measurement':'local_checkbox_pixels'}}}
        runner._local=MagicMock(return_value=local)
        runner._single_mode(self.frame)
        for value in (True,None):
            local['options']['multiple_jobs']['state']=value
            with self.subTest(state=value),self.assertRaises(BattleError):runner._single_mode(self.frame)
        local['options']['multiple_jobs']['state']=False
        local['image_sha256']='other'
        with self.assertRaises(BattleError):runner._single_mode(self.frame)

    def test_explicit_foreground_mode_focuses_game_before_observing(self):
        ready={**self.view,'evidence':'selected detail'}
        runner=self.navigation_runner([self.navigation_view('home'),ready,ready])
        runner.navigation_mode='foreground'
        runner._navigate()
        runner.focus.assert_called_once_with('game')

    def navigation_view(self,page,action='wait',target_label='',**overrides):
        return {'page':page,'action':action,'target_label':target_label,
                'target_box':{'x':.02,'y':.03,'width':.08,'height':.06} if action=='click' else None,
                'to_x':None,'to_y':None,'selected_stage_code':'','difficulty':'unknown',
                'start_action_box':None,'resource_sensitive':False,'evidence':'visible '+page,**overrides}

    def navigation_runner(self, answers):
        from shu.maa_battle import BattleRunner
        runner=BattleRunner.__new__(BattleRunner)
        runner.target=self.target;runner.cancel=threading.Event()
        runner.cancel.wait=MagicMock(return_value=False)
        runner.guard=MagicMock();runner.focus=MagicMock();runner.input=MagicMock()
        runner.publish=MagicMock(return_value={'path':'proof.json'});runner.progress=MagicMock()
        frames=[{**self.frame,'screenshot_id':str(index)} for index in range(90)]
        runner.snapshot=MagicMock(side_effect=frames)
        scripted=iter(answers)
        def navigate(frame,*args,**kwargs):
            value=next(scripted)
            if isinstance(value,Exception):raise value
            return {**value,'screenshot_id':frame['screenshot_id'],'image_sha256':frame['image_sha256']}
        runner.vision=SimpleNamespace(navigate=MagicMock(side_effect=navigate))
        return runner

    def test_arbitrary_initial_menu_returns_home_before_event_and_two_fresh_detail_checks(self):
        ready={**self.view,'evidence':'selected detail'}
        for initial in ('settings','stage_map'):
            with self.subTest(initial=initial):
                runner=self.navigation_runner([
                    self.navigation_view(initial,'click','返回'),self.navigation_view('home'),
                    self.navigation_view('home','click','当前活动'),
                    self.navigation_view('event_overview','click','已核实章节'),ready,ready])
                runner._navigate()
                phases=[call.args[1].get('navigation_phase') for call in runner.vision.navigate.call_args_list]
                self.assertEqual(phases,['return_home','return_home','to_target','to_target','to_target','to_target'])
                self.assertNotIn('navigation_phase',self.target)
                self.assertEqual([call.args[0]['target'] for call in runner.input.call_args_list],
                                 ['返回','当前活动','已核实章节'])
                proof=next(call.args[1] for call in runner.publish.call_args_list if call.args[0]=='game-ready')
                self.assertEqual([frame['screenshot_id'] for frame in proof['frames']],['4','5'])
                progress=[call.args[2]['navigation_phase'] for call in runner.progress.call_args_list
                          if 'navigation_phase' in call.args[2]]
                self.assertIn('return_home',progress);self.assertIn('to_target',progress)

    def test_already_correct_detail_cannot_be_ready_before_confirmed_home(self):
        ready={**self.view,'evidence':'selected detail'}
        runner=self.navigation_runner([ready,ready])
        # Each navigation attempt must establish its own home observation.
        runner.navigation_phase='to_target'
        with self.assertRaises(BattleError) as caught:runner._navigate()
        self.assertEqual(caught.exception.code,'navigation_home_unverified')
        self.assertFalse(hasattr(runner,'ready_ref'));runner.input.assert_not_called()

    def test_home_transition_requires_safe_wait_from_current_image(self):
        for changes in ({'image_sha256':'0'*64},{'screenshot_id':'old'},
                        {'resource_sensitive':True},{'action':'ready'}):
            with self.subTest(changes=changes):
                ready={**self.view,'evidence':'selected detail'}
                runner=self.navigation_runner([ready,ready])
                def invalid_home(frame,*args,**kwargs):
                    return {**self.navigation_view('home'),'screenshot_id':frame['screenshot_id'],
                            'image_sha256':frame['image_sha256'],**changes}
                runner.vision.navigate.side_effect=invalid_home
                with self.assertRaises(BattleError):runner._navigate()
                self.assertEqual(runner.navigation_phase,'return_home')
                self.assertFalse(hasattr(runner,'ready_ref'));runner.input.assert_not_called()

    def test_ready_replay_cannot_replace_second_fresh_frame(self):
        ready={**self.view,'evidence':'selected detail'}
        runner=self.navigation_runner([self.navigation_view('home'),ready,ready])
        runner.snapshot.side_effect=[{**self.frame,'screenshot_id':i} for i in ('home','detail','detail')]
        with self.assertRaises(BattleError):runner._navigate()
        self.assertFalse(hasattr(runner,'ready_ref'));runner.input.assert_not_called()

    def test_phase_is_carried_to_local_observer_and_model_fallback_without_mutating_target(self):
        runner=self.navigation_runner([self.navigation_view('home')])
        runner.navigation_phase='return_home'
        runner.local_navigation=SimpleNamespace(observe=MagicMock(return_value=None),
            check_model_home_action=MagicMock(side_effect=lambda frame,view,target:view))
        runner._navigation_observation([])
        for call in (runner.local_navigation.observe.call_args,runner.vision.navigate.call_args):
            self.assertEqual(call.args[1],{**self.target,'navigation_phase':'return_home'})
            self.assertIsNot(call.args[1],self.target)
        self.assertEqual(runner.local_navigation.check_model_home_action.call_args.args[2]['navigation_phase'],'return_home')
        self.assertNotIn('navigation_phase',self.target)

    def test_startup_and_wakeup_can_proceed_to_home_then_target(self):
        ready={**self.view,'evidence':'selected detail'}
        runner=self.navigation_runner([self.navigation_view('start','click','START'),
            self.navigation_view('wake_up','click','开始唤醒'),self.navigation_view('home'),ready,ready])
        runner._navigate()
        self.assertEqual([call.args[0]['target'] for call in runner.input.call_args_list],['START','开始唤醒'])
        self.assertEqual(runner.ready_frame['screenshot_id'],'4')

    def test_unknown_login_and_resource_prompts_never_proceed_to_target(self):
        for page,sensitive in (('unknown',False),('login_required',False),('home',True)):
            with self.subTest(page=page):
                runner=self.navigation_runner([self.navigation_view(page,'ask',resource_sensitive=sensitive)])
                with self.assertRaises(BattleError):runner._navigate()
                self.assertEqual(runner.navigation_phase,'return_home')
                self.assertFalse(hasattr(runner,'ready_ref'));runner.input.assert_not_called()

    def test_home_and_target_phases_share_forty_step_limit(self):
        runner=self.navigation_runner([self.navigation_view('start')]*20+
            [self.navigation_view('home')]+[self.navigation_view('stage_map')]*20)
        with self.assertRaises(BattleError) as caught:runner._navigate()
        self.assertEqual(caught.exception.code,'navigation_limit')
        self.assertEqual(runner.snapshot.call_count,40)
        self.assertEqual(runner.navigation_phase,'to_target');runner.input.assert_not_called()

    def test_return_home_repeated_back_without_page_change_stops(self):
        runner=self.navigation_runner([self.navigation_view('settings','click','返回')]*4)
        with self.assertRaises(BattleError) as caught:runner._navigate()
        self.assertEqual(caught.exception.code,'navigation_no_progress')
        self.assertEqual(runner.input.call_count,3)
        self.assertEqual(runner.navigation_phase,'return_home')

    def attach_input_transport(self, runner, responses):
        del runner.input
        runner.context={'task_id':'task', 'dispatch_id':'navigation'}
        runner.sequence=0
        runner.store=MagicMock()
        runner.store.prepare_operation.side_effect=lambda *a,**kw: {'operation_id':str(runner.sequence)}
        runner.store.finish_operation.side_effect=lambda oid,status,result: {
            'operation_id':oid,'status':status,'submitted_at':runner.store.get_operation(oid)['submitted_at']}
        runner.supervisor=MagicMock(target_identity='target')
        responses=iter(responses)
        def request(*args, **kw):
            reply, submitted=next(responses)
            runner.store.get_operation.return_value={'submitted_at':submitted}
            return reply
        runner.supervisor.request.side_effect=request

    def test_map_transition_reobserves_new_coordinates_after_confirmed_non_submission(self):
        ready={**self.view,'evidence':'selected detail'}
        stage=self.navigation_view('stage_map','click','UT-EX-1')
        settled={**stage,'target_box':{**stage['target_box'],'x':.4}}
        runner=self.navigation_runner([self.navigation_view('home'),stage,settled,ready,ready])
        self.attach_input_transport(runner,[
            ({'status':'not_issued','submission_entered':False,'error':'game_target_changed'},None),
            ({'status':'completed','submission_entered':True},'later')])
        runner._navigate()
        args=[call.args[0]['args'] for call in runner.supervisor.request.call_args_list]
        self.assertEqual([arg['screenshot_id'] for arg in args],['1','2'])
        self.assertNotEqual(args[0]['x'],args[1]['x'])
        self.assertEqual(runner.ready_frame['screenshot_id'],'4')

    def test_map_continuously_changing_stops_after_two_new_observations(self):
        stage=self.navigation_view('stage_map','click','UT-EX-1')
        runner=self.navigation_runner([self.navigation_view('home'),stage,stage,stage,stage])
        self.attach_input_transport(runner,[
            ({'status':'not_issued','submission_entered':False,'error':'game_target_changed'},None)]*4)
        with self.assertRaises(BattleError) as caught:
            runner._navigate()
        self.assertEqual(caught.exception.code,'navigation_target_unstable')
        self.assertEqual(runner.supervisor.request.call_count,3)
        self.assertFalse(hasattr(runner,'ready_ref'))

    def test_only_proven_unissued_game_navigation_can_reobserve(self):
        reply={'status':'not_issued','submission_entered':False,'error':'game_target_changed'}
        for surface, action, starts, changes, submitted in (
            ('game','click',False,{},'submitted'),
            ('game','click',False,{'submission_entered':True},None),
            ('game','click',False,{'submission_entered':None},None),
            ('game','click',False,{'status':'unknown'},None),
            ('game','click',False,{'error':'game_geometry_changed'},None),
            ('game','restore',False,{},None),
            ('maa','click',True,{},None),
            ('maa','click',False,{},None),
        ):
            with self.subTest(surface=surface, action=action, changes=changes, submitted=submitted):
                runner=self.navigation_runner([])
                self.attach_input_transport(runner,[({**reply,**changes},submitted)])
                with self.assertRaises(BattleError) as caught:
                    runner.input({'surface':surface,'action':action}, starts=starts)
                self.assertEqual(caught.exception.code,'input_unverified')
                self.assertEqual(runner.supervisor.request.call_count,1)

    def test_invalid_navigation_recaptures_twice_and_never_repeats_an_input(self):
        from shu.maa_battle_vision import BattleVisionError
        invalid=lambda:BattleVisionError('battle_vision_invalid_output','invalid fixture')
        ready={**self.view,'evidence':'visible selected detail'}
        runner=self.navigation_runner([invalid(),invalid(),self.navigation_view('home'),ready,ready])
        runner._navigate()
        self.assertEqual(runner.snapshot.call_count,5)
        self.assertEqual(len({call.args[0]['screenshot_id'] for call in runner.vision.navigate.call_args_list}),5)
        self.assertEqual(runner.ready_frame['screenshot_id'],'4')
        runner.input.assert_not_called()
        self.assertEqual(sum(call.args[0]=='vision-invalid-output' for call in runner.publish.call_args_list),2)

    def test_continuous_invalid_or_invalid_second_ready_observation_cannot_start(self):
        from shu.maa_battle_vision import BattleVisionError
        ready={**self.view,'evidence':'visible selected detail'}
        for prefix in ([],[self.navigation_view('home'),ready]):
            with self.subTest(second_ready=bool(prefix)):
                errors=[BattleVisionError('battle_vision_invalid_output','invalid fixture') for _ in range(3)]
                runner=self.navigation_runner(prefix+errors)
                with self.assertRaises(BattleVisionError):runner._navigate()
                self.assertEqual(runner.snapshot.call_count,len(prefix)+3)
                self.assertFalse(hasattr(runner,'ready_ref'))
                runner.input.assert_not_called()

    def local_fallback_runner(self):
        from shu.maa_battle import BattleRunner
        runner=BattleRunner.__new__(BattleRunner)
        runner.context={'contract':{}};runner.cancel=threading.Event()
        runner.roles=SimpleNamespace(battle_expected_job=SimpleNamespace(
            stage_code='UT-EX-1',mystery_code='prts://123'))
        missing={'page':'copilot','running':False,'multi_mode':False,
                 'mystery_code':'','stage_code':'','preview_title':'','start_box':None}
        runner.vision=SimpleNamespace(inspect_maa=MagicMock(return_value=missing))
        local={'ok':True,'image_sha256':self.frame['image_sha256'],'page':'copilot',
            'idle':True,'running':False,'single_job':True,'expected_title_visible':True,
            'expected_stage_visible':True,'codes':['prts://123'],
            'anchors':{'start':{'rect':{'x':200,'y':100,'width':150,'height':80}}}}
        runner._local=MagicMock(return_value=local);runner.publish=MagicMock()
        document={'doc':{'title':'Example UT-EX-1'},'stage_name':'unit','actions':[{}]}
        return runner,local,document,missing

    def test_loaded_local_fallback_requires_exact_frame_code_and_known_page_state(self):
        runner,local,document,missing=self.local_fallback_runner()
        view=runner._loaded_view(self.frame,document)
        self.assertEqual(loaded_target(view,document,{})['mystery_code'],'prts://123')
        self.assertEqual(view['start_box']['x'],200/1920)
        self.assertEqual(runner._local.call_args.kwargs['expected']['title'],document['doc']['title'])
        for changes in ({'image_sha256':'0'*64},{'codes':['prts://999']},
                        {'page':'unknown'},{'idle':None},{'single_job':None},
                        {'expected_title_visible':False},{'expected_stage_visible':False}):
            with self.subTest(changes=changes):
                runner._local.return_value={**local,**changes}
                self.assertIs(runner._loaded_view(self.frame,document),missing)
        runner.roles.battle_expected_job=None;runner._local.reset_mock()
        self.assertIs(runner._loaded_view(self.frame,document),missing)
        runner._local.assert_not_called()

    def test_prestart_unverified_reads_new_frames_before_returning_a_bound_target(self):
        runner,_,document,missing=self.local_fallback_runner()
        runner.document=document;runner.guard=MagicMock();runner._single_mode=MagicMock()
        runner._import_bound_target=MagicMock(side_effect=lambda target,raw:target)
        runner.input=MagicMock()
        frames=[{**self.frame,'screenshot_id':str(i)} for i in range(3)]
        runner.snapshot=MagicMock(side_effect=frames)
        valid={'page':'copilot','running':False,'multi_mode':False,'stage_code':'UT-EX-1',
               'mystery_code':'prts://123','preview_title':'Example UT-EX-1',
               'start_box':{'x':.2,'y':.1,'width':.1,'height':.1}}
        # Nonempty but malformed transcription reaches the bounded read-only retry.
        invalid={**missing,'stage_code':'UT-EX-1'}
        runner._loaded_view=MagicMock(side_effect=[invalid,invalid,valid])
        with patch('shu.maa_queue_receipt._read_stable',return_value=(b'cache-bytes',{})), \
                patch.object(runner.cancel,'wait',return_value=False):
            observed,view,target,raw=runner._prestart_loaded()
        self.assertEqual(observed['screenshot_id'],'2')
        self.assertEqual(runner._single_mode.call_count,3)
        self.assertEqual(runner.guard.call_count,3)
        runner._import_bound_target.assert_called_once_with(target,b'cache-bytes')
        runner.input.assert_not_called()

    def test_focus_denial_waits_readonly_then_continues_on_real_foreground(self):
        ready={**self.view,'evidence':'selected detail'}
        runner=self.navigation_runner([self.navigation_view('home'),ready,ready]);runner.navigation_mode='foreground'
        runner.focus.side_effect=BattleError('focus_not_acquired','fixture')
        frames=[{**self.frame,'screenshot_id':str(i),
                 'window':{**self.frame['window'],'foreground':i>0}} for i in range(5)]
        runner.snapshot.side_effect=frames
        with patch.object(runner.cancel,'wait',return_value=False):runner._navigate()
        runner.focus.assert_called_once_with('game')
        self.assertEqual(runner.vision.navigate.call_count,3)
        self.assertEqual(runner.ready_frame['screenshot_id'],'4')
        runner.input.assert_not_called()

    def test_foreground_wait_cancellation_and_timeout_never_reach_vision_or_input(self):
        from shu.core import ActionRejected
        for mode in ('cancel','timeout'):
            with self.subTest(mode=mode):
                runner=self.navigation_runner([]);runner.navigation_mode='foreground'
                runner.focus.side_effect=BattleError('focus_not_acquired','fixture')
                runner.snapshot.return_value={**self.frame,'window':{**self.frame['window'],'foreground':False}}
                runner.snapshot.side_effect=None
                def guard():
                    if runner.cancel.is_set():raise ActionRejected('cancelled fixture')
                runner.guard.side_effect=guard
                def wait(_):
                    runner.cancel.set();return True
                with patch.object(runner.cancel,'wait',side_effect=wait), \
                        patch('shu.maa_battle.time.monotonic',side_effect=[100,191] if mode=='timeout' else [100,101]):
                    with self.assertRaises(BattleError if mode=='timeout' else ActionRejected) as caught:
                        runner._navigate()
                if mode=='timeout':self.assertEqual(caught.exception.code,'game_foreground_timeout')
                runner.focus.assert_called_once_with('game')
                runner.vision.navigate.assert_not_called();runner.input.assert_not_called()


if __name__=='__main__':unittest.main()

"""Offline failure-boundary checks; no native GUI, model, network or DB IO."""
import copy
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from shu.battle_contract import authorize
from shu.maa_battle import BattleError, BattleRunner, _content_hash
from shu.maa_battle_vision import BattleVisionError
from shu.providers import ProviderConfig


IDENTITY = {'hwnd': 1, 'pid': 2, 'creation_time': 3,
            'executable': r'D:\MAA\MAA-v6.21.4-win-x64\MAA.exe'}
BOX = {'x': .2, 'y': .1, 'width': .15, 'height': .1}


def frame(identifier='f', width=1000):
    return {'screenshot_id': identifier, 'image_path': identifier+'.png', 'image_sha256': 'a'*64,
            'window': {**IDENTITY, 'foreground': True, 'client_rect': {'width': width, 'height': 1080}}}


def bare_runner():
    runner = BattleRunner.__new__(BattleRunner)
    runner.context = {'task_id':'test-task','revision':1,'epoch':1,'dispatch_id':'dispatch',
        'contract': authorize({'user_request':'开始MAA自动战斗一次', 'current_deliverable':'gameplay',
                               'task_spec':{'kind':'repeat_stage'}})}
    runner.store = MagicMock()
    runner.store.current.return_value = {'control_seq':4, 'writes_enabled':False}
    runner.cancel = threading.Event()
    runner.stopping = threading.Event()
    runner._stop_lock = threading.Lock()
    runner.start_operation = 'start-op'
    runner.running_ref = {'path':'running.json','sha256':'a'*64}
    runner.evidence = []
    runner.stop_result = None
    runner.supervisor = MagicMock(target_identity=IDENTITY)
    runner.vision = SimpleNamespace(client=SimpleNamespace(config=ProviderConfig()))
    runner.snapshot = MagicMock(return_value=frame())
    runner._fresh_target = MagicMock(return_value=frame('fresh'))
    runner._observation = MagicMock(side_effect=lambda f,s:{'path':f['screenshot_id']+'.json','sha256':'b'*64})
    runner._settle = MagicMock()
    runner._state = MagicMock(return_value={'idle':True,'running':False})
    return runner


class BattleRunnerStopTests(unittest.TestCase):
    def prepare(self):
        runner = bare_runner()
        operations = {'start-op':{'operation_id':'start-op','submitted_at':'submitted'}}
        order = []
        def prepare(task_id, sequence, key, action):
            identifier = 'focus-op' if action.get('phase')=='focus' else 'stop-op'
            operations[identifier] = {'operation_id':identifier,'submitted_at':None}
            return dict(operations[identifier])
        def submit(identifier):
            operations[identifier]['submitted_at'] = 'submitted'
            return dict(operations[identifier])
        runner.store.prepare_control_operation.side_effect = prepare
        runner.store.get_operation.side_effect = lambda identifier:dict(operations[identifier])
        runner.store.enter_submission.side_effect = submit
        focused = False
        def request(command=None, *, admit_callback=None, **kwargs):
            nonlocal focused
            if command is None:
                order.append('revoke')
                return {'status':'not_issued'}
            action=command['args']['action']
            order.append(action)
            admit_callback()
            if action=='focus':
                focused=True
                return {'status':'completed','result':{'focus_acquired':True}}
            return {'status':'completed','result':{}}
        runner.supervisor.request_stop.side_effect = request
        return runner, order

    def test_background_maa_stop_activates_and_reobserves_before_click(self):
        runner, order=self.prepare()
        background=frame('background');background['window']['foreground']=False
        runner.snapshot.side_effect=[background,frame('activated'),frame('idle1'),frame('idle2')]
        inspector=MagicMock()
        inspector.inspect_maa.return_value={'running':True,'stop_box':BOX}
        with patch('shu.maa_battle.BattleVision',return_value=inspector), patch('shu.maa_battle.time.sleep'):
            result=runner.stop('test-task',control_seq=4)
        self.assertTrue(result['external_stop_confirmed'])
        self.assertEqual(order,['revoke','focus','click'])
        self.assertEqual(inspector.inspect_maa.call_args.args[0]['screenshot_id'],'activated')
        self.assertEqual(runner._fresh_target.call_args.args[1]['screenshot_id'],'activated')
        self.assertTrue(runner._fresh_target.called)
        self.assertTrue(runner._settle.called)

    def test_changed_stop_target_is_not_clicked(self):
        runner, order=self.prepare()
        inspector=MagicMock()
        inspector.inspect_maa.return_value={'running':True,'stop_box':BOX}
        runner._fresh_target.side_effect=BattleError('target_changed','window switched after model returned')
        with patch('shu.maa_battle.BattleVision',return_value=inspector):
            with self.assertRaises(BattleError):
                runner.stop('test-task',control_seq=4)
        self.assertEqual(order,['revoke'])
        self.assertFalse(runner._settle.called)

    def test_run_finishing_during_automatic_stop_activation_is_reconciled(self):
        runner,order=self.prepare();runner.running_ref=None
        background=frame('running-background');background['window']['foreground']=False
        runner.snapshot.side_effect=[background,frame('ended-after-focus'),frame('idle-confirmed')]
        inspector=MagicMock()
        inspector.inspect_maa.side_effect=[{'page':'copilot','running':True,'stop_box':BOX},
            {'page':'copilot','running':False,'start_box':BOX}]
        with patch('shu.maa_battle.BattleVision',return_value=inspector),patch('shu.maa_battle.time.sleep'):
            result=runner.stop('test-task',control_seq=4)
        self.assertTrue(result['external_stop_confirmed'])
        self.assertEqual(order,['revoke','focus'])
        self.assertEqual(runner.running_ref['path'],'running-background.json')
        runner._fresh_target.assert_not_called();runner._settle.assert_called_once()

    def test_foreground_denial_during_stop_never_admits_button(self):
        runner,order=self.prepare()
        background=frame();background['window']['foreground']=False
        runner.snapshot.return_value=background
        inspector=MagicMock();inspector.inspect_maa.return_value={'running':True,'stop_box':BOX}
        original=runner.supervisor.request_stop.side_effect
        def denied(command=None,**kwargs):
            answer=original(command,**kwargs)
            if command and command['args']['action']=='focus':
                answer['result']={'ok':True,'action':'focus','focus_acquired':False}
            return answer
        runner.supervisor.request_stop.side_effect=denied
        with patch('shu.maa_battle.BattleVision',return_value=inspector):
            result=runner.stop('test-task',control_seq=4)
        self.assertFalse(result['external_stop_confirmed'])
        self.assertEqual(order,['revoke','focus'])
        runner._fresh_target.assert_not_called();runner._settle.assert_not_called()

    def test_still_minimized_after_known_focus_denial_never_clicks_stop(self):
        runner, order=self.prepare()
        runner.snapshot.side_effect=BattleError('minimized','minimized')
        original=runner.supervisor.request_stop.side_effect
        def denied(command=None,**kwargs):
            answer=original(command,**kwargs)
            if command and command['args']['action']=='focus':
                answer['result']={'ok':True,'action':'focus','focus_acquired':False}
            return answer
        runner.supervisor.request_stop.side_effect=denied
        with self.assertRaisesRegex(BattleError,'minimized'):
            runner.stop('test-task',control_seq=4)
        self.assertEqual(order,['revoke','focus'])
        self.assertFalse(runner._settle.called)

    def test_restored_background_window_after_known_focus_denial_is_not_clicked(self):
        runner,order=self.prepare()
        restored=frame('restored');restored['window']['foreground']=False
        runner.snapshot.side_effect=[BattleError('minimized','minimized'),restored,frame('idle1'),frame('idle2')]
        original=runner.supervisor.request_stop.side_effect
        def denied(command=None,**kwargs):
            answer=original(command,**kwargs)
            if command and command['args']['action']=='focus':
                answer['result']={'ok':True,'action':'focus','focus_acquired':False}
            return answer
        runner.supervisor.request_stop.side_effect=denied
        inspector=MagicMock();inspector.inspect_maa.return_value={'running':True,'stop_box':BOX}
        with patch('shu.maa_battle.BattleVision',return_value=inspector),patch('shu.maa_battle.time.sleep'):
            result=runner.stop('test-task',control_seq=4)
        self.assertFalse(result['external_stop_confirmed'])
        self.assertEqual(order,['revoke','focus'])
        self.assertFalse(runner._fresh_target.called)
        self.assertEqual(inspector.inspect_maa.call_args.args[0]['screenshot_id'],'restored')

    def test_unknown_focus_dispatch_does_not_proceed_to_background_stop(self):
        runner,order=self.prepare()
        runner.snapshot.side_effect=BattleError('minimized','minimized')
        original=runner.supervisor.request_stop.side_effect
        def unknown(command=None,**kwargs):
            answer=original(command,**kwargs)
            if command and command['args']['action']=='focus':
                answer={'status':'unknown','result':{'ok':True,'action':'focus','focus_acquired':False}}
            return answer
        runner.supervisor.request_stop.side_effect=unknown
        self.assertFalse(runner.stop('test-task',control_seq=4)['external_stop_confirmed'])
        self.assertEqual(order,['revoke','focus'])
        self.assertEqual(runner.snapshot.call_count,1)

    def test_stop_accepts_supervisor_stopping_receipt_but_still_requires_idle_readback(self):
        runner,order=self.prepare()
        original=runner.supervisor.request_stop.side_effect
        def stopping(command=None,**kwargs):
            answer=original(command,**kwargs)
            if answer.get('status')=='completed':answer['status']='stopping'
            return answer
        runner.supervisor.request_stop.side_effect=stopping
        inspector=MagicMock();inspector.inspect_maa.return_value={'running':True,'stop_box':BOX}
        with patch('shu.maa_battle.BattleVision',return_value=inspector),patch('shu.maa_battle.time.sleep'):
            result=runner.stop('test-task',control_seq=4)
        self.assertTrue(result['external_stop_confirmed'])
        self.assertEqual(order,['revoke','click'])
        self.assertTrue(runner._settle.called)


    def test_stop_before_this_runner_started_never_stops_another_maa_run(self):
        runner, order=self.prepare()
        runner.start_operation=None
        result=runner.stop('test-task',control_seq=4)
        self.assertFalse(result['external_stop_confirmed'])
        self.assertEqual(order,['revoke'])
        self.assertFalse(runner.snapshot.called)

    def test_prepared_start_id_without_submission_never_authorizes_external_stop(self):
        runner, order=self.prepare()
        original=runner.store.get_operation.side_effect
        runner.store.get_operation.side_effect=lambda identifier:(
            {'operation_id':'start-op','state':'issued','submitted_at':None}
            if identifier=='start-op' else original(identifier))
        with patch('shu.maa_battle.BattleVision') as vision:
            result=runner.stop('test-task',control_seq=4,timeout_stop=True)
        self.assertFalse(result['external_stop_confirmed'])
        self.assertEqual(order,['revoke'])
        self.assertTrue(runner.stopping.is_set())
        self.assertFalse(runner.store.prepare_control_operation.called)
        self.assertFalse(runner.store.control.called)
        self.assertFalse(runner.snapshot.called)
        self.assertFalse(vision.called)

    def test_start_receipt_without_submission_field_is_also_not_authority(self):
        runner, order=self.prepare()
        runner.store.get_operation.return_value={'operation_id':'start-op','state':'prepared'}
        runner.store.get_operation.side_effect=None
        result=runner.stop('test-task',control_seq=4)
        self.assertFalse(result['external_stop_confirmed'])
        self.assertEqual(order,['revoke'])
        self.assertFalse(runner.snapshot.called)

    def test_helper_rejecting_start_before_admission_does_not_grant_stop_authority(self):
        runner, order=self.prepare()
        runner.start_operation=None;runner.sequence=0;runner.guard=MagicMock()
        runner.publish=MagicMock(return_value={'path':'not-issued.json','sha256':'a'*64})
        runner.store.prepare_operation.return_value={'operation_id':'planned-start'}
        runner.store.get_operation.side_effect=lambda identifier:{
            'operation_id':identifier,'status':'issued','submitted_at':None}
        runner.supervisor.request.return_value={'status':'not_issued','error':'admission_rejected'}
        with self.assertRaises(BattleError):
            runner.input({'surface':'maa','action':'click','target':'battle_start'},starts=True)
        self.assertEqual(runner.start_operation,'planned-start')
        self.assertFalse(runner.store.enter_submission.called)
        result=runner.stop('test-task',control_seq=4,timeout_stop=True)
        self.assertFalse(result['external_stop_confirmed'])
        self.assertEqual(order,['revoke'])
        self.assertFalse(runner.store.prepare_control_operation.called)
        self.assertFalse(runner.snapshot.called)


class BattleRunnerCaptureRecoveryTests(unittest.TestCase):
    def prepare(self,responses):
        runner=bare_runner();runner.stop_deadline=None
        runner.snapshot=BattleRunner.snapshot.__get__(runner,BattleRunner)
        runner.supervisor.request.side_effect=responses
        runner.focus=MagicMock();runner.publish=MagicMock();runner.guard=MagicMock();runner.progress=MagicMock()
        return runner

    def test_prestart_minimized_capture_uses_admitted_focus_then_new_capture(self):
        restored=frame('restored');restored['window']['foreground']=False
        runner=self.prepare([{'status':'not_issued','error':'minimized'},
                             {'status':'completed','result':restored}])
        result=runner.snapshot('maa')
        self.assertIs(result,restored)
        runner.focus.assert_called_once_with('maa')
        self.assertEqual(runner.supervisor.request.call_count,2)
        self.assertEqual(runner.start_operation,'start-op')
        runner.store.prepare_operation.assert_not_called()

    def test_post_start_monitor_never_focuses_a_minimized_maa_window(self):
        runner=self.prepare([{'status':'not_issued','error':'minimized'}])
        runner.store.get_operation.return_value={'submitted_at':'confirmed-start'}
        with self.assertRaises(BattleError) as raised:
            runner.snapshot('maa', keep_background=True)
        self.assertEqual(raised.exception.code,'monitor_maa_minimized')
        runner.focus.assert_not_called()
        runner.store.prepare_operation.assert_not_called()

    def test_known_foreground_denial_can_still_read_a_restored_background_maa(self):
        runner=self.prepare([{'status':'not_issued','error':'minimized'},
                             {'status':'completed','result':frame('restored')}])
        runner.focus.side_effect=BattleError('focus_not_acquired','known no content input')
        self.assertEqual(runner.snapshot('maa')['screenshot_id'],'restored')
        runner.focus.assert_called_once_with('maa')

    def test_capture_recovery_is_bounded_and_never_swallows_unknown_or_cancellation(self):
        for condition in ('still_minimized','unknown_focus','cancelled','stopping','other_error'):
            with self.subTest(condition=condition):
                response={'status':'not_issued','error':'minimized' if condition!='other_error' else 'binding_mismatch'}
                runner=self.prepare([response,response])
                if condition=='unknown_focus':runner.focus.side_effect=BattleError('input_unverified','unknown')
                if condition=='cancelled':
                    def revoked(_):runner.cancel.set()
                    runner.focus.side_effect=revoked
                    runner.guard.side_effect=lambda:(_ for _ in ()).throw(BattleError('cancelled','cancelled'))
                if condition=='stopping':runner.stopping.set()
                with self.assertRaises(BattleError):runner.snapshot('maa')
                self.assertLessEqual(runner.focus.call_count,1)
                self.assertEqual(runner.supervisor.request.call_count,2 if condition=='still_minimized' else 1)



class BattleRunnerPostStartTests(unittest.TestCase):
    def test_invalid_monitor_frame_recovers_and_completes_without_stopping_or_starting_again(self):
        self._post_start_failure(recover_monitor=True)

    def test_failed_automatic_maa_activation_never_submits_start(self):
        self._post_start_failure(focus_error=BattleError('focus_not_acquired','denied'))

    def test_unexpected_type_error_after_start_still_requests_external_stop(self):
        self._post_start_failure()

    def test_initial_minimized_maa_defers_to_existing_capture_recovery(self):
        self._post_start_failure(minimized=True)

    def _post_start_failure(self, *, minimized=False, focus_error=None, recover_monitor=False):
        runner=bare_runner()
        runner.start_operation=None
        runner.roles=SimpleNamespace(_supervisor_lock=threading.Lock(),supervisor=None,
                                    directory=Path('unused-offline-root'))
        runner.guard=MagicMock()
        runner.store.pending_operations.return_value=[]
        runner.store.get_operation.return_value={'submitted_at':'already-submitted'}
        runner.progress=MagicMock()
        runner.publish=MagicMock()
        order=[]
        def focus(surface):
            order.append('focus:'+surface)
            if focus_error:raise focus_error
        runner.focus=MagicMock(side_effect=focus)
        runner._single_mode=MagicMock()
        runner.document={'doc':{'title':'UT-1'},'stage_name':'unit','actions':[{}]}
        raw=json.dumps(runner.document).encode()
        import hashlib
        runner.document_hash=hashlib.sha256(raw).hexdigest()
        runner.roster_hash=_content_hash({'operators':[]})
        target={'stage_code':'UT-1','difficulty':'normal','mystery_code':'prts://123','title':'UT-1','stage_id':'unit'}
        runner._loaded=MagicMock(return_value=target)
        runner._navigate=MagicMock(side_effect=lambda:order.append('navigate'))
        real_prestart=runner._prestart_loaded
        def prestart():
            order.append('prestart')
            return real_prestart()
        runner._prestart_loaded=MagicMock(side_effect=prestart)
        runner.ready_ref={'path':'ready.json','sha256':'c'*64}
        runner.ready_frame=frame()
        runner.vision.inspect_maa=MagicMock(return_value={'start_box':BOX})
        runner.snapshot.side_effect=lambda surface, **kwargs:frame(surface+str(runner.snapshot.call_count))
        if recover_monitor:
            runner.running_ref=None
            runner.cancel.wait=MagicMock(return_value=False)
            runner._state.side_effect=[BattleVisionError('battle_vision_invalid_output','bad observation'),
                {'running':True,'idle':False},{'running':False,'idle':True},{'running':False,'idle':True}]
            runner._settle.return_value={'path':'idle.json','sha256':'9'*64}
        else:
            runner._state.side_effect=TypeError('unexpected monitor payload shape')
        runner.stop=MagicMock(return_value={'external_stop_confirmed':True,'summary':'stopped'})
        def start_input(args, *, starts=False):
            if starts:
                order.append('start');runner.start_operation='start-op'
        runner.input=MagicMock(side_effect=start_input)
        game=SimpleNamespace(is_game_client=True,hwnd=4,pid=5,executable=r'D:\Game\Arknights.exe')
        native=MagicMock();native.creation_time.return_value=6
        native.permission.return_value={'known':True,'input_allowed':True,'own_integrity':12288,'target_integrity':8192}
        windows=MagicMock();windows.list_windows.return_value=[game]
        roster=MagicMock();roster.current.return_value={'operators':[]}
        with patch('shu.maa_visual_native.discover_maa_windows',return_value=[{**IDENTITY,'minimized':minimized}]), \
             patch('shu.maa_visual_native._NativeMaaBackend',return_value=native), \
             patch('shu.windows._NativeWindows',return_value=windows), \
             patch('shu.maa_battle.GuiSupervisor',return_value=runner.supervisor), \
             patch('shu.maa_queue_receipt._read_stable',return_value=(raw,{})), \
             patch('shu.maa_facts.RosterStore',return_value=roster), \
             patch('shu.maa_battle_settings.verify_single_run',return_value={'loop_limit':1,'settings_sha256':'a'*64}), \
             patch('shu.game_executor.compare_target_patch',return_value=True), \
             patch('shu.maa_battle.loaded_target',return_value=target):
            result=runner.run()
        runner.focus.assert_called_once_with('maa')
        if focus_error:
            self.assertFalse(result['battle_start_submitted'])
            runner.stop.assert_not_called();runner._prestart_loaded.assert_not_called()
            self.assertEqual(order,['navigate','focus:maa'])
            return
        if recover_monitor:
            self.assertEqual(result['status'],'battle_finished',result)
            self.assertEqual(runner._state.call_count,4)
            self.assertIsNotNone(runner.running_ref)
            runner._settle.assert_called_once()
            runner.stop.assert_not_called()
            self.assertEqual(order,['navigate','focus:maa','prestart','start'])
            return
        self.assertEqual(runner.stop.call_count,1,result)
        self.assertTrue(result['battle_start_submitted'])
        self.assertTrue(result['external_idle_confirmed'])
        self.assertEqual(result['status'],'needs_input')
        self.assertEqual(order,['navigate','focus:maa','prestart','start'])


if __name__=='__main__':unittest.main()

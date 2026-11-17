"""Bounded execution tests use fake windows and model responses only."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from PySide6.QtGui import QColor, QImage
from shu.game_executor import GameExecutor, GameExecutionError, compare_target_patch, parse_action
from shu.providers import ProviderCancelled
from shu.windows import WindowError


def response(kind, **kwargs):
    action={"kind":kind,"reason":"依据当前画面","expected_result":"显示新页面", "evidence":"当前页面中可见基建标题",
            "verification_of_previous":"not_applicable",**kwargs}
    if isinstance(kind,str) and kind in {"click","drag"}:action.update({"target_label":"测试按钮","x":.5,"y":.5,**kwargs})
    return {"role":"assistant","content":None,"tool_calls":[{"id":"call-1","type":"function",
            "function":{"name":"choose_game_action","arguments":json.dumps(action,ensure_ascii=False)}}]}


class FakeWindows:
    def __init__(self):
        self.captures=[];self.inputs=[];self.focus_count=0;self.enabled=True;self.on_capture=None;self.on_click=None
        self.permission_changes=[]
        self.focus_error=None;self.bound_reads=0
        self.bound_value=SimpleNamespace(hwnd=11,pid=22,foreground=False,minimized=False)
    def capture(self,*,background_only=False):
        if not background_only:raise AssertionError("Only strict background capture is permitted")
        count=len(self.captures)+1
        obs={"id":f"frame-{count}","window_id":11,"window_pid":22,"width":101,"height":201,
             "captured_at":123.,"path":f"fixture-frame-{count}.png"}
        self.captures.append(obs)
        if self.on_capture:self.on_capture(obs)
        return copy.deepcopy(obs)
    def focus_for_capture(self):
        self.focus_count+=1
        if self.focus_error:raise self.focus_error
    def bound_window(self):self.bound_reads+=1;return self.bound_value
    def controlled_click(self,x,y,observation_id,cancel_event=None):
        if cancel_event.is_set():raise ProviderCancelled()
        self.inputs.append(("click",x,y,observation_id))
        if self.on_click:self.on_click()
        return {"ok":True,"verification_required":True}
    def controlled_drag(self,x1,y1,x2,y2,duration_seconds,observation_id,cancel_event=None):
        if cancel_event.is_set():raise ProviderCancelled()
        self.inputs.append(("drag",x1,y1,x2,y2,duration_seconds,observation_id))
        return {"ok":True,"verification_required":True}
    def set_input_enabled(self,enabled):
        self.enabled=bool(enabled);self.permission_changes.append(bool(enabled))


class Decisions:
    def __init__(self,*items):self.items=list(items);self.calls=[];self.histories=[];self.hook=None
    def __call__(self,goal,observation,tools,history=None,cancel_event=None):
        self.calls.append(copy.deepcopy(observation))
        self.histories.append(copy.deepcopy(history))
        if self.hook:self.hook()
        return copy.deepcopy(self.items.pop(0))


class SimulatedTime:
    def __init__(self):self.now=0.;self.waits=[];self.on_wait=None
    def __call__(self):return self.now
    def wait(self,event,seconds):
        self.waits.append(seconds);self.now+=seconds
        if self.on_wait:self.on_wait(len(self.waits))
        return event.is_set()


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.windows=FakeWindows();self.cancel=threading.Event();self.events=[];self.guard_calls=0
    def guard(self):self.guard_calls+=1
    def run_loop(self,model,**kwargs):
        return GameExecutor(self.windows,model,self.guard,
            emit_event=lambda *event:self.events.append(event),compare_patch=lambda *args:True).run(
                "进入基建",self.cancel,**kwargs)
    def test_click_recaptures_before_input_and_verifies_on_post_input_frame(self):
        model=Decisions(response("click",x=.25,y=.75),response("finish",verification_of_previous="confirmed",summary="基建页面已打开"))
        result=self.run_loop(model)
        self.assertEqual(result["status"],"completed")
        self.assertEqual(self.windows.inputs,[("click",25,150,"frame-2")])
        self.assertEqual([obs["id"] for obs in model.calls],["frame-1","frame-3"])
        self.assertEqual(self.windows.focus_count,1)
        self.assertTrue(result["actions"][0]["verified"])
        self.assertTrue(result["model_goal_verified"])
        self.assertEqual(result["completion_evidence"]["source"],"model_visual_check")
        self.assertNotIn("completed_runs",result)
        self.assertEqual(self.windows.permission_changes,[])
        self.assertGreater(self.guard_calls,5)
    def test_drag_uses_normalized_client_pixels_and_bounded_duration(self):
        model=Decisions(response("drag",x=0,y=1,to_x=1,to_y=0,duration_seconds=.3),
                        response("ask",verification_of_previous="confirmed",summary="需要用户核对"))
        result=self.run_loop(model)
        self.assertEqual(self.windows.inputs,[("drag",0,200,100,0,.3,"frame-2")])
        self.assertEqual(result["status"],"needs_input")
    def test_target_patch_change_discards_old_coordinates_without_input(self):
        model=Decisions(response("click"),response("ask",summary="页面发生变化"))
        executor=GameExecutor(self.windows,model,self.guard,emit_event=lambda *e:self.events.append(e),compare_patch=lambda *a:False)
        result=executor.run("进入基建",self.cancel)
        self.assertEqual(self.windows.inputs,[])
        self.assertEqual(result["status"],"needs_input")
        self.assertEqual(model.calls[1]["id"],"frame-2")
        self.assertTrue(any(e[0]=="action_rejected" for e in self.events))
    def test_guard_revocation_while_model_runs_blocks_input(self):
        model=Decisions(response("click"))
        allowed=[True]
        def guard():
            if not allowed[0]:raise RuntimeError("permission revoked")
        model.hook=lambda:allowed.__setitem__(0,False)
        result=GameExecutor(self.windows,model,guard).run("进入基建",self.cancel)
        self.assertEqual(result["status"],"needs_input")
        self.assertEqual(self.windows.inputs,[])
        self.assertEqual(self.windows.focus_count,0)
    def test_cancelled_model_response_cannot_focus_or_click(self):
        model=Decisions(response("click"));model.hook=self.cancel.set
        result=self.run_loop(model)
        self.assertEqual(result["status"],"cancelled")
        self.assertEqual(self.windows.inputs,[])
        self.assertEqual(self.windows.focus_count,0)
    def test_cancelled_old_worker_cannot_disable_permission_for_next_run(self):
        model=Decisions(response("click"))
        def switch_to_next_run():
            self.cancel.set()
            self.windows.set_input_enabled(True)
        model.hook=switch_to_next_run
        result=self.run_loop(model)
        self.assertEqual(result["status"],"cancelled")
        self.assertTrue(self.windows.enabled)
        self.assertEqual(self.windows.permission_changes,[True])
        self.assertEqual(self.windows.inputs,[])
    def test_cancel_during_final_recapture_discards_decision_before_input(self):
        def captured(observation):
            if observation["id"]=="frame-2":self.cancel.set()
        self.windows.on_capture=captured
        result=self.run_loop(Decisions(response("click")))
        self.assertEqual(result["status"],"cancelled")
        self.assertEqual(self.windows.inputs,[])
    def test_cancel_during_patch_comparison_discards_decision_before_input(self):
        def compare(*_):self.cancel.set();return True
        result=GameExecutor(self.windows,Decisions(response("click")),self.guard,compare_patch=compare).run(
            "进入基建",self.cancel)
        self.assertEqual(result["status"],"cancelled")
        self.assertEqual(self.windows.inputs,[])
    def test_cancel_after_sent_input_preserves_input_record_but_stops_progress(self):
        self.windows.on_click=self.cancel.set
        result=self.run_loop(Decisions(response("click")))
        self.assertEqual(result["status"],"cancelled")
        self.assertEqual(len(result["actions"]),1)
        self.assertFalse(result["actions"][0]["verified"])
        self.assertIsNotNone(result['game_context']['pending_action'])
    def test_finish_requires_visible_evidence_and_prior_confirmation(self):
        for finish in [response("finish",verification_of_previous="unknown"),response("finish",verification_of_previous="confirmed",evidence="")]:
            with self.subTest(finish=finish):
                self.windows=FakeWindows()
                result=self.run_loop(Decisions(response("click"),finish))
                self.assertEqual(result["status"],"needs_input")
                self.assertFalse(result["model_goal_verified"])
    def test_already_satisfied_goal_uses_current_observation_without_input(self):
        result=self.run_loop(Decisions(response("finish",goal_already_satisfied=True)))
        self.assertEqual(result["status"],"completed")
        self.assertEqual(self.windows.inputs,[])
        self.assertEqual(self.windows.focus_count,0)
    def test_sensitive_resource_request_stops_before_focus_or_input(self):
        result=self.run_loop(Decisions(response("click",resource_sensitive=True)))
        self.assertEqual(result["status"],"needs_input")
        self.assertIn("额外资源",result["summary"])
        self.assertEqual(self.windows.inputs,[])
        self.assertEqual(self.windows.focus_count,0)
    def test_unknown_previous_result_cannot_authorize_another_input(self):
        result=self.run_loop(Decisions(response("click"),response("click",verification_of_previous="unknown")))
        self.assertEqual(len(self.windows.inputs),1)
        self.assertEqual(result["status"],"needs_input")
    def test_bad_coordinate_or_unbounded_drag_never_reaches_windows(self):
        for item in [response("click",x=500),response("click",x=True),response("drag",to_x=.8,to_y=.2,duration_seconds=8)]:
            with self.subTest(item=item):
                self.windows=FakeWindows()
                result=self.run_loop(Decisions(item))
                self.assertEqual(result["status"],"needs_input")
                self.assertEqual(self.windows.inputs,[])
    def test_prose_and_multiple_calls_are_not_executable(self):
        multiple=response("click");multiple["tool_calls"]*=2
        for item in [{"role":"assistant","content":"我已经点击"},multiple]:
            with self.subTest(item=item):
                self.windows=FakeWindows();result=self.run_loop(Decisions(item))
                self.assertEqual(result["status"],"needs_input")
                self.assertEqual(self.windows.inputs,[])
    def test_step_limit_is_bounded_without_game_input(self):
        model=Decisions(response("wait",wait_seconds=.1),response("wait",wait_seconds=.1))
        result=self.run_loop(model,max_steps=2)
        self.assertEqual(result["steps"],2)
        self.assertEqual(result["status"],"needs_input")
        self.assertEqual(self.windows.inputs,[])
    def test_model_latency_exceeding_deadline_cannot_authorize_input(self):
        clock=[0.]
        model=Decisions(response("click"));model.hook=lambda:clock.__setitem__(0,2.)
        result=GameExecutor(self.windows,model,self.guard,clock=lambda:clock[0]).run(
            "进入基建",self.cancel,max_seconds=1)
        self.assertEqual(result["status"],"needs_input")
        self.assertIn("时限",result["summary"])
        self.assertEqual(self.windows.inputs,[])
        self.assertEqual(self.windows.focus_count,0)
    def test_observation_event_payload_is_original_frame_not_nested_wrapper(self):
        self.run_loop(Decisions(response("ask")))
        observations=[details for kind,_,details in self.events if kind=="observation"]
        self.assertEqual([item["id"] for item in observations],["frame-1"])
        self.assertNotIn("observation",observations[0])
    def test_confirmed_base_then_rejected_back_does_not_create_pending_verification(self):
        model=Decisions(response('click',target_label='BASE'),
            response('click',target_label='BACK rejected',verification_of_previous='confirmed'),
            response('click',target_label='BACK corrected',verification_of_previous='unknown',
                     evidence='上一返回提案未执行；目前仍在基建'),
            response('finish',verification_of_previous='confirmed',evidence='已返回主页'))
        comparisons=iter([True,False,True])
        result=GameExecutor(self.windows,model,self.guard,
            compare_patch=lambda *args:next(comparisons)).run('进入基建后返回主页',self.cancel)
        self.assertEqual(result['status'],'completed')
        self.assertEqual([item[-1] for item in self.windows.inputs],['frame-2','frame-5'])
        self.assertEqual(len(result['actions']),2)
        self.assertTrue(all(item['verified'] for item in result['actions']))
        after_rejection=model.calls[2]['execution_context']
        self.assertIsNone(after_rejection['previous_action'])
        self.assertIsNone(after_rejection['pending_action'])
        self.assertTrue(after_rejection['last_executed_action']['verified'])
        self.assertEqual(after_rejection['last_proposal']['status'],'rejected')
        self.assertFalse(after_rejection['last_proposal']['executed_game_input'])
        self.assertEqual(after_rejection['last_proposal']['action']['target_label'],'BACK rejected')
    def test_confirmed_input_then_wait_can_finish_with_new_frame_evidence(self):
        model=Decisions(response('click'),response('wait',verification_of_previous='confirmed',wait_seconds=.1),
                        response('finish',verification_of_previous='unknown',evidence='新画面显示基建设施'))
        result=self.run_loop(model)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(self.windows.inputs),1)
        self.assertTrue(result['actions'][0]['verified'])
        self.assertIsNone(model.calls[2]['execution_context']['pending_action'])
        self.assertEqual(result['completion_evidence']['observation_id'],'frame-4')
        self.assertEqual(result['completion_evidence']['source'],'model_visual_check')
    def test_actual_pending_unknown_after_wait_still_blocks_another_click(self):
        model=Decisions(response('click'),response('wait',verification_of_previous='unknown',wait_seconds=.1),
                        response('click',verification_of_previous='unknown'))
        result=self.run_loop(model)
        self.assertEqual(result['status'],'needs_input')
        self.assertEqual(len(self.windows.inputs),1)
        self.assertFalse(result['actions'][0]['verified'])
        self.assertIsNotNone(model.calls[2]['execution_context']['pending_action'])
    def test_known_failed_input_is_cleared_before_following_corrective_proposal(self):
        model=Decisions(response('click'),response('wait',verification_of_previous='failed',wait_seconds=.1),
            response('click',verification_of_previous='unknown'),response('finish',verification_of_previous='confirmed'))
        result=self.run_loop(model)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(self.windows.inputs),2)
        self.assertFalse(result['actions'][0]['verified'])
        self.assertEqual(result['actions'][0]['verification']['value'],'failed')
        self.assertIsNone(model.calls[2]['execution_context']['pending_action'])
    def test_format_error_repairs_on_same_observation_without_new_capture(self):
        model=Decisions({'role':'assistant','content':'只有文字'},response('click'),
                        response('finish',verification_of_previous='confirmed'))
        result=self.run_loop(model)
        self.assertEqual(result['status'],'completed')
        self.assertEqual([o['id'] for o in model.calls],['frame-1','frame-1','frame-3'])
        self.assertEqual(len(self.windows.inputs),1)
        self.assertEqual(result['metrics']['repair_requests'],1)
        self.assertIn('唯一一条',model.histories[1][-1]['content'])
    def test_model_context_keeps_progress_without_repeating_verbose_old_rationale(self):
        verbose='历史长解释不应重复上传。'*100
        model=Decisions(response('click',reason=verbose,target_label='BASE'),
            response('wait',verification_of_previous='confirmed',wait_seconds=.1),response('ask'))
        result=self.run_loop(model)
        context=model.calls[2]['execution_context']
        self.assertNotIn(verbose,json.dumps(context,ensure_ascii=False))
        self.assertTrue(context['last_executed_action']['verified'])
        self.assertNotIn('input_result',context['last_executed_action'])
        self.assertTrue(context['recent_steps'][0]['outcome']['result_verified'])
        self.assertEqual(result['game_context']['recent_steps'][0]['action']['reason'],verbose)
    def test_format_repairs_stop_after_two_attempts_without_any_input(self):
        bad={'tool_calls':[]};model=Decisions(bad,bad,bad)
        result=self.run_loop(model)
        self.assertEqual(result['status'],'needs_input')
        self.assertEqual(result['metrics']['repair_requests'],2)
        self.assertEqual(len(model.calls),3);self.assertEqual(len(self.windows.captures),1)
        self.assertFalse(result['continuable']);self.assertEqual(self.windows.inputs,[])
    def test_segment_budget_preserves_pending_context_for_same_goal_continuation(self):
        first=self.run_loop(Decisions(response('click',screen_state='ready',state_summary='已点击基建，等待新画面')),max_steps=1)
        self.assertTrue(first['continuable']);self.assertEqual(first['stop_reason'],'decision_budget')
        self.assertIsNotNone(first['game_context']['pending_action'])
        self.assertFalse(first['game_context']['last_executed_action']['verified'])
        model=Decisions(response('finish',verification_of_previous='confirmed',evidence='新的基建设施画面'))
        resumed=self.run_loop(model,resume_context=first['game_context'])
        self.assertEqual(resumed['status'],'completed');self.assertEqual(len(self.windows.inputs),1)
        self.assertEqual(model.calls[0]['id'],'frame-4')
        self.assertIsNotNone(model.calls[0]['execution_context']['pending_action'])
        self.assertIsNone(resumed['game_context']['pending_action'])
        self.assertTrue(resumed['game_context']['last_executed_action']['verified'])
    def test_changed_goal_or_window_cannot_resume_or_replay_pending_input(self):
        first=self.run_loop(Decisions(response('click')),max_steps=1)
        wrong_goal=copy.deepcopy(first['game_context']);wrong_goal['goal']='另一个目标'
        with self.assertRaises(ValueError):self.run_loop(Decisions(),resume_context=wrong_goal)
        wrong_window=copy.deepcopy(first['game_context']);wrong_window['window_identity']['window_pid']=99
        result=self.run_loop(Decisions(),resume_context=wrong_window)
        self.assertEqual(result['error_code'],'resume_window_changed')
        self.assertFalse(result['continuable']);self.assertEqual(len(self.windows.inputs),1)
    def test_deadline_yields_checkpoint_but_cancellation_does_not_auto_continue(self):
        clock=SimulatedTime();model=Decisions(response('click'));model.hook=lambda:setattr(clock,'now',2.)
        result=GameExecutor(self.windows,model,self.guard,clock=clock,waiter=clock.wait).run('进入基建',self.cancel,max_seconds=1)
        self.assertTrue(result['continuable']);self.assertEqual(result['stop_reason'],'deadline')
        self.assertEqual(result['game_context']['goal'],'进入基建')
        self.cancel.set();cancelled=self.run_loop(Decisions())
        self.assertFalse(cancelled['continuable']);self.assertEqual(cancelled['status'],'cancelled')
    def test_local_loading_wait_uses_fresh_pixels_before_second_model_decision(self):
        from shu.observation_policy import ObservationPolicy
        from PySide6.QtGui import QColor,QImage
        with tempfile.TemporaryDirectory() as folder:
            paths=[]
            for name,color in [('loading','black'),('ready','white')]:
                image=QImage(101,201,QImage.Format.Format_RGB32);image.fill(QColor(color))
                path=Path(folder)/(name+'.png');image.save(str(path));paths.append(str(path))
            original=self.windows.capture
            def capture(**kwargs):
                item=original(**kwargs);item['path']=paths[0] if len(self.windows.captures)<=3 else paths[1]
                return item
            self.windows.capture=capture
            model=Decisions(response('wait',screen_state='loading',wait_seconds=.1),
                            response('finish',goal_already_satisfied=True,evidence='当前新画面已显示目标页面'))
            clock=SimulatedTime()
            result=GameExecutor(self.windows,model,self.guard,clock=clock,waiter=clock.wait,
                observation_policy=ObservationPolicy(clock=clock,poll_seconds=.1,loading_seconds=1.)).run('进入基建',self.cancel)
            self.assertEqual(result['status'],'completed')
            self.assertEqual(len(model.calls),2)
            self.assertEqual(model.calls[1]['id'],'frame-5')
            self.assertEqual(result['metrics']['model_requests'],2)
            self.assertEqual(result['metrics']['local_polls'],3)
            self.assertEqual(self.windows.inputs,[])
            self.assertFalse(model.calls[1]['observation_policy']['semantic_success'])
    def test_stream_loading_polls_never_enter_model_context_or_export_each_sample(self):
        from shu.observation_policy import ObservationPolicy
        from PySide6.QtGui import QColor,QImage
        with tempfile.TemporaryDirectory() as folder:
            paths=[]
            for name,color in [('loading','black'),('ready','white')]:
                image=QImage(101,201,QImage.Format.Format_RGB32);image.fill(QColor(color))
                path=Path(folder)/(name+'.png');image.save(str(path));paths.append(str(path))
            original=self.windows.capture
            def capture(**kwargs):
                frame=original(**kwargs);frame['path']=paths[0] if len(self.windows.captures)==1 else paths[1]
                return frame
            sample_count=[]
            def peek(**kwargs):
                sample_count.append(1);color=0 if len(sample_count)==1 else 255
                return {'sample_rgb':bytes([color])*6912,'sequence':len(sample_count),'generation':'test',
                    'actionable':False,'window_id':11,'window_pid':22,'width':101,'height':201}
            self.windows.capture=capture;self.windows.peek_observation_sample=peek
            model=Decisions(response('wait',screen_state='loading',wait_seconds=.1),
                response('finish',goal_already_satisfied=True,evidence='新的目标页面可见'))
            clock=SimulatedTime()
            result=GameExecutor(self.windows,model,self.guard,clock=clock,waiter=clock.wait,
                observation_policy=ObservationPolicy(clock=clock,poll_seconds=.1,loading_seconds=1.)).run('进入基建',self.cancel)
            self.assertEqual(result['status'],'completed')
            self.assertEqual(len(self.windows.captures),2)  # Initial + one final PNG evidence, never each sample.
            self.assertEqual(len(sample_count),3)
            self.assertEqual([frame['id'] for frame in model.calls],['frame-1','frame-2'])
            self.assertTrue(all('sample_rgb' not in frame for frame in model.calls))
            self.assertEqual(result['metrics']['model_requests'],2)


class ForegroundWaitTests(unittest.TestCase):
    def setUp(self):
        self.windows=FakeWindows();self.cancel=threading.Event();self.events=[];self.clock=SimulatedTime()
        self.windows.focus_error=WindowError('Windows 没有允许切换前台。','focus_denied')
    def run_loop(self,model,*,compare=None,max_seconds=180,guard=None):
        return GameExecutor(self.windows,model,guard or (lambda:None),
            emit_event=lambda *event:self.events.append(event),compare_patch=compare or (lambda *a:True),
            clock=self.clock,waiter=self.clock.wait).run('进入基建',self.cancel,max_seconds=max_seconds)
    def test_manual_foreground_switch_resumes_same_decision_after_fresh_capture(self):
        model=Decisions(response('click'),response('finish',verification_of_previous='confirmed'))
        def wait(number):
            if number<=3:
                self.assertEqual(len(model.calls),1)
                self.assertLessEqual(self.clock.waits[-1],.05)
            if number==3:self.windows.bound_value.foreground=True
        self.clock.on_wait=wait
        compared=[]
        result=self.run_loop(model,compare=lambda *args:compared.append(args) or True)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(self.windows.focus_count,1)
        self.assertEqual(self.windows.inputs,[('click',50,100,'frame-2')])
        self.assertEqual(len(model.calls),2)
        self.assertEqual([item['id'] for item in model.calls],['frame-1','frame-3'])
        self.assertEqual((compared[0][0]['id'],compared[0][1]['id']),('frame-1','frame-2'))
        self.assertEqual([e[0] for e in self.events].count('foreground_required'),1)
        self.assertEqual([e[0] for e in self.events].count('foreground_resumed'),1)
        self.assertTrue(result['foreground_waits'][0]['resumed'])
        self.assertAlmostEqual(result['foreground_waits'][0]['elapsed_seconds'],.15)
    def test_not_foreground_also_uses_one_activation_attempt_then_polls(self):
        self.windows.focus_error=WindowError('请切回游戏。','not_foreground')
        self.clock.on_wait=lambda _:setattr(self.windows.bound_value,'foreground',True)
        result=self.run_loop(Decisions(response('click'),response('finish',verification_of_previous='confirmed')))
        self.assertEqual(result['status'],'completed')
        self.assertEqual(self.windows.focus_count,1)
        self.assertEqual(result['foreground_waits'][0]['error_code'],'not_foreground')
    def test_twenty_second_timeout_stops_without_extra_model_call_or_input(self):
        self.windows.focus_error.details={'api':'SetForegroundWindow','raw_return':0,'last_error':0,
            'api_key':'DO_NOT_KEEP','nested':{'Authorization':'DO_NOT_KEEP','desktop':'Default'}}
        model=Decisions(response('click'))
        result=self.run_loop(model)
        self.assertEqual(result['status'],'needs_input')
        self.assertEqual(result['error_code'],'focus_denied')
        self.assertAlmostEqual(result['elapsed_seconds'],20.)
        self.assertLessEqual(max(self.clock.waits),.05)
        self.assertEqual(self.windows.focus_count,1)
        self.assertEqual(len(model.calls),1)
        self.assertEqual(len(self.windows.captures),1)
        self.assertEqual(self.windows.inputs,[])
        self.assertIn('保持前台',result['summary'])
        self.assertEqual(result['diagnostics']['api'],'SetForegroundWindow')
        self.assertNotIn('DO_NOT_KEEP',json.dumps(result))
        finished=self.events[-1][2]
        self.assertEqual(finished['error_code'],'focus_denied')
        self.assertEqual(finished['diagnostics'],result['diagnostics'])
    def test_cancel_during_wait_stops_promptly_without_input(self):
        self.clock.on_wait=lambda _:self.cancel.set()
        result=self.run_loop(Decisions(response('click')))
        self.assertEqual(result['status'],'cancelled')
        self.assertAlmostEqual(self.clock.now,.05)
        self.assertEqual(self.windows.inputs,[])
        self.assertEqual(self.windows.focus_count,1)
    def test_revoked_guard_during_wait_stops_without_exposing_arbitrary_exception(self):
        allowed=[True]
        self.clock.on_wait=lambda _:allowed.__setitem__(0,False)
        def guard():
            if not allowed[0]:raise RuntimeError('unknown diagnostic containing PRIVATE_VALUE')
        result=self.run_loop(Decisions(response('click')),guard=guard)
        self.assertEqual(result['status'],'needs_input')
        self.assertNotIn('PRIVATE_VALUE',json.dumps(result))
        self.assertEqual(self.windows.inputs,[])
        self.assertEqual(len(self.clock.waits),1)
    def test_foreground_wait_counts_against_overall_execution_deadline(self):
        model=Decisions(response('click'))
        result=self.run_loop(model,max_seconds=1)
        self.assertEqual(result['status'],'needs_input')
        self.assertIn('执行时限',result['summary'])
        self.assertAlmostEqual(result['elapsed_seconds'],1.)
        self.assertEqual(len(model.calls),1)
        self.assertEqual(self.windows.inputs,[])
        self.assertFalse(result['foreground_waits'][0]['resumed'])
    def test_closed_changed_or_minimized_binding_is_not_waited_around(self):
        for value,code in [(None,'window_missing'),
                (SimpleNamespace(hwnd=11,pid=23,foreground=True,minimized=False),'window_identity_changed'),
                (SimpleNamespace(hwnd=12,pid=22,foreground=True,minimized=False),'window_identity_changed'),
                (SimpleNamespace(hwnd=11,pid=22,foreground=False,minimized=True),'minimized')]:
            with self.subTest(code=code,value=value):
                self.windows=FakeWindows();self.clock=SimulatedTime()
                self.windows.focus_error=WindowError('无法聚焦','focus_denied');self.windows.bound_value=value
                result=self.run_loop(Decisions(response('click')))
                self.assertEqual(result['status'],'needs_input')
                self.assertEqual(result['error_code'],code)
                self.assertEqual(self.clock.waits,[])
                self.assertEqual(self.windows.inputs,[])
    def test_other_focus_errors_do_not_start_foreground_wait(self):
        self.windows.focus_error=WindowError('游戏已最小化。','minimized')
        result=self.run_loop(Decisions(response('click')))
        self.assertEqual(result['error_code'],'minimized')
        self.assertEqual(self.windows.bound_reads,0)
        self.assertEqual(self.clock.waits,[])
        self.assertNotIn('foreground_waits',result)
        self.assertFalse(any(e[0]=='foreground_required' for e in self.events))
    def test_target_changed_while_user_switches_foreground_discards_old_input(self):
        self.clock.on_wait=lambda _:setattr(self.windows.bound_value,'foreground',True)
        model=Decisions(response('click'),response('ask'))
        result=self.run_loop(model,compare=lambda *a:False)
        self.assertEqual(result['status'],'needs_input')
        self.assertEqual(self.windows.inputs,[])
        self.assertEqual([item['id'] for item in model.calls],['frame-1','frame-2'])
        self.assertTrue(any(event[0]=='action_rejected' for event in self.events))
    def test_non_focus_window_error_preserves_its_code_and_diagnostics(self):
        failure=WindowError('原生输入未通过校验。','pointer_displaced')
        failure.details={'expected':[50,100],'actual':[1,2]}
        def fail(*_args,**_kwargs):raise failure
        self.windows.focus_error=None;self.windows.controlled_click=fail
        result=self.run_loop(Decisions(response('click')))
        self.assertEqual(result['error_code'],'pointer_displaced')
        self.assertEqual(result['diagnostics'],failure.details)
        self.assertEqual(self.events[-1][2]['diagnostics'],failure.details)
        self.assertEqual(self.windows.inputs,[])

    def test_click_loses_foreground_before_down_waits_then_redecides_on_new_frame(self):
        self.windows.focus_error=None
        attempts=[];original=self.windows.controlled_click
        def click(x,y,observation_id,cancel_event=None):
            attempts.append((x,y,observation_id))
            if len(attempts)==1:
                self.windows.bound_value.foreground=False
                raise WindowError('输入前失焦','not_foreground')
            return original(x,y,observation_id,cancel_event)
        self.windows.controlled_click=click
        self.clock.on_wait=lambda _:setattr(self.windows.bound_value,'foreground',True)
        model=Decisions(response('click',x=.1,y=.2),response('click',x=.8,y=.7),
                        response('finish',verification_of_previous='confirmed'))
        result=self.run_loop(model)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(attempts,[(10,40,'frame-2'),(80,140,'frame-4')])
        self.assertEqual(self.windows.inputs,[('click',80,140,'frame-4')])
        self.assertEqual([item['id'] for item in model.calls],['frame-1','frame-3','frame-5'])
        self.assertIsNone(model.calls[1]['execution_context']['pending_action'])
        self.assertEqual(model.calls[1]['execution_context']['last_proposal']['status'],'rejected')
        self.assertFalse(model.calls[1]['execution_context']['last_proposal']['executed_game_input'])
        self.assertEqual(self.windows.focus_count,2,'One activation per newly selected action, none in recovery polling')
        self.assertEqual(len(result['actions']),1)
        self.assertTrue(result['foreground_waits'][0]['resumed'])

    def test_preinput_focus_race_timeout_and_cancel_are_bounded_without_replaying(self):
        for cancelled in (False,True):
            with self.subTest(cancelled=cancelled):
                self.setUp();self.windows.focus_error=None
                def rejected(*args,**kwargs):raise WindowError('输入前失焦','not_foreground')
                self.windows.controlled_click=rejected
                if cancelled:self.clock.on_wait=lambda _:self.cancel.set()
                model=Decisions(response('click'))
                result=self.run_loop(model)
                self.assertEqual(result['status'],'cancelled' if cancelled else 'needs_input')
                self.assertEqual(len(model.calls),1);self.assertEqual(self.windows.inputs,[])
                self.assertEqual(self.windows.focus_count,1)
                self.assertLessEqual(self.clock.now,20.)

    def test_unknown_or_partial_drag_focus_loss_is_never_automatically_replayed(self):
        for detail in ({},{'button_pressed':True},{'partial_input':True}):
            with self.subTest(detail=detail):
                self.setUp();self.windows.focus_error=None
                def failed(*args,**kwargs):
                    error=WindowError('拖动期间失焦','not_foreground');error.details=detail;raise error
                self.windows.controlled_drag=failed
                model=Decisions(response('drag',to_x=.8,to_y=.7,duration_seconds=.3))
                result=self.run_loop(model)
                self.assertEqual(result['status'],'needs_input')
                self.assertEqual(len(model.calls),1);self.assertEqual(self.windows.inputs,[])
                self.assertNotIn('foreground_waits',result)

    def test_explicitly_unpressed_drag_can_wait_but_still_requires_a_new_decision(self):
        self.windows.focus_error=None
        def rejected(*args,**kwargs):
            error=WindowError('拖动前失焦','not_foreground');error.details={'button_pressed':False};raise error
        self.windows.controlled_drag=rejected
        self.clock.on_wait=lambda _:setattr(self.windows.bound_value,'foreground',True)
        model=Decisions(response('drag',to_x=.8,to_y=.7,duration_seconds=.3),response('ask',summary='新画面需重新规划'))
        result=self.run_loop(model)
        self.assertEqual([item['id'] for item in model.calls],['frame-1','frame-3'])
        self.assertEqual(self.windows.inputs,[]);self.assertEqual(len(result['foreground_waits']),1)
        self.assertIsNone(model.calls[1]['execution_context']['pending_action'])


class KnowledgeLookupTests(unittest.TestCase):
    def setUp(self):
        self.windows=FakeWindows();self.cancel=threading.Event();self.events=[];self.clock=SimulatedTime()
    def run_loop(self,model,lookup=None,**kwargs):
        return GameExecutor(self.windows,model,lambda:None,lookup_knowledge=lookup,
            emit_event=lambda *event:self.events.append(event),compare_patch=lambda *a:True,
            clock=self.clock,waiter=self.clock.wait).run('进入基建',self.cancel,**kwargs)
    def test_lookup_keeps_same_frame_and_sources_without_game_input_or_extra_capture(self):
        called=[]
        def lookup(query,*,cancel_event):
            called.append((query,cancel_event))
            return {'ok':True,'snippets':[{'text':'基建入口规则参考','source':'https://example.test/wiki','title':'基建'}]}
        model=Decisions(response('lookup',query='  基建入口  '),response('ask'))
        result=self.run_loop(model,lookup)
        self.assertEqual(len(self.windows.captures),1);self.assertEqual(self.windows.inputs,[])
        self.assertEqual([o['id'] for o in model.calls],['frame-1','frame-1'])
        self.assertEqual(called,[('基建入口',self.cancel)])
        context=model.calls[1]['execution_context']
        self.assertEqual(context['knowledge_context'][0]['source'],'https://example.test/wiki')
        self.assertNotIn('knowledge_context',context['model_state'])
        event=next(e[2] for e in self.events if e[0]=='knowledge_lookup')
        self.assertTrue(event['ok']);self.assertFalse(event['executed_game_input'])
        self.assertFalse(event['semantic_success']);self.assertFalse(result['model_goal_verified'])
    def test_actual_pending_unknown_can_lookup_but_still_cannot_click_again(self):
        model=Decisions(response('click'),response('lookup',query='返回规则',verification_of_previous='unknown'),
                        response('click',verification_of_previous='unknown'))
        result=self.run_loop(model,lambda *a,**k:{'text':'参考','source':'https://example.test'})
        self.assertEqual(result['status'],'needs_input');self.assertEqual(len(self.windows.inputs),1)
        self.assertIsNotNone(model.calls[2]['execution_context']['pending_action'])
        self.assertEqual(model.calls[1]['id'],model.calls[2]['id'])
    def test_missing_callback_reports_failure_without_learning_or_input(self):
        result=self.run_loop(Decisions(response('lookup',query='游戏规则')))
        self.assertEqual(result['status'],'needs_input');self.assertIn('尚未连接',result['summary'])
        self.assertEqual(self.windows.inputs,[])
        self.assertTrue(any(e[0]=='knowledge_lookup' and not e[2]['ok'] for e in self.events))
    def test_only_three_queries_per_segment_and_empty_results_are_not_facts(self):
        calls=[]
        def lookup(query,**kwargs):calls.append(query);return {'ok':False,'error':'暂不可用'}
        model=Decisions(*(response('lookup',query='查询'+str(i)) for i in range(4)))
        result=self.run_loop(model,lookup)
        self.assertEqual(len(calls),3);self.assertEqual(result['metrics']['knowledge_lookups'],3)
        self.assertEqual(result['status'],'needs_input');self.assertIn('三次',result['summary'])
        self.assertEqual(result['game_context']['model_state']['knowledge_context'],[])
        self.assertEqual(len(self.windows.captures),1);self.assertEqual(self.windows.inputs,[])
    def test_cancelled_lookup_discards_late_reference_without_more_decisions(self):
        def lookup(*args,**kwargs):
            self.cancel.set();return {'text':'晚到资料','source':'https://example.test'}
        model=Decisions(response('lookup',query='规则'))
        result=self.run_loop(model,lookup)
        self.assertEqual(result['status'],'cancelled');self.assertEqual(len(model.calls),1)
        self.assertNotIn('knowledge_context',result['game_context']['model_state'])
        self.assertEqual(self.windows.inputs,[])
    def test_unknown_lookup_exception_never_exposes_raw_network_or_secret_text(self):
        def lookup(*args,**kwargs):raise RuntimeError('PRIVATE_API_KEY_VALUE')
        model=Decisions(response('lookup',query='规则'),response('ask'))
        result=self.run_loop(model,lookup)
        self.assertNotIn('PRIVATE_API_KEY_VALUE',json.dumps(result)+json.dumps(self.events))
        self.assertFalse(model.calls[1]['execution_context']['model_state']['knowledge_lookup_status']['ok'])
    def test_knowledge_is_bounded_with_sources_and_survives_same_goal_checkpoint(self):
        def lookup(*args,**kwargs):
            return {'results':[{'content':'规则'*3000,'url':'https://example.test/'+str(i),'title':'规则'*200}
                               for i in range(10)]}
        first=self.run_loop(Decisions(response('lookup',query='规则')),lookup,max_steps=1)
        self.assertTrue(first['continuable'])
        knowledge=first['game_context']['model_state']['knowledge_context']
        self.assertEqual(len(knowledge),3);self.assertTrue(all(len(k['text'])<=1200 for k in knowledge))
        self.assertTrue(all(len(k['title'])<=160 for k in knowledge))
        model=Decisions(response('ask'))
        self.run_loop(model,lookup,resume_context=first['game_context'])
        self.assertEqual(model.calls[0]['execution_context']['knowledge_context'],knowledge)
        self.assertEqual(self.windows.inputs,[])


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.windows=FakeWindows();self.cancel=threading.Event();self.clock=SimulatedTime();self.events=[]
    def item(self,identifier,status='pending',evidence=''):
        return {'id':identifier,'title':'步骤 '+identifier,'status':status,'evidence':evidence}
    def run_loop(self,model,**kwargs):
        return GameExecutor(self.windows,model,lambda:None,clock=self.clock,waiter=self.clock.wait,
            emit_event=lambda *event:self.events.append(event),lookup_knowledge=lambda *a,**k:
                {'text':'参考规则','source':'https://example.test/rules'}).run('多阶段任务',self.cancel,**kwargs)
    def checkpoint(self):
        return self.run_loop(Decisions(response('wait',wait_seconds=.1,
            workflow=[self.item('enter'),self.item('return')])),max_steps=1)['game_context']
    def test_partial_progress_merge_survives_segments_and_preserves_other_steps(self):
        context=self.checkpoint()
        model=Decisions(response('wait',wait_seconds=.1,workflow=[self.item('enter','observed_complete','基建布局可见')]),
                        response('ask'))
        result=self.run_loop(model,resume_context=context)
        workflow=result['game_context']['model_state']['workflow']
        self.assertEqual([item['id'] for item in workflow],['enter','return'])
        self.assertEqual([item['status'] for item in workflow],['observed_complete','pending'])
        self.assertEqual(workflow[0]['source'],'model_visual_check')
        self.assertEqual(workflow[0]['observation_id'],'frame-3')
        self.assertNotIn('confirmed_battle_runs',workflow[0])
        self.assertEqual(model.calls[1]['execution_context']['model_state']['workflow'],workflow)
        self.assertEqual(result['game_context']['goal'],'多阶段任务')
    def test_finish_cannot_drop_pending_workflow_by_omission_or_empty_update(self):
        context=self.checkpoint()
        finish=response('finish',goal_already_satisfied=True,workflow=[])
        result=self.run_loop(Decisions(finish,finish,finish),resume_context=context)
        self.assertEqual(result['status'],'needs_input');self.assertFalse(result['model_goal_verified'])
        self.assertIn('仍有待完成',result['summary']);self.assertEqual(self.windows.inputs,[])
        self.assertTrue(any(item['status']=='pending' for item in result['game_context']['model_state']['workflow']))
    def test_current_frame_evidence_can_close_all_steps_without_extra_planner_call(self):
        context=self.checkpoint()
        model=Decisions(response('finish',goal_already_satisfied=True,
            workflow=[self.item('enter','observed_complete','当前画面显示所需步骤结果'),
                      self.item('return','observed_complete','当前主页标识可见')]))
        result=self.run_loop(model,resume_context=context)
        self.assertEqual(result['status'],'completed');self.assertEqual(len(model.calls),1)
        self.assertEqual(result['completion_evidence']['source'],'model_visual_check')
        self.assertTrue(all(item['observation_id']=='frame-3' for item in result['game_context']['model_state']['workflow']))
        self.assertEqual(self.windows.inputs,[])
    def test_knowledge_lookup_does_not_automatically_complete_workflow(self):
        first=response('lookup',query='规则',workflow=[self.item('enter')])
        finish=response('finish',goal_already_satisfied=True)
        result=self.run_loop(Decisions(first,finish,finish,finish))
        self.assertEqual(result['status'],'needs_input');self.assertFalse(result['model_goal_verified'])
        self.assertEqual(result['game_context']['model_state']['workflow'][0]['status'],'pending')
        self.assertEqual(result['metrics']['knowledge_lookups'],1)
        self.assertEqual(len(self.windows.captures),1);self.assertEqual(self.windows.inputs,[])
    def test_unchanged_completed_step_keeps_original_evidence_observation(self):
        completed=self.item('enter','observed_complete','当前画面基建可见')
        model=Decisions(response('wait',wait_seconds=.1,workflow=[completed,self.item('return')]),
            response('wait',wait_seconds=.1,workflow=[completed]),response('ask'))
        result=self.run_loop(model)
        self.assertEqual(result['game_context']['model_state']['workflow'][0]['observation_id'],'frame-1')
    def test_reopening_completed_step_needs_evidence_and_plan_total_is_bounded(self):
        completed=self.item('enter','observed_complete','当前画面基建可见')
        bad=response('wait',wait_seconds=.1,workflow=[self.item('enter','in_progress')])
        result=self.run_loop(Decisions(response('wait',wait_seconds=.1,workflow=[completed]),bad,bad,bad))
        self.assertEqual(result['status'],'needs_input')
        self.assertIn('修正已完成',result['summary'])
        first=response('wait',wait_seconds=.1,workflow=[self.item(str(i)) for i in range(12)])
        extra=response('wait',wait_seconds=.1,workflow=[self.item('extra')])
        result=self.run_loop(Decisions(first,extra,extra,extra))
        self.assertEqual(result['status'],'needs_input');self.assertIn('合并后的workflow',result['summary'])


class ActionValidationTests(unittest.TestCase):
    def test_malformed_tool_envelopes_raise_safe_validation_error(self):
        for item in [None,{"tool_calls":{"0":{}}},{"tool_calls":"x"},
                     {"tool_calls":[{"function":None}]},{"tool_calls":[{"function":[]}]},
                     response(["click"]),response("click",verification_of_previous=[]),
                     response("click",summary={}),response("click",goal_already_satisfied="true"),
                     response("click",resource_sensitive="false"),response("click",shell="bad")]:
            with self.subTest(item=item):
                with self.assertRaises(GameExecutionError):parse_action(item)
    def test_nan_infinity_and_boolean_coordinates_are_never_valid(self):
        for number in [float("nan"),float("inf"),-float("inf"),True]:
            with self.subTest(number=number):
                with self.assertRaises(GameExecutionError):parse_action(response("click",x=number))
    def test_lookup_requires_bounded_nonempty_text_query(self):
        for query in [None,'',123,'a'*501]:
            with self.subTest(query=query):
                with self.assertRaises(GameExecutionError):parse_action(response('lookup',query=query))
    def test_workflow_rejects_invalid_status_duplicates_missing_evidence_and_large_plan(self):
        valid={'id':'base','title':'进入基建','status':'pending','evidence':''}
        for workflow in ['bad',[dict(valid,status='complete')],[dict(valid,status='observed_complete')],
                         [valid,valid],[dict(valid,title='x'*161)],[dict(valid,goal='改写目标')],
                         [dict(valid,id=str(i)) for i in range(13)],
                         [dict(valid,id=str(i),evidence='x'*650) for i in range(7)]]:
            with self.subTest(workflow=workflow):
                with self.assertRaises(GameExecutionError):parse_action(response('wait',wait_seconds=.1,workflow=workflow))


class TargetPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
    def save(self,name,color):
        image=QImage(100,100,QImage.Format.Format_RGB32);image.fill(QColor(color))
        path=self.root/name;self.assertTrue(image.save(str(path)))
        return {"id":name,"window_id":1,"window_pid":2,"width":100,"height":100,"path":str(path)}
    def test_same_target_patch_is_allowed_but_changed_target_is_rejected(self):
        a=self.save("a.png","white");b=self.save("b.png","white");c=self.save("c.png","black")
        action={"kind":"click","x":.5,"y":.5}
        self.assertTrue(compare_target_patch(a,b,action))
        self.assertFalse(compare_target_patch(a,c,action))
        b["window_pid"]=3;self.assertFalse(compare_target_patch(a,b,action))
    def test_unrelated_animated_background_does_not_invalidate_static_button(self):
        a=self.save("a.png","white");b=self.save("b.png","white")
        image=QImage(b["path"])
        for y in range(20):
            for x in range(100):image.setPixelColor(x,y,QColor("black"))
        self.assertTrue(image.save(b["path"]))
        self.assertTrue(compare_target_patch(a,b,{"kind":"click","x":.5,"y":.5}))
    def test_drag_requires_both_origin_and_destination_to_remain_stable(self):
        a=self.save("a.png","white");b=self.save("b.png","white")
        image=QImage(b["path"])
        for y in range(69,92):
            for x in range(69,92):image.setPixelColor(x,y,QColor("black"))
        self.assertTrue(image.save(b["path"]))
        action={"kind":"drag","x":.2,"y":.2,"to_x":.8,"to_y":.8}
        self.assertFalse(compare_target_patch(a,b,action))


if __name__=="__main__":unittest.main()

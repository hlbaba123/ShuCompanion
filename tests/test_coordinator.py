"""Real task/checkpoint storage with bounded fake execution workers only."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication
from shu.coordinator import ExecutionCoordinator
from shu.core import TaskEngine
from shu.providers import ProviderCancelled,ProviderConfig,ProviderError
from shu.runtime import RuntimeStore


class FakeWindows:
    def __init__(self):
        self.input_enabled=False;self.stopped=True;self.events=[]
        self.window=SimpleNamespace(hwnd=101,pid=202,title='明日方舟',is_game_client=True)
    def set_stop(self):self.stopped=True;self.input_enabled=False;self.events.append('stop')
    def reset_stop(self):self.stopped=False;self.events.append('reset')
    def set_input_enabled(self,enabled):self.input_enabled=enabled;self.events.append(('input',enabled))
    def bound_window(self):return self.window
    def list_windows(self,game_only=True):return [self.window]
    def bind(self,hwnd):self.events.append(('bind',hwnd))
    def require_input_permission(self):pass


class FakeClient:
    def __init__(self):self.config=ProviderConfig();self.calls=[];self.last_metrics={}
    def complete(self,messages,tools=None,cancel_event=None):
        self.calls.append(copy.deepcopy(messages))
        return {'role':'assistant','content':'fixture decision'}


class FakeExecutors:
    def __init__(self,*scripts):self.scripts=scripts;self.instances=[];self.runs=[]
    def __call__(self,windows,decide,guard,emit_event=None,**_kwargs):
        index=len(self.instances)
        if index>=len(self.scripts):raise AssertionError('Unexpected extra execution segment')
        runner=SimpleNamespace(windows=windows,decide=decide,guard=guard,emit=emit_event)
        def run(goal,cancel_event=None,**kwargs):
            self.runs.append({'goal':goal,**copy.deepcopy(kwargs)})
            return self.scripts[index](runner,goal,cancel_event,kwargs.get('resume_context'))
        runner.run=run;self.instances.append(runner);return runner


def checkpoint(goal,marker='preserved-plan'):
    return {'schema_version':1,'goal':goal,'executed_action_count':1,
            'window_identity':{'window_id':101,'window_pid':202,'width':640,'height':360},
            'pending_action':{'kind':'click','target_label':'上一动作','expected_result':'进入基建'},
            'model_state':{'marker':marker,'plan':['进入基建','收取产物']},'recent_steps':[]}


def completed(runner,goal,_cancel,_context):
    runner.guard();identifier=uuid.uuid4().hex
    runner.emit('observation','fixture frame',{'id':identifier,'captured_at':time.time(),
                                             'window_id':101,'width':640,'height':360})
    return {'status':'completed','summary':'fixture goal checked','steps':1,'model_goal_verified':True,
            'completion_evidence':{'source':'model_visual_check','observation_id':identifier,'text':'新画面确认目标已到达'},
            'game_context':checkpoint(goal)}


class CoordinatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name)
        self.engine=TaskEngine(self.directory);self.runtime=RuntimeStore(self.directory)
        self.windows=FakeWindows();self.permissions=[True,True];self.coordinator=None
        self.releases=[];self.clients=[];self.thread_errors=[];self.stores_closed=False
        self.extra_stores=[]
        self.hook=patch('threading.excepthook',side_effect=lambda args:self.thread_errors.append(args.exc_value));self.hook.start()

    def tearDown(self):
        for release in self.releases:release.set()
        if self.coordinator:
            self.coordinator.close();self.wait_for(lambda:not self.coordinator.active,timeout=3,required=False)
        if not self.stores_closed:self.runtime.close();self.engine.close()
        for store in self.extra_stores:store.close()
        self.app.processEvents();self.hook.stop();self.temp.cleanup()
        self.assertEqual(self.thread_errors,[],'Execution worker raised outside its result protocol')

    def gate(self):
        event=threading.Event();self.releases.append(event);return event

    def wait_for(self,predicate,timeout=2,required=True):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.app.processEvents()
            if predicate():return True
            time.sleep(.005)
        if required:self.fail('Timed out waiting for coordinator state')
        return False

    def create(self,*scripts):
        self.executors=FakeExecutors(*scripts)
        def make_client():
            client=FakeClient();self.clients.append(client);return client
        self.coordinator=ExecutionCoordinator(self.engine,self.windows,self.runtime,make_client,
            lambda:tuple(self.permissions),executor_factory=self.executors)
        self.coordinator.max_decisions=4;self.coordinator.max_model_calls=3;self.coordinator.max_seconds=5
        return self.coordinator

    def test_submit_returns_accepted_while_game_worker_is_still_running(self):
        entered=self.gate();release=self.gate();returned=self.gate();results=[]
        def blocked(runner,goal,cancel,context):
            entered.set();release.wait(3);return completed(runner,goal,cancel,context)
        coordinator=self.create(blocked)
        def submit():
            results.append(coordinator.submit('进入基建并收取产物',{'goal':'完整目标','constraints':['不换班']}));returned.set()
        threading.Thread(target=submit,daemon=True).start()
        self.wait_for(entered.is_set);self.wait_for(returned.is_set)
        self.assertFalse(release.is_set());self.assertTrue(coordinator.active)
        self.assertTrue(results[0]['accepted']);self.assertFalse(results[0]['game_input_sent'])
        release.set();self.wait_for(lambda:not coordinator.active)
        task=self.engine.get_task(results[0]['task_id'])
        self.assertEqual(task['status'],'completed');self.assertEqual(task['completed_runs'],0)

    def test_segment_budget_continues_with_same_goal_and_checkpoint(self):
        context=checkpoint('完整多步骤目标')
        def first(runner,goal,cancel,prior):
            self.assertIsNone(prior)
            return {'status':'needs_input','steps':2,'continuable':True,'stop_reason':'step_budget','game_context':context}
        def second(runner,goal,cancel,prior):
            self.assertEqual(prior,context);return completed(runner,goal,cancel,prior)
        coordinator=self.create(first,second);coordinator.segment_steps=2
        accepted=coordinator.submit('完整多步骤目标')
        self.wait_for(lambda:not coordinator.active)
        self.assertEqual(len(self.executors.runs),2);self.assertEqual(len(self.clients),1)
        self.assertEqual(self.engine.get_task(accepted['task_id'])['status'],'completed')
        self.assertIn('execution_checkpoint',[event['type'] for event in self.engine.events(accepted['task_id'])])

    def test_total_decision_budget_stops_automatic_continuation(self):
        def segment(_runner,goal,_cancel,_prior):
            return {'status':'needs_input','steps':1,'continuable':True,'game_context':checkpoint(goal)}
        coordinator=self.create(segment,segment,segment);coordinator.max_decisions=2
        accepted=coordinator.submit('预算内持续执行')
        self.wait_for(lambda:not coordinator.active)
        self.assertEqual(len(self.executors.runs),2)
        self.assertEqual(self.engine.get_task(accepted['task_id'])['status'],'needs_input')
        self.assertFalse(self.windows.input_enabled)

    def test_total_model_budget_is_enforced_before_an_additional_api_call(self):
        def calls(runner,goal,cancel,_prior):
            runner.decide(goal,{'state':'unknown'},[],cancel_event=cancel)
            runner.decide(goal,{'state':'unknown'},[],cancel_event=cancel)
            raise AssertionError('Budget should have stopped the second call')
        coordinator=self.create(calls);coordinator.max_model_calls=1
        accepted=coordinator.submit('模型调用有界')
        self.wait_for(lambda:not coordinator.active)
        self.assertEqual(len(self.clients[0].calls),1)
        task=self.engine.get_task(accepted['task_id'])
        self.assertEqual(task['status'],'needs_input')
        saved=self.runtime.load(accepted['task_id'])
        self.assertEqual(saved['phase'],'needs_input')
        self.assertEqual(saved['version'],task['version'])
        self.assertEqual(saved['metrics']['model_calls'],1)

    def test_game_receives_bounded_explicit_preferences_without_matching_goal_keywords(self):
        from shu.knowledge import KnowledgeStore
        store=KnowledgeStore(self.directory);self.extra_stores.append(store)
        for number in range(9):store.add_preference('长期偏好 '+str(number),'偏好：每天晚上先休息。'+('补充说明。'*100))
        self.assertEqual(store.retrieve('导航去基建'),[],'Fixture preferences must not rely on relevance retrieval')
        def decide(runner,goal,cancel,prior):
            runner.decide(goal,{'state':'unknown'},[],history=[],cancel_event=cancel)
            return completed(runner,goal,cancel,prior)
        coordinator=self.create(decide);coordinator.knowledge=store
        accepted=coordinator.submit('导航去基建',{'goal':'导航去基建','constraints':['本次不要采纳旧习惯覆盖目标']})
        self.wait_for(lambda:not coordinator.active)
        self.assertEqual(self.engine.get_task(accepted['task_id'])['status'],'completed')
        sent=self.clients[0].calls[0]
        message=next(item['content'] for item in sent if isinstance(item.get('content'),str)
                     and '以下是用户明确保存的长期偏好数据' in item['content'])
        self.assertIn('当前用户任务契约和明确约束优先',message)
        self.assertIn('偏好不能授予',message)
        entries=json.loads(message.split('\n',1)[1])
        self.assertGreater(len(entries),0);self.assertLessEqual(len(entries),6)
        self.assertLessEqual(sum(len(item['text']) for item in entries),1200)
        self.assertTrue(all(item['source_kind']=='user_preference' for item in entries))
        self.assertEqual(len(self.clients[0].calls),1,'Preferences must not create another planner/model request')

    def test_pause_and_immediate_resume_keep_same_task_and_semantic_checkpoint(self):
        entered=self.gate();release=self.gate();second_entered=self.gate()
        def first(runner,goal,cancel,prior):
            runner.emit('checkpoint','',{'game_context':checkpoint(goal)})
            entered.set();release.wait(3)
            return {'status':'cancelled','steps':1,'game_context':checkpoint(goal)}
        def second(runner,goal,cancel,prior):
            second_entered.set();self.assertEqual(prior['model_state']['marker'],'preserved-plan')
            self.assertTrue(prior['window_identity'],'Stored identity is needed to reject unsafe coordinate reuse')
            return completed(runner,goal,cancel,prior)
        coordinator=self.create(first,second);accepted=coordinator.submit('进入基建并收产物')
        self.wait_for(entered.is_set);paused=coordinator.pause();self.assertFalse(self.windows.input_enabled)
        coordinator.resume();release.set();self.wait_for(second_entered.is_set);self.wait_for(lambda:not coordinator.active)
        task=self.engine.get_task(accepted['task_id'])
        self.assertEqual(task['status'],'completed');self.assertGreater(task['version'],paused['version'])
        self.assertEqual(len(self.engine.list_tasks()),1);self.assertEqual(task['completed_runs'],0)

    def test_stop_closes_input_before_a_slow_worker_releases(self):
        entered=self.gate();release=self.gate()
        def blocked(runner,goal,cancel,prior):
            entered.set();release.wait(3);runner.guard()
            raise AssertionError('Stopped execution must fail its guard')
        coordinator=self.create(blocked);accepted=coordinator.submit('进入基建')
        self.wait_for(entered.is_set);coordinator.stop()
        self.assertFalse(self.windows.input_enabled);self.assertFalse(release.is_set())
        self.assertEqual(self.engine.get_task(accepted['task_id'])['status'],'cancelled')
        release.set();self.wait_for(lambda:not coordinator.active)
        self.assertEqual(self.engine.get_task(accepted['task_id'])['status'],'cancelled')

    def test_replacement_waits_for_old_release_and_rejects_stale_callbacks(self):
        old_entered=self.gate();old_release=self.gate();new_entered=self.gate();new_release=self.gate()
        def old(runner,goal,cancel,prior):
            old_entered.set();old_release.wait(3);runner.guard()
        def new(runner,goal,cancel,prior):
            new_entered.set();new_release.wait(3);return completed(runner,goal,cancel,prior)
        coordinator=self.create(old,new);first=coordinator.submit('旧目标');self.wait_for(old_entered.is_set)
        second=coordinator.submit('新目标');self.assertEqual(len(self.executors.instances),1)
        old_release.set();self.wait_for(new_entered.is_set)
        stop_count=self.windows.events.count('stop')
        self.executors.instances[0].emit('action_executed','过时事件',{'stale':True})
        self.assertTrue(self.windows.input_enabled);self.assertEqual(self.windows.events.count('stop'),stop_count)
        self.assertNotIn('过时事件',[event['message'] for event in self.engine.events(second['task_id'])])
        new_release.set();self.wait_for(lambda:not coordinator.active)
        self.assertEqual(self.engine.get_task(first['task_id'])['status'],'cancelled')
        self.assertEqual(self.engine.get_task(second['task_id'])['status'],'completed')

    def test_same_goal_modification_cannot_be_overwritten_by_old_final_checkpoint(self):
        entered=self.gate();release=self.gate();new_entered=self.gate();new_release=self.gate()
        def old(runner,goal,cancel,prior):
            entered.set();release.wait(3)
            return {'status':'cancelled','steps':1,'game_context':checkpoint(goal,'old')}
        def new(runner,goal,cancel,prior):
            new_entered.set();new_release.wait(3);return completed(runner,goal,cancel,prior)
        coordinator=self.create(old,new);first=coordinator.submit('相同目标',{'marker':'old'})
        self.wait_for(entered.is_set)
        second=coordinator.submit('相同目标',{'marker':'new','constraints':['新增约束']},modify=True)
        self.assertEqual(first['task_id'],second['task_id'])
        release.set();self.wait_for(new_entered.is_set)
        self.assertEqual(self.runtime.load(second['task_id'])['contract']['marker'],'new')
        new_release.set();self.wait_for(lambda:not coordinator.active)

    def test_resume_does_not_discard_a_new_goal_already_waiting_in_queue(self):
        entered=self.gate();release=self.gate();new_entered=self.gate()
        def old(runner,goal,cancel,prior):entered.set();release.wait(3);runner.guard()
        def new(runner,goal,cancel,prior):new_entered.set();return completed(runner,goal,cancel,prior)
        coordinator=self.create(old,new);coordinator.submit('旧目标');self.wait_for(entered.is_set)
        accepted=coordinator.submit('新目标');coordinator.resume();release.set()
        self.wait_for(new_entered.is_set);self.wait_for(lambda:not coordinator.active)
        self.assertEqual(self.engine.get_task(accepted['task_id'])['status'],'completed')
        self.assertEqual(self.thread_errors,[])

    def test_revoked_permission_and_closed_coordinator_cannot_start_new_input(self):
        coordinator=self.create(completed);self.permissions[1]=False
        with self.assertRaises(ProviderError):coordinator.submit('不得启动')
        self.assertEqual(self.executors.instances,[]);self.assertFalse(self.windows.input_enabled)
        self.permissions[1]=True
        task=self.engine.submit_goal('暂停任务');self.engine.pause_task(task['id'])
        coordinator.close()
        with self.assertRaises(ProviderError):coordinator.resume(task['id'])
        self.assertFalse(self.windows.input_enabled)

    def test_late_worker_after_close_never_touches_already_closed_databases(self):
        entered=self.gate();release=self.gate();worker_returned=self.gate()
        def slow(runner,goal,cancel,prior):
            entered.set();release.wait(4);worker_returned.set()
            return {'status':'cancelled','steps':1,'game_context':checkpoint(goal)}
        coordinator=self.create(slow);coordinator.submit('关闭时保留进度');self.wait_for(entered.is_set)
        coordinator.close();self.assertFalse(self.windows.input_enabled)
        self.runtime.close();self.engine.close();self.stores_closed=True
        release.set();self.wait_for(worker_returned.is_set)
        self.wait_for(lambda:not coordinator.active,timeout=2)
        self.assertEqual(self.thread_errors,[])

    def test_done_is_signalled_only_after_final_checkpoint_is_persisted(self):
        entered=self.gate();release=self.gate();saving=self.gate();allow_save=self.gate()
        def blocked(runner,goal,cancel,prior):
            entered.set();release.wait(3);return completed(runner,goal,cancel,prior)
        coordinator=self.create(blocked);accepted=coordinator.submit('等待最终进度落盘')
        self.wait_for(entered.is_set);job=coordinator._job
        original=self.runtime.save
        def slow_final_save(*args,**kwargs):
            if kwargs.get('phase')=='completed':
                saving.set();allow_save.wait(3)
            return original(*args,**kwargs)
        with patch.object(self.runtime,'save',side_effect=slow_final_save):
            release.set();self.wait_for(saving.is_set)
            try:self.assertFalse(job.done.is_set(),'Done must not race database closure')
            finally:allow_save.set()
            self.wait_for(job.done.is_set)
        self.assertEqual(self.runtime.load(accepted['task_id'])['phase'],'completed')

    def test_close_saves_active_context_before_closed_flag_and_keeps_same_task_resumable(self):
        entered=self.gate();release=self.gate()
        context=checkpoint('退出保留目标')
        def blocked(runner,goal,cancel,prior):
            runner.emit('checkpoint','',{'game_context':context})
            entered.set();release.wait(3);runner.guard()
        coordinator=self.create(blocked)
        accepted=coordinator.submit('退出保留目标',{'goal':'退出保留目标','constraints':['不花源石']})
        self.wait_for(entered.is_set)
        job=coordinator._job
        saves=[];original=self.runtime.save
        def save(*args,**kwargs):
            saves.append(coordinator._closed)
            return original(*args,**kwargs)
        with patch.object(self.runtime,'save',side_effect=save):
            coordinator.close()
        task=self.engine.get_task(accepted['task_id']);saved=self.runtime.load(task['id'])
        self.assertEqual(task['status'],'paused');self.assertEqual(saved['phase'],'paused')
        self.assertEqual(saved['version'],task['version']);self.assertEqual(saved['context'],context)
        self.assertEqual(saved['contract']['constraints'],['不花源石'])
        self.assertTrue(saves);self.assertFalse(any(saves),'No checkpoint write is allowed after closed is set')
        self.assertFalse(self.windows.input_enabled)
        coordinator.close();self.assertEqual(self.engine.get_task(task['id'])['version'],task['version'])
        release.set();self.wait_for(job.done.is_set)

    def test_close_with_pending_replacement_preserves_new_queued_goal_and_never_launches_it(self):
        entered=self.gate();release=self.gate()
        def old(runner,goal,cancel,prior):entered.set();release.wait(3);runner.guard()
        coordinator=self.create(old,completed)
        previous=coordinator.submit('旧目标');self.wait_for(entered.is_set)
        latest=coordinator.submit('新排队目标',{'goal':'新排队目标','constraints':['保留条件']})
        coordinator.close();release.set();self.wait_for(lambda:not coordinator.active)
        self.assertEqual(len(self.executors.instances),1)
        self.assertEqual(self.engine.get_task(previous['task_id'])['status'],'cancelled')
        task=self.engine.get_task(latest['task_id'])
        self.assertEqual(task['status'],'paused')
        saved=self.runtime.load(task['id'])
        self.assertEqual(saved['version'],task['version']);self.assertEqual(saved['phase'],'paused')
        self.assertEqual(saved['contract']['constraints'],['保留条件'])
        self.assertFalse(self.windows.input_enabled)

    def test_close_preserves_terminal_completed_and_explicitly_stopped_tasks(self):
        coordinator=self.create(completed);accepted=coordinator.submit('已完成目标')
        self.wait_for(lambda:not coordinator.active)
        before=self.engine.get_task(accepted['task_id']);coordinator.close()
        self.assertEqual(self.engine.get_task(before['id']),before)
        coordinator=self.create(completed)
        stopped=self.engine.submit_goal('显式停止目标');self.engine.stop_task(stopped['id'])
        coordinator.current_task_id=stopped['id'];coordinator.close()
        self.assertEqual(self.engine.get_task(stopped['id'])['status'],'cancelled')

    def test_shutdown_and_rebuilt_coordinator_resume_same_task_with_new_frame_without_replaying_input(self):
        from shu.game_executor import GameExecutor
        from PySide6.QtGui import QImage,QColor
        from types import SimpleNamespace
        context=checkpoint('恢复同一目标')
        context['pending_action']['observation_id']='before-restart-frame'
        context['window_identity'].update(width=640,height=360,
            client_rect={'left':0,'top':0,'width':640,'height':360})
        context['last_executed_action']={'kind':'click','target_label':'上一动作','verified':False}
        entered=self.gate();release=self.gate()
        def old(runner,goal,cancel,prior):
            runner.emit('checkpoint','',{'game_context':context});entered.set();release.wait(3);runner.guard()
        coordinator=self.create(old);accepted=coordinator.submit('恢复同一目标')
        self.wait_for(entered.is_set);job=coordinator._job
        coordinator.close();release.set();self.wait_for(job.done.is_set)
        self.runtime.close();self.engine.close()
        self.engine=TaskEngine(self.directory);self.engine.recover_interrupted_tasks();self.runtime=RuntimeStore(self.directory)
        window=SimpleNamespace(hwnd=101,pid=202,title='明日方舟',foreground=True,minimized=False,
            left=0,top=0,width=640,height=360)
        self.windows.window=window;captures=[];inputs=[];model_frames=[]
        frame_path=self.directory/'fresh-fixture.png'
        image=QImage(640,360,QImage.Format.Format_RGB32);image.fill(QColor('#35624d'));self.assertTrue(image.save(str(frame_path)))
        def capture(**kwargs):
            self.assertTrue(kwargs['background_only']);captures.append('fresh-after-restart')
            return {'id':'fresh-after-restart','captured_at':time.time(),'window_id':101,'window_pid':202,
                'width':640,'height':360,'path':str(frame_path),
                'client_rect':{'left':0,'top':0,'width':640,'height':360}}
        self.windows.capture=capture
        self.windows.controlled_click=lambda *a,**k:inputs.append(a)
        class FinishClient(FakeClient):
            def complete(client,messages,tools=None,cancel_event=None):
                client.calls.append(messages)
                return {'role':'assistant','tool_calls':[{'id':'finish','type':'function','function':{
                    'name':'choose_game_action','arguments':json.dumps({'kind':'finish','reason':'当前新画面确认',
                    'expected_result':'目标页面','verification_of_previous':'confirmed','evidence':'新画面显示基建设施'})}}]}
        def executor(windows,decide,guard,**kwargs):
            def observe(goal,observation,tools,history=None,cancel_event=None):
                model_frames.append(observation['id'])
                return decide(goal,observation,tools,history=history,cancel_event=cancel_event)
            return GameExecutor(windows,observe,guard,**kwargs)
        self.coordinator=ExecutionCoordinator(self.engine,self.windows,self.runtime,FinishClient,
            lambda:(True,True),executor_factory=executor)
        self.coordinator.current_task_id=accepted['task_id']
        resumed=self.coordinator.resume();self.wait_for(lambda:not self.coordinator.active)
        self.assertTrue(resumed['accepted']);self.assertEqual(len(self.engine.list_tasks()),1)
        self.assertEqual(self.engine.get_task(accepted['task_id'])['status'],'completed')
        self.assertEqual(captures,['fresh-after-restart']);self.assertEqual(model_frames,['fresh-after-restart'])
        self.assertEqual(inputs,[],'The stored pending click must never be replayed')
        self.assertIsNone(self.runtime.load(accepted['task_id'])['context']['pending_action'])


if __name__=='__main__':unittest.main()

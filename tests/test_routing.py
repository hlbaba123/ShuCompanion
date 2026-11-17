"""Default controller routing + real task/checkpoint storage, fake IO only."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QImage
from shu.controller import SessionController
from shu.coordinator import ExecutionCoordinator
from shu.providers import GameSession,ProviderConfig
from shu.task_contract import ROUTE_TOOL_CHOICE,ROUTE_USER_TOOL
from shu.network_dialogue import WEB_TOOLS


def routed(text,intent='chat',goal='',*,constraints=None,completion=None,reply='我在。',**changes):
    value={'intent':intent,'user_request':text,'goal':goal,'constraints':constraints or [],
           'completion_criteria':completion or [],'removed_constraints':[],'reply':reply}
    value.update(changes)
    return {'role':'assistant','content':None,'tool_calls':[{'id':'route-'+str(time.monotonic_ns()),
        'type':'function','function':{'name':'route_user_request','arguments':json.dumps(value,ensure_ascii=False)}}]}


class Model:
    def __init__(self,*responses):
        self.responses=list(responses);self.calls=[];self.config=ProviderConfig();self.last_metrics={}
    def complete(self,messages,tools=None,cancel_event=None,tool_choice=None):
        self.calls.append({'messages':copy.deepcopy(messages),'tools':copy.deepcopy(tools),
                           'tool_choice':copy.deepcopy(tool_choice)})
        if not self.responses:raise AssertionError('Unexpected extra model request')
        return copy.deepcopy(self.responses.pop(0))


class Windows:
    def __init__(self,directory):
        self.directory=directory;self.input_enabled=False;self.stop_count=0;self.capture_count=0
        self.bound=SimpleNamespace(hwnd=101,pid=202,title='明日方舟',is_game_client=True,
            width=20,height=30,left=0,top=0,minimized=False)
    def set_stop(self):self.stop_count+=1;self.input_enabled=False
    def reset_stop(self):self.input_enabled=False
    def set_input_enabled(self,enabled):self.input_enabled=bool(enabled)
    def bound_window(self):return self.bound
    def list_windows(self,game_only=True):return [self.bound]
    def bind(self,hwnd):return self.bound
    def require_input_permission(self):pass
    def capture(self,*,background_only=False):
        if background_only is not True:raise AssertionError('Capture must remain read-only background IO')
        self.capture_count+=1;path=self.directory/f'frame-{self.capture_count}.png'
        image=QImage(20,30,QImage.Format.Format_RGB32);image.fill(0xff708890);image.save(str(path))
        return {'id':f'frame-{self.capture_count}','path':str(path),'captured_at':time.time(),
                'window_id':101,'window_pid':202,'width':20,'height':30,'state':'unknown'}


class WaitingExecutors:
    """Each segment saves a plan, then waits for real coordinator cancellation."""
    def __init__(self):self.instances=[];self.entered=[];self.contexts=[];self.thread_errors=[]
    def __call__(self,windows,decide,guard,emit_event=None,**_kwargs):
        index=len(self.instances);entered=threading.Event();self.entered.append(entered)
        runner=SimpleNamespace(windows=windows,guard=guard,emit=emit_event)
        def run(goal,cancel_event=None,**kwargs):
            prior=copy.deepcopy(kwargs.get('resume_context'));self.contexts.append(prior)
            context={'schema_version':1,'goal':goal,'executed_action_count':0,
                'window_identity':{'window_id':101,'window_pid':202,'width':20,'height':30},
                'model_state':{'plan':['进入基建','收取产物'],'marker':f'job-{index}'},'recent_steps':[]}
            guard();emit_event('checkpoint','',{'game_context':context});entered.set()
            if not cancel_event.wait(5):raise AssertionError('Test must cancel the waiting fake worker')
            return {'status':'cancelled','steps':1,'game_context':context}
        runner.run=run;self.instances.append(runner);return runner


class StructuredRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.environment=patch.dict('os.environ',{'DEEPSEEK_API_KEY':''});self.environment.start()
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.c=SessionController(self.root,self.root/'data',execution_factory=ExecutionCoordinator)
        self.assertFalse(self.c.legacy_tools)
        self.c.key_path.write_text('sk-routing-fixture-not-real',encoding='utf-8')
        self.windows=Windows(self.root);self.c.windows=self.windows;self.c.execution.windows=self.windows
        self.c.set_capture_permission(True);self.c.set_game_input_permission(True)
        self.executors=WaitingExecutors();self.c.execution.executor_factory=self.executors
        self.c.execution.wiki=None;self.c.execution.knowledge=None
        self.c.execution.max_decisions=3;self.c.execution.max_model_calls=3;self.c.execution.max_seconds=6
        self.game=Model();self.c.game_client=self.game;self.c.game=GameSession(self.game)
        self.c.execution.client_factory=lambda:self.game
        self.messages=[];self.errors=[];self.notices=[];self.thread_errors=[]
        self.c.message.connect(lambda role,text:self.messages.append((role,text)))
        self.c.error.connect(self.errors.append);self.c.notice.connect(self.notices.append)
        self.hook=patch('threading.excepthook',side_effect=lambda args:self.thread_errors.append(args.exc_value));self.hook.start()

    def tearDown(self):
        self.c.stop(silent=True);self.wait_for(lambda:not self.c.execution.active,required=False)
        self.c.close();self.app.processEvents();self.hook.stop();self.temp.cleanup();self.environment.stop()
        self.assertEqual(self.thread_errors,[])

    def install(self,*responses):
        self.role=Model(*responses);self.c.client=self.c.intent_client=self.role;self.c.persona=self.c._new_persona()
        return self.role

    def test_normal_exit_and_controller_restart_preserve_resumable_goal_without_auto_input(self):
        text='进入基建后返回主页，不花源石'
        self.install(routed(text,'execute','进入基建后返回主页',constraints=['不花源石']))
        self.c.submit(text);self.wait_for(lambda:bool(self.executors.entered) and self.executors.entered[0].is_set())
        original=self.c.current_task();identifier=original['id']
        prior=self.c.runtime.load(identifier)['context']
        self.c.close();self.c.close()
        self.assertFalse(self.windows.input_enabled)
        restored=SessionController(self.root,self.root/'data',execution_factory=ExecutionCoordinator)
        self.c=restored;self.windows=Windows(self.root);restored.windows=self.windows;restored.execution.windows=self.windows
        self.assertEqual(restored.current_task()['id'],identifier)
        self.assertEqual(restored.current_task()['status'],'paused')
        self.assertEqual(restored.runtime.load(identifier)['context'],prior)
        self.assertFalse(restored.game_input_allowed);self.assertFalse(restored.execution.active)
        self.assertEqual(self.windows.capture_count,0);self.assertFalse(self.windows.input_enabled)
        captures=[]
        def executor(windows,decide,guard,emit_event=None,**kwargs):
            def run(goal,cancel_event=None,**limits):
                guard();context=limits['resume_context'];self.assertEqual(context['model_state'],prior['model_state'])
                fresh=windows.capture(background_only=True);captures.append(fresh['id'])
                emit_event('observation','恢复后新截图',fresh)
                return {'status':'needs_input','summary':'离线恢复已验证','steps':1,'game_context':context}
            return SimpleNamespace(run=run)
        restored.execution.executor_factory=executor;restored.execution.wiki=None;restored.execution.knowledge=None
        restored.set_game_input_permission(True);restored.resume()
        self.wait_for(lambda:not restored.execution.active)
        self.assertEqual(restored.current_task()['id'],identifier)
        self.assertEqual(len(restored.engine.list_tasks()),1)
        self.assertEqual(captures,['frame-1'])
        self.assertEqual(restored.runtime.load(identifier)['context']['model_state'],prior['model_state'])

    def wait_for(self,predicate,timeout=3,required=True):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.app.processEvents()
            if predicate():return True
            time.sleep(.004)
        if required:self.fail('Default routing worker did not reach expected state')
        return False

    def send(self,text):
        self.c.submit(text);self.wait_for(lambda:not self.c.is_busy)

    def waiting_job(self,index=0):
        self.wait_for(lambda:len(self.executors.entered)>index and self.executors.entered[index].is_set())

    def test_single_role_request_submits_local_job_then_chat_does_not_cancel_it(self):
        request='进入基建收取产物，不换班'
        self.install(routed(request,'execute','进入基建并收取产物',constraints=['不换班'],
                            completion=['画面确认产物已收取']),
                     routed('你今天怎么样','chat',reply='我很好，也在陪你把事情做好。'))
        self.send(request);self.waiting_job()
        self.assertEqual(len(self.role.calls),1)
        self.assertEqual(self.role.calls[0]['tool_choice'],'required')
        self.assertEqual(self.role.calls[0]['tools'],WEB_TOOLS+[ROUTE_USER_TOOL])
        self.assertFalse(self.c.is_busy);self.assertTrue(self.c.execution.active)
        result=json.loads(self.c.persona.messages[-1]['content'])
        self.assertTrue(result['accepted']);self.assertFalse(result['game_input_sent'])
        task=self.c.current_task();job=self.c.execution._job;stops=self.windows.stop_count
        self.send('你今天怎么样')
        self.assertEqual(len(self.role.calls),2);self.assertIs(self.c.execution._job,job)
        self.assertFalse(job.cancel.is_set());self.assertEqual(self.windows.stop_count,stops)
        self.assertEqual(self.c.current_task()['id'],task['id']);self.assertEqual(self.c.current_task()['goal'],task['goal'])
        self.assertIn(('assistant','我很好，也在陪你把事情做好。'),self.messages)
        self.assertEqual(self.game.calls,[]);self.assertEqual(self.errors,[])

    def test_explicit_preference_is_persisted_and_supplied_to_next_turn(self):
        text='记住，以后不要自动使用理智药'
        self.install(routed(text,'remember'),routed('你好','chat',reply='我在。'))
        self.send(text)
        self.assertIn(text,str(self.c.knowledge.preferences()))
        self.send('你好')
        self.assertIn(text,str(self.role.calls[-1]['messages']))
        self.assertFalse(self.c.execution.active)
        self.assertEqual(self.errors,[])

    def test_backend_without_research_receipts_routes_job_followup_instead_of_inventing_outputs(self):
        request='只记下去基建收取产物'
        followup='把刚才查到的神秘代码再列给我，不要重新搜索'
        self.install(routed(request,'draft','进入基建收取产物'),
                     routed(followup,'chat',reply='当前执行后端没有已核验的作业代码。'))
        self.send(request)
        summary=json.loads(self.c._task_summary())
        self.assertNotIn('registered_job_output',summary)
        self.send(followup)
        self.assertEqual(len(self.role.calls),2)
        self.assertEqual(self.errors,[])
        self.assertIn(('assistant','当前执行后端没有已核验的作业代码。'),self.messages)
        self.assertFalse(self.c.execution.active)

    def test_explicit_plain_chat_has_no_router_tool_requirement_and_history_survives(self):
        self.install({'role':'assistant','content':'今天也在这里。'},routed('你记得吗','chat',reply='记得。'))
        self.c.submit('我们先聊天',1);self.wait_for(lambda:not self.c.is_busy)
        self.assertEqual(self.role.calls[0]['tools'], WEB_TOOLS)
        self.assertEqual(self.role.calls[0]['tool_choice'], 'auto')
        self.assertNotIn('每条真实用户消息仅调用一次',self.role.calls[0]['messages'][0]['content'])
        self.send('你记得吗')
        self.assertIn('今天也在这里。',str(self.role.calls[1]['messages']))
        self.assertIsNone(self.c.current_task())

    def test_modify_preserves_constraints_and_discards_old_execution_context(self):
        first='刷六次，不吃药也不花源石';second='改成八次'
        self.install(routed(first,'execute','完成六次当前关卡',constraints=['不吃药','不花源石'],completion=['六次结算']),
                     routed(second,'modify','完成八次当前关卡',completion=['八次结算']))
        self.send(first);self.waiting_job();before=self.c.current_task()
        self.send(second);self.waiting_job(1)
        task=self.c.current_task();saved=self.c.runtime.load(task['id'])
        self.assertEqual(task['id'],before['id']);self.assertGreater(task['version'],before['version'])
        self.assertEqual(saved['contract']['constraints'],['不吃药','不花源石'])
        self.assertEqual(saved['contract']['completion_criteria'],['八次结算'])
        self.assertIn('八次',task['goal']);self.assertIn('不吃药',task['goal'])
        self.assertIsNone(self.executors.contexts[1]);self.assertEqual(len(self.role.calls),2)
        self.assertEqual(self.errors,[])

    def test_route_pause_resume_and_stop_keep_one_task_and_enforce_local_stop(self):
        request='去基建收产物';self.install(routed(request,'execute','进入基建收取产物'),
            routed('先暂停','pause'),routed('继续','resume'),routed('停下来','stop'))
        self.send(request);self.waiting_job();identifier=self.c.current_task()['id']
        self.send('先暂停');self.wait_for(lambda:not self.c.execution.active)
        self.assertEqual(self.c.current_task()['status'],'paused');self.assertFalse(self.windows.input_enabled)
        self.send('继续');self.waiting_job(1)
        self.assertEqual(self.c.current_task()['id'],identifier);self.assertEqual(len(self.c.engine.list_tasks()),1)
        self.assertEqual(self.executors.contexts[1]['model_state']['marker'],'job-0')
        self.send('停下来');self.wait_for(lambda:not self.c.execution.active)
        self.assertEqual(self.c.current_task()['status'],'cancelled');self.assertFalse(self.windows.input_enabled)
        self.assertEqual(len(self.role.calls),4);self.assertEqual(self.errors,[])

    def test_observation_and_status_do_not_replace_or_rewrite_existing_goal(self):
        request='只记下去基建收取产物'
        self.install(routed(request,'draft','进入基建收取产物',constraints=['不换班']),
                     routed('看看画面','observe'),routed('现在进度如何','status'))
        self.game.responses=[{'role':'assistant','content':'测试画面为主页，当前没有操作。'}]
        self.send(request);before=self.c.current_task()
        self.send('看看画面');self.send('现在进度如何')
        task=self.c.current_task()
        self.assertEqual(task['id'],before['id']);self.assertEqual(task['goal'],before['goal'])
        self.assertEqual(len(self.c.engine.list_tasks()),1);self.assertEqual(len(self.role.calls),3)
        self.assertEqual(self.windows.capture_count,1);self.assertEqual(len(self.game.calls),1)
        self.assertEqual(self.executors.instances,[]);self.assertFalse(self.windows.input_enabled)
        self.assertEqual(task['completed_runs'],0);self.assertEqual(self.errors,[])

    def test_active_observation_and_status_keep_the_running_job_alive(self):
        request='去基建收产物'
        self.install(routed(request,'execute','进入基建收取产物'),routed('看一下现在界面','observe'),
                     routed('现在是什么进度','status'))
        self.game.responses=[{'role':'assistant','content':'只描述刚取得的测试画面。'}]
        self.send(request);self.waiting_job();task=self.c.current_task();job=self.c.execution._job
        self.send('看一下现在界面');self.send('现在是什么进度')
        self.assertTrue(self.c.execution.active);self.assertIs(self.c.execution._job,job)
        self.assertFalse(job.cancel.is_set());self.assertEqual(self.c.current_task()['goal'],task['goal'])
        self.assertEqual(self.c.current_task()['version'],task['version']);self.assertEqual(self.errors,[])

    def test_disabled_input_keeps_full_goal_as_draft_without_launching_executor(self):
        self.c.set_game_input_permission(False)
        request='进入基建并收产物，不更换干员'
        self.install(routed(request,'execute','进入基建并收产物',constraints=['不更换干员'],completion=['产物已收取']))
        self.send(request)
        task=self.c.current_task();saved=self.c.runtime.load(task['id'])
        self.assertEqual(task['status'],'draft');self.assertEqual(saved['contract']['user_request'],request)
        self.assertEqual(saved['contract']['constraints'],['不更换干员'])
        self.assertEqual(self.executors.instances,[]);self.assertFalse(self.windows.input_enabled)
        self.assertEqual(len(self.role.calls),1);self.assertEqual(self.errors,[])

    def test_one_bad_contract_is_repaired_once_without_partial_execution(self):
        request='先聊聊计划'
        self.install(routed(request,'execute','错误计划',constraints='not an array'),
                     routed(request,'chat',reply='我们先把计划聊清楚。'))
        self.send(request)
        self.assertEqual(len(self.role.calls),2)
        self.assertEqual([call['tool_choice'] for call in self.role.calls],['required',ROUTE_TOOL_CHOICE])
        self.assertEqual(self.role.calls[1]['tools'],[ROUTE_USER_TOOL])
        self.assertEqual(self.executors.instances,[]);self.assertEqual(self.c.engine.list_tasks(),[])
        self.assertIn(('assistant','我们先把计划聊清楚。'),self.messages)
        self.assertIn('上次合同校验未通过',json.dumps(self.role.calls[1]['messages'],ensure_ascii=False))
        self.assertEqual(self.errors,[])

    def test_three_bad_contracts_end_after_two_repairs_and_never_execute(self):
        request='去基建'
        self.install(routed(request,'execute','进入基建',constraints='bad'),
                     routed(request,'execute','进入基建',completion=['']),
                     routed(request,'execute','进入基建',constraints='still bad'))
        self.send(request)
        self.assertEqual(len(self.role.calls),3);self.assertEqual(self.executors.instances,[])
        self.assertTrue(all(x['tools']==[ROUTE_USER_TOOL] for x in self.role.calls[1:]))
        self.assertEqual(self.c.engine.list_tasks(),[]);self.assertFalse(self.windows.input_enabled)
        self.assertEqual(len(self.errors),1);self.assertIn('任务意图校验未通过',self.errors[0])
        self.assertIn('两次格式修复',self.errors[0])

    def test_stop_between_route_validation_and_submission_rejects_late_job(self):
        request='进入基建'
        self.install(routed(request,'execute','进入基建'))
        entered=threading.Event();release=threading.Event();returned=threading.Event()
        original=self.c._apply_contract
        def held_before_submission(contract,operation):
            entered.set();release.wait(3)
            try:return original(contract,operation)
            finally:returned.set()
        with patch.object(self.c,'_apply_contract',side_effect=held_before_submission):
            self.c.submit(request);self.wait_for(entered.is_set)
            self.c.stop(silent=True)
            release.set();self.wait_for(returned.is_set);self.app.processEvents()
        self.assertEqual(self.executors.instances,[],'A late role result must not restart input after Stop')
        self.assertFalse(self.windows.input_enabled)
        self.assertFalse(self.c.execution.active)


if __name__=='__main__':unittest.main()

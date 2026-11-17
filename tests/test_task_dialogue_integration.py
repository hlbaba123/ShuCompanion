"""Task continuation and admission use real persistence, no live GUI input."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication
from shu.core import TaskEngine
from shu.core import ActionRejected
from shu.runtime import RuntimeStore
from shu.workflow_store import WorkflowStore
from shu.maa_coordinator import MaaCoordinator
from shu.controller import SessionController
from shu.providers import ProviderConfig
from shu.task_contract import parse_task_contract
from test_maa_coordinator import OfflineWorkflows, Roles, ReadOnlyWindows, wait_for
from test_leader_controller import Model, route

APP = QCoreApplication.instance() or QCoreApplication([])


class DialogueCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)
        self.runtime = RuntimeStore(self.root)
        self.store = WorkflowStore(self.engine)
        self.flow = OfflineWorkflows()
        self.c = MaaCoordinator(self.engine, ReadOnlyWindows(), self.runtime,
            role_config=ProviderConfig(), task_config=ProviderConfig(),
            permissions=lambda: (True, True), store=self.store,
            role_factory=Roles, workflow_factory=self.flow, recover=False)

    def tearDown(self):
        self.flow.release.set()
        self.c._closed=True  # Simulate process loss, without a graceful Pause command.
        self.runtime.close()
        self.engine.close()
        self.temp.cleanup()

    def test_answer_applies_new_contract_and_actually_launches_same_task(self):
        state = self.store.submit('孤星导入', {'goal': '孤星导入', 'request_id': 'original',
            'current_deliverable': 'maa_import'})
        self.store.suspend(state['task_id'], expected_epoch=state['epoch'], message='哪一关？', kind='business')
        self.store.management.accept_message('answer','CW-1普通')
        payload = {'goal': '导入 CW-1 普通', 'request_id': 'answer', 'relation': 'continue',
            'selected_task_id': state['task_id'], 'current_deliverable': 'maa_import'}
        result = self.c.continue_contract(state['task_id'], payload)
        wait_for(lambda: not self.c.active)
        self.assertEqual(result['task_id'], state['task_id'])
        self.assertEqual([x[0] for x in self.flow.runs], [state['task_id']])
        self.assertEqual(self.store.current(state['task_id'])['contract']['goal'], payload['goal'])
        self.assertEqual(self.store.management.question(state['task_id']), None)

    def test_waiting_task_does_not_prevent_new_task_or_cancel_it(self):
        first = self.c.submit('查甲', {'relation': 'new', 'current_deliverable': 'stage_catalog'})
        wait_for(lambda: not self.c.active)
        second = self.c.submit('查乙', {'relation': 'new', 'current_deliverable': 'stage_catalog'})
        wait_for(lambda: not self.c.active)
        self.assertEqual(self.store.current(first['task_id'])['status'], 'needs_input')
        self.assertEqual(self.store.current(second['task_id'])['status'], 'needs_input')

    def test_pending_tasks_are_not_lost_when_first_pending_is_cancelled(self):
        self.flow.blocking = True
        a=self.c.submit('甲', {'relation':'new','current_deliverable':'stage_catalog'})
        self.assertTrue(self.flow.entered.wait(1))
        b=self.c.submit('乙', {'relation':'new','current_deliverable':'stage_catalog'})
        c=self.c.submit('丙', {'relation':'new','current_deliverable':'stage_catalog'})
        self.c.stop(b['task_id'])
        self.flow.release.set()
        wait_for(lambda:not self.c.active)
        self.assertIn(c['task_id'],[i[0] for i in self.flow.runs])
        self.assertNotIn(b['task_id'],[i[0] for i in self.flow.runs])

    def test_user_pause_of_queued_task_cannot_be_reenabled_at_handoff(self):
        self.flow.blocking=True
        self.c.submit('甲', {'relation':'new','current_deliverable':'stage_catalog'})
        self.assertTrue(self.flow.entered.wait(1))
        b=self.c.submit('乙', {'relation':'new','current_deliverable':'stage_catalog'})
        self.store.control(b['task_id'],'pause')
        self.flow.release.set()
        wait_for(lambda:not self.c.active)
        self.assertNotIn(b['task_id'],[i[0] for i in self.flow.runs])

    def test_new_task_does_not_cancel_running_task(self):
        self.flow.blocking=True
        a=self.c.submit('甲', {'relation':'new','current_deliverable':'stage_catalog'})
        self.assertTrue(self.flow.entered.wait(1))
        b=self.c.submit('乙', {'relation':'new','current_deliverable':'stage_catalog'})
        self.assertFalse(self.c._job.cancel.is_set())
        self.assertEqual(self.store.current(a['task_id'])['status'],'running')
        self.flow.release.set()
        wait_for(lambda:not self.c.active)
        self.assertEqual([i[0] for i in self.flow.runs],[a['task_id'],b['task_id']])

    def test_committed_answer_before_launch_recovers_after_reopen(self):
        state=self.store.submit('查目录',{'current_deliverable':'stage_catalog','request_id':'original'})
        self.store.suspend(state['task_id'],expected_epoch=state['epoch'],message='哪一关?',kind='business')
        question=self.store.management.question(state['task_id'])
        self.store.management.accept_message('answer','A-1')
        state=self.store.current(state['task_id'])
        self.store.management.answer_question(state['task_id'],question['clarification_id'],
            {'goal':'A-1目录','request_id':'answer','current_deliverable':'stage_catalog'},
            request_id='answer',expected_revision=state['revision'],expected_epoch=state['epoch'])
        self.c.close()
        self.c=MaaCoordinator(self.engine,ReadOnlyWindows(),self.runtime,
            role_config=ProviderConfig(),task_config=ProviderConfig(),permissions=lambda:(True,True),
            store=self.store,role_factory=Roles,workflow_factory=self.flow,recover=True)
        wait_for(lambda:bool(self.flow.runs) and not self.c.active)
        self.assertEqual(self.flow.runs[0][0],state['task_id'])

    def test_old_interpretation_cannot_answer_a_newer_question(self):
        state=self.store.submit('查询',{'goal':'查询','current_deliverable':'stage_catalog'})
        self.store.suspend(state['task_id'],expected_epoch=state['epoch'],message='哪一关?',kind='business')
        old=self.store.current(state['task_id'])
        self.store.control(state['task_id'],'pause')
        payload={'goal':'核对CW-1','request_id':'answer','current_deliverable':'stage_catalog'}
        with self.assertRaises(ActionRejected):
            self.c.continue_contract(state['task_id'],payload,
                expected_state=(old['revision'],old['epoch']))


class DialogueControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict('os.environ', {'DEEPSEEK_API_KEY': ''})
        self.env.start()
        self.c = SessionController(self.root, self.root/'data')
        self.c.key_path.write_text('sk-offline-dialogue-fixture')
        self.errors = []
        self.c.error.connect(self.errors.append)

    def tearDown(self):
        self.c.close()
        self.env.stop()
        self.temp.cleanup()

    def test_busy_message_is_durably_received_then_processed(self):
        entered, release = threading.Event(), threading.Event()
        first = route('你好', '', intent='chat', reply='你好')
        second = route('我回来了', '', intent='chat', reply='欢迎回来')
        class SlowModel(Model):
            def complete(model, messages, **options):
                if not model.requests:
                    entered.set(); release.wait(3)
                return super().complete(messages, **options)
        self.c.client = self.c.intent_client = SlowModel(first, second)
        self.c.submit('你好')
        self.assertTrue(entered.wait(1))
        self.c.submit('我回来了')
        accepted = self.c.execution.store.management.messages()
        self.assertEqual([m['text'] for m in accepted], ['你好', '我回来了'])
        release.set()
        wait_for(lambda: not self.c.is_busy and all(
            m['status'] not in {'accepted', 'queued', 'processing'}
            for m in self.c.execution.store.management.messages()))
        self.assertEqual(len(self.c.client.requests), 2)
        self.assertEqual(self.errors, [])

    def test_explicit_new_input_target_is_available_and_visible(self):
        self.c.set_input_target(mode='new')
        self.assertEqual(self.c.task_management_state()['input_target']['mode'], 'new')

    def test_real_controller_answer_preserves_task_and_applies_payload(self):
        flow=OfflineWorkflows(result={'status':'needs_input','summary':'查询哪一关？','waiting_kind':'business'})
        self.c.execution.role_factory=Roles
        self.c.execution.workflow_factory=flow
        a='帮我查询关卡目录，关卡稍后补充'
        self.c.client=self.c.intent_client=Model(route(a,a,relation='new',current_deliverable='stage_catalog'))
        self.c.submit(a)
        wait_for(lambda:not self.c.is_busy and not self.c.execution.active)
        first=self.c.current_task()['id']
        b='CW-1，普通'
        self.c.client=self.c.intent_client=Model(route(b,'核对CW-1普通关卡目录',relation='modify',
            current_deliverable='stage_catalog',selected_task_id=first,
            target_stage_codes=['CW-1'],task_spec={'kind':'lookup','stage_code':'CW-1'}))
        self.c.submit(b)
        wait_for(lambda:not self.c.is_busy and not self.c.execution.active)
        self.assertEqual(self.errors,[])
        self.assertEqual(self.c.current_task()['id'],first)
        self.assertEqual(len(flow.runs),2)
        state=self.c.execution.store.current(first)
        self.assertEqual(state['contract']['target_stage_codes'],['CW-1'])
        self.assertEqual([m['task_id'] for m in self.c.execution.store.management.messages()],[first,first])


class DialogueContractTests(unittest.TestCase):
    def test_answer_concretizes_unspecified_scope_of_active_business_question(self):
        text='CW-1，普通'
        old={'task_id':'old','goal':'查作业','question':{'kind':'business','status':'open'},
            'contract':{'goal':'查作业','constraints':[], 'completion_criteria':[],
                'current_deliverable':'copilot_codes','target_stage_codes':[]}}
        value={'intent':'execute','relation':'modify','user_request':text,'goal':'CW-1普通作业',
            'constraints':[],'completion_criteria':[],'removed_constraints':[],'reply':'接着查询',
            'current_deliverable':'copilot_codes','selected_task_id':'old','source_task_id':None,
            'deferred_intents':[],'activity_name':'','target_stage_codes':['CW-1']}
        result=parse_task_contract(value,text,known_tasks=[old])
        self.assertEqual(list(result.target_stage_codes),['CW-1'])

    def test_execute_modify_can_remove_selected_old_constraint(self):
        text = '取消不下载的要求，继续查'
        old = {'task_id': 'old', 'goal': '查作业', 'contract': {
            'goal': '查作业', 'constraints': ['不下载JSON'],
            'completion_criteria': [], 'current_deliverable': 'copilot_codes'}}
        value = {'intent': 'execute', 'user_request': text, 'goal': '查作业',
            'constraints': [], 'completion_criteria': [], 'removed_constraints': ['不下载JSON'],
            'reply': '继续核查', 'relation': 'modify', 'current_deliverable': 'copilot_codes',
            'selected_task_id': 'old', 'source_task_id': None, 'deferred_intents': [],
            'activity_name': '', 'target_stage_codes': []}
        result = parse_task_contract(value, text, known_tasks=[old])
        self.assertEqual(result.constraints, ())

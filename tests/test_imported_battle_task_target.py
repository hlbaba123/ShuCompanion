"""A visible user-selected completed import is an explicit follow-up source."""
import copy
from pathlib import Path
import tempfile
import unittest

from tests import test_leader_controller as fixtures
from shu.core import ActionRejected, TaskEngine
from shu.workflow_store import WorkflowStore


class ImportedBattleTaskTargetTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.LeaderControllerTests('test_context_separates_requested_goal_from_available_capabilities')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.c=self.fixture.c

    def make_completed_import(self, label):
        payload={'goal':label,'current_deliverable':'maa_import','relation':'new',
                 'constraints':['本次不开始作战'],'target_stage_codes':['UT-EX-1'],
                 'task_spec':{'kind':'import','stage_code':'UT-EX-1'},
                 'import_scope':{'kind':'stages','stage_codes':['UT-EX-1'],
                                 'difficulty':2,'ordinal':None,'group_hint':''}}
        state=self.c.execution.store.submit(label,payload)
        with self.c.engine._transaction():
            self.c.engine._db.execute("UPDATE tasks SET status='completed' WHERE id=?",(state['task_id'],))
            self.c.engine._db.execute("UPDATE workflow_tasks SET writes_enabled=0,control_kind='import_verified' WHERE task_id=?",(state['task_id'],))
        return self.c.execution.store.current(state['task_id'])

    def test_selected_completed_import_uses_source_instead_of_modify_repair_loop(self):
        selected=self.make_completed_import('显式选择的导入')
        self.c._task_id=selected['task_id']
        self.c.set_input_target(selected['task_id'],mode='task')
        text='现在开始MAA自动战斗，运行刚才导入的这关一次，不使用理智药或源石。'
        response=fixtures.route(text,'核对并运行刚才导入的单作业一次',relation='followup',
            current_deliverable='gameplay',source_task_id=selected['task_id'],
            selected_task_id=None,task_spec={'kind':'repeat_stage','repeat_count':1,
                                            'medicine_limit':0,'premium_limit':0})
        original=copy.deepcopy(selected['contract'])
        self.fixture.send(text,response,response,response)
        self.assertEqual(self.fixture.errors,[])
        self.assertEqual(len(self.fixture.received),1,repr({'messages':self.fixture.messages,'requests':len(self.c.client.requests)}))
        result=self.fixture.received[0]
        self.assertEqual(result['relation'],'followup')
        self.assertEqual(result['source_task_id'],selected['task_id'])
        self.assertIsNone(result['selected_task_id'])
        self.assertNotIn('本次不开始作战',result['constraints'])
        self.assertNotEqual(self.c._task_id,selected['task_id'])
        self.assertEqual(self.c.execution.store.current(selected['task_id'])['contract'],original)
        self.assertEqual(len(self.c.client.requests),1)

    def test_selected_older_import_wins_over_model_newer_source(self):
        selected=self.make_completed_import('用户选中的旧导入')
        newer=self.make_completed_import('另一份较新导入')
        self.c._task_id=newer['task_id']
        self.c.set_input_target(selected['task_id'],mode='task')
        text='运行刚才导入的这关一次'
        response=fixtures.route(text,'运行选择的已导入作业一次',relation='followup',
            current_deliverable='gameplay',source_task_id=newer['task_id'],
            selected_task_id=None,task_spec={'kind':'repeat_stage','repeat_count':1})
        self.fixture.send(text,response,response,response)
        self.assertEqual(self.fixture.errors,[])
        self.assertEqual(len(self.fixture.received),1,repr({'messages':self.fixture.messages,'requests':len(self.c.client.requests)}))
        self.assertEqual(self.fixture.received[0]['source_task_id'],selected['task_id'])
        self.assertEqual(self.fixture.received[0]['relation'],'followup')
        self.assertEqual(len(self.c.client.requests),1)


class ExplicitImportFollowupAdmissionTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.engine=TaskEngine(Path(directory.name))
        self.addCleanup(self.engine.close)
        self.store=WorkflowStore(self.engine)
        self.source=self.store.submit('已完成的导入',{'current_deliverable':'maa_import'})
        self.source_id=self.source['task_id']
        with self.engine._transaction():
            self.engine._db.execute("UPDATE tasks SET status='completed' WHERE id=?",(self.source_id,))
            self.engine._db.execute("UPDATE workflow_tasks SET writes_enabled=0,control_kind='import_verified' WHERE task_id=?",(self.source_id,))
        self.store.management.accept_message('run','运行选定的已导入作业一次',mode='task',
                                             target_task_id=self.source_id)

    def followup(self, **changes):
        payload={'request_id':'run','relation':'followup','current_deliverable':'gameplay',
                 'source_task_id':self.source_id,'goal':'运行选定的已导入作业一次'}
        payload.update(changes)
        return self.store.submit('运行选定的已导入作业一次',payload)

    def test_selected_source_and_applied_task_are_preserved_as_separate_ids(self):
        target=self.followup()
        message=self.store.management.messages()[0]
        self.assertNotEqual(target['task_id'],self.source_id)
        self.assertEqual(message['target_task_id'],self.source_id)
        self.assertEqual(message['task_id'],target['task_id'])
        self.assertEqual(message['relation'],'followup')
        self.assertEqual(message['status'],'applied')
        self.assertEqual(self.store.current(self.source_id)['status'],'completed')
        self.assertEqual(self.followup()['task_id'],target['task_id'])

    def test_other_relation_source_or_delivery_cannot_rebind_explicit_target(self):
        another=self.store.submit('另一个任务',{'current_deliverable':'maa_import'})
        for changed in ({'relation':'new'}, {'source_task_id':None},
                        {'source_task_id':another['task_id']},
                        {'current_deliverable':'copilot_codes'},
                        {'current_deliverable':'maa_import'}):
            with self.subTest(changed=changed), self.assertRaises(ActionRejected):
                self.followup(**changed)
        self.assertEqual(self.store.management.messages()[0]['status'],'accepted')

    def test_noncompleted_unverified_or_recycled_source_cannot_rebind(self):
        with self.engine._transaction():
            self.engine._db.execute("UPDATE workflow_tasks SET control_kind='pause' WHERE task_id=?",(self.source_id,))
        with self.assertRaises(ActionRejected):self.followup()
        with self.engine._transaction():
            self.engine._db.execute("UPDATE workflow_tasks SET control_kind='import_verified' WHERE task_id=?",(self.source_id,))
            self.engine._db.execute("UPDATE tasks SET status='failed' WHERE id=?",(self.source_id,))
        with self.assertRaises(ActionRejected):self.followup()
        self.store.management.trash(self.source_id)
        with self.assertRaises(ActionRejected):self.followup()


if __name__=='__main__':unittest.main()

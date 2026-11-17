import tempfile
from pathlib import Path
import unittest
from shu.core import TaskEngine, ActionRejected
from shu.workflow_store import WorkflowStore
from shu.memory import ConversationMemory
from shu.task_context import task_context


class TaskContextTests(unittest.TestCase):
    def test_selected_task_context_never_merges_another_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);engine=TaskEngine(root);workflow=WorkflowStore(engine);memory=ConversationMemory(root)
            a=workflow.submit('任务A',{})['task_id'];b=workflow.submit('任务B',{})['task_id']
            for tid,req,text in ((a,'ra','A原话'),(b,'rb','B原话')):
                memory.append('user',text,request_id=req,task_id=tid)
                memory.append('assistant',text+'回答',request_id=req,task_id=tid)
            self.assertIn('A原话',task_context(memory,workflow,a)['history'])
            self.assertNotIn('B原话',task_context(memory,workflow,a)['history'])
            self.assertEqual(len(memory.load_messages(task_id=b)),2)
            workflow.management.trash(a)
            with self.assertRaises(ActionRejected):task_context(memory,workflow,a)
            memory.close();engine.close()

    def test_optional_preparer_failure_falls_back_without_changing_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);engine=TaskEngine(root);workflow=WorkflowStore(engine);memory=ConversationMemory(root)
            tid=workflow.submit('原合同',{})['task_id']
            memory.append('user','文本'*100,request_id='r',task_id=tid)
            memory.append('assistant','回答'*100,request_id='r',task_id=tid)
            def failed(**kwargs):raise TimeoutError()
            value=task_context(memory,workflow,tid,prepare=failed,char_budget=30)
            self.assertEqual(value['history_source'],'visible_task_transcript')
            self.assertEqual(workflow.current(tid)['goal'],'原合同')
            self.assertLessEqual(len(value['history']),30)
            memory.close();engine.close()

    def test_core_only_legacy_task_can_recycle_without_executable_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=TaskEngine(Path(directory));workflow=WorkflowStore(engine)
            old=engine.submit_goal('旧任务')
            self.assertTrue(workflow.management.list_tasks()[0]['legacy_only'])
            workflow.management.trash(old['id'])
            self.assertFalse(workflow.current(old['id'])['writes_enabled'])
            self.assertEqual(workflow.current(old['id'])['contract']['attempt_limit'],0)
            self.assertEqual(engine._db.execute("SELECT COUNT(*) FROM workflow_outbox WHERE type='command.submitted'").fetchone()[0],0)
            workflow.management.restore(old['id'])
            self.assertFalse(workflow.current(old['id'])['writes_enabled'])
            engine.close()

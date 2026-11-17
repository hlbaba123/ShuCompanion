"""Import subsets are contract-derived, ordered and durable; no GUI or network."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shu.core import TaskEngine, ActionRejected
from shu.leader_plan import PlanStore
from shu.workflow_store import WorkflowStore


class QueuePlanRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)
        self.addCleanup(self.engine.close)
        self.store = WorkflowStore(self.engine)
        self.plans = PlanStore(self.store)
        self.stages = [dict(stage_key=f'k{i}', display_code=f'UNIT-{i}',
            stage_id=f'unit_{i}', query_key=f'unit_{i}', difficulty=1,
            query_key_verified=True, difficulty_verified=True,
            opening_status='permanent', group_id='documented-group') for i in (1, 2, 3)]
        self.structure = dict(ok=True, schema_version=1, activity_id='unit',
            activity_name='测试活动', catalog_complete=True, stages=self.stages,
            groups=[dict(group_id='documented-group', label='实际章节',
                         stage_keys=['k1', 'k2', 'k3'])], excluded_stage_keys=[])
        path = self.root/'structure.json'
        path.write_text(json.dumps(self.structure, ensure_ascii=False), encoding='utf-8')
        self.ref = {'path':str(path), 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

    def task(self, targets=(), typed='maa_import'):
        return self.store.submit('导入测试活动的作业，不开始作战', {
            'goal':'导入测试活动的作业，不开始作战',
            'user_request':'导入测试活动的作业，不开始作战',
            'current_deliverable':typed, 'activity_name':'测试活动',
            'allow_start':False, 'attempt_limit':0,
            'task_spec':{'kind':'import' if typed=='maa_import' else 'lookup'},
            'target_stage_codes':list(targets)})

    def register(self, task, keys):
        return self.plans.register(task['task_id'], expected_revision=task['revision'],
            expected_epoch=task['epoch'], structure_ref=self.ref, ordered_steps=keys,
            import_stage_keys=keys, search_policy={})

    def test_full_activity_import_keeps_all_real_catalog_steps(self):
        task = self.task()
        result = self.register(task, ['k1','k2','k3'])
        self.assertEqual(result['ordered_stage_keys'], ['k1','k2','k3'])
        self.assertEqual(result['import_stage_keys'], ['k1','k2','k3'])
        self.assertEqual(self.register(task, ['k1','k2','k3']), result)
        events = self.engine._db.execute("SELECT * FROM workflow_outbox WHERE type='plan.registered'").fetchall()
        self.assertEqual(len(events), 1)

    def test_explicit_subset_follows_catalog_order_not_request_order(self):
        task = self.task(['UNIT-3','UNIT-1'])
        result = self.register(task, ['k1','k3'])
        self.assertEqual(result['ordered_stage_keys'], ['k1','k3'])
        self.assertEqual(len(result['stages']), 3)

    def test_import_subset_cannot_omit_target_or_reorder(self):
        for keys in (['k1'], ['k3','k1'], ['k1','k2','k3']):
            with self.subTest(keys=keys):
                task = self.task(['UNIT-1','UNIT-3'])
                with self.assertRaises((ValueError,ActionRejected)):
                    self.register(task,keys)
                self.assertIsNone(self.plans.current(task['task_id']))

    def test_subset_parameter_cannot_grant_import_to_readonly_contract(self):
        task = self.task(['UNIT-1'], typed='copilot_codes')
        with self.assertRaises((ValueError,ActionRejected)):
            self.register(task,['k1'])

    def test_trash_before_registration_cannot_publish_plan(self):
        task = self.task()
        self.store.management.trash(task['task_id'])
        with self.assertRaises(ActionRejected):
            self.register(task,['k1','k2','k3'])

    def test_production_registration_handles_whole_activity_in_catalog_order(self):
        from shu.planned_agents import register_import_plan
        task = self.task()
        state = {**task,'role_results':{'catalog':{'structure_ref':self.ref}}}
        with patch('shu.planned_agents.verify_planned_completion',return_value={'complete':True}):
            result = register_import_plan(self.store,state,task['contract'])
        self.assertEqual(result['import_stage_keys'],['k1','k2','k3'])

    def test_ambiguous_mode_is_business_selection_and_never_registers(self):
        from shu.planned_agents import register_import_plan, ImportSelectionRequired
        task = self.store.submit('不要普通，只导入突袭', {
            'user_request':'不要普通，只导入突袭', 'current_deliverable':'maa_import',
            'allow_start':False,'attempt_limit':0,'task_spec':{'kind':'import'}})
        state = {**task,'role_results':{'catalog':{'structure_ref':self.ref}}}
        with patch('shu.planned_agents.verify_planned_completion',return_value={'complete':True}):
            with self.assertRaises(ImportSelectionRequired):
                register_import_plan(self.store,state,task['contract'])
        self.assertIsNone(self.plans.current(task['task_id']))


if __name__=='__main__':
    unittest.main()

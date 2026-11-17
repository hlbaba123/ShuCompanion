import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from shu.core import ActionRejected, TaskEngine
from shu.workflow_store import WorkflowStore
from shu.leader_plan import PlanStore


class LeaderPlanTests(unittest.TestCase):
    def test_base_query_can_resolve_labelled_rerun_but_not_another_activity(self):
        from shu.leader_plan import activity_query_matches
        self.assertTrue(activity_query_matches('示例活动', '示例活动·复刻'))
        self.assertFalse(activity_query_matches('别的活动', '示例活动·复刻'))
        self.assertFalse(activity_query_matches('示例活动·复刻', '示例活动'))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)
        self.workflow = WorkflowStore(self.engine)
        self.state = self.workflow.submit('查活动的作业代码', {'current_deliverable': 'copilot_codes'})
        self.task_id = self.state['task_id']
        self.plans = PlanStore(self.workflow)
        stages = [dict(stage_key=f'k{i}', display_code=f'ZZ-{i}', stage_id=f'event_{i}',
                       query_key=f'event_{i}', difficulty=1, group_id='actual') for i in (1, 2)]
        self.structure = dict(ok=True, schema_version=1, activity_id='event', activity_name='示例活动',
                              catalog_complete=True, stages=stages,
                              groups=[dict(group_id='actual', label='实际章节', stage_keys=['k1', 'k2'])],
                              excluded_stage_keys=[])
        raw = json.dumps(self.structure, ensure_ascii=False).encode('utf-8')
        path = self.root / 'structure.json'
        path.write_bytes(raw)
        self.ref = dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest())

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def register(self):
        return self.plans.register(self.task_id, expected_revision=1, expected_epoch=1,
            structure_ref=self.ref, ordered_steps=['k1', 'k2'], search_policy={})

    def claim(self, dispatch='first'):
        state = self.workflow.current(self.task_id)
        job = self.workflow.ensure_dispatch(self.task_id, 'research', dispatch, {})
        return self.plans.claim_next(self.task_id, expected_revision=state['revision'],
            expected_epoch=state['epoch'], dispatch_id=job['dispatch_id'])

    def test_only_next_step_is_claimed_and_late_result_cannot_reopen_it(self):
        self.register()
        first = self.claim()
        self.assertEqual(first['resolved_stage']['display_code'], 'ZZ-1')
        self.assertEqual(self.claim()['lease_id'], first['lease_id'])
        self.plans.close_step(first, dict(status='search_incomplete', primary_ref=None, backup_ref=None))
        second = self.claim('next')
        self.assertEqual(second['resolved_stage']['display_code'], 'ZZ-2')
        with self.assertRaises(ActionRejected):
            self.plans.close_step(first, dict(status='search_incomplete'))

    def test_register_cannot_reorder_or_drop_a_catalog_member_silently(self):
        with self.assertRaises(ValueError):
            self.plans.register(self.task_id, expected_revision=1, expected_epoch=1,
                structure_ref=self.ref, ordered_steps=['k2', 'k1'], search_policy={})

    def test_legacy_chapter_label_can_bind_only_its_verified_parent_group(self):
        state = self.workflow.submit('查实际章节的作业', {'current_deliverable':'copilot_codes',
                                     'activity_name':'实际章节'})
        plan = self.plans.register(state['task_id'], expected_revision=1, expected_epoch=1,
            structure_ref=self.ref, ordered_steps=['k1','k2'], search_policy={})
        self.assertEqual(plan['activity_name'], '示例活动')
        state = self.workflow.submit('查不存在章节', {'current_deliverable':'copilot_codes',
                                     'activity_name':'不存在章节'})
        with self.assertRaises(ValueError):
            self.plans.register(state['task_id'], expected_revision=1, expected_epoch=1,
                structure_ref=self.ref, ordered_steps=['k1','k2'], search_policy={})

    def test_explicit_query_subset_cannot_be_widened_at_plan_registration(self):
        state = self.workflow.submit('只查ZZ-2', {'current_deliverable':'copilot_codes',
                                    'target_stage_codes':['ZZ-2']})
        with self.assertRaises(ValueError):
            self.plans.register(state['task_id'], expected_revision=1, expected_epoch=1,
                structure_ref=self.ref, ordered_steps=['k1','k2'], search_policy={})
        plan = self.plans.register(state['task_id'], expected_revision=1, expected_epoch=1,
            structure_ref=self.ref, ordered_steps=['k2'], search_policy={})
        self.assertEqual(plan['ordered_stage_keys'], ['k2'])

    def test_backup_budget_survives_pause_resume_and_unsettled_reservation(self):
        self.register()
        first = self.claim()
        reservation = self.plans.reserve_budget(first, phase='backup', pages=1, milliseconds=10000)
        self.plans.settle_budget(reservation, elapsed_ms=1500)
        self.plans.reserve_budget(first, phase='backup', pages=1, milliseconds=10000)
        self.workflow.control(self.task_id, 'pause')
        self.workflow.control(self.task_id, 'resume')
        reopened = PlanStore(self.workflow)
        current = self.claim('resume')
        self.assertEqual(current['budget']['backup']['pages_remaining'], 0)
        self.assertEqual(current['budget']['backup']['milliseconds_remaining'], 18500)
        with self.assertRaises(ActionRejected):
            reopened.reserve_budget(current, phase='backup', pages=1, milliseconds=1)
        with self.assertRaises(ActionRejected):
            reopened.close_step(first, dict(status='search_incomplete'))

    def test_revision_is_workflow_revision_and_preserves_pause_and_revocation(self):
        self.register()
        paused = self.workflow.control(self.task_id, 'pause')
        value = self.plans.revise(self.task_id, expected_revision=1,
            expected_epoch=paused['epoch'], change={'contract': {'note': '更正来源'}}, reason='核实资料')
        current = self.workflow.current(self.task_id)
        self.assertEqual(current['revision'], 2)
        self.assertEqual(value['revision'], 2)
        self.assertFalse(current['writes_enabled'])
        self.assertEqual(current['status'], 'paused')
        self.assertEqual(current['contract']['current_deliverable'], 'copilot_codes')

    def test_plan_and_outbox_are_committed_together_and_replay_is_idempotent(self):
        first = self.register()
        self.assertEqual(self.register(), first)
        rows = self.engine._db.execute("SELECT * FROM workflow_outbox WHERE type='plan.registered'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.plans.current(self.task_id)['revision'], 1)

    def test_finished_dispatch_cannot_keep_spending_or_close_lease(self):
        self.register()
        lease = self.claim()
        self.workflow.finish_dispatch(lease['dispatch_id'], {'status': 'completed'})
        with self.assertRaises(ActionRejected):
            self.plans.reserve_budget(lease, phase='primary', pages=1, milliseconds=10)
        with self.assertRaises(ActionRejected):
            self.plans.close_step(lease, {'status': 'search_incomplete'})

    def test_groups_cannot_silently_omit_catalog_stage(self):
        self.structure['groups'][0]['stage_keys'] = ['k1']
        raw = json.dumps(self.structure).encode()
        Path(self.ref['path']).write_bytes(raw)
        self.ref['sha256'] = hashlib.sha256(raw).hexdigest()
        with self.assertRaises(ValueError):
            self.plans.register(self.task_id, expected_revision=1, expected_epoch=1,
                structure_ref=self.ref, ordered_steps=['k1'], search_policy={})


if __name__ == '__main__':
    unittest.main()

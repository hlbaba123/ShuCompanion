"""Retire proven-exited recycled workers without reopening ordinary dispatch IO."""
from pathlib import Path
import tempfile
import unittest

from shu.core import ActionRejected, TaskEngine
from shu.task_cleanup import TaskCleanup
from shu.workflow_store import WorkflowStore


class TaskRetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = TaskEngine(Path(self.temp.name))
        self.store = WorkflowStore(self.engine)
        self.task_id = self.store.submit('private task', {})['task_id']
        self.peer_id = self.store.submit('keep task', {})['task_id']

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def claim(self, role):
        owner = 'retirement-test-' + role
        self.store.register_runner(owner)
        dispatch = self.store.ensure_dispatch(self.task_id, role, role, {})
        self.assertIsNotNone(self.store.claim_dispatch(dispatch['dispatch_id'], owner))
        return owner, dispatch

    def test_recycled_readonly_dispatches_retire_after_joined_exit_then_purge(self):
        claims = [self.claim(role) for role in ('leader', 'research', 'catalog', 'diagnosis')]
        self.store.management.trash(self.task_id)
        # A cancel flag or elapsed timeout is never proof of executor exit.
        self.assertEqual(self.store.audit_abandoned_dispatches(self.task_id), [])
        self.assertEqual(TaskCleanup(self.store).purge(self.task_id)['status'], 'blocked')
        before = [event['event_id'] for event in self.store.pending_events(self.task_id)]
        for owner, _ in claims:
            self.store.release_runner(owner)

        retired = self.store.audit_abandoned_dispatches(self.task_id)
        self.assertEqual(len(retired), len(claims))
        self.assertTrue(all(item['status'] == 'interrupted' for item in retired))
        self.assertEqual(self.store.management.lifecycle(self.task_id)['state'], 'purge_pending')
        self.assertFalse(self.store.current(self.task_id)['writes_enabled'])
        # Retirement is a restricted historical receipt, not executable Outbox work.
        self.assertEqual([event['event_id'] for event in self.store.pending_events(self.task_id)], before)
        self.assertEqual(self.store.audit_abandoned_dispatches(self.task_id), [])
        for _, dispatch in claims:
            with self.assertRaises(ActionRejected):
                self.store.finish_dispatch(dispatch['dispatch_id'], {'late': True})
        self.assertEqual(TaskCleanup(self.store).purge(self.task_id)['status'], 'completed')
        self.assertEqual(self.store.current(self.peer_id)['goal'], 'keep task')

    def test_recycled_maa_retirement_preserves_unknown_effect_and_barrier(self):
        owner, dispatch = self.claim('maa')
        operation = self.store.prepare_operation(self.task_id, dispatch['dispatch_id'], 'effect', {'kind': 'import'})
        self.store.issue_operation(operation['operation_id'])
        self.store.enter_submission(operation['operation_id'])
        self.store.management.trash(self.task_id)
        before = self.store.slot_state()
        self.store.release_runner(owner)

        retired = self.store.audit_abandoned_dispatches(self.task_id)
        self.assertEqual([item['status'] for item in retired], ['unknown'])
        self.assertEqual(self.store.get_operation(operation['operation_id'])['status'], 'unknown')
        self.assertEqual(self.store.slot_state(), before)
        self.assertEqual(TaskCleanup(self.store).purge(self.task_id)['status'], 'blocked')
        self.assertEqual(self.store.management.lifecycle(self.task_id)['state'], 'purge_pending')


if __name__ == '__main__':
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from shu.core import TaskEngine, ActionRejected
from shu.workflow_store import WorkflowStore


class TaskManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = TaskEngine(Path(self.tmp.name))
        self.store = WorkflowStore(self.engine)
        self.task = self.store.submit('查询作业', {'allow_start': False})
        self.id = self.task['task_id']
        self.m = self.store.management

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def waiting(self, kind='business'):
        self.store.suspend(self.id, expected_epoch=self.store.current(self.id)['epoch'], message='哪一关？', kind=kind)
        return self.m.question(self.id)

    def answer(self, q):
        self.m.accept_message('answer', 'CW-1')
        return self.m.answer_question(self.id, q['clarification_id'], {'stage': 'CW-1'}, request_id='answer', expected_revision=q['revision'], expected_epoch=q['epoch'])

    def test_message_identity_and_payload(self):
        a = self.m.accept_message('r', 'hello')
        self.assertEqual(a, self.m.accept_message('r', 'hello'))
        with self.assertRaises(ValueError): self.m.accept_message('r', 'different')
        with self.assertRaises(ValueError): self.m.accept_message('r', 'hello', mode='new')

    def test_answer_cas_and_restart(self):
        q = self.waiting()
        state = self.answer(q)
        self.assertTrue(state['writes_enabled'])
        self.assertGreater(state['epoch'], q['epoch'])
        with self.assertRaises(ActionRejected): self.answer(q)
        self.engine.close()
        self.engine = TaskEngine(Path(self.tmp.name))
        self.store = WorkflowStore(self.engine)
        self.assertIsNone(self.store.management.question(self.id))
        events = [e for e in self.store.pending_events(self.id) if e['type'] == 'command.continue']
        self.assertEqual(len(events), 1)
        self.assertEqual(self.store.management.messages(task_id=self.id)[0]['status'], 'applied')

    def test_pause_and_trash_reject_answer(self):
        q = self.waiting()
        self.store.control(self.id, 'pause')
        with self.assertRaises(ActionRejected): self.answer(q)
        self.m.trash(self.id)
        with self.assertRaises(ActionRejected): self.answer(q)

    def test_blocked_question_does_not_resume(self):
        self.waiting('blocked')
        self.assertIsNone(self.m.question(self.id))
        q = self.m.open_question(self.id, '关卡？')
        with self.assertRaises(ActionRejected): self.answer(q)

    def test_restore_keeps_writes_false_and_tokens_dead(self):
        q = self.waiting()
        first = self.m.trash(self.id)
        self.assertEqual(first, self.m.trash(self.id))
        self.m.restore(self.id)
        self.assertFalse(self.store.current(self.id)['writes_enabled'])
        self.assertIsNone(self.m.question(self.id))
        with self.assertRaises(ActionRejected): self.answer(q)
        self.assertEqual(len(self.m.list_tasks()), 1)
        self.assertEqual(self.m.list_tasks(trashed=True), [])

    def test_trash_a_preserves_b_slot(self):
        b = self.store.submit('B', {})
        d = self.store.ensure_dispatch(b['task_id'], 'maa', 'b', {})
        op = self.store.prepare_operation(b['task_id'], d['dispatch_id'], 'b-op', {'kind': 'import'})
        self.store.issue_operation(op['operation_id'])
        before = self.store.slot_state()
        self.m.trash(self.id)
        self.assertEqual(before, self.store.slot_state())
        self.store.enter_submission(op['operation_id'])

    def test_trash_gates_new_work_and_late_results(self):
        d = self.store.ensure_dispatch(self.id, 'research', 'r', {})
        self.m.trash(self.id)
        for operation in (lambda: self.store.submit('again', {}, self.id), lambda: self.store.control(self.id, 'resume'), lambda: self.store.ensure_dispatch(self.id, 'research', 'new', {}), lambda: self.store.finish_dispatch(d['dispatch_id'], {'late': True})):
            with self.assertRaises(ActionRejected): operation()
        self.assertEqual(self.store.current(self.id)['task_id'], self.id)

    def test_stale_trash_is_atomic(self):
        with self.assertRaises(ActionRejected): self.m.trash(self.id, expected_epoch=999)
        self.assertTrue(self.store.current(self.id)['writes_enabled'])

    def test_answer_publication_failure_rolls_back_every_write(self):
        q = self.waiting()
        self.m.accept_message('answer', 'CW-1')
        before = self.store.current(self.id)
        publish = self.store._publish_event
        def fail(*args, **kwargs):
            raise RuntimeError('disk failure')
        self.store._publish_event = fail
        try:
            with self.assertRaises(RuntimeError): self.answer(q)
        finally:
            self.store._publish_event = publish
        self.assertEqual(self.store.current(self.id), before)
        self.assertEqual(self.m.question(self.id), q)
        self.assertEqual(self.m.messages()[0]['status'], 'accepted')
        self.assertEqual(self.m.messages()[0]['task_id'], None)

    def test_trash_fences_inbox_command(self):
        event = self.store.pending_events(self.id)[0]
        self.m.trash(self.id)
        with self.assertRaises(ActionRejected): self.store.accept_event('graph', event['event_id'])
        self.m.restore(self.id)
        with self.assertRaises(ActionRejected): self.store.accept_event('graph', event['event_id'])

    def test_trashed_task_cannot_stop_other_tasks_slot(self):
        b = self.store.submit('B', {})
        d = self.store.ensure_dispatch(b['task_id'], 'maa', 'b', {})
        op = self.store.prepare_operation(b['task_id'], d['dispatch_id'], 'b-op', {'kind': 'import'})
        self.store.issue_operation(op['operation_id'])
        self.m.trash(self.id)
        a = self.store.current(self.id)
        with self.assertRaises(ActionRejected):
            self.store.prepare_control_operation(self.id, a['control_seq'], 'a-stop', {'kind': 'stop'})

    def test_valid_answer_is_preserved_while_unknown_input_blocks_continuation(self):
        dispatch = self.store.ensure_dispatch(self.id, 'maa', 'import', {})
        op = self.store.prepare_operation(self.id, dispatch['dispatch_id'], 'import-op', {'kind': 'import'})
        self.store.issue_operation(op['operation_id'])
        self.store.enter_submission(op['operation_id'])
        q = self.waiting()
        state = self.answer(q)
        self.assertFalse(state['writes_enabled'])
        self.assertTrue(state['blocked_reason'])
        self.assertEqual(state['contract'], {'stage': 'CW-1'})
        self.assertIsNone(self.m.question(self.id))
        self.assertEqual(self.m.messages(task_id=self.id)[0]['status'], 'applied')
        self.engine.close()
        self.engine = TaskEngine(Path(self.tmp.name))
        self.store = WorkflowStore(self.engine)
        self.assertEqual(self.store.current(self.id)['blocked_reason'], state['blocked_reason'])
        self.assertFalse(self.store.current(self.id)['writes_enabled'])
        with self.assertRaises(ActionRejected):
            self.store.ensure_dispatch(self.id, 'research', 'premature', {})

    def test_pause_a_preserves_b_slot(self):
        b = self.store.submit('B', {})
        d = self.store.ensure_dispatch(b['task_id'], 'maa', 'b', {})
        op = self.store.prepare_operation(b['task_id'], d['dispatch_id'], 'b-op', {'kind': 'import'})
        self.store.issue_operation(op['operation_id'])
        before = self.store.slot_state()
        self.store.control(self.id, 'pause')
        self.assertEqual(before, self.store.slot_state())
        self.store.enter_submission(op['operation_id'])

    def test_start_run_requires_current_active_write_authority(self):
        epoch = self.store.current(self.id)['epoch']
        with self.assertRaises(ActionRejected): self.m.start_run(self.id, expected_epoch=epoch + 1)
        running = self.m.start_run(self.id, expected_epoch=epoch)
        self.assertEqual(running['status'], 'running')
        self.assertEqual(running['epoch'], epoch)
        self.m.trash(self.id)
        with self.assertRaises(ActionRejected): self.m.start_run(self.id, expected_epoch=epoch)
        self.m.restore(self.id)
        with self.assertRaises(ActionRejected):
            self.m.start_run(self.id, expected_epoch=self.store.current(self.id)['epoch'])


if __name__ == '__main__': unittest.main()

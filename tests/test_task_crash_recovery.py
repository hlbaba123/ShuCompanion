"""Durable message application receipts survive the coordinator crash window."""
from pathlib import Path
import tempfile
import threading
import unittest

from shu.core import ActionRejected, TaskEngine
from shu.workflow_store import WorkflowStore


class TaskCrashRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.engine = TaskEngine(self.root)
        self.store = WorkflowStore(self.engine)

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def reopen(self):
        self.engine.close()
        self.engine = TaskEngine(self.root)
        self.store = WorkflowStore(self.engine)

    def test_new_request_is_bound_in_submit_transaction_and_retry_does_not_duplicate(self):
        self.store.management.accept_message('request', '查询 CW-1')
        payload = {'request_id': 'request', 'relation': 'new', 'goal': '查询 CW-1'}
        original = self.store.submit(payload['goal'], payload)
        # Simulate dying before the controller receives the submit result.
        self.reopen()
        message = self.store.management.messages()[0]
        self.assertEqual((message['status'], message['task_id']), ('applied', original['task_id']))
        retry = self.store.submit(payload['goal'], payload)
        self.assertTrue(retry['request_replayed'])
        self.assertEqual(retry['task_id'], original['task_id'])
        self.assertEqual(len(self.engine.list_tasks()), 1)
        with self.assertRaises(ActionRejected):
            self.store.submit('查询 CW-2', {**payload, 'goal': '查询 CW-2'})
        self.assertEqual(len(self.engine.list_tasks()), 1)

    def test_modify_receipt_is_atomic_and_same_retry_does_not_increment_revision(self):
        task = self.store.submit('查询', {'goal': '查询'})
        self.store.management.accept_message('edit', '补充 CW-1')
        payload = {'request_id': 'edit', 'relation': 'modify', 'goal': '查询 CW-1'}
        updated = self.store.replace_contract(task['task_id'], payload['goal'], payload,
            expected_revision=task['revision'], expected_epoch=task['epoch'])
        self.reopen()
        self.assertEqual(self.store.management.messages()[0]['status'], 'applied')
        retry = self.store.replace_contract(task['task_id'], payload['goal'], payload,
            expected_revision=task['revision'], expected_epoch=task['epoch'])
        self.assertTrue(retry['request_replayed'])
        self.assertEqual(retry['revision'], updated['revision'])

    def test_two_store_connections_apply_same_request_only_once(self):
        self.store.management.accept_message('request', '查询')
        payload = {'request_id': 'request', 'goal': '查询'}
        other_engine = TaskEngine(self.root)
        other_store = WorkflowStore(other_engine)
        ready = threading.Barrier(2)
        results, errors = [], []
        def submit(store):
            try:
                ready.wait(timeout=2)
                results.append(store.submit('查询', payload))
            except Exception as exc:
                errors.append(exc)
        workers = [threading.Thread(target=submit, args=(store,)) for store in (self.store, other_store)]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=3)
            self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertEqual(errors, [])
            self.assertEqual(len({result['task_id'] for result in results}), 1)
            self.assertEqual(sum(bool(result.get('request_replayed')) for result in results), 1)
            self.assertEqual(len(self.engine.list_tasks()), 1)
        finally:
            other_engine.close()

    def test_receipt_failure_rolls_back_task_and_request_application(self):
        self.store.management.accept_message('request', '查询')
        payload = {'request_id': 'request', 'goal': '查询'}
        original = self.store.management._finish_message
        def fail(*args, **kwargs):
            raise RuntimeError('message storage failure')
        self.store.management._finish_message = fail
        try:
            with self.assertRaises(RuntimeError):
                self.store.submit('查询', payload)
        finally:
            self.store.management._finish_message = original
        self.assertEqual(self.engine.list_tasks(), [])
        self.assertEqual(self.store.management.messages()[0]['status'], 'accepted')

    def test_late_duplicate_request_cannot_revive_a_recycled_task(self):
        self.store.management.accept_message('request', '查询')
        payload = {'request_id': 'request', 'goal': '查询'}
        task = self.store.submit('查询', payload)
        self.store.management.trash(task['task_id'])
        self.reopen()
        with self.assertRaises(ActionRejected):
            self.store.submit('查询', payload)
        self.assertEqual(len(self.engine.list_tasks()), 1)
        self.assertEqual(self.store.management.lifecycle(task['task_id'])['state'], 'trashed')

    def test_uncheckpointed_continuation_survives_reopen_until_explicit_ack(self):
        task = self.store.submit('导入', {'goal': '导入'})
        self.store.suspend(task['task_id'], expected_epoch=task['epoch'], message='哪一关？', kind='business')
        question = self.store.management.question(task['task_id'])
        self.store.management.accept_message('answer', 'CW-1')
        payload = {'request_id': 'answer', 'goal': '导入 CW-1'}
        result = self.store.management.answer_question(task['task_id'], question['clarification_id'], payload,
            request_id='answer', expected_revision=question['revision'], expected_epoch=question['epoch'])
        self.reopen()
        events = self.store.management.pending_continuations()
        self.assertEqual([row['event_id'] for row in events], [result['continuation_event_id']])
        self.store.management.ack_continuation(events[0]['event_id'])
        self.assertEqual(self.store.management.pending_continuations(), [])
        self.store.management.ack_continuation(events[0]['event_id'])

    def test_user_pause_withdraws_old_continuation_from_recovery(self):
        task = self.store.submit('导入', {'goal': '导入'})
        self.store.suspend(task['task_id'], expected_epoch=task['epoch'], message='哪一关？', kind='business')
        question = self.store.management.question(task['task_id'])
        self.store.management.accept_message('answer', 'CW-1')
        self.store.management.answer_question(task['task_id'], question['clarification_id'],
            {'request_id': 'answer', 'goal': '导入 CW-1'}, request_id='answer',
            expected_revision=question['revision'], expected_epoch=question['epoch'])
        self.store.control(task['task_id'], 'pause')
        self.reopen()
        self.assertEqual(self.store.management.pending_continuations(), [])


if __name__ == '__main__':
    unittest.main()

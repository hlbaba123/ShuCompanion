"""Request-owned history lifecycle against isolated SQLite and JSON stores."""
import importlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from shu.activity_history import ActivityHistory
from shu.core import ActionRejected, TaskEngine
from shu.memory import ConversationMemory
from shu.workflow_store import WorkflowStore


class ChatHistoryManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.path = Path(self.temp.name)
        self.engine = TaskEngine(self.path)
        self.store = WorkflowStore(self.engine)
        self.memory = ConversationMemory(self.path)
        self.history = ActivityHistory(self.path / 'activity-history.json')

    def tearDown(self):
        self.memory.close()
        self.engine.close()
        self.temp.cleanup()

    def manager(self):
        try:
            module = importlib.import_module('shu.chat_history_management')
        except ModuleNotFoundError:
            module = None
        self.assertIsNotNone(module, 'ordinary chats need an independent request-owned lifecycle')
        return module.ChatHistoryManagement(self.store.management, self.memory, self.history)

    def chat(self, request_id, text='相同的原话'):
        self.store.management.accept_message(request_id, text, mode='chat')
        self.store.management.finish_message(request_id, relation='chat', summary='聊天已返回')
        self.memory.append('user', text, request_id=request_id)
        self.memory.append('assistant', '普通对话回复', request_id=request_id)
        self.history.begin(request_id, text)
        self.history.finish(request_id, 'completed', '普通对话回复')

    def test_workflow_chat_and_legacy_are_recoverable_without_creating_tasks(self):
        self.chat('chat')
        self.history.begin('legacy', '旧历史')
        manager = self.manager()
        self.assertEqual({row['request_id'] for row in manager.list_chats()}, {'chat', 'legacy'})
        self.assertEqual(self.store.management.list_tasks(), [])
        manager.trash('legacy')
        self.assertEqual([row['request_id'] for row in manager.list_chats()], ['chat'])
        restarted = self.manager()
        self.assertEqual([row['request_id'] for row in restarted.list_chats(trashed=True)], ['legacy'])
        restarted.restore('legacy')
        self.assertEqual({row['request_id'] for row in restarted.list_chats()}, {'chat', 'legacy'})
        self.assertEqual(self.store.management.list_tasks(), [])
        self.assertEqual(self.store.pending_events(), [])

    def test_trash_hides_only_exact_request_and_restore_recovers_messages(self):
        self.chat('a')
        self.chat('b')
        manager = self.manager()
        manager.trash('a')
        self.assertEqual([m['request_id'] for m in self.memory.load_messages()], ['b', 'b'])
        manager.restore('a')
        self.assertEqual([m['request_id'] for m in self.memory.load_messages()], ['a', 'a', 'b', 'b'])

    def test_every_explicit_task_owner_is_rejected_as_chat(self):
        task = self.store.submit('真实任务', {})['task_id']
        self.store.management.accept_message('workflow-owned', '任务原话')
        self.store.management.finish_message('workflow-owned', task_id=task)
        self.history.begin('history-owned', '任务旧历史')
        self.history.append('history-owned', {'task_id': task, 'stage': 'notice', 'summary': '证据'})
        self.chat('memory-owned')
        self.memory.bind_request('memory-owned', task, [])
        manager = self.manager()
        self.assertEqual(manager.list_chats(), [])
        for request_id in ('workflow-owned', 'history-owned', 'memory-owned'):
            with self.subTest(request_id=request_id), self.assertRaises(ActionRejected):
                manager.trash(request_id)
        self.assertEqual(len(self.store.management.list_tasks()), 1)

    def test_pending_or_explicit_task_admission_cannot_be_recycled_as_chat(self):
        self.store.management.accept_message('pending', '待理解')
        task = self.store.submit('真实任务', {})['task_id']
        self.store.management.accept_message('targeted', '补充任务', mode='task', target_task_id=task)
        manager = self.manager()
        self.assertEqual(manager.list_chats(), [])
        for request_id in ('pending', 'targeted', 'unknown'):
            with self.subTest(request_id=request_id), self.assertRaises((ActionRejected, KeyError)):
                manager.trash(request_id)

    def test_purge_removes_exact_messages_and_history_but_preserves_neighbors(self):
        self.chat('a')
        self.chat('b')
        neighbor = self.history._record_path('b').read_bytes()
        manager = self.manager()
        with self.assertRaises(ActionRejected):
            manager.request_purge('a')
        manager.trash('a')
        self.assertEqual(manager.request_purge('a')['status'], 'queued')
        with self.assertRaises(ActionRejected):
            manager.restore('a')
        self.assertEqual(manager.purge('a')['status'], 'completed')
        self.assertEqual(manager.purge('a')['status'], 'completed')
        self.assertEqual(manager.list_chats(trashed=True), [])
        self.assertTrue(self.memory.request_deleted('a'))
        self.assertEqual([m['request_id'] for m in self.memory.load_messages()], ['b', 'b'])
        self.assertFalse(self.history._record_path('a').exists())
        self.assertEqual(self.history._record_path('b').read_bytes(), neighbor)
        self.assertIsNone(ActivityHistory(self.history.path).begin('a', '晚到重放'))
        with self.assertRaises(ValueError):
            self.memory.append('assistant', '晚到结果', request_id='a')
        self.assertFalse(any(row['task_id'] for row in self.store.management.messages()))

    def test_interrupted_history_cleanup_remains_durable_and_retries_after_restart(self):
        self.chat('retry')
        manager = self.manager()
        manager.trash('retry')
        manager.request_purge('retry')
        from unittest.mock import patch
        with patch.object(self.history, 'delete_request', side_effect=OSError('暂不可写')):
            self.assertEqual(manager.purge('retry')['status'], 'error')
        restarted = self.manager()
        self.assertEqual(restarted.pending_purges(), ['retry'])
        self.assertEqual(restarted.request_purge('retry')['status'], 'queued')
        self.assertEqual(restarted.purge('retry')['status'], 'completed')
        self.assertEqual(restarted.pending_purges(), [])
        self.assertFalse(self.history._record_path('retry').exists())

    def test_restart_repairs_crash_between_durable_trash_and_message_hiding(self):
        self.chat('interrupted-trash')
        manager = self.manager()
        from unittest.mock import patch
        with patch.object(self.memory, 'hide_request', side_effect=OSError('模拟中断')):
            with self.assertRaises(OSError):
                manager.trash('interrupted-trash')
        self.assertEqual(len(self.memory.load_messages()), 2)
        self.manager()
        self.assertEqual(self.memory.load_messages(), [])

    def test_lifecycle_change_between_cleanup_stages_blocks_old_operation(self):
        self.chat('versioned')
        manager = self.manager()
        manager.trash('versioned')
        manager.request_purge('versioned')
        from unittest.mock import patch
        original = self.memory.delete_request_messages
        def change_version(request_id):
            original(request_id)
            with self.engine._transaction():
                self.engine._db.execute("UPDATE workflow_chat_lifecycle SET operation_id='replacement' WHERE request_id=?", (request_id,))
        with patch.object(self.memory, 'delete_request_messages', change_version):
            result=manager.purge('versioned')
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(self.history._record_path('versioned').exists())
        row=self.store.management.messages()[0]
        self.assertEqual(row['text'], '相同的原话')


if __name__ == '__main__':
    unittest.main()

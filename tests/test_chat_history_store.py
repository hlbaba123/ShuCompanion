from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from shu.activity_history import ActivityHistory


class ChatHistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'activity-history.json'
        self.history = ActivityHistory(self.path)

    def test_delete_request_preserves_neighbor_and_fences_stale_callbacks(self):
        self.history.begin('chat', '普通聊天')
        self.history.begin('neighbor', '普通聊天')
        stale = ActivityHistory(self.path)
        delete = getattr(self.history, 'delete_request', None)
        self.assertTrue(callable(delete), 'request cleanup must own a separate namespace')
        self.assertEqual(delete('chat'), 1)
        self.assertFalse(stale.append('chat', {'stage': 'notice', 'summary': '晚到'}))
        self.assertFalse(stale.finish('chat', 'completed'))
        self.assertIsNone(stale.begin('chat', '重放'))
        self.assertEqual([r['request_id'] for r in ActivityHistory(self.path).records()], ['neighbor'])
        self.assertFalse(self.history._record_path('chat').exists())
        self.assertTrue(self.history._record_path('neighbor').exists())

    def test_chat_deletion_rejects_bound_task_even_outside_recent_index(self):
        self.history.begin('owned', '任务')
        self.history.bind_request('owned', 'task')
        for i in range(101):
            self.history.begin('new-' + str(i), '其他')
        delete = getattr(self.history, 'delete_request', None)
        self.assertTrue(callable(delete), 'task records must never use request cleanup')
        before = self.history._record_path('owned').read_bytes()
        with self.assertRaises(ValueError):
            delete('owned')
        self.assertEqual(self.history._record_path('owned').read_bytes(), before)

    def test_load_explicit_known_request_after_recent_index_rolls_over(self):
        self.history.begin('old-chat', '较早聊天')
        for i in range(101):
            self.history.begin('new-' + str(i), '其他')
        restarted = ActivityHistory(self.path)
        load = getattr(restarted, 'load_request', None)
        self.assertTrue(callable(load), 'durable request lifecycle must recover exact old display record')
        self.assertEqual(load('old-chat')['text'], '较早聊天')
        self.assertIsNone(load('../unknown'))
        self.assertEqual(len(restarted.records()), 100)


if __name__ == '__main__':
    unittest.main()

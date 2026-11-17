"""One cleanup owner handles chat and task namespaces without GUI/network IO."""
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from shu.activity_history import ActivityHistory
from shu.core import ActionRejected, TaskEngine
from shu.memory import ConversationMemory
from shu.task_dialogue import TaskDialogue
from shu.workflow_store import WorkflowStore


class ChatHistoryDialogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.path = Path(self.temp.name)
        self.engine = TaskEngine(self.path)
        self.store = WorkflowStore(self.engine)
        self.memory = ConversationMemory(self.path)
        self.history = ActivityHistory(self.path / 'activity-history.json')
        self.progress = []
        self.c = SimpleNamespace(memory=self.memory, task_history=self.history,
            changed=SimpleNamespace(emit=lambda: None),
            cleanup_progress=SimpleNamespace(emit=self.progress.append),
            execution=SimpleNamespace(store=self.store), _task_id=None, _closed=False)
        self.dialogue = TaskDialogue(self.c)
        self.store.management.accept_message('chat', '普通聊天', mode='chat')
        self.store.management.finish_message('chat', relation='chat')
        self.memory.append('user', '普通聊天', request_id='chat')
        self.history.begin('chat', '普通聊天')

    def tearDown(self):
        self.dialogue.close()
        self.memory.close()
        self.engine.close()
        self.temp.cleanup()

    def test_state_manages_ordinary_chat_and_blocks_replay_of_trashed_request(self):
        self.assertIn('chats', self.dialogue.state(), 'history state must expose chat lifecycle')
        self.dialogue.trash_chat('chat')
        self.assertEqual(self.dialogue.state()['chats'], [])
        self.assertEqual(self.dialogue.state()['trashed_chats'][0]['request_id'], 'chat')
        with self.assertRaises(ActionRejected):
            self.dialogue.accept('chat', '普通聊天')
        self.dialogue.restore_chat('chat')
        self.assertEqual(self.dialogue.state()['chats'][0]['request_id'], 'chat')

    def test_slow_chat_cleanup_is_submitted_to_owned_worker_and_shutdown_joins_it(self):
        start = getattr(self.dialogue, 'start_chat_purge', None)
        self.assertTrue(callable(start), 'ordinary-chat deletion requires the existing background owner')
        self.dialogue.trash_chat('chat')
        entered, release = threading.Event(), threading.Event()
        original = self.history.delete_request
        def slow(request_id):
            entered.set()
            release.wait(2)
            return original(request_id)
        with patch.object(self.history, 'delete_request', slow):
            before = time.monotonic()
            self.assertEqual(start('chat')['status'], 'queued')
            self.assertLess(time.monotonic() - before, .15)
            self.assertTrue(entered.wait(1))
            self.assertEqual(start('chat')['status'], 'queued')
            before = time.monotonic()
            self.store.management.list_tasks()
            self.assertLess(time.monotonic() - before, .08)
            closer = threading.Thread(target=self.dialogue.close)
            closer.start()
            time.sleep(.02)
            self.assertTrue(closer.is_alive())
            release.set()
            closer.join(2)
            self.assertFalse(closer.is_alive())
        self.assertFalse(any(t.name == 'shu-task-cleanup' and t.is_alive() for t in threading.enumerate()))
        self.assertTrue(self.memory.request_deleted('chat'))
        self.assertTrue(any(p.get('request_id') == 'chat' for p in self.progress))

    def test_restart_resumes_chat_purge_after_stop_without_scheduling_a_task(self):
        start = getattr(self.dialogue, 'start_chat_purge', None)
        self.assertTrue(callable(start), 'request cleanup must survive application exit')
        self.dialogue.trash_chat('chat')
        self.dialogue._cleanup_stop.set()
        start('chat')
        self.dialogue.close()
        self.dialogue = TaskDialogue(self.c)
        self.dialogue.resume_cleanup()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not self.memory.request_deleted('chat'):
            time.sleep(.005)
        self.dialogue.close()
        self.assertTrue(self.memory.request_deleted('chat'))
        self.assertEqual(self.store.pending_events(), [])
        self.assertEqual(self.store.management.list_tasks(), [])

    def test_restart_cleans_display_file_before_a_shell_attaches_the_history_view(self):
        self.assertTrue(callable(getattr(self.dialogue, 'start_chat_purge', None)))
        self.dialogue.trash_chat('chat')
        self.dialogue.chats.request_purge('chat')
        self.dialogue.close()
        del self.c.task_history
        self.c.data_dir=self.path
        self.dialogue=TaskDialogue(self.c)
        self.dialogue.resume_cleanup()
        deadline=time.monotonic()+2
        while time.monotonic()<deadline and self.history._record_path('chat').exists():
            time.sleep(.005)
        self.dialogue.close()
        self.assertFalse(self.history._record_path('chat').exists(), 'restart cleanup must not depend on an attached Qt view')


if __name__ == '__main__':
    unittest.main()

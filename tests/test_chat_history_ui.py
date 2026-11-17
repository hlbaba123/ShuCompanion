import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication
from shu.activity_ui import ActivityPanel


class ChatHistoryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not isinstance(cls.app, QApplication):
            raise unittest.SkipTest('Requires an isolated GUI process')

    def setUp(self):
        self.temp = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.panel = ActivityPanel()
        self.panel.configure_history(Path(self.temp.name) / 'activity-history.json')
        self.panel.begin_request('same', '普通聊天原话')
        self.panel.finish_request('same', 'completed', '普通聊天回复')

    def tearDown(self):
        self.panel.shutdown()
        self.panel.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def state(self):
        return {'tasks': [{'id': 'same', 'goal': '独立任务', 'status': 'paused'}],
            'trashed': [], 'requests': [{'request_id': 'same', 'task_id': None, 'text': '普通聊天原话'}],
            'chats': [{'request_id': 'same', 'text': '普通聊天原话', 'status': 'applied', 'kind': 'chat', 'lifecycle': 'active'}],
            'trashed_chats': [], 'input_target': {'mode': 'task', 'task_id': 'same'}}

    def test_chat_action_uses_request_namespace_even_if_a_task_has_the_same_id(self):
        state = self.state()
        self.panel.set_management_state(state)
        self.panel.select_request('same')
        self.assertTrue(self.panel.delete_button.isEnabled(), 'ordinary chat history must be manageable')
        self.assertTrue(self.panel.continue_button.isHidden())
        self.assertTrue(self.panel.answer_button.isHidden())
        chat = QSignalSpy(self.panel.chat_trash_requested)
        task = QSignalSpy(self.panel.trash_requested)
        self.panel.delete_button.click()
        self.assertEqual(chat.at(0), ['same'])
        self.assertEqual(task.count(), 0)
        self.assertEqual(state['input_target'], {'mode': 'task', 'task_id': 'same'})

    def test_recycle_shows_chat_restore_and_purge_without_task_controls(self):
        state = self.state()
        state['trashed_chats'] = [{**state['chats'][0], 'lifecycle': 'trashed'}]
        state['chats'] = []
        self.panel.set_management_state(state)
        self.panel.recycle_button.click()
        self.assertEqual(self.panel.history_list.count(), 1, 'recycle must include request-owned records')
        self.panel.history_list.setCurrentRow(0)
        self.assertEqual(self.panel.selected_request_id, 'same')
        self.assertIsNone(self.panel.selected_task_id)
        restore = QSignalSpy(self.panel.chat_restore_requested)
        purge = QSignalSpy(self.panel.chat_purge_requested)
        task = QSignalSpy(self.panel.purge_requested)
        self.panel.restore_button.click()
        self.assertEqual(restore.at(0), ['same'])
        self.panel.purge_button.click()
        self.assertEqual(purge.count(), 0)
        self.assertIn('普通聊天', self.panel.confirm_label.text())
        self.panel.confirm_button.click()
        self.assertEqual(purge.at(0), ['same'])
        self.assertEqual(task.count(), 0)

    def test_workflow_chat_without_display_file_still_renders_and_can_be_managed(self):
        state = self.state()
        state['chats'] = [{'request_id': 'durable', 'text': '持久聊天原话', 'summary': '持久回复',
                           'status': 'applied', 'kind': 'chat', 'lifecycle': 'active'}]
        self.panel.set_management_state(state)
        self.assertTrue(self.panel.select_request('durable'), 'workflow request binding is enough to review ordinary chat')
        self.assertIn('持久聊天原话', self.panel.goal.text())
        self.assertIn('持久回复', self.panel.summary.text())
        self.assertTrue(self.panel.delete_button.isEnabled())

    def test_request_progress_matches_only_the_selected_request(self):
        state = self.state()
        self.panel.set_management_state(state)
        self.panel.select_request('same')
        before = self.panel.history_hint.text()
        self.panel.set_cleanup_progress({'namespace': 'request', 'request_id': 'other', 'stage': 'error'})
        self.assertEqual(self.panel.history_hint.text(), before, 'other request cleanup must not overwrite selected history')


if __name__ == '__main__':
    unittest.main()

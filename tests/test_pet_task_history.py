import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
from PySide6.QtWidgets import QApplication
from shu.pet import PetConversation


class PetTaskHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        if not isinstance(cls.app,QApplication):
            raise unittest.SkipTest('Run pet history GUI tests in a separate QApplication process')

    def test_select_and_refresh_preserve_task_filter_without_mixing_chat(self):
        chat=PetConversation()
        try:
            records=[{'role':'user','content':'任务A原话','task_id':'A','request_id':'a'},
                     {'role':'assistant','content':'任务A答案','task_id':'A','request_id':'a'},
                     {'role':'user','content':'任务B原话','task_id':'B','request_id':'b'},
                     {'role':'assistant','content':'任务B答案','task_id':'B','request_id':'b'}]
            chat._history_loader=lambda:records
            chat._reload_history()
            for i in range(chat.history_filter.count()):
                if chat.history_filter.itemData(i)==('task','A'):chat.history_filter.setCurrentIndex(i)
            self.assertIn('任务A答案',chat.transcript.toPlainText())
            self.assertNotIn('任务B',chat.transcript.toPlainText())
            chat._reload_history()
            self.assertEqual(chat.history_filter.currentData(),('task','A'))
            self.assertNotIn('任务B',chat.transcript.toPlainText())
        finally:chat.history.close();chat.close()

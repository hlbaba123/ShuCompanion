"""Only explicit repetitions of the current job outputs bypass model routing."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, PropertyMock, patch

from PySide6.QtCore import QCoreApplication
from shu.controller import SessionController


APP=QCoreApplication.instance() or QCoreApplication([])


class DeliveryFollowupRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.c=SessionController(self.root,self.root/'data',key_path=self.root/'unread-key.txt')
        self.addCleanup(self.c.close)
        self.contract={'goal':'查找SL-1作业','current_deliverable':'copilot_codes','task_spec':{'kind':'lookup','stage_code':'SL-1'}}
        state=self.c.execution.store.submit(self.contract['goal'],self.contract)
        self.c._task_id=state['task_id'];self.c.execution.current_task_id=state['task_id']
        self.c.execution.research_output=Mock(return_value={'summary':'真实已登记的SL-1作业结果'})
        self.messages=[];self.c.message.connect(lambda role,text:self.messages.append((role,text)))
        start_patch=patch.object(self.c,'_start');self.start=start_patch.start();self.addCleanup(start_patch.stop)
        key_patch=patch.object(type(self.c),'has_key',new_callable=PropertyMock,return_value=True)
        key_patch.start();self.addCleanup(key_patch.stop)

    def test_new_or_previous_named_stage_goes_to_model_routing(self):
        for text in ('给出之前TL-6查到的神秘代码','把TL-2的作业码列出来','换个活动给我作业码',
                     '给出覆潮之下的作业码'):
            with self.subTest(text=text):
                self.start.reset_mock();self.c.execution.research_output.reset_mock();self.messages.clear()
                self.c.submit(text)
                self.start.assert_called_once()
                self.c.execution.research_output.assert_not_called()
                self.assertEqual(self.messages,[('user',text)])

    def test_knowledge_explanations_and_ordinary_references_go_to_model(self):
        for text in ('神秘代码是什么意思','你刚才说过的话是什么意思',
                     '给我解释一下刚才那份JSON怎么导入','刚才的结果为什么不适合我'):
            with self.subTest(text=text):
                self.start.reset_mock();self.c.execution.research_output.reset_mock();self.messages.clear()
                self.c.submit(text)
                self.start.assert_called_once()
                self.c.execution.research_output.assert_not_called()
                self.assertEqual(self.messages,[('user',text)])

    def test_explicit_current_job_repetition_uses_only_registered_delivery(self):
        for text in ('把你刚才查到的神秘代码列出来','给出SL-1查到的作业码','再发一下刚才的JSON'):
            with self.subTest(text=text):
                self.start.reset_mock();self.c.execution.research_output.reset_mock();self.messages.clear()
                self.c.submit(text)
                self.start.assert_not_called()
                self.c.execution.research_output.assert_called_once_with(self.c._task_id)
                self.assertEqual(self.messages,[('user',text),('assistant','真实已登记的SL-1作业结果')])

    def test_forbidden_research_does_not_turn_a_repetition_into_a_new_query(self):
        text='把你刚才查到并核对过的神秘代码再列给我，不要重新搜索。'
        self.c.submit(text)
        self.start.assert_not_called()
        self.c.execution.research_output.assert_called_once_with(self.c._task_id)
        self.assertEqual(self.messages[-1],('assistant','真实已登记的SL-1作业结果'))

    def test_explicit_positive_search_still_routes_even_after_a_negative_phrase(self):
        for text in ('把你刚才查到的神秘代码重新搜索后列给我',
                     '不要重新搜索网页，但请重新查询作业库后列出代码'):
            with self.subTest(text=text):
                self.start.reset_mock();self.c.execution.research_output.reset_mock()
                self.c.submit(text)
                self.start.assert_called_once()
                self.c.execution.research_output.assert_not_called()


if __name__=='__main__':unittest.main()

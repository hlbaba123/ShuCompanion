"""Desktop chat reaches real tool orchestration; IO is fake and GUI is absent."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication

from shu.controller import SessionController
from shu.providers import ProviderConfig

APP = QCoreApplication.instance() or QCoreApplication([])


def call(name, **arguments):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call-' + name,
        'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}


class Model:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.config = ProviderConfig()
        self.last_metrics = {}
    def complete(self, messages, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        return self.responses.pop(0)


class Tools:
    calls = []
    def __init__(self, *_args, **_kwargs):
        pass
    def search(self, query):
        self.calls.append(query)
        return {'ok': True, 'tool': 'search', 'data': {'results': [{
            'title': '合成的游戏内活动公告', 'url': 'https://ak.hypergryph.com/news/fixture',
            'date': '2026-11-06', 'content': '测试用关卡阶段开放公告，不是线下展会'}]}}
    def fetch(self, url):
        return {'ok': True, 'tool': 'fetch', 'url': url, 'data': {'url': url, 'content': '测试公告正文'}}


class NetworkControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.controller = SessionController(self.root, self.root / 'data')
        self.controller.key_path.write_text('sk-offline-network-fixture')
        self.messages, self.notices, self.errors = [], [], []
        self.controller.message.connect(lambda role, text: self.messages.append((role, text)))
        self.controller.notice.connect(self.notices.append)
        self.controller.error.connect(self.errors.append)
        Tools.calls = []

    def tearDown(self):
        self.controller.close()
        self.temp.cleanup()

    def run_turn(self, model, text, mode=0):
        self.controller.client = self.controller.intent_client = model
        self.controller.persona = self.controller._new_persona()
        with patch('shu.controller.ResearchTools', Tools):
            self.controller.submit(text, mode)
            end = time.monotonic() + 3
            while self.controller.is_busy and time.monotonic() < end:
                APP.processEvents()
                time.sleep(.005)
            APP.processEvents()
        self.assertFalse(self.controller.is_busy)

    def test_live_question_searches_without_screen_or_input_permission(self):
        question = '现在明日方舟有什么能刷关的新活动？'
        answer = '已找到测试公告，属于游戏内作战活动。'
        model = Model([call('web_search', query='明日方舟 国服 当前 游戏内作战活动'),
            call('route_user_request', intent='chat', user_request=question, goal='', constraints=[],
                 completion_criteria=[], removed_constraints=[], reply=answer)])
        self.run_turn(model, question)
        self.assertEqual(len(Tools.calls), 1)
        self.assertTrue(any(answer in text for role, text in self.messages if role == 'assistant'))
        self.assertTrue(any('联网' in text or '检索' in text for text in self.notices))
        self.assertEqual(self.controller.last_network_report['sources'][0]['url'],
                         'https://ak.hypergryph.com/news/fixture')
        self.assertFalse(self.controller.execution.active)
        self.assertFalse(self.controller.engine.list_tasks())
        self.assertEqual(self.errors, [])

    def test_activity_surface_shows_intent_and_network_steps_without_task_creation(self):
        progress, network = [], []
        self.controller.activity_progress.connect(progress.append)
        self.controller.network_progress.connect(network.append)
        question = '查一下当前活动'
        model = Model([call('web_search', query='明日方舟活动'),
                       call('route_user_request', intent='chat', user_request=question, goal='',
                            constraints=[], completion_criteria=[], removed_constraints=[], reply='测试公告返回。')])
        self.run_turn(model, question)
        self.assertEqual([event['stage'] for event in progress],
                         ['intent_started', 'intent_finished'])
        self.assertEqual([event['role'] for event in progress], ['leader', 'leader'])
        self.assertEqual([event['stage'] for event in network], ['tool_started', 'tool_finished'])
        self.assertEqual([event['tool'] for event in network], ['web_search', 'web_search'])
        self.assertEqual(network[0]['query'], '明日方舟活动')
        self.assertFalse(self.controller.engine.list_tasks())
        self.assertTrue(all('result' not in event and 'response' not in event for event in progress))

    def test_explicit_chat_mode_can_search_but_cannot_execute_task(self):
        model = Model([call('web_search', query='游戏内作战活动 测试'),
                       {'role': 'assistant', 'content': '测试公告已找到。'}])
        self.run_turn(model, '查一下当前游戏活动', mode=1)
        self.assertEqual(len(Tools.calls), 1)
        self.assertFalse(self.controller.engine.list_tasks())
        self.assertEqual(self.errors, [])

    def test_chat_without_search_does_not_claim_any_network_result(self):
        model = Model([call('route_user_request', intent='chat', user_request='你好', goal='',
            constraints=[], completion_criteria=[], removed_constraints=[], reply='我在。')])
        self.run_turn(model, '你好')
        self.assertEqual(Tools.calls, [])
        self.assertEqual(self.controller.last_network_report['sources'], [])


if __name__ == '__main__':
    unittest.main()

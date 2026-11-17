"""Independent task-understanding and Leader thinking, without provider IO."""
import copy
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

from shu.controller import SessionController
from shu.providers import PersonaSession, ProviderConfig, ProviderError


APP = QCoreApplication.instance() or QCoreApplication([])


class ScriptedClient:
    def __init__(self, *responses, thinking=True, finish_reason='tool_calls'):
        self.config = ProviderConfig(thinking=thinking)
        self.responses = list(responses)
        self.requests = []
        self.last_finish_reason = finish_reason

    def complete(self, messages, **options):
        self.requests.append({'messages': copy.deepcopy(messages), **options})
        return copy.deepcopy(self.responses.pop(0))


def call(identifier, name, **arguments):
    return {'role': 'assistant', 'content': None, 'reasoning_content': 'offline reasoning fixture',
            'tool_calls': [{'id': identifier, 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(arguments)}}]}


class Tools:
    def __init__(self):
        self.calls = []

    def search(self, query):
        self.calls.append(query)
        return {'ok': True, 'data': {'results': []}}


class IndependentThinkingConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict('os.environ', {'DEEPSEEK_API_KEY': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def controller(self, settings=None, *, legacy=False):
        directory = self.root / ('legacy' if legacy else 'data')
        directory.mkdir(exist_ok=True)
        if settings is not None:
            (directory / 'settings.json').write_text(json.dumps(settings), encoding='utf-8')
        controller = SessionController(self.root, directory, legacy_tools=legacy)
        self.addCleanup(controller.close)
        return controller

    def test_default_understanding_and_leader_think_with_separate_clients(self):
        controller = self.controller()
        self.assertFalse(controller.config.thinking)
        self.assertFalse(controller._new_persona(routing=False).client.config.thinking)
        self.assertIs(controller._new_persona(routing=False).client, controller.client)
        self.assertIs(controller._new_persona().client, controller.intent_client)
        self.assertIsNot(controller.client, controller.intent_client)
        for config in (controller.intent_config, controller.leader_config):
            self.assertTrue(config.thinking)
            self.assertGreaterEqual(config.max_tokens, 8192)
            self.assertGreaterEqual(config.timeout_seconds, 120)
        self.assertIs(controller.execution.role_config(), controller.leader_config)
        self.assertIsNot(controller.intent_config, controller.leader_config)

    def test_flags_stay_independent_on_load_save_and_session_rebuild(self):
        controller = self.controller({'intent_thinking': True, 'leader_thinking': False,
                                      'game_thinking': False})
        self.assertTrue(controller.intent_config.thinking)
        self.assertFalse(controller.leader_config.thinking)
        errors = []
        controller.error.connect(errors.append)
        controller.save_settings('deepseek-flash', 'https://api.deepseek.com/v1')
        self.assertEqual(errors, [])
        self.assertTrue(controller.intent_client.config.thinking)
        self.assertFalse(controller.execution.role_config().thinking)
        self.assertFalse(controller.game_config.thinking)
        controller.save_settings('deepseek-flash', 'https://api.deepseek.com',
                                 intent_thinking=False, leader_thinking=True)
        self.assertEqual(errors, [])
        controller._rebuild_sessions()
        self.assertFalse(controller.intent_client.config.thinking)
        self.assertTrue(controller.execution.role_config().thinking)
        saved = json.loads((controller.data_dir / 'settings.json').read_text(encoding='utf-8'))
        self.assertIs(saved['intent_thinking'], False)
        self.assertIs(saved['leader_thinking'], True)
        controller.close()
        restored = self.controller()
        self.assertFalse(restored.intent_config.thinking)
        self.assertTrue(restored.leader_config.thinking)
        self.assertFalse(restored.config.thinking)

    def test_invalid_thinking_preferences_cannot_be_saved(self):
        controller = self.controller()
        errors = []
        controller.error.connect(errors.append)
        for options in ({'intent_thinking': 'true'}, {'leader_thinking': 1}):
            controller.save_settings('deepseek-flash', 'https://api.deepseek.com', **options)
        self.assertEqual(len(errors), 2)
        self.assertTrue(controller.intent_config.thinking)
        self.assertTrue(controller.leader_config.thinking)

    def test_legacy_persona_keeps_original_nonthinking_client(self):
        controller = self.controller(legacy=True)
        self.assertIs(controller._new_persona().client, controller.client)
        self.assertFalse(controller._new_persona().client.config.thinking)


class ThinkingToolProtocolTests(unittest.TestCase):
    def run_turn(self, client, text='查一下当前活动', **options):
        self.persona = PersonaSession(client)
        self.tools = Tools()
        return importlib.import_module('shu.network_dialogue').run_network_dialogue(
            self.persona, text, task_summary='offline', research_tools=self.tools,
            cancel_event=None, progress=None, guard=None, **options)

    def test_route_only_thinking_uses_auto_and_exposes_only_route_tool(self):
        route = call('route', 'route_user_request', intent='chat')
        client = ScriptedClient(route)
        result = self.run_turn(client, '查TL-6的作业')
        self.assertEqual(result['response'], route)
        self.assertEqual(client.requests[0]['tool_choice'], 'auto')
        self.assertEqual([tool['function']['name'] for tool in client.requests[0]['tools']],
                         ['route_user_request'])
        self.assertEqual(self.tools.calls, [])

    def test_thinking_search_continuation_preserves_full_reasoning_and_tool_result(self):
        search = call('search', 'web_search', query='合成活动查询')
        route = call('route', 'route_user_request', intent='chat')
        client = ScriptedClient(search, route)
        self.run_turn(client)
        self.assertTrue(all(request['tool_choice'] == 'auto' for request in client.requests))
        history = client.requests[1]['messages']
        self.assertEqual(next(row for row in history if row.get('tool_calls'))['reasoning_content'],
                         search['reasoning_content'])
        self.assertEqual(next(row for row in history if row['role'] == 'tool')['tool_call_id'], 'search')
        self.assertEqual(self.tools.calls, ['合成活动查询'])

    def test_truncated_tool_call_is_explicit_error_before_any_tool_is_executed(self):
        client = ScriptedClient(call('search', 'web_search', query='不得执行'), finish_reason='length')
        with self.assertRaisesRegex(ProviderError, '长度|截断'):
            self.run_turn(client)
        self.assertEqual(self.tools.calls, [])
        self.assertEqual(len(client.requests), 1)
        self.assertTrue(client.config.thinking)

    def test_truncated_route_cannot_be_reported_as_complete(self):
        client = ScriptedClient(call('route', 'route_user_request', intent='chat'), finish_reason='length')
        with self.assertRaisesRegex(ProviderError, '长度|截断'):
            self.run_turn(client, route_only=True)


if __name__ == '__main__':
    unittest.main()

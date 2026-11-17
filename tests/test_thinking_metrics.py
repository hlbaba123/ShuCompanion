"""Check thinking request evidence without recording model reasoning text."""
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from shu.providers import DeepSeekClient, ProviderConfig


class ThinkingMetricsTests(unittest.TestCase):
    def test_metrics_reflect_actual_request_mode_and_response_size(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                client = DeepSeekClient(ProviderConfig(api_key='test-key-only', thinking=enabled))
                reply = {'choices': [{'message': {'role': 'assistant', 'content': '已收到',
                         'reasoning_content': 'private reasoning fixture'}, 'finish_reason': 'stop'}],
                         'usage': {'completion_tokens_details': {'reasoning_tokens': 9}}}
                client._opener.open = Mock(return_value=io.BytesIO(json.dumps(reply).encode()))
                tools = [{'type':'function','function':{'name':'read_fixture',
                          'parameters':{'type':'object','properties':{}}}}]
                client.complete([{'role': 'user', 'content': '测试'}], tools=tools, tool_choice='auto')
                payload = json.loads(client._opener.open.call_args.args[0].data)
                self.assertEqual(payload['thinking']['type'], 'enabled' if enabled else 'disabled')
                self.assertIs(client.last_metrics['thinking_enabled'], enabled)
                self.assertEqual(client.last_metrics['reasoning_content_chars'], 25)
                self.assertEqual(client.last_metrics['tool_choice'], 'auto')
                self.assertNotIn('private reasoning fixture', json.dumps(client.last_metrics))

    def test_acceptance_counts_separate_intent_client_once(self):
        from shu.conversation_acceptance import _persona_clients
        persona, intent = object(), object()
        self.assertEqual(_persona_clients(SimpleNamespace(client=persona, intent_client=intent)),
                         [persona, intent])
        self.assertEqual(_persona_clients(SimpleNamespace(client=persona, intent_client=persona)),
                         [persona])
        self.assertEqual(_persona_clients(SimpleNamespace(client=persona)), [persona])


if __name__ == '__main__':
    unittest.main()

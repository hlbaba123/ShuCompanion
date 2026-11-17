import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from shu.maa_agents import MaaRoleAgents
from shu.providers import ProviderConfig


class VisionAgentIntegrationTests(unittest.TestCase):
    def test_visual_observer_has_independent_client_and_reports_parent_task(self):
        agent = MaaRoleAgents.__new__(MaaRoleAgents)
        agent.directory = Path('unused-test-observation-directory')
        parent = SimpleNamespace(config=ProviderConfig(model='deepseek-v4-pro'))
        agent.clients = {'maa': parent}
        agent._progress = Mock()
        agent.metrics_callback = Mock()
        context = {'task_id': 'task', 'dispatch_id': 'dispatch'}
        client = SimpleNamespace(config=None)
        with patch('shu.maa_vision_readback.DeepSeekClient', return_value=client) as factory, \
             patch('shu.maa_vision_readback.MaaVisionReadback') as readback:
            agent._import_vision_readback(context, None)
            config = factory.call_args.args[0]
            self.assertEqual(config.model, 'deepseek-flash')
            self.assertFalse(config.thinking)
            self.assertIsNot(agent.clients['vision'], parent)
            args = readback.call_args.kwargs
            args['metrics_callback']({'usage': {'total_tokens': 5}})
            agent.metrics_callback.assert_called_once_with('vision', {'usage': {'total_tokens': 5}}, 'task')
            args['progress_callback']({'phase': 'started'})
            self.assertEqual(agent._progress.call_args.args[:2], ('tool_started', 'vision'))


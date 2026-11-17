"""Only screenshot-bound MAA running, idle, or unrecognized states are accepted."""
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from shu import maa_battle_vision
from shu.providers import ProviderCancelled, ProviderConfig
from test_battle_vision import FakeClient, maa, png, reply


TOOL = 'inspect_maa_state'
BOX = {'x': .19, 'y': .16, 'width': .15, 'height': .06}
RUNNING = {'page': 'copilot', 'running': True, 'start_box': None,
           'stop_box': BOX, 'evidence': '自动战斗页显示停止按钮。'}
IDLE = {'page': 'copilot', 'running': False, 'start_box': BOX,
        'stop_box': None, 'evidence': '自动战斗页显示开始按钮。'}
UNKNOWN = {'page': 'other', 'running': False, 'start_box': None,
           'stop_box': None, 'evidence': '当前画面看不清自动战斗控件。'}


class BattleMonitorVisionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        image = Path(folder.name) / 'maa.png'
        image.write_bytes(png())
        self.frame = {'image_path': str(image), 'screenshot_id': 'background-maa-frame',
                      'image_sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
                      'window': {'hwnd': 7, 'pid': 11, 'creation_time': 13, 'foreground': False,
                                 'client_rect': {'x': 20, 'y': 30, 'width': 320, 'height': 180}}}
        self.clients = []
        self.responses = []
        self.after = None

        def client_factory(config):
            client = FakeClient(config)
            client.responses = self.responses
            client.after = lambda: self.after() if self.after else None
            self.clients.append(client)
            return client

        patcher = patch.object(maa_battle_vision, 'DeepSeekClient', side_effect=client_factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.vision = maa_battle_vision.BattleVision(ProviderConfig(timeout_seconds=45))

    def inspect(self, value, **kwargs):
        frame = kwargs.pop('frame', self.frame)
        self.responses.append(reply(TOOL, value))
        method = getattr(self.vision, TOOL, None)
        self.assertTrue(callable(method), 'MAA monitoring needs a state-only inspection method')
        return method(frame, **kwargs)

    def test_running_idle_and_unknown_each_use_one_new_frame_bound_read_only_tool(self):
        for value in (RUNNING, IDLE, UNKNOWN):
            with self.subTest(value=value):
                result = self.inspect(value)
                self.assertEqual(result, {**value, 'screenshot_id': self.frame['screenshot_id'],
                                          'image_sha256': self.frame['image_sha256']})
                self.assertNotIn('reasoning_content', result)
        calls = [call for client in self.clients for call in client.calls]
        self.assertEqual(len(calls), 3)
        for messages, kwargs in calls:
            self.assertEqual(len(kwargs['tools']), 1)
            function = kwargs['tools'][0]['function']
            self.assertEqual(function['name'], TOOL)
            self.assertEqual(set(function['parameters']['required']), set(RUNNING))
            self.assertFalse(function['parameters']['additionalProperties'])
            self.assertNotEqual(kwargs.get('tool_choice'), 'required')
            self.assertTrue(any(block['type'] == 'image_url' for block in messages[-1]['content']))

    def test_monitor_budget_does_not_change_full_prestart_client(self):
        original = self.vision.client
        self.responses.append(reply('inspect_maa_battle', maa()))
        self.vision.inspect_maa(self.frame)
        self.inspect(RUNNING)
        self.assertIs(self.vision.client, original)
        self.assertEqual(original.config.timeout_seconds, 45)
        self.assertEqual(len(original.calls), 1)
        monitor, = [client for client in self.clients if client is not original and client.calls]
        self.assertLessEqual(monitor.config.timeout_seconds, 12)
        self.assertEqual(monitor.TRANSIENT_RETRIES, 0)

    def test_missing_wrong_or_ambiguous_state_cannot_grant_running_or_idle_evidence(self):
        for value in ({**RUNNING, 'start_box': BOX}, {**RUNNING, 'stop_box': None},
                      {**RUNNING, 'running': 1}, {**RUNNING, 'action': 'click'},
                      {**IDLE, 'start_box': None}, {**IDLE, 'stop_box': BOX},
                      {**UNKNOWN, 'running': True}, {**UNKNOWN, 'start_box': BOX},
                      {**RUNNING, 'stop_box': {**BOX, 'x': .99}},
                      {**IDLE, 'start_box': {**BOX, 'x': False}},
                      {key: field for key, field in RUNNING.items() if key != 'evidence'}):
            with self.subTest(value=value), self.assertRaises(maa_battle_vision.BattleVisionError) as rejected:
                self.inspect(value)
            error = rejected.exception
            self.assertEqual(error.code, 'battle_vision_invalid_output')
            self.assertEqual(error.evidence['screenshot_id'], self.frame['screenshot_id'])
            self.assertEqual(error.evidence['expected_tool'], TOOL)
            self.assertTrue(error.evidence['validation'])
            self.assertNotIn('PRIVATE REASONING', str(error.evidence))

    def test_invalid_frame_hash_and_geometry_stop_before_any_model_request(self):
        wrong_geometry = {**self.frame['window'], 'client_rect': {'width': 640, 'height': 360}}
        for changes in ({'image_sha256': '0' * 64}, {'window': wrong_geometry}):
            with self.subTest(changes=changes), self.assertRaises(maa_battle_vision.BattleVisionError) as rejected:
                self.inspect(RUNNING, frame={**self.frame, **changes})
            self.assertEqual(rejected.exception.code, 'battle_vision_invalid_input')
        self.assertFalse(any(client.calls for client in self.clients))

    def test_cancelled_model_response_is_not_a_valid_observation(self):
        cancel = threading.Event()
        self.after = cancel.set
        with self.assertRaises(ProviderCancelled):
            self.inspect(RUNNING, cancel_event=cancel)


if __name__ == '__main__':
    unittest.main()

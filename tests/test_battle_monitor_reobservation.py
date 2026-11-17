"""Transient read-only monitor failures must not replay a battle start."""
import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from shu.core import ActionRejected
from shu.maa_battle import BattleRunner
from shu.maa_battle_vision import BattleVisionError


class MonitorReobservationTests(unittest.TestCase):
    def runner(self, answers):
        runner = BattleRunner.__new__(BattleRunner)
        runner.cancel = threading.Event()
        runner.cancel.wait = Mock(return_value=False)
        runner.guard = Mock()
        runner.input = Mock()
        runner.progress = Mock()
        runner.start_operation = 'the-only-start'
        runner.snapshot = Mock(side_effect=[{'screenshot_id': str(i),
            'image_sha256': str(i)*64} for i in (1, 2, 3, 4)])
        runner._state = Mock(side_effect=answers)
        runner._observation = Mock(side_effect=lambda f, s: {'path': f['screenshot_id']+'.json'})
        self.published = []
        runner.publish = lambda kind, payload: self.published.append((kind, copy.deepcopy(payload)))
        return runner

    def invalid(self):
        error = BattleVisionError('battle_vision_invalid_output', 'invalid readonly response')
        error.evidence = {'validation': 'running_button_conflict'}
        return error

    def test_bad_then_valid_state_uses_new_frame_audits_failure_and_never_clicks(self):
        runner = self.runner([self.invalid(), {'running': True, 'idle': False}])
        frame, state, ref = runner._monitor_observation()
        self.assertEqual(frame['screenshot_id'], '2')
        self.assertTrue(state['running'])
        self.assertEqual(state['reobservation_attempts'], 1)
        self.assertEqual(ref['path'], '2.json')
        self.assertEqual([c.args[0]['screenshot_id'] for c in runner._state.call_args_list], ['1', '2'])
        self.assertEqual(len(self.published), 1)
        rejected = self.published[0][1]
        self.assertEqual(rejected['frame']['screenshot_id'], '1')
        self.assertEqual(rejected['diagnostic']['validation'], 'running_button_conflict')
        runner.input.assert_not_called()

    def test_three_invalid_outputs_exhaust_budget_without_a_fourth_frame_or_input(self):
        runner = self.runner([self.invalid() for _ in range(3)])
        with self.assertRaises(BattleVisionError):
            runner._monitor_observation()
        self.assertEqual(runner.snapshot.call_count, 3)
        self.assertEqual(len(self.published), 3)
        runner._observation.assert_not_called()
        runner.input.assert_not_called()

    def test_invalid_input_provider_and_unknown_errors_do_not_enter_format_retry(self):
        for error in (BattleVisionError('battle_vision_invalid_input', 'bad frame'),
                      RuntimeError('provider unavailable'), ActionRejected('cancelled')):
            with self.subTest(error=type(error).__name__):
                runner = self.runner([error])
                with self.assertRaises(type(error)):
                    runner._monitor_observation()
                self.assertEqual(runner.snapshot.call_count, 1)
                runner.input.assert_not_called()

    def test_cancellation_during_backoff_stops_without_another_capture(self):
        runner = self.runner([self.invalid()])
        runner.cancel.wait.side_effect = lambda delay: runner.cancel.set() or True
        def guard():
            if runner.cancel.is_set():
                raise ActionRejected('cancelled')
        runner.guard.side_effect = guard
        with self.assertRaises(ActionRejected):
            runner._monitor_observation()
        self.assertEqual(runner.snapshot.call_count, 1)
        runner.input.assert_not_called()

    def test_valid_local_state_has_no_retry(self):
        runner = self.runner([{'running': False, 'idle': True, 'local': True}])
        _, state, _ = runner._monitor_observation()
        self.assertTrue(state['idle'])
        self.assertEqual(state['reobservation_attempts'], 0)
        runner.cancel.wait.assert_not_called()

    def test_model_monitor_uses_state_only_tool_and_local_is_bound_to_exact_frame(self):
        runner = self.runner([])
        runner._state = BattleRunner._state.__get__(runner, BattleRunner)
        runner.stop_deadline = None
        runner._local = Mock(return_value={'ok': True, 'page': 'copilot',
            'running': True, 'idle': False, 'image_sha256': 'old-frame'})
        runner.vision = SimpleNamespace(inspect_maa_state=Mock(return_value={
            'page': 'copilot', 'running': True, 'start_box': None, 'stop_box': None}),
            inspect_maa=Mock(side_effect=AssertionError('full preview is not a monitor input')))
        state = runner._state({'image_sha256': 'new-frame'})
        self.assertTrue(state['running'])
        self.assertNotIn('local', state)
        runner.vision.inspect_maa_state.assert_called_once()


if __name__ == '__main__':
    unittest.main()

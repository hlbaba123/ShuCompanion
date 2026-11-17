"""Launch only once, then verify a real unique window before binding."""
import unittest
from unittest.mock import Mock

from shu.battle_windows import wait_for_window, allow_maa_startup


class WindowStartupTests(unittest.TestCase):
    def wait(self, observations, *, running=False, guard=None):
        launch = Mock()
        self.launch = launch
        return wait_for_window(
            'game', Mock(side_effect=observations), guard or Mock(), running=Mock(return_value=running),
            launch=launch, clock=Mock(side_effect=range(100)), sleep=lambda _: None, timeout=8)

    def test_cold_process_launches_once_while_window_loads(self):
        self.assertEqual(self.wait([[], [], [], [123]]), 123)
        self.launch.assert_called_once_with('game')

    def test_already_loading_waits_without_second_game(self):
        self.assertEqual(self.wait([[], [], [123]], running=True), 123)
        self.launch.assert_not_called()

    def test_open_or_ambiguous_window_never_launches_another(self):
        self.assertEqual(self.wait([[123]]), 123)
        self.launch.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, '多个'):
            self.wait([[123, 456]])
        self.launch.assert_not_called()

    def test_permission_revoked_while_checking_process_prevents_launch(self):
        with self.assertRaisesRegex(RuntimeError, 'revoked'):
            self.wait([[]], guard=Mock(side_effect=[None, RuntimeError('revoked')]))
        self.launch.assert_not_called()

    def test_automatic_task_setting_cannot_run_whole_queue_on_cold_start(self):
        def config(value):
            return {'Current':'a', 'Configurations':{'a':{'Gui':{'StartUpSettings':{'RunDirectly':value}}}}}
        for value in (True, None, 0, 'false'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                allow_maa_startup(config(value))
        allow_maa_startup(config(False))

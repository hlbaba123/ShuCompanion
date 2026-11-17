"""Focus denial is a known focus result, never proof about game content."""
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock,patch

from shu.maa_battle_native import _GameBackend
from shu.windows import WindowError


class KnownFocusDenialTests(unittest.TestCase):
    def backend(self, error):
        backend=_GameBackend.__new__(_GameBackend)
        backend._check=MagicMock()
        backend.gate=MagicMock()
        backend.native=SimpleNamespace(focus_guarded=MagicMock(side_effect=error))
        backend.native.native=SimpleNamespace()
        backend.activation_error=error
        backend.focus_result={}
        return backend

    def test_native_known_focus_denial_returns_without_content_input_unknown(self):
        backend=self.backend(WindowError('foreground not acquired','focus_denied'))
        with patch('shu.game_foreground_activation.activate_game',side_effect=backend.activation_error):
            backend.focus_window(123)
        self.assertIs(backend.focus_result['focus_acquired'],False)
        self.assertEqual(backend.focus_result['focus_method'],'known_focus_denied')
        backend.gate.assert_called_once()

    def test_after_input_or_partial_focus_failures_remain_unknown(self):
        for error in (WindowError('click unknown','focus_after_input_unknown',{'input_sent':True}),
                      WindowError('partial','partial_mouse_input',{'partial_input':True}),
                      WindowError('denied after key','focus_denied',{'input_sent':True})):
            with self.subTest(code=error.code):
                backend=self.backend(error)
                with patch('shu.game_foreground_activation.activate_game',side_effect=error):
                    with self.assertRaises(WindowError):backend.focus_window(123)

    def test_revocation_is_not_converted_to_focus_denial(self):
        backend=self.backend(None)
        backend.gate.side_effect=RuntimeError('revoked')
        with self.assertRaises(RuntimeError):backend.focus_window(123)
        backend.native.focus_guarded.assert_not_called()

    def test_activation_api_failure_without_content_input_is_known_false(self):
        backend=self.backend(WindowError('API failure','focus_activation_failed',{'input_sent':False}))
        with patch('shu.game_foreground_activation.activate_game',side_effect=backend.activation_error):
            backend.focus_window(123)
        self.assertIs(backend.focus_result['focus_acquired'],False)

    def test_cleanup_failure_is_not_relabelled_as_known_focus_denial(self):
        error=WindowError('topmost cleanup failed','focus_activation_cleanup_failed',{'input_sent':False})
        backend=self.backend(error)
        with patch('shu.game_foreground_activation.activate_game',side_effect=error),self.assertRaises(WindowError):
            backend.focus_window(123)

    def test_activation_failure_requires_explicit_no_content_input_proof(self):
        for details in ({},{'input_sent':True},{'input_sent':False,'partial_input':True}):
            error=WindowError('API failure','focus_activation_failed',details)
            backend=self.backend(error)
            with self.subTest(details=details),patch('shu.game_foreground_activation.activate_game',side_effect=error),self.assertRaises(WindowError):
                backend.focus_window(123)


if __name__=='__main__':unittest.main()

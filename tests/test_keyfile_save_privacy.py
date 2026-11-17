"""Exercise the real Key-save action using synthetic credentials only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QLineEdit
from shu.providers import ProviderConfig
from shu.settings_ui import PetSettings


class KeyFileSavePrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not isinstance(cls.app, QApplication):
            raise unittest.SkipTest('Key save UI requires QApplication')

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.destination = self.root / 'DeepSeek_API_Key.txt'
        self.destination.write_text('previous-synthetic-fixture-key\n', encoding='utf-8')
        self.key = 'synthetic-save-fixture-not-a-real-credential'
        config = ProviderConfig(model='test-model', base_url='https://example.invalid')
        self.subject = SimpleNamespace(
            c=SimpleNamespace(key_path=self.destination, config=config, game_config=config,
                _validate_settings=lambda *_: None, save_settings=lambda *_, **__: None),
            key=QLineEdit(self.key), role_model=QLineEdit(config.model),
            game_model=QLineEdit(config.model), url=QLineEdit(config.base_url),
            thinking=QCheckBox(), feedback=QLabel())

    def tearDown(self):
        self.temp.cleanup()

    def test_replace_failure_keeps_original_key_and_removes_new_secret_file(self):
        before = self.destination.read_bytes()
        with patch.object(Path, 'replace', side_effect=PermissionError('synthetic locked target')):
            PetSettings.save(self.subject)
        self.assertEqual(self.destination.read_bytes(), before)
        self.assertEqual(set(self.root.iterdir()), {self.destination})
        self.assertNotIn(self.key.encode(), self.destination.read_bytes())
        self.assertIn('未能保存', self.subject.feedback.text())

    def test_failed_save_never_overwrites_or_deletes_a_preexisting_temp_file(self):
        existing = self.root / 'DeepSeek_API_Key.txt.tmp'
        existing.write_bytes(b'preserve-preexisting-fixture')
        with patch.object(Path, 'replace', side_effect=PermissionError('synthetic locked target')):
            PetSettings.save(self.subject)
        self.assertEqual(existing.read_bytes(), b'preserve-preexisting-fixture')
        self.assertEqual(set(self.root.iterdir()), {self.destination, existing})
        self.assertNotIn(self.key.encode(), b''.join(p.read_bytes() for p in self.root.iterdir()))

    def test_success_replaces_key_without_leaving_a_temporary_copy(self):
        PetSettings.save(self.subject)
        self.assertEqual(self.destination.read_text(encoding='utf-8'), self.key + '\n')
        self.assertEqual(set(self.root.iterdir()), {self.destination})
        self.assertEqual(self.subject.key.text(), '')
        self.assertIn('已保存', self.subject.feedback.text())


if __name__ == '__main__':
    unittest.main()

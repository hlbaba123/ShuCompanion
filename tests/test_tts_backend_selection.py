import json
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
from PySide6.QtWidgets import QApplication


class BackendSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def test_clone_runtime_selects_streaming_output_and_never_old_model(self):
        from shu.tts import SpeechOutput
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'install.json').write_text(json.dumps({'backend':'moss-nano','ready':False}),encoding='utf8')
            output=SpeechOutput(runtime_root=root,data_dir=root/'data')
            self.addCleanup(output.close)
            self.assertEqual(type(output).__module__,'shu.moss_tts')
            self.assertFalse(output.ready)

    def test_non_object_manifest_recovers_to_text(self):
        from shu.tts import SpeechOutput
        for payload in ([],None,17,'bad manifest'):
            with self.subTest(payload=payload),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);(root/'install.json').write_text(json.dumps(payload),encoding='utf8')
                output=SpeechOutput(runtime_root=root,data_dir=root/'data')
                try:self.assertFalse(output.ready)
                finally:output.close()

if __name__=='__main__':unittest.main()

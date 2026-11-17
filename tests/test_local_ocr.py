"""Local OCR transport bounds, cancellation and untrusted result validation."""
import io
import json
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest


class Process:
    def __init__(self, output=b'', error=b'', *, returncode=0, running=False):
        self.stdout = io.BytesIO(output)
        self.stderr = io.BytesIO(error)
        self.returncode = returncode
        self.running = running
        self.killed = False

    def poll(self):
        return None if self.running else self.returncode

    def kill(self):
        self.killed = True
        self.running = False

    def wait(self, timeout=None):
        return self.returncode


def result(**changes):
    value = {'width': 202, 'height': 65, 'engine': 'windows.media.ocr:zh-Hans-CN',
             'elapsed_ms': 56, 'words': [{'text': '开', 'rect':
                 {'x': 59, 'y': 24, 'width': 19, 'height': 20}}]}
    value.update(changes)
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


class LocalOcrTests(unittest.TestCase):
    def setUp(self):
        from shu.local_ocr import LocalOcr, LocalOcrError
        self.LocalOcr, self.Error = LocalOcr, LocalOcrError
        self.directory = tempfile.TemporaryDirectory(prefix='黍 OCR ')
        self.addCleanup(self.directory.cleanup)
        self.image = Path(self.directory.name) / "中文 空格 ';&$().png"
        self.image.write_bytes(b'image fixture')
        self.calls = []

    def adapter(self, process, **kwargs):
        def factory(arguments, **options):
            self.calls.append((arguments, options))
            return process
        return self.LocalOcr(process_factory=factory, **kwargs)

    def assert_error(self, code, operation):
        with self.assertRaises(self.Error) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_unicode_path_is_one_literal_argument_and_output_is_projected(self):
        process = Process(result(extra='not exported'))
        observation = self.adapter(process).recognize(self.image)
        arguments, options = self.calls[0]
        self.assertIsInstance(arguments, list)
        self.assertEqual(arguments[-2:], ['-ImagePath', str(self.image.resolve())])
        for flag in ('-NoProfile', '-NonInteractive', '-File'):
            self.assertIn(flag, arguments)
        self.assertNotIn('-Command', arguments)
        self.assertNotIn('-ExecutionPolicy', arguments)
        self.assertFalse(options['shell'])
        self.assertEqual(observation['words'][0]['text'], '开')
        self.assertEqual(set(observation), {'width', 'height', 'words', 'engine', 'elapsed_ms'})
        self.assertGreaterEqual(observation['elapsed_ms'], 0)

    def test_cancellation_before_launch_does_not_read_or_start_host(self):
        cancel = threading.Event()
        cancel.set()
        self.assert_error('ocr_cancelled', lambda: self.adapter(Process()).recognize(self.image, cancel_event=cancel))
        self.assertEqual(self.calls, [])

    def test_cancellation_during_host_terminates_process(self):
        cancel = threading.Event()
        process = Process(running=True)
        def factory(*args, **kwargs):
            cancel.set()
            return process
        adapter = self.LocalOcr(process_factory=factory)
        self.assert_error('ocr_cancelled', lambda: adapter.recognize(self.image, cancel_event=cancel))
        self.assertTrue(process.killed)

    def test_timeout_terminates_process(self):
        process = Process(running=True)
        ticks = iter((0., 0., 2., 3.))
        adapter = self.adapter(process, timeout=1., clock=lambda: next(ticks))
        self.assert_error('ocr_timeout', lambda: adapter.recognize(self.image))
        self.assertTrue(process.killed)

    def test_stdout_and_stderr_are_bounded(self):
        from shu.local_ocr import MAX_OUTPUT_BYTES, MAX_ERROR_BYTES
        for output, error in ((b'x' * (MAX_OUTPUT_BYTES+1), b''), (b'', b'x' * (MAX_ERROR_BYTES+1))):
            with self.subTest(stderr=bool(error)):
                self.assert_error('ocr_output_too_large', lambda: self.adapter(Process(output, error)).recognize(self.image))

    def test_host_failure_and_policy_failure_never_expose_raw_error(self):
        secret = b'private path and provider sk-DO-NOT-EXPORT'
        error = self.assert_error('ocr_host_failed', lambda: self.adapter(Process(error=secret, returncode=1)).recognize(self.image))
        self.assertNotIn('DO-NOT', str(error))
        self.assert_error('ocr_host_policy_blocked', lambda: self.adapter(Process(error=b'PSSecurityException', returncode=1)).recognize(self.image))

    def test_script_failure_is_a_known_safe_code(self):
        self.assert_error('ocr_language_unavailable', lambda: self.adapter(Process(b'{"ok":false,"code":"ocr_language_unavailable"}')).recognize(self.image))
        self.assert_error('ocr_failed', lambda: self.adapter(Process(b'{"ok":false,"code":"private arbitrary message"}')).recognize(self.image))

    def test_invalid_json_or_nonfinite_data_are_rejected(self):
        for payload in (b'not json', b'[]', result(width=True), result(height=10001),
                        result(words=[{'text': 'a', 'rect': {'x': 201, 'y': 0, 'width': 2, 'height': 3}}]),
                        result(words=[{'text': 'a', 'rect': {'x': float('nan'), 'y': 0, 'width': 2, 'height': 3}}]),
                        result(engine='unknown')):
            with self.subTest(payload=payload[:80]):
                self.assert_error('ocr_invalid_output', lambda: self.adapter(Process(payload)).recognize(self.image))

    def test_word_count_is_bounded(self):
        from shu.local_ocr import MAX_WORDS
        word = {'text': 'a', 'rect': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}
        self.assert_error('ocr_invalid_output', lambda: self.adapter(Process(result(words=[word]*(MAX_WORDS+1)))).recognize(self.image))

    def test_missing_or_large_input_never_launches(self):
        adapter = self.adapter(Process())
        self.assert_error('ocr_image_missing', lambda: adapter.recognize(self.image.with_name('missing.png')))
        from shu.local_ocr import MAX_IMAGE_BYTES
        with self.image.open('wb') as stream:
            stream.truncate(MAX_IMAGE_BYTES+1)
        self.assert_error('ocr_image_too_large', lambda: adapter.recognize(self.image))
        self.assertEqual(self.calls, [])

    def test_launch_error_and_read_error_are_safe(self):
        def unavailable(*args, **kwargs):
            raise OSError('private provider sk-DO-NOT-EXPORT')
        error = self.assert_error('ocr_host_unavailable', lambda: self.LocalOcr(process_factory=unavailable).recognize(self.image))
        self.assertNotIn('DO-NOT', str(error))
        class BrokenPipe(io.BytesIO):
            def read(self, *args):
                raise OSError('private pipe details')
        process = Process(result())
        process.stdout = BrokenPipe()
        self.assert_error('ocr_host_failed', lambda: self.adapter(process).recognize(self.image))

    def test_running_host_is_killed_when_output_overflows(self):
        from shu.local_ocr import MAX_OUTPUT_BYTES
        process = Process(b'x' * (MAX_OUTPUT_BYTES + 1), running=True)
        self.assert_error('ocr_output_too_large', lambda: self.adapter(process).recognize(self.image))
        self.assertTrue(process.killed)

    def test_word_text_rectangles_and_timing_are_untrusted(self):
        bad_words = [
            [{'text': '', 'rect': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}],
            [{'text': 'x' * 513, 'rect': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}],
            [{'text': 'a', 'rect': {'x': -1, 'y': 0, 'width': 1, 'height': 1}}],
            [{'text': 'a', 'rect': {'x': 0, 'y': 0, 'width': 0, 'height': 1}}],
        ]
        for words in bad_words:
            with self.subTest(words=words):
                self.assert_error('ocr_invalid_output', lambda: self.adapter(Process(result(words=words))).recognize(self.image))
        for elapsed in (True, -1, float('inf'), 10**400):
            with self.subTest(elapsed=elapsed):
                self.assert_error('ocr_invalid_output', lambda: self.adapter(Process(result(elapsed_ms=elapsed))).recognize(self.image))


@unittest.skipUnless(os.name == 'nt' and os.environ.get('SHU_TEST_LOCAL_OCR_IMAGE'),
                     'opt-in real Windows OCR fixture: SHU_TEST_LOCAL_OCR_IMAGE')
class WindowsOcrSmokeTests(unittest.TestCase):
    def test_real_chinese_fixture_from_literal_unicode_path_is_read_only(self):
        from shu.local_ocr import LocalOcr
        original = Path(os.environ['SHU_TEST_LOCAL_OCR_IMAGE'])
        with tempfile.TemporaryDirectory(prefix='黍 OCR ') as directory:
            target = Path(directory) / "中文 空格 ';&$().png"
            shutil.copyfile(original, target)
            before = hashlib.sha256(target.read_bytes()).digest()
            observation = LocalOcr().recognize(target)
            self.assertEqual((observation['width'], observation['height']), (202, 65))
            self.assertEqual(''.join(word['text'] for word in observation['words']), '开始唤醒')
            self.assertEqual(observation['engine'], 'windows.media.ocr:zh-Hans-CN')
            self.assertEqual(hashlib.sha256(target.read_bytes()).digest(), before)


class NavigationOcrTests(unittest.TestCase):
    assert_error = LocalOcrTests.assert_error

    def setUp(self):
        LocalOcrTests.setUp(self)
        from PySide6.QtGui import QImage
        image = QImage(1280, 720, QImage.Format.Format_RGB32)
        image.fill(0xffffffff)
        self.assertTrue(image.save(str(self.image), 'PNG'))
        self.temporary = Path(self.directory.name) / 'temporary'
        self.temporary.mkdir()
        self.child_image = None

    def batch(self, **changes):
        original = json.loads(result(width=1280, height=720))
        region = json.loads(result(width=1408, height=792))
        value = {'observations': [original, region]}
        value.update(changes)
        return json.dumps(value, ensure_ascii=False).encode('utf-8')

    def nav_adapter(self, process, *, during_launch=None, **options):
        def factory(arguments, **kwargs):
            self.calls.append((arguments, kwargs))
            self.child_image = Path(arguments[arguments.index('-NavigationImagePath') + 1])
            self.assertTrue(self.child_image.is_file())
            self.assertEqual(self.child_image.suffix, '.bmp')
            self.assertLess(self.child_image.stat().st_size, 32 * 1024 * 1024)
            if during_launch:
                during_launch()
            return process
        return self.LocalOcr(process_factory=factory, temp_root=self.temporary, **options)

    def test_navigation_uses_one_literal_batch_and_cleans_technical_image(self):
        value = self.nav_adapter(Process(self.batch())).recognize_navigation(self.image)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(value['regions'][1]['origin'], {'x': 576, 'y': 324})
        self.assertEqual(value['regions'][1]['scale'], {'x': 2., 'y': 2.})
        self.assertEqual(value['regions'][0]['observation']['width'], 1280)
        self.assertEqual((value['width'], value['height']), (1280, 720))
        self.assertFalse(self.child_image.exists())
        self.assertEqual(list(self.temporary.iterdir()), [])
        arguments, options = self.calls[0]
        self.assertIn(str(self.image.resolve()), arguments)
        self.assertFalse(options['shell'])
        self.assertNotIn('-ExecutionPolicy', arguments)
        self.assertNotIn('-Command', arguments)

    def test_navigation_cleans_temporary_input_on_host_error_or_invalid_batch(self):
        for process in (Process(error=b'private', returncode=1), Process(b'{}'),
                        Process(self.batch(observations=[])),
                        Process(self.batch(observations=[json.loads(result())] * 3))):
            with self.subTest(process=process):
                with self.assertRaises(self.Error):
                    self.nav_adapter(process).recognize_navigation(self.image)
                self.assertFalse(self.child_image.exists())
                self.assertEqual(list(self.temporary.iterdir()), [])

    def test_navigation_cancellation_kills_only_child_and_removes_temporary_image(self):
        cancel = threading.Event()
        process = Process(running=True)
        adapter = self.nav_adapter(process, during_launch=cancel.set)
        self.assert_error('ocr_cancelled', lambda: adapter.recognize_navigation(self.image, cancel_event=cancel))
        self.assertTrue(process.killed)
        self.assertFalse(self.child_image.exists())
        self.assertEqual(list(self.temporary.iterdir()), [])

    def test_navigation_timeout_includes_child_and_cleans_temporary_image(self):
        ticks = [0.]
        process = Process(running=True)
        adapter = self.nav_adapter(process, timeout=1., clock=lambda: ticks[0], during_launch=lambda: ticks.__setitem__(0, 2.))
        self.assert_error('ocr_timeout', lambda: adapter.recognize_navigation(self.image))
        self.assertTrue(process.killed)
        self.assertFalse(self.child_image.exists())

    def test_navigation_rejects_invalid_input_without_launch(self):
        self.image.write_bytes(b'not an image')
        self.assert_error('ocr_image_unreadable', lambda: self.nav_adapter(Process()).recognize_navigation(self.image))
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.temporary.iterdir()), [])

    def test_navigation_dimensions_must_match_preprocessed_source(self):
        self.assert_error('ocr_invalid_output', lambda: self.nav_adapter(Process(
            self.batch(observations=[json.loads(result()), json.loads(result())]))).recognize_navigation(self.image))
        self.assertEqual(list(self.temporary.iterdir()), [])

    def test_navigation_output_is_bounded_and_temp_is_removed(self):
        from shu.local_ocr import MAX_NAV_OUTPUT_BYTES
        process = Process(b'x' * (MAX_NAV_OUTPUT_BYTES + 1), running=True)
        self.assert_error('ocr_output_too_large', lambda: self.nav_adapter(process).recognize_navigation(self.image))
        self.assertTrue(process.killed)
        self.assertEqual(list(self.temporary.iterdir()), [])


if __name__ == '__main__':
    unittest.main()

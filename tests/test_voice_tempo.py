"""Offline PCM tests: no model, GPU, audio device, or network is used."""
import importlib.util
from pathlib import Path
import random
import sys
import unittest

dependencies=Path(__file__).resolve().parents[2]/'.shu-voice-eval/deps'
if dependencies.is_dir():sys.path.insert(0,str(dependencies))

try:
    import numpy as np
except ImportError:
    np = None


class TempoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'assets' / 'voice_tempo.py'
        if not path.is_file():
            cls.TempoStream = None
            return
        spec = importlib.util.spec_from_file_location('voice_tempo', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.TempoStream = module.TempoStream

    def stream(self, **kwargs):
        self.assertIsNotNone(self.TempoStream, 'Streaming pitch-preserving tempo adapter is missing')
        return self.TempoStream(**kwargs)

    def convert(self, pcm, chunks, **kwargs):
        stream = self.stream(**kwargs)
        pieces = []
        offset = 0
        for size in chunks:
            pieces.append(stream.process(pcm[offset:offset + size]))
            self.assertEqual(len(pieces[-1]) % (2 * kwargs.get('channels', 2)), 0)
            offset += size
        pieces.append(stream.process(pcm[offset:]))
        tail = stream.finish()
        pieces.append(tail)
        self.assertEqual(stream.finish(), b'')
        return b''.join(pieces)

    def test_unity_is_byte_identical_across_partial_pcm_frames(self):
        pcm = random.Random(7).randbytes(16004)
        result = self.convert(pcm, [1, 2, 701, 4095, 3, 112], speed=1.0)
        self.assertEqual(result, pcm)

    def test_invalid_speed_and_pcm_format_fail_early(self):
        for speed in (0.9, 1.21, float('nan'), float('inf')):
            with self.subTest(speed=speed), self.assertRaises(ValueError):
                self.stream(speed=speed)
        for rate, channels in ((0, 2), (48000, 0), (48000, 1.5)):
            with self.subTest(rate=rate, channels=channels), self.assertRaises(ValueError):
                self.stream(rate=rate, channels=channels)

    def test_partial_final_pcm_frame_is_not_silently_lost(self):
        stream = self.stream(speed=1.0)
        self.assertEqual(stream.process(b'\x01\x02\x03'), b'')
        with self.assertRaises(ValueError):
            stream.finish()

    def test_finished_stream_cannot_accept_more_input(self):
        stream = self.stream(speed=1.0)
        stream.finish()
        with self.assertRaises(RuntimeError):
            stream.process(b'\0' * 4)

    @unittest.skipIf(np is None, 'NumPy is available in the speech inference environment')
    def test_default_tempo_shortens_duration_without_raising_pitch(self):
        frames = np.arange(144000)
        tone = (12000 * np.sin(2 * np.pi * 220 * frames / 48000)).astype('<i2')
        pcm = np.column_stack((tone, tone)).tobytes()
        result = self.convert(pcm, [4097, 117, 22001, 10003, 7])
        output = np.frombuffer(result, dtype='<i2').reshape(-1, 2)
        self.assertAlmostEqual(len(output), 133333, delta=1)
        signal = output[4096:-4096, 0].astype(float)
        spectrum = abs(np.fft.rfft(signal * np.hanning(len(signal))))
        frequency = np.argmax(spectrum) * 48000 / len(signal)
        self.assertAlmostEqual(frequency, 220, delta=1.5)
        np.testing.assert_array_equal(output[:, 0], output[:, 1])

    @unittest.skipIf(np is None, 'NumPy is available in the speech inference environment')
    def test_chunk_boundaries_do_not_reset_time_stretch_state(self):
        frames = np.arange(48001)
        tone = (7000 * np.sin(2 * np.pi * 220 * frames / 48000)).astype('<i2')
        # Anti-phase channels must remain synchronized.
        pcm = np.column_stack((tone, -tone)).tobytes()
        whole = self.convert(pcm, [], speed=1.08)
        fragmented = self.convert(pcm, [1, 307, 4000, 91, 8011] * 10, speed=1.08)
        self.assertEqual(fragmented, whole)
        output = np.frombuffer(whole, dtype='<i2').reshape(-1, 2).astype(int)
        self.assertAlmostEqual(len(output), 44445, delta=1)
        self.assertLessEqual(np.max(abs(output[:, 0] + output[:, 1])), 1)
        self.assertLess(np.max(abs(np.diff(output[2048:-2048, 0]))), 900)

    @unittest.skipIf(np is None, 'NumPy is available in the speech inference environment')
    def test_final_flush_keeps_short_utterances_and_audible_tail(self):
        for count in (20, 480, 1900, 48001):
            with self.subTest(count=count):
                pcm = np.full((count, 2), 6000, dtype='<i2').tobytes()
                result = self.convert(pcm, [1, 3, 199, 871], speed=1.2)
                output = np.frombuffer(result, dtype='<i2').reshape(-1, 2)
                self.assertAlmostEqual(len(output), round(count / 1.2), delta=1)
                self.assertGreater(np.max(abs(output)), 1000)
                self.assertGreater(np.mean(abs(output[-min(40, len(output)):])), 1000)

    @unittest.skipIf(np is None, 'NumPy is available in the speech inference environment')
    def test_stereo_channels_share_one_alignment(self):
        # Independent channel correlations can choose different source times
        # when one channel has a DC offset. A linked WSOLA must preserve this
        # hand-constructed affine relationship after overlap/normalization.
        frames = np.arange(48000)
        envelope = .6 + .4 * np.sin(2 * np.pi * 3.3 * frames / 48000)
        left = np.rint(6000 * envelope * np.sin(2 * np.pi * 220 * frames / 48000))
        right = 6000 + left / 2
        pcm = np.column_stack((left, right)).astype('<i2').tobytes()
        result = self.convert(pcm, [4101] * 40)
        out = np.frombuffer(result, dtype='<i2').reshape(-1, 2).astype(float)
        relation = out[2048:-2048, 1] - out[2048:-2048, 0] / 2
        self.assertLessEqual(np.max(abs(relation - 6000)), 2)

    @unittest.skipIf(np is None, 'NumPy is available in the speech inference environment')
    def test_late_sound_is_kept_when_last_analysis_window_is_partial(self):
        pcm = np.zeros((48001, 2), dtype='<i2')
        pcm[-3840:] = 8000
        result = self.convert(pcm.tobytes(), [4096] * 46)
        out = np.frombuffer(result, dtype='<i2').reshape(-1, 2)
        self.assertGreater(np.mean(abs(out[-960:])), 7000)

    @unittest.skipIf(np is None, 'NumPy is available in the speech inference environment')
    def test_empty_stream_finishes_without_producing_padding(self):
        stream = self.stream()
        self.assertEqual(stream.process(b''), b'')
        self.assertEqual(stream.finish(), b'')


if __name__ == '__main__':
    unittest.main()

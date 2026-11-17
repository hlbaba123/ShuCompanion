"""A deterministic waveOut device double keeps verification silent."""
import importlib.util
import unittest


class Device:
    def __init__(self):
        self.buffers = []; self.released = []; self.frames = 0
        self.started = False; self.closed = False; self.reset_count = 0
    def open(self, rate, channels): self.rate, self.channels = rate, channels
    def pause(self): self.started = False
    def restart(self): self.started = True
    def write(self, data):
        buffer = {'data': data, 'done': False}; self.buffers.append(buffer); return buffer
    def done(self, buffer): return buffer['done']
    def release(self, buffer): self.released.append(buffer)
    def position(self): return self.frames
    def reset(self):
        self.reset_count += 1
        for buffer in self.buffers: buffer['done'] = True
    def close(self): self.closed = True


class StreamAudioTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('shu.stream_audio'), 'Queued PCM player is missing')
        from shu.stream_audio import PcmPlayer
        self.device = Device()
        self.player = PcmPlayer(device_factory=lambda: self.device)
        self.addCleanup(self.player.stop)

    def test_prebuffer_prevents_start_until_240ms_and_preserves_every_byte(self):
        self.player.begin(48000, 2)
        data = b'\x01\x00\x02\x00' * 5760
        self.player.append(data)
        self.assertFalse(self.player.status()[1]); self.assertFalse(self.device.started)
        self.player.append(data)
        self.assertTrue(self.player.status()[1]); self.assertTrue(self.device.started)
        self.assertEqual(b''.join(buffer['data'] for buffer in self.device.buffers), data + data)

    def test_clock_comes_from_device_and_empty_queue_cannot_finish_live_generation(self):
        self.player.begin(48000, 2); self.player.append(b'\x00' * 48000)
        self.device.frames = 2400
        self.assertEqual(self.player.status(), (.05, True))
        self.device.buffers[0]['done'] = True
        self.assertEqual(self.player.status(), (.05, True))
        self.assertEqual(self.device.released, self.device.buffers)
        self.player.finish()
        self.assertEqual(self.player.status(), (.05, False))

    def test_short_final_chunk_starts_and_finish_waits_for_whdr_done(self):
        self.player.begin(48000, 2); self.player.append(b'\x00' * 1920)
        self.player.finish()
        self.assertTrue(self.player.status()[1])
        self.assertEqual(self.device.released, [])
        self.device.frames = 480; self.device.buffers[0]['done'] = True
        self.assertEqual(self.player.status(), (.01, False))

    def test_stop_resets_before_releasing_buffers_and_clears_clock(self):
        self.player.begin(48000, 2); self.player.append(b'\x00' * 48000)
        self.player.stop()
        self.assertEqual(self.device.reset_count, 1); self.assertTrue(self.device.closed)
        self.assertEqual(self.device.released, self.device.buffers)
        self.assertEqual(self.player.status(), (0., False))

    def test_partial_frame_and_append_after_finish_are_rejected(self):
        self.player.begin(48000, 2)
        with self.assertRaises(ValueError): self.player.append(b'\x00')
        self.player.finish()
        with self.assertRaises(RuntimeError): self.player.append(b'\x00' * 4)


if __name__ == '__main__': unittest.main()

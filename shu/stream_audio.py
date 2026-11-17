"""Queued int16 PCM playback with the Windows waveOut sample clock.

No inference libraries and no playback thread live in the Qt process. Buffers
remain owned until the device marks them WHDR_DONE (or waveOutReset returns).
"""
import ctypes
from ctypes import wintypes
import sys


class _WaveFormat(ctypes.Structure):
    _fields_ = [('wFormatTag', wintypes.WORD), ('nChannels', wintypes.WORD),
                ('nSamplesPerSec', wintypes.DWORD), ('nAvgBytesPerSec', wintypes.DWORD),
                ('nBlockAlign', wintypes.WORD), ('wBitsPerSample', wintypes.WORD),
                ('cbSize', wintypes.WORD)]


class _WaveHeader(ctypes.Structure):
    _fields_ = [('lpData', ctypes.c_void_p), ('dwBufferLength', wintypes.DWORD),
                ('dwBytesRecorded', wintypes.DWORD), ('dwUser', ctypes.c_size_t),
                ('dwFlags', wintypes.DWORD), ('dwLoops', wintypes.DWORD),
                ('lpNext', ctypes.c_void_p), ('reserved', ctypes.c_size_t)]


class _TimeValue(ctypes.Union):
    _fields_ = [('sample', wintypes.DWORD), ('ms', wintypes.DWORD),
                ('cb', wintypes.DWORD), ('padding', ctypes.c_byte * 8)]


class _MultimediaTime(ctypes.Structure):
    _fields_ = [('wType', wintypes.UINT), ('value', _TimeValue)]


class _NativeWaveOut:
    def __init__(self):
        if sys.platform != 'win32': raise RuntimeError('PCM playback requires Windows')
        self._api = ctypes.WinDLL('winmm')
        self._handle = wintypes.HANDLE()
        self._rate = self._channels = 0
        signatures = {
            'waveOutOpen': [ctypes.POINTER(wintypes.HANDLE), wintypes.UINT, ctypes.POINTER(_WaveFormat),
                            ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD],
            'waveOutPrepareHeader': [wintypes.HANDLE, ctypes.POINTER(_WaveHeader), wintypes.UINT],
            'waveOutWrite': [wintypes.HANDLE, ctypes.POINTER(_WaveHeader), wintypes.UINT],
            'waveOutUnprepareHeader': [wintypes.HANDLE, ctypes.POINTER(_WaveHeader), wintypes.UINT],
            'waveOutGetPosition': [wintypes.HANDLE, ctypes.POINTER(_MultimediaTime), wintypes.UINT],
            'waveOutPause': [wintypes.HANDLE], 'waveOutRestart': [wintypes.HANDLE],
            'waveOutReset': [wintypes.HANDLE], 'waveOutClose': [wintypes.HANDLE],
        }
        for name, arguments in signatures.items():
            function = getattr(self._api, name)
            function.argtypes = arguments; function.restype = wintypes.UINT

    def _call(self, name, *args):
        code = getattr(self._api, name)(*args)
        if code: raise OSError(f'Audio device error {code}')

    def open(self, rate, channels):
        self._rate, self._channels = rate, channels
        format_ = _WaveFormat(1, channels, rate, rate * channels * 2, channels * 2, 16, 0)
        self._call('waveOutOpen', ctypes.byref(self._handle), 0xFFFFFFFF, ctypes.byref(format_), 0, 0, 0)

    def pause(self): self._call('waveOutPause', self._handle)
    def restart(self): self._call('waveOutRestart', self._handle)

    def write(self, data):
        memory = ctypes.create_string_buffer(data)
        header = _WaveHeader(ctypes.cast(memory, ctypes.c_void_p), len(data), 0, 0, 0, 0, None, 0)
        self._call('waveOutPrepareHeader', self._handle, ctypes.byref(header), ctypes.sizeof(header))
        try:
            self._call('waveOutWrite', self._handle, ctypes.byref(header), ctypes.sizeof(header))
        except OSError:
            self._call('waveOutUnprepareHeader', self._handle, ctypes.byref(header), ctypes.sizeof(header))
            raise
        return memory, header

    def done(self, buffer): return bool(buffer[1].dwFlags & 1)  # WHDR_DONE
    def release(self, buffer):
        self._call('waveOutUnprepareHeader', self._handle, ctypes.byref(buffer[1]), ctypes.sizeof(buffer[1]))

    def position(self):
        value = _MultimediaTime(); value.wType = 2  # TIME_SAMPLES
        self._call('waveOutGetPosition', self._handle, ctypes.byref(value), ctypes.sizeof(value))
        if value.wType == 2: return value.value.sample
        if value.wType == 4: return value.value.cb // (2 * self._channels)
        if value.wType == 1: return value.value.ms * self._rate // 1000
        raise RuntimeError('Audio device did not return a playback clock')

    def reset(self): self._call('waveOutReset', self._handle)
    def close(self):
        self._call('waveOutClose', self._handle)
        self._handle = wintypes.HANDLE()


class PcmPlayer:
    """Single-owner queue; all calls belong on the Qt/UI thread.

    `playing` stays true during an underrun until finish() and final drain.
    The initial 240 ms prebuffer delays start, but never cuts PCM samples.
    """
    MAX_QUEUED_BYTES = 48_000 * 2 * 2 * 120

    def __init__(self, *, device_factory=None):
        self._factory = device_factory or _NativeWaveOut
        self._device = None
        self._queue = []
        self._queued_bytes = self._submitted_frames = 0
        self._rate = self._channels = 0
        self._started = self._finished = False

    def begin(self, rate, channels):
        if type(rate) is not int or not 8000 <= rate <= 192000 or type(channels) is not int or channels not in (1, 2):
            raise ValueError('Invalid PCM format')
        self.stop()
        self._rate, self._channels = rate, channels
        self._device = self._factory()
        try:
            self._device.open(rate, channels)
            self._device.pause()
        except (OSError, RuntimeError):
            self.stop()
            raise

    def _collect(self):
        while self._queue and self._device.done(self._queue[0][0]):
            buffer, size = self._queue[0]
            self._device.release(buffer)
            self._queue.pop(0); self._queued_bytes -= size

    def _start(self):
        if not self._started and self._queue:
            self._device.restart(); self._started = True

    def append(self, data):
        if self._device is None or self._finished: raise RuntimeError('PCM stream is not accepting audio')
        if not isinstance(data, bytes) or len(data) % (2 * self._channels): raise ValueError('Incomplete PCM frame')
        if not data: return
        self._collect()
        if self._queued_bytes + len(data) > self.MAX_QUEUED_BYTES: raise ValueError('PCM queue limit exceeded')
        buffer = self._device.write(data)
        self._queue.append((buffer, len(data))); self._queued_bytes += len(data)
        self._submitted_frames += len(data) // (2 * self._channels)
        if self._queued_bytes >= round(self._rate * .24) * self._channels * 2: self._start()

    def finish(self):
        self._finished = True
        if self._device is not None: self._start()

    def status(self):
        if self._device is None: return 0., False
        self._collect()
        frames = min(self._device.position(), self._submitted_frames)
        return frames / self._rate, self._started and not (self._finished and not self._queue)

    def stop(self):
        if self._device is not None:
            # On a driver error keep the buffer owners alive: freeing memory
            # still borrowed by waveOut would be unsafe. A later stop can retry.
            self._device.reset()
            while self._queue:
                self._device.release(self._queue[0][0]); self._queue.pop(0)
            self._device.close(); self._device = None
        self._queue.clear(); self._queued_bytes = self._submitted_frames = 0
        self._started = self._finished = False
        self._rate = self._channels = 0

"""An explicitly started HWND-specific WGC stream with one immutable latest frame.

The caller owns permission, PID/geometry validation and observation IDs. It must
close this stream on stop, revoke, rebind and job completion. No input is sent.
Native frame references never escape the callback. QPC timestamps remain the
source of evidence age, including when a cached frame is read repeatedly.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import sys
import threading
import time
import uuid

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QImageWriter


class CaptureStreamError(RuntimeError):
    def __init__(self, message, code='capture_stream_error'):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FrameSnapshot:
    sequence: int
    generation: str
    window_id: int
    window_pid: int
    geometry: tuple[int, int, int, int]
    width: int
    height: int
    bgra: bytes
    captured_monotonic: float
    captured_at: float
    arrived_monotonic: float
    native_timespan: int


def _qpc_mapper(clock):
    """SystemRelativeTime.Duration is QPC time in 100ns units, not arrival time."""
    if sys.platform != 'win32':
        raise CaptureStreamError('原生捕获时钟仅适用于 Windows。', 'capture_clock_unavailable')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    for name in ('QueryPerformanceCounter', 'QueryPerformanceFrequency'):
        getattr(kernel, name).argtypes = [ctypes.POINTER(ctypes.c_longlong)]
        getattr(kernel, name).restype = ctypes.c_int
    frequency, counter = ctypes.c_longlong(), ctypes.c_longlong()
    before = clock()
    if not kernel.QueryPerformanceFrequency(ctypes.byref(frequency)) or not kernel.QueryPerformanceCounter(ctypes.byref(counter)):
        raise CaptureStreamError('无法核对捕获帧时钟。', 'capture_clock_unavailable')
    after = clock()
    offset = (before+after)/2-counter.value/frequency.value
    return lambda ticks: ticks/10_000_000.+offset


def _default_factory(**kwargs):
    from windows_capture import WindowsCapture
    return WindowsCapture(**kwargs)


class CaptureStream:
    def __init__(self, hwnd, pid, geometry, *, factory=None, clock=time.monotonic,
                 wall_clock=time.time, timestamp_mapper=None, minimum_update_interval_ms=100):
        if (not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0
                or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
                or len(geometry) != 4 or any(not isinstance(v, int) for v in geometry)
                or min(geometry[2:]) <= 0):
            raise ValueError('A stream requires an explicit HWND, PID and client geometry.')
        self.hwnd, self.pid, self.geometry = hwnd, pid, tuple(geometry)
        self.clock, self.wall_clock = clock, wall_clock
        self._timestamp = timestamp_mapper or _qpc_mapper(clock)
        self._factory = factory or _default_factory
        self.minimum_update_interval_ms = max(0, min(1000, int(minimum_update_interval_ms)))
        self.generation = uuid.uuid4().hex
        self._condition = threading.Condition()
        self._closed = False
        self._started = False
        self._latest = None
        self._sequence = 0
        self._source_size = None
        self._last_source = float('-inf')
        self._session = self._control = None
        self._error = None
        self._stop_done = threading.Event()
        self.frames_received = self.frames_published = 0

    @property
    def closed(self):
        with self._condition:
            return self._closed

    @property
    def latest_sequence(self):
        with self._condition:
            return self._sequence

    def start(self, guard=None):
        if guard:
            guard()
        with self._condition:
            if self._closed:
                raise CaptureStreamError('捕获流已关闭。', 'capture_stream_closed')
            if self._started:
                return self
            self._started = True
        try:
            session = self._factory(window_hwnd=self.hwnd, cursor_capture=False, draw_border=None,
                                    minimum_update_interval=self.minimum_update_interval_ms)

            @session.event
            def on_frame_arrived(frame, capture_control):
                if self.closed:
                    capture_control.stop()
                    return
                try:
                    self._publish(frame)
                except Exception:
                    self._fail('捕获流返回无效帧，已停止。', 'capture_stream_frame_failed')
                    capture_control.stop()

            @session.event
            def on_closed():
                self._fail('游戏窗口或捕获会话已关闭。', 'capture_stream_closed')

            with self._condition:
                self._session = session
            control = session.start_free_threaded()
            with self._condition:
                if not self._closed:
                    self._control = control
                    return self
            self._stop_async(control)
            raise CaptureStreamError('捕获流启动期间已被关闭。', 'capture_stream_closed')
        except CaptureStreamError:
            raise
        except Exception:
            self._fail('Windows 持久捕获会话未能启动。', 'capture_stream_start_failed')
            raise CaptureStreamError('Windows 持久捕获会话未能启动。', 'capture_stream_start_failed') from None

    def _publish(self, frame):
        arrival = self.clock()
        width, height = int(frame.width), int(frame.height)
        raw_time = int(frame.timespan)
        source = self._timestamp(raw_time)
        with self._condition:
            self.frames_received += 1
            if self._closed:
                return
            if (not math.isfinite(source) or source > arrival+.05 or arrival-source > 2.
                    or source <= self._last_source):
                return  # Old, repeated or uncalibrated native frames are not new evidence.
            if width <= 0 or height <= 0 or width*height > 40_000_000:
                raise ValueError('invalid frame dimensions')
            if self._source_size is not None and self._source_size != (width, height):
                self._fail('捕获窗口尺寸已改变，请重新建立会话。', 'geometry_changed')
                return
        pixels = frame.frame_buffer.tobytes()  # Only this owned immutable copy survives the callback.
        if len(pixels) != width*height*4:
            raise ValueError('incomplete frame')
        stride = max(4, (len(pixels)//4096//4)*4)
        sample = b''.join(pixels[channel::stride] for channel in range(3))
        if not sample or max(sample) < 5:
            return
        captured_at = self.wall_clock()-(self.clock()-source)
        with self._condition:
            if self._closed or source <= self._last_source:
                return
            self._sequence += 1
            self._source_size = (width, height)
            self._last_source = source
            self._latest = FrameSnapshot(self._sequence, self.generation, self.hwnd, self.pid,
                self.geometry, width, height, pixels, source, captured_at, arrival, raw_time)
            self.frames_published += 1
            self._condition.notify_all()

    def _fail(self, message, code):
        with self._condition:
            if self._error is None:
                self._error = CaptureStreamError(message, code)
        self.close()

    def _stop_async(self, control):
        def stop():
            try:
                control.stop()  # Native stop joins its thread; never do this on Qt's thread.
            except Exception:
                pass
            finally:
                self._stop_done.set()
        threading.Thread(target=stop, name='shu-wgc-stop', daemon=True).start()

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._latest = None
            control, self._control = self._control, None
            self._session = None
            self._condition.notify_all()
        if control is not None:
            self._stop_async(control)
        elif not self._started:
            self._stop_done.set()

    def wait_closed(self, timeout=.5):
        """Optional shutdown verification for worker/tests; close() itself never joins."""
        return self._stop_done.wait(timeout)

    def read_frame(self, *, after_sequence=0, not_before=None, max_age=.35, timeout=2.,
                   cancel_event=None, guard=None):
        if not math.isfinite(max_age) or max_age <= 0 or not math.isfinite(timeout) or not 0 <= timeout <= 10:
            raise ValueError('Invalid frame age or bounded timeout.')
        deadline = self.clock()+timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise CaptureStreamError('捕获读取已取消。', 'capture_cancelled')
            if guard:
                guard()
            with self._condition:
                if self._closed:
                    raise self._error or CaptureStreamError('捕获流已关闭。', 'capture_stream_closed')
                if not self._started:
                    raise CaptureStreamError('请先在授权范围内启动捕获流。', 'capture_stream_not_started')
                frame = self._latest
                age = self.clock()-frame.captured_monotonic if frame is not None else None
                eligible = (frame is not None and frame.sequence > after_sequence and 0 <= age <= max_age
                            and (not_before is None or frame.captured_monotonic >= not_before))
                if eligible:
                    chosen = frame
                else:
                    remaining = deadline-self.clock()
                    if remaining <= 0:
                        raise CaptureStreamError('没有更新且未过期的游戏帧；未将缓存改成新观察。', 'capture_stream_timeout')
                    self._condition.wait(min(.025, remaining))
                    continue
            if cancel_event is not None and cancel_event.is_set():
                raise CaptureStreamError('捕获读取已取消。', 'capture_cancelled')
            if guard:
                guard()
            with self._condition:
                if self._closed:
                    raise self._error or CaptureStreamError('捕获流已关闭。', 'capture_stream_closed')
                if self.clock()-chosen.captured_monotonic > max_age:
                    continue
            return chosen

    def peek_sample(self, info, crop_geometry, **read_options):
        frame = self.read_frame(**read_options)
        image, _ = _client_image(frame, info, crop_geometry)
        image = image.scaled(64, 36, Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation).convertToFormat(QImage.Format.Format_RGB888)
        sample = bytes(image.constBits())
        return {'sample_rgb': sample, 'fingerprint': hashlib.blake2b(sample, digest_size=12).hexdigest(),
            'sequence': frame.sequence, 'generation': frame.generation, 'captured_at': frame.captured_at,
            'captured_monotonic': frame.captured_monotonic, 'width': info.width, 'height': info.height,
            'window_id': info.hwnd, 'window_pid': info.pid, 'actionable': False,
            'client_rect': {'left': info.left, 'top': info.top, 'width': info.width, 'height': info.height}}


def _client_image(frame, info, crop_geometry):
    identity = (info.hwnd, info.pid, (info.left, info.top, info.width, info.height))
    if identity != (frame.window_id, frame.window_pid, frame.geometry):
        raise CaptureStreamError('缓存帧与当前窗口身份或几何不一致。', 'capture_stream_identity_changed')
    left, top = crop_geometry(info, frame.width, frame.height)
    if min(left, top) < 0 or left+info.width > frame.width or top+info.height > frame.height:
        raise CaptureStreamError('捕获帧与客户区边界不一致。', 'wgc_client_bounds')
    image = QImage(frame.bgra, frame.width, frame.height, frame.width*4, QImage.Format.Format_ARGB32)
    if image.isNull():
        raise CaptureStreamError('缓存帧图像无效。', 'invalid_image')
    return image.copy(left, top, info.width, info.height), {'left': left, 'top': top}


def export_frame(frame, path, info, crop_geometry):
    """Lossless Qt PNG export, preserving the original capture timestamp."""
    image, crop = _client_image(frame, info, crop_geometry)
    writer = QImageWriter(str(Path(path)), b'png')
    writer.setCompression(1)
    if not writer.write(image):
        raise CaptureStreamError('无法写入捕获帧图片。', 'capture_stream_export_failed')
    return {'method': 'windows_graphics_capture_stream', 'width': info.width, 'height': info.height,
        'requires_foreground': False, 'background_capture': True, 'warnings': [],
        'source_frame_size': [frame.width, frame.height], 'client_crop': crop,
        'captured_at': frame.captured_at, 'captured_monotonic': frame.captured_monotonic,
        'frame_sequence': frame.sequence, 'stream_generation': frame.generation}

"""Native-free lifecycle, timestamp, bounded-buffer and lossless export tests."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from PySide6.QtGui import QImage
from shu.capture_stream import CaptureStream, CaptureStreamError, export_frame


class Buffer:
    def __init__(self,pixels):self.pixels=bytearray(pixels)
    def tobytes(self):return bytes(self.pixels)


class Control:
    def __init__(self):self.calls=0;self.stop_gate=None;self.entered=threading.Event()
    def stop(self):
        self.calls+=1;self.entered.set()
        if self.stop_gate:self.stop_gate.wait(2)


class Session:
    def __init__(self,**kwargs):self.kwargs=kwargs;self.events={};self.control=Control()
    def event(self,handler):self.events[handler.__name__]=handler;return handler
    def start_free_threaded(self):return self.control
    def emit(self,seconds,*,width=2,height=2,pixels=None):
        buffer=Buffer(pixels if pixels is not None else bytes([30,60,200,255])*(width*height))
        frame=SimpleNamespace(width=width,height=height,timespan=round(seconds*10_000_000),frame_buffer=buffer)
        self.events['on_frame_arrived'](frame,self.control)
        return buffer


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.now=100.;self.sessions=[]
        def factory(**kwargs):
            result=Session(**kwargs);self.sessions.append(result);return result
        self.stream=CaptureStream(11,22,(0,0,2,2),factory=factory,clock=lambda:self.now,
            wall_clock=lambda:1000+self.now,timestamp_mapper=lambda raw:raw/10_000_000.)
        self.addCleanup(self.stream.close)
        self.info=SimpleNamespace(hwnd=11,pid=22,left=0,top=0,width=2,height=2)
    def start(self):self.stream.start();return self.sessions[0]
    def read(self,**kwargs):return self.stream.read_frame(timeout=0,**kwargs)
    def test_constructor_does_not_capture_and_start_requires_explicit_target(self):
        self.assertEqual(self.sessions,[])
        with self.assertRaises(CaptureStreamError):self.read()
        session=self.start();self.stream.start()
        self.assertEqual(len(self.sessions),1)
        self.assertEqual(session.kwargs['window_hwnd'],11)
        self.assertFalse(session.kwargs['cursor_capture']);self.assertIsNone(session.kwargs['draw_border'])
        self.assertEqual(session.kwargs['minimum_update_interval'],100)
        self.assertNotIn('monitor_index',session.kwargs)
    def test_native_view_is_copied_and_only_latest_owned_frame_is_retained(self):
        session=self.start();native=session.emit(100.)
        first=self.read();native.pixels[:]=bytes(len(native.pixels))
        self.assertEqual(first.bgra[:4],bytes([30,60,200,255]))
        with self.assertRaises(FrozenInstanceError):first.sequence=9
        self.now=100.1;session.emit(100.1)
        latest=self.read(after_sequence=first.sequence)
        self.assertEqual(latest.sequence,2);self.assertEqual(self.stream.frames_published,2)
        self.assertNotEqual(first.sequence,latest.sequence)
        self.assertEqual(first.captured_monotonic,100.)
    def test_reading_cache_does_not_refresh_its_capture_time(self):
        self.start().emit(100.);first=self.read();self.now=100.2
        again=self.read();self.assertEqual(first.captured_at,again.captured_at)
        self.now=100.5
        with self.assertRaises(CaptureStreamError) as raised:self.read()
        self.assertEqual(raised.exception.code,'capture_stream_timeout')
    def test_not_before_uses_native_capture_time_instead_of_callback_arrival(self):
        self.start().emit(99.9)
        with self.assertRaises(CaptureStreamError):self.read(not_before=100.)
        self.now=100.1;self.sessions[0].emit(100.05)
        frame=self.read(not_before=100.)
        self.assertEqual(frame.captured_monotonic,100.05)
        self.assertEqual(frame.arrived_monotonic,100.1)
    def test_repeated_or_older_native_timestamp_never_gets_a_new_sequence(self):
        session=self.start();session.emit(100.);self.now=100.1
        session.emit(100.);session.emit(99.99)
        self.assertEqual(self.stream.latest_sequence,1)
        with self.assertRaises(CaptureStreamError):self.read(after_sequence=1)
    def test_black_and_future_frames_never_become_evidence(self):
        session=self.start();session.emit(100.,pixels=bytes([0,0,0,255])*4);session.emit(105.)
        self.assertEqual(self.stream.frames_published,0)
        with self.assertRaises(CaptureStreamError):self.read()
    def test_resize_closes_and_clears_old_frame(self):
        session=self.start();session.emit(100.);self.now=100.1;session.emit(100.1,width=3)
        self.assertTrue(self.stream.closed)
        with self.assertRaises(CaptureStreamError) as raised:self.read()
        self.assertEqual(raised.exception.code,'geometry_changed')
    def test_guard_and_cancel_are_checked_before_exposing_a_frame(self):
        self.start().emit(100.);cancel=threading.Event();cancel.set()
        with self.assertRaises(CaptureStreamError) as raised:self.read(cancel_event=cancel)
        self.assertEqual(raised.exception.code,'capture_cancelled')
        checks=[]
        def guard():
            checks.append(1)
            if len(checks)==2:raise RuntimeError('permission revoked')
        with self.assertRaisesRegex(RuntimeError,'permission revoked'):self.read(guard=guard)
        self.assertEqual(len(checks),2)
    def test_start_guard_denies_session_creation(self):
        def guard():raise RuntimeError('denied')
        with self.assertRaisesRegex(RuntimeError,'denied'):self.stream.start(guard)
        self.assertEqual(self.sessions,[])
    def test_close_is_nonblocking_even_if_native_join_blocks_and_late_frames_are_ignored(self):
        session=self.start();session.emit(100.)
        gate=threading.Event();session.control.stop_gate=gate
        started=time.perf_counter();self.stream.close();elapsed=time.perf_counter()-started
        self.assertLess(elapsed,.1);self.assertTrue(session.control.entered.wait(.5))
        self.now=100.1;session.emit(100.1)
        self.assertEqual(self.stream.frames_published,1)
        with self.assertRaises(CaptureStreamError):self.read()
        gate.set();self.assertTrue(self.stream.wait_closed(.5))
    def test_close_wakes_waiting_reader_without_waiting_for_a_new_native_frame(self):
        self.start();errors=[];entered=threading.Event()
        def read():
            entered.set()
            try:self.stream.read_frame(timeout=2)
            except CaptureStreamError as error:errors.append(error.code)
        thread=threading.Thread(target=read);thread.start();entered.wait(.5);self.stream.close();thread.join(.5)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,['capture_stream_closed'])
    def test_peek_returns_non_actionable_rgb_sample_without_writing_a_file(self):
        self.start().emit(100.)
        sample=self.stream.peek_sample(self.info,lambda *_:(0,0),timeout=0)
        self.assertEqual(len(sample['sample_rgb']),64*36*3)
        self.assertFalse(sample['actionable']);self.assertNotIn('path',sample)
        self.assertEqual(sample['captured_monotonic'],100.)
        self.assertEqual(sample['client_rect'],{'left':0,'top':0,'width':2,'height':2})
    def test_qt_export_is_lossless_and_keeps_original_frame_age(self):
        self.start().emit(100.)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'frame.png';frame=self.read();self.now=100.2
            result=export_frame(frame,path,self.info,lambda *_:(0,0))
            image=QImage(str(path));color=image.pixelColor(0,0)
            self.assertEqual((color.red(),color.green(),color.blue(),color.alpha()),(200,60,30,255))
            self.assertEqual(result['captured_monotonic'],100.)
            self.assertEqual(result['captured_at'],frame.captured_at)
            self.assertEqual(result['frame_sequence'],1)
    def test_export_rejects_other_pid_or_geometry_without_creating_output(self):
        self.start().emit(100.);frame=self.read()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'bad.png';self.info.pid=23
            with self.assertRaises(CaptureStreamError):export_frame(frame,path,self.info,lambda *_:(0,0))
            self.assertFalse(path.exists())


if __name__=='__main__':unittest.main()

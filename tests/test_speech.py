"""Offline voice lifecycle checks. All listening processes are test doubles.

These tests never open a microphone, change Windows permissions, or use a
network. A separate --probe / synthesized-WAV check exercises the real bridge.
"""
import io
import json
import os
from pathlib import Path
import queue
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from shu.speech import SpeechInput, probe_capabilities, request_dictation


class FakeOutput:
    def __init__(self):self.items=queue.Queue();self.closed=False
    def emit(self,event):self.items.put(json.dumps(event)+'\n')
    def __iter__(self):
        while True:
            line=self.items.get()
            if line is None:return
            yield line
    def close(self):self.closed=True


class FakeProcess:
    def __init__(self):
        self.stdin=io.StringIO();self.stdout=FakeOutput()
        self.returncode=None;self.ended=threading.Event();self.terminated=False
    def finish(self,code=0):
        if self.returncode is None:
            self.returncode=code;self.stdout.items.put(None);self.ended.set()
    def terminate(self):self.terminated=True;self.finish(-15)
    def kill(self):self.terminate()
    def poll(self):return self.returncode
    def wait(self,timeout=None):
        if not self.ended.wait(timeout):raise subprocess.TimeoutExpired('fake-voice',timeout)
        return self.returncode


class SpeechTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        if isinstance(cls.app,QApplication):cls.app.setQuitOnLastWindowClosed(False)
    def setUp(self):
        self.temp=TemporaryDirectory();self.path=Path(self.temp.name)/'helper.exe';self.path.touch()
        self.process=FakeProcess();self.created=threading.Event();self.calls=[]
        def factory(*args,**kwargs):
            self.calls.append((args,kwargs));self.created.set();return self.process
        self.voice=SpeechInput(path=self.path,process_factory=factory)
        self.recordings=[];self.levels=[];self.transcripts=[];self.errors=[]
        self.voice.recording_changed.connect(self.recordings.append)
        self.voice.level_changed.connect(self.levels.append)
        self.voice.transcript.connect(self.transcripts.append)
        self.voice.error.connect(self.errors.append)
    def tearDown(self):
        self.voice.cancel();self.process.finish();self.app.processEvents()
        self.voice.deleteLater();self.app.processEvents();self.temp.cleanup()
    def until(self,condition,timeout=2):
        limit=time.monotonic()+timeout
        while not condition() and time.monotonic()<limit:
            self.app.processEvents();time.sleep(.005)
        self.app.processEvents();self.assertTrue(condition())
    def start(self):
        with patch('shu.speech.sys.platform','win32'):self.assertTrue(self.voice.start())
        self.until(self.created.is_set)
    def ready(self):
        self.start();self.process.stdout.emit({'type':'ready'})
        self.until(lambda:self.voice.state=='recording')

    def test_construction_never_starts_process_or_opens_microphone(self):
        self.assertEqual(self.calls,[]);self.assertEqual(self.voice.state,'idle')
        self.assertFalse(self.voice.is_recording);self.assertEqual(self.recordings,[])

    def test_start_waits_for_helper_readiness_before_showing_recording(self):
        self.start()
        self.assertTrue(self.voice.is_recording);self.assertEqual(self.voice.state,'starting')
        self.assertEqual(self.recordings,[])
        args,kwargs=self.calls[0]
        self.assertEqual(args[0],[str(self.path),'--listen'])
        self.assertIn('creationflags',kwargs)
        self.process.stdout.emit({'type':'ready'})
        self.until(lambda:self.recordings==[True])
        self.assertFalse(self.voice.start(),'Two clicks must not open two microphones')
        self.assertEqual(len(self.calls),1)

    def test_stop_requests_final_text_without_sending_it_as_chat(self):
        self.ready();self.voice.stop()
        self.assertIn('stop\n',self.process.stdin.getvalue())
        self.assertEqual(self.voice.state,'stopping');self.assertEqual(self.recordings,[True,False])
        self.assertEqual(self.transcripts,[])
        self.process.stdout.emit({'type':'result','text':' 请打开基建页面。 '});self.process.finish()
        self.until(lambda:self.transcripts==['请打开基建页面。'])
        self.assertEqual(self.voice.state,'idle');self.assertEqual(self.errors,[])

    def test_empty_transcript_is_reported_as_no_speech(self):
        self.ready();self.process.stdout.emit({'type':'result','text':' '});self.process.finish()
        self.until(lambda:bool(self.errors));self.assertIn('没有识别',self.errors[0])
        self.assertEqual(self.transcripts,[]);self.assertEqual(self.recordings,[True,False])

    def test_cancel_terminates_helper_and_discards_late_text(self):
        self.ready();generation=self.voice._generation;self.voice.cancel()
        self.assertTrue(self.process.terminated);self.assertEqual(self.voice.state,'idle')
        self.voice._on_event(generation,{'type':'result','text':'迟到的转写'})
        self.voice._on_event(generation,{'type':'ready'})
        self.assertEqual(self.transcripts,[]);self.assertEqual(self.recordings,[True,False])

    def test_levels_are_clamped_and_nonfinite_levels_rejected(self):
        self.ready();generation=self.voice._generation
        for value in [0,.6,100,-1,'bad',float('nan'),float('inf')]:
            self.voice._on_event(generation,{'type':'level','value':value})
        self.assertEqual(self.levels,[0.0,.6,1.0,0.0])

    def test_permission_failure_never_claims_recording_or_changes_settings(self):
        self.start();self.process.stdout.emit({'type':'error','code':'microphone_denied'});self.process.finish(1)
        self.until(lambda:bool(self.errors))
        self.assertEqual(self.recordings,[]);self.assertIn('拒绝',self.errors[0])
        self.assertEqual(self.voice.state,'idle')

    def test_timeout_cancels_recording_and_disallows_late_result(self):
        self.ready();generation=self.voice._generation;self.voice._timeout()
        self.assertTrue(self.process.terminated);self.assertEqual(self.voice.state,'idle')
        self.voice._on_event(generation,{'type':'result','text':'迟到'})
        self.assertEqual(self.transcripts,[]);self.assertIn('超时',self.errors[0])

    def test_abnormal_exit_clears_recording_and_reports_one_error(self):
        self.ready();self.process.finish(1)
        self.until(lambda:bool(self.errors))
        self.assertEqual(len(self.errors),1);self.assertEqual(self.voice.state,'idle')
        self.assertEqual(self.recordings,[True,False])

    def test_missing_helper_does_not_start_any_process(self):
        self.voice._path=Path(self.temp.name)/'missing.exe'
        self.assertFalse(self.voice.start());self.assertEqual(self.calls,[])
        self.assertEqual(self.voice.state,'idle');self.assertEqual(len(self.errors),1)

    def test_readonly_probe_uses_only_probe_mode(self):
        runner=Mock(return_value=subprocess.CompletedProcess([],0,json.dumps({
            'type':'capabilities','available':True,'languages':['zh-CN']})))
        with patch('shu.speech.sys.platform','win32'):
            result=probe_capabilities(self.path,runner=runner)
        self.assertTrue(result['available']);self.assertFalse(result['microphone_opened'])
        self.assertEqual(runner.call_args.args[0],[str(self.path),'--probe'])

    def test_invalid_probe_output_and_legacy_dictation_never_send_shortcut(self):
        runner=Mock(return_value=subprocess.CompletedProcess([],1,'bad json'))
        with patch('shu.speech.sys.platform','win32'):
            result=probe_capabilities(self.path,runner=runner)
        self.assertFalse(result['available']);self.assertFalse(result['microphone_opened'])
        self.assertFalse(request_dictation()['requested'])


if __name__=='__main__':unittest.main()

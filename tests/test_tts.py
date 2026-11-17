import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from PySide6.QtCore import QObject, Signal, QProcess
from PySide6.QtWidgets import QApplication

class Process(QObject):
    started=Signal(); finished=Signal(int, QProcess.ExitStatus); errorOccurred=Signal(QProcess.ProcessError)
    def __init__(self,parent=None):
        super().__init__(parent); self.running=False; self.output=b''; self.input=b''; self.killed=False
    def setProcessEnvironment(self,e): pass
    def setCreateProcessArgumentsModifier(self,fn): pass
    def setProgram(self,p): self.program=p
    def setArguments(self,a): self.args=a
    def start(self): self.running=True
    def write(self,b): self.input+=bytes(b)
    def closeWriteChannel(self): pass
    def kill(self): self.killed=True  # Deliberately delayed OS termination
    def state(self): return QProcess.ProcessState.Running if self.running else QProcess.ProcessState.NotRunning
    def readAllStandardOutput(self): return self.output
    def readAllStandardError(self): return b''
    def finish(self):
        self.running=False; self.finished.emit(0,QProcess.ExitStatus.NormalExit)

class Player:
    def __init__(self): self.paths=[]; self.position=0.; self.playing=False
    def play(self,path): self.paths.append(path); self.position=0.; self.playing=True
    def stop(self): self.playing=False
    def status(self): return self.position,self.playing

class SpeechTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('shu.tts'),'Speech output feature missing')
        from shu.tts import SpeechOutput, prepare_text
        self.SpeechOutput=SpeechOutput; self.prepare=prepare_text
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.runtime=self.root/'runtime'; self.runtime.mkdir()
        (self.runtime/'models').mkdir()
        for name in ('model','voices','config'): (self.runtime/'models'/name).write_bytes(b'x')
        (self.runtime/'python.exe').write_bytes(b'x')
        (self.runtime/'pythonw.exe').write_bytes(b'x')
        self.manifest=dict(ready=True,python=str(self.runtime/'python.exe'),model='models/model',voices='models/voices',config='models/config',provider='CUDAExecutionProvider',voice='zf_001',speed=.95)
        self.save_manifest(); self.processes=[]; self.player=Player()
    def save_manifest(self): (self.runtime/'install.json').write_text(json.dumps(self.manifest))
    def create(self):
        def factory(parent):
            p=Process(parent); self.processes.append(p); return p
        self.s=self.SpeechOutput(runtime_root=self.runtime,resource_root=self.root,data_dir=self.root/'data',process_factory=factory,player=self.player)
        self.addCleanup(self.s.close); return self.s
    def complete(self,p):
        import wave
        path=Path(p.args[p.args.index('--output')+1]); path.parent.mkdir(parents=True,exist_ok=True)
        with wave.open(str(path),'wb') as w: w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b'\x01\x00'*240)
        p.output=json.dumps(dict(ok=True,path=str(path),provider='CUDAExecutionProvider')).encode(); p.finish()
    def test_bounded_markdown_and_code(self):
        self.assertEqual(self.prepare('你好 [朋友](https://example.com)\n```py\nprint(1)\n``` **再见**'),'你好 朋友 再见')
        self.assertEqual(len(self.prepare('黍'*1000)),220)
        self.assertEqual(self.prepare('```code```\x00'), '')
    def test_no_start_until_fresh_assistant(self):
        s=self.create(); self.assertTrue(s.enabled); self.assertTrue(s.ready); self.assertEqual(self.processes,[])
        s.handle_message('system','你好'); s.handle_message('user','你好'); self.assertEqual(self.processes,[])
        s.handle_message('assistant','你好'); self.assertEqual(len(self.processes),1)
        p=self.processes[0]; p.started.emit(); self.assertEqual(json.loads(p.input)['text'],'你好')
        self.complete(p); self.assertEqual(len(self.player.paths),1)
    def test_cancel_starting_and_late_completion_cannot_play(self):
        s=self.create(); s.handle_message('assistant','旧回复'); p=self.processes[0]
        s.stop(); p.started.emit(); self.assertEqual(p.input,b''); self.complete(p)
        self.assertEqual(self.player.paths,[])
    def test_latest_waits_for_termination_and_replaces_pending(self):
        s=self.create(); s.handle_message('assistant','旧'); p=self.processes[0]
        s.handle_message('assistant','中'); s.handle_message('assistant','新')
        self.assertEqual(len(self.processes),1); self.complete(p)
        self.assertEqual(len(self.processes),2); q=self.processes[1]; q.started.emit()
        self.assertEqual(json.loads(q.input)['text'],'新'); self.complete(q)
        self.assertEqual(len(self.player.paths),1)
    def test_disable_persists_and_cancels(self):
        s=self.create(); s.handle_message('assistant','旧'); s.set_enabled(False); self.complete(self.processes[0]); self.assertEqual(self.player.paths,[])
        s.close(); other=self.create(); self.assertFalse(other.enabled)
    def test_windows_launch_uses_windowless_python_when_available(self):
        import sys
        if sys.platform!='win32': self.skipTest('Windows runtime selection')
        windowless=self.runtime/'pythonw.exe'; windowless.write_bytes(b'x')
        s=self.create(); s.preview()
        self.assertEqual(self.processes[0].program,str(windowless))

    def test_invalid_runtime_fails_without_start(self):
        self.manifest['model']='../outside'; self.save_manifest(); s=self.create(); self.assertFalse(s.ready)
        errors=[]; s.error.connect(errors.append); s.preview(); self.assertTrue(errors); self.assertEqual(self.processes,[])

    def test_audio_device_stop_failure_still_kills_worker(self):
        s=self.create(); s.handle_message('assistant','旧回复'); p=self.processes[0]
        def broken_stop(): raise RuntimeError('audio device unavailable')
        self.player.stop=broken_stop
        s.stop()
        self.assertTrue(p.killed)
        self.player.stop=lambda:None
        self.complete(p)
        self.assertEqual(self.player.paths,[])

    def test_audio_device_play_failure_does_not_escape_or_skip_cleanup(self):
        import wave
        s=self.create(); s.handle_message('assistant','新回复'); p=self.processes[0]
        path=Path(p.args[p.args.index('--output')+1])
        with wave.open(str(path),'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000); wav.writeframes(b'\x01\x00'*240)
        p.output=json.dumps(dict(ok=True,path=str(path),provider='CUDAExecutionProvider')).encode()
        def broken_play(_path): raise RuntimeError('audio device unavailable')
        self.player.play=broken_play
        errors=[]; s.error.connect(errors.append)
        s._finished(p,path,s._generation,0)
        self.assertIsNone(s._process)
        self.assertTrue(errors)

    def test_voice_selection_persists_and_changes_worker_request(self):
        s=self.create()
        self.assertTrue(hasattr(s,'voices'),'Voice choices missing')
        self.assertGreaterEqual(len(s.voices),6)
        chosen=s.voices[2]['id']; s.set_voice(chosen); s.preview()
        p=self.processes[0]; p.started.emit()
        self.assertEqual(json.loads(p.input)['voice'],chosen)
        s.close(); other=self.create(); self.assertEqual(other.voice_id,chosen)
        with self.assertRaises(ValueError): other.set_voice('../../untrusted')

    def test_captions_wait_for_audio_and_follow_player_position(self):
        import wave
        s=self.create(); self.assertTrue(hasattr(s,'caption'),'Playback captions missing')
        captions=[]; finished=[]; s.caption.connect(captions.append); s.playback_finished.connect(lambda:finished.append(True))
        s.handle_message('assistant','第一句。第二句。'); p=self.processes[0]
        self.assertEqual(captions,[],'No reply subtitle before actual playback')
        path=Path(p.args[p.args.index('--output')+1])
        with wave.open(str(path),'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes(b'\x01\x00'*48000)
        cues=[{'text':'第一句。','start':0.,'end':1.},{'text':'第二句。','start':1.,'end':2.}]
        p.output=json.dumps(dict(ok=True,path=str(path),provider='CUDAExecutionProvider',cues=cues)).encode();p.finish()
        s._poll_playback(); self.assertEqual(captions,['第一句。'])
        self.player.position=.8;s._poll_playback();self.assertEqual(len(captions),1)
        self.player.position=1.2;s._poll_playback();self.assertEqual(captions[-1],'第二句。')
        self.player.position=2.;self.player.playing=False;s._poll_playback();self.assertEqual(finished,[True])

    def test_cancel_prevents_stale_playback_captions(self):
        s=self.create();self.assertTrue(hasattr(s,'caption'),'Playback captions missing')
        captions=[];s.caption.connect(captions.append)
        s.preview();p=self.processes[0];self.complete(p)
        s.stop();count=len(captions);self.player.position=100;s._poll_playback()
        self.assertEqual(len(captions),count)

    def test_worker_start_failure_releases_waiting_caption(self):
        s=self.create();captions=[];finished=[]
        s.caption.connect(captions.append);s.playback_finished.connect(lambda:finished.append(True))
        def unavailable(_parent):raise OSError('worker unavailable')
        s._factory=unavailable;s.handle_message('assistant','仍然可以读到文字。')
        self.assertFalse(s._waiting)
        self.assertEqual(captions,['仍然可以读到文字。'])
        self.assertEqual(finished,[True])

if __name__=='__main__': unittest.main()

"""Lightweight speech orchestration; inference stays out of Qt."""
import json
import math
from pathlib import Path
import re
import sys
import uuid
import wave
from PySide6.QtCore import QObject, Signal, QProcess, QProcessEnvironment, QTimer
from .audio_player import WavPlayer


def prepare_text(text):
    text=re.sub(r'```.*?(?:```|$)|~~~.*?(?:~~~|$)',' ',str(text)[:16000],flags=re.S)
    text=re.sub(r'!?\[([^\]]*)\]\([^)]*\)',r'\1',text); text=re.sub(r'https?://\S+',' ',text)
    text=re.sub(r'<[^>]*>|[`*_#>|]',' ',text); text=''.join(c if c.isprintable() else ' ' for c in text)
    return ' '.join(text.split())[:220]


def discover_runtime(resource_root):
    roots=[Path(resource_root).resolve(),Path(sys.executable).resolve().parent]
    for base in roots:
        for parent in [base,*list(base.parents)[:5]]:
            clone=parent/'.shu-voice-eval'
            if (clone/'install.json').is_file():return clone
            candidate=parent/'.shu-tts'
            if (candidate/'install.json').is_file(): return candidate
    return roots[0]/'.shu-tts'


class LegacySpeechOutput(QObject):
    notice=Signal(str); error=Signal(str)
    caption=Signal(str); preparing=Signal(); playback_finished=Signal(); playback_cancelled=Signal()
    def __init__(self,parent=None,*,runtime_root=None,resource_root=None,data_dir=None,process_factory=None,player=None):
        super().__init__(parent); self.resource_root=Path(resource_root or Path(__file__).resolve().parents[1]); self.runtime_root=Path(runtime_root or discover_runtime(self.resource_root)).resolve()
        self.data_dir=Path(data_dir or self.resource_root/'data'); self._prefs=self.data_dir/'voice-output.json'; self._cache=self.data_dir/'voice-cache'
        self._factory=process_factory or QProcess; self._player=player or WavPlayer(); self._process=None; self._pending=None; self._generation=0; self._closed=False; self._manifest=None
        catalog=self.resource_root/'assets/tts_voices.json'
        if not catalog.is_file():catalog=Path(__file__).resolve().parents[1]/'assets/tts_voices.json'
        self.voices=json.loads(catalog.read_text(encoding='utf-8'));self.voice_id=self.voices[0]['id']
        self._speaking=False;self._waiting=False;self._cues=[];self._cue_index=-1
        self._play_timer=QTimer(self);self._play_timer.setInterval(40);self._play_timer.timeout.connect(self._poll_playback)
        try:
            m=json.loads((self.runtime_root/'install.json').read_text(encoding='utf-8-sig')); python=Path(m['python'])
            if m.get('ready') is not True or m.get('provider')!='CUDAExecutionProvider' or not python.is_absolute() or not python.is_file(): raise ValueError()
            self._python=python.with_name('pythonw.exe') if sys.platform=='win32' else python
            if not self._python.is_file(): raise ValueError()
            for key in ('model','voices','config'):
                path=(self.runtime_root/m[key]).resolve()
                if not path.is_relative_to(self.runtime_root) or not path.is_file(): raise ValueError()
            self._manifest=m
        except (OSError,ValueError,KeyError,TypeError): pass
        self._enabled=self.ready
        try:
            preferences=json.loads(self._prefs.read_text(encoding='utf-8'))
            self._enabled=preferences['enabled'] is True
            if preferences.get('voice') in {v['id'] for v in self.voices}:self.voice_id=preferences['voice']
        except (OSError,ValueError,KeyError,TypeError): pass
        self._timeout=QTimer(self); self._timeout.setSingleShot(True); self._timeout.setInterval(120000); self._timeout.timeout.connect(self._timed_out); self._prune()
    @property
    def ready(self): return self._manifest is not None
    @property
    def enabled(self): return self._enabled
    def _save_preferences(self):
        try:
            self.data_dir.mkdir(parents=True,exist_ok=True);temporary=self._prefs.with_suffix('.tmp')
            temporary.write_text(json.dumps({'enabled':self.enabled,'voice':self.voice_id}),encoding='utf-8');temporary.replace(self._prefs)
        except OSError:self.error.emit('朗读偏好未能保存。')
    def set_voice(self,voice):
        if voice not in {v['id'] for v in self.voices}:raise ValueError('Unknown voice')
        self.stop();self.voice_id=voice;self._save_preferences()
    def set_enabled(self,enabled):
        self._enabled=bool(enabled)
        if not enabled: self.stop()
        self._save_preferences()
        if enabled and not self.ready: self.error.emit('本地 GPU 语音尚未安装完成，文字交流仍可使用。')
    def handle_message(self,role,text):
        if role=='user': self.stop()
        elif role=='assistant' and self.enabled: self._request(text)
    def preview(self): self._request('你好，我是黍。慢慢来，我会陪着你，把眼前的事情一件件做好。')
    def _request(self,text):
        if self._closed: return
        self.stop(); text=prepare_text(text)
        if not text: return
        if not self.ready: self.error.emit('本地 GPU 语音尚未安装完成，文字交流仍可使用。'); return
        profile=next(v for v in self.voices if v['id']==self.voice_id)
        self._pending=(self._generation,text,self.voice_id,profile['speed']);self._waiting=True;self.preparing.emit()
        if self._process is None: self._launch()
    def _launch(self):
        if self._pending is None or self._closed: return
        generation,text,voice,speed=self._pending; self._pending=None
        try:
            self._cache.mkdir(parents=True,exist_ok=True); path=self._cache/(uuid.uuid4().hex+'.wav'); p=self._factory(self); self._process=p
            env=QProcessEnvironment.systemEnvironment()
            for key,value in {'PYTHONIOENCODING':'utf-8','PYTHONUTF8':'1','OMP_NUM_THREADS':'2','OPENBLAS_NUM_THREADS':'2','MKL_NUM_THREADS':'2'}.items(): env.insert(key,value)
            temporary=self.runtime_root/'tmp'; temporary.mkdir(exist_ok=True)
            env.insert('TEMP',str(temporary)); env.insert('TMP',str(temporary))
            p.setProcessEnvironment(env)
            # pythonw is the Windows GUI subsystem executable. QProcess supplies
            # redirected pipes; verified stdin/stdout/stderr work without a console.
            p.setProgram(str(self._python)); p.setArguments([str(self.resource_root/'assets/tts_worker.py'),'--runtime',str(self.runtime_root),'--output',str(path)])
            def started():
                if generation!=self._generation or self._closed: p.kill(); return
                p.write(json.dumps({'text':text,'voice':voice,'speed':speed},ensure_ascii=False).encode('utf-8')); p.closeWriteChannel()
            p.started.connect(started); p.finished.connect(lambda code,status:self._finished(p,path,generation,code,text)); p.errorOccurred.connect(lambda error:self._process_error(p,path,generation,error))
            self.notice.emit('正在生成本地 GPU 语音…'); self._timeout.start(); p.start()
        except (OSError,RuntimeError):
            self._process=None;self._waiting=False;self._timeout.stop()
            self.error.emit('无法启动本地 GPU 语音，文字交流仍可使用。')
            self.caption.emit(text);self.playback_finished.emit()
    def _process_error(self,p,path,generation,error):
        if error==QProcess.ProcessError.FailedToStart: self._finished(p,path,generation,1)
    def _finished(self,p,path,generation,code,spoken_text=''):
        if self._process is not p: return
        self._timeout.stop(); self._process=None
        try:
            if generation==self._generation and not self._closed:
                lines=bytes(p.readAllStandardOutput()).decode('utf-8',errors='replace').splitlines(); result=json.loads(lines[-1]) if lines else {}
                if code or result.get('ok') is not True or result.get('provider')!='CUDAExecutionProvider' or Path(result.get('path','')).resolve()!=path.resolve(): raise ValueError()
                with wave.open(str(path),'rb') as wav:
                    if not wav.getnframes() or wav.getnchannels()!=1: raise ValueError()
                    duration=wav.getnframes()/wav.getframerate()
                cues=result.get('cues') or [{'text':spoken_text,'start':0.,'end':duration}]
                previous=0.
                for cue in cues:
                    if (not isinstance(cue.get('text'),str) or not all(isinstance(cue.get(k),(int,float)) and math.isfinite(cue[k]) for k in ('start','end'))
                        or cue['start']<previous-.001 or cue['end']<=cue['start'] or cue['end']>duration+.02):raise ValueError('Invalid caption timing')
                    previous=cue['end']
                try:
                    self._player.play(path)
                except (RuntimeError,OSError):
                    self.error.emit('系统音频设备无法播放，文字交流仍可使用。')
                else:
                    self._waiting=False;self._speaking=True;self._cues=cues;self._cue_index=-1;self._play_timer.start();self._poll_playback()
                    self.notice.emit('正在朗读黍的回复。')
            else: path.unlink(missing_ok=True)
        except (OSError,ValueError,EOFError,wave.Error): path.unlink(missing_ok=True); self.error.emit('GPU 语音生成失败，请检查 CUDA 运行环境或显存；文字交流仍可使用。')
        p.deleteLater(); self._prune(); self._launch()
        if not self._speaking and generation==self._generation:
            self._waiting=False
            if spoken_text:self.caption.emit(spoken_text)
            self.playback_finished.emit()
    def _poll_playback(self):
        if not self._speaking:return
        try:position,playing=self._player.status()
        except (OSError,RuntimeError,ValueError):
            self.stop();self.error.emit('无法读取音频播放进度，已停止播报。');return
        if not playing:
            self._speaking=False;self._play_timer.stop()
            try:self._player.stop()
            except (OSError,RuntimeError):pass
            self.playback_finished.emit();return
        index=next((i for i,cue in enumerate(self._cues) if cue['start']<=position<cue['end']),-1)
        if index>=0 and index!=self._cue_index:
            self._cue_index=index;self.caption.emit(self._cues[index]['text'])
    def _timed_out(self): self.stop(); self.error.emit('GPU 语音生成超时，已停止此次朗读。')
    def stop(self):
        active=self._waiting or self._speaking;self._waiting=False;self._speaking=False;self._play_timer.stop();self._cues=[]
        self._generation+=1; self._pending=None; self._timeout.stop()
        try:
            self._player.stop()
        except (RuntimeError,OSError):
            self.error.emit('系统音频设备未响应停止请求。')
        finally:
            if self._process is not None: self._process.kill()
        if active:self.playback_cancelled.emit()
    def close(self):
        self._closed=True; self.stop()
        if self._process is not None and hasattr(self._process,'waitForFinished'): self._process.waitForFinished(1500)
        self._prune()
    def _prune(self):
        try:
            files=sorted(self._cache.glob('*.wav'),key=lambda p:p.stat().st_mtime,reverse=True); total=0
            for index,path in enumerate(files):
                total+=path.stat().st_size
                if index>=6 or total>20_000_000: path.unlink(missing_ok=True)
        except OSError: pass


def SpeechOutput(*args,**kwargs):
    """Choose the installed streaming clone without importing ML into Qt."""
    resource_root=kwargs.get('resource_root') or Path(__file__).resolve().parents[1]
    runtime=Path(kwargs.get('runtime_root') or discover_runtime(resource_root))
    try:
        manifest=json.loads((runtime/'install.json').read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):manifest={}
    if not isinstance(manifest,dict):manifest={}
    if manifest.get('backend')=='moss-nano':
        from .moss_tts import MossSpeechOutput
        return MossSpeechOutput(*args,**dict(kwargs,runtime_root=runtime))
    return LegacySpeechOutput(*args,**kwargs)

"""Offline, app-owned Chinese voice input; no Windows dictation overlay.

Only explicit start() opens the microphone through an installed SAPI recognizer.
Audio stays local and is never saved or sent to the model. stop() submits the
recognized text; cancel() discards it and all late callbacks.
"""
from __future__ import annotations
import json
import math
from pathlib import Path
import subprocess
import sys
import threading
from typing import Callable
from PySide6.QtCore import QObject, QTimer, Signal, Slot

ERROR_MESSAGES={
 'missing_recognizer':'未找到本机中文语音识别器，当前无法离线转写。仍可直接打字。',
 'microphone_denied':'Windows 拒绝了麦克风访问。请自行检查桌面应用的麦克风权限后重试。',
 'microphone_unavailable':'无法打开默认麦克风，请检查设备连接及是否被其他应用占用。',
 'recognizer_unavailable':'本机离线中文识别器未能启动，请检查 Windows 中文语音组件。',
 'no_speech':'没有识别到清晰语音，请重试或直接打字。',
 'recognition_failed':'本次语音识别未完成，请重试。',
}

def helper_path()->Path:
    root=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1]))
    return root/'assets/voice-helper.exe'

def probe_capabilities(path:Path|None=None,*,runner:Callable=subprocess.run)->dict:
    """Explicit read-only probe; the helper never opens audio in --probe mode."""
    selected=Path(path) if path is not None else helper_path()
    if sys.platform!='win32' or not selected.is_file():
        return {'available':False,'offline':True,'microphone_opened':False,
                'message':'离线语音组件不可用，请使用完整 Windows 发行包。'}
    try:
        result=runner([str(selected),'--probe'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                      text=True,encoding='utf-8',timeout=8,
                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        record=json.loads(result.stdout.strip().splitlines()[-1])
        if record.get('type')!='capabilities':raise ValueError()
        return {'available':record.get('available') is True,'offline':True,'microphone_opened':False,
                'languages':record.get('languages',[]),
                'message':'使用本机离线中文识别；点击录音后才打开麦克风。' if record.get('available') else ERROR_MESSAGES['missing_recognizer']}
    except (OSError,ValueError,IndexError,subprocess.SubprocessError):
        return {'available':False,'offline':True,'microphone_opened':False,
                'message':ERROR_MESSAGES['recognizer_unavailable']}

class SpeechInput(QObject):
    recording_changed=Signal(bool)
    level_changed=Signal(float)
    transcript=Signal(str)
    error=Signal(str)
    _event=Signal(int,dict)

    def __init__(self,parent=None,*,path:Path|None=None,process_factory:Callable|None=None):
        super().__init__(parent)
        self._path=Path(path) if path is not None else helper_path()
        self._factory=process_factory or subprocess.Popen
        self._state='idle';self._generation=0;self._process=None
        self._process_lock=threading.Lock()
        self._cancelled=threading.Event();self._stopping=threading.Event()
        self._reported_recording=False;self._event.connect(self._on_event)
        self._deadline=QTimer(self);self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(self._timeout)
    @property
    def is_recording(self)->bool:return self._state in ('starting','recording')
    @property
    def state(self)->str:return self._state
    def start(self)->bool:
        if self._state!='idle':return False
        if sys.platform!='win32' or not self._path.is_file():
            self.error.emit('离线语音组件不可用，请完整解压新版 Windows 发行包。');return False
        self._generation+=1;generation=self._generation
        self._cancelled,self._stopping=threading.Event(),threading.Event()
        cancelled,stopping=self._cancelled,self._stopping
        self._state='starting';self._deadline.start(12000)
        def run():
            process=None
            try:
                if cancelled.is_set():return
                process=self._factory([str(self._path),'--listen'],stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,
                    encoding='utf-8',errors='replace',bufsize=1,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                with self._process_lock:
                    if not cancelled.is_set():self._process=process
                if cancelled.is_set():process.terminate();return
                if stopping.is_set():self._write_command(process,'stop')
                for line in process.stdout:
                    if cancelled.is_set():break
                    if len(line)>65536:raise ValueError('oversized speech event')
                    try:event=json.loads(line)
                    except (ValueError,TypeError):continue
                    if isinstance(event,dict):self._event.emit(generation,event)
                code=process.wait(timeout=3)
                if not cancelled.is_set():self._event.emit(generation,{'type':'exit','code':code})
            except (OSError,ValueError,subprocess.SubprocessError):
                if not cancelled.is_set():self._event.emit(generation,{'type':'error','code':'recognition_failed'})
            finally:
                if process is not None:
                    with self._process_lock:
                        if self._process is process:self._process=None
                    try:
                        if process.poll() is None:process.terminate()
                        process.wait(timeout=2)
                    except (OSError,subprocess.SubprocessError):
                        try:process.kill()
                        except OSError:pass
                    for pipe in (getattr(process,'stdin',None),getattr(process,'stdout',None)):
                        if pipe:
                            try:pipe.close()
                            except OSError:pass
        threading.Thread(target=run,name='shu-offline-speech',daemon=True).start()
        return True
    @staticmethod
    def _write_command(process,command:str)->None:
        try:
            if process.stdin is not None:process.stdin.write(command+'\n');process.stdin.flush()
        except (OSError,ValueError):pass
    def stop(self)->None:
        if self._state not in ('starting','recording'):return
        self._state='stopping';self._stopping.set();self._deadline.start(5000)
        with self._process_lock:process=self._process
        if process is not None:self._write_command(process,'stop')
        self._set_recording(False)
    def cancel(self)->None:
        self._generation+=1;self._cancelled.set()
        with self._process_lock:process,self._process=self._process,None
        if process is not None:
            try:process.terminate()
            except OSError:pass
        self._finish()
    def _set_recording(self,active:bool)->None:
        if self._reported_recording!=active:self._reported_recording=active;self.recording_changed.emit(active)
        if not active:self.level_changed.emit(0.0)
    def _finish(self)->None:
        self._deadline.stop();self._state='idle';self._set_recording(False)
    def _timeout(self)->None:
        active=self._state;self.cancel()
        if active!='idle':self.error.emit('语音识别响应超时，录音已停止。')
    @Slot(int,dict)
    def _on_event(self,generation:int,event:dict)->None:
        if generation!=self._generation or self._cancelled.is_set() or self._state=='idle':return
        kind=event.get('type')
        if kind=='ready':
            if self._state=='starting':self._state='recording';self._deadline.start(65000);self._set_recording(True)
        elif kind=='level' and self._state=='recording':
            try:
                level=float(event.get('value',0))
                if not math.isfinite(level):return
                level=max(0.0,min(1.0,level))
            except (TypeError,ValueError):return
            self.level_changed.emit(level)
        elif kind=='result':
            text=event.get('text');self._finish()
            if isinstance(text,str) and text.strip():self.transcript.emit(text.strip()[:16000])
            else:self.error.emit(ERROR_MESSAGES['no_speech'])
        elif kind=='error':self._finish();self.error.emit(ERROR_MESSAGES.get(event.get('code'),ERROR_MESSAGES['recognition_failed']))
        elif kind=='exit':self._finish();self.error.emit(ERROR_MESSAGES['recognition_failed'])

def request_dictation(**_ignored)->dict:
    """Safe compatibility notice; no keyboard shortcut is sent."""
    return {'requested':False,'message':'语音已改为桌宠内离线录音，请使用新版语音按钮。'}

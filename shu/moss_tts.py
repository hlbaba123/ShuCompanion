"""Lightweight persistent MOSS orchestration; CUDA inference is external."""
import base64
import binascii
import json
from pathlib import Path
import re
import sys

from PySide6.QtCore import QObject, Signal, QProcess, QProcessEnvironment, QTimer
from .stream_audio import PcmPlayer


def prepare_text(text):
    text = re.sub(r'```.*?(?:```|$)|~~~.*?(?:~~~|$)', ' ', str(text)[:16000], flags=re.S)
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'https?://\S+', ' ', text)
    text = re.sub(r'<[^>]*>|[`*_#>|]', ' ', text)
    return ' '.join(''.join(c if c.isprintable() else ' ' for c in text).split())[:220]


def discover_runtime(resource_root):
    roots = [Path(resource_root).resolve(), Path(sys.executable).resolve().parent]
    for root in roots:
        for parent in [root, *list(root.parents)[:5]]:
            candidate = parent / '.shu-voice-eval'
            if (candidate / 'install.json').is_file(): return candidate
    return roots[0] / '.shu-voice-eval'


class MossSpeechOutput(QObject):
    notice = Signal(str)
    error = Signal(str)
    caption = Signal(str)
    preparing = Signal()
    playback_finished = Signal()
    playback_cancelled = Signal()
    MAX_EVENT_BYTES = 1_048_576
    MAX_FRAMES = 48_000 * 120

    def __init__(self, parent=None, *, runtime_root=None, resource_root=None,
                 data_dir=None, process_factory=None, player=None):
        super().__init__(parent)
        self.resource_root = Path(resource_root or Path(__file__).resolve().parents[1]).resolve()
        self.runtime_root = Path(runtime_root or discover_runtime(self.resource_root)).resolve()
        self.data_dir = Path(data_dir or self.resource_root / 'data')
        self._prefs = self.data_dir / 'voice-output.json'
        self._factory = process_factory or QProcess
        self._player = player if player is not None else PcmPlayer()
        self._process = None; self._worker_ready = False; self._terminating = False
        self._pending = None; self._generation = 0; self._closed = False; self._prewarming=False
        self._manifest = None; self._buffer = b''; self._stderr = b''
        self._waiting = self._speaking = self._done = False
        self._text = ''; self._received_frames = 0; self._cues = []; self._cue_index = -1
        self._reset_progressive()
        self.voices = [{'id': 'shu-gentle', 'name': '黍 · 温柔清晰', 'label': '黍 · 温柔清晰', 'speed': 1.0}]
        self.voice_id = 'shu-gentle'
        try:
            manifest = json.loads((self.runtime_root / 'install.json').read_text(encoding='utf-8-sig'))
            python = Path(manifest['python'])
            if (manifest.get('ready') is not True or manifest.get('backend') != 'moss-nano'
                    or manifest.get('provider') != 'CUDAExecutionProvider'
                    or not python.is_absolute() or not python.is_file()): raise ValueError('Invalid runtime')
            self._python = python.with_name('pythonw.exe') if sys.platform == 'win32' else python
            voices = manifest['voices']
            if not isinstance(voices, list) or not 1 <= len(voices) <= 16: raise ValueError('Invalid voices')
            for voice in voices:
                if (not isinstance(voice, dict) or not isinstance(voice.get('id'), str)
                        or not re.fullmatch(r'[a-z0-9_-]{1,40}', voice['id'])
                        or not isinstance(voice.get('name'), str) or not 1 <= len(voice['name']) <= 80
                        or type(voice.get('speed')) not in (int, float) or not .5 <= voice['speed'] <= 1.5):
                    raise ValueError('Invalid voice')
            if (not self._python.is_file() or not (self.resource_root / 'assets/moss_worker.py').is_file()
                    or len({voice['id'] for voice in voices}) != len(voices)): raise ValueError('Incomplete runtime')
            self.voices = [dict(voice, label=voice.get('label') or voice['name']) for voice in voices]
            self.voice_id = voices[0]['id']; self._manifest = manifest
        except (OSError, ValueError, KeyError, TypeError): pass
        self._enabled = self.ready
        self.language = 'zh'; self.speed = 1.08
        try:
            preferences = json.loads(self._prefs.read_text(encoding='utf8'))
            self._enabled = preferences['enabled'] is True
            if preferences.get('voice') in {voice['id'] for voice in self.voices}:
                self.voice_id = preferences['voice']
            if preferences.get('language') in ('zh', 'ja'):
                self.language = preferences['language']
            speed = preferences.get('speed')
            if type(speed) in (int, float) and 1.0 <= speed <= 1.2:
                self.speed = float(speed)
        except (OSError, ValueError, KeyError, TypeError): pass
        self._play_timer = QTimer(self); self._play_timer.setInterval(40)
        self._play_timer.timeout.connect(self._poll_playback)
        self._timeout = QTimer(self); self._timeout.setSingleShot(True); self._timeout.setInterval(120000)
        self._timeout.timeout.connect(self._timed_out)

    @property
    def ready(self): return self._manifest is not None

    @property
    def enabled(self): return self._enabled

    def _save_preferences(self):
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temporary = self._prefs.with_suffix('.tmp')
            temporary.write_text(json.dumps({'enabled': self.enabled, 'voice': self.voice_id,
                                             'language': self.language, 'speed': self.speed}), encoding='utf8')
            temporary.replace(self._prefs)
        except OSError: self.error.emit('朗读偏好未能保存。')

    def set_voice(self, voice):
        if voice not in {entry['id'] for entry in self.voices}: raise ValueError('Unknown voice')
        self.stop(); self.voice_id = voice; self._save_preferences()

    def set_language(self, language):
        if language not in ('zh', 'ja'): raise ValueError('Unknown speech language')
        if language != self.language:
            self.stop(); self.language = language; self._save_preferences()

    def set_speed(self, speed):
        if type(speed) not in (int, float) or not 1.0 <= speed <= 1.2:
            raise ValueError('Speech speed must be between 1.0 and 1.2')
        if speed != self.speed:
            self.stop(); self.speed = float(speed); self._save_preferences()

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        if not enabled: self.stop()
        self._save_preferences()
        if enabled and not self.ready: self.error.emit('本地 GPU 语音尚未安装完成，文字交流仍可使用。')

    def handle_message(self, role, text):
        if role == 'user': self.stop()
        elif role == 'assistant' and self.enabled: self._request(text)

    def preview(self):
        self._request('こんにちは、シュウです。焦らずに、目の前のことを一つずつ、一緒に進めていきましょう。'
                      if self.language == 'ja' else '你好，我是黍。慢慢来，我会陪着你，把眼前的事情一件件做好。')

    def warm_up(self):
        if self._closed or not self.enabled or not self.ready or self._process is not None:return
        self._prewarming=True;self._timeout.start(20000);self._launch()

    def _request(self, text):
        if self._closed: return
        if self._waiting or self._speaking: self.stop()
        text = prepare_text(text)
        if not text: return
        if not self.ready:
            self.error.emit('本地 GPU 语音尚未安装完成，文字交流仍可使用。'); return
        self._generation += 1
        self._pending = {'op': 'speak', 'id': self._generation, 'text': text,
                         'language': self.language, 'speed': self.speed}
        self._waiting = True; self._done = False; self._text = text
        self._received_frames = 0; self._cues = []; self._cue_index = -1
        self._reset_progressive()
        self.preparing.emit(); self._timeout.start(120000)
        if self._process is None: self._launch()
        else: self._send_pending()

    def _launch(self):
        if self._closed or (self._pending is None and not self._prewarming) or self._process is not None: return
        try:
            self._worker_ready = False; self._terminating = False; self._buffer = b''; self._stderr = b''
            environment = QProcessEnvironment.systemEnvironment()
            for key, value in {'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1', 'OMP_NUM_THREADS': '2',
                               'OPENBLAS_NUM_THREADS': '2', 'MKL_NUM_THREADS': '2'}.items():
                environment.insert(key, value)
            temporary = self.runtime_root / 'tmp'; temporary.mkdir(exist_ok=True)
            environment.insert('TEMP', str(temporary)); environment.insert('TMP', str(temporary))
            process = self._factory(self); self._process = process
            process.setProcessEnvironment(environment)
            process.setProgram(str(self._python))
            process.setArguments([str(self.resource_root / 'assets/moss_worker.py'), '--runtime', str(self.runtime_root)])
            process.readyReadStandardOutput.connect(lambda: self._read_stdout(process))
            process.readyReadStandardError.connect(lambda: self._read_stderr(process))
            process.finished.connect(lambda code, status: self._exited(process, code))
            process.errorOccurred.connect(lambda error: self._process_error(process, error))
            process.start()
        except (OSError, RuntimeError):
            if self._process is not None and hasattr(self._process,'state') and self._process.state()==QProcess.ProcessState.NotRunning:
                self._process.deleteLater();self._process=None
            self._fail('无法启动本地 GPU 语音，文字交流仍可使用。')

    def _send_pending(self):
        if not self._worker_ready or self._terminating or self._pending is None: return
        request, self._pending = self._pending, None
        data = (json.dumps(request, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf8')
        if self._process.write(data) != len(data): self._fail('无法发送本地语音请求，文字交流仍可使用。')

    def _read_stderr(self, process):
        if process is self._process:
            self._stderr = (self._stderr + bytes(process.readAllStandardError()))[-2048:]

    def _read_stdout(self, process):
        data = bytes(process.readAllStandardOutput())
        if process is not self._process or self._terminating or self._closed: return
        self._buffer += data
        try:
            while b'\n' in self._buffer:
                line, self._buffer = self._buffer.split(b'\n', 1)
                if len(line) > self.MAX_EVENT_BYTES: raise ValueError('Oversized worker message')
                self._event(json.loads(line.decode('utf8')))
                if self._terminating: self._buffer = b''; return
            if len(self._buffer) > self.MAX_EVENT_BYTES: raise ValueError('Oversized worker message')
        except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error, OSError, RuntimeError):
            self._fail('本地语音生成或音频播放未完成，文字交流仍可使用。')

    def _event(self, event):
        if not isinstance(event, dict): raise ValueError('Invalid worker event')
        kind = event.get('event')
        if kind == 'ready':
            if self._worker_ready or event.get('provider') != 'CUDAExecutionProvider': raise ValueError('Invalid ready')
            self._worker_ready = True;self._prewarming=False
            if self._pending is None:self._timeout.stop()
            self._send_pending(); return
        ident = event.get('id')
        if type(ident) is not int: raise ValueError('Invalid worker ID')
        if ident != self._generation or not (self._waiting or self._speaking): return
        if not self._worker_ready or self._pending is not None: raise ValueError('Worker was not ready')
        if kind == 'error':
            self._fail('本地 GPU 语音生成未完成，文字交流仍可使用。'); return
        if self._done: raise ValueError('Audio after done')
        if kind == 'done':
            if not self._received_frames: raise ValueError('Empty speech')
            if self._progressive or 'frames' in event:
                frames = event.get('frames')
                if type(frames) is not int or frames != self._received_frames:
                    raise ValueError('Inconsistent completed frame count')
                self._actual_frames = frames
            self._done = True; self._timeout.stop(); self._player.finish(); self._poll_playback(); return
        if kind != 'pcm': raise ValueError('Unknown worker event')
        rate, channels, start, text, encoded = (event.get(key) for key in ('rate', 'channels', 'start', 'text', 'data'))
        if (type(rate) is not int or rate != 48000 or type(channels) is not int or channels != 2
                or type(start) is not int or start < 0 or start > self._received_frames
                or not isinstance(text, str) or not text.strip() or len(text) > 220
                or any(not c.isprintable() for c in text)
                or not isinstance(encoded, str) or len(encoded) > self.MAX_EVENT_BYTES):
            raise ValueError('Invalid PCM metadata')
        pcm = base64.b64decode(encoded, validate=True)
        if not pcm or len(pcm) % 4 or self._received_frames + len(pcm) // 4 > self.MAX_FRAMES:
            raise ValueError('Invalid PCM frames')
        progressive = event.get('progressive', False)
        if type(progressive) is not bool or (self._received_frames and progressive != self._progressive):
            raise ValueError('Inconsistent caption mode')
        if progressive:
            estimate = event.get('estimated_frames')
            if (start != 0 or text != self._text or type(estimate) is not int
                    or not 1 <= estimate <= self.MAX_FRAMES
                    or (self._estimated_frames is not None and estimate != self._estimated_frames)):
                raise ValueError('Invalid progressive caption metadata')
            self._progressive = True; self._estimated_frames = estimate
        elif not self._cues or start != self._cues[-1]['start']:
            if start != self._received_frames: raise ValueError('Invalid segment start')
            self._cues.append({'start': start, 'text': text})
        elif text != self._cues[-1]['text']: raise ValueError('Inconsistent segment text')
        if not self._received_frames: self._player.begin(rate, channels)
        self._player.append(pcm); self._received_frames += len(pcm) // 4
        self._waiting = False; self._speaking = True; self._play_timer.start(); self._poll_playback()

    def _poll_playback(self):
        if not self._speaking: return
        try:
            position, playing = self._player.status()
            if playing:
                if self._progressive:
                    self._poll_progressive(position)
                else:
                    frame = position * 48000
                    index = next((i for i in range(len(self._cues) - 1, -1, -1) if self._cues[i]['start'] <= frame), -1)
                    if index >= 0 and index != self._cue_index:
                        self._cue_index = index; self.caption.emit(self._cues[index]['text'])
            elif self._done:
                self._player.stop(); self._speaking = False; self._play_timer.stop()
                if self._progressive: self.caption.emit(self._text)
                self._reset_progressive()
                self._cues = []; self.playback_finished.emit()
        except (OSError, RuntimeError, ValueError):
            self._fail('系统音频设备无法播放，文字交流仍可使用。')

    def _reset_progressive(self):
        self._progressive = False
        self._estimated_frames = self._actual_frames = None
        self._caption_chars = 0; self._caption_frame = self._caption_position = 0

    def _poll_progressive(self, position):
        # Only the device's consumed samples advance the caption. Repeated
        # timer/chunk/done callbacks during a stalled queue cannot type ahead.
        frame = min(self._received_frames, max(0, int(position * 48000)))
        advanced = frame > self._caption_position
        self._caption_position = max(frame, self._caption_position)
        total = self._actual_frames or max(self._estimated_frames, self._received_frames)
        target = min(len(self._text) - 1, 1 + frame * len(self._text) // total)
        if self._caption_chars == 0:
            count = min(1, target)
        elif advanced and frame - self._caption_frame >= 1920:
            count = min(target, self._caption_chars + 2)
        else:
            return
        if count > self._caption_chars:
            self._caption_chars = count; self._caption_frame = frame
            self.caption.emit(self._text[:count])

    def _process_error(self, process, error):
        if process is not self._process: return
        if error == QProcess.ProcessError.FailedToStart: self._exited(process, 1)

    def _exited(self, process, code):
        if process is not self._process: return
        self._read_stdout(process); self._read_stderr(process)
        terminating = self._terminating
        self._process = None; self._worker_ready = False; self._terminating = False; self._buffer = b''
        self._prewarming=False
        process.deleteLater()
        if not terminating and (self._waiting or (self._speaking and not self._done)):
            self._fail('本地 GPU 语音进程已退出，文字交流仍可使用。')
        self._launch()

    def _fail(self, message):
        active = self._waiting or self._speaking
        text = self._text
        self._waiting = self._speaking = False; self._pending = None; self._cues = []
        self._reset_progressive()
        self._prewarming=False
        self._play_timer.stop(); self._timeout.stop()
        try: self._player.stop()
        except (OSError, RuntimeError): pass
        if self._process is not None:
            self._terminating = True; self._worker_ready = False; self._process.kill()
        if active:
            self.error.emit(message)
            if text: self.caption.emit(text)
            self.playback_finished.emit()

    def _timed_out(self): self._fail('本地 GPU 语音生成超时，文字交流仍可使用。')

    def stop(self):
        active = self._waiting or self._speaking
        keep_warming=self._prewarming and not active and not self._closed and self.enabled
        self._generation += 1; self._pending = None
        self._waiting = self._speaking = self._done = False; self._cues = []; self._text = ''
        self._reset_progressive()
        if not keep_warming:self._timeout.stop();self._prewarming=False
        self._play_timer.stop()
        if not keep_warming:self._buffer = b''
        try: self._player.stop()
        except (OSError, RuntimeError): self.error.emit('系统音频设备未响应停止请求。')
        finally:
            if self._process is not None and (active or self._closed or not self.enabled):
                self._terminating = True; self._worker_ready = False; self._process.kill()
        if active: self.playback_cancelled.emit()

    def close(self):
        self._closed = True; self.stop()
        if self._process is not None and hasattr(self._process, 'waitForFinished'):
            self._process.waitForFinished(1500)

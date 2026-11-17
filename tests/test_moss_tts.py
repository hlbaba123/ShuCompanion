"""Qt orchestration tests; the external GPU and audio device are isolated."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QObject, Signal, QProcess
from PySide6.QtWidgets import QApplication


class Process(QObject):
    started = Signal()
    finished = Signal(int, QProcess.ExitStatus)
    errorOccurred = Signal(QProcess.ProcessError)
    readyReadStandardOutput = Signal()
    readyReadStandardError = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.input = b''
        self.output = b''
        self.killed = False

    def setProcessEnvironment(self, environment): self.environment = environment
    def setProgram(self, program): self.program = program
    def setArguments(self, arguments): self.arguments = arguments
    def start(self): self.started.emit()
    def write(self, data): self.input += bytes(data); return len(data)
    def kill(self): self.killed = True  # OS exit deliberately arrives later.
    def readAllStandardOutput(self):
        data, self.output = self.output, b''
        return data
    def readAllStandardError(self): return b''
    def finish(self, code=0): self.finished.emit(code, QProcess.ExitStatus.NormalExit)
    def event(self, payload):
        self.output += json.dumps(payload, ensure_ascii=False).encode() + b'\n'
        self.readyReadStandardOutput.emit()


class Player:
    def __init__(self):
        self.pcm = []
        self.position = 0.
        self.playing = False
        self.finished = False
    def begin(self, rate, channels):
        self.rate, self.channels = rate, channels
        self.position = 0.; self.playing = False; self.finished = False
    def append(self, data): self.pcm.append(data); self.playing = True
    def finish(self): self.finished = True
    def stop(self): self.playing = False
    def status(self): return self.position, self.playing


class MossSpeechTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('shu.moss_tts'), 'Streaming speech output is missing')
        from shu.moss_tts import MossSpeechOutput
        self.Output = MossSpeechOutput
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / '.shu-voice-eval'
        self.runtime.mkdir()
        scripts = self.root / '.shu-tts/venv/Scripts'
        scripts.mkdir(parents=True)
        for name in ('python.exe', 'pythonw.exe'): (scripts / name).write_bytes(b'x')
        (self.root / 'assets').mkdir()
        (self.root / 'assets/moss_worker.py').write_text('# fixture worker')
        self.manifest = {'ready': True, 'backend': 'moss-nano', 'python': str(scripts / 'python.exe'),
                         'provider': 'CUDAExecutionProvider',
                         'voices': [{'id': 'shu-gentle', 'name': '黍 · 温柔清晰', 'speed': 1.0}]}
        self.save_manifest()
        self.processes = []
        self.player = Player()

    def save_manifest(self):
        (self.runtime / 'install.json').write_text(json.dumps(self.manifest), encoding='utf8')

    def create(self, **kwargs):
        def factory(parent):
            process = Process(parent); self.processes.append(process); return process
        result = self.Output(runtime_root=self.runtime, resource_root=self.root, data_dir=self.root / 'data',
                             player=self.player, process_factory=factory, **kwargs)
        self.addCleanup(result.close)
        return result

    def ready(self, process): process.event({'event': 'ready', 'provider': 'CUDAExecutionProvider'})

    def speak(self, speech, text='第一句。第二句。'):
        speech.handle_message('assistant', text)
        process = self.processes[-1]
        self.ready(process)
        request = json.loads(process.input.splitlines()[-1])
        return process, request['id']

    def pcm(self, process, ident, text='第一句。', start=0, frames=48000, **kwargs):
        payload = {'event': 'pcm', 'id': ident, 'rate': 48000, 'channels': 2, 'text': text,
                   'start': start, 'data': base64.b64encode(b'\x01\x00\x01\x00' * frames).decode()}
        payload.update(kwargs); process.event(payload)

    def test_default_request_uses_chinese_and_faster_playback(self):
        speech = self.create(); process, _ = self.speak(speech)
        request = json.loads(process.input)
        self.assertEqual(request.get('language'), 'zh')
        self.assertEqual(request.get('speed'), 1.08)

    def test_language_and_speed_persist_with_enabled_and_voice(self):
        speech = self.create()
        speech.set_language('ja'); speech.set_speed(1.2); speech.set_enabled(False)
        replacement = self.create()
        self.assertFalse(replacement.enabled)
        self.assertEqual(replacement.voice_id, 'shu-gentle')
        self.assertEqual(replacement.language, 'ja'); self.assertEqual(replacement.speed, 1.2)
        replacement.set_enabled(True)
        process, _ = self.speak(replacement, 'こんにちは。')
        request = json.loads(process.input)
        self.assertEqual(request.get('language'), 'ja'); self.assertEqual(request.get('speed'), 1.2)

    def test_invalid_saved_language_and_speed_use_defaults(self):
        directory = self.root / 'data'; directory.mkdir()
        for language, speed in [('en', .9), (True, True), ('ja', 1.3), ('zh', '1.08')]:
            with self.subTest(language=language, speed=speed):
                (directory / 'voice-output.json').write_text(json.dumps(
                    {'enabled': True, 'voice': 'shu-gentle', 'language': language, 'speed': speed}), encoding='utf8')
                speech = self.create(); process, _ = self.speak(speech)
                request = json.loads(process.input)
                self.assertEqual(request.get('language'), language if language in ('zh', 'ja') else 'zh')
                self.assertEqual(request.get('speed'), 1.08)

    def test_language_and_speed_changes_cancel_generation_and_playback(self):
        for setting, value in [('language', 'ja'), ('speed', 1.15)]:
            for playing in (False, True):
                with self.subTest(setting=setting, playing=playing):
                    (self.root / 'data/voice-output.json').unlink(missing_ok=True)
                    speech = self.create(); cancelled = []; captions = []
                    speech.playback_cancelled.connect(lambda: cancelled.append(True))
                    speech.caption.connect(captions.append)
                    process, ident = self.speak(speech)
                    if playing: self.pcm(process, ident)
                    before = list(captions)
                    getattr(speech, 'set_' + setting)(value)
                    self.assertTrue(process.killed); self.assertFalse(self.player.playing)
                    self.assertEqual(cancelled, [True])
                    self.pcm(process, ident); process.event({'event': 'done', 'id': ident})
                    self.assertEqual(captions, before)

    def test_invalid_language_and_speed_do_not_interrupt_speech(self):
        speech = self.create(); process, _ = self.speak(speech)
        for language in ('en', '', None, True):
            with self.subTest(language=language), self.assertRaises(ValueError): speech.set_language(language)
        for speed in (.99, 1.21, '1.08', None, True, float('nan'), float('inf')):
            with self.subTest(speed=speed), self.assertRaises(ValueError): speech.set_speed(speed)
        self.assertFalse(process.killed)
        speech.set_language('zh'); speech.set_speed(1.08)
        self.assertFalse(process.killed)

    def test_preview_uses_selected_language(self):
        speech = self.create(); speech.set_language('ja'); speech.preview()
        process = self.processes[-1]; self.ready(process)
        request = json.loads(process.input)
        self.assertEqual(request.get('language'), 'ja')
        self.assertTrue(any('\u3040' <= letter <= '\u30ff' for letter in request['text']))

    def test_progressive_caption_stalls_with_device_clock_and_stays_a_prefix(self):
        text = '这是随着真实音频时钟逐渐出现的字幕。'
        speech = self.create(); captions = []; speech.caption.connect(captions.append)
        process, ident = self.speak(speech, text)
        self.pcm(process, ident, text=text, progressive=True, estimated_frames=480000)
        self.assertEqual(captions, [text[:1]])
        for position in (.4, .8, 1.2):
            self.player.position = position; speech._poll_playback()
        before = list(captions)
        for _ in range(30): speech._poll_playback()
        self.assertEqual(captions, before)
        self.assertTrue(all(text.startswith(caption) for caption in captions))
        self.assertTrue(all(0 < len(b) - len(a) <= 2 for a, b in zip(captions, captions[1:])))
        self.assertGreater(len(captions[-1]), 1)

    def test_progressive_done_does_not_release_queued_text_before_device_drain(self):
        text = '甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥。'
        speech = self.create(); events = []
        speech.caption.connect(lambda caption: events.append(('caption', caption)))
        speech.playback_finished.connect(lambda: events.append(('finished', None)))
        process, ident = self.speak(speech, text)
        for _ in range(3):
            self.pcm(process, ident, text=text, progressive=True, estimated_frames=480000)
        self.player.position = .5; speech._poll_playback()
        before = list(events)
        process.event({'event': 'done', 'id': ident, 'frames': 144000})
        self.assertEqual(events, before)
        for position in (.54, .58, 1., 2., 2.5, 2.9, 3.):
            self.player.position = position; speech._poll_playback()
        captions = [value for kind, value in events if kind == 'caption']
        self.assertTrue(all(0 < len(b) - len(a) <= 2 for a, b in zip(captions, captions[1:])))
        self.assertNotIn(text, captions); self.assertNotIn(('finished', None), events)
        self.player.playing = False; speech._poll_playback(); speech._poll_playback()
        self.assertEqual(events[-2:], [('caption', text), ('finished', None)])
        self.assertEqual(events.count(('finished', None)), 1)

    def test_progressive_completion_holds_last_character_until_device_drain(self):
        text = '你好。'
        speech = self.create(); captions = []; speech.caption.connect(captions.append)
        process, ident = self.speak(speech, text)
        self.pcm(process, ident, text=text, progressive=True, estimated_frames=48000)
        process.event({'event': 'done', 'id': ident, 'frames': 48000})
        self.player.position = .99; speech._poll_playback()
        self.assertEqual(captions[-1], '你好')
        self.player.position = 1.; speech._poll_playback()
        self.assertEqual(captions[-1], '你好')
        self.player.playing = False; speech._poll_playback()
        self.assertEqual(captions[-1], text)

    def test_progressive_estimate_correction_never_shrinks_or_catches_up_during_underrun(self):
        text = '甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥。'
        speech = self.create(); captions = []; speech.caption.connect(captions.append)
        process, ident = self.speak(speech, text)
        self.pcm(process, ident, text=text, progressive=True, estimated_frames=48000)
        self.player.position = .5; speech._poll_playback()
        first = len(captions[-1])
        self.pcm(process, ident, text=text, progressive=True, estimated_frames=48000)
        self.pcm(process, ident, text=text, progressive=True, estimated_frames=48000)
        process.event({'event': 'done', 'id': ident, 'frames': 144000})
        for _ in range(20): speech._poll_playback()
        self.assertEqual(len(captions[-1]), first)
        self.player.position = .54; speech._poll_playback()
        self.assertGreaterEqual(len(captions[-1]), first)
        self.assertTrue(all(len(b) >= len(a) for a, b in zip(captions, captions[1:])))

    def test_progressive_protocol_rejects_invalid_estimates_and_completion_lengths(self):
        for changes in [{'estimated_frames': value} for value in (None, True, 0, -1, 1.5, 5760001)] + [
                {'progressive': 'yes'}, {'text': '不同的字幕。'}]:
            with self.subTest(changes=changes):
                speech = self.create(); errors = []; speech.error.connect(errors.append)
                process, ident = self.speak(speech, '完整字幕。')
                payload = {'progressive': True, 'estimated_frames': 48000, 'text': '完整字幕。'}
                payload.update(changes)
                before = len(self.player.pcm)
                self.pcm(process, ident, frames=10, **payload)
                self.assertEqual(len(self.player.pcm), before); self.assertEqual(len(errors), 1)
        for frames in (None, True, 0, 47999, 48001):
            with self.subTest(frames=frames):
                speech = self.create(); errors = []; speech.error.connect(errors.append)
                process, ident = self.speak(speech, '完整字幕。')
                self.pcm(process, ident, text='完整字幕。', progressive=True, estimated_frames=48000)
                process.event({'event': 'done', 'id': ident, 'frames': frames})
                self.assertEqual(len(errors), 1); self.assertTrue(process.killed)

    def test_progressive_stop_clears_caption_state_and_ignores_stale_ids(self):
        text = '停下来后旧字幕不再出现。'
        speech = self.create(); captions = []; speech.caption.connect(captions.append)
        process, ident = self.speak(speech, text)
        self.pcm(process, ident, text=text, progressive=True, estimated_frames=48000)
        speech.stop(); before = list(captions)
        self.player.position = 1.; speech._poll_playback()
        self.pcm(process, ident, text=text, progressive=True, estimated_frames=48000)
        process.event({'event': 'done', 'id': ident, 'frames': 48000})
        self.assertEqual(captions, before)
        process.finish(1)
        new, new_id = self.speak(speech, '新字幕。')
        self.pcm(new, ident, text=text, progressive=True, estimated_frames=48000)
        self.assertEqual(captions, before)
        self.pcm(new, new_id, text='新字幕。', progressive=True, estimated_frames=48000)
        self.assertEqual(captions[-1], '新')

    def test_progressive_stream_cannot_change_caption_mode_or_timing_metadata(self):
        for changes in ({'progressive': False}, {'start': 10}, {'estimated_frames': 96000}):
            with self.subTest(changes=changes):
                speech = self.create(); errors = []; speech.error.connect(errors.append)
                process, ident = self.speak(speech, '字幕保持一致。')
                self.pcm(process, ident, frames=10, text='字幕保持一致。',
                         progressive=True, estimated_frames=48000)
                before = len(self.player.pcm)
                payload = {'progressive': True, 'estimated_frames': 48000}
                payload.update(changes)
                self.pcm(process, ident, frames=10, text='字幕保持一致。', **payload)
                self.assertEqual(len(self.player.pcm), before)
                self.assertEqual(len(errors), 1); self.assertTrue(process.killed)

    def test_progressive_typing_budget_requires_forty_milliseconds_of_consumed_audio(self):
        text = '甲乙丙丁戊己庚辛壬癸' * 4
        speech = self.create(); captions = []; speech.caption.connect(captions.append)
        process, ident = self.speak(speech, text)
        self.pcm(process, ident, text=text, progressive=True, estimated_frames=48000)
        for position in (.01, .02, .039):
            self.player.position = position; speech._poll_playback()
            self.assertEqual(captions, ['甲'])
        self.player.position = .04; speech._poll_playback()
        self.assertEqual(captions[-1], '甲乙')
        self.player.position = .079; speech._poll_playback()
        self.assertEqual(captions[-1], '甲乙')
        self.player.position = .08; speech._poll_playback()
        self.assertEqual(captions[-1], '甲乙丙丁')

    def test_request_waits_for_ready_and_worker_is_reused_after_device_drain(self):
        speech = self.create()
        self.assertTrue(speech.ready)
        self.assertEqual(self.processes, [])
        speech.handle_message('assistant', '你好。')
        process = self.processes[0]
        self.assertEqual(process.input, b'')
        self.ready(process)
        first = json.loads(process.input)
        self.assertEqual(first['op'], 'speak'); self.assertEqual(first['text'], '你好。')
        self.pcm(process, first['id'])
        process.event({'event': 'done', 'id': first['id']})
        self.player.playing = False
        speech._poll_playback()
        speech.handle_message('assistant', '再见。')
        self.assertEqual(len(self.processes), 1)
        requests = [json.loads(line) for line in process.input.splitlines()]
        self.assertEqual(requests[-1]['text'], '再见。')
        self.assertGreater(requests[-1]['id'], first['id'])
        self.assertFalse(process.killed)

    def test_latest_request_waits_for_terminated_worker_and_stale_events_never_play(self):
        speech = self.create(); process, ident = self.speak(speech)
        speech.handle_message('assistant', '中间。')
        speech.handle_message('assistant', '最新。')
        self.assertTrue(process.killed); self.assertEqual(len(self.processes), 1)
        self.pcm(process, ident)
        self.assertEqual(self.player.pcm, [])
        process.finish(1)
        self.assertEqual(len(self.processes), 2)
        new = self.processes[-1]; self.ready(new)
        request = json.loads(new.input)
        self.assertEqual(request['text'], '最新。')
        self.pcm(new, ident)
        self.assertEqual(self.player.pcm, [])
        self.pcm(new, request['id'])
        self.assertEqual(len(self.player.pcm), 1)

    def test_done_waits_for_final_device_drain_and_underrun_cannot_finish_generation(self):
        speech = self.create(); completions = []
        speech.playback_finished.connect(lambda: completions.append(True))
        process, ident = self.speak(speech)
        self.pcm(process, ident)
        self.player.playing = False
        speech._poll_playback()
        self.assertEqual(completions, [])
        self.player.playing = True
        process.event({'event': 'done', 'id': ident})
        self.assertTrue(self.player.finished); self.assertEqual(completions, [])
        self.player.playing = False; speech._poll_playback(); speech._poll_playback()
        self.assertEqual(completions, [True])

    def test_captions_use_sample_offsets_and_deduplicate_repeated_segment_chunks(self):
        speech = self.create(); captions = []; speech.caption.connect(captions.append)
        process, ident = self.speak(speech)
        self.assertEqual(captions, [])
        self.pcm(process, ident, frames=24000)
        self.pcm(process, ident, frames=24000)
        self.pcm(process, ident, text='第二句。', start=48000)
        self.player.position = .75; speech._poll_playback()
        self.assertEqual(captions, ['第一句。'])
        self.player.position = 1.; speech._poll_playback()
        self.assertEqual(captions, ['第一句。', '第二句。'])
        speech.stop(); self.player.position = 20.; speech._poll_playback()
        self.assertEqual(len(captions), 2)

    def test_disable_and_close_kill_worker_and_persist_user_preferences(self):
        speech = self.create(); process, ident = self.speak(speech)
        speech.set_enabled(False)
        self.assertTrue(process.killed); self.assertFalse(speech.enabled)
        self.pcm(process, ident); self.assertEqual(self.player.pcm, [])
        process.finish(1)
        speech.close(); speech.preview(); self.assertEqual(len(self.processes), 1)
        replacement = self.create(); self.assertFalse(replacement.enabled)
        replacement.set_enabled(True); replacement.preview()
        replacement.close(); self.assertTrue(self.processes[-1].killed)

    def test_invalid_runtime_never_spawns_and_keeps_text_usable(self):
        for key, value in [('backend', 'other'), ('provider', 'CPUExecutionProvider'),
                           ('python', 'relative/python.exe'), ('voices', []), ('ready', False)]:
            with self.subTest(key=key):
                original = self.manifest[key]; self.manifest[key] = value; self.save_manifest()
                speech = self.create(); errors = []; speech.error.connect(errors.append)
                self.assertFalse(speech.ready); speech.preview(); self.assertTrue(errors)
                self.assertEqual(self.processes, []); self.manifest[key] = original

    def test_malformed_or_oversized_protocol_falls_back_once_without_raw_worker_error(self):
        for data in [b'not json\n', b'[' + b' ' * 1_100_000]:
            with self.subTest(length=len(data)):
                speech = self.create(); captions = []; errors = []; done = []
                speech.caption.connect(captions.append); speech.error.connect(errors.append)
                speech.playback_finished.connect(lambda: done.append(True))
                process, _ = self.speak(speech, '完整文字。')
                process.output = data; process.readyReadStandardOutput.emit()
                process.finish(1)
                self.assertTrue(process.killed); self.assertEqual(captions, ['完整文字。'])
                self.assertEqual(done, [True]); self.assertEqual(len(errors), 1)

    def test_pcm_rejects_bad_alignment_offsets_format_and_base64(self):
        for changes in [{'data': '!!!!'}, {'data': 'AQ=='}, {'rate': True}, {'channels': 1},
                        {'start': -1}, {'start': 2}, {'text': ''}]:
            with self.subTest(changes=changes):
                speech = self.create(); errors = []; speech.error.connect(errors.append)
                process, ident = self.speak(speech)
                before = len(self.player.pcm)
                self.pcm(process, ident, frames=10, **changes)
                self.assertEqual(len(self.player.pcm), before); self.assertTrue(errors)

    def test_worker_crash_start_failure_and_audio_failure_release_text(self):
        for failure in ('crash', 'start', 'audio', 'worker'):
            with self.subTest(failure=failure):
                speech = self.create(); captions = []; done = []; errors = []
                speech.caption.connect(captions.append); speech.error.connect(errors.append)
                speech.playback_finished.connect(lambda: done.append(True))
                process, ident = self.speak(speech, '文字仍可读。')
                if failure == 'crash': process.finish(1)
                elif failure == 'start': process.errorOccurred.emit(QProcess.ProcessError.FailedToStart)
                elif failure == 'worker': process.event({'event': 'error', 'id': ident, 'message': 'secret path'})
                else:
                    def unavailable(*args): raise OSError('no audio device')
                    self.player.begin = unavailable; self.pcm(process, ident)
                    del self.player.begin
                self.assertEqual(captions, ['文字仍可读。']); self.assertEqual(done, [True])
                self.assertEqual(len(errors), 1); self.assertNotIn('secret path', errors[0])

    def test_process_launch_is_windowless_and_limits_external_threads(self):
        speech = self.create(); speech.preview(); process = self.processes[0]
        self.assertEqual(Path(process.program).name, 'pythonw.exe')
        self.assertEqual(process.arguments, [str(self.root / 'assets/moss_worker.py'), '--runtime', str(self.runtime)])
        for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
            self.assertEqual(process.environment.value(key), '2')

    def test_user_message_after_finished_speech_preserves_idle_warm_worker(self):
        speech = self.create(); process, ident = self.speak(speech)
        self.pcm(process, ident); process.event({'event': 'done', 'id': ident})
        self.player.playing = False; speech._poll_playback()
        speech.stop(); speech.handle_message('user', '新问题。')
        self.assertFalse(process.killed)
        speech.handle_message('assistant', '新的回答。')
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(json.loads(process.input.splitlines()[-1])['text'], '新的回答。')

    def test_disable_and_close_release_even_an_idle_warm_worker(self):
        for action in ('disable', 'close'):
            with self.subTest(action=action):
                speech = self.create(); process, ident = self.speak(speech)
                self.pcm(process, ident); process.event({'event': 'done', 'id': ident})
                self.player.playing = False; speech._poll_playback()
                if action == 'disable': speech.set_enabled(False)
                else: speech.close()
                self.assertTrue(process.killed)
                process.finish(1)
                speech.set_enabled(True)

    def test_settings_catalog_supplies_label_from_manifest_name(self):
        speech = self.create()
        self.assertEqual(speech.voices[0]['label'], '黍 · 温柔清晰')

    def test_prewarm_loads_without_speaking_and_reuses_ready_worker(self):
        speech=self.create();notices=[];speech.notice.connect(notices.append);speech.warm_up()
        self.assertEqual(notices,[],'Preparation should not expose internal prefaces in any UI surface')
        self.assertEqual(len(self.processes),1)
        process=self.processes[0]
        process.event({'event':'ready','provider':'CUDAExecutionProvider'})
        self.assertEqual(process.input,b'')
        self.assertFalse(speech._timeout.isActive())
        speech.handle_message('assistant','预热后的回答。')
        self.assertEqual(len(self.processes),1)
        self.assertEqual(json.loads(process.input)['text'],'预热后的回答。')

    def test_temp_directory_failure_can_retry_without_waiting_for_unstarted_child(self):
        speech=self.create();factory=speech._factory
        (self.runtime/'tmp').write_text('blocked path')
        speech._factory=lambda parent:QProcess(parent)
        speech.preview()
        self.assertIsNone(speech._process)
        (self.runtime/'tmp').unlink();speech._factory=factory
        speech.preview();self.assertEqual(len(self.processes),1)
        self.ready(self.processes[0]);self.assertTrue(self.processes[0].input)

    def test_idle_stop_during_prewarm_keeps_watchdog(self):
        speech=self.create();speech.warm_up();speech.stop()
        self.assertTrue(speech._timeout.isActive())

    def test_user_message_during_fragmented_prewarm_ready_preserves_protocol(self):
        speech=self.create();speech.warm_up();process=self.processes[0]
        process.output=b'{"event":"ready",';process.readyReadStandardOutput.emit()
        speech.handle_message('user','还在输入')
        process.output=b'"provider":"CUDAExecutionProvider"}\n';process.readyReadStandardOutput.emit()
        self.assertTrue(speech._worker_ready);self.assertFalse(process.killed)

    def test_missing_worker_or_windowless_python_is_not_a_ready_runtime(self):
        for path in (self.root / 'assets/moss_worker.py', self.root / '.shu-tts/venv/Scripts/pythonw.exe'):
            content = path.read_bytes(); path.unlink()
            speech = self.create(); self.assertFalse(speech.ready)
            path.write_bytes(content)

    def test_launch_factory_failure_releases_caption_once(self):
        speech = self.create(); captions = []; completions = []
        speech.caption.connect(captions.append)
        speech.playback_finished.connect(lambda: completions.append(True))
        def unavailable(parent): raise OSError('could not create child')
        speech._factory = unavailable
        speech.handle_message('assistant', '文字仍可读。')
        self.assertEqual(captions, ['文字仍可读。']); self.assertEqual(completions, [True])

    def test_fragments_are_buffered_until_a_complete_protocol_line(self):
        speech = self.create(); speech.preview(); process = self.processes[0]
        process.output = b'{"event":"ready",'; process.readyReadStandardOutput.emit()
        self.assertEqual(process.input, b'')
        process.output = b'"provider":"CUDAExecutionProvider"}\n'; process.readyReadStandardOutput.emit()
        self.assertEqual(json.loads(process.input)['op'], 'speak')

    def test_real_windowless_process_persists_and_flushes_jsonl_without_audio_or_gpu(self):
        import sys
        # Real redirected pythonw pipes caught incompatibilities that a fake
        # process cannot. The child below contains no inference or device calls.
        self.manifest['python'] = sys.executable; self.save_manifest()
        (self.root / 'assets/moss_worker.py').write_text('''import base64,json,sys
def emit(item):
    print(json.dumps(item),flush=True)
emit({'event':'ready','provider':'CUDAExecutionProvider'})
for line in sys.stdin:
    request=json.loads(line)
    emit({'event':'pcm','id':request['id'],'text':request['text'],'start':0,
          'rate':48000,'channels':2,'data':base64.b64encode(bytes(48000)).decode()})
    emit({'event':'done','id':request['id']})
''', encoding='utf8')
        processes = []
        def factory(parent):
            process = QProcess(parent); processes.append(process); return process
        speech = self.create(); speech._factory = factory
        captions = []; errors = []
        speech.caption.connect(captions.append); speech.error.connect(errors.append)
        def wait_until(predicate):
            deadline = time.monotonic() + 5
            while not predicate() and time.monotonic() < deadline:
                self.app.processEvents(); time.sleep(.005)
            self.assertTrue(predicate(), repr(errors))
        speech.handle_message('assistant', '第一句。')
        wait_until(lambda: self.player.finished)
        self.assertEqual(captions, ['第一句。'])
        self.player.playing = False; speech._poll_playback()
        speech.handle_message('user', '新问题。')
        speech.handle_message('assistant', '第二句。')
        wait_until(lambda: len(captions) == 2)
        self.assertEqual(captions[-1], '第二句。')
        self.assertEqual(len(processes), 1); self.assertEqual(errors, [])
        speech.close()
        self.assertEqual(processes[0].state(), QProcess.ProcessState.NotRunning)

    def test_stop_failure_still_terminates_gpu_process(self):
        speech = self.create(); process, _ = self.speak(speech)
        def unavailable(): raise OSError('device disconnected')
        self.player.stop = unavailable
        speech.stop(); self.assertTrue(process.killed)
        del self.player.stop


if __name__ == '__main__': unittest.main()

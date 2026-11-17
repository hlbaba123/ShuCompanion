"""Pet-first desktop shell. Panels are opened only by an explicit user action."""
from pathlib import Path
import ctypes
import subprocess
import sys
from uuid import uuid4
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
    QHBoxLayout, QLabel, QMenu, QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget)
from .activity_ui import ActivityPanel
from .pet import FloatingPet
from .speech import SpeechInput
from .tts import SpeechOutput

PANEL_STYLE = '''
QDialog{background:#fbfcfa;}
QWidget{color:#354146;font-family:"Microsoft YaHei UI","Segoe UI";font-size:13px;}
QLabel#Muted{color:#7a8588;}
QPushButton,QComboBox{background:#f2f5f3;border:1px solid #dfe6e3;border-radius:9px;padding:10px;}
QPushButton:hover{background:#e7efeb;} QPushButton:focus{border-color:#90b5e4;}
QPushButton#Primary{background:#e2edfc;color:#355a83;}
QCheckBox{padding:8px 0;spacing:8px;}
'''


class ObservationPreview(QLabel):
    def __init__(self):
        super().__init__('还没有读取画面。')
        self.original = QPixmap()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(240, 160)
    def load(self, path):
        self.original = QPixmap(str(path))
        self._fit()
    def _fit(self):
        if not self.original.isNull():
            self.setPixmap(self.original.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                Qt.TransformationMode.SmoothTransformation))
    def resizeEvent(self, event):
        super().resizeEvent(event); self._fit()


class CompanionShell(QWidget):
    """Hidden lifetime owner for the pet, settings, progress and preview windows.

    No multi-page workbench exists. This owner is never shown or used as the
    primary input surface; opening/closing a panel never submits/stops a task.
    """
    def __init__(self, controller, root: Path):
        super().__init__()
        self.c, self.root = controller, Path(root)
        self.setWindowTitle('黍 · 桌面陪伴')
        self._exiting = False
        self._settings_dialog = None
        self._displayed_observation = None
        self._capture_pending = False
        self._request_id = None
        self._request_summaries = {}
        self._request_terminal = set()
        self._task_requests = {}
        self._task_last_request = {}
        self._last_activity_notice = None
        self.tray = None
        self.pet = FloatingPet(self.root / 'assets/spritesheet.png')
        self.activity = ActivityPanel(self)
        self.activity.configure_history(self.c.data_dir / 'activity-history.json')
        self.c.task_history=self.activity._history
        self.activity.configure_geometry(self.c.data_dir / 'activity-window.json')
        self.activity.stop_requested.connect(self.stop)
        self._setup_task_management()
        self.preview_dialog = QDialog(self)
        self.preview_dialog.setWindowTitle('黍 · 最近读取的画面')
        self.preview_dialog.setStyleSheet(PANEL_STYLE); self.preview_dialog.resize(680, 440)
        layout = QVBoxLayout(self.preview_dialog)
        self.preview = ObservationPreview(); layout.addWidget(self.preview, 1)
        self.preview_caption = QLabel('尚未读取游戏画面。'); self.preview_caption.setWordWrap(True)
        self.preview_caption.setObjectName('Muted'); layout.addWidget(self.preview_caption)
        self.voice = SpeechInput(self, path=self.root / 'assets/voice-helper.exe')
        self.speech_output = SpeechOutput(self, resource_root=self.root, data_dir=self.c.data_dir)
        if hasattr(self.c, 'set_response_language'):
            self.c.set_response_language(getattr(self.speech_output, 'language', 'zh'))
        self._connect()
        self.pet.skin_changed.connect(self._sync_pet_skin)
        self.pet.configure_skins(self.c.data_dir / 'pet-appearance.json')
        self._setup_tray()
        self.refresh()

    def _connect(self):
        self.pet.text_submitted.connect(self.submit_pet_text)
        self.pet.settings_requested.connect(self.open_settings)
        self.pet.activity_requested.connect(self.open_activity)
        self.pet.capture_requested.connect(self.capture_from_pet)
        self.pet.stop_requested.connect(self.stop)
        self.pet.quit_requested.connect(self.quit_application)
        self.pet.voice_toggle_requested.connect(self.toggle_voice)
        self.voice.recording_changed.connect(self.pet.set_recording)
        self.voice.level_changed.connect(self.pet.set_voice_level)
        self.voice.transcript.connect(self.pet.receive_transcript)
        self.voice.error.connect(self.show_error)
        if hasattr(self.c, 'context_message'):
            self.c.context_message.connect(self._present_context_message)
        else:
            self.c.message.connect(self._present_pet_message)
        if hasattr(self.c, 'request_started'):
            self.c.request_started.connect(self._on_request_started)
        self.c.message.connect(self.speech_output.handle_message)
        self.c.notice.connect(self.on_notice)
        self.c.error.connect(self.show_error)
        self.c.busy_changed.connect(self.set_busy)
        self.c.changed.connect(self.refresh)
        self.c.observation_ready.connect(self.show_observation)
        self.c.plan_ready.connect(self.show_plan)
        self.speech_output.error.connect(self.show_error)
        self.speech_output.notice.connect(self.pet.set_status)
        self.speech_output.preparing.connect(self.pet.speech_preparing)
        self.speech_output.caption.connect(self.pet.speech_caption)
        self.speech_output.playback_finished.connect(self.pet.speech_finished)
        self.speech_output.playback_cancelled.connect(self.pet.speech_cancelled)
        if hasattr(self.c, 'network_progress'):
            self.c.network_progress.connect(self.on_network_progress)
        if hasattr(self.c, 'activity_progress'):
            self.c.activity_progress.connect(self.on_activity_progress)
        if hasattr(self.c, 'execution_changed'):
            self.c.execution_changed.connect(self.pet.set_game_active)
            self.c.execution_changed.connect(self.activity.set_active)
        if hasattr(self.c, 'memory'):
            self.pet.restore_history(self.c.memory.load_messages(limit=200))
            self.pet.chat._history_loader=lambda:self.c.memory.load_messages(limit=2000)
        self.pet.menu.addAction('查看最近画面', self.show_recent_preview)

    def _on_request_started(self, request_id, text):
        self._request_id = request_id
        self.activity.begin_request(request_id, text)

    def _present_context_message(self, payload):
        self._present_pet_message(payload['role'], payload['text'],
            request_id=payload.get('request_id'), task_id=payload.get('task_id'), scoped=True)
        if self.pet.chat.history.isVisible():
            QTimer.singleShot(0,self.pet.chat._reload_history)

    def _present_pet_message(self, role, text, *, request_id=None, task_id=None, scoped=False):
        if role == 'user' and not hasattr(self.c,'request_started'):
            self._on_request_started(uuid4().hex,text)
        if not scoped:
            request_id=self._request_id
        if request_id and task_id:
            self.activity._history.bind_request(request_id,task_id)
        if role == 'assistant' and request_id:
            self._request_summaries[request_id] = text
            if not self.c.is_busy:
                self._finish_request_display(request_id)
        elif role == 'system' and request_id and not self.c.is_busy:
            self._request_summaries[request_id] = text
            QTimer.singleShot(0, self, lambda request_id=request_id: self._finish_request_display(request_id))
        synchronized = role == 'assistant' and self.speech_output.enabled and self.speech_output.ready
        self.pet.present_message(role, text, show_bubble=not synchronized)

    def _sync_pet_skin(self, _skin_id):
        frame = self.pet.canvas.frames.get((0, 0))
        if frame is not None:
            icon = QIcon(frame); self.setWindowIcon(icon)
            if self.tray is not None: self.tray.setIcon(icon)

    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable(): return
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip('黍 · 桌面陪伴')
        menu = QMenu(self)
        menu.addAction('和黍说话', self.pet.show_composer)
        menu.addAction('任务进度', self.open_activity)
        menu.addAction('设置', lambda: self.open_settings('model'))
        menu.addAction('显示黍', self.show_pet)
        menu.addSeparator(); menu.addAction('停止操作', self.stop)
        menu.addAction('退出黍', self.quit_application)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.pet.show_composer()
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.show()

    def refresh(self):
        if self._exiting: return
        if self.pet.chat.history.isVisible():self.pet.chat._reload_history()
        self.activity.set_task(self.c.current_task())
        if callable(getattr(self.c,'task_management_state',None)):
            try:
                state=self.c.task_management_state()
                for row in state.get('requests',[]):
                    if row.get('request_id') and row.get('task_id'):
                        self.activity._history.bind_request(row['request_id'],row['task_id'])
                self.activity.set_management_state(state)
                self._refresh_input_targets(state)
            except (ValueError,RuntimeError,OSError) as error:
                self.activity.set_management_notice('任务状态暂未读取：'+str(error))
        if self._settings_dialog is not None:
            self._settings_dialog.refresh_binding()

    def _setup_task_management(self):
        if not callable(getattr(self.c,'task_management_state',None)):
            return  # Older isolated controller fixtures have no lifecycle API.
        for action,signal in (('trash',self.activity.trash_requested),('restore',self.activity.restore_requested),('purge',self.activity.purge_requested)):
            signal.connect(lambda task_id,action=action:self._manage_task(action,task_id))
        for action,signal in (('trash',self.activity.chat_trash_requested),('restore',self.activity.chat_restore_requested),
                              ('purge',self.activity.chat_purge_requested)):
            signal.connect(lambda request_id,action=action:self._manage_chat(action,request_id))
        for action,signal in (('continue',self.activity.continue_requested),('cancel',self.activity.cancel_requested),('stop',self.activity.task_stop_requested)):
            signal.connect(lambda task_id,action=action:self._control_task(action,task_id))
        self.activity.target_requested.connect(self._answer_task)
        self.activity.maa_permission_requested.connect(self._authorize_maa_import)
        if hasattr(self.c,'message_routed'):
            self.c.message_routed.connect(self._message_routed)
        if hasattr(self.c,'cleanup_progress'):
            self.c.cleanup_progress.connect(self._cleanup_progress)
        self.input_target=QComboBox(self.pet.chat)
        self.input_target.setAccessibleName('本条消息的任务归属')
        self.input_target.setStyleSheet('QComboBox{background:#f5f7f4;border:1px solid #e1e7e1;border-radius:7px;padding:2px 7px;font-size:10px;min-height:21px;}')
        self.input_target.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.input_target.setMinimumContentsLength(13)
        self.input_target.setMaximumWidth(235)
        self.input_target.currentIndexChanged.connect(self._input_target_selected)
        target_row=QHBoxLayout()
        target_row.addWidget(self.input_target,1)
        self.new_task_button=QPushButton('新建任务')
        self.new_task_button.setStyleSheet('QPushButton{font-size:10px;padding:2px 7px;min-height:21px;}')
        self.new_task_button.clicked.connect(lambda:self._set_input_target(None,'new'))
        target_row.addWidget(self.new_task_button)
        self.pet.chat.panel.layout().insertLayout(0,target_row)
        width,height=self.pet.chat.expanded_size
        self.pet.chat.expanded_size=(width,height+29)
        self.route_label=QLabel('自动判断归属',self.pet.chat)
        self.route_label.setTextFormat(Qt.TextFormat.PlainText)
        self.route_label.setStyleSheet('font-size:10px;color:#7a8588;')
        self.route_label.setWordWrap(True)
        self.pet.chat.panel.layout().insertWidget(1,self.route_label)
        self.pet.chat.expanded_size=(width,height+47)

    def _refresh_input_targets(self,state):
        if not hasattr(self,'input_target'): return
        self.input_target.blockSignals(True); self.input_target.clear()
        for label,mode in (('自动判断归属','auto'),('新建任务','new'),('普通聊天','chat')):
            self.input_target.addItem(label,(mode,None))
        for row in state.get('tasks',[]):
            task_id=row.get('id') or row.get('task_id')
            if task_id:
                self.input_target.addItem('补充：'+str(row.get('goal','任务'))[:14]+' · #'+task_id[:6],('task',task_id))
        target=state.get('input_target') or {'mode':'auto'}
        selected=self.input_target.findData((target.get('mode','auto'),target.get('task_id')))
        self.input_target.setCurrentIndex(selected)
        self.input_target.blockSignals(False)

    def _input_target_selected(self,index):
        value=self.input_target.itemData(index)
        if value: self._set_input_target(value[1],value[0])

    def _set_input_target(self,task_id,mode):
        try:
            self.c.set_input_target(task_id,mode=mode)
            self.refresh()
        except (ValueError,RuntimeError,OSError) as error:
            self.activity.set_management_notice('输入归属未改变：'+str(error))

    def _answer_task(self,task_id):
        self._set_input_target(task_id,'task')
        self.pet.show_composer()

    def _message_routed(self,payload):
        task_id,request_id=payload.get('task_id'),payload.get('request_id')
        if task_id and request_id:
            self.activity._history.bind_request(request_id,task_id)
        label=('任务 #'+task_id[:6] if task_id else '普通聊天 / 待选择归属')
        text=label+' · '+str(payload.get('summary') or payload.get('relation',''))
        if hasattr(self,'route_label'): self.route_label.setText(text[:90]); self.route_label.setToolTip(text)
        self.activity.set_management_notice(text)
        self.refresh()

    def _manage_task(self,action,task_id):
        try:
            method=(getattr(self.c,'start_task_cleanup',None) if action=='purge' else None)
            result=(method or getattr(self.c,action+'_task'))(task_id)
            status=result.get('status','')
            reason=result.get('reason') or result.get('error') or result.get('blocked_reason')
            self.refresh()
            if action=='trash' and not reason:
                self.activity.offer_restore(task_id)
            self.activity.set_management_notice(str(reason or {'trash':'已移入回收箱，可恢复','restore':'已恢复可见性，等待明确继续','purge':'清理状态：'+str(status)}[action]))
        except (ValueError,RuntimeError,OSError) as error:
            self.activity.set_management_notice('任务操作未完成：'+str(error))

    def _manage_chat(self,action,request_id):
        try:
            method=self.c.start_chat_cleanup if action=='purge' else getattr(self.c,action+'_chat')
            result=method(request_id)
            self.refresh()
            if action=='trash':self.activity.offer_chat_restore(request_id)
            self.activity.set_management_notice(result.get('reason') or {'trash':'对话已移入回收箱，可恢复',
                'restore':'对话已恢复','purge':'对话正在后台清理'}[action])
        except (ValueError,RuntimeError,OSError) as error:
            self.activity.set_management_notice('对话操作未完成：'+str(error))

    def _cleanup_progress(self,payload):
        if self._exiting:return
        self.activity.set_cleanup_progress(payload)
        if payload.get('status') in ('completed','blocked','error','paused'):
            self.refresh()
            self.activity.set_cleanup_progress(payload)

    def _control_task(self,action,task_id):
        try:
            method=getattr(self.c,action+'_task')
            result=method(task_id)
            if action=='continue': self._set_input_target(task_id,'task')
            self.refresh()
            if isinstance(result,dict):
                self.activity.set_management_notice(str(result.get('reason') or result.get('summary') or result.get('status') or '操作已提交'))
        except (ValueError,RuntimeError,OSError) as error:
            self.activity.set_management_notice('任务控制未完成：'+str(error))

    def _authorize_maa_import(self,task_id):
        try:
            result = self.c.authorize_maa_import(task_id)
            self._set_input_target(task_id,'task')
            self.refresh()
            self.activity.set_management_notice(str(result.get('summary') or '已授权，继续原任务；未开始作战。'))
        except (ValueError,RuntimeError,OSError) as error:
            self.activity.set_management_notice('MAA 授权未完成：'+str(error))

    def set_busy(self, busy):
        self.pet.set_busy(busy); self.activity.set_busy(busy)
        if not busy:
            self._capture_pending = False
            if not hasattr(self.c, 'context_message'):
                request_id = self._request_id
                QTimer.singleShot(0, self, lambda: self._finish_request_display(request_id))

    def _finish_request_display(self, request_id):
        if self._exiting or not request_id or self.c.is_busy or request_id in self._request_terminal:
            return
        if request_id in self._task_last_request.values():
            # The user message was parsed, but its workflow has its own lifetime.
            return
        self.activity.finish_request(request_id, 'understood', self._request_summaries.get(request_id, '本条要求的理解已返回；执行结果需单独核实。'))

    def on_activity_progress(self, event):
        self._last_activity_notice = event.get('summary')
        legacy = hasattr(self, 'c') and not hasattr(self.c, 'request_started')
        task_id, epoch = event.get('task_id'), event.get('epoch')
        stage = event.get('stage', '')
        originating_request = event.get('request_id')
        if legacy and not originating_request:
            originating_request=self._request_id
        if task_id and stage == 'task_started' and originating_request:
            self._task_requests[(task_id, epoch)] = originating_request
            self._task_last_request[task_id] = originating_request
        is_intent = stage.startswith('intent_')
        request_id = event.get('request_id')
        if legacy and is_intent:
            request_id=self._request_id
        if not request_id and task_id and not is_intent:
            request_id = self._task_requests.get((task_id, epoch)) or self._task_last_request.get(task_id)
        if not request_id and not task_id and legacy:
            request_id=self._request_id
        self.activity.append_event({**event, **({'request_id':request_id} if request_id else {})})
        if request_id and event.get('stage') in ('task_finished','task_waiting','task_paused','task_stopped','intent_failed'):
            self._request_terminal.add(request_id)
            self.activity.finish_request(request_id, event.get('status') or 'unknown', event.get('summary',''))

    def on_network_progress(self, event):
        # This is the actual read-only search loop, not a speculative task step.
        summary = event.get('query') or event.get('url') or event.get('label', '')
        legacy={'request_id':self._request_id} if hasattr(self, 'c') and not hasattr(self.c,'request_started') and self._request_id else {}
        self.activity.append_event({**event, **legacy, 'role': 'research', 'summary': str(summary)})

    def on_notice(self, text):
        self.pet.set_status(text)
        if (text != self._last_activity_notice and not str(text).startswith('研究Agent：')
                and text != 'MAA 工作流证据已保存'):
            self.activity.set_notice(text)

    def show_error(self, text):
        self._capture_pending = False
        self.activity.set_error(text)
        if self._request_id and not hasattr(self.c, 'context_message'):
            self._request_terminal.add(self._request_id)
            self.activity.finish_request(self._request_id, 'failed', text)
        self.pet.present_message('system', text)

    def show_observation(self, observation):
        self._displayed_observation = dict(observation)
        self.preview.load(observation.get('path', ''))
        text = f"{observation.get('width', '?')} × {observation.get('height', '?')} · {observation.get('method', '未知方式')} · 仅表示已读取画面"
        self.preview_caption.setText(text)
        self.activity.append_event({'stage':'observation', 'role':'maa', 'status':'completed',
            'summary':'已读取绑定窗口；画面内容与作战结果仍需核实。'})
        self._capture_pending = False

    def show_plan(self, plan):
        self.activity.append_event({'stage':'observation_analysis', 'role':'leader', 'status':'completed',
            'summary':plan.get('summary') or '本次画面分析已返回，尚未执行操作。'})

    def open_activity(self):
        self.refresh()
        self.activity.show_at(self.pet.geometry())

    def show_recent_preview(self):
        self.preview_dialog.show(); self.preview_dialog.raise_(); self.preview_dialog.activateWindow()

    def show_pet(self): self.pet.show()

    def submit_pet_text(self, text):
        self.speech_output.stop(); self.c.submit(text, 0)

    def set_speech_language(self, language):
        setter = getattr(self.speech_output, 'set_language', None)
        if callable(setter): setter(language)
        if hasattr(self.c, 'set_response_language'): self.c.set_response_language(language)

    def toggle_voice(self):
        if self.voice.is_recording: self.voice.stop()
        else: self.speech_output.stop(); self.voice.start()

    def open_settings(self, section='model'):
        from .settings_ui import PetSettings
        if self._settings_dialog is None: self._settings_dialog = PetSettings(self)
        self._settings_dialog.select(section)

    def bind_dialog(self):
        dialog = QDialog(self); dialog.setWindowTitle('选择游戏窗口')
        dialog.setStyleSheet(PANEL_STYLE); dialog.resize(540, 220)
        layout = QVBoxLayout(dialog)
        caption = QLabel('选择已进入游戏的窗口。启动器不能代替游戏画面。')
        caption.setWordWrap(True); layout.addWidget(caption)
        combo = QComboBox(); layout.addWidget(combo)
        others = QCheckBox('显示其他可见窗口'); layout.addWidget(others)
        def populate():
            combo.clear()
            try:
                for window in self.c.windows.list_windows(game_only=not others.isChecked()):
                    combo.addItem(window.title + (' · 已最小化' if window.minimized else ''), window.hwnd)
                if combo.count() == 0: combo.addItem('没有找到游戏窗口', None)
            except Exception: combo.addItem('暂时无法读取窗口列表，请重试。', None)
        others.toggled.connect(populate); populate()
        row = QHBoxLayout(); layout.addLayout(row)
        reload_button = QPushButton('刷新'); reload_button.clicked.connect(populate); row.addWidget(reload_button)
        row.addStretch(); cancel = QPushButton('取消'); cancel.clicked.connect(dialog.reject); row.addWidget(cancel)
        bind = QPushButton('绑定'); bind.setObjectName('Primary'); row.addWidget(bind)
        def accept():
            if combo.currentData() is None: return
            try: self.c.bind_window(combo.currentData()); dialog.accept(); self.refresh()
            except Exception: self.show_error('绑定未完成，请重新选择已打开的游戏窗口。')
        bind.clicked.connect(accept); dialog.exec()

    def capture_from_pet(self):
        if not getattr(self.c, 'screen_capture_allowed', False):
            self.open_settings('screen'); return
        if self.c.is_busy or self._capture_pending:
            self.on_notice('正在处理上一条；停止按钮仍可使用。'); return
        if not self.c.windows.bound_window():
            try:
                windows = [w for w in self.c.windows.list_windows(game_only=True) if w.title.strip() == '明日方舟']
                if len(windows) == 1: self.c.bind_window(windows[0].hwnd)
            except Exception: pass
        if not self.c.windows.bound_window(): self.bind_dialog()
        if not self.c.windows.bound_window(): return
        self._displayed_observation = None
        self.preview.original = QPixmap(); self.preview.clear()
        self.preview_caption.setText('正在读取新的画面；旧截图不作为本次证据。')
        self._capture_pending = True
        if self.c.capture() is False: self._capture_pending = False

    def pause(self): self.speech_output.stop(); self.c.pause()
    def resume(self): self.c.resume()
    def stop(self):
        self._capture_pending = False
        awaiting_maa = bool(getattr(getattr(self.c, 'execution', None), 'active', False))
        self.speech_output.stop(); self.c.stop()
        self.activity.append_event({'role':'leader',
            'stage':'task_waiting' if awaiting_maa else 'task_stopped',
            'status':'needs_input' if awaiting_maa else 'cancelled',
            'summary':'本地输入已撤销，等待 MAA 停止回读。' if awaiting_maa else
                      '本次本地处理已停止；迟到的对话或查询结果不会继续采用。'})

    def restart_elevated(self):
        if sys.platform != 'win32':
            self.show_error('管理员重启仅适用于 Windows。'); return
        command = Path(sys.executable).resolve()
        arguments = [] if getattr(sys, 'frozen', False) else [str(Path(__file__).resolve().parents[1] / 'main.py')]
        arguments += ['--data-dir', str(self.c.data_dir), '--key-file', str(self.c.key_path), '--wait-for-profile']
        shell = ctypes.WinDLL('shell32', use_last_error=True)
        shell.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
        shell.ShellExecuteW.restype = ctypes.c_void_p
        self.c.pause()
        result = shell.ShellExecuteW(None, 'runas', str(command), subprocess.list2cmdline(arguments), str(command.parent), 1)
        if int(result or 0) > 32: self.quit_application()
        else: self.show_error('管理员重启未完成，当前黍和任务检查点已保留。')

    def quit_application(self):
        if self._exiting: return
        self._exiting = True; self._capture_pending = False
        self.speech_output.stop()
        # Exit pauses durable work. It is intentionally distinct from Stop.
        self.c.close(); self.speech_output.close(); self.voice.cancel()
        self.pet.close(); self.activity.shutdown(); self.preview_dialog.close()
        if self._settings_dialog is not None: self._settings_dialog.close()
        if self.tray is not None: self.tray.hide()
        self.hide(); QApplication.quit()

    def closeEvent(self, event):
        if self._exiting: event.accept()
        else: event.ignore(); self.hide(); self.show_pet()

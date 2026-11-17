"""Focused settings opened from the desktop pet."""
from pathlib import Path
from tempfile import NamedTemporaryFile
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QCheckBox, QComboBox, QDoubleSpinBox, QStackedWidget, QFormLayout, QScrollArea, QFrame)


class PetSettings(QDialog):
    def __init__(self, workspace):
        super().__init__(workspace)
        self.workspace=workspace;self.c=workspace.c
        self.setWindowTitle('黍 · 设置');self.resize(650,690)
        self.setStyleSheet('''
QDialog {background:#fbfcfa;}
QWidget {color:#354146;font-family:"Microsoft YaHei UI","Microsoft YaHei","Segoe UI";font-size:12px;}
QLabel#Heading {font-size:23px;font-weight:600;color:#283338;}
QLabel#Muted {color:#7a8588;font-size:12px;}
QLineEdit,QComboBox,QDoubleSpinBox {background:white;border:1px solid #dde4e2;border-radius:9px;padding:10px;color:#354146;selection-background-color:#dceafd;}
QLineEdit:focus,QComboBox:focus,QDoubleSpinBox:focus {border-color:#a8c6ed;}
QComboBox QAbstractItemView {background:white;color:#354146;selection-background-color:#e7effa;}
QPushButton {background:#f0f3f1;border:1px solid #dfe6e3;border-radius:9px;padding:9px 13px;color:#49575b;min-height:18px;}
QPushButton:hover {background:#e5eee9;}
QPushButton#Primary {background:#dfebfd;color:#355a83;border:1px solid #cee0fa;}
QCheckBox {color:#435156;spacing:9px;padding:7px 0;}
QCheckBox::indicator {width:16px;height:16px;border-radius:4px;border:1px solid #b7c5c3;background:white;}
QCheckBox::indicator:checked {background:#aac9f0;border-color:#aac9f0;}
''')
        layout=QVBoxLayout(self);layout.setContentsMargins(26,24,26,24);layout.setSpacing(16)
        title=QLabel('黍的设置');title.setObjectName('Heading');layout.addWidget(title)
        self.section=QComboBox();self.section.addItems(['模型与 API Key','屏幕与 MAA 操作','桌宠与输入'])
        layout.addWidget(self.section);self.pages=QStackedWidget()
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet('QScrollArea{background:transparent;border:0;} QScrollBar:vertical{background:#f1f4f2;width:6px;border:0;} QScrollBar::handle:vertical{background:#cdd7d1;border-radius:3px;min-height:24px;} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}')
        scroll.setWidget(self.pages);layout.addWidget(scroll,1)
        self._model();self._screen();self._general()
        self.section.currentIndexChanged.connect(self.pages.setCurrentIndex)
        self.feedback=QLabel('');self.feedback.setWordWrap(True);self.feedback.setObjectName('Muted');layout.addWidget(self.feedback)
        footer=QHBoxLayout();footer.addStretch();close=QPushButton('完成');close.clicked.connect(self.accept);footer.addWidget(close);layout.addLayout(footer)
        self.c.notice.connect(self.feedback.setText);self.c.error.connect(self.feedback.setText)

    def _page(self):
        page=QWidget();layout=QVBoxLayout(page);layout.setContentsMargins(0,8,0,0);layout.setSpacing(14)
        self.pages.addWidget(page);return layout

    def _text(self,text):
        label=QLabel(text);label.setWordWrap(True);label.setObjectName('Muted');return label

    def _model(self):
        layout=self._page();form=QFormLayout();form.setSpacing(14)
        self.key=QLineEdit();self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText('留空沿用本地 Key；输入新 Key 后点击保存')
        self.role_model=QLineEdit(self.c.config.model)
        game=getattr(self.c,'game_config',self.c.config)
        self.game_model=QLineEdit(game.model);self.url=QLineEdit(self.c.config.base_url)
        form.addRow('DeepSeek API Key',self.key);form.addRow('角色对话模型',self.role_model)
        form.addRow('游戏决策模型',self.game_model);form.addRow('API 地址',self.url);layout.addLayout(form)
        self.thinking=QCheckBox('游戏决策启用推理模式');self.thinking.setChecked(getattr(game,'thinking',True));layout.addWidget(self.thinking)
        layout.addWidget(self._text('角色交流与游戏观察使用独立会话和客户端，可以共用这一把 Key。游戏推理会消耗更多 token；两套会话本身不等于已接通自主游戏。'))
        row=QHBoxLayout();save=QPushButton('保存并重新读取');save.setObjectName('Primary');save.clicked.connect(self.save)
        test=QPushButton('测试连接');test.clicked.connect(self.c.test_connection)
        text=QPushButton('打开 Key 文本');text.clicked.connect(lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.c.key_path))))
        row.addWidget(save);row.addWidget(test);row.addWidget(text);layout.addLayout(row);layout.addStretch()

    def _screen(self):
        layout=self._page()
        self.allowed=QCheckBox('允许黍读取 MAA 及所选游戏窗口的画面')
        self.allowed.setChecked(getattr(self.c,'screen_capture_allowed',False))
        self.allowed.toggled.connect(self._permission_changed);layout.addWidget(self.allowed)
        layout.addWidget(self._text('MAA 导入读取当前 MAA 客户区；游戏观察读取绑定窗口。截图保存在本机，本地识别不足时由配置的 DeepSeek 视觉模型分析。'))
        self.input_allowed=QCheckBox('本次启动允许黍操作 MAA 客户端')
        self.input_allowed.setChecked(getattr(self.c,'game_input_allowed',False))
        self.input_allowed.toggled.connect(self.c.set_game_input_permission);layout.addWidget(self.input_allowed)
        layout.addWidget(self._text('开启后，MAA Agent 可使用客户端的可见控件配置任务，研究和诊断 Agent 不直接输入。游戏画面用于观察；实际游戏操作交给 MAA。授权不随重启保存，可随时从桌宠停止。'))
        layout.addWidget(self._text('使用绑定窗口的 Windows Graphics Capture 后台画面。游戏可以被其他窗口遮挡，无需切换前台；请保持游戏未最小化。失败时会给出原因，不把桌面区域裁剪当作游戏截图。'))
        self.bound=QLabel('');self.bound.setWordWrap(True);layout.addWidget(self.bound)
        self.input_permission_info=self._text('');layout.addWidget(self.input_permission_info)
        self.restart_admin=QPushButton('以管理员身份重启')
        self.restart_admin.clicked.connect(self.workspace.restart_elevated)
        layout.addWidget(self.restart_admin)
        layout.addWidget(self._text('仅当 MAA 或游戏需要更高权限时使用。重启会停止当前任务并保留对话；Windows 提示由你自行确认。'))
        row=QHBoxLayout();bind=QPushButton('选择游戏窗口');bind.clicked.connect(self.bind)
        observe=QPushButton('观察一次');observe.setObjectName('Primary');observe.clicked.connect(self.observe)
        row.addWidget(bind);row.addWidget(observe);layout.addLayout(row)
        layout.addStretch();layout.addWidget(self._text('撤销画面读取权限会停止本地处理、丢弃旧观察。它不会暂停游戏本身。'))

    def _general(self):
        layout=self._page();self.follow=QCheckBox('黍的视线跟随鼠标');self.follow.setChecked(self.workspace.pet.canvas.follow_cursor)
        self.follow.toggled.connect(self.workspace.pet.follow_action.setChecked);layout.addWidget(self.follow)
        self.animation=QCheckBox('播放原图集中的动作');self.animation.setChecked(self.workspace.pet.canvas.animated)
        self.animation.toggled.connect(self.workspace.pet.animated_action.setChecked);layout.addWidget(self.animation)
        layout.addWidget(self._text('单击黍：展开输入。拖动：移动桌宠。双击：跳跃。鼠标靠近时显示工具条，移开后收起；草稿会保留。\n\n语音按钮直接开始本机离线中文识别，录音状态显示在桌宠内。再次点击结束，识别文字进入输入框，由你检查并发送。首次使用需已有 Windows 麦克风访问权限；不再打开系统听写面板，音频不上传。'))
        self.speech_enabled=QCheckBox('朗读黍的新回复')
        self.speech_enabled.setChecked(self.workspace.speech_output.enabled)
        self.speech_enabled.toggled.connect(self.workspace.speech_output.set_enabled)
        layout.addWidget(self.speech_enabled)
        speech_form=QFormLayout();speech_form.setSpacing(10)
        self.speech_language=QComboBox();self.speech_language.addItem('中文','zh');self.speech_language.addItem('日本語','ja')
        self.speech_language.setCurrentIndex(max(0,self.speech_language.findData(getattr(self.workspace.speech_output,'language','zh'))))
        self.speech_language.setEnabled(callable(getattr(self.workspace.speech_output,'set_language',None)))
        self.speech_language.currentIndexChanged.connect(lambda _index:self.workspace.set_speech_language(self.speech_language.currentData()))
        speech_form.addRow('回复与朗读语言',self.speech_language)
        self.speech_speed=QDoubleSpinBox();self.speech_speed.setRange(1.0,1.2);self.speech_speed.setDecimals(2)
        self.speech_speed.setSingleStep(.04);self.speech_speed.setSuffix(' ×');self.speech_speed.setKeyboardTracking(False)
        self.speech_speed.setValue(getattr(self.workspace.speech_output,'speed',1.08))
        set_speed=getattr(self.workspace.speech_output,'set_speed',None)
        self.speech_speed.setEnabled(callable(set_speed))
        if callable(set_speed):self.speech_speed.valueChanged.connect(set_speed)
        speech_form.addRow('朗读速度',self.speech_speed);layout.addLayout(speech_form)
        self.speech_voice=QComboBox()
        for voice in self.workspace.speech_output.voices:self.speech_voice.addItem(voice['label'],voice['id'])
        self.speech_voice.setCurrentIndex(max(0,self.speech_voice.findData(self.workspace.speech_output.voice_id)))
        self.speech_voice.currentIndexChanged.connect(lambda _index:self.workspace.speech_output.set_voice(self.speech_voice.currentData()))
        layout.addWidget(self.speech_voice)
        layout.addWidget(self._text('中文和日语分别使用你提供的黍原配音作为固定声线参考。一条回复连续合成，标点用于自然停顿；点击试听可听当前语言和语速。'))
        layout.addWidget(self._text('每次最多朗读前 220 字，气泡随实际播放进度渐渐显示文字。首次启动会加载语音，后续复用；关闭朗读可释放模型。'))
        layout.addWidget(self._text('语音环境已就绪。' if self.workspace.speech_output.ready else '本地 GPU 语音尚未安装完成。文字交流仍可使用。'))
        speech_row=QHBoxLayout(); preview=QPushButton('试听当前音色'); stop=QPushButton('停止播报')
        preview.clicked.connect(self.workspace.speech_output.preview); stop.clicked.connect(self.workspace.speech_output.stop)
        speech_row.addWidget(preview); speech_row.addWidget(stop); layout.addLayout(speech_row)
        self.workspace.speech_output.notice.connect(lambda text:self.feedback.setText(text))
        self.workspace.speech_output.error.connect(lambda text:self.feedback.setText(text))
        activity=QPushButton('查看任务进度');activity.clicked.connect(lambda:(self.accept(),self.workspace.open_activity()))
        layout.addWidget(activity);layout.addStretch()

    def select(self,section):
        self.section.setCurrentIndex({'model':0,'screen':1,'general':2}.get(section,0))
        self.refresh_binding();self.show();self.raise_();self.activateWindow()

    def refresh_binding(self):
        for checkbox,value in ((self.allowed,getattr(self.c,'screen_capture_allowed',False)),
                               (self.input_allowed,getattr(self.c,'game_input_allowed',False))):
            checkbox.blockSignals(True)
            checkbox.setChecked(value is True)
            checkbox.blockSignals(False)
        try:window=self.c.windows.bound_window()
        except Exception:window=None
        self.bound.setText('当前窗口：'+(window.title if window else '尚未绑定'))
        status=(self.c.windows.input_permission_status() if hasattr(self.c.windows,'input_permission_status')
                else {'requires_elevation':False,'message':'绑定游戏后检查输入权限。'})
        if status.get('requires_elevation'):
            permission_text=('绑定窗口正以管理员权限运行，当前桌宠为普通模式。' if status.get('target_elevated')
                             else '绑定程序声明需要管理员权限，当前桌宠为普通模式。')
        elif status.get('own_elevated'):permission_text='当前桌宠已使用管理员模式。'
        else:permission_text='当前未发现需要提升权限的依据。'
        self.input_permission_info.setText(permission_text)
        self.restart_admin.setVisible(status.get('requires_elevation') is True)

    def bind(self):
        self.workspace.bind_dialog();self.refresh_binding()

    def observe(self):
        if not getattr(self.c,'screen_capture_allowed',False):
            self.feedback.setText('请先决定是否允许读取所选游戏窗口。');return
        self.accept();self.workspace.capture_from_pet()

    def _permission_changed(self,allowed):
        self.c.set_capture_permission(allowed)
        self.feedback.setText('已允许读取绑定窗口。' if allowed else '已撤销画面读取权限。')

    def save(self):
        key=self.key.text().strip()
        if key and (len(key)<8 or any(ord(ch)>127 or ch.isspace() for ch in key)):
            self.feedback.setText('Key 格式不正确，请只填写 API Key。');return
        try:
            self.c._validate_settings(self.role_model.text().strip(),self.url.text().strip())
            self.c._validate_settings(self.game_model.text().strip(),self.url.text().strip())
            if key:
                destination=Path(self.c.key_path);temporary=None
                try:
                    with NamedTemporaryFile('w',encoding='utf-8',dir=destination.parent,
                            prefix=f'.{destination.name}.',suffix='.tmp',delete=False) as staging:
                        temporary=Path(staging.name)
                        staging.write(key+'\n')
                    temporary.replace(destination)
                finally:
                    if temporary is not None:temporary.unlink(missing_ok=True)
                self.key.clear()
            self.c.save_settings(self.role_model.text().strip(),self.url.text().strip(),
                                 game_model=self.game_model.text().strip(),game_thinking=self.thinking.isChecked())
            self.role_model.setText(self.c.config.model);self.url.setText(self.c.config.base_url)
            game=getattr(self.c,'game_config',self.c.config)
            self.game_model.setText(game.model);self.thinking.setChecked(getattr(game,'thinking',True))
            self.feedback.setText('已保存。可点击测试连接，或回到黍身边继续。')
        except Exception:
            self.feedback.setText('设置未能保存，请检查 Key 文件和本地目录是否可写。')

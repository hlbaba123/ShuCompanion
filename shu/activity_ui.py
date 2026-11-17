"""Small, on-demand activity view for the pet's real agent/tool events.

This is a presentation-only widget. It neither invokes tools nor persists raw
model or tool payloads. The session history is bounded and safe to render.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
import json
from pathlib import Path
from urllib.parse import quote, urlsplit

from PySide6.QtCore import QEasingCurve, QPoint, QRect, QPropertyAnimation, Qt, QTimer, Signal, QSize, QEvent
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QTextCursor, QPainter, QPen, QPixmap, QIcon
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
                              QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget, QListWidget, QListWidgetItem, QComboBox)

from .activity_history import (ActivityHistory, TERMINAL_TASK_STATUSES, safe_text as _text,
    public_source_url as _source_url, event_fields as _event_fields)
from .activity_events import TOOL_NAMES
from .activity_timeline import FAILURE, SUCCESS, project_timeline


_ROLES = {
    'leader': 'Leader · 黍', 'research': '研究 Agent', 'catalog': '目录 Agent', 'maa': 'MAA Agent',
    'diagnosis': '诊断 Agent', 'dialogue': '黍 · 对话', 'persona': '黍 · 对话',
}
_STAGES = {
    'dispatch_started': '开始处理', 'dispatch_completed': '已返回结果',
    'dispatch_reused': '复用已有结果', 'tool_started': '调用工具',
    'tool_finished': '工具已返回', 'task_started': '任务已开始',
    'task_finished': '任务已结束', 'notice': '状态通知', 'error': '出错',
    'intent_started': '理解要求', 'intent_finished': '要求已解析', 'intent_failed': '要求解析失败',
    'task_waiting': '等待处理', 'task_paused': '任务已暂停', 'task_stopped': '任务已停止',
    'observation': '画面已取得', 'observation_analysis': '画面分析已返回',
}
_TASK_STATUS = {
    'draft': '等待处理', 'observing': '正在观察', 'running': '执行中',
    'paused': '已暂停', 'needs_input': '等待处理', 'completed': '已结束',
    'import_verified': '已核实导入', 'failed': '出错', 'cancelled': '已停止',
    'research_verified': '资料已核实',
    'planned_verified': '计划交付已核实', 'catalog_verified': '活动目录已核实',
    'researched': '资料已返回',
    'understood': '理解已返回',
    'understood': '已理解要求', 'accepted':'已接收', 'waiting':'等待处理',
}
_TOOLS = {'web_search': '网页搜索', 'web_fetch': '网页读取'}
_SHANGHAI = timezone(timedelta(hours=8))


def _time_label(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(_SHANGHAI)
        return stamp.strftime('%H:%M:%S')
    except (ValueError, TypeError, AttributeError):
        return '—'


STYLE = '''
QWidget {font-family:"Microsoft YaHei UI","Microsoft YaHei","Segoe UI";font-size:12px;color:#354247;}
QFrame#ActivitySurface {background:#fcfdfc;border:1px solid #e0e6e5;border-radius:19px;}
QLabel {background:transparent;}
QLabel#ActivityTitle {font-size:18px;font-weight:600;color:#29363c;}
QLabel#ActivityMuted {font-size:11px;color:#889399;}
QLabel#ActivityGoal {color:#6b777d;font-size:11px;}
QLabel#ActivityAgent {font-size:12px;font-weight:600;color:#45698c;}
QLabel#ActivitySummary {font-size:13px;color:#39494f;}
QPushButton {background:#f1f4f3;border:1px solid #e0e6e3;border-radius:11px;padding:8px 16px;min-height:22px;}
QPushButton:hover {background:#e7eeeb;}
QPushButton:focus {border:1px solid #92b8e2;}
QPushButton:disabled {color:#a6aeaf;background:#f4f6f5;border-color:#e9edeb;}
QPushButton#ActivityClose {background:transparent;border:0;border-radius:12px;padding:0;font-size:21px;color:#879197;min-height:32px;min-width:32px;}
QPushButton#ActivityClose:hover {background:#edf1ef;color:#43545b;}
QPushButton#ActivityStop {background:#f8eeed;border-color:#ecdbd8;color:#9c625f;}
QPushButton#ActivityStop:hover {background:#f0dfdc;}
QPushButton#ActivityStop:disabled {background:#f5f6f5;border-color:#e5e9e6;color:#a6aeaf;}
QTextBrowser {background:transparent;border:0;padding:0;selection-background-color:#dceafa;color:#445359;}
QScrollBar:vertical {background:transparent;width:5px;margin:2px 0;}
QScrollBar::handle:vertical {background:#d5ddda;min-height:26px;border-radius:2px;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}
QWidget#HistorySidebar {background:#f4f7f5;border-radius:12px;}
QListWidget#HistoryList {background:transparent;border:0;outline:0;color:#6a787c;font-size:11px;}
QListWidget#HistoryList::item {border-radius:8px;padding:8px 6px;margin:2px 0;}
QListWidget#HistoryList::item:selected {background:#e6eef8;color:#45698c;}
QListWidget#HistoryList::item:hover {background:#edf2ee;}
QComboBox#HistorySelector {background:#f4f7f5;border:1px solid #e0e6e3;border-radius:8px;padding:7px;color:#5b6e77;}
'''


class ActivityPanel(QDialog):
    """Pet-anchored activity history; all setters leave visibility unchanged."""
    stop_requested = Signal()
    trash_requested = Signal(str)
    restore_requested = Signal(str)
    purge_requested = Signal(str)
    chat_trash_requested = Signal(str)
    chat_restore_requested = Signal(str)
    chat_purge_requested = Signal(str)
    target_requested = Signal(str)
    continue_requested = Signal(str)
    maa_permission_requested = Signal(str)
    cancel_requested = Signal(str)
    task_stop_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle('黍 · 任务进度')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(STYLE)
        self.resize(620, 520)
        self.setMinimumSize(300, 270)
        self.events = []
        self.last_event = None
        self._active = self._busy = self._closing = False
        self._shutting_down = False
        self._task_id = None
        self._render_generation = 0
        self._heading_event = None
        self._history = ActivityHistory()
        self.current_request_id = None
        self.selected_request_id = None
        self._history_view_locked = False
        self._live_task = None
        self._timeline_steps = []
        self._timeline_expanded = {}
        self._management = None
        self.selected_task_id = None
        self._recycle = False
        self._confirmation = None
        self._geometry_path = None
        self._placed = False
        self._drag_origin = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 14)
        self.surface = QFrame(self)
        self.surface.setObjectName('ActivitySurface')
        shadow = QGraphicsDropShadowEffect(self.surface)
        shadow.setBlurRadius(20)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(31, 53, 62, 24))
        self.surface.setGraphicsEffect(shadow)
        outer.addWidget(self.surface)
        columns = QHBoxLayout(self.surface)
        columns.setContentsMargins(12,12,12,12); columns.setSpacing(14)
        self.history_sidebar = QWidget()
        self.history_sidebar.setObjectName('HistorySidebar')
        self.history_sidebar.setFixedWidth(148)
        left = QVBoxLayout(self.history_sidebar); left.setContentsMargins(10,14,10,8); left.setSpacing(8)
        history_title = QLabel('历史记录'); history_title.setObjectName('ActivityMuted'); left.addWidget(history_title)
        self.history_list = QListWidget(); self.history_list.setObjectName('HistoryList')
        self.history_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.history_list.setWordWrap(True)
        self.history_list.currentItemChanged.connect(self._history_item_changed)
        left.addWidget(self.history_list,1); columns.addWidget(self.history_sidebar)
        detail = QWidget(); detail.setMinimumWidth(0); columns.addWidget(detail,1)
        layout = QVBoxLayout(detail)
        layout.setContentsMargins(21, 17, 21, 18)
        layout.setSpacing(9)
        self.history_selector = QComboBox(); self.history_selector.setObjectName('HistorySelector')
        self.history_selector.currentIndexChanged.connect(self._history_selector_changed)
        self.history_selector.hide(); layout.addWidget(self.history_selector)
        self.title_area = QWidget()
        header = QHBoxLayout(self.title_area)
        header.setContentsMargins(0,0,0,0)
        title = QLabel('任务进度')
        title.setObjectName('ActivityTitle')
        header.addWidget(title)
        version = _text(QApplication.applicationVersion(), 64) or '源码'
        self.version_label = QLabel(version)
        self.version_label.setObjectName('ActivityMuted')
        self.version_label.setTextFormat(Qt.TextFormat.PlainText)
        self.version_label.setToolTip('当前程序版本：'+version)
        header.addWidget(self.version_label)
        header.addStretch()
        self.delete_button = QPushButton()
        icon=QPixmap(18,18); icon.fill(Qt.GlobalColor.transparent)
        painter=QPainter(icon); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#879197'),1.4))
        painter.drawLine(4,5,14,5); painter.drawLine(7,3,11,3)
        painter.drawLine(5,6,6,15); painter.drawLine(6,15,12,15); painter.drawLine(12,15,13,6)
        painter.drawLine(8,8,8,12); painter.drawLine(10,8,10,12); painter.end()
        self.delete_button.setIcon(QIcon(icon))
        self.delete_button.setFixedSize(32,32)
        self.delete_button.setStyleSheet('background:transparent;border:0;padding:4px;')
        self.delete_button.setAccessibleName('删除选定任务')
        self.delete_button.setToolTip('将选定任务及关联消息移入回收箱')
        self.delete_button.setEnabled(False)
        self.delete_button.hide()
        self.delete_button.clicked.connect(self._trash_selected)
        header.addWidget(self.delete_button)
        close = QPushButton('×')
        close.setObjectName('ActivityClose')
        close.setAccessibleName('收起任务进度')
        close.setToolTip('收起')
        close.clicked.connect(self.hide_animated)
        header.addWidget(close)
        layout.addWidget(self.title_area)
        for draggable in (self.title_area, title, self.version_label):
            draggable.installEventFilter(self)
        self.goal = QLabel('还没有进行中的任务')
        self.goal.setObjectName('ActivityGoal')
        self.goal.setTextFormat(Qt.TextFormat.PlainText)
        self.goal.setWordWrap(True)
        self.goal.setMaximumHeight(38)
        layout.addWidget(self.goal)
        row = QHBoxLayout()
        self.agent = QLabel('角色未标明')
        self.agent.setObjectName('ActivityAgent')
        self.agent.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(self.agent)
        row.addStretch()
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(self.status)
        layout.addLayout(row)
        self.summary = QLabel('黍开始处理后，会在这里显示实际进展。')
        self.summary.setObjectName('ActivitySummary')
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        self.summary.setMaximumHeight(84)
        layout.addWidget(self.summary)
        self.question_card = QLabel()
        self.question_card.setTextFormat(Qt.TextFormat.PlainText)
        self.question_card.setWordWrap(True)
        self.question_card.hide()
        question_row=QHBoxLayout()
        question_row.addWidget(self.question_card,1)
        self.answer_button = QPushButton('回答此问题')
        self.answer_button.hide()
        self.answer_button.clicked.connect(lambda: self.target_requested.emit(self.selected_task_id) if self.selected_task_id else None)
        self.answer_button.setStyleSheet('padding:3px 8px;min-height:20px;font-size:11px;')
        question_row.addWidget(self.answer_button)
        layout.addLayout(question_row)
        self.maa_permission_button=QPushButton('允许画面读取及本次 MAA 操作并继续')
        self.maa_permission_button.hide()
        self.maa_permission_button.setStyleSheet('background:#dfebfd;color:#355a83;border:1px solid #cee0fa;padding:5px 8px;min-height:22px;')
        self.maa_permission_button.clicked.connect(self._request_maa_permission)
        layout.addWidget(self.maa_permission_button)
        task_controls=QHBoxLayout()
        self.continue_button=QPushButton('继续此任务')
        self.cancel_task_button=QPushButton('取消此任务')
        self.task_stop_button=QPushButton('停止此任务')
        for button,signal in ((self.continue_button,self.continue_requested),
                (self.cancel_task_button,self.cancel_requested),(self.task_stop_button,self.task_stop_requested)):
            button.hide()
            button.setStyleSheet('padding:4px 8px;min-height:20px;font-size:11px;')
            button.clicked.connect(lambda checked=False,signal=signal:signal.emit(self.selected_task_id) if self.selected_task_id else None)
            task_controls.addWidget(button)
        layout.addLayout(task_controls)
        self.confirm_label = QLabel()
        self.confirm_label.setWordWrap(True)
        self.confirm_label.setTextFormat(Qt.TextFormat.PlainText)
        self.confirm_label.hide()
        layout.addWidget(self.confirm_label)
        confirmations = QHBoxLayout()
        self.confirm_button = QPushButton('确认')
        self.confirm_button.clicked.connect(self._confirm_management)
        self.cancel_confirm = QPushButton('取消')
        self.cancel_confirm.clicked.connect(self._clear_confirmation)
        for button in (self.confirm_button,self.cancel_confirm):
            button.hide(); confirmations.addWidget(button)
        layout.addLayout(confirmations)
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet('color:#e7ece9;')
        layout.addWidget(divider)
        self.timeline = QTextBrowser()
        self.timeline.setAccessibleName('任务步骤与可展开的实际工具记录')
        self.timeline.setReadOnly(True)
        self.timeline.setOpenExternalLinks(False)
        self.timeline.setOpenLinks(False)
        self.timeline.anchorClicked.connect(self._open_source)
        self.timeline.document().setDefaultStyleSheet(
            'p{margin-top:5px;margin-bottom:17px;line-height:135%;}'
            'a{color:#487bb0;text-decoration:none;}')
        self.timeline.setPlaceholderText('还没有执行记录。')
        layout.addWidget(self.timeline, 1)
        footer = QHBoxLayout()
        self.history_hint = QLabel('本次会话')
        self.history_hint.setObjectName('ActivityMuted')
        footer.addWidget(self.history_hint)
        footer.addStretch()
        self.restore_button = QPushButton('恢复')
        self.restore_button.hide()
        self.restore_button.clicked.connect(self._restore_selected)
        footer.addWidget(self.restore_button)
        self.undo_button=QPushButton('撤销删除')
        self.undo_button.hide()
        self._undo_task_id=None
        self._undo_request_id=None
        self.undo_button.clicked.connect(self._undo_trash)
        footer.addWidget(self.undo_button)
        self.purge_button = QPushButton('彻底删除')
        self.purge_button.setObjectName('ActivityStop')
        self.purge_button.hide()
        self.purge_button.clicked.connect(self._purge_selected)
        footer.addWidget(self.purge_button)
        self.stop_button = QPushButton('停止')
        self.stop_button.setObjectName('ActivityStop')
        self.stop_button.setEnabled(False)
        self.stop_button.setToolTip('停止本次处理')
        self.stop_button.clicked.connect(self.stop_requested)
        footer.addWidget(self.stop_button)
        layout.addLayout(footer)
        self.recycle_button = QPushButton('回收箱（0）')
        self.recycle_button.hide()
        self.recycle_button.clicked.connect(self._toggle_recycle)
        left.addWidget(self.recycle_button)
        self.compact_recycle_button=QPushButton('回收箱（0）')
        self.compact_recycle_button.clicked.connect(self._toggle_recycle)
        self.compact_recycle_button.hide()
        layout.insertWidget(1,self.compact_recycle_button)
        self.return_to_pet = QPushButton('移回桌宠旁')
        self.return_to_pet.hide()
        self.return_to_pet.clicked.connect(self._reset_anchor)
        self.return_to_pet.setStyleSheet('padding:2px 5px;min-height:18px;font-size:10px;')
        left.addWidget(self.return_to_pet)
        self._motion = QPropertyAnimation(self, b'windowOpacity', self)
        self._motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._motion.finished.connect(self._motion_finished)
        self._set_status('空闲')

    def _history_call(self, method, *args):
        try:
            return method(*args)
        except OSError:
            self.history_hint.setText('历史暂未保存')
            self.history_hint.setToolTip('本次显示仍可继续，历史文件暂时无法写入。')
            return False

    def configure_history(self, path):
        """Attach a portable JSON display store; it is not execution state."""
        self._history = ActivityHistory(path)
        self.current_request_id = None
        self.selected_request_id = None
        self._history_view_locked = False
        records = self._history.records()
        self._refresh_history_list()
        if records:
            self._select_request(records[0]['request_id'], user=False)
        return len(records)

    def begin_request(self, request_id, text, timestamp=None):
        accepted = self._history_call(self._history.begin, request_id, text, timestamp)
        if accepted is None or self._history.get(request_id) is None:
            return False
        self.current_request_id = request_id
        self._refresh_history_list()
        if (not self._recycle and not self._history_view_locked
                and self.selected_task_id is None):
            self._select_request(request_id, user=False)
        return True

    def finish_request(self, request_id, status, summary=''):
        result = self._history_call(self._history.finish, request_id, status, summary)
        self._refresh_history_list()
        if self.selected_request_id == request_id:
            self._render_record()
        return result

    def select_request(self, request_id):
        """Read-only view selection. Does not emit any task or control action."""
        return self._select_request(request_id, user=True)

    def _select_request(self, request_id, *, user):
        chat=next((row for row in self._chat_rows() if row['request_id']==request_id),None)
        if self._history.get(request_id) is None and chat is None:
            return False
        if self.selected_request_id != request_id or self.selected_task_id is not None:
            self._clear_confirmation()
        self.selected_task_id = None
        self.selected_request_id = request_id
        if user:
            self._history_view_locked = request_id != self.current_request_id
        self._refresh_history_list()
        if chat is not None:
            self._render_chat(chat)
        else:
            self._render_record()
        if self._management is not None:
            self._management_controls()
        return True

    def _history_item_changed(self, current, _previous):
        if current is not None:
            value = current.data(Qt.ItemDataRole.UserRole)
            if self._management is not None and isinstance(value, str) and value.startswith('task:'):
                self.select_task(value[5:])
            else:
                self.select_request(value[8:] if isinstance(value,str) and value.startswith('request:') else value)

    def _history_selector_changed(self, _index):
        request_id = self.history_selector.currentData()
        if request_id is not None:
            if self._management is not None and request_id.startswith('task:'):
                self.select_task(request_id[5:])
            else:
                self.select_request(request_id[8:] if request_id.startswith('request:') else request_id)

    def _refresh_history_list(self):
        if self._management is not None:
            self._refresh_task_list()
            return
        self.history_list.blockSignals(True); self.history_selector.blockSignals(True)
        self.history_list.clear(); self.history_selector.clear()
        selected = -1
        for index, record in enumerate(self._history.summaries()):
            text = record['text'].replace('\n',' ')
            title = text[:11] + ('…' if len(text)>11 else '')
            status = _TASK_STATUS.get(record['status'], record['status'] or '状态未知')
            stamp = record['timestamp'][5:16].replace('T',' ') if record['timestamp'] else ''
            item = QListWidgetItem(title+'\n'+stamp+'\n'+status)
            item.setSizeHint(QSize(122,76)); item.setToolTip(text)
            item.setData(Qt.ItemDataRole.UserRole, record['request_id'])
            self.history_list.addItem(item)
            self.history_selector.addItem(text[:45], record['request_id'])
            if record['request_id'] == self.selected_request_id:
                selected = index
        self.history_list.setCurrentRow(selected)
        self.history_selector.setCurrentIndex(selected)
        self.history_list.blockSignals(False); self.history_selector.blockSignals(False)

    def set_management_state(self, state):
        """Consume controller authority; selecting a row never changes input target."""
        self._management = dict(state)
        self.recycle_button.show()
        self.return_to_pet.show()
        self.recycle_button.setText(('返回历史' if self._recycle else '回收箱')+'（'+str(len(state.get('trashed',[]))+len(state.get('trashed_chats',[])))+'）')
        self.compact_recycle_button.setText(self.recycle_button.text())
        self.compact_recycle_button.setVisible(self.width()<560)
        self._refresh_task_list()
        if self.selected_task_id:
            if not self.select_task(self.selected_task_id,user=False):
                self.selected_task_id = None
                self._clear_task_detail()
        elif self.selected_request_id:
            groups,_=self._history.task_groups(state.get('requests',[]))
            owner=next((task_id for task_id,records in groups.items()
                if any(row['request_id']==self.selected_request_id for row in records)),None)
            if owner:
                if not self.select_task(owner,user=False): self._clear_task_detail()
            elif self._selected_chat():
                self._render_chat(self._selected_chat())
                self._management_controls()
            elif 'chats' in state and (self._recycle or self.selected_request_id!=self.current_request_id):
                self._clear_task_detail()
            else:
                self._management_controls()
        if self.selected_task_id:
            job=next((row for row in state.get('cleanup',[]) if row.get('task_id')==self.selected_task_id),None)
            if job:self.set_cleanup_progress({**job,'status':job.get('stage')})
        elif self.selected_request_id:
            job=next((row for row in state.get('cleanup',[]) if row.get('request_id')==self.selected_request_id),None)
            if job:self.set_cleanup_progress({**job,'status':job.get('stage')})

    def set_cleanup_progress(self,payload):
        if payload.get('namespace')=='request' or payload.get('request_id'):
            if self.selected_task_id is not None or not self.selected_request_id or payload.get('request_id')!=self.selected_request_id:return
        elif not self.selected_task_id or payload.get('task_id')!=self.selected_task_id:return
        stage=payload.get('stage','requested')
        names={'requested':'已提交','queued':'已提交','manifest':'准备文件清单','graph':'清理检查点',
            'runtime':'清理运行上下文','memory':'清理关联聊天','history':'清理显示历史',
            'files':'清理私有文件','business':'清理任务内容','completed':'已完成',
            'blocked':'等待核对','paused':'已暂停，下次启动继续','error':'清理失败，可重试'}
        reason=payload.get('reason') or payload.get('error')
        self.set_management_notice('后台清理 · '+names.get(stage,stage)+((' · '+str(reason)) if reason else ''))
        active=stage not in ('completed','blocked','paused','error')
        self.purge_button.setEnabled(not active)
        self.purge_button.setText('正在清理…' if active else '重试清理')

    def _task_rows(self):
        return self._management.get('trashed' if self._recycle else 'tasks',[]) if self._management else []

    def _chat_rows(self):
        return self._management.get('trashed_chats' if self._recycle else 'chats',[]) if self._management else []

    def _selected_chat(self):
        return next((row for row in self._chat_rows() if row['request_id']==self.selected_request_id),None) if not self.selected_task_id else None

    def _selected_task(self):
        return next((row for row in self._task_rows() if (row.get('id') or row.get('task_id')) == self.selected_task_id), None)

    def _refresh_task_list(self):
        self.history_list.blockSignals(True); self.history_selector.blockSignals(True)
        self.history_list.clear(); self.history_selector.clear()
        _, legacy = self._history.task_groups(self._management.get('requests',[]))
        entries = []
        for row in self._task_rows():
            task_id = row.get('id') or row.get('task_id')
            if not task_id:
                continue
            name = _text(row.get('goal'),1600) or '任务目标未提供'
            status = _TASK_STATUS.get(row.get('status'), row.get('status','状态未知'))
            extra = row.get('blocked_reason') or row.get('quiescence') or row.get('lifecycle','')
            if isinstance(extra,dict): extra = extra.get('reason') or extra.get('status','')
            label = name[:16]+'\n#'+task_id[:6]+' · '+str(status)
            if self._recycle:
                life=row.get('lifecycle_state') or {}
                label += '\n'+str(row.get('trashed_at') or row.get('deleted_at') or life.get('updated_at') or '')[:16]+' '+str(extra)
            entries.append(('task:'+task_id,label,name))
        if 'chats' in self._management:
            for row in self._chat_rows():
                text=_text(row.get('text'),12000)
                label=('旧对话记录' if row.get('kind')=='legacy' else '普通聊天')+'\n'+text[:16]
                if self._recycle:label+='\n'+str(row.get('trashed_at') or '')[:16]
                entries.append(('request:'+row['request_id'],label,text))
        elif not self._recycle:
            for row in legacy:
                if self._history.get(row['request_id']) is not None:
                    entries.append(('request:'+row['request_id'],'未关联任务的对话\n'+row['text'][:16],row['text']))
        chosen = 'task:'+self.selected_task_id if self.selected_task_id else ('request:'+self.selected_request_id if self.selected_request_id else None)
        index = -1
        for i,(key,label,tip) in enumerate(entries):
            item=QListWidgetItem(label); item.setToolTip(tip); item.setData(Qt.ItemDataRole.UserRole,key)
            item.setSizeHint(QSize(122,78)); self.history_list.addItem(item)
            self.history_selector.addItem(label.replace('\n',' · '),key)
            if key == chosen: index=i
        self.history_list.setCurrentRow(index); self.history_selector.setCurrentIndex(index)
        self.history_list.blockSignals(False); self.history_selector.blockSignals(False)

    def select_task(self, task_id, *, user=True):
        previous = self.selected_task_id
        if previous != task_id: self._clear_confirmation()
        self.selected_task_id = task_id
        row = self._selected_task()
        if row is None:
            self.selected_task_id = previous
            return False
        self.selected_request_id = None
        if user: self._history_view_locked = True
        groups,_ = self._history.task_groups(self._management.get('requests',[]),task_id=task_id)
        records = sorted(groups.get(task_id,[]),key=lambda record:record.get('timestamp',''))
        self.events = [event for record in records for event in record.get('events',[])]
        self.last_event = self.events[-1] if self.events else None
        self.goal.setText(_text(row.get('goal'),1600)+' · #'+task_id[:6])
        self.goal.setToolTip(_text(row.get('goal'),1600))
        self._set_status(_TASK_STATUS.get(row.get('status'),row.get('status','状态未知')))
        self.agent.setText('任务 · #'+task_id[:6])
        self.history_hint.setText('回收箱 · 只读' if self._recycle else '任务记录')
        self.summary.setText(_text(row.get('summary') or row.get('blocked_reason'),900) or ('回收箱 · 详情只读' if self._recycle else '任务关联要求与实际记录'))
        html = []
        for record in records:
            html.append('<p><b>原话 · '+escape(record['request_id'][:12])+'</b><br>'+escape(record['text']).replace('\n','<br>')+'</p>')
        self._timeline_steps = project_timeline(self.events)
        html.append(self._timeline_html())
        self._set_timeline_html(''.join(html) or '<p>暂无已证实关联的消息或执行记录。</p>',
                                preserve_position=previous == task_id)
        question = row.get('question') or {}
        prompt = question.get('prompt','') if isinstance(question,dict) and question.get('status','open') == 'open' else ''
        self.question_card.setText('等待补充：'+_text(prompt,1600))
        self.question_card.setVisible(bool(prompt) and not self._recycle)
        self.answer_button.setText('回答此问题' if prompt else '补充此任务')
        self._management_controls()
        self._refresh_task_list()
        return True

    def _management_controls(self):
        row = self._selected_task()
        chat = self._selected_chat()
        selected = row or chat
        self.delete_button.setVisible(not self._recycle)
        self.delete_button.setEnabled(bool(selected) and not self._recycle)
        self.delete_button.setAccessibleName('删除选定对话' if chat else '删除选定任务')
        self.delete_button.setToolTip('将选定对话移入回收箱' if chat else '将选定任务及关联消息移入回收箱')
        self.restore_button.setVisible(self._recycle)
        self.undo_button.setVisible(bool(self._undo_task_id or self._undo_request_id) and not self._recycle)
        self.purge_button.setVisible(self._recycle)
        self.restore_button.setEnabled(bool(selected) and selected.get('lifecycle') != 'purge_pending')
        self.purge_button.setEnabled(bool(selected))
        self.purge_button.setText('重试清理' if selected and selected.get('lifecycle')=='purge_pending' else '彻底删除')
        if row and row.get('blocked_reason'):
            self.purge_button.setEnabled(False)
            self.purge_button.setToolTip(str(row['blocked_reason']))
        self.answer_button.setVisible(bool(row) and not row.get('legacy_only') and not self._recycle)
        for button in (self.continue_button,self.cancel_task_button,self.task_stop_button):
            button.setVisible(bool(row) and not row.get('legacy_only') and not self._recycle)
        permission_required=self._needs_maa_permission(row)
        self.maa_permission_button.setVisible(permission_required)
        self.maa_permission_button.setEnabled(permission_required)
        self.continue_button.setEnabled(not permission_required)
        self.continue_button.setToolTip('请先允许画面读取及本次 MAA 操作；普通继续不会改变权限。' if permission_required else '')
        self.stop_button.setVisible(not self._recycle)
        if self._management is not None:
            self.stop_button.setText('全局停止')

    def _needs_maa_permission(self, row):
        if (not isinstance(row,dict) or self._recycle
                or row.get('status') not in ('needs_input','paused')
                or row.get('lifecycle') in ('trashed','purge_pending','purged')):
            return False
        permissions=(self._management or {}).get('permissions')
        if (not isinstance(permissions,dict)
                or any(type(permissions.get(key)) is not bool for key in ('screen_capture','maa_input'))
                or all(permissions[key] for key in ('screen_capture','maa_input'))):
            return False
        reason='\n'.join(row[key] for key in ('blocked_reason','summary') if isinstance(row.get(key),str))
        return any(message in reason for message in (
            'MAA 输入权限未开启', '尚未允许读取 MAA 画面',
            '请在桌宠设置中开启画面读取', '请在桌宠设置中开启本次 MAA 界面操作授权'))

    def _request_maa_permission(self):
        # Selection and permission snapshots can change before a queued click.
        # Only the controller may grant permission and resume the exact task.
        if self.selected_task_id and self._needs_maa_permission(self._selected_task()):
            self.maa_permission_requested.emit(self.selected_task_id)

    def _clear_task_detail(self):
        self.selected_task_id = None
        self.selected_request_id = None
        self.events=[]; self.last_event=None
        self.agent.setText('没有选定任务'); self._set_status('空闲')
        self.goal.setText('回收箱为空' if self._recycle and not (self._task_rows() or self._chat_rows()) else '请选择历史记录')
        self.summary.setText('恢复仅恢复可见性，等待明确继续。' if self._recycle else '选择任务查看关联消息和执行记录。')
        self.timeline.clear(); self.question_card.hide(); self._management_controls()

    def _toggle_recycle(self):
        self._recycle = not self._recycle
        self.selected_task_id = None
        self.selected_request_id = None
        self._clear_confirmation()
        self.set_management_state(self._management)
        self._clear_task_detail()

    def _trash_selected(self):
        if self._selected_chat() is not None and not self._recycle:
            self.chat_trash_requested.emit(self.selected_request_id)
            return
        row = self._selected_task()
        if row is None or self._recycle: return
        if row.get('status') in ('running','observing'):
            self._ask_management('trash')
        else:
            self.trash_requested.emit(self.selected_task_id)

    def _purge_selected(self):
        if (self._selected_task() is not None or self._selected_chat() is not None) and self._recycle:
            self._ask_management('purge')

    def _restore_selected(self):
        if self._selected_chat() is not None:
            self.chat_restore_requested.emit(self.selected_request_id)
        elif self.selected_task_id:
            self.restore_requested.emit(self.selected_task_id)

    def _ask_management(self, action):
        chat=self._selected_chat()
        if chat is not None:
            self._confirmation=(action,self.selected_request_id,'request')
            self.confirm_label.setText('普通聊天 · '+_text(chat.get('text'),160)+'\n彻底删除此条对话的聊天与显示记录；保留长期记忆和其他历史。')
            self.confirm_button.setText('确认彻底删除')
            for widget in (self.confirm_label,self.confirm_button,self.cancel_confirm):widget.show()
            return
        row = self._selected_task()
        task_id = self.selected_task_id
        groups,_ = self._history.task_groups(self._management.get('requests',[]))
        count = len(groups.get(task_id,[]))
        self._confirmation=(action,task_id,'task')
        scope = '彻底删除任务专属消息、历史、结果与检查点；保留 Box、长期记忆、Key、皮肤与语音。' if action == 'purge' else '停止并移入回收箱；等待执行者退出和操作核对。'
        self.confirm_label.setText(f'{row.get("goal","")} · #{task_id[:6]} · {count} 条关联消息\n'+scope)
        self.confirm_button.setText('确认彻底删除' if action == 'purge' else '停止并移入回收箱')
        for widget in (self.confirm_label,self.confirm_button,self.cancel_confirm): widget.show()

    def _confirm_management(self):
        pending=self._confirmation
        self._clear_confirmation()
        if pending and pending[2]=='request':
            if self._selected_chat() is not None and pending[1]==self.selected_request_id:
                (self.chat_purge_requested if pending[0]=='purge' else self.chat_trash_requested).emit(pending[1])
        elif pending and pending[1] == self.selected_task_id:
            (self.purge_requested if pending[0]=='purge' else self.trash_requested).emit(pending[1])

    def _clear_confirmation(self):
        self._confirmation=None
        for widget in (self.confirm_label,self.confirm_button,self.cancel_confirm): widget.hide()

    def set_management_notice(self, text):
        self.history_hint.setText(_text(text,900))
        self.history_hint.setToolTip(_text(text,1600))

    def offer_restore(self,task_id):
        self._undo_request_id=None
        self._undo_task_id=task_id
        self.undo_button.setVisible(not self._recycle)

    def offer_chat_restore(self,request_id):
        self._undo_task_id=None
        self._undo_request_id=request_id
        self.undo_button.setVisible(not self._recycle)

    def _undo_trash(self):
        task_id=self._undo_task_id
        request_id=self._undo_request_id
        self.undo_button.hide(); self._undo_task_id=None;self._undo_request_id=None
        if task_id: self.restore_requested.emit(task_id)
        elif request_id:self.chat_restore_requested.emit(request_id)

    def _render_chat(self,row):
        request_id=row['request_id']
        record=self._history.load_request(request_id)
        if record is not None:
            self._render_record()
        else:
            self.events=[];self.last_event=None;self._timeline_steps=[]
            self.goal.setText(_text(row.get('text'),12000)[:64])
            self.goal.setToolTip(_text(row.get('text'),12000))
            self.summary.setText(_text(row.get('summary'),900) or '普通对话记录')
            self._set_status('已结束')
            self.timeline.clear()
        self.agent.setText('旧对话记录' if row.get('kind')=='legacy' else '普通聊天')
        self.history_hint.setText('回收箱 · 对话只读' if self._recycle else '普通聊天记录')
        self.question_card.hide()

    def _render_record(self):
        record = self._history.get(self.selected_request_id)
        if record is None:
            return
        self.goal.setText(record['text'][:64]+('…' if len(record['text'])>64 else ''))
        self.goal.setToolTip(record['text'])
        self.events = list(record['events'])
        self.last_event = dict(self.events[-1]) if self.events else None
        self._heading_event = None
        self.agent.setText('角色未标明')
        self.summary.setText('黍开始处理后，会在这里显示实际进展。')
        self._set_status(_TASK_STATUS.get(record['status'], '处理中' if record['status']=='running' else '状态未标明'))
        for event in self.events:
            self._apply_heading(event)
        if record['status'] != 'running':
            self._set_status(_TASK_STATUS.get(record['status'], '状态未标明'))
            if record['summary']:
                full = record['summary']
                preview = full[:64]+'…' if record['status'] in SUCCESS and len(full)>64 else full
                self.summary.setText(preview); self.summary.setToolTip(full)
        viewing_history = self.selected_request_id != self.current_request_id
        self.history_hint.setText('历史回看' if viewing_history else '当前要求')
        self.stop_button.setText('停止当前' if viewing_history else '停止')
        self.stop_button.setToolTip('停止当前正在处理的要求；历史记录只供回看。')
        self._render_timeline()

    def _set_status(self, value):
        self.status.setText(value)
        color, background = {
            '出错': ('#a65b56', '#f8ecea'),
            '等待处理': ('#977748', '#f8f3e9'),
            '已暂停': ('#977748', '#f8f3e9'),
            '处理中': ('#5177a0', '#ecf3fb'),
            '执行中': ('#5177a0', '#ecf3fb'),
            '已核实导入': ('#587a67', '#edf4ef'),
            '已结束': ('#587a67', '#edf4ef'),
        }.get(value, ('#79878c', '#f0f3f2'))
        self.status.setStyleSheet(
            f'color:{color};background:{background};border-radius:9px;padding:4px 8px;font-size:10px;')

    def set_task(self, task):
        self._live_task = task
        if self._management is not None:
            return
        if self.selected_request_id is not None:
            return
        if not isinstance(task, dict):
            self._task_id = None
            self.goal.setText('还没有进行中的任务')
            self.goal.setToolTip('')
            return
        task_id = task.get('id')
        if task_id != self._task_id:
            self.agent.setText('角色未标明')
            self._heading_event = None
        self._task_id = task_id
        goal = _text(task.get('goal'), 1600) or '任务目标未提供'
        self.goal.setText(goal[:64] + ('…' if len(goal) > 64 else ''))
        self.goal.setToolTip(goal)
        if not self._active and not self._busy and self._heading_event is None:
            self._set_status(_TASK_STATUS.get(task.get('status'), '状态未标明'))

    def set_active(self, active):
        self._active = bool(active)
        self.stop_button.setEnabled(self._active or self._busy)
        if self._management is None and self._active and self._heading_event is None:
            self._set_status('执行中')

    def set_busy(self, busy):
        self._busy = bool(busy)
        self.stop_button.setEnabled(self._active or self._busy)
        if self._management is None and self._busy and self._heading_event is None:
            self._set_status('处理中')

    def append_event(self, event):
        clean = _event_fields(event)
        if not clean:
            return False
        request_id = clean.get('request_id')
        if request_id is None and self.current_request_id is not None:
            return False
        if request_id is not None:
            if self._history.get(request_id) is None and clean.get('task_id'):
                self._history.load_bound(request_id,clean['task_id'])
            if self._history.get(request_id) is None:
                return False
            self._history_call(self._history.append, request_id, clean)
            self._refresh_history_list()
            if self._management is not None and self.selected_task_id:
                self.select_task(self.selected_task_id,user=False)
                return True
            if request_id != self.selected_request_id:
                return True
            record = self._history.get(request_id)
            self.events = list(record['events'])
        else:
            self.events = [*self.events,clean][-200:]
        self.last_event = dict(clean)
        if request_id is not None and record['status'] in TERMINAL_TASK_STATUSES:
            self._render_record()
            return True
        self._apply_heading(clean)
        self._render_timeline()
        return True

    def _apply_heading(self, clean):
        # General application notices are useful history, not evidence that
        # another agent started or that the current step/error was resolved.
        if (clean.get('stage') == 'notice' and clean.get('role') not in _ROLES
                and self._heading_event is not None
                and (self._heading_event.get('stage') != 'notice'
                     or self._heading_event.get('role') in _ROLES)):
            return
        self._heading_event = clean
        self.agent.setText(_ROLES.get(clean.get('role'), '角色未标明'))
        stage, status = clean.get('stage'), clean.get('status')
        if stage in ('error', 'intent_failed') or status in FAILURE:
            display = '出错'
        elif stage in ('intent_finished', 'task_waiting', 'task_paused', 'task_stopped',
                       'observation', 'observation_analysis'):
            display = {'intent_finished': '已理解要求', 'task_waiting': '等待处理',
                       'task_paused': '已暂停', 'task_stopped': '已停止',
                       'observation': '已获取画面', 'observation_analysis': '已返回分析'}[stage]
        elif stage == 'task_finished':
            display = _TASK_STATUS.get(status, '已结束（状态未标明）')
        elif stage == 'dispatch_completed':
            display = '已返回结果'
        elif stage == 'dispatch_reused':
            display = '复用已有结果'
        elif stage == 'tool_finished':
            display = '已返回结果' if status == 'completed' else '等待核实'
        elif stage in ('dispatch_started', 'tool_started', 'task_started', 'intent_started'):
            display = '处理中'
        else:
            display = _TASK_STATUS.get(status, '状态通知')
        self._set_status(display)
        summary = clean.get('summary') or _STAGES.get(stage, '收到执行状态')
        preview = summary[:64]+'…' if stage == 'task_finished' and status in SUCCESS and len(summary)>64 else summary
        self.summary.setText(preview)
        self.summary.setToolTip(summary)

    def _render_timeline(self, *, follow_new=True):
        record = self._history.get(self.selected_request_id)
        self._timeline_steps = project_timeline(self.events, record.get('status') if record else None)
        self._set_timeline_html(self._timeline_html(), follow_new=follow_new)

    def _set_timeline_html(self, html, *, preserve_position=True, follow_new=True):
        """Keep a reader's viewport across document replacement and Qt relayout."""
        self._render_generation += 1
        generation = self._render_generation
        bar = self.timeline.verticalScrollBar()
        previous = bar.value() if preserve_position else 0
        follow = preserve_position and follow_new and bar.value() >= bar.maximum() - 2
        selection = self.timeline.textCursor()
        selected = ((selection.anchor(), selection.position())
                    if preserve_position and selection.hasSelection() else None)
        self.timeline.setHtml(html)
        if selected:
            cursor = self.timeline.textCursor()
            maximum = max(0, self.timeline.document().characterCount() - 1)
            cursor.setPosition(min(selected[0], maximum))
            cursor.setPosition(min(selected[1], maximum), QTextCursor.MoveMode.KeepAnchor)
            self.timeline.setTextCursor(cursor)
        def restore():
            if generation == self._render_generation:
                bar.setValue(bar.maximum() if follow else min(previous, bar.maximum()))
        restore()
        QTimer.singleShot(0, self, restore)

    def _timeline_html(self):
        """Qt rich text keeps the existing selection, links and scroll surface."""
        expanded = self._timeline_expanded.setdefault(self.selected_request_id, {})
        rows = []
        for index, step in enumerate(self._timeline_steps):
            opened = expanded.get(step.key, not step.completed)
            failed = step.status in FAILURE
            color = '#aa655f' if failed else '#62816d' if step.completed else '#527ba1'
            label = {'running':'处理中', 'completed':'已返回结果', 'understood':'已理解要求',
                'reused':'复用结果', 'research_verified':'资料已核实', 'import_verified':'导入已核实',
                'planned_verified':'计划交付已核实', 'catalog_verified':'活动目录已核实',
                'needs_input':'等待处理', 'paused':'已暂停', 'cancelled':'已停止',
                'failed':'出错', 'error':'出错', 'rejected':'已拒绝', 'researched':'资料已返回',
                'partial':'部分返回', 'returned':'已返回结果'}.get(step.status,'状态记录')
            count = sum(bool(item.tool) for item in step.items)
            meta = (' · '+str(count)+' 项工具') if count else ''
            role = _ROLES.get(step.role, '')
            if role and role != step.title:
                label = role+' · '+label
            href = 'shu-step://toggle/'+quote(step.key, safe='')
            tooltip = step.title+'\n'+('收起' if opened else '展开')+'实际工具记录'+(('\n'+step.summary) if step.summary else '')
            title = step.title[:24]+('…' if len(step.title)>24 else '')
            rows.append('<tr><td colspan="2" width="20" align="center" valign="top">'
                f'<span style="color:{color};font-size:12px;">●</span></td><td>'
                '<p style="margin:0 0 5px 0;">'
                f'<a href="{escape(href,quote=True)}" title="'+escape(tooltip,quote=True)+'" style="color:#35494e;text-decoration:none;">'
                '<b>'+escape(title)+'</b>  '+('−' if opened else '+')+'</a>'
                f' <span style="color:{color};font-size:10px;">'+escape(label+meta)+'</span></p></td></tr>')
            connector = 'border-right:1px solid #dce5df;' if index < len(self._timeline_steps)-1 else ''
            rows.append('<tr><td width="10" style="'+connector+'">'
                '</td><td width="10"></td><td>')
            if step.summary:
                preview = step.summary[:26]+'…' if not opened and len(step.summary)>26 else step.summary
                rows.append('<p title="'+escape(step.summary,quote=True)+'" style="margin:0 0 7px 0;color:#64767c;font-size:11px;">'
                    +escape(preview).replace('\n','<br>')+'</p>')
            if opened:
                for item in step.items:
                    rows.append(self._timeline_item_html(item, step.summary))
            else:
                for error in step.failures:
                    rows.append('<p style="margin:0 0 7px 0;color:#aa655f;font-size:11px;">'
                        +escape(error).replace('\n','<br>')+'</p>')
            rows.append('<p style="margin:0;font-size:5px;">&nbsp;</p></td></tr>')
        return '<table width="100%" cellspacing="0" cellpadding="0">'+''.join(rows)+'</table>' if rows else ''

    def _timeline_item_html(self, item, parent_summary):
        failed = item.status in FAILURE
        color = '#aa655f' if failed else '#7d8d92'
        tool = _TOOLS.get(item.tool, TOOL_NAMES.get(item.tool, item.tool))
        title = tool or _STAGES.get(item.stage, '状态通知')
        state = {'running':'进行中', 'completed':'已返回', 'failed':'失败', 'error':'失败',
            'partial':'部分返回', 'cancelled':'已停止', 'needs_input':'等待处理', 'rejected':'已拒绝',
            'returned':'已返回，状态待核实'}.get(item.status,'')
        facts = [_time_label(item.timestamp), title]
        if state:
            facts.append(state)
        if item.elapsed_seconds is not None:
            facts.append(f'{item.elapsed_seconds:g} 秒')
        if item.cached:
            facts.append('来自缓存')
        if item.task_id: facts.append('任务 '+item.task_id[:12])
        if item.item_count is not None:
            facts.append('已取得 '+str(item.item_count)+((' / '+str(item.total)) if item.total is not None else '')+' 条')
        if item.group_label: facts.append(item.group_label)
        if item.stage_code: facts.append(item.stage_code)
        if item.step_id: facts.append('步骤 '+item.step_id[:12])
        if item.primary_count is not None: facts.append('主选 '+str(item.primary_count))
        if item.backup_count is not None: facts.append('备选 '+str(item.backup_count))
        if item.plan_revision is not None: facts.append('计划版本 '+str(item.plan_revision))
        if item.stop_reason: facts.append('停止原因 '+item.stop_reason)
        html = '<p style="margin:2px 0 11px 0;">'+f'<span style="color:{color};font-size:10px;">'+escape(' · '.join(facts))+'</span>'
        if item.summary and item.summary != parent_summary:
            html += '<br><span style="color:'+('#aa655f' if failed else '#52666e')+';font-size:11px;">'+escape(item.summary).replace('\n','<br>')+'</span>'
        if item.query and item.query not in (item.summary,parent_summary):
            html += '<br><span style="color:#6e8087;font-size:11px;">'+escape(item.query).replace('\n','<br>')+'</span>'
        safe = _source_url(item.url)
        if safe:
            address = urlsplit(safe)
            source = address.netloc + address.path
            source = source[:64]+('…' if len(source)>64 else '')
            html += '<br><a href="'+escape(safe,quote=True)+'" title="'+escape(safe,quote=True)+'" style="font-size:11px;">'+escape(source)+'</a>'
        return html+'</p>'

    def set_notice(self, text):
        summary = _text(text)
        if (self.events and self.events[-1].get('stage') == 'notice'
                and self.events[-1].get('summary') == summary):
            return False
        return self.append_event({'stage': 'notice', 'summary': summary})

    def set_error(self, text):
        return self.append_event({'stage': 'error', 'status': 'failed', 'summary': _text(text)})

    def _open_source(self, url):
        if url.scheme() == 'shu-step':
            key = url.path().lstrip('/')
            if url.host() == 'toggle':
                step = next((step for step in self._timeline_steps if step.key == key), None)
                if step is not None:
                    expanded = self._timeline_expanded.setdefault(self.selected_request_id, {})
                    expanded[key] = not expanded.get(key, not step.completed)
                    if self.selected_task_id:
                        self.select_task(self.selected_task_id,user=False)
                    else:
                        self._render_timeline(follow_new=False)
            return
        safe = _source_url(url.toString())
        if safe:
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(safe))

    def _available_geometry(self, point):
        screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else QRect(point.x()-430, point.y()-500, 860, 1000)

    def configure_geometry(self, path):
        self._geometry_path = Path(path)
        try:
            state = json.loads(self._geometry_path.read_text(encoding='utf-8'))
            rect = state.get('rect')
            if not isinstance(rect,list) or len(rect)!=4 or any(type(x) is not int for x in rect): return
            if rect[2]<=0 or rect[3]<=0: return
            self.setGeometry(QRect(*rect)); self._placed=True
            self._clamp_geometry()
        except (OSError, ValueError, TypeError):
            pass

    def _save_geometry(self):
        if not self._placed or self._geometry_path is None: return
        screen = QGuiApplication.screenAt(self.geometry().center())
        from .activity_history import _atomic_json
        try:
            _atomic_json(self._geometry_path, {'version':1,'rect':[self.x(),self.y(),self.width(),self.height()],
                'screen':screen.name() if screen else '', 'logical_dpi':screen.logicalDotsPerInch() if screen else 96})
        except OSError:
            self.set_management_notice('窗口位置暂未保存')

    def _clamp_geometry(self):
        rect=self.geometry()
        screens=QGuiApplication.screens()
        screen=next((s for s in screens if s.availableGeometry().contains(rect.center())),None)
        screen=screen or QGuiApplication.primaryScreen()
        if screen is None: return
        area=screen.availableGeometry()
        width,height=min(rect.width(),area.width()),min(rect.height(),area.height())
        self.setMinimumSize(min(300,width),min(270,height))
        self.resize(width,height)
        self.move(max(area.left(),min(rect.x(),area.right()-width+1)),max(area.top(),min(rect.y(),area.bottom()-height+1)))

    def _reset_anchor(self):
        self._placed=False
        self.show_at(self._last_anchor)

    def eventFilter(self, watched, event):
        kind=event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button()==Qt.MouseButton.LeftButton:
            self._drag_origin=(event.globalPosition().toPoint(),self.pos())
            self._native_drag=False
        elif kind == QEvent.Type.MouseMove and self._drag_origin and event.buttons() & Qt.MouseButton.LeftButton:
            origin,position=self._drag_origin
            delta=event.globalPosition().toPoint()-origin
            if delta.manhattanLength() >= QApplication.startDragDistance():
                handle=self.windowHandle()
                if not self._native_drag and handle is not None:
                    self._native_drag=bool(handle.startSystemMove())
                if not self._native_drag:
                    self.move(position+delta)
                self._placed=True
                return True
        elif kind == QEvent.Type.MouseButtonRelease and self._drag_origin:
            self._drag_origin=None
            self._clamp_geometry(); self._save_geometry()
        return super().eventFilter(watched,event)

    def show_at(self, anchor):
        """Explicit user opening near a pet rectangle or a global point."""
        if isinstance(anchor, QPoint):
            anchor = QRect(anchor, anchor)
        if not isinstance(anchor, QRect):
            raise TypeError('anchor must be a global QPoint or QRect')
        self._last_anchor=QRect(anchor)
        available = self._available_geometry(anchor.center())
        if not self._placed:
            width, height = min(620, available.width()), min(520, available.height())
            self.setMinimumSize(min(300, width), min(270, height))
            self.resize(width, height)
            x = anchor.left() - width - 10
            if x < available.left(): x = anchor.right() + 10
            x = max(available.left(), min(x, available.right() - width + 1))
            y = max(available.top(), min(anchor.bottom() - height + 20, available.bottom() - height + 1))
            self.move(x,y)
            self._placed=True
        else:
            self._clamp_geometry()
        self._motion.stop()
        self._closing = False
        if not self.isVisible():
            self.setWindowOpacity(0)
        self.show()
        self.raise_()
        self.activateWindow()
        self._motion.setDuration(140)
        self._motion.setStartValue(self.windowOpacity())
        self._motion.setEndValue(1.0)
        self._motion.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'history_sidebar'):
            compact = self.width() < 560
            self.history_sidebar.setVisible(not compact)
            self.history_selector.setVisible(compact)
            if hasattr(self,'compact_recycle_button'):
                self.compact_recycle_button.setVisible(compact and self._management is not None)

    def hide_animated(self):
        if not self.isVisible() or self._closing:
            return
        self._motion.stop()
        self._closing = True
        self._motion.setDuration(100)
        self._motion.setStartValue(self.windowOpacity())
        self._motion.setEndValue(0.0)
        self._motion.start()

    def _motion_finished(self):
        if self._closing:
            self.hide()
            self._closing = False
            self.setWindowOpacity(1)

    def hideEvent(self, event):
        self._motion.stop()
        self._save_geometry()
        super().hideEvent(event)

    def closeEvent(self, event):
        if self._shutting_down:
            event.accept()
            return
        event.ignore()
        self.hide_animated()

    def shutdown(self):
        """Final app exit accepts close immediately instead of deferring quit."""
        self._shutting_down = True
        self._motion.stop()
        self.close()


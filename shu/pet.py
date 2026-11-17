"""Pet-first shell; original atlas frames are cropped once, never recompressed."""
from __future__ import annotations
import math
from pathlib import Path
import time
from PySide6.QtCore import Qt, QTimer, QPoint, QRect, QRectF, Signal, QPropertyAnimation, QEasingCurve, QEvent
from PySide6.QtGui import QActionGroup, QColor, QCursor, QGuiApplication, QPainter, QPen, QPixmap, QRegion, QTextCursor, QTextDocument
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLayout, QMenu, QPushButton, QTextEdit, QToolButton, QVBoxLayout, QWidget
from .pet_core import Animation, ATLAS_SIZE, CELL_HEIGHT, CELL_WIDTH, LABELS, STATES, clamp_position, look_cell
from .idle_vignette import IdleVignetteClock
from .skins import (LEGACY_SKIN, TRANSITION_OUT_FRAMES, TRANSITION_IN_FRAMES,
                    Skin, SkinCatalog, SkinChange, load_skin_preference, save_skin_preference)

PANEL_STYLE = '''
QWidget {color:#333b40;font-family:"Microsoft YaHei UI","Microsoft YaHei","Segoe UI";font-size:12px;}
QFrame#PetPanel {background:#fdfdfb;border:1px solid #e0e5e4;border-radius:21px;}
QTextEdit {background:transparent;border:0;padding:0;selection-background-color:#d8e7fd;color:#333b40;}
QToolButton {background:transparent;border:0;border-radius:14px;padding:3px;color:#727b80;}
QToolButton:hover {background:#eef1f0;}
QPushButton {background:#f2f4f3;border:1px solid #e3e7e5;border-radius:10px;padding:6px 11px;color:#535f64;}
QPushButton:hover {background:#e9efeb;}
QLabel#PetMuted {color:#a0a8ac;font-size:10px;}
QScrollBar:vertical {background:transparent;width:5px;margin:2px;}
QScrollBar::handle:vertical {background:#d5dcda;min-height:20px;border-radius:2px;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}
'''

def _bounded(point, size, screen=None):
    screen=screen or QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
    if screen is None:return point
    a=screen.availableGeometry()
    return QPoint(*clamp_position(point.x(),point.y(),size.width(),size.height(),(a.x(),a.y(),a.width(),a.height())))

class PetCanvas(QWidget):
    activated=Signal()
    frame_changed=Signal()
    skin_changed=Signal(str)
    skin_switching_changed=Signal(bool)
    skin_error=Signal(str)
    def __init__(self,atlas_path:Path,height=240,parent=None,*,native_pixels=False):
        super().__init__(parent)
        self.skin_id=LEGACY_SKIN;self._skin=None;self._skin_switch=None;self._switch_frame=None;self._queued_state=None
        self._vignette_frames={};self._vignette_clock=None;self._vignette_frame=None;self._vignette_blocked=False
        self.atlas=QPixmap(str(atlas_path));self.frames={}
        if not self.atlas.isNull() and (self.atlas.width(),self.atlas.height())==ATLAS_SIZE:
            for row in range(11):
                count=len(list(STATES.values())[row][1]) if row<9 else 8
                for col in range(count):self.frames[row,col]=self.atlas.copy(col*CELL_WIDTH,row*CELL_HEIGHT,CELL_WIDTH,CELL_HEIGHT)
        self.native_pixels=native_pixels;self.animation=Animation(time.monotonic());self.current_cell=self.animation.cell
        self.follow_cursor=False;self.animated=True;self.dragging=False
        self._last_cursor=QCursor.pos();self._cursor_moved_at=-100.
        ratio=self.devicePixelRatioF() if native_pixels else 1.
        h=CELL_HEIGHT/ratio if native_pixels else height;w=h*CELL_WIDTH/CELL_HEIGHT
        self.draw_rect=QRectF(0,0,w,h);self.setFixedSize(math.ceil(w),math.ceil(h))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.timer=QTimer(self);self.timer.timeout.connect(self._tick);self.timer.start(33)
    def set_follow_cursor(self,enabled):self.notify_interaction();self.follow_cursor=bool(enabled);self._tick()
    def set_animated(self,enabled):self.notify_interaction();self.animated=bool(enabled);self._tick()
    @property
    def skin_switching(self):return self._skin_switch is not None
    @property
    def idle_vignette_playing(self):return self._vignette_frame is not None
    @property
    def idle_vignette_phase(self):return self._vignette_clock.phase if self._vignette_clock else 'idle'
    def notify_interaction(self,now=None):
        if self._vignette_clock is not None:self._vignette_clock.interact(time.monotonic() if now is None else now)
        if self._vignette_frame is not None:
            self._vignette_frame=None;self.frame_changed.emit();self.update()
    def set_idle_vignette_blocked(self,blocked):
        blocked=bool(blocked)
        if blocked!=self._vignette_blocked:self.notify_interaction();self._vignette_blocked=blocked
    def displayed_frame(self):
        if self._switch_frame is not None:return self._switch_frame
        return self._vignette_frame if self._vignette_frame is not None else self.frames.get(self.current_cell)
    @staticmethod
    def _load_skin(skin):
        atlas=QPixmap(str(skin.atlas))
        if atlas.isNull() or (atlas.width(),atlas.height())!=ATLAS_SIZE:
            raise ValueError('皮肤图集尺寸或文件无效')
        frames={}
        for row in range(11):
            count=len(list(STATES.values())[row][1]) if row<9 else 8
            for column in range(8):
                frame=atlas.copy(column*CELL_WIDTH,row*CELL_HEIGHT,CELL_WIDTH,CELL_HEIGHT)
                region=QRegion(frame.mask())
                if column<count:
                    if region.isEmpty() or not frame.hasAlphaChannel() or QRegion(QRect(0,0,CELL_WIDTH,CELL_HEIGHT)).subtracted(region).isEmpty():
                        raise ValueError('皮肤图集有空帧或缺少透明背景')
                    frames[row,column]=frame
                elif not region.isEmpty():raise ValueError('皮肤图集的空白格含有图像')
        return atlas,frames
    @staticmethod
    def _load_transition(path,count):
        strip=QPixmap(str(path))
        if strip.isNull() or (strip.width(),strip.height())!=(CELL_WIDTH*count,CELL_HEIGHT):
            raise ValueError('换肤动画尺寸无效')
        frames=[strip.copy(index*CELL_WIDTH,0,CELL_WIDTH,CELL_HEIGHT) for index in range(count)]
        if any(QRegion(frame.mask()).isEmpty() or not frame.hasAlphaChannel() or
               QRegion(QRect(0,0,CELL_WIDTH,CELL_HEIGHT)).subtracted(QRegion(frame.mask())).isEmpty() for frame in frames):
            raise ValueError('换肤动画有空帧或缺少透明背景')
        return frames
    @staticmethod
    def _load_vignette(skin):
        if skin.idle_vignette is None:return {}
        result={}
        clips=[('enter',skin.idle_vignette.enter),('exit',skin.idle_vignette.exit)]
        if skin.idle_vignette.hold is not None:clips.append(('hold',skin.idle_vignette.hold))
        for name,clip in clips:
            sheet=QPixmap(str(clip.path));rows=(clip.frames+clip.columns-1)//clip.columns
            if sheet.isNull() or (sheet.width(),sheet.height())!=(clip.columns*CELL_WIDTH,rows*CELL_HEIGHT):
                raise ValueError('猫咪彩蛋图集尺寸无效')
            frames=[]
            for index in range(rows*clip.columns):
                frame=sheet.copy((index%clip.columns)*CELL_WIDTH,(index//clip.columns)*CELL_HEIGHT,CELL_WIDTH,CELL_HEIGHT)
                region=QRegion(frame.mask())
                if index>=clip.frames:
                    if not region.isEmpty():raise ValueError('猫咪彩蛋的空白格含有图像')
                else:
                    if (region.isEmpty() or not frame.hasAlphaChannel() or
                            QRegion(QRect(0,0,CELL_WIDTH,CELL_HEIGHT)).subtracted(region).isEmpty()):
                        raise ValueError('猫咪彩蛋有空帧或缺少透明背景')
                    frames.append(frame)
            result[name]=frames
        return result
    def _commit_skin(self,skin,atlas,frames,vignette_frames):
        self._skin=skin;self.skin_id=skin.id;self.atlas=atlas;self.frames=frames
        self._vignette_frames=vignette_frames;self._vignette_frame=None;self._vignette_clock=None
        if vignette_frames:
            spec=skin.idle_vignette
            self._vignette_clock=IdleVignetteClock(now=time.monotonic(),idle_seconds=spec.idle_seconds,
                enter_frames=spec.enter.frames,exit_frames=spec.exit.frames,
                enter_frame_ms=spec.enter.frame_ms,exit_frame_ms=spec.exit.frame_ms,hold_ms=spec.hold_ms,
                hold_frames=spec.hold.frames if spec.hold is not None else 0,
                hold_frame_ms=spec.hold.frame_ms if spec.hold is not None else 125)
        state=self._queued_state or self.animation.state;self._queued_state=None
        self.animation=Animation(time.monotonic())
        if state in STATES:self.animation.play(state,time.monotonic(),loop=state in ('idle','running','waiting','running-left','running-right'))
        self.current_cell=self.animation.cell;self.skin_changed.emit(skin.id);self.frame_changed.emit();self.update()
    def set_skin(self,skin:Skin,*,animate=True):
        self.notify_interaction()
        if self.skin_switching or skin==self._skin:return False
        try:
            atlas,frames=self._load_skin(skin)
            transition=[]
            if animate and self._skin is not None and self._skin.transition_out and skin.transition_in:
                transition=(self._load_transition(self._skin.transition_out,TRANSITION_OUT_FRAMES)+
                            self._load_transition(skin.transition_in,TRANSITION_IN_FRAMES))
        except (OSError,ValueError):
            self.skin_error.emit('这套皮肤的素材未准备完整，已保留当前形象。');return False
        try:vignette_frames=self._load_vignette(skin)
        except (OSError,ValueError):
            vignette_frames={};self.skin_error.emit('猫咪彩蛋素材未准备完整，已使用日常形象。')
        if not transition:
            self._commit_skin(skin,atlas,frames,vignette_frames);return True
        self._skin_switch={'clock':SkinChange(time.monotonic()),'skin':skin,'atlas':atlas,'frames':frames,'transition':transition,'vignette_frames':vignette_frames}
        self._switch_frame=transition[0];self.skin_switching_changed.emit(True);self.frame_changed.emit();self.update()
        return True
    def _tick(self):
        now=time.monotonic()
        if self._skin_switch is not None:
            switch=self._skin_switch;switch['clock'].advance(now)
            if not switch['clock'].done:
                frame=switch['transition'][switch['clock'].frame]
                if frame is not self._switch_frame:self._switch_frame=frame;self.frame_changed.emit()
                self.update();return
            self._skin_switch=None;self._switch_frame=None
            self._commit_skin(switch['skin'],switch['atlas'],switch['frames'],switch['vignette_frames'])
            self.skin_switching_changed.emit(False)
        if self.animated:self.animation.advance(now)
        cell=self.animation.cell if self.animated else (0,0);cursor=QCursor.pos()
        if cursor!=self._last_cursor:
            self._last_cursor=cursor;self._cursor_moved_at=now
            if self.rect().adjusted(-12,-12,12,12).contains(self.mapFromGlobal(cursor)):self.notify_interaction(now)
        previous_vignette=self._vignette_frame
        if self._vignette_clock is not None:
            ref=self._vignette_clock.advance(now,eligible=self.animated and not self.dragging and not self._vignette_blocked and self.animation.state=='idle')
            self._vignette_frame=self._vignette_frames[ref[0]][ref[1]] if ref else None
        if self.animated and not self.idle_vignette_playing and self.follow_cursor and not self.dragging and self.animation.state=='idle' and now-self._cursor_moved_at<1.8:
            head=self.mapToGlobal(QPoint(self.width()//2,round(self.height()*68/CELL_HEIGHT)))
            dx,dy=cursor.x()-head.x(),cursor.y()-head.y()
            if dx*dx+dy*dy>24*24:cell=look_cell(dx,dy)
        if cell!=self.current_cell:self.current_cell=cell;self.frame_changed.emit()
        elif previous_vignette is not self._vignette_frame:self.frame_changed.emit()
        self.update()
    def play(self,state):
        if state not in STATES:return
        self.notify_interaction()
        if self.skin_switching:self._queued_state=state;return
        self.animated=True
        looping=state in ('idle','running','waiting','running-left','running-right')
        if state==self.animation.state and looping and self.animation.loop:return
        self.animation.play(state,time.monotonic(),loop=looping)
        self.current_cell=self.animation.cell;self.frame_changed.emit();self.update()
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,not self.native_pixels)
        frame=self.displayed_frame()
        if frame is None:p.setPen(Qt.GlobalColor.white);p.drawText(self.rect(),Qt.AlignmentFlag.AlignCenter,'黍');return
        # Default desktop size is 192x208 physical pixels, including a 125% screen.
        p.drawPixmap(self.draw_rect,frame,QRectF(0,0,CELL_WIDTH,CELL_HEIGHT))
    def mouseReleaseEvent(self,event):
        self.notify_interaction()
        if event.button()==Qt.MouseButton.LeftButton:self.activated.emit()

class PetToolButton(QToolButton):
    def __init__(self,kind,tooltip,parent=None):
        super().__init__(parent);self.kind=kind;self.level=0.;self.recording=False;self.setFixedSize(35,34)
        self.setToolTip(tooltip);self.setAccessibleName(tooltip);self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet('QToolButton{border:0;border-radius:12px;background:transparent;}QToolButton:hover{background:#e4e8e2;}')
    def paintEvent(self,event):
        super().paintEvent(event);p=QPainter(self);p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor('#414a43'),1.6,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
        if self.kind=='write':
            p.drawRoundedRect(QRectF(9,10,15,15),4,4);p.fillRect(QRectF(18,8,9,9),QColor('#f7f8f5'))
            p.drawLine(17,18,26,9);p.drawLine(17,18,16,22);p.drawLine(16,22,20,21)
        elif self.kind=='voice':
            if self.recording:
                p.setBrush(QColor('#202624'));p.setPen(Qt.PenStyle.NoPen);p.drawEllipse(QRectF(10,10,14,14))
            else:
                for x,a,b in ((9,15,21),(13,10,25),(17,7,28),(21,12,23),(25,16,20)):p.drawLine(x,a,x,b)
        elif self.kind=='send':
            p.setPen(QPen(QColor('#4b75aa'),1.7,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
            p.drawLine(17,24,17,10);p.drawLine(17,10,11,16);p.drawLine(17,10,23,16)
        elif self.kind=='plus':
            p.drawLine(11,17,23,17);p.drawLine(17,11,17,23)
        else:
            p.setBrush(QColor('#414a43'))
            for x in (10,17,24):p.drawEllipse(QRectF(x-1.3,16,2.6,2.6))

class PetComposer(QTextEdit):
    submitted=Signal()
    dismissed=Signal()
    def keyPressEvent(self,event):
        if event.key()==Qt.Key.Key_Escape:self.dismissed.emit();return
        if event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter) and not event.modifiers()&Qt.KeyboardModifier.ShiftModifier:
            self.submitted.emit();return
        super().keyPressEvent(event)

class PetConversation(QWidget):
    submitted=Signal(str)
    stop_requested=Signal()
    settings_requested=Signal()
    collapse_requested=Signal()
    collapsed=Signal()
    voice_toggle_requested=Signal()
    def __init__(self,parent=None):
        super().__init__(parent,Qt.WindowType.Tool|Qt.WindowType.FramelessWindowHint|Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle('和黍说话');self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(PANEL_STYLE);self.expanded_size=(370,96);self.resize(*self.expanded_size)
        self._closing=False;self._recording=False;self._transition_pixmap=None
        self.motion=QPropertyAnimation(self,b'geometry',self);self.motion.setDuration(200);self.motion.setEasingCurve(QEasingCurve.Type.OutCubic);self.motion.finished.connect(self._motion_finished)
        outer=QVBoxLayout(self);outer.setContentsMargins(2,2,2,2);outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        panel=QFrame();self.panel=panel;panel.setObjectName('PetPanel');outer.addWidget(panel);l=QVBoxLayout(panel);l.setContentsMargins(13,9,11,8);l.setSpacing(1)
        self.input=PetComposer();self.input.setFixedHeight(35);self.input.setPlaceholderText('和黍说点什么…')
        self.input.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.input.submitted.connect(self.send);self.input.dismissed.connect(self.collapse_requested.emit);l.addWidget(self.input)
        footer=QHBoxLayout();footer.setContentsMargins(0,0,0,0);footer.setSpacing(2)
        self.more_button=PetToolButton('plus','更多与对话记录',self);self.more_button.setFixedSize(28,28)
        self.more_menu=QMenu(self);self.more_menu.setStyleSheet('QMenu{background:#fdfdfb;color:#38423f;border:1px solid #e0e6e1;padding:5px;}QMenu::item{padding:8px 18px;}QMenu::item:selected{background:#edf2ee;}')
        self.more_menu.addAction('对话记录',self.show_history);self.more_menu.addAction('模型与 Key 设置',self.settings_requested.emit)
        self.more_menu.addSeparator();self.more_menu.addAction('停止本地操作',self.stop_requested.emit)
        self.more_button.clicked.connect(lambda:self.more_menu.popup(self.more_button.mapToGlobal(QPoint(0,29))))
        voice=PetToolButton('voice','语音输入',self);self.voice_button=voice;voice.setFixedSize(28,28);voice.clicked.connect(self.begin_dictation)
        self.stop_button=QPushButton('停止');self.stop_button.setToolTip('立即停止本地任务，不等待模型回复');self.stop_button.clicked.connect(self.stop_requested.emit);self.stop_button.hide()
        footer.addWidget(self.more_button);footer.addWidget(voice);footer.addWidget(self.stop_button)
        self.voice_status=QLabel('Enter 发送');self.voice_status.setObjectName('PetMuted');self.voice_status.setMaximumWidth(210)
        footer.addStretch();footer.addWidget(self.voice_status)
        self.send_button=PetToolButton('send','发送',self);self.send_button.setFixedSize(32,32)
        self.send_button.setStyleSheet('QToolButton{background:#bed7fc;border:0;border-radius:16px;}QToolButton:hover{background:#a8c9f7;}QToolButton:disabled{background:#e4e9ed;}')
        self.send_button.clicked.connect(self.send);footer.addWidget(self.send_button);l.addLayout(footer)
        self.history=QDialog(parent,Qt.WindowType.Tool);self.history.setWindowTitle('黍 · 对话记录');self.history.resize(440,480)
        self.history.setStyleSheet(PANEL_STYLE+'QDialog{background:#fbfcfa;}')
        history_layout=QVBoxLayout(self.history);history_layout.setContentsMargins(22,20,22,18);history_layout.setSpacing(14)
        title=QLabel('和黍的对话');title.setStyleSheet('font-size:17px;font-weight:600;color:#35433d;');history_layout.addWidget(title)
        self.history_filter=QComboBox();self.history_filter.addItem('全部对话',None)
        self.history_filter.setToolTip('按任务或独立对话选择本地记录')
        history_layout.addWidget(self.history_filter)
        self._history_loader=None;self._history_records=[]
        self.history_filter.currentIndexChanged.connect(self._render_history)
        self.transcript=QTextEdit();self.transcript.setReadOnly(True);self.transcript.setPlaceholderText('还没有对话。');history_layout.addWidget(self.transcript,1)
        close=QPushButton('收起记录');close.clicked.connect(self.history.hide);history_layout.addWidget(close,0,Qt.AlignmentFlag.AlignRight)
        self.deactivate_timer=QTimer(self);self.deactivate_timer.setSingleShot(True);self.deactivate_timer.setInterval(100);self.deactivate_timer.timeout.connect(self._collapse_if_unused)
        self.more_menu.aboutToHide.connect(self.deactivate_timer.start)
    def expand_from(self,start,end):
        self._closing=False;self.motion.stop();self.deactivate_timer.stop();self._transition_pixmap=None
        self.panel.show();self.setGeometry(end);self.layout().activate();self.show()
        self._transition_pixmap=self.grab();self.panel.hide()
        self.setGeometry(start);self.show();self.raise_();self.activateWindow()
        self.motion.setStartValue(start);self.motion.setEndValue(end);self.motion.start()
    def collapse_to(self,end):
        if not self.isVisible() or self._closing:return
        self.motion.stop();self.deactivate_timer.stop()
        if self._transition_pixmap is None:self._transition_pixmap=self.grab()
        self.panel.hide();self._closing=True
        self.motion.setStartValue(self.geometry());self.motion.setEndValue(end);self.motion.start()
    def _motion_finished(self):
        self._transition_pixmap=None;self.panel.show()
        if self._closing:self.hide();self.collapsed.emit()
        else:self.input.setFocus()
        self.update()
    def reposition(self,geometry):
        # Dragging can interrupt the snapshot transition before finished fires.
        # Settle it explicitly so the live editor never remains hidden behind it.
        self.motion.stop();self.setGeometry(geometry);self._motion_finished()
    def _collapse_if_unused(self):
        if self.isVisible() and not self.isActiveWindow() and not self._recording and not self.more_menu.isVisible():
            self.collapse_requested.emit()
    def event(self,event):
        if event.type()==QEvent.Type.WindowDeactivate and hasattr(self,'deactivate_timer') and not self._recording:
            self.deactivate_timer.start()
        return super().event(event)
    def paintEvent(self,event):
        if self._transition_pixmap is not None:
            p=QPainter(self);p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawPixmap(QRectF(self.rect()),self._transition_pixmap,QRectF(self._transition_pixmap.rect()))
        else:super().paintEvent(event)
    def hideEvent(self,event):
        self.motion.stop();self.deactivate_timer.stop();self.more_menu.hide();self._transition_pixmap=None;self.panel.show()
        super().hideEvent(event)
    def show_history(self):
        self._reload_history()
        self.history.show();self.history.raise_();self.history.activateWindow()
    def _reload_history(self):
        if not callable(self._history_loader):return
        self._history_records=self._history_loader()
        selected=self.history_filter.currentData()
        self.history_filter.blockSignals(True);self.history_filter.clear();self.history_filter.addItem('全部对话',None)
        seen=set()
        for item in reversed(self._history_records):
            tid,rid=item.get('task_id'),item.get('request_id')
            key=('task',tid) if tid else ('chat',rid) if rid else None
            if key is None or key in seen:continue
            seen.add(key)
            first=next((m.get('content','') for m in self._history_records if m.get('role')=='user'
                and (m.get('task_id')==tid if tid else m.get('request_id')==rid)), '')
            self.history_filter.addItem(('任务 · ' if tid else '对话 · ')+first[:30]+f' #{key[1][:6]}',key)
        for i in range(self.history_filter.count()):
            if self.history_filter.itemData(i)==selected:self.history_filter.setCurrentIndex(i);break
        self.history_filter.blockSignals(False);self._render_history()
    def _render_history(self):
        if not self._history_records:return
        selected=self.history_filter.currentData();self.transcript.clear()
        for item in self._history_records:
            if selected and item.get('task_id' if selected[0]=='task' else 'request_id')!=selected[1]:continue
            self.append_message(item['role'],item['content'])
    def send(self):
        text=self.input.toPlainText().strip()
        if text and self.send_button.isEnabled():
            self.input.clear();self.submitted.emit(text);self.collapse_requested.emit()
    def append_message(self,role,text):
        names={'user':'你','assistant':'黍','system':'状态'}
        c=self.transcript.textCursor();c.movePosition(QTextCursor.MoveOperation.End)
        if not self.transcript.document().isEmpty():c.insertText('\n\n')
        c.insertText(f"{names.get(role,'状态')}\n{text}");self.transcript.setTextCursor(c);self.transcript.ensureCursorVisible()
    def begin_dictation(self):
        self.voice_toggle_requested.emit()
    def set_recording(self,recording):
        self._recording=bool(recording);self.voice_button.recording=self._recording;self.voice_button.update()
        self.voice_button.setToolTip('结束录音' if recording else '语音输入')
        self.voice_status.setText('正在听…' if recording else 'Enter 发送')
    def set_busy(self,busy):
        self.send_button.setEnabled(not busy);self.stop_button.setVisible(busy);self.voice_status.setText('黍正在想…' if busy else 'Enter 发送')

class MessageBubble(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent,Qt.WindowType.Tool|Qt.WindowType.FramelessWindowHint|Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground);self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus);self.setStyleSheet(PANEL_STYLE)
        l=QVBoxLayout(self);l.setContentsMargins(2,2,2,2);self.panel=QFrame();self.panel.setObjectName('PetPanel');self.panel.setStyleSheet('QFrame#PetPanel{background:#fffefa;border:1px solid #e1e6e1;border-radius:15px;}');l.addWidget(self.panel)
        inner=QVBoxLayout(self.panel);inner.setContentsMargins(16,12,16,12);inner.setSpacing(5)
        self.text=QTextEdit();self.text.setReadOnly(True);self.text.setFocusPolicy(Qt.FocusPolicy.NoFocus);self.text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.text.setStyleSheet('QTextEdit{background:transparent;color:#38433d;border:0;padding:0;font-size:12px;}');inner.addWidget(self.text)
        self.scroll_hint=QLabel('滚动查看全部');self.scroll_hint.setObjectName('PetMuted');self.scroll_hint.hide();inner.addWidget(self.scroll_hint)
        self.expiry=QTimer(self);self.expiry.setSingleShot(True);self.expiry.timeout.connect(self._expire_if_due)
        self.fade=QPropertyAnimation(self,b'windowOpacity',self);self.fade.setDuration(400);self.fade.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._generation=0;self._fade_generation=None;self.fade.finished.connect(self._fade_finished)
        self.last_hold_ms=0;self._hovering=False;self._fade_due=0.;self._pinned=False
    @staticmethod
    def hold_duration(text):
        # Leave ample reading time, including longer technical answers.
        return int(min(90000,max(2100,1200+len(text)*72.5)))
    def present(self,text,anchor,*,hold=False):
        text=str(text).strip()
        if not text:return
        previous=self.text.toPlainText();bar=self.text.verticalScrollBar()
        continuing=hold and self._pinned and self.isVisible() and text.startswith(previous)
        scroll_position=bar.value();follow_end=scroll_position>=bar.maximum()-2
        self._generation+=1;self.expiry.stop();self.fade.stop();self._fade_generation=None;self.setWindowOpacity(1.0)
        self._pinned=hold
        if continuing:
            selected=self.text.textCursor();position,anchor_position=selected.position(),selected.anchor()
            cursor=QTextCursor(self.text.document());cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(text[len(previous):])
            selected.setPosition(anchor_position);selected.setPosition(position,QTextCursor.MoveMode.KeepAnchor)
            self.text.setTextCursor(selected)
        else:self.text.setPlainText(text)
        self.setFixedWidth(322)
        screen=QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        maximum=min(420,int(screen.availableGeometry().height()*.55)) if screen else 420
        measured=QTextDocument();measured.setDefaultFont(self.text.font());measured.setPlainText(text);measured.setTextWidth(284)
        height=math.ceil(measured.size().height())+3
        scroll=height>maximum-28;self.scroll_hint.setVisible(scroll)
        hint_height=self.scroll_hint.sizeHint().height()+5 if scroll else 0
        text_height=max(28,min(height,maximum-28-hint_height))
        self.text.setFixedHeight(text_height)
        # An earlier fixed height may cache the layout's constrained sizeHint.
        # Explicitly size the full bubble so short -> long replies can grow again.
        self.setFixedHeight(text_height+28+hint_height);self.layout().activate()
        bar.setValue(bar.maximum() if continuing and follow_end else scroll_position if continuing else 0)
        self.reanchor(anchor);self.show();self.last_hold_ms=self.hold_duration(text)
        if not hold:self._schedule_expiry(self.last_hold_ms)
    def release(self,milliseconds=1600):
        self._pinned=False;self._schedule_expiry(milliseconds)
    def reanchor(self,anchor):
        point=QPoint(anchor.x()-self.width()//2,anchor.y()-self.height()-9)
        self.move(_bounded(point,self.size(),QGuiApplication.screenAt(anchor)))
    def begin_fade(self):
        self._fade_generation=self._generation;self.fade.stop();self.fade.setStartValue(self.windowOpacity());self.fade.setEndValue(0.0);self.fade.start()
    def _schedule_expiry(self,milliseconds):
        self._fade_due=time.monotonic()+milliseconds/1000;self.expiry.start(milliseconds)
    def _expire_if_due(self):
        # A queued timeout from the previous reply must not start a new reply's fade.
        if self._pinned:return
        remaining=self._fade_due-time.monotonic()
        if remaining>.02:self.expiry.start(max(1,math.ceil(remaining*1000)))
        else:self.begin_fade()
    def _fade_finished(self):
        if self._fade_generation==self._generation and self.windowOpacity()<.01:self.hide()
    def enterEvent(self,event):
        self._hovering=True;self.expiry.stop();self.fade.stop();self.setWindowOpacity(1.0);super().enterEvent(event)
    def leaveEvent(self,event):
        self._hovering=False
        if not self._pinned:self._schedule_expiry(1100)
        super().leaveEvent(event)
    def hideEvent(self,event):
        self.expiry.stop();self.fade.stop();super().hideEvent(event)

class FloatingPet(QWidget):
    activity_requested=Signal()
    open_requested=Signal()  # Legacy workbench request, never emitted on a single pet click.
    workspace_requested=Signal()
    settings_requested=Signal(str)
    capture_requested=Signal()
    stop_requested=Signal()
    quit_requested=Signal()
    text_submitted=Signal(str)
    voice_toggle_requested=Signal()
    skin_changed=Signal(str)
    def __init__(self,atlas_path:Path):
        super().__init__(None,Qt.WindowType.Tool|Qt.WindowType.FramelessWindowHint|Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground);self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle('黍 · 桌面陪伴');self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip('点击和黍说话 · 拖动移动 · 双击跳跃 · 右键设置与动作')
        self.canvas=PetCanvas(atlas_path,CELL_HEIGHT,self,native_pixels=True)
        self.skin_catalog=SkinCatalog(atlas_path);self._appearance_path=None
        self.canvas.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents);self.canvas.set_follow_cursor(True)
        self.setFixedSize(max(self.canvas.width()+16,132),self.canvas.height()+53);self.canvas.move((self.width()-self.canvas.width())//2,0)
        self.toolbar=QFrame(self);self.toolbar.setObjectName('PetToolbar')
        self.toolbar.setStyleSheet('QFrame#PetToolbar{background:#f7f8f5;border:1px solid #e2e5de;border-radius:20px;}')
        self.toolbar.setGeometry((self.width()-122)//2,self.canvas.height()+5,122,40)
        bar=QHBoxLayout(self.toolbar);bar.setContentsMargins(7,3,7,3);bar.setSpacing(1)
        self.write_button=PetToolButton('write','打字和黍说话',self.toolbar);self.voice_button=PetToolButton('voice','语音输入',self.toolbar)
        self.menu_button=PetToolButton('more','任务进度、设置与屏幕权限',self.toolbar)
        for b in (self.write_button,self.voice_button,self.menu_button):bar.addWidget(b)
        self.write_button.clicked.connect(self.show_composer);self.voice_button.clicked.connect(self.voice_toggle_requested.emit);self.menu_button.clicked.connect(self.show_menu)
        self.chat=PetConversation(self);self.chat.submitted.connect(self.text_submitted.emit);self.chat.stop_requested.connect(self.stop_requested.emit)
        self.chat.settings_requested.connect(lambda:self.settings_requested.emit('model'));self.bubble=MessageBubble(self)
        self._bubble_source=None
        self.chat.voice_toggle_requested.connect(self.voice_toggle_requested.emit)
        self.chat.collapse_requested.connect(self._collapse_composer);self.chat.collapsed.connect(self._composer_collapsed)
        self._dock_origin=None;self._dock_target=None
        self._pet_motion=QPropertyAnimation(self,b'pos',self);self._pet_motion.setDuration(200);self._pet_motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.offset=None;self.dragged=False;self._press_global=QPoint();self._press_origin=QPoint();self._last_drag=QPoint();self._busy=False;self._status='';self._recording=False;self._voice_level=0.
        self._toolbar_opacity=QGraphicsOpacityEffect(self.toolbar);self._toolbar_opacity.setOpacity(0);self.toolbar.setGraphicsEffect(self._toolbar_opacity)
        self._toolbar_motion=QPropertyAnimation(self._toolbar_opacity,b'opacity',self);self._toolbar_motion.setDuration(150);self._toolbar_motion.setEasingCurve(QEasingCurve.Type.OutCubic);self._toolbar_motion.finished.connect(self._toolbar_motion_finished)
        self._toolbar_wanted=False;self.toolbar.hide()
        self._hover_timer=QTimer(self);self._hover_timer.setInterval(50);self._hover_timer.timeout.connect(self._update_hover);self._hover_timer.start()
        self._voice_timer=QTimer(self);self._voice_timer.setInterval(50);self._voice_timer.timeout.connect(self._animate_voice)
        self.menu=self._make_menu();self.canvas.frame_changed.connect(self._refresh_mask)
        for control in (self,self.toolbar,*self.toolbar.findChildren(QWidget),self.chat.input):control.installEventFilter(self)
        self.chat.input.textChanged.connect(self.canvas.notify_interaction)
        self.canvas.skin_changed.connect(self._skin_committed)
        self.canvas.skin_switching_changed.connect(lambda _active:self._update_skin_menu())
        self.canvas.skin_error.connect(lambda text:self.present_message('system',text))
        self.position_at_edge();self._refresh_mask()
    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Type.MouseButtonPress,QEvent.Type.MouseButtonDblClick,
                            QEvent.Type.Wheel,QEvent.Type.KeyPress,QEvent.Type.TouchBegin):
            self.canvas.notify_interaction()
        return super().eventFilter(watched,event)
    def _make_menu(self):
        menu=QMenu(self)
        menu.aboutToShow.connect(self.canvas.notify_interaction)
        menu.aboutToHide.connect(self.canvas.notify_interaction)
        menu.triggered.connect(lambda _action:self.canvas.notify_interaction())
        menu.setStyleSheet('QMenu{background:#f7f8f5;color:#29362f;border:1px solid #dce2d9;border-radius:8px;padding:5px;font-family:"Microsoft YaHei UI","Microsoft YaHei";}QMenu::item{padding:8px 22px;}QMenu::item:selected{background:#dde7da;border-radius:4px;}')
        menu.addAction('和黍说话',self.show_composer);menu.addAction('语音输入',self.voice_toggle_requested.emit)
        menu.addAction('对话记录',self.show_history)
        settings=menu.addMenu('设置');settings.addAction('模型与 API Key',lambda:self.settings_requested.emit('model'))
        settings.addAction('屏幕获取与权限',lambda:self.settings_requested.emit('screen'));settings.addAction('其他设置',lambda:self.settings_requested.emit('general'))
        menu.addAction('观察游戏画面',self.capture_requested.emit);menu.addAction('任务进度',self.activity_requested.emit);menu.addSeparator()
        self.follow_action=menu.addAction('视线跟随鼠标');self.follow_action.setCheckable(True);self.follow_action.setChecked(True);self.follow_action.toggled.connect(self.canvas.set_follow_cursor)
        self.animated_action=menu.addAction('播放动画');self.animated_action.setCheckable(True);self.animated_action.setChecked(True);self.animated_action.toggled.connect(self.canvas.set_animated)
        actions=menu.addMenu('动作')
        for state,text in LABELS.items():actions.addAction(text,lambda checked=False,s=state:self.canvas.play(s))
        self.skin_menu=menu.addMenu('切换皮肤');self.skin_menu.aboutToShow.connect(self._update_skin_menu);self._rebuild_skin_menu()
        menu.addAction('回到屏幕右下角',self.position_at_edge);menu.addSeparator()
        menu.addAction('停止本地操作',self.stop_requested.emit);menu.addAction('退出黍',self.quit_requested.emit)
        return menu
    def configure_skins(self,appearance_path=None,*,catalog=None):
        """Resolve a local appearance without touching shared model/voice preferences."""
        if catalog is not None:self.skin_catalog=catalog
        self._appearance_path=Path(appearance_path) if appearance_path is not None else None
        self._rebuild_skin_menu()
        saved=load_skin_preference(self._appearance_path) if self._appearance_path else None
        chosen=self.skin_catalog.get(saved or '') or self.skin_catalog.get(self.skin_catalog.default_id)
        if chosen is None or (chosen!=self.canvas._skin and not self.canvas.set_skin(chosen,animate=False)):
            # A malformed optional skin must never prevent the original pet loading.
            self.canvas.set_skin(self.skin_catalog.legacy,animate=False)
        self._update_skin_menu()
    def _rebuild_skin_menu(self):
        self.skin_menu.clear();self.skin_actions={};self._skin_group=QActionGroup(self.skin_menu)
        self._skin_group.setExclusive(True)
        for skin in self.skin_catalog.ordered():
            action=self.skin_menu.addAction(skin.label);action.setCheckable(True)
            self._skin_group.addAction(action);self.skin_actions[skin.id]=action
            action.triggered.connect(lambda checked=False,key=skin.id:self.select_skin(key))
        self._update_skin_menu()
    def _update_skin_menu(self):
        for skin_id,action in self.skin_actions.items():
            action.setChecked(skin_id==self.canvas.skin_id)
            action.setEnabled(not self.canvas.skin_switching)
    def select_skin(self,skin_id):
        skin=self.skin_catalog.get(skin_id)
        changed=bool(skin and not self.canvas.dragging and self.canvas.set_skin(skin))
        self._update_skin_menu();return changed
    def _skin_committed(self,skin_id):
        self._update_skin_menu()
        if self._appearance_path:
            try:save_skin_preference(self._appearance_path,skin_id)
            except OSError:self.set_status('皮肤已切换，本次外观偏好未能保存。')
        self.skin_changed.emit(skin_id)
    def _open_workspace(self):self.workspace_requested.emit();self.open_requested.emit()
    def show_menu(self):
        self.canvas.notify_interaction();self.menu.popup(self.toolbar.mapToGlobal(QPoint(0,self.toolbar.height()+4)));self._update_hover()
    def _toolbar_motion_finished(self):
        if not self._toolbar_wanted:self.toolbar.hide();self._refresh_mask()
    def _set_toolbar_visible(self,visible,*,immediate=False):
        visible=bool(visible) and not self.chat.isVisible()
        if self._toolbar_wanted==visible and not immediate:return
        self._toolbar_wanted=visible;self._toolbar_motion.stop()
        if visible:self.toolbar.show();self._refresh_mask()
        target=1. if visible else 0.
        if immediate:
            self._toolbar_opacity.setOpacity(target)
            if not visible:self.toolbar.hide();self._refresh_mask()
        else:
            self._toolbar_motion.setStartValue(self._toolbar_opacity.opacity());self._toolbar_motion.setEndValue(target);self._toolbar_motion.start()
    def _update_hover(self):
        blocked=(not self.isVisible() or self.chat.isVisible() or self.chat.history.isVisible()
                 or self.menu.isVisible() or self._recording or self._busy
                 or getattr(self,'_game_active',False) or self.bubble.isVisible())
        self.canvas.set_idle_vignette_blocked(blocked)
        if not self.isVisible():return
        near=self.geometry().adjusted(-14,-10,14,12).contains(QCursor.pos())
        self._set_toolbar_visible(near or self._recording or self.menu.isVisible())
    def set_recording(self,recording):
        self._recording=bool(recording);self.voice_button.recording=self._recording;self.voice_button.update();self.chat.set_recording(recording)
        self.voice_button.setToolTip('结束录音' if recording else '语音输入')
        if recording:self._voice_timer.start()
        else:self._voice_timer.stop();self._voice_level=0.
        self._animate_voice();self._update_hover()
    def set_voice_level(self,level):
        self._voice_level=max(0.,min(1.,float(level)));self.voice_button.level=self._voice_level;self.chat.voice_button.level=self._voice_level;self._animate_voice()
    def _animate_voice(self):
        if self._recording:
            pulse=.2+.65*self._voice_level
            self.voice_button.setStyleSheet(f'QToolButton{{border:0;border-radius:16px;background:rgba(208,215,210,{round(55+pulse*100)});}}QToolButton:hover{{background:#dde5df;}}')
        else:self.voice_button.setStyleSheet('QToolButton{border:0;border-radius:12px;background:transparent;}QToolButton:hover{background:#e4e8e2;}')
        self.voice_button.update()
    def receive_transcript(self,text):
        text=str(text).strip()
        if not text:return
        self.set_recording(False);self.show_composer()
        existing=self.chat.input.toPlainText().strip()
        self.chat.input.setPlainText((existing+' ' if existing else '')+text)
        self.chat.input.moveCursor(QTextCursor.MoveOperation.End)
    def position_at_edge(self):
        screen=QGuiApplication.screenAt(self.pos()) or QGuiApplication.primaryScreen()
        if screen:
            a=screen.availableGeometry();self.move(_bounded(QPoint(a.right()-self.width()-25,a.bottom()-self.height()-18),self.size(),screen))
    def show_composer(self,checked=False,*,voice=False):
        self.canvas.notify_interaction()
        if voice:self.voice_toggle_requested.emit();return
        if not self.chat.isVisible() or self.chat._closing:
            start=self.chat.geometry() if self.chat.isVisible() else QRect(self.toolbar.mapToGlobal(QPoint(0,0)),self.toolbar.size())
            screen=QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
            if self._dock_origin is None:self._dock_origin=self.pos()
            end_pos=self.pos()
            if screen:
                available=screen.availableGeometry()
                overflow=max(0,end_pos.y()+self.canvas.height()+5+self.chat.expanded_size[1]-(available.bottom()-12))
                end_pos=QPoint(end_pos.x(),end_pos.y()-overflow)
            self._dock_target=end_pos
            self._pet_motion.stop();self._pet_motion.setStartValue(self.pos());self._pet_motion.setEndValue(end_pos);self._pet_motion.start()
            final=self._composer_rect(end_pos)
            self._set_toolbar_visible(False,immediate=True);self.chat.expand_from(start,final)
        else:self.chat.raise_();self.chat.activateWindow();self.chat.input.setFocus()
        if not self._busy:self.canvas.play('waving')
        self._update_hover()
    def _composer_rect(self,pet_position=None):
        position=pet_position if pet_position is not None else self.pos()
        width,height=self.chat.expanded_size
        point=QPoint(position.x()+self.width()//2-width//2,position.y()+self.canvas.height()+5)
        return QRect(_bounded(point,self._expanded_size(),QGuiApplication.screenAt(position)),self._expanded_size())
    def _expanded_size(self):
        from PySide6.QtCore import QSize
        return QSize(*self.chat.expanded_size)
    def _position_chat(self):
        self.chat.reposition(self._composer_rect())
    def _collapse_composer(self):
        if not self.chat.isVisible():return
        self._pet_motion.stop()
        origin=self._dock_origin if self._dock_origin is not None else self.pos()
        target=QRect(origin+QPoint(self.toolbar.x(),self.toolbar.y()),self.toolbar.size())
        self._pet_motion.setStartValue(self.pos());self._pet_motion.setEndValue(origin);self._pet_motion.start()
        self.chat.collapse_to(target)
    def _composer_collapsed(self):
        self._dock_origin=None;self._dock_target=None;self._update_hover();self._refresh_mask()
    def _dismiss_input(self):
        self._pet_motion.stop();self.chat.motion.stop();self.chat.hide();self.chat.history.hide()
        if self._dock_origin is not None:self.move(self._dock_origin)
        self._dock_origin=None;self._dock_target=None;self._set_toolbar_visible(False,immediate=True);self._refresh_mask()
        self._update_hover()
    def show_history(self):self.canvas.notify_interaction();self.chat.show_history();self._update_hover()
    def restore_history(self,messages):
        self.chat.transcript.clear()
        for message in messages or []:
            if isinstance(message,dict):role,text=message.get('role'),message.get('content',message.get('text',''))
            elif isinstance(message,(list,tuple)) and len(message)==2:role,text=message
            else:continue
            if role in ('user','assistant','system') and isinstance(text,str) and text.strip():self.chat.append_message(role,text)
    def present_message(self,role,text,*,show_bubble=True):
        self.canvas.notify_interaction()
        self.chat.append_message(role,text)
        if show_bubble and role!='user' and self.isVisible():
            self._bubble_source=role;self.bubble.present(text,self._bubble_anchor())
        if role=='system' and not self._busy:self.canvas.play('waiting')
    def speech_preparing(self):
        self.canvas.notify_interaction()
        if self._bubble_source in ('assistant','speech'):
            self._bubble_source=None;self.bubble.hide();self.bubble.text.clear()
    def speech_caption(self,text):
        self.canvas.notify_interaction()
        if self.isVisible():
            self._bubble_source='speech';self.bubble.present(text,self._bubble_anchor(),hold=True)
    def speech_finished(self):
        if self._bubble_source=='speech':self.bubble.release()
    def speech_cancelled(self):
        if self._bubble_source=='speech':
            self._bubble_source=None;self.bubble.hide()
    def set_busy(self,busy):
        self._busy=bool(busy);self.chat.set_busy(busy)
        active=getattr(self,'_game_active',False)
        self.chat.stop_button.setVisible(bool(busy) or active)
        self.canvas.play('running' if busy or active else 'idle')
    def set_game_active(self,active):
        self._game_active=bool(active);self.set_busy(self._busy)
        if active and not self._busy:self.chat.voice_status.setText('执行中 · 可继续补充要求')
    def play(self,state):self.canvas.play(state)
    def _bubble_anchor(self):
        return self.canvas.mapToGlobal(QPoint(self.canvas.width()//2,round(self.canvas.draw_rect.height()*.14)))
    def set_status(self,text):self._status=str(text);self.setToolTip(self._status+'\n点击和黍说话 · 拖动移动 · 右键设置')
    def _refresh_mask(self):
        if QGuiApplication.platformName()=='offscreen':return
        frame=self.canvas.displayed_frame()
        if frame:
            mask=frame.scaled(self.canvas.size(),Qt.AspectRatioMode.IgnoreAspectRatio,Qt.TransformationMode.FastTransformation).mask()
            region=QRegion(mask).translated(self.canvas.pos())
            if not self.toolbar.isHidden():region|=QRegion(self.toolbar.geometry())
            self.setMask(region)
    def mousePressEvent(self,event):
        self.canvas.notify_interaction()
        if self.canvas.skin_switching:return
        if event.button()==Qt.MouseButton.LeftButton:
            self.offset=event.globalPosition().toPoint()-self.pos();self._press_global=event.globalPosition().toPoint();self._press_origin=self.pos()
            self._last_drag=self._press_global;self.dragged=False;self.canvas.dragging=True
    def mouseMoveEvent(self,event):
        if self.canvas.skin_switching:return
        if self.offset is None or not event.buttons()&Qt.MouseButton.LeftButton:return
        self.canvas.notify_interaction()
        point=event.globalPosition().toPoint();delta=point-self._press_global
        if not self.dragged and delta.manhattanLength()<QApplication.startDragDistance():return
        self.dragged=True;self.setCursor(Qt.CursorShape.ClosedHandCursor);self.move(_bounded(self._press_origin+delta,self.size(),QGuiApplication.screenAt(point)))
        horizontal=point.x()-self._last_drag.x()
        if abs(horizontal)>=2:self.canvas.play('running-right' if horizontal>0 else 'running-left')
        self._last_drag=point
        if self._dock_origin is not None:self._dock_origin=self.pos();self._dock_target=self.pos()
        if self.chat.isVisible():self._position_chat()
        self.bubble.hide()
    def mouseReleaseEvent(self,event):
        self.canvas.notify_interaction()
        if event.button()==Qt.MouseButton.LeftButton and self.offset is not None:
            dragged=self.dragged;self.offset=None;self.canvas.dragging=False;self.setCursor(Qt.CursorShape.OpenHandCursor)
            if dragged:self.canvas.play('running' if self._busy else 'idle')
            else:self.show_composer()
    def mouseDoubleClickEvent(self,event):
        self.canvas.notify_interaction()
        if self.canvas.skin_switching:return
        if event.button()==Qt.MouseButton.LeftButton:self._collapse_composer();self.canvas.play('jumping')
    def contextMenuEvent(self,event):self.canvas.notify_interaction();self.menu.popup(event.globalPos());self._update_hover()
    def showEvent(self,event):
        super().showEvent(event)
        if hasattr(self,'menu'):self._update_hover()
    def hideEvent(self,event):
        self.canvas.set_idle_vignette_blocked(True)
        if hasattr(self,'chat'):self._dismiss_input();self.bubble.hide()
        super().hideEvent(event)
    def hide(self):
        self.canvas.set_idle_vignette_blocked(True)
        if hasattr(self,'chat'):self._dismiss_input();self.bubble.hide()
        if hasattr(self,'menu'):self.menu.hide()
        super().hide()
    def moveEvent(self,event):
        if hasattr(self,'bubble') and self.bubble.isVisible():self.bubble.reanchor(self._bubble_anchor())
        super().moveEvent(event)
    def closeEvent(self,event):self._pet_motion.stop();self._hover_timer.stop();self._voice_timer.stop();self._toolbar_motion.stop();self.chat.close();self.chat.history.close();self.bubble.close();super().closeEvent(event)

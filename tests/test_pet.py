"""Pet shell checks, with no real dictation, model or game operation."""
import hashlib
import math
import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication
from shu.pet import FloatingPet, PetCanvas
from shu.pet_core import STATES

ROOT=Path(__file__).resolve().parents[1]
ATLAS=ROOT/'assets/spritesheet.png'

class PetShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        if not isinstance(cls.app,QApplication):raise unittest.SkipTest('Run pet GUI tests in a separate process')
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):self.pet=FloatingPet(ATLAS)
    def tearDown(self):
        self.pet.close();self.pet.deleteLater();self.app.processEvents()

    def test_preserves_original_atlas_and_all_73_cells(self):
        self.assertEqual(hashlib.sha256(ATLAS.read_bytes()).hexdigest(),'3eedb89169f57a4414e850662ccd4390c9468cbfa79b1d4dd76da47169fd4857')
        self.assertEqual(len(self.pet.canvas.frames),73)
        for state,(row,_) in STATES.items():
            self.pet.canvas.play(state)
            self.assertEqual(self.pet.canvas.current_cell,(row,0))

    def test_default_draw_uses_native_physical_pixels(self):
        canvas=self.pet.canvas;ratio=canvas.devicePixelRatioF()
        self.assertAlmostEqual(canvas.draw_rect.width()*ratio,192)
        self.assertAlmostEqual(canvas.draw_rect.height()*ratio,208)

    def test_cursor_motion_selects_each_of_sixteen_look_directions(self):
        c=self.pet.canvas;c.play('idle');c.follow_cursor=True
        head=c.mapToGlobal(QPoint(c.width()//2,round(c.height()*68/208)))
        for sector in range(16):
            angle=math.radians(sector*22.5)
            cursor=head+QPoint(round(200*math.sin(angle)),round(-200*math.cos(angle)))
            with patch('shu.pet.QCursor.pos',return_value=cursor):c._tick()
            self.assertEqual(c.current_cell,(9+sector//8,sector%8))
        c.play('running')
        with patch('shu.pet.QCursor.pos',return_value=head+QPoint(300,0)):c._tick()
        self.assertEqual(c.current_cell[0],7,'Busy animation must not be replaced by a gaze frame')

    def test_pet_click_surface_opens_compact_input_not_workbench(self):
        opened=[];self.pet.open_requested.connect(lambda:opened.append(True))
        self.pet.write_button.click();self.app.processEvents()
        self.assertTrue(self.pet.chat.isVisible())
        self.assertEqual(opened,[])
        self.pet.hide();self.assertFalse(self.pet.chat.isVisible())

    def test_composer_enter_sends_and_shift_enter_keeps_newline(self):
        submitted=[];self.pet.text_submitted.connect(submitted.append)
        self.pet.show_composer();self.pet.chat.input.setPlainText('观察游戏画面')
        QTest.keyClick(self.pet.chat.input,Qt.Key.Key_Return)
        self.assertEqual(submitted,['观察游戏画面'])
        self.pet.chat.input.setPlainText('保留这一行')
        QTest.keyClick(self.pet.chat.input,Qt.Key.Key_Return,Qt.KeyboardModifier.ShiftModifier)
        self.assertIn('\n',self.pet.chat.input.toPlainText())

    def test_controls_emit_requests_and_stop_never_waits_for_chat(self):
        stops=[];settings=[];workspace=[]
        self.pet.stop_requested.connect(lambda:stops.append(True));self.pet.settings_requested.connect(settings.append)
        self.pet.workspace_requested.connect(lambda:workspace.append(True))
        self.pet.set_busy(True);self.pet.chat.stop_button.click()
        self.assertEqual(stops,[True]);self.assertFalse(self.pet.chat.send_button.isEnabled())
        settings_menu=next(a.menu() for a in self.pet.menu.actions() if a.text()=='设置')
        settings_menu.actions()[0].trigger();self.assertEqual(settings,['model'])
        self.pet._open_workspace();self.assertEqual(workspace,[True])

    def test_original_frames_paint_without_swallowed_qt_exception(self):
        with patch('sys.excepthook') as hook:
            self.pet.show();self.app.processEvents()
            for state in STATES:self.pet.canvas.play(state);self.pet.canvas.repaint()
            self.pet.show_composer();self.app.processEvents()
        self.assertFalse(hook.called,hook.call_args_list)

    def test_voice_button_requests_native_recording_without_opening_a_system_ui(self):
        calls=[];self.pet.voice_toggle_requested.connect(lambda:calls.append(True))
        self.pet.voice_button.click();self.assertEqual(calls,[True])
        self.assertFalse(self.pet.chat.isVisible())
        self.pet.chat.voice_button.click();self.assertEqual(calls,[True,True])
        self.pet.set_recording(True)
        self.assertTrue(self.pet.voice_button.recording);self.assertTrue(self.pet.chat.voice_button.recording)
        self.assertNotIn('Windows',self.pet.chat.voice_status.text())
        self.pet.set_recording(False)
        self.assertFalse(self.pet.voice_button.recording)

    def test_voice_transcript_preserves_draft_and_waits_for_user_send(self):
        sent=[];self.pet.text_submitted.connect(sent.append)
        self.pet.chat.input.setPlainText('已有草稿')
        self.pet.receive_transcript('语音结果');QTest.qWait(230)
        self.assertTrue(self.pet.chat.isVisible())
        self.assertEqual(self.pet.chat.input.toPlainText(),'已有草稿 语音结果')
        self.assertEqual(sent,[])

    def test_running_motion_does_not_restart_the_same_animation_on_each_mouse_move(self):
        c=self.pet.canvas;c.timer.stop()
        for state in ('running-left','running-right'):
            c.play(state);c.animation.advance(c.animation.deadline+.001)
            frame,deadline=c.animation.frame,c.animation.deadline
            changes=[];c.frame_changed.connect(lambda:changes.append(True))
            for _ in range(20):c.play(state)
            self.assertTrue(c.animation.loop)
            self.assertEqual((c.animation.frame,c.animation.deadline),(frame,deadline))
            self.assertEqual(changes,[])

    def test_bottom_composer_expands_smoothly_and_send_collapses_back_to_pill(self):
        self.pet.show();self.pet.move(400,380);self.pet.show_composer();QTest.qWait(240)
        self.assertEqual(self.pet.chat.motion.duration(),200)
        self.assertTrue(self.pet.chat.isVisible());self.assertTrue(self.pet.toolbar.isHidden())
        self.assertGreaterEqual(self.pet.chat.y(),self.pet.y()+self.pet.canvas.height())
        self.assertEqual(self.pet.chat.width(),370)
        self.pet.chat.input.setPlainText('输入框发送后收起');self.pet.chat.send();QTest.qWait(240)
        self.assertFalse(self.pet.chat.isVisible())

    def test_reply_is_always_above_pet_and_full_text_survives_long_answers(self):
        self.pet.show();self.pet.move(400,450);self.pet.show_composer();QTest.qWait(230)
        text='这是一段保留完整内容的答复。'*100
        self.pet.present_message('assistant',text);self.app.processEvents()
        self.assertTrue(self.pet.chat.isVisible());self.assertTrue(self.pet.bubble.isVisible())
        self.assertEqual(self.pet.bubble.text.toPlainText(),text)
        self.assertLess(self.pet.bubble.y(),self.pet.y())
        self.assertTrue(self.pet.bubble.scroll_hint.isVisible())
        self.assertGreater(self.pet.bubble.last_hold_ms,self.pet.bubble.hold_duration('短回复'))

    def test_new_answer_cancels_old_fade_and_uses_a_400ms_fade_out(self):
        self.pet.show();b=self.pet.bubble;b.present('第一条回复',QPoint(420,420))
        self.assertEqual(b.fade.duration(),400);b.begin_fade();QTest.qWait(160)
        self.assertGreater(b.windowOpacity(),0);self.assertLess(b.windowOpacity(),1)
        b.present('第二条回复要完整保留。',QPoint(420,420));QTest.qWait(550)
        self.assertTrue(b.isVisible());self.assertEqual(b.text.toPlainText(),'第二条回复要完整保留。')
        self.assertAlmostEqual(b.windowOpacity(),1,places=2)

    def test_restore_history_is_silent_and_does_not_open_or_flash_a_reply(self):
        self.pet.restore_history([{'role':'user','content':'上一次聊天'}, {'role':'assistant','content':'已经保留上下文'}])
        self.assertIn('上一次聊天',self.pet.chat.transcript.toPlainText())
        self.assertIn('已经保留上下文',self.pet.chat.transcript.toPlainText())
        self.assertFalse(self.pet.bubble.isVisible());self.assertFalse(self.pet.chat.isVisible())
        self.assertFalse(self.pet.chat.history.isVisible())

    def test_speech_preparation_is_silent_and_keeps_the_complete_history(self):
        self.pet.show();self.pet.present_message('assistant','上一句已经说完了。')
        self.pet.present_message('assistant','新的完整回复先保存在记录。',show_bubble=False)
        self.pet.speech_preparing();self.app.processEvents()
        self.assertFalse(self.pet.bubble.isVisible())
        self.assertIn('新的完整回复先保存在记录。',self.pet.chat.transcript.toPlainText())
        self.assertNotIn('正在准备语音',self.pet.chat.transcript.toPlainText())

    def test_speech_events_preserve_a_newer_system_message(self):
        self.pet.show();self.pet.speech_caption('正在说出的部分')
        self.pet.present_message('system','麦克风无法使用，请检查权限。')
        hold=self.pet.bubble.expiry.remainingTime()
        self.pet.speech_preparing();self.pet.speech_finished();self.pet.speech_cancelled()
        self.assertTrue(self.pet.bubble.isVisible())
        self.assertEqual(self.pet.bubble.text.toPlainText(),'麦克风无法使用，请检查权限。')
        self.assertGreater(self.pet.bubble.expiry.remainingTime(),hold-100)

    def test_progressive_speech_stays_pinned_then_releases_after_playback(self):
        self.pet.show();self.pet.speech_preparing()
        for caption in ('慢','慢慢来','慢慢来，我会陪着你。'):
            self.pet.speech_caption(caption)
            self.assertEqual(self.pet.bubble.text.toPlainText(),caption)
            self.assertFalse(self.pet.bubble.expiry.isActive())
            self.assertTrue(self.pet.bubble.isVisible())
        self.pet.speech_finished()
        self.assertTrue(self.pet.bubble.expiry.isActive())
        self.pet.speech_cancelled();self.assertFalse(self.pet.bubble.isVisible())

    def test_progressive_caption_preserves_selection_and_follows_new_lines(self):
        self.pet.show();self.pet.speech_caption('慢慢来，我会陪着你。')
        editor=self.pet.bubble.text;cursor=editor.textCursor()
        cursor.setPosition(0);cursor.setPosition(3,cursor.MoveMode.KeepAnchor);editor.setTextCursor(cursor)
        self.pet.speech_caption('慢慢来，我会陪着你。把眼前的事情一件件做好。')
        self.assertEqual(editor.textCursor().selectedText(),'慢慢来')
        self.pet.speech_caption('逐字朗读的新一段。'*60)
        bar=editor.verticalScrollBar();bar.setValue(bar.maximum())
        self.pet.speech_caption('逐字朗读的新一段。'*61)
        self.assertEqual(bar.value(),bar.maximum())

    def test_queued_expiry_cannot_fade_a_pinned_speech_caption(self):
        self.pet.show();self.pet.present_message('system','旧状态')
        self.pet.bubble._fade_due=0
        self.pet.speech_caption('刚开始读的字幕')
        self.pet.bubble._expire_if_due()
        self.assertIsNone(self.pet.bubble._fade_generation)
        self.assertFalse(self.pet.bubble.expiry.isActive())

    def test_long_reply_grows_after_a_short_bubble_and_keeps_its_final_line(self):
        b=self.pet.bubble;b.present('短回复',QPoint(450,450));self.app.processEvents();short=b.height()
        long_text='这是较长回复，用于验证从短气泡扩展。'*60+'\n答复最后一行。'
        b.present(long_text,QPoint(450,450));self.app.processEvents()
        self.assertGreater(b.height(),short+100)
        self.assertGreater(b.text.height(),200)
        self.assertEqual(b.text.toPlainText(),long_text)
        self.assertGreater(b.text.verticalScrollBar().maximum(),0)
        b.text.verticalScrollBar().setValue(b.text.verticalScrollBar().maximum())
        self.assertTrue(b.text.toPlainText().endswith('答复最后一行。'))

    def test_late_expiry_callback_cannot_start_a_new_reply_fade_early(self):
        b=self.pet.bubble;b.present('旧消息',QPoint(400,400))
        b.present('新消息仍然应该保持可读。',QPoint(400,400))
        b._expire_if_due()
        self.assertIsNone(b._fade_generation)
        self.assertAlmostEqual(b.windowOpacity(),1,places=2)
        self.assertTrue(b.expiry.isActive())

    def test_toolbar_is_hidden_at_rest_and_fades_in_only_near_pet(self):
        self.pet.show();self.pet.move(400,380)
        with patch('shu.pet.QCursor.pos',return_value=QPoint(0,0)):
            self.pet._update_hover();QTest.qWait(180)
            self.assertTrue(self.pet.toolbar.isHidden())
        with patch('shu.pet.QCursor.pos',return_value=self.pet.geometry().center()):
            self.pet._update_hover();QTest.qWait(180)
            self.assertFalse(self.pet.toolbar.isHidden())
            self.assertAlmostEqual(self.pet._toolbar_opacity.opacity(),1,places=2)
        with patch('shu.pet.QCursor.pos',return_value=QPoint(0,0)):
            self.pet._update_hover();QTest.qWait(180)
            self.assertTrue(self.pet.toolbar.isHidden())

    def test_clicking_elsewhere_collects_input_even_with_a_draft(self):
        self.pet.show();self.pet.show_composer();QTest.qWait(230)
        self.pet.chat.input.setPlainText('这份草稿不要丢失')
        with patch.object(self.pet.chat,'isActiveWindow',return_value=False):
            self.pet.chat._collapse_if_unused();QTest.qWait(230)
        self.assertFalse(self.pet.chat.isVisible())
        self.assertEqual(self.pet.chat.input.toPlainText(),'这份草稿不要丢失')

    def test_dragging_during_input_expansion_keeps_the_editor_live(self):
        self.pet.show();self.pet.show_composer()
        self.pet.chat.motion.pause();self.pet.chat.motion.setCurrentTime(40)
        self.assertIsNotNone(self.pet.chat._transition_pixmap)
        self.pet._position_chat();self.app.processEvents()
        self.assertIsNone(self.pet.chat._transition_pixmap)
        self.assertTrue(self.pet.chat.input.isVisible())
        self.pet.chat.input.setPlainText('动画中移动后仍可输入')
        self.assertEqual(self.pet.chat.input.toPlainText(),'动画中移动后仍可输入')

    def test_dismissing_the_more_menu_rechecks_outside_focus(self):
        self.pet.show();self.pet.show_composer();QTest.qWait(230)
        self.pet.chat.input.setPlainText('菜单关闭后保留草稿')
        with patch.object(self.pet.chat,'isActiveWindow',return_value=False):
            self.pet.chat.more_menu.popup(QPoint(300,300));QTest.qWait(130)
            self.assertTrue(self.pet.chat.isVisible())
            collected=QSignalSpy(self.pet.chat.collapsed)
            self.pet.chat.more_menu.hide()
            # Await the actual transition, not 100 ms + 200 ms + a narrow margin.
            # Parallel Qt processes can delay a timer tick without losing it.
            self.assertTrue(collected.wait(2000),'Input did not collect after the menu lost focus')
        self.assertFalse(self.pet.chat.isVisible())
        self.assertEqual(self.pet.chat.input.toPlainText(),'菜单关闭后保留草稿')

    def test_game_activity_keeps_input_available_and_stop_visible(self):
        self.pet.show();self.pet.show_composer();QTest.qWait(230)
        self.pet.set_game_active(True)
        self.assertTrue(self.pet.chat.send_button.isEnabled())
        self.assertFalse(self.pet.chat.stop_button.isHidden())
        self.pet.set_busy(True)
        self.assertFalse(self.pet.chat.send_button.isEnabled())
        self.pet.set_busy(False)
        self.assertTrue(self.pet.chat.send_button.isEnabled())
        self.assertFalse(self.pet.chat.stop_button.isHidden())
        self.pet.set_game_active(False)
        self.assertTrue(self.pet.chat.stop_button.isHidden())

    def test_reply_lifetime_is_exactly_half_the_previous_duration(self):
        for length in (1,20,100,10000):
            previous=min(180000,max(4200,2400+length*145))
            self.assertEqual(self.pet.bubble.hold_duration('字'*length),int(previous/2))

if __name__=='__main__':unittest.main()

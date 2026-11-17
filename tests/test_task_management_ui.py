import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
import threading
import time
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory
from PySide6.QtCore import QPoint, QPointF, QRect, Qt, QEvent, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication
from shu.activity_ui import ActivityPanel
from shu.activity_history import ActivityHistory
from shu.companion_ui import CompanionShell
from tests.ui_fixtures import FakeController


class ManagementController(FakeController):
    message_routed=Signal(dict)
    request_started=Signal(str,str)
    context_message=Signal(dict)

    def __init__(self,path):
        super().__init__(path)
        self.target={'mode':'auto','task_id':None}
        self.controls=[]

    def task_management_state(self):
        return {'tasks':[{'id':'a','goal':'孤星导入','status':'paused'}],
                'trashed':[], 'requests':[], 'input_target':self.target}

    def set_input_target(self,task_id=None,mode='auto'):
        self.target={'mode':mode,'task_id':task_id}

    def trash_task(self,task_id):
        self.controls.append(('trash',task_id))
        raise ValueError('测试：保护中，等待核对')

    def continue_task(self,task_id):
        self.controls.append(('continue',task_id))
        return {'status':'running'}


class TaskManagementUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not isinstance(cls.app, QApplication):
            raise unittest.SkipTest('Requires an isolated GUI process')

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.panel = ActivityPanel()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.panel.deleteLater)
        self.addCleanup(self.panel.hide)

    def state(self, status='paused'):
        return {'tasks': [{'id': 'a', 'goal': '孤星导入', 'status': status}],
                'trashed': [{'id': 'b', 'goal': '旧任务', 'status': 'paused', 'lifecycle': 'trashed'}],
                'requests': [{'request_id': 'r1', 'task_id': 'a', 'text': '首次要求'},
                             {'request_id': 'r2', 'task_id': 'a', 'text': '补充要求'}]}

    def test_actual_tasks_group_messages_and_legacy_does_not_guess(self):
        self.panel.begin_request('legacy', '孤星导入')
        self.panel.set_management_state(self.state())
        self.assertEqual(self.panel.history_list.count(), 2)
        self.panel.select_task('a')
        self.assertIn('首次要求', self.panel.timeline.toPlainText())
        self.assertIn('补充要求', self.panel.timeline.toPlainText())
        self.assertEqual(self.panel.selected_task_id, 'a')
        self.panel.select_request('legacy')
        self.assertIsNone(self.panel.selected_task_id)
        self.assertFalse(self.panel.delete_button.isEnabled())

    def test_shell_cleanup_submission_does_not_run_synchronous_scanner(self):
        controller=ManagementController(Path(self.temp.name))
        self.addCleanup(controller.engine.close)
        submitted=[]
        def slow_purge(task):
            time.sleep(.15)
            return {'status':'completed'}
        controller.purge_task=slow_purge
        controller.start_task_cleanup=lambda task:submitted.append(task) or {'status':'queued'}
        with patch.object(CompanionShell,'_setup_tray'):
            shell=CompanionShell(controller,Path(__file__).resolve().parents[1])
        self.addCleanup(shell.deleteLater);self.addCleanup(shell.quit_application)
        before=time.monotonic();shell._manage_task('purge','a')
        self.assertLess(time.monotonic()-before,.08,'Qt handler must submit cleanup only')
        self.assertEqual(submitted,['a'])

    def test_new_input_preserves_explicit_task_and_recycle_browsing(self):
        self.panel.set_management_state(self.state())
        self.panel.select_task('a')
        self.panel.begin_request('new', '另一条输入')
        self.assertEqual(self.panel.selected_task_id, 'a')
        self.assertIn('孤星导入', self.panel.goal.text())
        self.panel.recycle_button.click()
        self.panel.select_task('b')
        self.panel.begin_request('newer', '新的普通聊天')
        self.assertEqual(self.panel.selected_task_id, 'b')
        self.assertIn('旧任务', self.panel.goal.text())
        self.assertEqual(self.panel.history_hint.text(), '回收箱 · 只读')

    def _show_long_task_timeline(self):
        state = self.state('running')
        self.panel.begin_request('r1', '首次要求')
        self.panel.set_task(state['tasks'][0])
        self.panel.set_management_state(state)
        self.panel.select_task('a')
        self.panel.show_at(QPoint(800, 500))
        for index in range(18):
            self._append_task_progress(index)
        self.app.processEvents()
        bar = self.panel.timeline.verticalScrollBar()
        self.assertGreater(bar.maximum(), 60)
        return state, bar

    def _append_task_progress(self, index):
        self.panel.append_event({'request_id':'r1','task_id':'a','stage':'notice',
            'summary':f'任务进度 {index}：' + '当前关卡已查到公开资料。'*7})

    def test_managed_task_refresh_preserves_reading_position(self):
        state, bar = self._show_long_task_timeline()
        bar.setValue(38)
        self._append_task_progress(18)
        self.panel.set_management_state(state)
        self.app.processEvents()
        self.assertEqual(bar.value(), 38)
        self.assertIn('任务进度 18', self.panel.timeline.toPlainText())

    def test_managed_task_at_end_follows_later_events(self):
        _, bar = self._show_long_task_timeline()
        old_end = bar.maximum()
        bar.setValue(old_end)
        self._append_task_progress(18)
        self.app.processEvents()
        self.assertGreater(bar.maximum(), old_end)
        self.assertEqual(bar.value(), bar.maximum())

    def test_background_task_progress_does_not_move_selected_history(self):
        state, bar = self._show_long_task_timeline()
        state['tasks'][0]['status'] = 'completed'
        state['tasks'].insert(0, {'id':'live','goal':'后台的新任务','status':'running'})
        state['requests'].append({'request_id':'live-request','task_id':'live','text':'新的要求'})
        self.panel.begin_request('live-request', '新的要求')
        self.panel.set_task(state['tasks'][0])
        self.panel.set_management_state(state)
        self.app.processEvents()
        bar.setValue(38)
        self.panel.append_event({'request_id':'live-request','task_id':'live','stage':'notice',
            'summary':'新任务的后台进度'})
        self.panel.set_management_state(state)
        self.app.processEvents()
        self.assertEqual(self.panel.selected_task_id,'a')
        self.assertEqual(bar.value(),38)
        self.assertNotIn('新任务的后台进度',self.panel.timeline.toPlainText())

    def test_empty_recycle_does_not_display_new_live_request(self):
        state=self.state(); state['trashed']=[]
        self.panel.set_management_state(state)
        self.panel.recycle_button.click()
        self.panel.begin_request('new', '新的普通聊天')
        self.assertIsNone(self.panel.selected_request_id)
        self.assertIn('回收箱为空', self.panel.goal.text())

    def test_switching_task_starts_at_top_and_invalidates_old_scroll_restore(self):
        state, bar = self._show_long_task_timeline()
        bar.setValue(bar.maximum())
        self._append_task_progress(18)
        state['tasks'].append({'id':'other','goal':'另一条任务','status':'paused'})
        state['requests'].append({'request_id':'r-other','task_id':'other','text':'另一条任务'})
        self.panel.begin_request('r-other','另一条任务')
        self.panel.set_management_state(state)
        self.panel.select_task('other')
        self.app.processEvents()
        self.assertEqual(self.panel.selected_task_id,'other')
        self.assertEqual(bar.value(),0)
        self.assertNotIn('任务进度 18',self.panel.timeline.toPlainText())

    def test_same_task_refresh_keeps_selected_text(self):
        _, bar = self._show_long_task_timeline()
        cursor = self.panel.timeline.textCursor()
        cursor.setPosition(1)
        cursor.setPosition(8, cursor.MoveMode.KeepAnchor)
        self.panel.timeline.setTextCursor(cursor)
        selected = self.panel.timeline.textCursor().selectedText()
        bar.setValue(38)
        self._append_task_progress(18)
        self.app.processEvents()
        self.assertEqual(self.panel.timeline.textCursor().selectedText(),selected)
        self.assertEqual(bar.value(),38)

    def test_selected_task_retains_evidenced_progress_beyond_recent_index(self):
        self.panel.configure_history(Path(self.temp.name)/'history.json')
        self.panel.begin_request('r1','首次要求')
        self.panel._history.bind_request('r1','a')
        self.panel.append_event({'request_id':'r1','task_id':'a','stage':'tool_started','summary':'旧任务工具证据'})
        for i in range(100): self.panel.begin_request('new'+str(i),'其他消息')
        self.panel.set_management_state(self.state())
        self.panel.select_task('a')
        self.assertIn('旧任务工具证据',self.panel.timeline.toPlainText())
        self.assertTrue(self.panel.append_event({'request_id':'r1','task_id':'a','stage':'tool_finished','summary':'归档请求晚到结果'}))
        self.assertIn('归档请求晚到结果',self.panel.timeline.toPlainText())
        self.assertFalse(self.panel.begin_request('r1','过期的重复输入'))
        self.assertEqual(self.panel.current_request_id,'new99')

    def test_running_delete_requires_explicit_confirmation_and_id(self):
        self.panel.set_management_state(self.state('running'))
        self.panel.select_task('a')
        spy = QSignalSpy(self.panel.trash_requested)
        self.panel.delete_button.click()
        self.assertEqual(spy.count(), 0)
        self.assertIn('孤星导入', self.panel.confirm_label.text())
        self.panel.confirm_button.click()
        self.assertEqual(spy.at(0), ['a'])

    def test_recycle_restore_never_continues_and_purge_requires_confirmation(self):
        self.panel.set_management_state(self.state())
        self.panel.recycle_button.click()
        self.panel.select_task('b')
        restore = QSignalSpy(self.panel.restore_requested)
        purge = QSignalSpy(self.panel.purge_requested)
        self.assertTrue(self.panel.stop_button.isHidden())
        self.assertTrue(self.panel.delete_button.isHidden())
        self.panel.restore_button.click()
        self.assertEqual(restore.at(0), ['b'])
        self.panel.purge_button.click()
        self.assertEqual(purge.count(), 0)
        self.panel.confirm_button.click()
        self.assertEqual(purge.at(0), ['b'])

    def test_reopen_preserves_position_and_size_and_persists_separately(self):
        path = Path(self.temp.name) / 'activity-window.json'
        self.panel.configure_geometry(path)
        self.panel.show_at(QRect(600, 400, 50, 50))
        self.panel.move(70, 65)
        self.panel.resize(480, 390)
        self.panel.hide()
        self.panel.show_at(QRect(800, 700, 20, 20))
        self.assertEqual(self.panel.pos(), QPoint(70, 65))
        self.assertEqual(self.panel.width(), 480)
        self.panel.hide()
        second = ActivityPanel()
        self.addCleanup(second.deleteLater)
        self.addCleanup(second.hide)
        second.configure_geometry(path)
        second.show_at(QRect(800, 700, 20, 20))
        self.assertEqual(second.pos(), QPoint(70, 65))

    def test_title_drag_ignores_buttons_and_body(self):
        self.panel.show_at(QPoint(600, 400))
        start = self.panel.pos()
        QTest.mousePress(self.panel.timeline.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(5,5))
        QTest.mouseMove(self.panel.timeline.viewport(), QPoint(30,30))
        QTest.mouseRelease(self.panel.timeline.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(30,30))
        self.assertEqual(self.panel.pos(), start)

    def test_title_drag_uses_global_coordinates_and_clamps_offscreen_restore(self):
        self.panel.show_at(QPoint(600,400))
        start=self.panel.pos()
        area=self.panel.title_area
        origin=area.mapToGlobal(QPoint(5,5))
        press=QMouseEvent(QEvent.Type.MouseButtonPress,QPointF(5,5),QPointF(origin),Qt.MouseButton.LeftButton,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        move=QMouseEvent(QEvent.Type.MouseMove,QPointF(25,25),QPointF(origin+QPoint(20,20)),Qt.MouseButton.NoButton,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(area,press)
        QApplication.sendEvent(area,move)
        self.assertEqual(self.panel.pos(),start+QPoint(20,20))
        self.panel.move(-20000,-20000)
        self.panel.show_at(QPoint(600,400))
        self.assertTrue(self.app.primaryScreen().availableGeometry().contains(self.panel.geometry()))

    def test_history_purge_removes_only_proven_task_and_fences_restart(self):
        path=Path(self.temp.name)/'history.json'
        history=ActivityHistory(path)
        history.begin('owned','同一标题')
        history.bind_request('owned','a')
        history.begin('legacy','同一标题')
        history.begin('other','其他任务')
        history.bind_request('other','b')
        history.delete_task('a')
        restored=ActivityHistory(path)
        self.assertIsNone(restored.get('owned'))
        self.assertIsNotNone(restored.get('legacy'))
        self.assertIsNotNone(restored.get('other'))
        self.assertIsNone(restored.begin('owned','迟到回调'))
        self.assertFalse(restored.append('owned',{'task_id':'a','stage':'task_started'}))

    def test_independent_cleanup_instance_fences_existing_history_writer(self):
        path=Path(self.temp.name)/'history.json'
        history=ActivityHistory(path)
        history.begin('owned','任务')
        history.bind_request('owned','a')
        ActivityHistory(path).delete_task('a')
        self.assertFalse(history.append('owned',{'task_id':'a','stage':'task_started'}))
        history.begin('unrelated','其他')
        self.assertIsNone(ActivityHistory(path).get('owned'))

    def test_question_and_purge_pending_retry_preserve_task_protection(self):
        state=self.state()
        state['tasks'][0]['question']={'prompt':'哪一个关卡？','status':'open'}
        self.panel.set_management_state(state)
        self.panel.select_task('a')
        target=QSignalSpy(self.panel.target_requested)
        self.assertIn('哪一个关卡',self.panel.question_card.text())
        self.panel.answer_button.click()
        self.assertEqual(target.at(0),['a'])
        state['trashed'][0]['lifecycle']='purge_pending'
        self.panel.set_management_state(state)
        self.panel.recycle_button.click()
        self.panel.select_task('b')
        self.assertFalse(self.panel.restore_button.isEnabled())
        self.assertTrue(self.panel.purge_button.isEnabled())
        self.assertEqual(self.panel.purge_button.text(), '重试清理')
        purge = QSignalSpy(self.panel.purge_requested)
        restore = QSignalSpy(self.panel.restore_requested)
        resume = QSignalSpy(self.panel.continue_requested)
        self.panel.restore_button.click()
        self.assertEqual(restore.count(), 0)
        self.panel.purge_button.click()
        self.assertEqual(purge.count(), 0)
        self.assertEqual(self.panel.selected_task_id, 'b')
        self.panel.confirm_button.click()
        self.assertEqual(purge.count(), 1)
        self.assertEqual(purge.at(0), ['b'])
        self.assertEqual(restore.count(), 0)
        self.assertEqual(resume.count(), 0)

    def test_selected_task_controls_emit_exact_id_and_recycle_hides_them(self):
        self.panel.set_management_state(self.state())
        self.panel.select_task('a')
        resume=QSignalSpy(self.panel.continue_requested)
        cancel=QSignalSpy(self.panel.cancel_requested)
        stop=QSignalSpy(self.panel.task_stop_requested)
        self.panel.continue_button.click()
        self.panel.cancel_task_button.click()
        self.panel.task_stop_button.click()
        self.assertEqual(resume.at(0),['a'])
        self.assertEqual(cancel.at(0),['a'])
        self.assertEqual(stop.at(0),['a'])
        self.panel.recycle_button.click()
        for button in (self.panel.continue_button,self.panel.cancel_task_button,self.panel.task_stop_button):
            self.assertTrue(button.isHidden())

    def permission_state(self, *, status='needs_input', reason='MAA 输入权限未开启', screen=True, inputs=False):
        state=self.state(status)
        state['tasks'][0]['blocked_reason']=reason
        state['permissions']={'screen_capture':screen,'maa_input':inputs}
        return state

    def test_maa_permission_action_binds_selected_blocked_task_without_plain_resume(self):
        self.panel.set_management_state(self.permission_state())
        self.panel.select_task('a')
        grant=QSignalSpy(self.panel.maa_permission_requested)
        resume=QSignalSpy(self.panel.continue_requested)
        self.assertFalse(self.panel.maa_permission_button.isHidden())
        self.assertFalse(self.panel.continue_button.isEnabled())
        self.assertIn('不会改变权限',self.panel.continue_button.toolTip())
        self.panel.continue_button.click()
        self.assertEqual(resume.count(),0)
        self.panel.maa_permission_button.click()
        self.assertEqual(grant.at(0),['a'])
        self.assertEqual(resume.count(),0)

    def test_shell_permission_click_reaches_selected_controller_task(self):
        controller=ManagementController(Path(self.temp.name))
        self.addCleanup(controller.engine.close)
        state=self.permission_state()
        controller.task_management_state=lambda:state
        def grant(task_id):
            controller.controls.append(('authorize',task_id))
            state['permissions']={'screen_capture':True,'maa_input':True}
            return {'accepted':True,'task_id':task_id}
        controller.authorize_maa_import=grant
        with patch.object(CompanionShell,'_setup_tray'):
            shell=CompanionShell(controller,Path(__file__).resolve().parents[1])
        self.addCleanup(shell.deleteLater)
        self.addCleanup(shell.quit_application)
        shell.activity.select_task('a')
        shell.activity.maa_permission_button.click()
        self.assertEqual(controller.controls,[('authorize','a')])
        self.assertEqual(controller.target,{'mode':'task','task_id':'a'})
        self.assertTrue(shell.activity.maa_permission_button.isHidden())

    def test_permission_action_hides_after_authorization_and_for_other_blockers(self):
        self.panel.set_management_state(self.permission_state())
        self.panel.select_task('a')
        for state in (self.permission_state(screen=True,inputs=True),
                      self.permission_state(reason='等待新的独立截图'),
                      self.permission_state(status='running'),
                      self.permission_state(status='completed')):
            with self.subTest(state=state):
                self.panel.set_management_state(state)
                self.assertTrue(self.panel.maa_permission_button.isHidden())
                self.assertTrue(self.panel.continue_button.isEnabled())

    def test_permission_action_requires_explicit_typed_permission_snapshot(self):
        for permissions in (None,{}, {'screen_capture':True},
                            {'screen_capture':True,'maa_input':0}):
            state=self.permission_state();state['permissions']=permissions
            self.panel.set_management_state(state)
            self.panel.select_task('a')
            self.assertTrue(self.panel.maa_permission_button.isHidden())
        state=self.permission_state(screen=False,inputs=True,reason='尚未允许读取 MAA 画面')
        self.panel.set_management_state(state)
        self.assertFalse(self.panel.maa_permission_button.isHidden())

    def test_permission_action_never_grants_for_recycle_or_changed_selection(self):
        state=self.permission_state()
        state['tasks'].append({'id':'other','goal':'其他等待','status':'needs_input','blocked_reason':'其他原因'})
        state['trashed'][0].update(status='needs_input',blocked_reason='MAA 输入权限未开启')
        self.panel.set_management_state(state)
        self.panel.select_task('a')
        grant=QSignalSpy(self.panel.maa_permission_requested)
        self.panel.select_task('other')
        self.panel._request_maa_permission()
        self.assertEqual(grant.count(),0)
        self.panel.recycle_button.click()
        self.panel.select_task('b')
        self.assertTrue(self.panel.maa_permission_button.isHidden())
        self.panel._request_maa_permission()
        self.assertEqual(grant.count(),0)

    def test_shell_target_browsing_and_api_failure_never_fake_deletes(self):
        controller=ManagementController(Path(self.temp.name))
        self.addCleanup(controller.engine.close)
        with patch.object(CompanionShell,'_setup_tray'):
            shell=CompanionShell(controller,Path(__file__).resolve().parents[1])
        self.addCleanup(shell.deleteLater)
        self.addCleanup(shell.quit_application)
        shell.activity.select_task('a')
        self.assertEqual(controller.target,{'mode':'auto','task_id':None})
        shell.activity.answer_button.click()
        self.assertEqual(controller.target,{'mode':'task','task_id':'a'})
        shell.new_task_button.click()
        self.assertEqual(controller.target,{'mode':'new','task_id':None})
        shell.activity.continue_button.click()
        self.assertEqual(controller.controls,[('continue','a')])
        shell.activity.delete_button.click()
        self.assertEqual(controller.controls[-1],('trash','a'))
        self.assertEqual(shell.activity.history_list.count(),1)
        self.assertIn('等待核对',shell.activity.history_hint.text())
        controller.message_routed.emit({'request_id':'r','task_id':'a','relation':'modify','summary':'已补充'})
        self.assertIn('#a',shell.route_label.text())

    def test_contextual_replies_and_errors_never_finish_the_newest_input(self):
        controller=ManagementController(Path(self.temp.name))
        self.addCleanup(controller.engine.close)
        with patch.object(CompanionShell,'_setup_tray'):
            shell=CompanionShell(controller,Path(__file__).resolve().parents[1])
        self.addCleanup(shell.deleteLater)
        self.addCleanup(shell.quit_application)
        controller.request_started.emit('first','第一条')
        controller.request_started.emit('second','第二条')
        controller.busy_changed.emit(False)
        self.app.processEvents()
        self.assertEqual(shell.activity._history.get('second')['status'],'running')
        controller.context_message.emit({'role':'assistant','text':'第一条的回复','request_id':'first','task_id':None})
        self.assertEqual(shell.activity._history.get('first')['summary'],'第一条的回复')
        self.assertEqual(shell.activity._history.get('first')['status'],'understood')
        self.assertEqual(shell.activity._history.get('second')['status'],'running')
        controller.error.emit('语音服务暂时失败')
        self.assertEqual(shell.activity._history.get('second')['status'],'running')
        controller.activity_progress.emit({'stage':'intent_failed','request_id':'second','status':'failed','summary':'第二条失败'})
        controller.context_message.emit({'role':'assistant','text':'迟到的第一条回复','request_id':'first','task_id':None})
        self.assertEqual(shell.activity._history.get('second')['summary'],'第二条失败')
        self.assertEqual(shell.activity._history.get('second')['status'],'failed')

    def test_real_controller_queued_chat_finishes_each_original_request(self):
        from shu.controller import SessionController
        from tests.test_leader_controller import Model, route
        environment=patch.dict(os.environ,{'DEEPSEEK_API_KEY':''})
        environment.start(); self.addCleanup(environment.stop)
        directory=Path(self.temp.name)/'actual-controller'
        controller=SessionController(directory,directory/'data')
        self.addCleanup(controller.close)
        controller.key_path.write_text('sk-offline-ui-fixture')
        errors=[]; controller.error.connect(errors.append)
        entered_first,release_first,entered_second,release_second=[threading.Event() for _ in range(4)]
        class SlowModel(Model):
            def complete(model,messages,**options):
                entered,release=(entered_second,release_second) if model.requests else (entered_first,release_first)
                entered.set(); release.wait(4)
                return super().complete(messages,**options)
        controller.client=controller.intent_client=SlowModel(route('第一条','',intent='chat',reply='第一条的回复'),
                                    route('第二条','',intent='chat',reply='第二条的回复'))
        with patch.object(CompanionShell,'_setup_tray'):
            shell=CompanionShell(controller,Path(__file__).resolve().parents[1])
        self.addCleanup(shell.deleteLater); self.addCleanup(shell.quit_application)
        self.addCleanup(release_first.set); self.addCleanup(release_second.set)
        def wait_for(predicate):
            deadline=time.monotonic()+3
            while not predicate() and time.monotonic()<deadline:
                self.app.processEvents(); time.sleep(.004)
            self.assertTrue(predicate(),'queued chat did not progress')
        controller.submit('第一条'); wait_for(entered_first.is_set)
        controller.submit('第二条')
        first,second=[row['request_id'] for row in controller.dialogue.management.messages()]
        release_first.set(); wait_for(entered_second.is_set)
        self.assertEqual(shell.activity._history.get(first)['summary'],'第一条的回复')
        self.assertEqual(shell.activity._history.get(first)['status'],'understood')
        self.assertEqual(shell.activity._history.get(second)['status'],'running')
        release_second.set(); wait_for(lambda:not controller.is_busy)
        self.app.processEvents()
        self.assertEqual(shell.activity._history.get(second)['summary'],'第二条的回复')
        self.assertEqual(shell.activity._history.get(second)['status'],'understood')
        self.assertEqual(errors,[])


if __name__ == '__main__':
    unittest.main()

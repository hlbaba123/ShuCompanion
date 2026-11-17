"""Pet-only entry integration, with no paid model or real desktop operations."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import Mock, PropertyMock, patch
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from shu.companion_ui import CompanionShell as MainWindow
from shu.core import TaskEngine
from tests.ui_fixtures import FakeController

ROOT = Path(__file__).resolve().parents[1]

class CompanionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not isinstance(cls.app, QApplication):
            raise unittest.SkipTest('Pet UI requires a separate GUI process')
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.controller = FakeController(Path(self.temp.name))
        self.tray = patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=False)
        self.tray.start()
        self.window = MainWindow(self.controller, ROOT)

    def tearDown(self):
        self.window.quit_application()
        self.window.deleteLater(); self.app.processEvents()
        self.controller.engine.close(); self.tray.stop(); self.temp.cleanup()

    def test_startup_has_pet_and_no_workbench_or_automatic_panels(self):
        self.window.show_pet(); self.app.processEvents()
        self.assertTrue(self.window.pet.isVisible())
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.window.activity.isVisible())
        self.assertIsNone(self.window._settings_dialog)
        self.assertFalse(hasattr(self.window, 'stack'))
        self.assertFalse(hasattr(self.window, 'hero'))

    def test_menu_exposes_task_progress_and_has_no_old_workbench(self):
        texts=[a.text() for a in self.window.pet.menu.actions()]
        self.assertIn('任务进度', texts)
        self.assertFalse(any('工作台' in t or '轨迹' in t for t in texts))

    def test_pet_input_uses_real_automatic_chat_entry(self):
        with patch.object(self.window.speech_output, 'stop') as stop:
            self.window.pet.text_submitted.emit('查一下现在的游戏活动')
        self.assertEqual(self.controller.submissions, [('查一下现在的游戏活动', 0)])
        stop.assert_called_once()

    def test_progress_is_on_demand_and_does_not_submit_a_task(self):
        self.window.open_activity(); self.app.processEvents()
        self.assertTrue(self.window.activity.isVisible())
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.controller.submissions, [])
        self.window.activity.close(); QTest.qWait(160); self.app.processEvents()
        self.assertFalse(self.window.activity.isVisible())
        self.assertEqual(self.controller.stops, 0)

    def test_network_progress_survives_hidden_panel_and_can_be_reviewed(self):
        self.window.on_network_progress({'stage':'tool_started','tool':'web_search',
            'query':'明日方舟活动','status':'running','label':'研究Agent：正在联网检索'})
        self.assertFalse(self.window.activity.isVisible())
        self.assertEqual(self.window.activity.last_event['role'], 'research')
        self.assertEqual(self.window.activity.last_event['tool'], 'web_search')
        self.window.open_activity(); self.app.processEvents()
        self.assertTrue(self.window.activity.events)

    def test_error_is_logged_without_opening_a_large_window(self):
        self.controller.error.emit('资料服务请求受限，请稍后重试')
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.window.activity.isVisible())
        self.assertTrue(any(e.get('status') == 'failed' for e in self.window.activity.events))

    def test_settings_stays_independent_and_no_workbench_button(self):
        self.window.open_settings('general'); self.app.processEvents()
        settings=self.window._settings_dialog
        self.assertTrue(settings.isVisible())
        from PySide6.QtWidgets import QPushButton
        self.assertFalse(any('工作台' in w.text() for w in settings.findChildren(QPushButton)))
        self.assertFalse(self.window.activity.isVisible())

    def test_capture_permission_is_not_enabled_by_observe_button(self):
        self.window.capture_from_pet()
        self.assertEqual(self.controller.captures, 0)
        self.assertFalse(self.controller.screen_capture_allowed)
        self.assertTrue(self.window._settings_dialog.isVisible())

    def test_background_capture_keeps_pet_visible_and_does_not_open_panel(self):
        self.controller.windows.window=SimpleNamespace(hwnd=1,title='明日方舟')
        self.controller.screen_capture_allowed=True
        self.window.show_pet(); self.window.capture_from_pet()
        self.assertEqual(self.controller.captures, 1)
        self.assertTrue(self.window.pet.isVisible())
        self.assertFalse(self.window.activity.isVisible())
        self.controller.is_busy=False; self.controller.busy_changed.emit(False)

    def test_observation_is_real_preview_and_not_fake_task_completion(self):
        path=Path(self.temp.name)/'frame.png'
        image=QImage(80,40,QImage.Format.Format_RGB32); image.fill(QColor('#596e89')); image.save(str(path))
        observation={'id':'read-only','path':str(path),'width':80,'height':40,'method':'fixture'}
        self.controller.observation_ready.emit(observation)
        self.assertFalse(self.window.preview.original.isNull())
        self.assertEqual(self.window._displayed_observation['id'], 'read-only')
        self.assertFalse(self.window.preview_dialog.isVisible())
        self.assertEqual(self.controller.submissions, [])

    def test_stop_remains_immediate_and_cancels_speech_first(self):
        order=[]
        with (patch.object(self.window.speech_output,'stop',side_effect=lambda:order.append('speech')),
              patch.object(self.controller,'stop',side_effect=lambda:order.append('task'))):
            self.window.activity.stop_requested.emit()
        self.assertEqual(order, ['speech','task'])

    def test_local_query_stop_does_not_leave_a_running_progress_heading(self):
        self.window.on_network_progress({'stage':'tool_started','tool':'web_search',
            'status':'running','query':'公开资料'})
        self.window.stop()
        self.assertEqual(self.window.activity.status.text(),'已停止')

    def test_stop_waiting_for_maa_does_not_claim_maa_already_stopped(self):
        self.controller.execution=SimpleNamespace(active=True)
        self.window.stop()
        self.assertEqual(self.window.activity.status.text(),'等待处理')
        self.assertIn('等待 MAA',self.window.activity.summary.text())

    def test_speech_caption_keeps_playback_driven_behavior(self):
        self.window.speech_output.set_enabled(True); self.window.show_pet()
        with (patch.object(type(self.window.speech_output),'ready',new_callable=PropertyMock,return_value=True),
              patch.object(self.window.speech_output,'_request')):
            self.controller.message.emit('assistant','准备播放的完整回答')
        self.assertNotEqual(self.window.pet.bubble.text.toPlainText(),'准备播放的完整回答')
        self.window.speech_output.caption.emit('准备')
        self.assertEqual(self.window.pet.bubble.text.toPlainText(),'准备')
        self.window.speech_output.playback_cancelled.emit()
        self.assertFalse(self.window.pet.bubble.isVisible())

    def test_close_preserves_task_and_does_not_call_explicit_stop(self):
        self.window.quit_application()
        self.assertEqual(self.controller.stops, 0)
        self.assertEqual(self.controller.closes, 1)

    def test_application_exit_closes_progress_immediately_without_collapse_timer(self):
        self.window.open_activity(); self.app.processEvents()
        self.assertTrue(self.window.activity.isVisible())
        self.window.quit_application()
        self.assertFalse(self.window.activity.isVisible())

    def test_cancelled_admin_restart_keeps_current_pet_usable(self):
        launch=Mock(return_value=5)
        with patch('shu.companion_ui.ctypes.WinDLL',return_value=SimpleNamespace(ShellExecuteW=launch)):
            self.window.restart_elevated()
        self.assertFalse(self.window._exiting)
        self.assertEqual(self.controller.pauses, 1)
        self.assertEqual(self.controller.stops, 0)

    def test_skin_preference_is_preserved_in_shared_data_path(self):
        from shu.skins import save_skin_preference
        path=self.controller.data_dir/'pet-appearance.json'
        save_skin_preference(path,'purple')
        self.window.quit_application(); self.window.deleteLater(); self.app.processEvents()
        self.window=MainWindow(self.controller,ROOT)
        self.assertEqual(self.window.pet.canvas.skin_id,'purple')
        self.assertEqual(self.window.pet._appearance_path,path)

    def test_voice_recording_cancels_speech_and_does_not_open_system_dictation(self):
        with (patch.object(type(self.window.voice), 'is_recording', new_callable=PropertyMock, return_value=False),
              patch.object(self.window.speech_output,'stop') as stop,
              patch.object(self.window.voice,'start') as start):
            self.window.toggle_voice()
        stop.assert_called_once(); start.assert_called_once()

    def test_settings_saves_models_without_old_workbench_fields(self):
        self.controller._validate_settings=Mock()
        self.controller.save_settings=Mock()
        self.window.open_settings('model')
        settings=self.window._settings_dialog
        settings.role_model.setText('test-model'); settings.save()
        self.controller.save_settings.assert_called_once_with('test-model','https://example.invalid',
            game_model='test-model',game_thinking=True)
        self.assertIn('已保存',settings.feedback.text())

    def test_language_setting_updates_response_and_voice(self):
        self.window.open_settings('general'); settings=self.window._settings_dialog
        with patch.object(self.window.speech_output,'set_language') as changed:
            settings.speech_language.setCurrentIndex(settings.speech_language.findData('ja'))
        changed.assert_called_once_with('ja')
        self.assertEqual(self.controller.response_language,'ja')

    def test_real_structured_event_signal_reaches_panel_without_opening_it(self):
        self.controller.activity_progress.emit({'stage':'dispatch_started','role':'maa',
            'status':'running','summary':'核对MAA窗口状态','task_id':'fixture','epoch':1})
        self.assertEqual(self.window.activity.last_event['role'],'maa')
        self.assertFalse(self.window.activity.isVisible())

    def test_each_user_request_has_separate_persistent_progress_history(self):
        self.controller.message.emit('user', '查一下当前活动')
        first=self.window.activity.current_request_id
        self.controller.network_progress.emit({'stage':'tool_started','tool':'web_search',
            'query':'公开活动公告','status':'running'})
        self.controller.message.emit('assistant','查询结果已返回')
        self.controller.message.emit('user','刷六次，不用理智药')
        second=self.window.activity.current_request_id
        self.assertNotEqual(first, second)
        self.window.activity.select_request(first)
        self.assertIn('公开活动公告',self.window.activity.timeline.toPlainText())
        self.assertNotIn('刷六次',self.window.activity.goal.text())
        self.window.activity.select_request(second)
        self.assertIn('刷六次',self.window.activity.goal.text())
        self.assertTrue((self.controller.data_dir/'activity-history.json').is_file())

    def test_execution_events_stay_with_origin_request_during_another_chat(self):
        self.controller.message.emit('user','导入TL-10作业')
        origin=self.window.activity.current_request_id
        self.controller.activity_progress.emit({'stage':'task_started','role':'leader','status':'running',
            'task_id':'game-task','epoch':1,'summary':'开始处理导入要求'})
        self.controller.message.emit('user','你今天怎么样')
        chat=self.window.activity.current_request_id
        self.controller.activity_progress.emit({'stage':'tool_started','role':'maa','status':'running',
            'task_id':'game-task','epoch':1,'summary':'检查MAA窗口'})
        self.window.activity.select_request(chat)
        self.assertNotIn('检查MAA窗口',self.window.activity.timeline.toPlainText())
        self.window.activity.select_request(origin)
        self.assertIn('检查MAA窗口',self.window.activity.timeline.toPlainText())

    def test_followup_intent_with_previous_task_context_belongs_to_new_request(self):
        self.controller.message.emit('user','查询活动作业')
        origin=self.window.activity.current_request_id
        self.controller.activity_progress.emit({'stage':'task_started','role':'leader',
            'task_id':'lookup','epoch':1,'status':'running'})
        self.controller.activity_progress.emit({'stage':'task_waiting','role':'leader',
            'task_id':'lookup','epoch':2,'status':'needs_input','summary':'缺少完整JSON证据'})
        self.controller.message.emit('user','把刚才找到的代码给我')
        followup=self.window.activity.current_request_id
        for stage in ('intent_started','intent_finished'):
            self.controller.activity_progress.emit({'stage':stage,'role':'leader',
                'task_id':'lookup','epoch':2,'status':'running' if stage=='intent_started' else 'completed',
                'summary':'理解这次追问'})
        previous=self.window.activity._history.get(origin)
        current=self.window.activity._history.get(followup)
        self.assertEqual(previous['status'],'needs_input')
        self.assertEqual(previous['summary'],'缺少完整JSON证据')
        self.assertNotIn('理解这次追问',str(previous['events']))
        self.assertEqual([event['stage'] for event in current['events']],
            ['intent_started','intent_finished'])
        self.controller.activity_progress.emit({'stage':'tool_finished','role':'research',
            'task_id':'lookup','epoch':1,'tool':'research_fetch','summary':'迟到的原任务结果'})
        self.assertNotIn('迟到的原任务结果',str(self.window.activity._history.get(followup)['events']))

    def test_structured_progress_and_identical_notice_are_only_one_history_event(self):
        self.controller.message.emit('user','查询资料')
        self.controller.activity_progress.emit({'stage':'tool_started','role':'leader','tool':'knowledge',
            'status':'running','summary':'Leader：正在查询本地资料'})
        self.controller.notice.emit('Leader：正在查询本地资料')
        self.assertEqual(len(self.window.activity.events),1)


@unittest.skipIf(MainWindow is None, 'PySide6 is not installed')
class ExitTaskPersistenceTests(unittest.TestCase):
    """Exercise actual controller/engine/coordinator from the real UI exits."""
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        if not isinstance(cls.app,QApplication):
            raise unittest.SkipTest('Run UI tests in a separate GUI process')
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        from shu.controller import SessionController
        from shu.coordinator import ExecutionCoordinator
        self.temp=TemporaryDirectory();self.directory=Path(self.temp.name)
        self.environment=patch.dict(os.environ,{'DEEPSEEK_API_KEY':''});self.environment.start()
        self.tray=patch.object(QSystemTrayIcon,'isSystemTrayAvailable',return_value=False);self.tray.start()
        self.controller=SessionController(self.directory,self.directory/'data',execution_factory=ExecutionCoordinator)
        self.windows=SimpleNamespace(input_enabled=False)
        self.windows.window=SimpleNamespace(hwnd=101,pid=202,title='明日方舟',is_game_client=True,
            width=640,height=360,left=0,top=0,minimized=False)
        self.windows.bound_window=lambda:self.windows.window
        self.windows.discover_capabilities=lambda:{'window_enumeration':True}
        self.windows.set_stop=lambda:setattr(self.windows,'input_enabled',False)
        self.windows.reset_stop=lambda:setattr(self.windows,'input_enabled',False)
        self.windows.set_input_enabled=lambda value:setattr(self.windows,'input_enabled',bool(value))
        self.windows.require_input_permission=lambda:None
        self.controller.windows=self.windows;self.controller.execution.windows=self.windows
        self.controller.screen_capture_allowed=True;self.controller.game_input_allowed=True
        self.controller.execution.wiki=None;self.controller.execution.knowledge=None
        self.controller.execution.client_factory=lambda:SimpleNamespace()
        self.entered=threading.Event();self.runs=0;self.thread_errors=[]
        def executor(_windows,_decide,guard,emit_event=None,**kwargs):
            def run(goal,cancel_event=None,**options):
                guard();self.runs+=1
                context={'schema_version':1,'goal':goal,'executed_action_count':0,
                    'window_identity':{'window_id':101,'window_pid':202,'width':640,'height':360},
                    'model_state':{'workflow':[{'id':'collect','status':'pending'}]},'recent_steps':[]}
                emit_event('checkpoint','',{'game_context':context});self.entered.set()
                if not cancel_event.wait(4):raise AssertionError('UI exit must release the fake worker')
                return {'status':'cancelled','steps':1,'game_context':context}
            return SimpleNamespace(run=run)
        self.controller.execution.executor_factory=executor
        self.hook=patch('threading.excepthook',side_effect=lambda args:self.thread_errors.append(args.exc_value));self.hook.start()
        self.window=MainWindow(self.controller,Path(__file__).resolve().parents[1])
        result=self.controller.execution.submit('进入基建并收取产物',{'goal':'进入基建并收取产物','constraints':['不换班']})
        self.identifier=result['task_id'];self.controller._task_id=self.identifier
        self.wait_for(self.entered.is_set)

    def tearDown(self):
        self.window.quit_application();self.controller.close()
        self.window.deleteLater();self.app.processEvents()
        self.hook.stop();self.tray.stop();self.environment.stop();self.temp.cleanup()
        self.assertEqual(self.thread_errors,[])

    def wait_for(self,predicate):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            self.app.processEvents()
            if predicate():return
            time.sleep(.004)
        self.fail('UI persistence fixture did not finish in time')

    def test_quitting_real_controller_preserves_paused_task_and_durable_checkpoint(self):
        from shu.runtime import RuntimeStore
        with patch.object(self.controller,'stop',wraps=self.controller.stop) as stop:
            self.window.quit_application();self.controller.close()
            stop.assert_not_called()
        self.app.processEvents();self.assertFalse(self.windows.input_enabled)
        engine=TaskEngine(self.directory/'data');runtime=RuntimeStore(self.directory/'data')
        try:
            task=engine.get_task(self.identifier);saved=runtime.load(self.identifier)
            self.assertEqual(task['status'],'paused');self.assertEqual(len(engine.list_tasks()),1)
            self.assertEqual(saved['phase'],'paused')
            self.assertEqual(saved['contract']['constraints'],['不换班'])
            self.assertEqual(saved['context']['model_state']['workflow'][0]['id'],'collect')
            self.assertNotIn('task_cancelled',[event['type'] for event in engine.events(self.identifier)])
        finally:runtime.close();engine.close()

    def test_cancelled_uac_keeps_real_task_paused_and_controller_can_resume_same_task(self):
        launch=Mock(return_value=5);observed=[]
        def cancelled_launch(*args):
            observed.append((self.windows.input_enabled,self.controller.current_task()['status'],self.controller._closed))
            return 5
        launch.side_effect=cancelled_launch
        with (patch('shu.companion_ui.ctypes.WinDLL',return_value=SimpleNamespace(ShellExecuteW=launch)),
              patch.object(self.controller,'stop',wraps=self.controller.stop) as stop):
            self.window.restart_elevated();stop.assert_not_called()
        self.assertEqual(observed,[(False,'paused',False)])
        self.assertFalse(self.window._exiting);self.assertFalse(self.controller._closed)
        self.wait_for(lambda:not self.controller.execution.active)
        self.entered.clear();self.controller.resume();self.wait_for(self.entered.is_set)
        self.assertEqual(self.controller.current_task()['id'],self.identifier)
        self.assertEqual(self.controller.current_task()['status'],'running')
        self.assertEqual(len(self.controller.engine.list_tasks()),1)

    def test_successful_uac_closes_real_controller_with_resumable_same_task(self):
        with (patch('shu.companion_ui.ctypes.WinDLL',return_value=SimpleNamespace(ShellExecuteW=Mock(return_value=33))),
              patch.object(self.controller,'stop',wraps=self.controller.stop) as stop):
            self.window.restart_elevated();stop.assert_not_called()
        self.assertTrue(self.controller._closed);self.assertTrue(self.window._exiting)
        engine=TaskEngine(self.directory/'data')
        try:
            self.assertEqual(engine.get_task(self.identifier)['status'],'paused')
            self.assertEqual(len(engine.list_tasks()),1)
        finally:engine.close()



if __name__ == '__main__': unittest.main()

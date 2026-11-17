"""Task activity is evidence driven and never opens a window by itself."""
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QPoint, QRect, Qt, QUrl
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from shu.activity_ui import ActivityPanel


class ActivityPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not isinstance(cls.app, QApplication):
            raise unittest.SkipTest('Run activity panel checks in a separate GUI process')
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.panel = ActivityPanel()

    def tearDown(self):
        self.panel.hide()
        self.panel.deleteLater()
        self.app.processEvents()

    def test_receiving_real_progress_never_automatically_opens_panel(self):
        self.panel.set_task({'id': 't1', 'goal': '查找当前活动', 'status': 'running'})
        self.panel.set_active(True)
        self.panel.append_event({'role': 'research', 'stage': 'tool_started',
            'status': 'running', 'summary': '正在检索官方活动', 'tool': 'web_search',
            'query': '明日方舟 国服 活动', 'timestamp': '2026-11-06T21:00:00+08:00'})
        self.assertFalse(self.panel.isVisible())
        self.assertEqual(self.panel.agent.text(), '研究 Agent')
        self.assertIn('正在检索官方活动', self.panel.summary.text())
        self.assertIn('明日方舟 国服 活动', self.panel.timeline.toPlainText())
        self.assertTrue(self.panel.stop_button.isEnabled())

    def test_version_uses_running_application_metadata(self):
        previous=self.app.applicationVersion()
        self.addCleanup(self.app.setApplicationVersion,previous)
        self.app.setApplicationVersion('0.3.7-proof')
        panel=ActivityPanel()
        self.addCleanup(panel.deleteLater)
        self.assertEqual(panel.version_label.text(),'0.3.7-proof')
        self.assertIn('0.3.7-proof',panel.version_label.toolTip())

    def test_long_dispatch_title_is_shortened_with_full_tooltip_and_errors_visible(self):
        title='核对用户所要求的TL-1到TL-10公开作业与完整JSON来源及关卡对应关系'
        self.panel.append_event({'stage':'dispatch_started','role':'research',
            'dispatch_id':'long','step_title':title})
        self.panel.append_event({'stage':'tool_finished','role':'research','dispatch_id':'long',
            'tool':'research_download','status':'rejected','summary':'完整JSON缺少当前关卡证据'})
        self.panel.append_event({'stage':'dispatch_completed','role':'research',
            'dispatch_id':'long','status':'completed'})
        self.assertIn(title[:24]+'…',self.panel.timeline.toPlainText())
        found=self.panel.timeline.document().find(title[:24])
        self.assertIn(title,found.charFormat().toolTip())
        self.assertIn('完整JSON缺少当前关卡证据',self.panel.timeline.toPlainText())

    def test_missing_role_or_completion_evidence_does_not_invent_progress(self):
        self.panel.append_event({'stage': 'notice', 'summary': '收到状态通知'})
        self.assertEqual(self.panel.agent.text(), '角色未标明')
        self.assertNotIn('已完成', self.panel.status.text())
        self.assertNotIn('%', self.panel.timeline.toPlainText())
        self.assertNotIn('timestamp', self.panel.last_event)

    def test_only_display_fields_survive_and_text_is_not_interpreted_as_html(self):
        event = {'role': 'maa', 'stage': 'dispatch_started', 'summary': '<b>不是指令</b>',
            'reasoning_content': 'hidden-reasoning-fixture', 'api_key': 'secret-fixture',
            'result': {'private': 'private-result-fixture'}, 'task_id': 't1', 'epoch': 2}
        self.panel.append_event(event)
        text = self.panel.timeline.toPlainText()
        self.assertIn('<b>不是指令</b>', text)
        self.assertNotIn('hidden-reasoning-fixture', str(self.panel.events))
        self.assertNotIn('private-result-fixture', self.panel.timeline.toHtml())
        self.assertEqual(self.panel.summary.textFormat(), Qt.TextFormat.PlainText)

    def test_sources_are_clickable_public_urls_but_not_executable_schemes(self):
        url = 'https://ak.hypergryph.com/news/fixture?a=1&b=2'
        self.panel.append_event({'role': 'research', 'stage': 'tool_finished', 'status': 'completed',
            'summary': '读取了公告', 'url': url})
        self.assertIn('href="https://ak.hypergryph.com/news/fixture?a=1&amp;b=2"',
                      self.panel.timeline.toHtml())
        self.panel.append_event({'stage': 'notice', 'url': 'file:///C:/secret.txt'})
        self.panel.append_event({'stage': 'notice', 'url': 'javascript:alert(1)'})
        self.assertNotIn('file:', self.panel.timeline.toHtml())
        self.assertNotIn('javascript:', self.panel.timeline.toHtml())

    def test_credential_like_query_and_url_are_not_displayed_or_opened(self):
        self.panel.append_event({'role': 'research', 'stage': 'tool_started',
            'query': '查 sk-fixture-private-token',
            'url': 'https://ak.hypergryph.com/?api_key=sk-fixture-private-token',
            'summary': 'Bearer opaque-private-token'})
        visible = self.panel.timeline.toPlainText() + str(self.panel.events)
        self.assertNotIn('sk-fixture-private-token', visible)
        self.assertNotIn('opaque-private-token', visible)
        self.assertNotIn('api_key=', visible)

    def test_session_timeline_is_bounded_to_two_hundred_events(self):
        for index in range(205):
            self.panel.append_event({'stage': 'notice', 'summary': f'事件 {index}'})
        self.assertEqual(len(self.panel.events), 200)
        self.assertEqual(self.panel.events[0]['summary'], '事件 5')
        self.assertEqual(self.panel.last_event['summary'], '事件 204')

    def test_close_hides_with_short_animation_and_reopen_preserves_timeline(self):
        self.panel.append_event({'stage': 'notice', 'summary': '保留这条记录'})
        self.panel.show_at(QPoint(800, 500))
        QTest.qWait(180)
        self.assertTrue(self.panel.isVisible())
        self.panel.close()
        QTest.qWait(160)
        self.assertFalse(self.panel.isVisible())
        self.panel.show_at(QPoint(800, 500))
        self.assertIn('保留这条记录', self.panel.timeline.toPlainText())
        self.assertFalse(self.panel.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose))

    def test_show_uses_available_screen_bounds_on_a_narrow_screen(self):
        available = QRect(40, 50, 360, 420)
        with patch.object(self.panel, '_available_geometry', return_value=available):
            self.panel.show_at(QRect(350, 400, 90, 100))
        self.assertTrue(available.contains(self.panel.geometry()))

    def test_errors_are_visible_without_opening_panel_and_stop_is_a_signal(self):
        self.panel.set_error('检索服务返回 HTTP 429')
        self.assertFalse(self.panel.isVisible())
        self.assertEqual(self.panel.status.text(), '出错')
        self.assertIn('HTTP 429', self.panel.timeline.toPlainText())
        self.panel.set_busy(True)
        spy = QSignalSpy(self.panel.stop_requested)
        self.panel.stop_button.click()
        self.assertEqual(spy.count(), 1)

    def test_dispatch_return_is_not_task_completion(self):
        self.panel.set_active(True)
        self.panel.append_event({'role': 'research', 'stage': 'dispatch_completed',
            'summary': '候选列表已返回', 'status': 'completed'})
        self.assertEqual(self.panel.status.text(), '已返回结果')
        self.assertNotEqual(self.panel.status.text(), '任务已完成')
        self.assertTrue(self.panel.stop_button.isEnabled())

    def test_auxiliary_notice_does_not_replace_agent_step_or_error_state(self):
        self.panel.append_event({'role': 'maa', 'stage': 'tool_started',
            'status': 'running', 'summary': '正在核对 MAA 作业输入框'})
        self.panel.set_notice('连接状态已更新')
        self.assertEqual(self.panel.agent.text(), 'MAA Agent')
        self.assertEqual(self.panel.summary.text(), '正在核对 MAA 作业输入框')
        self.assertEqual(self.panel.status.text(), '处理中')
        self.panel.set_error('窗口连接失败')
        self.panel.set_notice('本地设置已保存')
        self.assertEqual(self.panel.status.text(), '出错')
        self.assertEqual(self.panel.summary.text(), '窗口连接失败')
        self.assertIn('本地设置已保存', self.panel.timeline.toPlainText())

    def test_repeated_identical_auxiliary_notice_is_not_added_twice(self):
        self.panel.set_notice('本地设置已保存')
        self.panel.set_notice('本地设置已保存')
        self.assertEqual(len(self.panel.events), 1)

    def test_old_task_refresh_cannot_hide_a_more_recent_request_error(self):
        old = {'id':'finished-task', 'goal':'上次查询', 'status':'completed'}
        self.panel.set_task(old)
        self.panel.set_error('本次资料请求失败')
        self.panel.set_task(old)
        self.assertEqual(self.panel.status.text(), '出错')
        self.assertEqual(self.panel.summary.text(), '本次资料请求失败')
        self.panel.set_task({'id':'new-task', 'goal':'新的查询', 'status':'draft'})
        self.assertEqual(self.panel.status.text(), '等待处理')

    def test_intent_wait_pause_stop_and_observation_stages_have_factual_labels(self):
        expected = {
            'intent_started': '处理中', 'intent_finished': '已理解要求',
            'intent_failed': '出错', 'task_waiting': '等待处理',
            'task_paused': '已暂停', 'task_stopped': '已停止',
            'observation': '已获取画面', 'observation_analysis': '已返回分析',
        }
        for stage, status in expected.items():
            with self.subTest(stage=stage):
                self.panel.append_event({'role': 'leader', 'stage': stage, 'summary': '实际返回的状态'})
                self.assertEqual(self.panel.status.text(), status)
                self.assertNotIn(stage, self.panel.timeline.toPlainText())
        self.panel.append_event({'role': 'leader', 'stage': 'task_waiting'})
        self.assertNotEqual(self.panel.status.text(), '已结束')

    def test_reading_earlier_rows_does_not_jump_to_bottom_when_new_event_arrives(self):
        self.panel.show_at(QPoint(800, 500))
        for index in range(20):
            self.panel.append_event({'stage': 'notice', 'summary': f'记录 {index}：' + '详细事实。'*8})
        self.app.processEvents()
        bar = self.panel.timeline.verticalScrollBar()
        self.assertGreater(bar.maximum(), 30)
        bar.setValue(12)
        self.panel.append_event({'stage': 'notice', 'summary': '新的事件'})
        self.app.processEvents()
        self.assertEqual(bar.value(), 12)

    def test_request_history_lists_one_row_per_understood_request_and_reloads(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'requests.json'
            self.panel.configure_history(path)
            self.panel.begin_request('one','查当前活动')
            self.panel.append_event({'request_id':'one','role':'research','stage':'tool_started','summary':'查官方公告'})
            self.panel.finish_request('one','understood','资料已返回')
            self.panel.begin_request('two','刷六次，不使用理智药')
            self.panel.begin_request('two','不能新建第二条')
            self.assertEqual(self.panel.history_list.count(),2)
            self.assertEqual(self.panel.selected_request_id,'two')
            second=ActivityPanel()
            try:
                second.configure_history(path)
                self.assertEqual(second.history_list.count(),2)
                second.select_request('one')
                self.assertIn('查官方公告',second.timeline.toPlainText())
            finally: second.shutdown();second.deleteLater();self.app.processEvents()

    def test_reading_old_request_never_replays_or_steals_current_stop_target(self):
        self.panel.begin_request('old','旧要求')
        self.panel.append_event({'request_id':'old','role':'research','summary':'旧资料'})
        self.panel.begin_request('current','当前要求')
        self.panel.set_active(True)
        spy=QSignalSpy(self.panel.stop_requested)
        self.panel.select_request('old')
        self.panel.append_event({'request_id':'current','role':'maa','stage':'tool_started','summary':'当前MAA步骤'})
        self.assertEqual(self.panel.selected_request_id,'old')
        self.assertEqual(self.panel.current_request_id,'current')
        self.assertIn('旧资料',self.panel.timeline.toPlainText())
        self.assertNotIn('当前MAA步骤',self.panel.timeline.toPlainText())
        self.assertEqual(spy.count(),0)
        self.panel.stop_button.click();self.assertEqual(spy.count(),1)
        self.panel.select_request('current')
        self.assertIn('当前MAA步骤',self.panel.timeline.toPlainText())

    def test_narrow_screen_uses_history_selector_without_overflow(self):
        self.panel.begin_request('one','第一条要求')
        available=QRect(0,0,360,440)
        with patch.object(self.panel,'_available_geometry',return_value=available):
            self.panel.show_at(QPoint(330,400))
        self.app.processEvents()
        self.assertTrue(available.contains(self.panel.geometry()))
        self.assertTrue(self.panel.history_selector.isVisible())
        self.assertFalse(self.panel.history_sidebar.isVisible())

    def test_live_refresh_does_not_replace_selected_historical_goal(self):
        self.panel.begin_request('old','旧要求的完整原话')
        self.panel.begin_request('now','当前任务')
        self.panel.select_request('old')
        self.panel.set_task({'id':'t','goal':'后台新任务','status':'running'})
        self.assertEqual(self.panel.goal.text(),'旧要求的完整原话')

    def test_completed_dispatch_collapses_actual_tool_details_until_expanded(self):
        self.panel.append_event({'stage':'dispatch_started','role':'research',
            'dispatch_id':'lookup-1','summary':'研究公开作业','status':'running'})
        self.panel.append_event({'stage':'tool_started','role':'research','tool':'web_search',
            'dispatch_id':'lookup-1','query':'TL-6 完整作业查询词','status':'running'})
        self.panel.append_event({'stage':'tool_finished','role':'research','tool':'web_search',
            'dispatch_id':'lookup-1','summary':'目录已返回','status':'completed'})
        self.panel.append_event({'stage':'dispatch_completed','role':'research',
            'dispatch_id':'lookup-1','summary':'找到真实候选','status':'completed'})
        text=self.panel.timeline.toPlainText()
        self.assertIn('找到真实候选',text)
        self.assertNotIn('TL-6 完整作业查询词',text)
        import re
        match=re.search(r'href="(shu-step://toggle/[^"]+)"',self.panel.timeline.toHtml())
        self.assertIsNotNone(match,'actual dispatch needs an expandable timeline step')
        with patch('shu.activity_ui.QDesktopServices.openUrl') as opened:
            self.panel.timeline.anchorClicked.emit(QUrl(match.group(1)))
        self.assertIn('TL-6 完整作业查询词',self.panel.timeline.toPlainText())
        opened.assert_not_called()

    def test_real_role_return_and_redelegation_produce_separate_main_steps(self):
        for stage,role,summary in (
            ('intent_started','leader','解析要求'),
            ('intent_finished','leader','要求已理解'),
            ('dispatch_started','research','首次查询'),
            ('dispatch_completed','research','首次结果已返回'),
            ('dispatch_started','leader','核对已有结果'),
            ('dispatch_completed','leader','需要补充来源'),
            ('dispatch_started','research','补充来源')):
            self.panel.append_event({'stage':stage,'role':role,'summary':summary})
        html=self.panel.timeline.toHtml()
        import re
        self.assertEqual(len(set(re.findall(r'href="(shu-step://toggle/[^"]+)"',html))),4)
        self.assertIn('补充来源',self.panel.timeline.toPlainText())
        self.assertNotIn('MAA Agent',self.panel.timeline.toPlainText())

    def test_current_tool_pair_renders_one_child_with_original_query_and_final_result(self):
        self.panel.append_event({'stage':'dispatch_started','role':'research','summary':'查询资料'})
        self.panel.append_event({'stage':'tool_started','role':'research','tool':'web_fetch',
            'summary':'开始读取的临时文案','query':'需要保留的真实查询词',
            'url':'https://example.com/source','status':'running'})
        self.panel.append_event({'stage':'tool_finished','role':'research','tool':'web_fetch',
            'summary':'网页读取返回事实','status':'completed'})
        text=self.panel.timeline.toPlainText()
        self.assertNotIn('开始读取的临时文案',text)
        self.assertEqual(text.count('需要保留的真实查询词'),1)
        self.assertIn('网页读取返回事实',text)
        self.assertIn('https://example.com/source',self.panel.timeline.toHtml())

    def test_collapsed_completed_phase_keeps_failure_visible(self):
        self.panel.append_event({'stage':'dispatch_started','role':'research','summary':'查询资料'})
        self.panel.append_event({'stage':'tool_started','role':'research','tool':'web_fetch',
            'summary':'读取网页','status':'running'})
        self.panel.append_event({'stage':'tool_finished','role':'research','tool':'web_fetch',
            'summary':'公开网页返回 HTTP 429','status':'failed'})
        self.panel.append_event({'stage':'dispatch_completed','role':'research',
            'summary':'已有其他来源','status':'completed'})
        self.assertIn('HTTP 429',self.panel.timeline.toPlainText())
        self.assertIn('已有其他来源',self.panel.timeline.toPlainText())

    def test_no_events_does_not_show_a_scripted_future_workflow(self):
        self.panel.begin_request('empty','查询资料')
        self.assertEqual(self.panel.timeline.toPlainText(),'')
        self.assertNotIn('shu-step://',self.panel.timeline.toHtml())

    def test_expanding_finished_step_keeps_the_clicked_header_in_view(self):
        self.panel.append_event({'stage':'dispatch_started','role':'research','summary':'查询资料'})
        for index in range(30):
            self.panel.append_event({'stage':'tool_finished','role':'research','tool':'web_fetch',
                'summary':f'网页 {index} 返回了可核对的事实与公开来源。','status':'completed'})
        self.panel.append_event({'stage':'dispatch_completed','role':'research','status':'completed',
            'summary':'资料已返回'})
        self.panel.append_event({'stage':'dispatch_started','role':'leader','summary':'核对来源'})
        self.panel.show_at(QPoint(800,500));self.app.processEvents()
        bar=self.panel.timeline.verticalScrollBar();bar.setValue(0)
        import re
        href=re.search(r'href="(shu-step://toggle/[^"]+)"',self.panel.timeline.toHtml()).group(1)
        self.panel.timeline.anchorClicked.emit(QUrl(href));self.app.processEvents()
        self.assertGreater(bar.maximum(),100)
        self.assertEqual(bar.value(),0)

    def test_rejected_tool_remains_visible_when_completed_dispatch_is_collapsed(self):
        self.panel.append_event({'stage':'dispatch_started','role':'research','summary':'查询资料'})
        self.panel.append_event({'stage':'tool_finished','role':'research','tool':'research_catalog',
            'status':'rejected','summary':'查询已拒绝：本次预算已用尽'})
        self.panel.append_event({'stage':'dispatch_completed','role':'research',
            'status':'completed','summary':'保留已经取得的结果'})
        self.assertIn('本次预算已用尽',self.panel.timeline.toPlainText())
        self.assertEqual(self.panel.events[1]['status'],'rejected')

    def test_collapsed_step_uses_short_summary_and_expansion_keeps_full_public_result(self):
        full='公开目录摘要已核对。'*16+'最终公开结果。'
        self.panel.append_event({'stage':'dispatch_started','role':'research'})
        self.panel.append_event({'stage':'dispatch_completed','role':'research',
            'status':'completed','summary':full})
        self.assertNotIn('最终公开结果。',self.panel.timeline.toPlainText())
        self.assertEqual(self.panel.events[-1]['summary'],full)
        title=self.panel.timeline.document().find('研究 Agent')
        self.assertIn(full,title.charFormat().toolTip())
        import re
        href=re.search(r'href="(shu-step://toggle/[^"]+)"',self.panel.timeline.toHtml()).group(1)
        self.panel.timeline.anchorClicked.emit(QUrl(href))
        self.assertIn(full,self.panel.timeline.toPlainText())

    def test_final_summary_is_a_short_preview_with_full_tooltip(self):
        full='实际取得的公开资料摘要。'*30
        self.panel.begin_request('summary','查询公开资料')
        self.panel.finish_request('summary','research_verified',full)
        self.assertLessEqual(len(self.panel.summary.text()),65)
        self.assertEqual(self.panel.summary.toolTip(),full)

    def test_long_request_keeps_full_original_in_tooltip_and_short_preview(self):
        full='只读查询公开作业摘要，不导入客户端。'*15
        self.panel.begin_request('long-goal',full)
        self.assertLessEqual(len(self.panel.goal.text()),65)
        self.assertEqual(self.panel.goal.toolTip(),full)


if __name__ == '__main__':
    unittest.main()

import unittest
from types import SimpleNamespace
from shu.activity_events import make_activity_event
from shu.activity_timeline import project_timeline
from shu.companion_ui import CompanionShell

class ProgressIntegrationTests(unittest.TestCase):
    def test_request_identity_comes_from_controller(self):
        calls = []
        shell = SimpleNamespace(activity=SimpleNamespace(begin_request=lambda *args: calls.append(args)), _request_id=None)
        CompanionShell._on_request_started(shell, 'real-id', '查活动')
        self.assertEqual(shell._request_id, 'real-id')
        self.assertEqual(calls, [('real-id', '查活动')])

    def test_catalog_progress_survives_projection(self):
        event = make_activity_event('tool_finished', role='catalog', request_id='req', tool='catalog_structure', step_id='s1', stage_code='SN-1', group_label='普通', primary_count=1, backup_count=0, stop_reason='budget', plan_revision=2)
        self.assertIsNotNone(event)
        self.assertEqual(event['request_id'], 'req')
        step = project_timeline([event])[0]
        self.assertEqual(step.items[0].stage_code, 'SN-1')
        self.assertEqual(step.items[0].backup_count, 0)
        self.assertEqual(step.items[0].group_label, '普通')

    def test_progress_bounds(self):
        event = make_activity_event('tool_started', primary_count=-1, backup_count=True, plan_revision=1000001, stop_reason='x'*500)
        self.assertNotIn('primary_count', event)
        self.assertNotIn('backup_count', event)
        self.assertNotIn('plan_revision', event)
        self.assertLessEqual(len(event['stop_reason']), 120)

    def test_dispatch_boundary_progress_is_visible(self):
        event = make_activity_event('dispatch_completed', role='catalog', dispatch_id='d',
                                    stage_code='A-1', group_label='入口', backup_count=0)
        step = project_timeline([event])[0]
        self.assertEqual((step.items[0].stage_code, step.items[0].group_label, step.items[0].backup_count),
                         ('A-1', '入口', 0))

class RequestAttributionTests(unittest.TestCase):
    def test_background_task_maps_to_old_request_and_unknown_never_to_new(self):
        events=[]
        shell=SimpleNamespace(_last_activity_notice=None, _request_id='new', _task_requests={('old-task',1):'old'}, _task_last_request={'old-task':'old'}, _request_terminal=set(), activity=SimpleNamespace(append_event=lambda e: events.append(e), finish_request=lambda *a: None))
        CompanionShell.on_activity_progress(shell, {'task_id':'old-task','epoch':1,'stage':'tool_finished'})
        CompanionShell.on_activity_progress(shell, {'task_id':'unknown','epoch':1,'stage':'tool_started'})
        CompanionShell.on_activity_progress(shell, {'stage':'intent_started'})
        self.assertEqual(events[0]['request_id'],'old')
        self.assertNotIn('request_id',events[1])
        self.assertNotIn('request_id',events[2])

    def test_panel_does_not_attach_unidentified_event_to_current_request(self):
        from PySide6.QtWidgets import QApplication
        from shu.activity_ui import ActivityPanel
        app=QApplication.instance() or QApplication([])
        if not isinstance(app, QApplication):
            self.skipTest('Widget check runs in the separate GUI verification process')
        panel=ActivityPanel()
        panel.begin_request('new','最新聊天')
        panel.append_event({'stage':'tool_started','summary':'无归属'})
        self.assertEqual(panel._history.get('new')['events'], [])
        panel.close()

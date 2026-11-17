"""A real workflow delivers its own automatic-pause failure to chat."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from shu.controller import SessionController
from shu.maa_visual_native import MAA_EXECUTABLE
from tests.test_battle_ledger import png
from tests.test_maa_coordinator import APP, Roles, wait_for


class BattleMonitorReplyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.controller = SessionController(root, root/'data')
        self.addCleanup(self.controller.close)
        self.coordinator = self.controller.execution
        self.coordinator.permissions = lambda: (True, True)
        self.store = self.coordinator.store
        self.replies, self.finished, self.events = [], [], []
        self.controller.context_message.connect(self.replies.append)
        self.coordinator.finished.connect(self.finished.append)
        self.coordinator.event.connect(lambda kind, _, data: self.events.append((kind,data)))
        self.paused, self.release = threading.Event(), threading.Event()
        self.addCleanup(self.release.set)
        self.confirmed = True
        self.marker_change = {}
        self.only_prepared = False
        self.return_status = 'needs_input'
        self.coordinator.role_factory = self.roles

    def roles(self, store, directory, **kwargs):
        value = Roles(store, directory, **kwargs)
        value.handlers = {'maa': self.finish_after_pause}
        return value

    def finish_after_pause(self, context, cancel):
        task_id = context['task_id']
        self.dispatch_id = context['dispatch_id']
        identity = {'hwnd':1, 'pid':2, 'creation_time':3, 'executable':MAA_EXECUTABLE}
        start = self.store.prepare_operation(task_id, self.dispatch_id, 'fixture:battle:start',
                                             {'kind':'start', 'maa_identity':identity}, starts=True)
        operation_id = start['operation_id']
        if not self.only_prepared:
            self.store.issue_operation(operation_id)
            start = self.store.enter_submission(operation_id)
        self.start = start
        state = self.store.control(task_id, 'pause', expected_epoch=context['epoch'])
        if self.confirmed and not self.only_prepared:
            proof = self.idle_proof(start, state, identity)
            self.store.settle_battle_idle(operation_id, proof)
        self.paused.set()
        self.release.wait(15)
        return {'status':self.return_status, 'code':'battle_vision_invalid_output',
                'summary':'MAA 画面格式核验失败。',
                'battle_start_submitted':True, 'start_operation_id':operation_id,
                'external_idle_confirmed':self.confirmed,
                'automatic_pause_state':{**{key:state[key] for key in ('task_id','revision','epoch','control_seq')},
                    'from_epoch':context['epoch'], 'from_revision':context['revision'],
                    'dispatch_id':self.dispatch_id, 'start_operation_id':operation_id, **self.marker_change},
                'stop_result':{'external_stop_confirmed':self.confirmed,
                    'summary':'读回两次空闲。' if self.confirmed else '停止状态尚未核实。'}}

    def idle_proof(self, start, state, identity):
        root = self.controller.data_dir/'task-private'/state['task_id']
        root.mkdir(parents=True)
        def ref(name, data):
            path = root/name
            path.write_bytes(data)
            return {'path':str(path), 'sha256':hashlib.sha256(data).hexdigest()}

        def facts(name, value):
            return ref(name+'.json', json.dumps(value).encode('utf-8'))

        generation = {key:start[key] for key in ('task_id','revision','epoch')}
        correlation = {**generation, 'start_operation_id':start['operation_id'], 'maa_identity':identity}
        submitted = datetime.fromisoformat(start['submitted_at']).timestamp()
        observations = []
        for index in range(3):
            image = ref(f'frame-{index}.png', png())
            observations.append(facts(f'frame-{index}', {'schema':'maa_battle_observation/v1', **correlation,
                'captured_at':submitted+index*.2, 'running':index==0, 'idle':index!=0,
                'method':'win32_printwindow', 'screenshot_ref':image, 'image_sha256':image['sha256']}))
        return facts('verified-idle', {'schema':'maa_battle_idle/v1', **correlation,
            'start_payload_hash':start['payload_hash'], 'control_seq':state['control_seq'],
            'battle_start_submitted':True, 'external_idle_confirmed':True,
            'observed_running_ref':observations[0], 'idle_observation_refs':observations[1:]})

    def begin(self):
        request = '开始MAA自动战斗，运行当前已导入的单作业一次'
        outcome = self.coordinator.submit(request, {'goal':request, 'user_request':request,
            'request_id':'run-once-message', 'current_deliverable':'gameplay', 'task_spec':{'kind':'repeat_stage'}})
        self.task_id = outcome['task_id']
        wait_for(self.paused.is_set, timeout=10)

    def end(self):
        self.release.set()
        wait_for(lambda:not self.coordinator.active)
        APP.processEvents()
        return self.store.current(self.task_id)

    def test_failure_reaches_original_request_with_new_paused_epoch_and_verified_idle(self):
        self.begin()
        state = self.end()
        self.assertEqual(state['epoch'], self.start['epoch']+1)
        self.assertEqual(state['status'], 'paused')
        self.assertFalse(state['writes_enabled'])
        self.assertEqual(len(self.finished), 1)
        result = self.finished[0]
        self.assertEqual(result['status'], 'needs_input')
        self.assertFalse(result['goal_complete'])
        self.assertEqual(result['code'], 'battle_vision_invalid_output')
        self.assertEqual(result['epoch'], state['epoch'])
        self.assertTrue(result['external_idle_confirmed'])
        self.assertEqual(len(self.replies), 1)
        reply = self.replies[0]
        self.assertEqual((reply['request_id'], reply['task_id']), ('run-once-message', self.task_id))
        self.assertIn('MAA 画面格式核验失败', reply['text'])
        self.assertIn('空闲', reply['text'])
        self.assertEqual([kind for kind,_ in self.events if kind in {'task_waiting','task_finished'}], ['task_waiting'])

    def test_unconfirmed_stop_is_still_projected_as_failure_with_barrier_intact(self):
        self.confirmed = False
        self.begin()
        self.end()
        self.assertEqual(self.store.slot_state()['barrier_operation_id'], self.start['operation_id'])
        self.assertEqual(len(self.finished), 1)
        self.assertFalse(self.finished[0]['external_idle_confirmed'])
        self.assertIn('停止状态尚未核实', self.replies[0]['text'])

    def test_receipt_for_wrong_control_sequence_is_dropped(self):
        self.marker_change = {'control_seq':999}
        self.begin()
        self.end()
        self.assertFalse(self.replies)

    def test_prepared_without_submit_does_not_support_recovery_projection(self):
        self.only_prepared = True
        self.begin()
        self.end()
        self.assertFalse(self.replies)

    def test_late_receipt_cannot_report_against_a_newer_pause_epoch(self):
        self.begin()
        paused = self.store.control(self.task_id, 'pause')
        self.assertEqual(self.end()['epoch'], paused['epoch'])
        self.assertFalse(self.replies)

    def test_user_stop_wins_while_own_receipt_is_pending(self):
        self.begin()
        self.coordinator.stop(self.task_id)
        self.assertEqual(self.end()['status'], 'cancelled')
        self.assertFalse(self.replies)

    def test_late_success_with_automatic_pause_marker_does_not_finish_task(self):
        self.return_status = 'battle_finished'
        self.begin()
        self.assertEqual(self.end()['status'], 'paused')
        self.assertFalse(self.replies)


if __name__ == '__main__':
    unittest.main()

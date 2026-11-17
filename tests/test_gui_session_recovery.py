"""New tasks may retire an exited MAA session, never invent its import result.

Real SQLite lifecycle/operation fences; OS identity and supervisor transport are
controlled fixtures. No native GUI, model, network or production data is used.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct
import threading
import unittest
from unittest.mock import patch
import uuid
import zlib

from shu.core import ActionRejected
from shu.task_cleanup import TaskCleanup
from shu.workflow_store import _encode
from tests import test_maa_queue_store as store_fixtures


def png(width=16, height=16):
    def chunk(kind, payload):
        return (struct.pack('>I', len(payload)) + kind + payload
                + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\x00' + b'\xff' * (3 * width)) * height))
            + chunk(b'IEND', b''))


class ReadOnlySupervisor:
    def __init__(self, directory, identity):
        self.directory = Path(directory)
        self.target_identity = identity
        self.requests = []
        self.after_capture = None
        self.answer_status = 'completed'
        self.health_value = {
            'unknown': False, 'revoked': False, 'child_alive': True,
            'helper_exit_confirmed': False, 'pending': 0, 'stop_pending': 0,
            'closed': False,
        }

    def health(self):
        return copy.deepcopy(self.health_value)

    def request(self, command, **kwargs):
        self.requests.append(copy.deepcopy(command))
        if command != {'op': 'snapshot'}:
            raise AssertionError('Recovery must never send GUI input')
        identifier = uuid.uuid4().hex
        file = self.directory / 'maa-visual-captures' / (identifier + '.png')
        file.parent.mkdir(parents=True, exist_ok=True)
        raw = png()
        file.write_bytes(raw)
        frame = {
            'screenshot_id': identifier, 'image_path': str(file),
            'image_sha256': hashlib.sha256(raw).hexdigest(),
            'method': 'win32_printwindow',
            'window': {**self.target_identity,
                       'client_rect': {'x': 0, 'y': 0, 'width': 16, 'height': 16},
                       'minimized': False},
        }
        if self.after_capture:
            self.after_capture()
        return {'status': self.answer_status, 'result': frame}


class GuiSessionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = store_fixtures.QueueStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.workflow, self.engine = f.workflow, f.engine
        self.old_context = copy.deepcopy(f.context)
        self.old_process = copy.deepcopy(f.snapshot['binding']['process'])
        baseline = f.publish('input-baseline', {
            'schema': 'maa_queue_input_baseline/v1',
            'snapshot': f.snapshot, 'binding': f.snapshot['binding'],
            'target': f.plan['targets'][0],
        })
        self.operation = f.action(baseline)
        self.workflow.issue_operation(self.operation['operation_id'])
        stamp = datetime.now(timezone.utc).isoformat()
        with self.engine._transaction():
            # This fixture represents a crash AFTER durable input admission.
            # Full source/Box admission is tested in test_maa_queue_store.
            self.engine._db.execute(
                "UPDATE workflow_operations SET status='unknown',submitted_at=? WHERE operation_id=?",
                (stamp, self.operation['operation_id']))
        self.workflow.control(self.old_context['task_id'], 'pause')
        self.old_state = copy.deepcopy(self.workflow.current(self.old_context['task_id']))
        self.new_context = f.start_task()
        self.new_process = {**self.old_process, 'hwnd': 11, 'pid': 22, 'creation_time': 33}
        self.supervisor = ReadOnlySupervisor(f.root, self.new_process)
        self.cancel = threading.Event()
        self.inspected = []
        self.process_result = {'status': 'exited', 'pid': self.old_process['pid'],
                               'creation_time': self.old_process['creation_time'],
                               'executable': self.old_process['executable'],
                               'reason': 'pid_absent'}

    def inspect(self, identity):
        self.inspected.append(copy.deepcopy(identity))
        return copy.deepcopy(self.process_result)

    def recover(self, *, guard=lambda: None):
        from shu.maa_input_recovery import retire_exited_queue_inputs
        with patch('shu.maa_visual_native.inspect_process_instance', side_effect=self.inspect):
            return retire_exited_queue_inputs(self.workflow, self.supervisor,
                task_id=self.new_context['task_id'], guard=guard, cancel_event=self.cancel)

    def assert_pending_unchanged(self, result):
        self.assertIn(result['status'], {'pending', 'blocked'}, result)
        operation = self.workflow.get_operation(self.operation['operation_id'])
        self.assertEqual(operation['status'], 'unknown')
        self.assertIsNone(operation['result'])
        self.assertEqual(self.workflow.slot_state()['barrier_operation_id'], operation['operation_id'])
        self.assertEqual(self.workflow.current(self.old_context['task_id']), self.old_state)
        self.assertEqual(result['gui_inputs'], 0)

    def test_new_task_retires_exited_owner_without_claiming_import_success(self):
        result = self.recover()
        self.assertEqual(result['status'], 'reconciled', result)
        self.assertEqual(result['recovered_operation_ids'], [self.operation['operation_id']])
        self.assertEqual(result['gui_inputs'], 0)
        self.assertEqual(self.supervisor.requests, [{'op': 'snapshot'}])
        self.assertTrue(self.inspected)
        self.assertTrue(all(item == self.old_process for item in self.inspected))
        old = self.workflow.get_operation(self.operation['operation_id'])
        self.assertEqual(old['status'], 'failed')
        self.assertTrue(old['result']['session_retired'])
        self.assertEqual(old['result']['outcome'], 'unknown')
        self.assertEqual(self.workflow.current(self.old_context['task_id']), self.old_state)
        self.assertIsNone(self.workflow.slot_state()['barrier_operation_id'])
        self.assertTrue(result['evidence_refs'])
        for ref in result['evidence_refs']:
            file = Path(ref['path'])
            self.assertTrue(file.is_relative_to(self.fixture.root / 'task-private'
                                                / self.new_context['task_id']))
            self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(), ref['sha256'])
        new = self.workflow.prepare_operation(self.new_context['task_id'],
            self.new_context['dispatch_id'], 'new-task-after-retirement', {'kind': 'maa_import'})
        self.workflow.issue_operation(new['operation_id'])
        self.assertEqual(self.workflow.slot_state()['barrier_operation_id'], new['operation_id'])

    def test_live_old_process_cannot_be_retired_even_if_window_changed(self):
        self.process_result['status'] = 'alive'
        self.supervisor.target_identity = {**self.old_process, 'hwnd': 11}
        self.assert_pending_unchanged(self.recover())

    def test_access_denied_or_unknown_identity_is_not_exit_proof(self):
        self.process_result.update(status='unknown', reason='access_denied')
        self.assert_pending_unchanged(self.recover())

    def test_identity_receipt_for_another_process_is_rejected(self):
        self.process_result['pid'] = 999
        self.assert_pending_unchanged(self.recover())

    def test_old_process_must_still_be_confirmed_exited_at_commit(self):
        def race(identity):
            self.inspected.append(copy.deepcopy(identity))
            result = copy.deepcopy(self.process_result)
            if len(self.inspected) > 1:
                result.update(status='alive', reason='original_process_alive')
            return result
        self.inspect = race
        self.assert_pending_unchanged(self.recover())

    def test_still_authorized_old_generation_is_not_stolen(self):
        with self.engine._transaction():
            self.engine._db.execute('UPDATE workflow_tasks SET epoch=? WHERE task_id=?',
                (self.old_context['epoch'], self.old_context['task_id']))
            self.engine._db.execute('UPDATE tasks SET version=?,status=? WHERE id=?',
                (self.old_context['epoch'], 'running', self.old_context['task_id']))
            self.engine._db.execute('UPDATE workflow_tasks SET writes_enabled=1 WHERE task_id=?',
                (self.old_context['task_id'],))
        self.old_state = copy.deepcopy(self.workflow.current(self.old_context['task_id']))
        self.assert_pending_unchanged(self.recover())

    def test_pid_reuse_is_exit_of_original_creation_instance(self):
        self.process_result.update(reason='pid_reused', observed_creation_time=444)
        self.assertEqual(self.recover()['status'], 'reconciled')

    def test_exclusive_snapshot_failure_does_not_release_old_barrier(self):
        self.supervisor.answer_status = 'unknown'
        self.assert_pending_unchanged(self.recover())

    def test_cancelled_request_does_not_capture_or_retire(self):
        self.cancel.set()
        self.assert_pending_unchanged(self.recover())
        self.assertEqual(self.supervisor.requests, [])

    def test_revocation_during_capture_wins_against_retirement(self):
        self.supervisor.after_capture = lambda: self.workflow.control(self.new_context['task_id'], 'pause')
        self.assert_pending_unchanged(self.recover())

    def test_new_epoch_during_capture_cannot_use_previous_request_guard(self):
        def change_epoch():
            self.workflow.control(self.new_context['task_id'], 'pause')
            self.workflow.control(self.new_context['task_id'], 'resume')
        self.supervisor.after_capture = change_epoch
        self.assert_pending_unchanged(self.recover())

    def test_busy_native_helper_cannot_be_handed_over(self):
        self.supervisor.health_value['pending'] = 1
        self.assert_pending_unchanged(self.recover())

    def test_non_queue_or_start_effect_is_never_retired(self):
        for change in ({'kind': 'maa_import'}, {'kind': 'start'}):
            with self.subTest(action=change):
                with self.engine._transaction():
                    self.engine._db.execute('UPDATE workflow_operations SET action=?,payload_hash=? WHERE operation_id=?',
                        (*_encode(change), self.operation['operation_id']))
                self.assert_pending_unchanged(self.recover())

    def test_tampered_original_queue_run_cannot_authorize_retirement(self):
        with self.engine._transaction():
            self.engine._db.execute("UPDATE workflow_queue_runs SET payload_hash=? WHERE task_id=?",
                                    ('0' * 64, self.old_context['task_id']))
        self.assert_pending_unchanged(self.recover())

    def test_retirement_is_idempotent_and_late_worker_cannot_reopen_effect(self):
        first = self.recover()
        self.assertEqual(first['status'], 'reconciled', first)
        original = self.workflow.get_operation(self.operation['operation_id'])
        second = self.recover()
        self.assertEqual(second['status'], 'reconciled', second)
        self.assertEqual(second['recovered_operation_ids'], [])
        self.assertEqual(self.workflow.get_operation(self.operation['operation_id']), original)
        with self.assertRaises((ActionRejected, ValueError)):
            self.workflow.finish_operation(self.operation['operation_id'], 'unknown', {'late': True})
        self.assertEqual(self.workflow.get_operation(self.operation['operation_id']), original)

    def test_each_task_owns_an_independent_proof_without_private_screenshot_links(self):
        result = self.recover()
        self.assertEqual(result['status'], 'reconciled', result)
        operation = self.workflow.get_operation(self.operation['operation_id'])
        old_ref = operation['result']['evidence_ref']
        new_ref = result['evidence_refs'][0]
        self.assertNotEqual(old_ref['path'], new_ref['path'])
        self.assertTrue(Path(old_ref['path']).is_relative_to(
            self.fixture.root / 'task-private' / self.old_context['task_id']))
        self.assertTrue(Path(new_ref['path']).is_relative_to(
            self.fixture.root / 'task-private' / self.new_context['task_id']))

        def references(value):
            if isinstance(value, dict):
                if 'path' in value and 'sha256' in value:
                    yield value['path']
                if 'image_path' in value and 'image_sha256' in value:
                    yield value['image_path']
                for child in value.values():
                    yield from references(child)
            elif isinstance(value, list):
                for child in value:
                    yield from references(child)

        for reference in (old_ref, new_ref):
            payload = json.loads(Path(reference['path']).read_text(encoding='utf-8'))
            self.assertEqual(list(references(payload)), [], payload)
            self.assertNotIn('image_path', payload['observation'])

        # The foreign-reference scanner must not consider A a consumer of B's
        # proof. It remains an independent owner receipt, not an adopted file.
        cleanup = TaskCleanup(self.workflow)
        cleanup._file_manifest(self.new_context['task_id'], cleanup._selection(self.new_context['task_id']))
        cleanup._file_manifest(self.old_context['task_id'], cleanup._selection(self.old_context['task_id']))
        owned = list(self.engine._db.execute(
            'SELECT task_id,path FROM workflow_cleanup_files WHERE path IN (?,?)',
            (old_ref['path'], new_ref['path'])))
        self.assertEqual({tuple(row) for row in owned}, {
            (self.old_context['task_id'], old_ref['path']),
            (self.new_context['task_id'], new_ref['path'])})
        self.workflow.finish_dispatch(self.new_context['dispatch_id'], {'status': 'fixture_done'})
        self.workflow.management.trash(self.new_context['task_id'])
        purged = cleanup.purge(self.new_context['task_id'])
        self.assertEqual(purged['status'], 'completed', purged)
        self.assertFalse(Path(new_ref['path']).exists())
        self.assertTrue(Path(old_ref['path']).is_file())
        self.assertEqual(self.workflow.get_operation(self.operation['operation_id']), operation)

    def test_purging_or_purged_source_cannot_be_resurrected_by_recovery(self):
        self.workflow.management.trash(self.old_context['task_id'])
        self.old_state = copy.deepcopy(self.workflow.current(self.old_context['task_id']))
        for lifecycle in ('purge_pending', 'purged'):
            with self.subTest(lifecycle=lifecycle):
                # Model an existing cleanup tombstone while retaining this
                # unresolved row as a negative crash-recovery fixture.
                with self.engine._transaction():
                    self.engine._db.execute('UPDATE workflow_lifecycle SET state=? WHERE task_id=?',
                        (lifecycle, self.old_context['task_id']))
                self.assert_pending_unchanged(self.recover())
                self.assertEqual(self.workflow.management.lifecycle(self.old_context['task_id'])['state'], lifecycle)

    def test_old_generation_reauthorized_during_capture_wins_at_commit(self):
        def authorize_old_generation():
            # Test-only race fixture: same original generation becomes an
            # active writer after the first lifetime check, before commit.
            with self.engine._transaction():
                self.engine._db.execute('UPDATE workflow_tasks SET epoch=?,writes_enabled=1 WHERE task_id=?',
                    (self.old_context['epoch'], self.old_context['task_id']))
                self.engine._db.execute('UPDATE tasks SET version=?,status=? WHERE id=?',
                    (self.old_context['epoch'], 'running', self.old_context['task_id']))
            self.old_state = copy.deepcopy(self.workflow.current(self.old_context['task_id']))
        self.supervisor.after_capture = authorize_old_generation
        self.assert_pending_unchanged(self.recover())


if __name__ == '__main__':
    unittest.main()

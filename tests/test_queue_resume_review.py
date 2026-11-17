"""Old queue effects can reconcile after task epoch advance, without input."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock, patch

from shu import maa_queue_receipt as receipt
from shu.core import ActionRejected
from shu.workflow_store import _encode


class QueueResumeReviewTests(unittest.TestCase):
    def setUp(self):
        self.initialize()

    def initialize(self, before_pause=None):
        from tests.test_maa_queue_store import QueueStoreTests
        from tests.test_maa_queue_receipt import QueueReceiptTests, SOURCE
        self.source = SOURCE
        self.store_case = QueueStoreTests()
        self.addCleanup(self.store_case.doCleanups)
        self.store_case.setUp()
        self.files = QueueReceiptTests()
        self.addCleanup(self.files.doCleanups)
        self.files.setUp()
        case = self.store_case
        case.context = case.start_task()
        case.plan = case.make_plan()
        target = case.plan['targets'][0]
        target.update(stage_id=SOURCE['stage_name'],source_difficulty=3,
            document_ref=case.publish('document',SOURCE),mystery_code='prts://123',
            source_semantic_sha256=_encode(receipt.canonical_copilot_document(SOURCE))[1])
        case.plan['plan_sha256'] = _encode({k:v for k,v in case.plan.items() if k!='plan_sha256'})[1]
        case.plan_ref = case.publish('plan',case.plan)
        case.queue.register_plan(case.context,case.plan,case.plan_ref)
        self.files.evidence = Path(case.plan_ref['path']).parent
        self.files.frame = self.files.fresh_frame()
        case.snapshot = self.files.snapshot([])
        case.queue.record_snapshot(case.context,case.snapshot)
        baseline = receipt.capture_queue_baseline(self.files.binding,target,SOURCE,
            self.files.evidence,case.snapshot)
        self.action = case.action(baseline['evidence_ref'])
        case.workflow.issue_operation(self.action['operation_id'])
        submitted = datetime.fromtimestamp(self.files.now,timezone.utc).isoformat()
        with case.engine._transaction():
            case.engine._db.execute("UPDATE workflow_operations SET status='submitted',submitted_at=? WHERE operation_id=?",
                (submitted,self.action['operation_id']))
        self.original = dict(case.context)
        if before_pause is not None:
            before_pause()
        case.workflow.control(case.context['task_id'],'pause')
        self.current = case.workflow.control(case.context['task_id'],'resume')
        self.files.now += 1
        self.files.append_success()
        self.files.snapshot([self.files.row()])
        self.captures = case.root/'maa-visual-captures'
        self.captures.mkdir()
        self.commands = []
        self.native = SimpleNamespace(directory=case.root,target_identity=self.files.process,
            request=self.snapshot,health=lambda:{'revoked':True,'unknown':False,'helper_exit_confirmed':True},
            reconcile=Mock())
        self.view = Mock()
        self.view.requires_frame_refresh = False
        self.view.observe.side_effect = self.observe
        self.guard = self.check_current

    def check_current(self):
        case = self.store_case
        case.workflow.management.require_active(self.current['task_id'])
        state = case.workflow.current(self.current['task_id'])
        if state['epoch']!=self.current['epoch'] or not state['writes_enabled']:
            raise ActionRejected('current task revoked')

    def snapshot(self, command, **kwargs):
        self.assertEqual(command,{'op':'snapshot'})
        self.commands.append(command)
        import cv2
        import numpy as np
        # A second recovery capture occurs after the bounded observation wait.
        # Advance the synthetic clock before that file is created as well.
        self.files.now += max(0,getattr(self,'capture_advance',.01)-.01)
        identifier = uuid.uuid4().hex
        path = self.captures/(identifier+'.png')
        ok, encoded = cv2.imencode('.png',np.full((40,80,3),255,dtype=np.uint8))
        self.assertTrue(ok)
        path.write_bytes(encoded.tobytes())
        stamp = round(self.files.now*1000)*1_000_000
        os.utime(path,ns=(stamp,stamp))
        self.files.now += getattr(self,'capture_advance',.01)
        return {'status':'completed','result':{'screenshot_id':identifier,'image_path':str(path),
            'image_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'method':'win32_printwindow','window':{**self.files.process,
                'client_rect':{'x':0,'y':0,'width':80,'height':40},'dpi':96,
                'foreground':True,'minimized':False},'permission':{'input_allowed':False}}}

    def observe(self,path,*,expected=None,cancel_event=None):
        self.assertEqual(expected['rows'][0]['copilot_id'],123)
        return {'schema':'maa_queue_vision/v1','ok':True,'page':'copilot','queue_mode':True,
            'idle':True,'running':False,'width':80,'height':40,
            'image_sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            'projection_sha256':expected['projection_sha256'],'visible_complete':True}

    def recover(self):
        from shu.maa_queue_recovery import recover_queue_inputs
        return recover_queue_inputs(self.store_case.workflow,self.native,self.view,
            task_id=self.current['task_id'],guard=self.guard)

    def test_old_epoch_confirmed_auto_add_releases_only_original_barrier(self):
        case = self.store_case
        self.assertIsNone(case.queue.current(self.current['task_id']))
        before = case.workflow.pending_operations(self.current['task_id'])
        self.assertEqual(before[0]['status'],'unknown')
        result = self.recover()
        self.assertEqual(result['status'],'reconciled',result)
        self.assertEqual(result['recovered_operation_ids'],[self.action['operation_id']])
        self.assertEqual(case.workflow.get_operation(self.action['operation_id'])['status'],'verified')
        self.assertIsNone(case.workflow.slot_state()['barrier_operation_id'])
        self.assertIsNone(case.queue.current(self.current['task_id']))
        self.assertTrue(case.workflow.current(self.current['task_id'])['writes_enabled'])
        self.assertEqual(self.commands,[{'op':'snapshot'}])
        self.native.reconcile.assert_called_once()

    def test_trashed_task_never_captures_or_creates_recovery_evidence(self):
        self.store_case.workflow.management.trash(self.current['task_id'])
        before = set(self.files.evidence.iterdir())
        result = self.recover()
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(set(self.files.evidence.iterdir()),before)
        self.assertEqual(self.commands,[])
        self.native.reconcile.assert_not_called()

    def test_visual_incomplete_keeps_pending_barrier_and_performs_no_input(self):
        self.view.observe.side_effect = None
        self.view.observe.return_value = {'schema':'maa_queue_vision/v1','ok':False}
        result = self.recover()
        self.assertEqual(result['status'],'pending',result)
        self.assertEqual(self.store_case.workflow.slot_state()['barrier_operation_id'],self.action['operation_id'])
        self.assertEqual(self.commands,[{'op':'snapshot'}])
        self.native.reconcile.assert_not_called()

    def test_recovery_never_reads_or_settles_a_peer_task_input(self):
        case = self.store_case
        other = case.start_task()
        self.current = other
        result = self.recover()
        self.assertEqual(result['recovered_operation_ids'],[])
        self.assertEqual(case.workflow.get_operation(self.action['operation_id'])['status'],'unknown')
        self.assertEqual(case.workflow.slot_state()['barrier_operation_id'],self.action['operation_id'])
        self.assertEqual(self.commands,[])
        self.native.reconcile.assert_not_called()

    def test_revocation_during_vision_keeps_barrier_and_creates_no_effect_receipt(self):
        observe = self.observe
        def revoke(path,**kwargs):
            result = observe(path,**kwargs)
            self.store_case.workflow.control(self.current['task_id'],'pause')
            return result
        self.view.observe.side_effect = revoke
        before = set(self.files.evidence.glob('maa-queue-recovery-receipt-*.json'))
        result = self.recover()
        self.assertEqual(result['status'],'blocked',result)
        self.assertEqual(set(self.files.evidence.glob('maa-queue-recovery-receipt-*.json')),before)
        self.assertEqual(self.store_case.workflow.slot_state()['barrier_operation_id'],self.action['operation_id'])
        self.native.reconcile.assert_not_called()

    def test_next_day_recovery_retains_original_request_window_and_uses_fresh_native_frame(self):
        self.files.now += 24*60*60
        result = self.recover()
        self.assertEqual(result['status'],'reconciled',result)
        self.assertEqual(result['recovered_operation_ids'],[self.action['operation_id']])
        self.assertEqual(self.store_case.workflow.get_operation(self.action['operation_id'])['status'],'verified')
        self.assertIsNone(self.store_case.workflow.slot_state()['barrier_operation_id'])
        self.assertEqual(self.commands,[{'op':'snapshot'}])
        self.native.reconcile.assert_called_once()

    def test_next_day_second_matching_request_cannot_replace_original_committed_effect(self):
        self.files.now += 24*60*60
        self.files.append_success()
        result = self.recover()
        self.assertEqual(result['status'],'pending',result)
        self.assertEqual(self.store_case.workflow.slot_state()['barrier_operation_id'],self.action['operation_id'])
        self.native.reconcile.assert_not_called()

    def prepare_observation_scroll(self):
        case = self.store_case
        self.scroll = case.queue.prepare_item_action(case.context,None,phase='scroll',snapshot=case.snapshot)
        case.workflow.issue_operation(self.scroll['operation_id'])
        stamp = datetime.fromtimestamp(self.files.now,timezone.utc).isoformat()
        with patch('shu.workflow_store._utc_now',return_value=stamp):
            case.workflow.enter_submission(self.scroll['operation_id'],queue_guard=self.scroll['queue_guard'])

    def test_old_observation_scroll_settles_after_parent_with_fresh_visual_and_exited_worker(self):
        self.initialize(before_pause=self.prepare_observation_scroll)
        result = self.recover()
        self.assertEqual(result['status'],'reconciled',result)
        self.assertEqual(result['recovered_operation_ids'],
                         [self.action['operation_id'],self.scroll['operation_id']])
        case = self.store_case
        self.assertEqual(case.workflow.get_operation(self.scroll['operation_id'])['status'],'verified')
        self.assertIsNone(case.workflow.slot_state()['barrier_operation_id'])
        self.assertIsNone(case.queue.current(self.current['task_id']))
        self.assertEqual(self.commands,[{'op':'snapshot'},{'op':'snapshot'}])
        self.native.reconcile.assert_called_once()

    def test_old_observation_scroll_never_settles_without_confirmed_old_worker_exit(self):
        self.initialize(before_pause=self.prepare_observation_scroll)
        self.native.health = lambda:{'revoked':True,'unknown':True,'child_alive':True,
                                    'helper_exit_confirmed':False}
        result = self.recover()
        self.assertEqual(result['status'],'pending',result)
        self.assertEqual(self.store_case.workflow.get_operation(self.scroll['operation_id'])['status'],'unknown')
        self.assertEqual(result['recovered_operation_ids'],[self.action['operation_id']])
        self.native.reconcile.assert_not_called()
        self.assertTrue(all(command=={'op':'snapshot'} for command in self.commands))

    def test_new_readonly_capture_worker_cannot_be_confused_with_old_input_worker(self):
        self.initialize(before_pause=self.prepare_observation_scroll)
        self.native.health = lambda:{'revoked':True,'unknown':True,
            'child_alive':bool(self.commands),'helper_exit_confirmed':not self.commands}
        result = self.recover()
        # The old worker was confirmed exited before either fresh snapshot.
        # A completed capture has acquired the exclusive desktop/helper locks;
        # its new live read-only child must not block reconciliation forever.
        self.assertEqual(result['status'],'reconciled',result)
        self.assertEqual(result['code'],'queue_inputs_reconciled')
        self.assertEqual(result['recovered_operation_ids'],
                         [self.action['operation_id'],self.scroll['operation_id']])
        self.assertEqual(self.store_case.workflow.get_operation(self.scroll['operation_id'])['status'],'verified')
        self.native.reconcile.assert_called_once()

    def test_new_capture_worker_with_pending_request_still_blocks_resuming(self):
        for blocker in ('pending','stop_pending','closed'):
            with self.subTest(blocker=blocker):
                self.initialize(before_pause=self.prepare_observation_scroll)
                self.native.health = lambda:{'revoked':True,'unknown':True,
                    'child_alive':bool(self.commands),'helper_exit_confirmed':not self.commands,
                    blocker:bool(self.commands)}
                result = self.recover()
                self.assertEqual(result['status'],'pending',result)
                self.assertEqual(result['code'],'queue_recovery_native_pending')
                self.assertEqual(result['recovered_operation_ids'],
                                 [self.action['operation_id'],self.scroll['operation_id']])
                self.native.reconcile.assert_not_called()

    def test_real_readonly_worker_can_reconcile_while_retaining_exclusive_locks(self):
        from shu.gui_supervisor import GuiSupervisor
        from tests.test_gui_supervisor import IDENTITY, synthetic_gui
        lock=self.store_case.root/'readonly-recovery.lock'
        state=lock.with_suffix('.lock.state.json')
        state.write_text(json.dumps({'unknown':True,'revoked':True,
            'reason':'submission_unconfirmed'}),encoding='utf-8')
        owner=GuiSupervisor(IDENTITY,lock_path=lock,test_handler=synthetic_gui)
        self.addCleanup(owner.close)
        answer=owner.request({'op':'snapshot'},timeout=5)
        self.assertEqual(answer['status'],'completed',answer)
        self.assertTrue(owner.health()['child_alive'])
        self.assertFalse(owner.health()['helper_exit_confirmed'])
        self.assertTrue(owner.health()['unknown'])
        result=owner.reconcile({'reconciled':True,'evidence_ref':{'path':'verified-receipt'}},resume=True)
        self.assertEqual(result['status'],'reconciled')
        self.assertFalse(owner.health()['unknown'])
        self.assertFalse(owner.health()['revoked'])
        peer=GuiSupervisor(IDENTITY,lock_path=lock,test_handler=synthetic_gui)
        self.addCleanup(peer.close)
        competing=peer.request({'op':'snapshot'},timeout=5)
        self.assertNotEqual(competing['status'],'completed',competing)
        self.assertEqual(competing['error'],'desktop_control_slot_busy')

    def test_stale_native_file_is_not_fresh_observation_even_with_matching_pixels(self):
        snapshot = self.snapshot
        def stale(command,**kwargs):
            answer = snapshot(command,**kwargs)
            stamp = int((self.files.now-3600)*1e9)
            os.utime(answer['result']['image_path'],ns=(stamp,stamp))
            return answer
        self.native.request = stale
        result = self.recover()
        self.assertEqual(result['status'],'pending',result)
        self.assertEqual(result['code'],'queue_recovery_capture_invalid')
        self.assertEqual(result['recovered_operation_ids'],[])
        self.assertEqual(self.store_case.workflow.slot_state()['barrier_operation_id'],self.action['operation_id'])
        self.native.reconcile.assert_not_called()

    def test_revoked_during_scroll_readback_keeps_scroll_unknown_after_parent_reconciled(self):
        self.initialize(before_pause=self.prepare_observation_scroll)
        observe = self.observe
        def revoke_second(path,**kwargs):
            result = observe(path,**kwargs)
            if len(self.commands)==2:
                self.store_case.workflow.control(self.current['task_id'],'pause')
            return result
        self.view.observe.side_effect = revoke_second
        result = self.recover()
        self.assertEqual(result['status'],'blocked',result)
        self.assertEqual(result['recovered_operation_ids'],[self.action['operation_id']])
        self.assertEqual(self.store_case.workflow.get_operation(self.scroll['operation_id'])['status'],'unknown')
        self.assertEqual(list(self.files.evidence.glob('maa-queue-observation-recovery-*.json')),[])
        self.native.reconcile.assert_not_called()
        self.assertEqual(self.commands,[{'op':'snapshot'},{'op':'snapshot'}])

    def test_observation_parent_cannot_belong_to_another_task_or_dispatch(self):
        self.initialize(before_pause=self.prepare_observation_scroll)
        case = self.store_case
        other = case.start_task()
        with case.engine._transaction():
            case.engine._db.execute('UPDATE workflow_operations SET dispatch_id=? WHERE operation_id=?',
                                   (other['dispatch_id'],self.scroll['operation_id']))
        result = self.recover()
        self.assertEqual(result['status'],'pending',result)
        self.assertEqual(result['code'],'queue_recovery_operation_mismatch')
        self.assertEqual(case.workflow.get_operation(self.scroll['operation_id'])['status'],'unknown')
        self.assertEqual(result['recovered_operation_ids'],[self.action['operation_id']])
        self.native.reconcile.assert_not_called()

    def test_long_queue_recovery_requires_full_readonly_coverage_and_never_scrolls(self):
        self.initialize(before_pause=self.prepare_observation_scroll)
        observe = self.observe
        def incomplete(path,**kwargs):
            return {**observe(path,**kwargs),'visible_complete':False}
        self.view.observe.side_effect = incomplete
        result = self.recover()
        self.assertEqual(result['status'],'pending',result)
        self.assertEqual(result['code'],'queue_recovery_full_visual_required')
        self.assertEqual(result['recovered_operation_ids'],[])
        self.assertEqual(self.store_case.workflow.slot_state()['barrier_operation_id'],self.action['operation_id'])
        self.assertEqual(self.commands,[{'op':'snapshot'}])
        self.native.reconcile.assert_not_called()

    def use_old_delete_fixture(self):
        from tests.test_queue_effect_receipts import QueueEffectLedgerTests
        fixture = QueueEffectLedgerTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        case,files = fixture.ledger,fixture.files
        self.store_case,self.files = case,files
        self.action = case.queue.prepare_item_action(case.context,None,phase='delete',
            snapshot=fixture.code_proof['current_snapshot'],row_identity=fixture.extra,
            effect_baseline_ref=fixture.effect['evidence_ref'])
        case.workflow.issue_operation(self.action['operation_id'])
        stamp = datetime.fromtimestamp(files.now,timezone.utc).isoformat()
        with patch('shu.workflow_store._utc_now',return_value=stamp):
            case.workflow.enter_submission(self.action['operation_id'],queue_guard=self.action['queue_guard'])
        self.original = dict(case.context)
        case.workflow.control(case.context['task_id'],'pause')
        self.current = case.workflow.control(case.context['task_id'],'resume')
        files.now += .3
        files.snapshot([files.row()])
        self.captures = case.root/'maa-visual-captures'
        self.captures.mkdir()
        self.commands = []
        self.native.directory,self.native.target_identity = case.root,files.process
        self.capture_advance = .2
        return fixture

    def test_old_delete_reconciles_only_its_owned_extra_without_replaying_click(self):
        fixture = self.use_old_delete_fixture()
        result = self.recover()
        self.assertEqual(result['status'],'reconciled',result)
        self.assertEqual(result['recovered_operation_ids'],[self.action['operation_id']])
        operation = self.store_case.workflow.get_operation(self.action['operation_id'])
        self.assertEqual(operation['status'],'verified')
        self.assertEqual(operation['result']['deleted_rows'],[fixture.extra])
        self.assertEqual(self.commands,[{'op':'snapshot'},{'op':'snapshot'}])
        self.assertIsNone(self.store_case.queue.current(self.current['task_id']))

    def test_old_delete_changed_source_never_retires_effect_barrier(self):
        fixture = self.use_old_delete_fixture()
        with self.store_case.engine._transaction():
            self.store_case.engine._db.execute("UPDATE workflow_operations SET status='unknown' WHERE operation_id=?",
                                              (fixture.source_operation['operation_id'],))
        result = self.recover()
        self.assertNotEqual(result['status'],'reconciled',result)
        self.assertEqual(self.store_case.workflow.get_operation(self.action['operation_id'])['status'],'unknown')
        self.assertEqual(self.store_case.workflow.slot_state()['barrier_operation_id'],self.action['operation_id'])
        self.native.reconcile.assert_not_called()

    def test_old_delete_outside_receipt_window_remains_pending_without_input(self):
        self.use_old_delete_fixture()
        self.files.now += 24*60*60
        result = self.recover()
        self.assertEqual(result['status'],'pending',result)
        self.assertEqual(result['code'],'receipt_window_expired')
        self.assertEqual(self.store_case.workflow.get_operation(self.action['operation_id'])['status'],'unknown')
        self.assertEqual(self.store_case.workflow.slot_state()['barrier_operation_id'],self.action['operation_id'])
        self.native.reconcile.assert_not_called()
        self.assertEqual(self.commands,[{'op':'snapshot'},{'op':'snapshot'}])

    def test_old_delete_cancelled_on_second_capture_never_reconciles(self):
        self.use_old_delete_fixture()
        observe = self.observe
        def revoke_second(path,**kwargs):
            result = observe(path,**kwargs)
            if len(self.commands)==2:
                self.store_case.workflow.control(self.current['task_id'],'pause')
            return result
        self.view.observe.side_effect = revoke_second
        result = self.recover()
        self.assertEqual(result['status'],'blocked',result)
        self.assertEqual(result['recovered_operation_ids'],[])
        self.assertEqual(list(self.files.evidence.glob('maa-queue-effect-receipt-*.json')),[])
        self.assertEqual(self.store_case.workflow.get_operation(self.action['operation_id'])['status'],'unknown')
        self.assertEqual(self.store_case.workflow.slot_state()['barrier_operation_id'],self.action['operation_id'])
        self.native.reconcile.assert_not_called()


if __name__=='__main__':
    unittest.main()

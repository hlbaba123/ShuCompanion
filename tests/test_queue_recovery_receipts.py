"""Late reconciliation preserves the original bounded input and fresh readback."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

from shu import maa_queue_receipt as receipts
from tests import test_maa_queue_receipt as fixtures
from tests import test_maa_queue_store as store_fixtures
from shu.workflow_store import _encode


class QueueRecoveryReceiptTests(unittest.TestCase):
    def setUp(self):
        self.files = fixtures.QueueReceiptTests()
        self.files.setUp()
        self.addCleanup(self.files.doCleanups)
        files = self.files
        self.baseline = files.baseline([])
        self.document_ref = receipts._publish(files.evidence,'original-document',fixtures.SOURCE)
        self.committed = {'operation_id':'committed-operation','task_id':'original-task',
            'revision':1,'epoch':2,'submitted_at':datetime.fromtimestamp(files.now+.1,timezone.utc).isoformat(),
            'payload_hash':'a'*64,'baseline':self.baseline,'document_ref':self.document_ref}
        files.now += 1
        files.append_success()
        files.config.write_bytes(fixtures.raw_config([files.row()]))

    def collect(self, delay=3600, *, frame=None):
        self.files.now += delay
        return receipts.collect_queue_recovery_receipt(self.committed,self.files.binding,self.files.evidence,
            screenshot_refs=[frame or self.files.fresh_frame()],visible_complete=True)

    def expected(self):
        return {**self.committed,'target':self.files.target,'source_document':fixtures.SOURCE,
                'binding':self.files.binding}

    def test_one_hour_and_one_day_late_snapshot_recovers_original_timely_load(self):
        for delay in (3600,86400):
            with self.subTest(delay=delay):
                result = self.collect(delay)
                self.assertEqual(result['status'],'verified',result)
                payload = self.files.payload(result)
                self.assertEqual(receipts.verify_queue_recovery_receipt(payload,
                    self.expected())['status'],'verified')
                ordinary = receipts.collect_queue_receipt(self.baseline,self.files.binding,self.files.evidence,
                    screenshot_refs=[self.files.fresh_frame()],visible_complete=True)
                self.assertEqual(ordinary['code'],'receipt_window_expired')

    def test_second_get_changed_prefix_or_preinput_frame_never_recovers(self):
        self.files.now += 3600
        stale = receipts.collect_queue_recovery_receipt(self.committed,self.files.binding,self.files.evidence,
            screenshot_refs=self.baseline['snapshot']['screenshot_refs'],visible_complete=True)
        self.assertNotEqual(stale['status'],'verified',stale)
        self.files.append_success()
        result = self.collect(0)
        self.assertNotEqual(result['status'],'verified',result)
        self.files.log.write_bytes(b'different prefix\n'+self.files.log.read_bytes())
        result = self.collect(0)
        self.assertNotEqual(result['status'],'verified',result)

    def test_load_after_original_window_is_not_historical_success(self):
        self.files.log.write_bytes(b'older log\n')
        self.files.now += 3600
        self.files.append_success()
        result = self.collect(0)
        self.assertNotEqual(result['status'],'verified',result)

    def test_wrong_operation_baseline_source_and_current_binding_are_rejected(self):
        result = self.collect()
        self.assertTrue(result['verified'],result)
        payload = self.files.payload(result)
        changed_source = copy.deepcopy(fixtures.SOURCE)
        changed_source['actions'][0]['direction'] = 'Left'
        for changes in ({'operation_id':'other'}, {'epoch':3}, {'payload_hash':'b'*64},
                        {'document_ref':{**self.document_ref,'sha256':'0'*64}},
                        {'source_document':changed_source},
                        {'baseline':{**self.baseline,'input_id':'different'}}):
            with self.subTest(changes=list(changes)):
                checked = receipts.verify_queue_recovery_receipt(payload,{**self.expected(),**changes})
                self.assertNotEqual(checked['status'],'verified',checked)

    def test_recovered_input_retains_provenance_for_its_combined_mode_extra(self):
        files = self.files
        files.config.write_bytes(fixtures.raw_config([files.row(),files.row(raid=True)]))
        result = self.collect()
        self.assertTrue(result['verified'],result)
        payload = files.payload(result)
        source = {'operation_id':self.committed['operation_id'],
                  'receipt_ref':result['receipt_ref'],'receipt':payload}
        extra = next(row['identity'] for row in payload['current_snapshot']['rows'] if row['is_raid'])
        baseline = receipts.capture_queue_effect_baseline(files.binding,'delete',files.evidence,
            payload['current_snapshot'],target=files.target,source_document=fixtures.SOURCE,
            source_input=source,row_identity=extra,planned_targets=[files.target])
        self.assertEqual(baseline['row_identity'],extra)


class QueueRecoveryLedgerTests(unittest.TestCase):
    def test_late_original_input_can_reconcile_after_trash_without_reauthorizing(self):
        files = fixtures.QueueReceiptTests()
        files.setUp()
        self.addCleanup(files.doCleanups)
        ledger = store_fixtures.QueueStoreTests()
        ledger.setUp()
        self.addCleanup(ledger.doCleanups)
        context = ledger.start_task()
        ledger.context = context
        plan = ledger.make_plan()
        target = plan['targets'][0]
        document_ref = ledger.publish('document',fixtures.SOURCE)
        target.update(stage_id=fixtures.SOURCE['stage_name'],source_difficulty=3,
            document_ref=document_ref,mystery_code='prts://123',
            source_semantic_sha256=_encode(receipts.canonical_copilot_document(fixtures.SOURCE))[1])
        plan['plan_sha256'] = _encode({key:value for key,value in plan.items() if key!='plan_sha256'})[1]
        ledger.queue.register_plan(context,plan,ledger.publish('plan',plan))
        files.evidence = Path(ledger.publish('directory',{})['path']).parent
        files.frame = files.fresh_frame()
        snapshot = files.snapshot([])
        ledger.queue.record_snapshot(context,snapshot)
        baseline = receipts.capture_queue_baseline(files.binding,target,fixtures.SOURCE,files.evidence,snapshot)
        operation = ledger.queue.prepare_item_action(context,target['target_key'],phase='code',
            snapshot=snapshot,input_baseline_ref=baseline['evidence_ref'])
        ledger.workflow.issue_operation(operation['operation_id'])
        stamp = datetime.fromtimestamp(files.now,timezone.utc).isoformat()
        with ledger.engine._transaction():
            ledger.engine._db.execute("UPDATE workflow_operations SET status='submitted',submitted_at=? WHERE operation_id=?",
                                     (stamp,operation['operation_id']))
        committed = ledger.workflow.get_operation(operation['operation_id'])
        committed.update(baseline=baseline,document_ref=document_ref)
        files.now += 1
        files.append_success()
        files.config.write_bytes(fixtures.raw_config([files.row()]))
        files.now += 86400
        result = receipts.collect_queue_recovery_receipt(committed,files.binding,files.evidence,
            screenshot_refs=[files.fresh_frame()],visible_complete=True)
        self.assertTrue(result['verified'],result)
        proof = files.payload(result)
        ledger.queue.register_evidence(context,[result['receipt_ref'],baseline['evidence_ref'],
            *ledger.queue._snapshot_files(context,proof['current_snapshot'])])
        ledger.workflow.management.trash(context['task_id'])
        applied = ledger.queue.record_reconciliation(context['task_id'],original_epoch=context['epoch'],
            operation_id=operation['operation_id'],evidence_ref=result['receipt_ref'],result=result)
        self.assertEqual(applied['status'],'verified')
        self.assertIsNone(ledger.workflow.slot_state()['barrier_operation_id'])
        self.assertFalse(ledger.workflow.current(context['task_id'])['writes_enabled'])


if __name__=='__main__':
    unittest.main()

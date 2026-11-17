"""Post-input queue effects use real local file evidence and synthetic MAA data."""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from shu import maa_queue_receipt as receipt
from tests import test_maa_queue_receipt as fixtures
from tests import test_maa_queue_store as store_fixtures
from shu.workflow_store import _encode

SOURCE = fixtures.SOURCE
raw_config = fixtures.raw_config


class QueueEffectReceiptTests(unittest.TestCase):
    def setUp(self):
        self.files = fixtures.QueueReceiptTests()
        self.files.setUp()
        self.addCleanup(self.files.doCleanups)
        self.source_operation = 'source-code-operation'

    def source_input(self, rows=(), *, auto_added=True):
        files = self.files
        baseline = files.baseline(list(rows))
        files.now += 1
        files.append_success()
        files.config.write_bytes(raw_config([*rows, files.row(), files.row(raid=True)] if auto_added else rows))
        result = files.collect(baseline)
        self.assertEqual(result['status'], 'verified' if auto_added else 'pending', result)
        return {'operation_id':self.source_operation, 'receipt_ref':result['receipt_ref'],
                'receipt':files.payload(result)}

    def effect_baseline(self, phase='delete', source=None):
        files = self.files
        source = source or self.source_input()
        current = source['receipt']['current_snapshot']
        raid = next((row for row in current['rows'] if row['is_raid']), None)
        return receipt.capture_queue_effect_baseline(files.binding, phase, files.evidence,
            current, target=files.target, source_document=SOURCE, source_input=source,
            row_identity=raid['identity'] if phase=='delete' else None,
            planned_targets=[files.target])

    def finish(self, baseline, rows, operation='effect-operation'):
        files = self.files
        submitted = files.now + .1
        files.now += .25
        first = files.snapshot(rows, frame=files.fresh_frame())
        files.now += .2
        result = receipt.collect_queue_effect_receipt(baseline, files.binding, files.evidence,
            previous_snapshot=first, screenshot_refs=[files.fresh_frame()], visible_complete=True,
            operation_id=operation, submitted_at=submitted)
        if 'receipt_ref' in result:
            payload = json.loads(Path(result['receipt_ref']['path']).read_text(encoding='utf-8'))
        else:
            payload = None
        return result, payload, submitted

    def expected(self, baseline, submitted):
        return {'binding':self.files.binding, 'phase':baseline['phase'],
            'baseline_ref':baseline['evidence_ref'], 'effect_id':baseline['effect_id'],
            'operation_id':'effect-operation', 'submitted_at':submitted,
            'source_operation_id':self.source_operation, 'target':self.files.target,
            'source_document':SOURCE, 'row_identity':baseline.get('row_identity')}

    def test_owned_combined_mode_extra_delete_requires_exact_unique_effect(self):
        baseline = self.effect_baseline()
        result, payload, submitted = self.finish(baseline, [self.files.row()])
        self.assertEqual(result['status'], 'verified', result)
        self.assertEqual(result['deleted_rows'], [baseline['row_identity']])
        self.assertEqual(result['added_rows'], [])
        self.assertTrue(result['preserved_baseline'])
        self.assertEqual(receipt.verify_queue_effect_receipt(payload,
            self.expected(baseline, submitted))['status'], 'verified')

    def test_manual_add_post_effect_verifies_exactly_one_selected_mode(self):
        source = self.source_input(auto_added=False)
        baseline = self.effect_baseline('add', source)
        result, payload, submitted = self.finish(baseline,[self.files.row()])
        self.assertEqual(result['status'],'verified',result)
        self.assertEqual(result['queue_effect'],'add_row')
        self.assertEqual(len(result['added_rows']),1)
        self.assertEqual(result['deleted_rows'],[])
        self.assertEqual(receipt.verify_queue_effect_receipt(payload,
            self.expected(baseline,submitted))['status'],'verified')

    def test_combined_source_input_can_prove_the_other_manual_add_mode(self):
        source = self.source_input(auto_added=False)
        target = {**self.files.target,'target_key':'raid-target','difficulty':2}
        baseline = receipt.capture_queue_effect_baseline(self.files.binding,'add',self.files.evidence,
            source['receipt']['current_snapshot'],target=target,source_document=SOURCE,
            source_input=source,planned_targets=[target])
        self.files.target = target
        result, payload, submitted = self.finish(baseline,[self.files.row(raid=True)])
        self.assertEqual(result['status'],'verified',result)
        self.assertEqual(receipt.verify_queue_effect_receipt(payload,
            self.expected(baseline,submitted))['status'],'verified')

    def test_manual_add_wrong_mode_two_rows_or_unchecked_does_not_verify(self):
        for change in ('wrong_mode','two_rows','unchecked'):
            with self.subTest(change=change):
                self.setUp()
                source = self.source_input(auto_added=False)
                baseline = self.effect_baseline('add',source)
                rows = [self.files.row(raid=change=='wrong_mode',checked=change!='unchecked')]
                if change=='two_rows':
                    rows.append(self.files.row(raid=True))
                result,_,_ = self.finish(baseline,rows)
                self.assertNotEqual(result['status'],'verified',result)

    def test_same_target_different_cache_path_cannot_be_verified_as_manual_add(self):
        baseline = self.effect_baseline('add')
        other = self.files.maa/'config/copilot/different-path.json'
        other.write_text(json.dumps(SOURCE),encoding='utf-8')
        extra = self.files.row(path=str(other))
        result,_,_ = self.finish(baseline,[self.files.row(),self.files.row(raid=True),extra])
        self.assertNotEqual(result['status'],'verified',result)

    def test_delete_refuses_planned_target_original_user_row_or_duplicate(self):
        source = self.source_input()
        current = source['receipt']['current_snapshot']
        ordinary = next(row for row in current['rows'] if not row['is_raid'])
        with self.assertRaises(receipt.MaaQueueReceiptError):
            receipt.capture_queue_effect_baseline(self.files.binding,'delete',self.files.evidence,
                current,target=self.files.target,source_document=SOURCE,source_input=source,
                row_identity=ordinary['identity'],planned_targets=[self.files.target])
        forged = copy.deepcopy(source)
        forged['operation_id'] = ''
        with self.assertRaises(receipt.MaaQueueReceiptError):
            self.effect_baseline(source=forged)

    def test_user_baseline_row_cannot_become_owned_extra(self):
        existing = self.files.row(raid=True)
        source = self.source_input([existing])
        with self.assertRaises(receipt.MaaQueueReceiptError):
            self.effect_baseline(source=source)

    def test_checked_changes_unrelated_additions_and_missing_target_never_verify(self):
        for changes in ('checked','extra','target_removed'):
            with self.subTest(changes=changes):
                self.setUp()
                baseline = self.effect_baseline()
                rows = [self.files.row()]
                if changes=='checked':
                    rows[0]['IsChecked'] = False
                elif changes=='extra':
                    rows.append(self.files.row(code=789))
                else:
                    rows = []
                result,_,_ = self.finish(baseline, rows)
                self.assertNotEqual(result['status'],'verified',result)

    def test_stale_or_pre_submission_frames_and_wrong_operation_never_verify(self):
        baseline = self.effect_baseline()
        result, payload, submitted = self.finish(baseline,[self.files.row()])
        self.assertEqual(result['status'],'verified',result)
        expected = self.expected(baseline,submitted)
        for change in ({'operation_id':'different'}, {'source_operation_id':'different'},
                       {'submitted_at':payload['previous_snapshot']['captured_at']+1}):
            with self.subTest(change=change):
                self.assertNotEqual(receipt.verify_queue_effect_receipt(payload,
                    {**expected,**change})['status'],'verified')

    def test_tampered_source_proof_and_binding_cannot_authorize_delete(self):
        source = self.source_input()
        source['receipt']['log_evidence']['events'][0] += 'tampered'
        with self.assertRaises(receipt.MaaQueueReceiptError):
            self.effect_baseline(source=source)

    def test_no_row_and_model_ready_bool_remain_unverified(self):
        files = self.files
        baseline = files.baseline([])
        files.now += 1
        files.append_success()
        first_result = files.collect(baseline)
        payload = files.payload(first_result)
        files.now += .2
        current = files.snapshot([],frame=files.fresh_frame())
        result = receipt.verify_queue_absence_receipt({
            'schema':'maa_queue_absence_receipt/v1','input_receipt':payload,
            'previous_snapshot':payload['current_snapshot'],'current_snapshot':current,
            'parse_ready':{'queue_add_enabled':True,'idle':True,'model_says_ready':True}},
            files.expected(baseline))
        self.assertEqual(result['status'],'pending')
        self.assertEqual(result['code'],'parse_completion_unverified')
        self.assertIs(result['verified'],False)


class QueueEffectLedgerTests(unittest.TestCase):
    def setUp(self):
        self.files = fixtures.QueueReceiptTests()
        self.files.setUp()
        self.addCleanup(self.files.doCleanups)
        self.ledger = store_fixtures.QueueStoreTests()
        self.ledger.setUp()
        self.addCleanup(self.ledger.doCleanups)
        ledger, files = self.ledger, self.files
        ledger.context = ledger.start_task()
        plan = ledger.make_plan()
        self.target = plan['targets'][0]
        self.target.update(stage_id=SOURCE['stage_name'],source_difficulty=3,
            mystery_code='prts://123',document_ref=ledger.publish('document',SOURCE),
            source_semantic_sha256=_encode(receipt.canonical_copilot_document(SOURCE))[1])
        plan['targets'] = [self.target]
        plan['batches'][0]['target_keys'] = [self.target['target_key']]
        plan['batches'][0]['stage_keys'] = [self.target['stage_key']]
        plan['plan_sha256'] = _encode({k:v for k,v in plan.items() if k!='plan_sha256'})[1]
        ledger.queue.register_plan(ledger.context,plan,ledger.publish('plan',plan))
        self.plan = plan
        files.evidence = Path(ledger.publish('directory',{})['path']).parent
        files.frame = files.fresh_frame()
        before = files.snapshot([])
        ledger.queue.record_snapshot(ledger.context,before)
        baseline = receipt.capture_queue_baseline(files.binding,self.target,SOURCE,files.evidence,before)
        operation = ledger.queue.prepare_item_action(ledger.context,self.target['target_key'],
            phase='code',snapshot=before,input_baseline_ref=baseline['evidence_ref'])
        ledger.workflow.issue_operation(operation['operation_id'])
        stamp = datetime.fromtimestamp(files.now,timezone.utc).isoformat()
        with ledger.engine._transaction():
            ledger.engine._db.execute("UPDATE workflow_operations SET status='submitted',submitted_at=? WHERE operation_id=?",
                                     (stamp,operation['operation_id']))
        files.now += 1
        files.append_success()
        files.config.write_bytes(raw_config([files.row(),files.row(raid=True)]))
        result = files.collect(baseline)
        self.assertTrue(result['verified'],result)
        self.code_proof = files.payload(result)
        current = self.code_proof['current_snapshot']
        ledger.queue.register_evidence(ledger.context,[result['receipt_ref'],baseline['evidence_ref'],
            *ledger.queue._snapshot_files(ledger.context,current)])
        ledger.queue.record_reconciliation(ledger.context['task_id'],original_epoch=ledger.context['epoch'],
            operation_id=operation['operation_id'],evidence_ref=result['receipt_ref'],result=result)
        ledger.queue.record_snapshot(ledger.context,current)
        self.source_operation = operation
        self.source_input = {'operation_id':operation['operation_id'],'receipt_ref':result['receipt_ref'],
                             'receipt':self.code_proof}
        self.extra = next(row['identity'] for row in current['rows'] if row['is_raid'])
        self.effect = receipt.capture_queue_effect_baseline(files.binding,'delete',files.evidence,current,
            target=self.target,source_document=SOURCE,source_input=self.source_input,
            row_identity=self.extra,planned_targets=self.plan['targets'])

    def test_prepared_effect_is_immutable_and_verified_late_delete_retires_only_original_input(self):
        ledger,files = self.ledger,self.files
        current = self.code_proof['current_snapshot']
        operation = ledger.queue.prepare_item_action(ledger.context,None,phase='delete',snapshot=current,
            row_identity=self.extra,effect_baseline_ref=self.effect['evidence_ref'])
        self.assertEqual(ledger.queue.prepare_item_action(ledger.context,None,phase='delete',snapshot=current,
            row_identity=self.extra,effect_baseline_ref=self.effect['evidence_ref'])['operation_id'],operation['operation_id'])
        ledger.workflow.issue_operation(operation['operation_id'])
        submitted = files.now
        with patch('shu.workflow_store._utc_now',return_value=datetime.fromtimestamp(submitted,timezone.utc).isoformat()):
            ledger.workflow.enter_submission(operation['operation_id'],queue_guard=operation['queue_guard'])
        files.now += .3
        previous = files.snapshot([files.row()],frame=files.fresh_frame())
        files.now += .2
        applied = receipt.collect_queue_effect_receipt(self.effect,files.binding,files.evidence,
            previous_snapshot=previous,screenshot_refs=[files.fresh_frame()],visible_complete=True,
            operation_id=operation['operation_id'],submitted_at=submitted)
        self.assertTrue(applied['verified'],applied)
        payload = files.payload(applied)
        ledger.queue.register_evidence(ledger.context,[applied['receipt_ref'],self.effect['evidence_ref'],
            *ledger.queue._snapshot_files(ledger.context,previous),
            *ledger.queue._snapshot_files(ledger.context,payload['current_snapshot'])])
        ledger.workflow.management.trash(ledger.context['task_id'])
        result = ledger.queue.record_reconciliation(ledger.context['task_id'],original_epoch=ledger.context['epoch'],
            operation_id=operation['operation_id'],evidence_ref=applied['receipt_ref'],result=applied)
        self.assertEqual(result['status'],'verified')
        self.assertEqual(result['result']['deleted_rows'],[self.extra])
        self.assertIsNone(ledger.workflow.slot_state()['barrier_operation_id'])
        self.assertFalse(ledger.workflow.current(ledger.context['task_id'])['writes_enabled'])

    def test_wrong_source_operation_or_missing_effect_baseline_never_prepares_delete(self):
        ledger = self.ledger
        with self.assertRaises((ValueError,RuntimeError)):
            ledger.queue.prepare_item_action(ledger.context,None,phase='delete',
                snapshot=self.code_proof['current_snapshot'],row_identity=self.extra)
        forged = copy.deepcopy(self.effect)
        forged.pop('evidence_ref')
        forged['source_input']['operation_id'] = 'not-an-operation'
        reference = ledger.publish('forged-effect',forged)
        with self.assertRaises((ValueError,RuntimeError,KeyError)):
            ledger.queue.prepare_item_action(ledger.context,None,phase='delete',
                snapshot=self.code_proof['current_snapshot'],row_identity=self.extra,effect_baseline_ref=reference)

    def test_effect_source_mutation_between_prepare_and_submit_preserves_barrier(self):
        ledger = self.ledger
        operation = ledger.queue.prepare_item_action(ledger.context,None,phase='delete',
            snapshot=self.code_proof['current_snapshot'],row_identity=self.extra,
            effect_baseline_ref=self.effect['evidence_ref'])
        ledger.workflow.issue_operation(operation['operation_id'])
        with ledger.engine._transaction():
            ledger.engine._db.execute("UPDATE workflow_operations SET status='unknown' WHERE operation_id=?",
                                     (self.source_operation['operation_id'],))
        with self.assertRaises((ValueError,RuntimeError)):
            ledger.workflow.enter_submission(operation['operation_id'],queue_guard=operation['queue_guard'])
        self.assertIsNone(ledger.workflow.get_operation(operation['operation_id'])['submitted_at'])
        self.assertEqual(ledger.workflow.slot_state()['barrier_operation_id'],operation['operation_id'])


if __name__=='__main__':
    unittest.main()

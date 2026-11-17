"""Real SQLite queue ownership and submission fences, without GUI or networking."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from shu.core import ActionRejected, TaskEngine
from shu import maa_queue_receipt as evidence
from shu.maa_queue_store import QueueStore, check_queue_guard_in_transaction
from shu.workflow_store import WorkflowStore, _encode


class QueueStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.open_store()
        self.contract = {
            'goal': '导入两个作业',
            'current_deliverable': 'maa_import',
            'allow_start': False,
            'attempt_limit': 0,
            'task_spec': {'kind': 'import'},
        }
        self.context = self.start_task()
        self.plan = self.make_plan()
        self.plan_ref = self.publish('plan', self.plan)
        self.queue.register_plan(self.context, self.plan, self.plan_ref)
        self.snapshot = self.make_snapshot()
        self.queue.record_snapshot(self.context, self.snapshot)

    def open_store(self):
        self.engine = TaskEngine(self.root)
        self.addCleanup(self.engine.close)
        self.workflow = WorkflowStore(self.engine)
        self.queue = QueueStore(self.workflow)

    def start_task(self):
        state = self.workflow.submit(self.contract['goal'], self.contract)
        dispatch = self.workflow.ensure_dispatch(state['task_id'], 'maa', 'queue', {})
        self.workflow.claim_dispatch(dispatch['dispatch_id'], 'test')
        return {**state, 'dispatch_id': dispatch['dispatch_id']}

    def publish(self, kind, value):
        return evidence._publish(self.root / 'task-private' / self.context['task_id']
                                 / 'maa-queue' / str(self.context['epoch']), kind, value)

    def make_plan(self):
        plan = {
            'schema': 'maa_queue_plan/v1',
            **{key: self.context[key] for key in ('task_id', 'revision', 'epoch')},
            'structure_ref': self.publish('structure', {'example': 'catalog'}),
            'targets': [],
            'batches': [],
            'exclusions': [],
            'deferred': [],
        }
        for number in (1, 2):
            binding = self.publish('source', {'number': number})
            document = self.publish('document', {'number': number})
            plan['targets'].append({
                'target_key': f'unit-{number}', 'stage_key': f'unit-{number}',
                'stage_id': f'unit_stage_{number}', 'stage_code': f'UT-{number}',
                'difficulty': 1, 'source_difficulty': 1, 'slot': 'primary',
                'batch_key': 'unit', 'mystery_code': f'prts://{number}',
                'source_binding_ref': binding, 'document_ref': document,
                'candidate_ref': self.publish('candidate', {'number': number}),
                'roster_ref': {'box_revision': 1},
            })
        plan['batches'].append({
            'batch_key': 'unit', 'label': 'unit', 'stage_keys': ['unit-1','unit-2'],
            'target_keys': ['unit-1','unit-2'], 'navigation_scope_ref': plan['structure_ref'],
            'navigation_verified': False,
        })
        plan['plan_sha256'] = _encode(plan)[1]
        return plan

    def make_snapshot(self, at=100):
        projection = {'current': 'unit', 'rows': []}
        frame = Path(self.plan_ref['path']).with_name(f'unit-frame-{at}.png')
        raw = b'unit screenshot placeholder'
        frame.write_bytes(raw)
        snapshot = {
            'schema': 'maa_queue_snapshot/v1',
            'binding': {
                'process': {'hwnd': 1, 'pid': 2, 'creation_time': 3,
                            'executable': str(evidence._MAA_EXECUTABLE)},
                'current_config': 'unit',
                'config_path': str((evidence._MAA_ROOT / 'config/gui.new.json').resolve()),
                'maa_version': evidence.PINNED_VERSION,
            },
            'captured_at': at, 'config_state': {}, 'projection': projection,
            'projection_sha256': _encode(projection)[1], 'rows': [], 'stable': True,
            'screenshot_refs': [{'path':str(frame),'sha256':hashlib.sha256(raw).hexdigest()}],
            'visible_complete': True, 'body_evidence': [],
        }
        snapshot['evidence_ref'] = self.publish('snapshot', snapshot)
        return snapshot

    def action(self,input_baseline_ref=None):
        return self.queue.prepare_item_action(self.context, 'unit-1', phase='code',
                                              snapshot=self.snapshot,input_baseline_ref=input_baseline_ref)

    def test_registration_is_idempotent_but_identity_cannot_change(self):
        first = self.queue.current(self.context['task_id'])
        self.assertEqual(self.queue.register_plan(self.context, self.plan, self.plan_ref), first)
        altered = copy.deepcopy(self.plan)
        altered['exclusions'].append({'why': 'different'})
        del altered['plan_sha256']
        altered['plan_sha256'] = _encode(altered)[1]
        with self.assertRaises(ActionRejected):
            self.queue.register_plan(self.context, altered, self.publish('plan', altered))

    def test_same_planned_action_is_reused_after_reopen(self):
        action = self.action()
        self.engine.close()
        self.open_store()
        self.assertEqual(self.action()['operation_id'], action['operation_id'])
        self.assertEqual(len(self.workflow.pending_operations()), 1)

    def test_later_snapshot_revokes_old_guard_even_if_projection_identical(self):
        action = self.action()
        self.queue.record_snapshot(self.context, self.make_snapshot(101))
        with self.assertRaises(ActionRejected):
            self.queue.submission_guard(self.context, action)

    def test_plus_cannot_be_prepared_before_verified_parse_without_add(self):
        with self.assertRaises(ActionRejected):
            self.queue.prepare_item_action(self.context,'unit-1',phase='add',snapshot=self.snapshot)

    def test_trash_fences_out_ordinary_result_and_submission(self):
        action = self.action()
        self.workflow.issue_operation(action['operation_id'])
        self.workflow.management.trash(self.context['task_id'])
        with self.assertRaises(ActionRejected):
            self.queue.record_item(self.context, {'target_key': 'unit-1', 'status': 'unknown'})
        with self.engine._transaction():
            operation = self.workflow._operation_row(action['operation_id'])
            with self.assertRaises(ActionRejected):
                check_queue_guard_in_transaction(self.workflow, operation, action['queue_guard'], None)
        self.assertIsNone(self.workflow.get_operation(action['operation_id'])['submitted_at'])

    def test_native_only_result_cannot_release_pending_barrier(self):
        action = self.action()
        self.workflow.issue_operation(action['operation_id'])
        with self.assertRaises(ActionRejected):
            self.workflow.enter_submission(action['operation_id'],queue_guard=action['queue_guard'])
        # A test-only recovery fixture representing a committed submit. The
        # source/Box guard integration is covered by the paired workflow tests.
        with self.engine._transaction():
            self.engine._db.execute("UPDATE workflow_operations SET status='submitted',submitted_at='test'"
                                    " WHERE operation_id=?", (action['operation_id'],))
        unverified = self.publish('native', {'status': 'completed'})
        with self.assertRaises(ActionRejected):
            self.queue.record_reconciliation(self.context['task_id'],
                original_epoch=self.context['epoch'], operation_id=action['operation_id'],
                evidence_ref=unverified, result={'status':'completed'})
        with self.assertRaises(ActionRejected):
            self.workflow.finish_operation(action['operation_id'],'verified',{'status':'completed'})
        self.assertEqual(self.workflow.slot_state()['barrier_operation_id'], action['operation_id'])
        second = self.start_task()
        with self.assertRaises(ActionRejected):
            self.workflow.prepare_operation(second['task_id'], second['dispatch_id'],
                                            'second-task', {'kind':'click'})

    def test_actual_import_preparation_and_box_are_checked_in_submission_transaction(self):
        from tests.test_import_preparation import ImportPreparationTests

        fixture = ImportPreparationTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.context = fixture.new_context(allow_start=False,attempt_limit=0,task_spec={'kind':'import'})
        prepared = fixture.prepare()
        context = fixture.use_context(prepared)
        # Existing tests use the same bridge for gameplay; here we issue a
        # queue contract on its normal import path.
        self.assertEqual(context['contract']['current_deliverable'],'maa_import')
        self.root,self.context = fixture.root,context
        self.engine,self.workflow = fixture.engine,fixture.workflow
        self.queue = QueueStore(self.workflow)
        self.plan = self.make_plan()
        target = self.plan['targets'][0]
        record = prepared['candidate_record']
        target.update(stage_key=fixture.stage['stage_key'],stage_id=record['stage_name'],
            mystery_code=prepared['verified_copilot']['mystery_code'],roster_ref=record['roster_ref'],
            document_ref={k:record[k] for k in ('path','sha256')},
            source_binding_ref=record['source_binding']['evidence_ref'],
            candidate_ref=prepared['candidate_ref'])
        self.plan['batches'][0]['stage_keys'][0] = fixture.stage['stage_key']
        self.plan['plan_sha256'] = _encode({k:v for k,v in self.plan.items() if k != 'plan_sha256'})[1]
        self.plan_ref = self.publish('plan',self.plan)
        self.queue.register_plan(context,self.plan,self.plan_ref)
        self.snapshot = self.make_snapshot()
        self.queue.record_snapshot(context,self.snapshot)
        baseline_ref = self.publish('baseline', {'schema':'maa_queue_input_baseline/v1',
            'snapshot':self.snapshot,'binding':self.snapshot['binding'],'target':target})
        action = self.action(baseline_ref)
        self.workflow.issue_operation(action['operation_id'])
        guard = fixture.bridge.submission_guard(context,record)
        with self.engine._transaction():
            row = self.workflow._operation_row(action['operation_id'])
            self.workflow._check_import_guard(row,guard)
            check_queue_guard_in_transaction(self.workflow,row,action['queue_guard'],guard)
        original = copy.deepcopy(guard)
        fixture.publish_box(observed_at=1001)
        with self.assertRaisesRegex(ActionRejected,'Box'):
            self.workflow.enter_submission(action['operation_id'],import_guard=guard,
                                            queue_guard=action['queue_guard'])
        self.assertEqual(guard,original)
        self.assertIsNone(self.workflow.get_operation(action['operation_id'])['submitted_at'])

    def test_trash_then_true_late_receipt_reconciles_only_original_operation(self):
        from tests.test_maa_queue_receipt import QueueReceiptTests, SOURCE

        fixture = QueueReceiptTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.context = self.start_task()
        self.plan = self.make_plan()
        target = self.plan['targets'][0]
        document = self.publish('document',SOURCE)
        target.update(stage_id=SOURCE['stage_name'],source_difficulty=3,
            document_ref=document,mystery_code='prts://123',
            source_semantic_sha256=_encode(evidence.canonical_copilot_document(SOURCE))[1])
        self.plan['plan_sha256'] = _encode({k:v for k,v in self.plan.items() if k != 'plan_sha256'})[1]
        self.plan_ref = self.publish('plan',self.plan)
        self.queue.register_plan(self.context,self.plan,self.plan_ref)
        fixture.evidence = Path(self.plan_ref['path']).parent
        fixture.frame = fixture.fresh_frame()
        self.snapshot = fixture.snapshot([])
        self.queue.record_snapshot(self.context,self.snapshot)
        baseline = evidence.capture_queue_baseline(fixture.binding,target,SOURCE,
                                                   fixture.evidence,self.snapshot)
        action = self.action(baseline['evidence_ref'])
        self.workflow.issue_operation(action['operation_id'])
        submitted = datetime.fromtimestamp(fixture.now,timezone.utc).isoformat()
        with self.engine._transaction():
            self.engine._db.execute("UPDATE workflow_operations SET status='submitted',submitted_at=?"
                                    " WHERE operation_id=?", (submitted,action['operation_id']))
        fixture.now += 1
        fixture.append_success()
        fixture.snapshot([fixture.row()])
        applied = fixture.collect(baseline)
        self.assertTrue(applied['verified'],applied)
        ref = applied['receipt_ref']
        proof = json.loads(Path(ref['path']).read_bytes())
        current = proof['current_snapshot']
        self.queue.register_evidence(self.context,[ref, baseline['evidence_ref'],current['evidence_ref'],
            *current['screenshot_refs'],*[part['ref'] for part in current['body_evidence']]])
        self.workflow.management.trash(self.context['task_id'])
        later = datetime.fromtimestamp(fixture.now + 1,timezone.utc).isoformat()
        with self.engine._transaction():
            self.engine._db.execute('UPDATE workflow_operations SET submitted_at=? WHERE operation_id=?',
                                    (later,action['operation_id']))
        with self.assertRaisesRegex(ActionRejected,'提交时间'):
            self.queue.record_reconciliation(self.context['task_id'],original_epoch=self.context['epoch'],
                operation_id=action['operation_id'],evidence_ref=ref,result={'status':'verified'})
        self.assertEqual(self.workflow.slot_state()['barrier_operation_id'],action['operation_id'])
        with self.engine._transaction():
            self.engine._db.execute('UPDATE workflow_operations SET submitted_at=? WHERE operation_id=?',
                                    (submitted,action['operation_id']))
        receipt = self.queue.record_reconciliation(self.context['task_id'],original_epoch=self.context['epoch'],
            operation_id=action['operation_id'],evidence_ref=ref,result={'status':'verified'})
        self.assertEqual(receipt['status'],'verified')
        self.assertIsNone(self.workflow.slot_state()['barrier_operation_id'])
        self.assertIsNone(self.queue.current(self.context['task_id']))
        self.workflow.management.restore(self.context['task_id'])
        self.assertIsNone(self.queue.current(self.context['task_id']))
        self.assertFalse(self.workflow.current(self.context['task_id'])['writes_enabled'])

    def test_existing_matching_row_is_reused_after_database_reopen(self):
        from tests.test_maa_queue_receipt import QueueReceiptTests, SOURCE

        fixture = QueueReceiptTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.context = self.start_task()
        self.plan = self.make_plan()
        target = self.plan['targets'][0]
        target.update(stage_id=SOURCE['stage_name'],source_difficulty=3,mystery_code='prts://123',
            source_semantic_sha256=_encode(evidence.canonical_copilot_document(SOURCE))[1])
        self.plan['plan_sha256'] = _encode({k:v for k,v in self.plan.items() if k != 'plan_sha256'})[1]
        self.plan_ref = self.publish('plan',self.plan)
        self.queue.register_plan(self.context,self.plan,self.plan_ref)
        fixture.evidence = Path(self.plan_ref['path']).parent
        fixture.frame = fixture.fresh_frame()
        snapshot = fixture.snapshot([fixture.row()])
        self.queue.record_snapshot(self.context,snapshot)
        result = {'target_key':'unit-1','status':'reused','row_identity':snapshot['rows'][0]['identity']}
        item = self.queue.record_item(self.context,result)
        self.assertFalse(item['owned'])
        self.engine.close()
        self.open_store()
        self.assertEqual(self.queue.current(self.context['task_id'])['items'][0],item)
        self.assertEqual(self.queue.record_item(self.context,result),item)
        self.assertEqual(self.workflow.pending_operations(),[])
        with self.assertRaises(ActionRejected):
            self.queue.prepare_item_action(self.context,None,phase='delete',snapshot=snapshot,
                                           row_identity=item['row_identity'])

    def test_matching_row_added_after_baseline_cannot_be_claimed_as_reused(self):
        from tests.test_maa_queue_receipt import QueueReceiptTests, SOURCE

        fixture = QueueReceiptTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.context = self.start_task()
        self.plan = self.make_plan()
        target = self.plan['targets'][0]
        target.update(stage_id=SOURCE['stage_name'],source_difficulty=3,mystery_code='prts://123',
            source_semantic_sha256=_encode(evidence.canonical_copilot_document(SOURCE))[1])
        self.plan['plan_sha256'] = _encode({k:v for k,v in self.plan.items() if k != 'plan_sha256'})[1]
        self.plan_ref = self.publish('plan',self.plan)
        self.queue.register_plan(self.context,self.plan,self.plan_ref)
        fixture.evidence = Path(self.plan_ref['path']).parent
        fixture.frame = fixture.fresh_frame()
        self.queue.record_snapshot(self.context,fixture.snapshot([]))
        fixture.now += 1
        fixture.frame = fixture.fresh_frame()
        snapshot = fixture.snapshot([fixture.row()])
        self.queue.record_snapshot(self.context,snapshot)
        with self.assertRaises(ActionRejected):
            self.queue.record_item(self.context,{'target_key':'unit-1','status':'reused',
                'row_identity':snapshot['rows'][0]['identity']})

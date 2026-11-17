"""Cross-task battle submission rechecks only the explicitly pinned import in SQLite."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from shu.battle_contract import authorize, authorized
from shu.core import ActionRejected, TaskEngine
from shu.workflow_store import WorkflowStore, _encode
from tests import test_battle_import_binding as fixtures


class CrossTaskBattleGateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BattleImportBindingTests('test_completed_actual_import_becomes_frozen_non_authorizing_pin')
        self.fixture.import_difficulty = 2
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine = TaskEngine(self.fixture.data)
        self.addCleanup(self.engine.close)
        self.store = WorkflowStore(self.engine)
        self.source_id = self.fixture.tid
        self.source = self.store.current(self.source_id)
        request = '现在开始MAA自动战斗，运行刚才导入的UT-EX-1突袭作业一次，不使用理智药或源石'
        self.contract = authorize({'user_request': request, 'current_deliverable': 'gameplay',
            'source_task_id': self.source_id, 'target_stage_codes': ['UT-EX-1'],
            'task_spec': {'kind': 'repeat_stage', 'stage_code': 'UT-EX-1', 'repeat_count': 1,
                          'medicine_limit': 0, 'premium_limit': 0}})
        self.assertTrue(authorized(self.contract))
        self.target = self.store.submit(request, self.contract)
        self.dispatch = self.store.ensure_dispatch(self.target['task_id'], 'maa', 'battle',
                                                   {'contract': self.contract})
        self.store.claim_dispatch(self.dispatch['dispatch_id'], 'fixture')
        self.binding = {'task_id': self.source_id, 'revision': self.source['revision'],
            'epoch': self.source['epoch'], 'dispatch_id': self.fixture.import_dispatch_id,
            'receipt_ref': self.fixture.proof_ref}
        self.action = {'kind': 'start', 'target': 'battle_start',
            'source_import_task_id': self.source_id, 'import_proof_ref': self.fixture.proof_ref,
            'source_import_binding': copy.deepcopy(self.binding)}
        self.sequence = 0

    def prepare(self, action=None):
        self.sequence += 1
        return self.store.prepare_operation(self.target['task_id'], self.dispatch['dispatch_id'],
            f'related:start:{self.sequence}', action or self.action, starts=True)

    def rewrite_receipt(self, **changes):
        receipt = {**self.fixture.receipt, **changes}
        payload, digest = _encode(receipt)
        with self.engine._transaction():
            self.engine._db.execute('UPDATE workflow_results SET result=?,payload_hash=? WHERE dispatch_id=?',
                (payload, digest, self.fixture.import_dispatch_id))

    def test_new_gameplay_authority_can_use_completed_non_starting_import(self):
        self.assertFalse(self.source['contract']['allow_start'])
        operation = self.prepare()
        self.store.issue_operation(operation['operation_id'])
        submitted = self.store.enter_submission(operation['operation_id'])
        self.assertIsNotNone(submitted['submitted_at'])
        self.assertEqual(submitted['task_id'], self.target['task_id'])
        self.assertFalse(self.store.current(self.source_id)['contract']['allow_start'])

    def test_ordinary_import_dispatch_without_execution_step_remains_valid(self):
        payload, digest = _encode({'contract': self.source['contract']})
        with self.engine._transaction():
            self.engine._db.execute('UPDATE workflow_dispatches SET payload=?,payload_hash=? WHERE dispatch_id=?',
                (payload, digest, self.fixture.import_dispatch_id))
        operation = self.prepare()
        self.store.issue_operation(operation['operation_id'])
        self.assertIsNotNone(self.store.enter_submission(operation['operation_id'])['submitted_at'])

    def test_source_change_after_worker_validation_is_rejected_at_issue(self):
        for control in ('trash', 'stop', 'resume'):
            with self.subTest(control=control):
                case = type(self)('test_new_gameplay_authority_can_use_completed_non_starting_import')
                case.setUp()
                try:
                    operation = case.prepare()
                    if control == 'trash':
                        case.store.management.trash(case.source_id)
                    else:
                        case.store.control(case.source_id, control)
                    with self.assertRaises(ActionRejected):
                        case.store.issue_operation(operation['operation_id'])
                    unchanged = case.store.get_operation(operation['operation_id'])
                    self.assertIsNone(unchanged['issued_at'])
                    self.assertIsNone(unchanged['submitted_at'])
                finally:
                    case.doCleanups()

    def test_source_change_after_issue_is_rejected_before_submission(self):
        for control in ('trash', 'stop', 'resume'):
            with self.subTest(control=control):
                case = type(self)('test_new_gameplay_authority_can_use_completed_non_starting_import')
                case.setUp()
                try:
                    operation = case.prepare()
                    case.store.issue_operation(operation['operation_id'])
                    if control == 'trash':
                        case.store.management.trash(case.source_id)
                    else:
                        case.store.control(case.source_id, control)
                    with self.assertRaises(ActionRejected):
                        case.store.enter_submission(operation['operation_id'])
                    self.assertIsNone(case.store.get_operation(operation['operation_id'])['submitted_at'])
                finally:
                    case.doCleanups()

    def test_new_source_dispatch_invalidates_old_binding_after_issue(self):
        operation = self.prepare()
        self.store.issue_operation(operation['operation_id'])
        # A completed source cannot normally schedule writes. Simulate a stale
        # or independently inserted newer dispatch without changing its state.
        with self.engine._transaction():
            self.engine._db.execute('INSERT INTO workflow_dispatches '
                '(dispatch_id,task_id,revision,epoch,role,dispatch_key,payload,payload_hash,status,created_at) '
                "SELECT dispatch_id||':new',task_id,revision,epoch,role,dispatch_key||':new',payload,payload_hash,'pending',created_at "
                'FROM workflow_dispatches WHERE dispatch_id=?', (self.fixture.import_dispatch_id,))
        with self.assertRaises(ActionRejected):
            self.store.enter_submission(operation['operation_id'])
        self.assertIsNone(self.store.get_operation(operation['operation_id'])['submitted_at'])

    def test_source_proof_reference_change_after_issue_closes_submission(self):
        operation = self.prepare()
        self.store.issue_operation(operation['operation_id'])
        replacement = {'path': str(self.fixture.data / 'different-proof.json'), 'sha256': 'a' * 64}
        self.rewrite_receipt(receipt_ref=replacement,
                             evidence_refs=[*self.fixture.receipt['evidence_refs'], replacement])
        with self.assertRaises(ActionRejected):
            self.store.enter_submission(operation['operation_id'])
        self.assertIsNone(self.store.get_operation(operation['operation_id'])['submitted_at'])

    def test_source_import_operation_change_after_issue_closes_submission(self):
        operation = self.prepare()
        self.store.issue_operation(operation['operation_id'])
        with self.engine._transaction():
            self.engine._db.execute("UPDATE workflow_operations SET status='unknown' WHERE task_id=?", (self.source_id,))
        with self.assertRaises(ActionRejected):
            self.store.enter_submission(operation['operation_id'])
        self.assertIsNone(self.store.get_operation(operation['operation_id'])['submitted_at'])

    def test_source_revision_change_while_receipt_remains_rejects_submit(self):
        operation = self.prepare()
        self.store.issue_operation(operation['operation_id'])
        with self.engine._transaction():
            self.engine._db.execute('INSERT INTO workflow_contracts '
                'SELECT task_id,revision+1,command_id||\':revision\',goal,contract,payload_hash,created_at '
                'FROM workflow_contracts WHERE task_id=? AND revision=?', (self.source_id,self.source['revision']))
            self.engine._db.execute('UPDATE workflow_tasks SET revision=revision+1 WHERE task_id=?', (self.source_id,))
        with self.assertRaises(ActionRejected):
            self.store.enter_submission(operation['operation_id'])
        self.assertIsNone(self.store.get_operation(operation['operation_id'])['submitted_at'])

    def test_binding_must_match_explicit_source_and_exact_generation(self):
        for field, value in (('task_id', 'unrelated-source'), ('revision', 2), ('epoch', 2),
                             ('epoch', True), ('dispatch_id', 'unrelated-dispatch')):
            action = copy.deepcopy(self.action)
            action['source_import_binding'][field] = value
            with self.subTest(field=field,value=value), self.assertRaises(ActionRejected):
                self.prepare(action)

    def test_related_start_requires_binding_and_consistent_aliases(self):
        for field, value in (('source_import_binding', None), ('source_import_task_id', 'other-task'),
            ('import_proof_ref', {'path': str(self.fixture.data/'other.json'), 'sha256':'a'*64})):
            action = {**self.action, field: value}
            with self.subTest(field=field), self.assertRaises(ActionRejected):
                self.prepare(action)

    def test_queue_result_cannot_replace_single_import_receipt(self):
        operation = self.prepare()
        self.store.issue_operation(operation['operation_id'])
        self.rewrite_receipt(queue_receipt_ref={'path':'queue.json','sha256':'a'*64})
        with self.assertRaises(ActionRejected):
            self.store.enter_submission(operation['operation_id'])

    def test_database_hash_tamper_is_rejected_before_input(self):
        operation = self.prepare()
        self.store.issue_operation(operation['operation_id'])
        with self.engine._transaction():
            self.engine._db.execute("UPDATE workflow_results SET payload_hash=? WHERE dispatch_id=?",
                                    ('a'*64, self.fixture.import_dispatch_id))
        with self.assertRaises(ActionRejected):
            self.store.enter_submission(operation['operation_id'])

    def test_issue_and_submission_never_rehash_files_inside_sqlite_gate(self):
        operation = self.prepare()
        with patch.object(Path, 'open', side_effect=AssertionError('No filesystem IO inside SQLite admission')):
            with patch.object(self.store, '_battle_reference', side_effect=AssertionError('Worker already verified bytes')):
                self.store.issue_operation(operation['operation_id'])
                submitted = self.store.enter_submission(operation['operation_id'])
        self.assertIsNotNone(submitted['submitted_at'])


if __name__ == '__main__':
    unittest.main()

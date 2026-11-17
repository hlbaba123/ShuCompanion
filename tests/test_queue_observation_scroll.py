"""Queue observation scrolling preserves the submitted mutation's barrier."""
import unittest
import threading
from unittest.mock import Mock

from shu.core import ActionRejected


class QueueObservationScrollTests(unittest.TestCase):
    def test_downward_scan_stops_after_one_unchanged_nonbottom_frame(self):
        from shu.maa_queue_import import QueueCodeImporter, QueueImportBlocked
        importer = object.__new__(QueueCodeImporter)
        importer._require_page = Mock()
        frame = {'ok':True, 'queue_mode':True, 'image_sha256':'b'*64,
                 'projection_sha256':'a'*64, 'rows_measured':True,
                 'queue_rows':[{'name':f'UNIT-{i}', 'mode':1, 'is_checked':True}
                               for i in range(20)],
                 'scroll_state':{'top':True, 'bottom':False}}
        snapshot = {'projection_sha256':'a'*64,
                    'rows':[{'name':f'UNIT-{i}'} for i in range(25)],
                    'screenshot_refs':[], 'evidence_ref':{}}
        importer._scroll = Mock(return_value=(frame, snapshot))
        with self.assertRaises(QueueImportBlocked) as rejected:
            importer._scan_queue({},frame,snapshot,{},None,threading.Event())
        self.assertEqual(rejected.exception.code,'queue_scroll_no_progress')
        importer._scroll.assert_called_once()

    def fixture(self):
        from tests.test_maa_queue_store import QueueStoreTests
        fixture = QueueStoreTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        operation = fixture.action()
        fixture.workflow.issue_operation(operation['operation_id'])
        # Fixture represents a code already dispatched to the external client.
        with fixture.engine._transaction():
            fixture.engine._db.execute("UPDATE workflow_operations SET status='submitted',submitted_at='fixture' WHERE operation_id=?",
                                       (operation['operation_id'],))
        return fixture,operation

    def test_same_owner_observation_scroll_keeps_parent_barrier_until_reconciliation(self):
        fixture,parent = self.fixture()
        scroll = fixture.queue.prepare_item_action(fixture.context,None,phase='scroll',snapshot=fixture.snapshot)
        self.assertEqual(scroll['action']['queue_observation_of'],parent['operation_id'])
        fixture.workflow.issue_operation(scroll['operation_id'])
        fixture.workflow.enter_submission(scroll['operation_id'],queue_guard=scroll['queue_guard'])
        self.assertEqual(fixture.workflow.slot_state()['barrier_operation_id'],parent['operation_id'])
        fixture.workflow.finish_operation(scroll['operation_id'],'verified',{'receipt':'native_scroll_dispatched'})
        self.assertEqual(fixture.workflow.slot_state()['barrier_operation_id'],parent['operation_id'])
        self.assertEqual(fixture.workflow.get_operation(parent['operation_id'])['status'],'submitted')

    def test_code_and_option_changes_cannot_use_observation_exception(self):
        fixture,parent = self.fixture()
        for phase in ('options','mode','focus'):
            with self.subTest(phase=phase),self.assertRaises(ActionRejected):
                fixture.queue.prepare_item_action(fixture.context,None,phase=phase,snapshot=fixture.snapshot)
        self.assertEqual(fixture.workflow.slot_state()['barrier_operation_id'],parent['operation_id'])

    def test_revocation_rejects_prepared_scroll_and_retains_original_unknown(self):
        fixture,parent = self.fixture()
        scroll = fixture.queue.prepare_item_action(fixture.context,None,phase='scroll',snapshot=fixture.snapshot)
        fixture.workflow.issue_operation(scroll['operation_id'])
        fixture.workflow.management.trash(fixture.context['task_id'])
        with self.assertRaises(ActionRejected):
            fixture.workflow.enter_submission(scroll['operation_id'],queue_guard=scroll['queue_guard'])
        self.assertEqual(fixture.workflow.slot_state()['barrier_operation_id'],parent['operation_id'])
        self.assertEqual(fixture.workflow.get_operation(parent['operation_id'])['status'],'unknown')


if __name__=='__main__':
    unittest.main()

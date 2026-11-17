"""Regression for a Box update after evidence checking but before commit."""
import unittest
from unittest.mock import patch

from tests.test_code_pair_search import PairIntegration
from shu.code_delivery import CodeDeliveryStore
from shu.core import ActionRejected
from shu import planned_agents


class BoxCommitRaceTests(unittest.TestCase):
    setUp = PairIntegration.setUp
    candidate = PairIntegration.candidate
    ref = PairIntegration.ref
    screen = PairIntegration.screen

    def _primary(self):
        candidate = self.candidate(1)
        pool = self.ref([candidate], True)
        return self.store.register_code(self.lease, candidate['candidate_locator'], self.screen(pool))

    def _change_box(self):
        self.rosters.facts.publish('local-default', [{'name': '甲', 'owned': False}], True,
            {'kind': 'user-roster-spreadsheet', 'observed_at': 2000,
             'evidence_ref': 'fixture:concurrent-update'}, explicit_ownership=True)

    def test_close_rejects_box_update_after_read_code(self):
        receipt = self._primary()
        original = CodeDeliveryStore.read_code

        def change_after_verified(store, *args, **kwargs):
            result = original(store, *args, **kwargs)
            self._change_box()
            return result

        with patch.object(CodeDeliveryStore, 'read_code', change_after_verified):
            with self.assertRaises(ActionRejected):
                self.plans.close_step(self.lease, {'status': 'primary_only', 'primary_ref': receipt})
        self.assertEqual(self.plans.current(self.task)['steps'][0]['status'], 'pending')
        self.assertFalse(self.store.summary(self.task)['complete'])

    def test_completion_rejects_box_update_after_verified_summary(self):
        receipt = self._primary()
        self.plans.close_step(self.lease, {'status': 'primary_only', 'primary_ref': receipt})
        self.workflow.finish_dispatch(self.lease['dispatch_id'], {'status': 'researched'})
        original = planned_agents.verify_planned_completion

        def change_after_verified(*args, **kwargs):
            result = original(*args, **kwargs)
            self.assertTrue(result['complete'])
            self._change_box()
            return result

        with patch.object(planned_agents, 'verify_planned_completion', change_after_verified):
            with self.assertRaises(ActionRejected):
                self.workflow.complete_planned_verified(self.task, expected_epoch=1)
        self.assertNotEqual(self.workflow.current(self.task)['status'], 'completed')
        self.assertFalse(self.store.summary(self.task)['complete'])

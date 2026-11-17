from tests.test_leader_plan import LeaderPlanTests
from shu.core import ActionRejected


class RetryPlanTests(LeaderPlanTests):
    def test_terminal_dispatch_lease_can_be_reclaimed_without_resetting_budget(self):
        self.register()
        first = self.claim()
        self.plans.reserve_budget(first, phase='primary', pages=1, milliseconds=9000)
        with self.assertRaises(ActionRejected):
            self.claim('replacement')
        self.workflow.finish_dispatch(first['dispatch_id'], {'status': 'failed'}, status='failed')
        replacement = self.claim('replacement')
        self.assertEqual(replacement['step_id'], first['step_id'])
        self.assertNotEqual(replacement['lease_id'], first['lease_id'])
        self.assertEqual(replacement['budget']['primary']['pages_remaining'], 3)
        self.assertEqual(replacement['budget']['primary']['milliseconds_remaining'], 21000)
        with self.assertRaises(ActionRejected):
            self.plans.close_step(first, {'status': 'search_incomplete'})

    def test_retry_only_failed_once_with_separate_durable_allowance(self):
        self.register()
        first=self.claim()
        spend=self.plans.reserve_budget(first,phase='primary',pages=4,milliseconds=30000)
        self.plans.close_step(first,{'status':'search_incomplete','backup_reason':'network_error'})
        second=self.claim('second')
        self.plans.close_step(second,{'status':'no_box_match'})
        job=self.workflow.ensure_dispatch(self.task_id,'research','retry',{})
        args=dict(expected_revision=1,expected_epoch=1,dispatch_id=job['dispatch_id'])
        reopened=self.plans.retry_failed(self.task_id,**args)
        self.assertEqual(reopened,[first['step_id']])
        lease=self.plans.claim_next(self.task_id,**args)
        self.assertEqual(lease['attempt'],2)
        self.assertEqual(self.plans.budget(lease)['primary']['pages_remaining'],4)
        self.assertEqual(self.plans.budget(lease)['primary']['milliseconds_remaining'],30000)
        with self.assertRaises(ActionRejected):
            self.plans.close_step(first,{'status':'search_incomplete'})
        self.plans.close_step(lease,{'status':'search_incomplete'})
        self.assertEqual(self.plans.retry_failed(self.task_id,**args),[])
        self.assertEqual(self.engine._db.execute('SELECT COUNT(*) FROM leader_step_attempts').fetchone()[0],3)
        self.assertIsNone(self.engine._db.execute('SELECT elapsed_ms FROM leader_budget WHERE reservation_id=?',
            (spend['reservation_id'],)).fetchone()[0])

    def test_pause_does_not_reset_second_round_budget(self):
        self.register(); first=self.claim()
        self.plans.close_step(first,{'status':'search_incomplete'})
        next_=self.claim('next');self.plans.close_step(next_,{'status':'no_box_match'})
        job=self.workflow.ensure_dispatch(self.task_id,'research','retry',{})
        args=dict(expected_revision=1,expected_epoch=1,dispatch_id=job['dispatch_id'])
        self.plans.retry_failed(self.task_id,**args)
        lease=self.plans.claim_next(self.task_id,**args)
        self.plans.reserve_budget(lease,phase='primary',pages=4,milliseconds=30000)
        self.workflow.control(self.task_id,'pause');state=self.workflow.control(self.task_id,'resume')
        job=self.workflow.ensure_dispatch(self.task_id,'research','resume',{})
        lease=self.plans.claim_next(self.task_id,expected_revision=1,expected_epoch=state['epoch'],dispatch_id=job['dispatch_id'])
        self.assertEqual(lease['attempt'],2)
        self.assertEqual(self.plans.budget(lease)['primary']['pages_remaining'],0)

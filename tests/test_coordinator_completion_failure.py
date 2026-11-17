"""Unexpected completion storage errors cannot strand a running product task."""
import sqlite3
import threading
import unittest
from unittest.mock import patch

from tests import test_maa_coordinator as fixtures
from shu.providers import ProviderError

APP = fixtures.APP
wait_for = fixtures.wait_for


class CompletionFailureTests(unittest.TestCase):
    setUp = fixtures.MaaCoordinatorTests.setUp
    tearDown = fixtures.MaaCoordinatorTests.tearDown
    make_coordinator = fixtures.MaaCoordinatorTests.make_coordinator

    def start_research(self):
        coordinator = self.make_coordinator()
        self.workflows.result = {'status': 'research_verified', 'summary': '不能直接相信的模型成功',
                                 'role_results': {'research': {'evidence_refs': []}}}
        results, events = [], []
        coordinator.finished.connect(results.append)
        coordinator.event.connect(lambda kind, summary, facts: events.append((kind, summary, facts)))
        outcome = coordinator.submit('只读查询公开资料', {'goal': '只读查询公开资料',
                                                      'task_spec': {'kind': 'lookup'}})
        self.assertTrue(self.workflows.entered.wait(1))
        return coordinator, outcome, coordinator._job, results, events

    def test_storage_exception_finishes_unverified_revokes_and_redacts(self):
        coordinator, outcome, job, results, events = self.start_research()
        with patch.object(self.store, 'complete_research_verified',
                          side_effect=sqlite3.OperationalError('fixture DB failure Bearer sk-private-fixture')):
            self.workflows.release.set()
            wait_for(lambda: not coordinator.active)
        APP.processEvents()
        state = self.store.current(outcome['task_id'])
        self.assertFalse(state['writes_enabled'])
        self.assertEqual(state['status'], 'needs_input')
        self.assertTrue(job.done.is_set())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['status'], 'needs_input')
        self.assertEqual(results[0]['code'], 'completion_persistence_error')
        self.assertIn('OperationalError', results[0]['summary'])
        self.assertNotIn('sk-private', results[0]['summary'])
        self.assertNotIn('不能直接相信', results[0]['summary'])
        self.assertTrue(any(kind == 'task_waiting' for kind, _, _ in events))
        self.assertFalse(any(kind == 'task_finished' for kind, _, _ in events))

    def test_stop_wins_during_a_late_failing_completion(self):
        coordinator, outcome, job, results, events = self.start_research()
        entered, release = threading.Event(), threading.Event()
        def failing(*args, **kwargs):
            entered.set()
            release.wait(2)
            raise sqlite3.OperationalError('late completion failure')
        with patch.object(self.store, 'complete_research_verified', side_effect=failing):
            self.workflows.release.set()
            self.assertTrue(entered.wait(1))
            coordinator.stop()
            release.set()
            wait_for(lambda: not coordinator.active)
        APP.processEvents()
        state = self.store.current(outcome['task_id'])
        self.assertEqual(state['status'], 'cancelled')
        self.assertFalse(state['writes_enabled'])
        self.assertTrue(job.done.is_set())
        self.assertFalse(results)
        self.assertFalse(any(kind in {'task_finished', 'task_waiting'} for kind, _, _ in events))

    def test_projection_failure_still_releases_the_coordinator(self):
        coordinator, outcome, job, results, events = self.start_research()
        # A failed evidence gate first suspends/revokes. Failure to save the
        # disposable UI projection must not leave execution.active stuck true.
        with patch.object(coordinator, '_project', side_effect=RuntimeError('fixture projection unavailable')):
            self.workflows.result = {'status': 'needs_input', 'summary': '缺少有效产物'}
            self.workflows.release.set()
            wait_for(lambda: not coordinator.active)
        APP.processEvents()
        self.assertFalse(self.store.current(outcome['task_id'])['writes_enabled'])
        self.assertTrue(job.done.is_set())
        self.assertEqual(results[0]['status'], 'needs_input')
        self.assertIn('projection unavailable', results[0]['summary'])

    def test_failed_recovery_never_claims_revocation_and_blocks_new_execution(self):
        coordinator, outcome, job, results, events = self.start_research()
        with patch.object(self.store, 'complete_research_verified',
                          side_effect=sqlite3.OperationalError('fixture completion storage unavailable')), \
             patch.object(self.store, 'suspend', side_effect=sqlite3.OperationalError('fixture revocation unavailable')):
            self.workflows.release.set()
            wait_for(lambda: not coordinator.active)
            APP.processEvents()
            self.assertTrue(job.done.is_set())
            self.assertEqual(results[0]['status'], 'needs_input')
            self.assertIn('撤权持久化也未能确认', results[0]['summary'])
            self.assertTrue(all(role.closed for role in self.roles))
            with self.assertRaises(ProviderError):
                coordinator.submit('不要在未知存储状态下重启输入')


if __name__ == '__main__':
    unittest.main()

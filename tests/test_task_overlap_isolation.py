"""Overlapping user tasks retain independent identities, memory and checkpoints.

SQLite authority, coordinator scheduling and LangGraph persistence are real.
The role/workflow doubles below never search, capture a screen or send input.
"""
import copy
import threading
import time
import unittest

from shu.memory import ConversationMemory
from shu.task_context import task_context
from tests import test_maa_coordinator as fixtures


APP = fixtures.APP
wait_for = fixtures.wait_for


class GatedWorkflows:
    """Block named executions independently while keeping their state visible."""

    def __init__(self):
        self.release = threading.Event()
        self.gates = {}
        self.entered = {}
        self.results = {}
        self.errors = {}
        self.runs = []
        self.instances = []
        self.running = 0
        self.maximum_running = 0

    def prepare(self, name, *, result=None, error=None):
        self.gates[name] = threading.Event()
        self.entered[name] = threading.Event()
        self.results[name] = result or {'status': 'needs_input', 'summary': name + '待补充'}
        if error is not None:
            self.errors[name] = error

    def __call__(self, store, directory, handlers, *, emit, cancel_event):
        owner = self

        class Workflow:
            def run(self, task_id):
                state = store.current(task_id)
                name = state['contract']['request_id']
                owner.runs.append({'task_id': task_id, 'epoch': state['epoch'],
                                   'contract': copy.deepcopy(state['contract']),
                                   'cancel': cancel_event})
                owner.running += 1
                owner.maximum_running = max(owner.maximum_running, owner.running)
                owner.entered[name].set()
                try:
                    deadline = time.monotonic() + 5
                    while not owner.release.is_set() and not owner.gates[name].wait(.005):
                        if time.monotonic() >= deadline:
                            raise TimeoutError('test did not release ' + name)
                    if name in owner.errors:
                        raise owner.errors[name]
                    return copy.deepcopy(owner.results[name])
                finally:
                    owner.running -= 1

            def close(self):
                self.closed = True

        instance = Workflow()
        self.instances.append(instance)
        return instance


class TaskOverlapIsolationTests(unittest.TestCase):
    setUp = fixtures.MaaCoordinatorTests.setUp
    tearDown = fixtures.MaaCoordinatorTests.tearDown
    make_coordinator = fixtures.MaaCoordinatorTests.make_coordinator

    def begin(self, *names):
        self.workflows = GatedWorkflows()
        for name in names:
            self.workflows.prepare(name)
        return self.make_coordinator()

    def submit(self, coordinator, name):
        return coordinator.submit('只读任务' + name, {
            'relation': 'new', 'request_id': name, 'goal': '只读任务' + name,
            'constraints': ['只属于' + name], 'task_spec': {'kind': 'lookup'},
            'current_deliverable': 'stage_catalog'})

    def finish(self, name):
        self.workflows.gates[name].set()

    def test_a_running_then_b_and_c_queue_in_order_without_losing_task_or_context(self):
        coordinator = self.begin('A', 'B', 'C')
        results = []
        coordinator.finished.connect(results.append)
        first = self.submit(coordinator, 'A')
        self.assertTrue(self.workflows.entered['A'].wait(1))
        second = self.submit(coordinator, 'B')
        third = self.submit(coordinator, 'C')
        ids = [item['task_id'] for item in (first, second, third)]
        self.assertEqual(len(set(ids)), 3)
        self.assertFalse(self.workflows.entered['B'].is_set())
        self.assertFalse(self.workflows.entered['C'].is_set())
        self.assertFalse(self.workflows.runs[0]['cancel'].is_set())

        # Save visible task-owned transcripts while all three task contracts
        # coexist; selecting the latest task must not merge A/B/C history.
        memory = ConversationMemory(self.directory)
        try:
            for name, tid in zip(('A', 'B', 'C'), ids):
                for role in ('user', 'assistant'):
                    memory.append(role, name + '-独有记录-' + role,
                                  request_id='history-' + name, task_id=tid)
            for name, tid in zip(('A', 'B', 'C'), ids):
                view = task_context(memory, self.store, tid)
                self.assertEqual(view['task_id'], tid)
                self.assertIn(name + '-独有记录', view['history'])
                for other in set('ABC') - {name}:
                    self.assertNotIn(other + '-独有记录', view['history'])
        finally:
            memory.close()

        self.finish('A')
        wait_for(lambda: self.workflows.entered['B'].is_set())
        self.assertFalse(self.workflows.entered['C'].is_set())
        self.assertEqual(self.store.current(ids[0])['status'], 'needs_input')
        self.assertTrue(self.store.current(ids[1])['writes_enabled'])
        self.finish('B')
        wait_for(lambda: self.workflows.entered['C'].is_set())
        self.finish('C')
        wait_for(lambda: not coordinator.active)
        APP.processEvents()
        self.assertEqual([run['task_id'] for run in self.workflows.runs], ids)
        self.assertEqual(self.workflows.maximum_running, 1)
        self.assertEqual([result['task_id'] for result in results], ids)
        for name, run, tid in zip(('A', 'B', 'C'), self.workflows.runs, ids):
            self.assertEqual(run['contract']['request_id'], name)
            self.assertEqual(run['contract']['constraints'], ['只属于' + name])
            self.assertEqual(self.store.current(tid)['status'], 'needs_input')
        self.assertEqual(self.store.pending_operations(), [])

    def test_a_waiting_for_user_does_not_block_a_new_independent_b(self):
        coordinator = self.begin('A', 'B')
        self.workflows.results['A']['waiting_kind'] = 'business'
        first = self.submit(coordinator, 'A')
        self.assertTrue(self.workflows.entered['A'].wait(1))
        self.finish('A')
        wait_for(lambda: not coordinator.active)
        before = copy.deepcopy(self.store.current(first['task_id']))
        self.assertEqual(before['status'], 'needs_input')
        self.assertFalse(before['writes_enabled'])
        second = self.submit(coordinator, 'B')
        wait_for(lambda: self.workflows.entered['B'].is_set())
        self.assertNotEqual(first['task_id'], second['task_id'])
        self.assertEqual(self.store.current(first['task_id']), before)
        self.finish('B')
        wait_for(lambda: not coordinator.active)

    def test_stop_a_during_overlap_does_not_revoke_queued_b_or_emit_late_a_result(self):
        coordinator = self.begin('A', 'B')
        results = []
        coordinator.finished.connect(results.append)
        first = self.submit(coordinator, 'A')
        self.assertTrue(self.workflows.entered['A'].wait(1))
        second = self.submit(coordinator, 'B')
        b_before = copy.deepcopy(self.store.current(second['task_id']))
        coordinator.stop(first['task_id'])
        self.assertTrue(self.workflows.runs[0]['cancel'].is_set())
        self.assertEqual(self.store.current(first['task_id'])['status'], 'cancelled')
        self.assertEqual(self.store.current(second['task_id']), b_before)
        self.finish('A')
        wait_for(lambda: self.workflows.entered['B'].is_set())
        self.assertFalse(self.workflows.runs[1]['cancel'].is_set())
        self.assertTrue(self.store.current(second['task_id'])['writes_enabled'])
        self.finish('B')
        wait_for(lambda: not coordinator.active)
        APP.processEvents()
        self.assertEqual([item['task_id'] for item in results], [second['task_id']])
        self.assertTrue(all(not role.stops for role in self.roles))

    def test_a_workflow_exception_does_not_strand_already_queued_b(self):
        coordinator = self.begin('A', 'B')
        self.workflows.errors['A'] = RuntimeError('A模拟只读查询失败')
        results = []
        coordinator.finished.connect(results.append)
        first = self.submit(coordinator, 'A')
        self.assertTrue(self.workflows.entered['A'].wait(1))
        second = self.submit(coordinator, 'B')
        self.finish('A')
        wait_for(lambda: self.workflows.entered['B'].is_set())
        self.assertEqual(self.store.current(first['task_id'])['status'], 'needs_input')
        self.assertFalse(self.store.current(first['task_id'])['writes_enabled'])
        self.assertTrue(self.store.current(second['task_id'])['writes_enabled'])
        self.finish('B')
        wait_for(lambda: not coordinator.active)
        APP.processEvents()
        self.assertEqual([item['task_id'] for item in results],
                         [first['task_id'], second['task_id']])
        self.assertIn('A模拟只读查询失败', results[0]['summary'])
        self.assertNotIn('A模拟只读查询失败', results[1]['summary'])

    def test_new_b_after_a_has_failed_keeps_a_history_without_resuming_it(self):
        coordinator = self.begin('A', 'B')
        self.workflows.errors['A'] = RuntimeError('A已失败')
        first = self.submit(coordinator, 'A')
        self.assertTrue(self.workflows.entered['A'].wait(1))
        self.finish('A')
        wait_for(lambda: not coordinator.active)
        a_before = copy.deepcopy(self.store.current(first['task_id']))
        second = self.submit(coordinator, 'B')
        wait_for(lambda: self.workflows.entered['B'].is_set())
        self.finish('B')
        wait_for(lambda: not coordinator.active)
        self.assertEqual(self.store.current(first['task_id']), a_before)
        self.assertEqual([run['task_id'] for run in self.workflows.runs],
                         [first['task_id'], second['task_id']])

    def test_real_graph_checkpoints_are_thread_scoped_and_do_not_replay_other_tasks(self):
        from shu.maa_graph import MaaWorkflow

        first = self.store.submit('A独立流程', {'goal': 'A独立流程', 'request_id': 'A'})
        second = self.store.submit('B独立流程', {'goal': 'B独立流程', 'request_id': 'B'})
        calls = []

        def leader(context, cancel):
            calls.append(copy.deepcopy(context))
            marker = context['contract']['request_id']
            return {'next_role': 'ask', 'ask_kind': 'business', 'summary': marker + '独立澄清问题'}

        graph = MaaWorkflow(self.store, self.directory, {'leader': leader})
        try:
            a_result = graph.run(first['task_id'])
            b_result = graph.run(second['task_id'])
            self.assertEqual([item['task_id'] for item in calls],
                             [first['task_id'], second['task_id']])
            self.assertEqual(a_result['summary'], 'A独立澄清问题')
            self.assertEqual(b_result['summary'], 'B独立澄清问题')
            a_snapshot = graph.graph.get_state({'configurable': {'thread_id': first['task_id']}})
            b_snapshot = graph.graph.get_state({'configurable': {'thread_id': second['task_id']}})
            self.assertEqual(a_snapshot.values['task_id'], first['task_id'])
            self.assertEqual(b_snapshot.values['task_id'], second['task_id'])
            a_ids = set(a_snapshot.values['completed_dispatch_ids'])
            b_ids = set(b_snapshot.values['completed_dispatch_ids'])
            self.assertTrue(a_ids and b_ids)
            self.assertFalse(a_ids & b_ids)
            self.assertEqual(graph.run(first['task_id'])['summary'], 'A独立澄清问题')
            self.assertEqual(len(calls), 2)
        finally:
            graph.close()

        # The same per-task snapshots must remain separate after opening a new
        # workflow object against the existing checkpoint database.
        reopened = MaaWorkflow(self.store, self.directory, {'leader': leader})
        try:
            self.assertEqual(reopened.run(second['task_id'])['summary'], 'B独立澄清问题')
            self.assertEqual(reopened.run(first['task_id'])['summary'], 'A独立澄清问题')
            self.assertEqual(len(calls), 2)
        finally:
            reopened.close()


if __name__ == '__main__':
    unittest.main()

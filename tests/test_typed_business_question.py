"""Leader questions must survive typed completion checks without unblocking faults.

Real graph, checkpoint and task journal; no model, network or GUI execution.
The explicit ask_kind marker is the protocol boundary, not punctuation/prose.
"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shu.core import TaskEngine
from shu.maa_graph import MaaWorkflow
from shu.workflow_store import WorkflowStore


class TypedBusinessQuestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.engine = TaskEngine(self.directory)
        self.addCleanup(self.engine.close)
        self.store = WorkflowStore(self.engine)

    def run_question(self, typed, decision):
        goal = '核对用户指定活动的目录'
        contract = {'goal': goal, 'allow_start': False,
                    'task_spec': {'kind': 'lookup', 'unresolved': []}}
        if typed:
            contract['current_deliverable'] = typed
        current = self.store.submit(goal, contract)
        calls = []

        def leader(context, cancel):
            calls.append(context['task_id'])
            return decision

        graph = MaaWorkflow(self.store, self.directory, {'leader': leader})
        self.addCleanup(graph.close)
        with patch('shu.planned_agents.verify_planned_completion', return_value={
                'complete': False, 'summary': '缺少本任务真实目录来源。'}):
            result = graph.run(current['task_id'])
        self.assertEqual(calls, [current['task_id']])
        self.assertEqual(result['status'], 'needs_input')
        self.assertFalse(result['goal_complete'])
        # This is the same authority transition used by MaaCoordinator._run.
        self.store.suspend(current['task_id'], expected_epoch=current['epoch'],
                           message=result['summary'],
                           kind=result.get('waiting_kind', 'blocked'))
        return current, result

    def test_explicit_typed_business_question_is_preserved_and_persisted(self):
        for typed in ('stage_catalog', 'copilot_codes'):
            with self.subTest(typed=typed):
                prompt = '请确认本次要原版活动目录还是复刻活动目录？'
                current, result = self.run_question(typed, {
                    'next_role': 'ask', 'task': '', 'summary': prompt,
                    'ask_kind': 'business'})
                self.assertEqual(result.get('waiting_kind'), 'business')
                self.assertEqual(result['summary'], prompt)
                question = self.store.management.question(current['task_id'])
                self.assertIsNotNone(question)
                self.assertEqual(question['kind'], 'business')
                self.assertEqual(question['prompt'], prompt)

    def test_typed_evidence_fault_stays_blocked_without_question(self):
        for typed in ('stage_catalog', 'copilot_codes'):
            with self.subTest(typed=typed):
                current, result = self.run_question(typed, {
                    'next_role': 'ask', 'task': '',
                    'summary': '资料工具超时，是否继续？', 'ask_kind': 'blocked'})
                self.assertEqual(result.get('waiting_kind'), 'blocked')
                self.assertIn('缺少本任务真实目录来源', result['summary'])
                self.assertIsNone(self.store.management.question(current['task_id']))

    def test_generic_explicit_business_question_remains_answerable(self):
        prompt = '请确认本次查询国服还是国际服？'
        current, result = self.run_question(None, {
            'next_role': 'ask', 'task': '', 'summary': prompt,
            'ask_kind': 'business'})
        self.assertEqual(result.get('waiting_kind'), 'business')
        self.assertEqual(self.store.management.question(current['task_id'])['prompt'], prompt)

    def test_missing_or_invalid_question_kind_is_blocked_even_with_question_mark(self):
        for extra in ({}, {'ask_kind': 'unknown'}, {'ask_kind': None}):
            with self.subTest(extra=extra):
                current, result = self.run_question(None, {
                    'next_role': 'ask', 'task': '',
                    'summary': '工具失败，是否重试？', **extra})
                self.assertEqual(result.get('waiting_kind'), 'blocked')
                self.assertIsNone(self.store.management.question(current['task_id']))

    def test_fault_code_cannot_be_reclassified_as_business(self):
        for typed in (None, 'stage_catalog', 'copilot_codes'):
            with self.subTest(typed=typed):
                current, result = self.run_question(typed, {
                    'next_role':'ask', 'task':'', 'summary':'网络超时，请补充？',
                    'ask_kind':'business', 'code':'provider_http_503'})
                self.assertEqual(result.get('waiting_kind'), 'blocked')
                self.assertIsNone(self.store.management.question(current['task_id']))


if __name__ == '__main__':
    unittest.main()

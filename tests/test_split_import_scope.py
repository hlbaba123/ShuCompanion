"""Query coverage and import authority stay distinct across durable handoff."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shu.core import ActionRejected, TaskEngine
from shu.import_preparation import ImportPreparation
from shu.leader_plan import PlanStore
from shu.maa_queue_plan import (ImportScopeAmbiguous, ordered_import_candidates,
                                select_import_stages, select_research_stages)
from shu.planned_agents import ImportSelectionRequired, register_import_plan
from shu.task_contract import parse_task_contract
from shu.workflow_store import WorkflowStore


def scope(kind, *, stages=(), difficulty=None, ordinal=None, group=''):
    return {'kind': kind, 'stage_codes': list(stages), 'difficulty': difficulty,
            'ordinal': ordinal, 'group_hint': group}


def import_contract(selection, *, query=()):
    return {'goal': '查询测试活动作业并导入所选关卡',
            'user_request': '查询测试活动作业并导入所选关卡',
            'current_deliverable': 'maa_import', 'activity_name': '测试活动',
            'task_spec': {'kind': 'import', 'stage_code': None},
            'target_stage_codes': list(query), 'import_scope': selection,
            'allow_start': False, 'attempt_limit': 0}


class SplitImportScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)
        self.addCleanup(self.engine.close)
        self.store = WorkflowStore(self.engine)
        self.plans = PlanStore(self.store)
        self.stages = []
        self.groups = []
        for group_id, label, codes, difficulty in (
                ('main', '本篇', ['UNIT-1', 'UNIT-2'], 1),
                ('extra', '后记', ['UNIT-EX-1', 'UNIT-EX-2'], 1),
                ('challenge', '后记突袭', ['UNIT-EX-1', 'UNIT-EX-2'], 2)):
            keys = []
            for index, code in enumerate(codes):
                key = group_id + '-' + str(index)
                keys.append(key)
                self.stages.append({'stage_key': key, 'display_code': code,
                    'stage_id': 'actual-' + key, 'query_key': 'actual-' + key,
                    'difficulty': difficulty, 'query_key_verified': True,
                    'difficulty_verified': True, 'opening_status': 'permanent',
                    'group_id': group_id})
            self.groups.append({'group_id': group_id, 'label': label, 'stage_keys': keys})
        self.structure = {'ok': True, 'catalog_complete': True, 'activity_id': 'unit',
            'activity_name': '测试活动', 'stages': self.stages, 'groups': self.groups,
            'excluded_stage_keys': []}
        path = self.root / 'catalog.json'
        path.write_text(json.dumps(self.structure, ensure_ascii=False), encoding='utf-8')
        self.reference = {'path': str(path),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        self.rows = ordered_import_candidates(self.structure)

    def task(self, contract):
        return self.store.submit(contract['goal'], contract)

    def register(self, task):
        state = {**task, 'role_results': {'catalog': {'structure_ref': self.reference}}}
        with patch('shu.planned_agents.verify_planned_completion',
                   return_value={'complete': True}):
            return register_import_plan(self.store, state, task['contract'])

    def test_ordinal_offers_catalog_choices_and_does_not_register_full_import(self):
        contract = import_contract(scope('ordinal', ordinal=1))
        task = self.task(contract)
        with self.assertRaises(ImportSelectionRequired) as failure:
            self.register(task)
        message = str(failure.exception)
        for choice in ('UNIT-1（普通）', 'UNIT-EX-1（普通）', 'UNIT-EX-1（突袭）'):
            self.assertIn(choice, message)
        self.assertIsNone(self.plans.current(task['task_id']))
        self.assertEqual(self.store.current(task['task_id'])['contract']['import_scope']['kind'], 'ordinal')

    def test_selected_first_stage_keeps_all_query_steps_and_only_one_import_key(self):
        contract = import_contract(scope('stages', stages=['UNIT-1'], difficulty=1))
        task = self.task(contract)
        plan = self.register(task)
        self.assertEqual(plan['ordered_stage_keys'], [row['stage_key'] for row in self.rows])
        self.assertEqual(plan['import_stage_keys'], ['main-0'])
        bridge = ImportPreparation(self.store, self.root)
        source, bound, wanted = bridge._linked_plan(task)
        self.assertEqual(source['task_id'], task['task_id'])
        self.assertEqual(bound['ordered_stage_keys'], plan['ordered_stage_keys'])
        self.assertEqual(wanted, {'main-0'})

    def test_subset_plan_cannot_drop_unselected_query_steps_or_widen_import(self):
        contract = import_contract(scope('stages', stages=['UNIT-1'], difficulty=1))
        for queried, imported in ((['main-0'], ['main-0']),
                                  ([row['stage_key'] for row in self.rows], ['main-0', 'main-1'])):
            with self.subTest(queried=queried, imported=imported):
                task = self.task(contract)
                with self.assertRaises((ActionRejected, ValueError)):
                    self.plans.register(task['task_id'], expected_revision=task['revision'],
                        expected_epoch=task['epoch'], structure_ref=self.reference,
                        ordered_steps=queried, import_stage_keys=imported, search_policy={})
                self.assertIsNone(self.plans.current(task['task_id']))

    def test_explicit_single_query_and_mode_do_not_fetch_the_whole_activity(self):
        contract = import_contract(scope('stages', stages=['UNIT-EX-1'], difficulty=2),
                                   query=['UNIT-EX-1'])
        plan = self.register(self.task(contract))
        self.assertEqual(plan['ordered_stage_keys'], ['challenge-0'])
        self.assertEqual(plan['import_stage_keys'], ['challenge-0'])

    def test_all_import_means_all_of_the_declared_query(self):
        contract = import_contract(scope('all', difficulty=1), query=['UNIT-1', 'UNIT-2'])
        plan = self.register(self.task(contract))
        self.assertEqual(plan['ordered_stage_keys'], ['main-0', 'main-1'])
        self.assertEqual(plan['import_stage_keys'], ['main-0', 'main-1'])

    def test_all_import_with_a_named_mode_keeps_only_that_mode_in_query(self):
        contract = import_contract(scope('all', difficulty=2), query=['UNIT-EX-1'])
        plan = self.register(self.task(contract))
        self.assertEqual(plan['ordered_stage_keys'], ['challenge-0'])
        self.assertEqual(plan['import_stage_keys'], ['challenge-0'])

    def test_first_stage_can_resolve_unique_actual_group_without_code_prefix_guessing(self):
        contract = import_contract(scope('ordinal', ordinal=1, group='后记突袭'))
        selected = select_import_stages(self.rows, contract)
        self.assertEqual([row['stage_key'] for row in selected], ['challenge-0'])
        self.assertEqual(len(select_research_stages(self.rows, contract)), 6)
        self.assertEqual(self.rows, ordered_import_candidates(self.structure))

    def test_explicit_stage_with_two_modes_defaults_to_both_supported_modes(self):
        contract = import_contract(scope('stages', stages=['UNIT-EX-1']))
        selected=select_import_stages(self.rows,contract)
        self.assertEqual({row['difficulty'] for row in selected},{1,2})
        self.assertEqual({row['display_code'] for row in selected},{'UNIT-EX-1'})

    def test_legacy_ordinal_cannot_expand_into_whole_activity(self):
        contract = import_contract(scope('all'))
        contract.pop('import_scope')
        contract['user_request'] = '查询测试活动的作业，并把第一关导入 MAA'
        task = self.task(contract)
        with self.assertRaises(ImportSelectionRequired):
            self.register(task)
        self.assertIsNone(self.plans.current(task['task_id']))

    def test_linked_research_filters_actual_stage_keys_and_preserves_source(self):
        source = self.task({'goal': '查询测试活动全部作业',
                            'current_deliverable': 'copilot_codes', 'activity_name': '测试活动'})
        keys = [row['stage_key'] for row in self.rows]
        self.plans.register(source['task_id'], expected_revision=source['revision'],
            expected_epoch=source['epoch'], structure_ref=self.reference,
            ordered_steps=keys, search_policy={})
        contract = import_contract(scope('stages', stages=['UNIT-EX-1'], difficulty=2))
        contract.update(relation='followup', source_task_id=source['task_id'])
        imported = self.task(contract)
        bridge = ImportPreparation(self.store, self.root)
        linked, plan, wanted = bridge._linked_plan(imported)
        self.assertEqual(linked['task_id'], source['task_id'])
        self.assertEqual(wanted, {'challenge-0'})
        self.assertEqual(plan['ordered_stage_keys'], keys)
        self.assertIsNone(self.plans.current(imported['task_id']))

    def test_all_linked_import_uses_only_the_source_actual_query_steps(self):
        source_contract = import_contract(scope('stages', stages=['UNIT-1'], difficulty=1),
                                          query=['UNIT-1', 'UNIT-2'])
        source = self.task(source_contract)
        source_plan = self.register(source)
        contract = import_contract(scope('all'))
        contract.update(relation='followup', source_task_id=source['task_id'])
        imported = self.task(contract)
        bridge = ImportPreparation(self.store, self.root)
        linked, plan, wanted = bridge._linked_plan(imported)
        self.assertEqual(linked['task_id'], source['task_id'])
        self.assertEqual(wanted, {'main-0', 'main-1'})
        self.assertEqual(plan['ordered_stage_keys'], source_plan['ordered_stage_keys'])
        self.assertIsNone(self.plans.current(imported['task_id']))

    def test_real_answer_keeps_query_scope_and_activity_before_plan_registration(self):
        request = '查询测试活动的作业，并将第一关导入MAA'
        prior_fields = {'intent': 'execute', 'user_request': request, 'goal': request,
            'constraints': [], 'completion_criteria': [], 'removed_constraints': [],
            'reply': '先核对目录。', 'relation': 'new', 'current_deliverable': 'maa_import',
            'activity_name': '测试活动', 'target_stage_codes': [], 'task_spec': {'kind': 'import'}}
        prior = parse_task_contract(prior_fields, request).as_dict()
        answer = 'UNIT-EX-1 普通'
        fields = {**prior_fields, 'user_request': answer, 'goal': '查询测试活动作业并导入UNIT-EX-1普通',
            'reply': '按你的选择继续。', 'relation': 'modify', 'selected_task_id': 'task-original',
            'activity_name': '', 'target_stage_codes': ['UNIT-EX-1'],
            'import_scope': scope('stages', stages=['UNIT-EX-1'], difficulty=1)}
        contract = parse_task_contract(fields, answer, known_tasks=[{
            'task_id': 'task-original', 'contract': prior,
            'question': {'kind': 'business', 'status': 'open'}}]).as_dict()
        self.assertEqual(contract['activity_name'], '测试活动')
        self.assertEqual(contract['target_stage_codes'], [])
        self.assertIsNone(contract['task_spec']['stage_code'])
        plan = self.register(self.task(contract))
        self.assertEqual(plan['ordered_stage_keys'], [row['stage_key'] for row in self.rows])
        self.assertEqual(plan['import_stage_keys'], ['extra-0'])

    def test_preparation_rejects_other_researched_mode_before_download(self):
        from tests import test_import_preparation as fixtures

        fixture = fixtures.ImportPreparationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.make_two_mode_source()
        selected = fixture.new_context(target_stage_codes=[],
            import_scope=scope('stages', stages=['A-1'], difficulty=1))
        options = fixture.bridge.options(selected)
        self.assertTrue(options['scope_resolved'])
        self.assertEqual([(row['stage_code'], row['difficulty']) for row in options['steps']], [('A-1', 1)])
        source_plan = fixture.plans.current(fixture.source)
        excluded = next(row for row in source_plan['steps'] if row['stage_key'] == 'stage-challenge')
        with patch('shu.import_preparation.ResearchTools') as download:
            with self.assertRaisesRegex(ActionRejected, '目标范围'):
                fixture.bridge.prepare(selected, source_task_id=fixture.source,
                    step_id=excluded['step_id'], slot='primary')
        download.assert_not_called()
        self.assertEqual(len(fixture.plans.current(fixture.source)['steps']), 2)


if __name__ == '__main__':
    unittest.main()

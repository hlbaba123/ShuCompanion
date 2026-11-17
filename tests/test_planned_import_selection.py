"""Explicit import choices survive natural wording and keep source identity."""
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from shu.planned_agents import _import_slot, run_import_research


QUESTION = (
    '查找《明日方舟》时间啊，向前！活动 TL-2 普通难度的作业，按我的 Box 核验后，'
    '把适合的主选作业的神秘代码输入 MAA 并核实加载结果。只导入，不开始作战。'
)


def contract(request, **changes):
    return {'user_request': request, 'goal': request, 'current_deliverable': 'maa_import',
            'task_spec': {'kind': 'import', 'stage_code': 'TL-2'}, **changes}


class SelectionTests(unittest.TestCase):
    def test_partial_research_shows_real_codes_and_remaining_box_gap(self):
        agents = SimpleNamespace(_guard=Mock(), store=Mock(), directory=Path('.'))
        request='查孤星全部作业并导入MAA'
        context={'task_id':'current','contract':contract(request)}
        delivery={'summary':'当前可查询 1/2 关有主选','items':[
            {'stage_code':'CW-1','primary_code':'prts://42'},
            {'stage_code':'CW-2','status':'no_box_match'}]}
        with patch('shu.planned_agents.run_planned_research',return_value={'status':'unknown'}), \
             patch('shu.code_delivery.CodeDeliveryStore') as codes, \
             patch('shu.import_preparation.ImportPreparation') as preparation:
            codes.return_value.summary.return_value=delivery
            result=run_import_research(agents,context,threading.Event())
        self.assertEqual(result['code'],'import_research_partial')
        self.assertIn('CW-1：主选 prts://42',result['summary'])
        self.assertIn('CW-2',result['summary'])
        self.assertIn('不符合当前 Box',result['summary'])
        preparation.assert_not_called()

    def test_actual_acceptance_request_selects_primary_under_typed_import_contract(self):
        self.assertEqual(_import_slot(QUESTION, contract=contract(QUESTION)), 'primary')

    def test_explicit_primary_backup_input_wording_is_not_bound_to_one_stage(self):
        for request, expected in [
            ('把适合的备选作业的神秘代码输入 MAA 并核实加载结果', 'backup'),
            ('按 Box 核对后，将主选方案的作业码填入 MAA', 'primary'),
            ('输入 A-7 的备选代码到 MAA', 'backup'),
            ('选主选作业，确认阵容后再导入 MAA', 'primary'),
            ('选择备选，按我的 Box 核验后载入 MAA', 'backup'),
            ('主选', 'primary'),
        ]:
            with self.subTest(request=request):
                self.assertEqual(_import_slot(request, contract=contract(request)), expected)

    def test_query_mentions_do_not_override_explicit_import_clause(self):
        for request, expected in [('查主选和备选，只导入主选', 'primary'),
                                  ('先查主选，把备选作业导入 MAA', 'backup'),
                                  ('不要导入主选，把备选的神秘代码输入 MAA', 'backup')]:
            with self.subTest(request=request):
                self.assertEqual(_import_slot(request, contract=contract(request)), expected)

    def test_conflicting_selection_still_needs_input(self):
        for request in ('导入主选和备选',
                        '把主选作业或备选作业的代码输入 MAA', '导入主选，再导入备选',
                        '主选先不要导入'):
            with self.subTest(request=request):
                with self.assertRaises(ValueError):
                    _import_slot(request, contract=contract(request))

    def test_researching_both_slots_does_not_request_importing_both(self):
        request='查主选和备选，导入 MAA'
        self.assertEqual(_import_slot(request,contract=contract(request)),'primary')

    def test_typed_import_keeps_leader_primary_choice_for_suitable_jobs(self):
        for request in ('查询 X-3 合适的作业并导入 MAA', '查询后导入 MAA'):
            with self.subTest(request=request):
                self.assertEqual(_import_slot(request, contract=contract(request,
                    task_spec={'kind': 'import', 'stage_code': 'X-3'})), 'primary')
        with self.assertRaises(ValueError):
            _import_slot('你好', contract=contract('你好'))
        with self.assertRaises(ValueError):
            _import_slot('如何导入 MAA', contract={'current_deliverable': 'chat', 'task_spec': {'kind': 'lookup'}})

    def test_existing_explicit_selection_is_only_a_fallback(self):
        self.assertEqual(_import_slot('导入 MAA', selected_slot='backup'), 'backup')
        self.assertEqual(_import_slot('导入主选', selected_slot='backup'), 'primary')
        with self.assertRaises(ValueError):
            _import_slot('导入主选和备选', selected_slot='primary')
        with self.assertRaises(ValueError):
            _import_slot('导入 MAA', selected_slot='anything')
        with self.assertRaises(ValueError):
            _import_slot('主选', contract={'current_deliverable': 'copilot_codes', 'task_spec': {'kind': 'lookup'}})

    def test_negated_slots_cannot_be_restored_by_primary_default_or_previous_selection(self):
        for request in ('不要导入主选，导入 MAA', '别用主选，把适合的作业导入 MAA',
                        '不要主选，导入合适的作业', '主选先不要导入，然后导入 MAA',
                        '不要导入主选和备选，导入 MAA', '不要主选，也不要备选，导入 MAA'):
            for previous in (None, 'primary'):
                with self.subTest(request=request, selected_slot=previous):
                    with self.assertRaises(ValueError):
                        _import_slot(request, contract=contract(request), selected_slot=previous)
        request = '不要导入主选，把备选作业输入 MAA'
        self.assertEqual(_import_slot(request, contract=contract(request), selected_slot='primary'), 'backup')
        with self.assertRaises(ValueError):
            _import_slot('你好', contract={'current_deliverable': 'chat', 'task_spec': {'kind': 'lookup'}},
                         selected_slot='primary')


class ResearchSourceBindingTests(unittest.TestCase):
    def exercise(self, *, linked, request=QUESTION):
        source_id = 'previous-code-task' if linked else 'current-import-task'
        current = contract(request, **({'source_task_id': source_id} if linked else {}))
        context = {'task_id': 'current-import-task', 'revision': 1, 'epoch': 2,
                   'dispatch_id': 'current-research-dispatch', 'contract': current}
        agents = SimpleNamespace(_guard=Mock(), store=object(), directory=Path('fixture-data'))
        preparation = Mock()
        preparation.options.return_value = {'source_task_id': source_id, 'steps': [{
            'step_id': 'source-step', 'stage_key': 'source-stage', 'stage_code': 'TL-2', 'difficulty': 1,
            'primary_code': 'prts://1', 'backup_code': 'prts://2'}]}
        preparation.prepare.return_value = {'candidate_ref': {'fixture': 'candidate'},
            'verified_copilot': {'copilot_id': 'candidate-id', 'source_binding': {'source_task_id': source_id}},
            'assessment': {'classification': 'compatible'}}
        cancel = threading.Event()
        with patch('shu.import_preparation.ImportPreparation', return_value=preparation), \
             patch('shu.planned_agents.run_planned_research', return_value={'status': 'researched'}) as research:
            result = run_import_research(agents, context, cancel)
        self.assertEqual(result['status'], 'researched')
        self.assertEqual(preparation.prepare.call_args.kwargs['source_task_id'], source_id)
        self.assertEqual(preparation.prepare.call_args.kwargs['step_id'], 'source-step')
        self.assertEqual(preparation.prepare.call_args.kwargs['slot'], 'primary')
        self.assertEqual(context['task_id'], 'current-import-task')
        self.assertEqual(context['contract'], current)
        if linked:
            research.assert_not_called()
        else:
            research.assert_called_once_with(agents, context, cancel)

    def test_actual_question_new_import_uses_own_plan_and_selected_primary(self):
        self.exercise(linked=False)

    def test_linked_import_keeps_current_dispatch_and_previous_source_task_separate(self):
        self.exercise(linked=True)

    def test_linked_ambiguous_mode_returns_answerable_selection_without_download(self):
        agents = SimpleNamespace(_guard=Mock(),store=object(),directory=Path('fixture-data'))
        current = contract('不要普通，只导入突袭',source_task_id='source')
        preparation = Mock()
        preparation.options.return_value = {'source_task_id':'source','steps':[{
            'step_id':'step','stage_key':'stage','stage_code':'TL-2','difficulty':1,
            'primary_code':'prts://1','backup_code':None}]}
        with patch('shu.import_preparation.ImportPreparation',return_value=preparation):
            result = run_import_research(agents,{'contract':current},threading.Event())
        self.assertEqual(result['code'],'import_selection_required')
        self.assertEqual(result['waiting_kind'],'business')
        preparation.prepare.assert_not_called()
        preparation.prepare_queue_plan.assert_not_called()


if __name__ == '__main__':
    unittest.main()

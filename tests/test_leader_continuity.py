import unittest

from shu.maa_queue_plan import select_research_stages, select_import_stages
from shu.leader_policy import _battle_requested
from shu.planned_agents import _import_slot


class LeaderContinuityTests(unittest.TestCase):
    def setUp(self):
        self.rows = [dict(stage_key=f'{group}-{i}-{mode}', display_code=f'ZZ-{prefix}{i}',
                          group_id=group, group_label=label, difficulty=mode)
                     for group, label, prefix in [('one', '初章', ''), ('two', '后篇', 'EX-')]
                     for i in (1, 2) for mode in (1, 2)]

    def test_named_chapter_is_not_expanded_to_whole_event(self):
        contract = {'chapter_scope': {'name': '后篇'}, 'target_stage_codes': [],
                    'import_scope': {'kind': 'all'}}
        self.assertEqual([r['stage_key'] for r in select_research_stages(self.rows, contract)],
                         [r['stage_key'] for r in self.rows if r['group_id'] == 'two'])
        self.assertEqual(select_import_stages(self.rows, contract), select_research_stages(self.rows, contract))

    def test_ordinal_chapter_uses_actual_directory_not_ex_template(self):
        contract = {'chapter_scope': {'ordinal': 2}, 'target_stage_codes': []}
        self.assertTrue(all(r['group_id'] == 'two' for r in select_research_stages(self.rows, contract)))

    def test_unknown_chapter_is_blocked_instead_of_silently_widened(self):
        with self.assertRaises(ValueError):
            select_research_stages(self.rows, {'chapter_scope': {'name': '不存在'}})

    def test_preexcluded_other_chapters_do_not_renumber_verified_chapter(self):
        rows=[{**r,'group_ordinal':2} for r in self.rows if r['group_id']=='two']
        selected=select_research_stages(rows,{'chapter_scope':{'name':'后篇','ordinal':2}})
        self.assertEqual(selected,rows)
        with self.assertRaises(ValueError):
            select_research_stages(rows,{'chapter_scope':{'name':'后篇','ordinal':1}})

    def test_unspecified_mode_keeps_supported_modes_without_question(self):
        rows = select_import_stages(self.rows, {'import_scope': {'kind': 'stages', 'stage_codes': ['ZZ-1']}})
        self.assertEqual([r['difficulty'] for r in rows], [1, 2])

    def test_negative_success_claim_does_not_request_battle(self):
        self.assertFalse(_battle_requested('不声称已开始作战或战斗已验证'))
        self.assertFalse(_battle_requested('不得声称已经开始自动战斗'))
        self.assertTrue(_battle_requested('导入后开始作战'))
        self.assertTrue(_battle_requested('不要声称已准备而应导入后开始作战'))

    def test_chapter_request_must_reach_leader_instead_of_relisting_codes(self):
        from shu.controller import _current_delivery_repeat
        text='请只核对刚才活动第二章的关卡目录，列出章节名称和关卡代码，不要查询第一章作业，也不导入MAA。'
        self.assertFalse(_current_delivery_repeat(text,{'current_deliverable':'stage_catalog'}))
        self.assertFalse(_current_delivery_repeat(text,{'current_deliverable':'copilot_codes'}))

    def test_directory_followup_displays_its_own_research_not_source_directory(self):
        from types import SimpleNamespace
        from unittest.mock import Mock,patch
        from shu.maa_coordinator import MaaCoordinator
        state={'contract':{'current_deliverable':'copilot_codes','source_task_id':'directory'},'status':'needs_input'}
        fake=SimpleNamespace(store=Mock(),directory='.',current_task_id='codes')
        fake.store.current.return_value=state
        with patch('shu.code_delivery.CodeDeliveryStore') as delivery:
            delivery.return_value.summary.return_value={'complete':False,'items':[],'summary':'actual partial'}
            MaaCoordinator.research_output(fake,'codes')
            delivery.return_value.summary.assert_called_once_with('codes')

    def test_pasted_primary_backup_list_is_not_a_slot_choice(self):
        contract = {'current_deliverable': 'maa_import', 'task_spec': {'kind': 'import'},
                    'import_scope': {'kind': 'all'}}
        self.assertEqual(_import_slot('把这些导入MAA\nZZ-1：主选 prts://1；备选 prts://2', contract=contract), 'primary')
        self.assertEqual(_import_slot('先导入主选，失败再使用备选', contract=contract), 'primary')
        self.assertEqual(_import_slot('导入备选', contract=contract), 'backup')
        self.assertEqual(_import_slot('把刚才的作业导入MAA，备选留作替代',contract=contract),'primary')
        self.assertEqual(_import_slot('这些作业都有备选，帮我导入MAA',contract=contract),'primary')
        with self.assertRaises(ValueError):
            _import_slot('把主选和备选都导入', contract=contract)


if __name__ == '__main__':
    unittest.main()

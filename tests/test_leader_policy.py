"""Requested objectives stay separate from capability and evidence claims."""
import unittest

from shu.leader_policy import (TaskSpecError, completion_decision,
                               import_satisfies_goal, normalize_task_spec)


RECEIPT = {'status':'import_verified','battle_start_submitted':False,
           'evidence_refs':[{'path':'fixture.json','sha256':'a'*64}]}


class LeaderPolicyTests(unittest.TestCase):
    def test_activity_import_uses_typed_scope_not_explanatory_group_prefixes(self):
        from shu.leader_policy import research_stage_codes
        contract={'current_deliverable':'maa_import','target_stage_codes':[],
                  'user_request':'去给我找孤星的所有的作业导入MAA',
                  'goal':'查询孤星全部作业，含 CW 主线关、CW-TR、CW-P、CW-EX 及突袭、CW-S 系列',
                  'task_spec':{'kind':'import','stage_code':None}}
        self.assertEqual(research_stage_codes(contract),[])
        contract.update(target_stage_codes=['CW-2','CW-EX-1'])
        self.assertEqual(research_stage_codes(contract),['CW-2','CW-EX-1'])

    def test_legacy_import_scope_keeps_original_specific_targets(self):
        from shu.leader_policy import research_stage_codes
        contract={'current_deliverable':'maa_import','user_request':'导入 TL-2 和 TL-9',
                  'goal':'包含 TL-EX 与 TL-S 系列','task_spec':{'kind':'import','stage_code':None}}
        self.assertEqual(research_stage_codes(contract),['TL-2','TL-9'])
        contract['user_request']='导入 TL-1～TL-3'
        self.assertEqual(research_stage_codes(contract),['TL-1','TL-2','TL-3'])
        contract['user_request']='导入孤星所有作业'
        self.assertEqual(research_stage_codes(contract),[])

    def test_reported_no_start_outcome_does_not_block_query_to_import_handoff(self):
        for outcome in ('未发生开始作战操作','没有发生开始战斗操作','未曾触发自动作战',
                        '没有进行任何开始作战操作','未发生任何自动战斗','未出现开始作战动作'):
            contract={'user_request':'把刚才两关主选导入 MAA，不开始作战',
                      'goal':'导入刚才查到的两关主选','task_spec':{'kind':'import'},
                      'completion_criteria':['两关均已导入',outcome]}
            with self.subTest(outcome=outcome):
                self.assertTrue(import_satisfies_goal(contract))
                contract['completion_criteria'].append('导入后开始作战')
                self.assertFalse(import_satisfies_goal(contract))

    def test_parenthesized_copilot_page_in_completion_is_import_only(self):
        from shu.leader_policy import import_satisfies_goal
        for label in ('作业（自动战斗）页面', '作业(自动战斗)页面'):
            contract = {'goal': '查询 TL-2 并导入 MAA，只导入，不开始作战',
                        'user_request': '查询 TL-2 并导入 MAA，只导入，不开始作战',
                        'task_spec': {'kind': 'import'},
                        'completion_criteria': ['适合的作业已导入 MAA 的' + label]}
            self.assertTrue(import_satisfies_goal(contract))
            contract['completion_criteria'].append('随后开始自动战斗')
            self.assertFalse(import_satisfies_goal(contract))

    def test_ordinary_stage_noun_does_not_request_battle(self):
        request='找最新活动的全部已开放普通关卡作业，筛选阵容，给出代码。只读查询，不操作游戏。'
        spec=normalize_task_spec({'kind':'lookup'},user_request=request,goal=request,intent='execute')
        self.assertEqual(spec.kind,'lookup')

    def test_ordinary_short_label_allows_batch_import_and_clarification_reply(self):
        requests = (
            '查询将进酒 IW-1 和 IW-2 普通关的合格作业，并将这两关的主选作业全部加入 MAA 多作业队列。'
            '保留已有队列，只导入，不开始作战。',
            '选 IW-1 普通关，查询范围还是整个将进酒，只导入这一关。',
        )
        for request in requests:
            with self.subTest(request=request):
                spec=normalize_task_spec({'kind':'import'}, user_request=request,
                                         goal=request, intent='execute')
                self.assertEqual(spec.kind,'import')
                self.assertTrue(import_satisfies_goal({'user_request':request, 'goal':request,
                    'task_spec':spec.as_dict(), 'completion_criteria':['普通关主选作业已加入队列']}))

    def test_ordinary_short_label_does_not_hide_positive_clear_or_start_request(self):
        for request in ('帮我通关IW-1普通关', '找到普通关作业并开始作战',
                        '通关普通关卡', '不开始旧关，导入普通关作业后开始自动战斗'):
            with self.subTest(request=request), self.assertRaises(TaskSpecError):
                normalize_task_spec({'kind':'import'}, user_request=request,
                                    goal=request, intent='execute')

    def test_stage_code_and_repeat_count_are_independent_trusted_literals(self):
        spec=normalize_task_spec(None,user_request='TL-10刷六次，不使用理智药，也不花源石',
                                 goal='TL-10完成六次作战',intent='execute')
        self.assertEqual(spec.stage_code,'TL-10')
        self.assertEqual(spec.repeat_count,6)
        self.assertEqual(spec.medicine_limit,0)
        self.assertEqual(spec.premium_limit,0)
        self.assertEqual(spec.kind,'repeat_stage')

    def test_model_cannot_replace_explicit_tl10_with_repeat_number_tl6(self):
        for value,goal in (({'kind':'repeat_stage','stage_code':'TL-6','repeat_count':6},'刷TL-6六次'),
                           ({'kind':'repeat_stage','stage_code':'TL-10','repeat_count':5},'刷TL-10六次')):
            with self.subTest(value=value),self.assertRaises(TaskSpecError):
                normalize_task_spec(value,user_request='刷TL-10六次',goal=goal,intent='execute')

    def test_modify_only_count_keeps_stage_and_resource_boundaries(self):
        prior={'goal':'TL-10刷六次','task_spec':{'kind':'repeat_stage','stage_code':'TL-10',
               'repeat_count':6,'medicine_limit':0,'premium_limit':0}}
        spec=normalize_task_spec({'repeat_count':8},user_request='改成八次',goal='完成八次',
                                 intent='modify',previous=prior)
        self.assertEqual((spec.stage_code,spec.repeat_count,spec.medicine_limit,spec.premium_limit),
                         ('TL-10',8,0,0))

    def test_modify_cannot_invent_a_new_stage_without_user_change(self):
        prior={'goal':'TL-10刷六次','task_spec':{'kind':'repeat_stage','stage_code':'TL-10','repeat_count':6}}
        with self.assertRaises(TaskSpecError):
            normalize_task_spec({'stage_code':'TL-6'},user_request='刷六次，不吃药',goal='TL-6刷六次',
                                intent='modify',previous=prior)

    def test_no_medicine_literal_cannot_be_overridden_by_model_quota(self):
        with self.assertRaises(TaskSpecError):
            normalize_task_spec({'medicine_limit':2},user_request='刷六次，不吃药',
                                goal='刷六次',intent='execute')

    def test_import_only_receipt_does_not_complete_repeat_or_clear_event(self):
        for goal,spec in [('TL-10刷六次',{'kind':'repeat_stage','stage_code':'TL-10','repeat_count':6}),
                          ('清完当前活动',{'kind':'clear_event'})]:
            contract={'goal':goal,'task_spec':spec,'allow_start':False,'workflow_scope':'import_only'}
            result=completion_decision(contract,RECEIPT)
            self.assertFalse(result['goal_complete'])
            self.assertEqual(result['status'],'needs_input')
            self.assertTrue(result['pending_objectives'])
            self.assertIn('battle_start',result['blocked_actions'])

    def test_legacy_explicit_battle_goal_is_not_silently_treated_as_import(self):
        for goal in ('导入TL-10后开始自动战斗，刷6次','TL-10完成六次真实结算'):
            self.assertFalse(import_satisfies_goal({'goal':goal,'allow_start':False}))

    def test_existing_legacy_import_contracts_keep_their_completion_semantics(self):
        for goal in ('导入','导入TL-2，不开始','TL-2'):
            with self.subTest(goal=goal):
                result=completion_decision({'goal':goal},RECEIPT)
                self.assertTrue(result['goal_complete'])
                self.assertEqual(result['status'],'import_verified')

    def test_policy_does_not_create_a_receipt_or_grant_permission(self):
        result=completion_decision({'goal':'导入TL-2'}, {'summary':'模型说已导入'})
        self.assertFalse(result['goal_complete'])
        self.assertNotIn('allow_start',result)
        self.assertNotIn('input_allowed',result)

    def test_invalid_spec_fields_are_rejected_without_authority_expansion(self):
        for value in ({'allow_start':True},{'repeat_count':True},{'repeat_count':-1},
                      {'medicine_limit':-1},{'kind':'run_anything'},{'unresolved':['']},
                      {'stage_code':'TL-6; shell'}):
            with self.subTest(value=value),self.assertRaises(TaskSpecError):
                normalize_task_spec(value,user_request='查资料',goal='查资料',intent='chat')

    def test_positive_resource_limits_must_have_explicit_user_basis(self):
        spec=normalize_task_spec({'medicine_limit':2},user_request='最多使用两瓶理智药',
                                 goal='TL-10刷六次',intent='modify',
                                 previous={'goal':'TL-10刷六次','task_spec':{'medicine_limit':0}})
        self.assertEqual(spec.medicine_limit,2)
        with self.assertRaises(TaskSpecError):
            normalize_task_spec({'medicine_limit':2},user_request='刷六次',goal='刷六次',intent='execute')

    def test_finish_current_round_is_distinct_from_immediate_stop(self):
        spec=normalize_task_spec(None,user_request='这一局结束后就停',goal='完成当前局后停止',intent='modify',
                                 previous={'goal':'TL-10刷六次','task_spec':{'kind':'repeat_stage','stage_code':'TL-10'}})
        self.assertTrue(spec.stop_after_current)
        self.assertEqual(spec.stage_code,'TL-10')

    def test_multi_segment_stage_code_is_not_truncated(self):
        spec=normalize_task_spec(None,user_request='SL-EX-8刷两次',goal='刷SL-EX-8两次',intent='execute')
        self.assertEqual(spec.stage_code,'SL-EX-8')
        self.assertEqual(spec.repeat_count,2)

    def test_explicit_no_auto_battle_does_not_make_import_incomplete(self):
        for goal in ('仅导入TL-10，不开始自动战斗','只加载作业，不要自动作战'):
            self.assertTrue(import_satisfies_goal({'goal':goal}))

    def test_no_battle_phrases_remain_import_in_both_intent_and_completion(self):
        request = '将刚才查到的 TL-2 主选作业导入 MAA，只导入，不开始。'
        for boundary in ('不点击开始作战', '不得开始作战', '不进行自动战斗', '不触发自动作战',
                         '不发起自动战斗', '无需开始作战', '不执行自动作战'):
            goal = '导入 TL-2 主选作业并核实加载结果，' + boundary
            with self.subTest(boundary=boundary):
                spec = normalize_task_spec({'kind':'import'}, user_request=request,
                                           goal=goal, intent='execute')
                self.assertEqual(spec.kind, 'import')
                self.assertTrue(import_satisfies_goal({'goal':goal, 'task_spec':spec.as_dict(),
                    'completion_criteria':['确认未点击开始作战、未进行游戏内操作']}))

    def test_no_battle_clause_does_not_hide_a_separate_positive_battle_request(self):
        for request in ('先不开始自动战斗，导入后开始作战',
                        '不使用理智药，TL-2 刷六次',
                        '不导入旧作业，开始自动作战'):
            with self.subTest(request=request), self.assertRaises(TaskSpecError):
                normalize_task_spec({'kind':'import'}, user_request=request,
                                    goal=request, intent='execute')


if __name__ == '__main__': unittest.main()

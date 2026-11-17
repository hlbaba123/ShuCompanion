"""Structured intent validation only; no model, desktop or permission writes."""
import json
import unittest

from shu.task_contract import (INTENTS, ROUTE_USER_TOOL, TaskContract,
                               TaskContractError, parse_task_contract)


def payload(intent='execute', request='进入基建', **changes):
    value={'intent':intent,'user_request':request,'goal':'进入基建页面',
           'constraints':[],'completion_criteria':['新画面显示基建页面'],
           'removed_constraints':[],'reply':'我先进入基建。'}
    value.update(changes)
    return value


class TaskContractTests(unittest.TestCase):
    def test_leader_preserves_endpoint_even_if_model_picks_a_partial_route(self):
        from shu.battle_contract import authorize, compound_authorized

        request = '给我找TL-EX-3的作业并开始执行'
        for delivery, kind in (('copilot_codes', 'lookup'), ('maa_import', 'import'),
                               ('gameplay', 'repeat_stage')):
            with self.subTest(delivery=delivery):
                fields = payload(request=request, goal=request, relation='new',
                    current_deliverable=delivery, target_stage_codes=['TL-EX-3'],
                    task_spec={'kind': kind, 'stage_code': 'TL-EX-3', 'stop_after_current': True})
                result = authorize(parse_task_contract(fields, request).as_dict())
                self.assertTrue(compound_authorized(result))
                self.assertEqual(result['user_request'], request)
                self.assertEqual(result['import_scope']['stage_codes'], ['TL-EX-3'])
                self.assertEqual(result['import_scope']['difficulty'], 1)

    def test_followup_import_does_not_remove_prior_query_only_constraints(self):
        source={'goal':'查询第二章代码','current_deliverable':'copilot_codes',
                'constraints':['本轮只查询，不导入MAA'],'target_stage_codes':['AA-1']}
        request='把刚才的作业导入MAA，不开始作战'
        result=parse_task_contract(payload(request=request,goal=request,relation='followup',
            current_deliverable='maa_import',source_task_id='source',activity_name='活动',
            removed_constraints=['本轮只查询，不导入MAA'],constraints=['不开始作战'],
            import_scope={'kind':'all'},task_spec={'kind':'import'}),request,
            known_tasks=[{'task_id':'source','contract':source}]).as_dict()
        self.assertEqual(result['source_task_id'],'source')
        self.assertEqual(source['constraints'],['本轮只查询，不导入MAA'])
        self.assertEqual(result['constraints'],['不开始作战'])
    def test_continue_null_chapter_preserves_prior_scope(self):
        old={'goal':'查询第二章作业','current_deliverable':'copilot_codes',
             'chapter_scope':{'name':'','ordinal':2},'target_stage_codes':[],'task_spec':{'kind':'lookup'}}
        request='继续'
        result=parse_task_contract(payload(intent='resume',request=request,goal='',relation='continue',
            current_deliverable='copilot_codes',selected_task_id='task',source_task_id=None,
            chapter_scope=None,activity_name=''),request,known_tasks=[{'task_id':'task','contract':old}]).as_dict()
        self.assertEqual(result['chapter_scope'],old['chapter_scope'])
    def test_activity_query_and_ordinal_import_are_separate(self):
        request = '给我查将进酒的作业，并将将进酒的第一关的作业导入MAA'
        fields = payload(request=request, goal=request, relation='new',
                         current_deliverable='maa_import', activity_name='将进酒',
                         target_stage_codes=[], task_spec={'kind':'import'})
        result = parse_task_contract(fields, request).as_dict()
        self.assertEqual(result['target_stage_codes'], [])
        self.assertIsNone(result['task_spec']['stage_code'])
        self.assertEqual(result['import_scope'], {'kind':'ordinal','stage_codes':[],
                         'ordinal':1,'difficulty':None,'group_hint':''})
        for scope in ({'kind':'all'}, {'kind':'ordinal','ordinal':2},
                      {'kind':'stages','stage_codes':['IW-1']}):
            with self.subTest(scope=scope), self.assertRaises(TaskContractError):
                parse_task_contract({**fields,'import_scope':scope}, request)

    def test_explicit_single_import_does_not_narrow_activity_query(self):
        request = '查将进酒作业，只把IW-1的作业导入MAA'
        result = parse_task_contract(payload(request=request, goal=request, relation='new',
            current_deliverable='maa_import', activity_name='将进酒', target_stage_codes=[],
            import_scope={'kind':'stages','stage_codes':['IW-1']},
            task_spec={'kind':'import','stage_code':'IW-1'}), request).as_dict()
        self.assertEqual(result['target_stage_codes'], [])
        self.assertIsNone(result['task_spec']['stage_code'])
        self.assertEqual(result['import_scope']['stage_codes'], ['IW-1'])

    def test_answered_ordinal_only_changes_import_scope(self):
        original_request = '查将进酒的作业，将第一关导入MAA'
        prior = parse_task_contract(payload(request=original_request, goal=original_request,
            relation='new', current_deliverable='maa_import', activity_name='将进酒',
            target_stage_codes=[], task_spec={'kind':'import'}), original_request).as_dict()
        tasks = [{'task_id':'original','contract':prior,'question':{'kind':'business','status':'open'}}]
        answer = '我说的是 IW-EX-1 的普通难度'
        fields = payload(request=answer, goal='查询将进酒作业并导入 IW-EX-1 普通',
            relation='modify', selected_task_id='original', current_deliverable='maa_import',
            activity_name='将进酒', target_stage_codes=['IW-EX-1'],
            import_scope={'kind':'stages','stage_codes':['IW-EX-1'],'difficulty':1},
            task_spec={'kind':'import','stage_code':'IW-EX-1'})
        result = parse_task_contract(fields,answer,known_tasks=tasks).as_dict()
        self.assertEqual(result['selected_task_id'],'original')
        self.assertEqual(result['target_stage_codes'], [])
        self.assertIsNone(result['task_spec']['stage_code'])
        self.assertEqual(result['import_scope']['stage_codes'],['IW-EX-1'])
        self.assertEqual(result['import_scope']['difficulty'],1)
        with self.assertRaises(TaskContractError):
            parse_task_contract({**fields,'import_scope':{'kind':'all'}},answer,known_tasks=tasks)

    def test_difficulty_fact_check_does_not_ask_user_again(self):
        request = '查找TL-EX-6突袭的作业，导入MAA并核实加载'
        flags = ['突袭难度需在作业目录中核对；用户未指定其他难度', '请选择使用主选还是备选']
        result = parse_task_contract(payload(request=request, goal=request, relation='new',
            current_deliverable='maa_import', target_stage_codes=['TL-EX-6'],
            task_spec={'kind':'import','stage_code':'TL-EX-6','unresolved':flags}), request)
        self.assertEqual(result.task_spec.unresolved, ('请选择使用主选还是备选',))

    def test_copilot_options_roundtrip_and_inherit_only_current_task(self):
        request = '勾选自动编队，关闭借助战，再导入 A-1 作业'
        options = {'auto_formation': True, 'use_support': False}
        original = parse_task_contract(payload(request=request, goal=request,
            copilot_options=options, task_spec={'kind': 'import', 'stage_code': 'A-1'}), request)
        self.assertEqual(original.as_dict()['copilot_options'], options)
        resumed = parse_task_contract(payload('resume', '继续', goal=''), '继续', original)
        self.assertEqual(resumed.as_dict()['copilot_options'], options)
        changed = parse_task_contract(payload('modify', '关闭自动编队', goal=original.goal,
            copilot_options={'auto_formation': False}), '关闭自动编队', original)
        self.assertEqual(changed.as_dict()['copilot_options'], {'auto_formation': False, 'use_support': False})
        fresh = parse_task_contract(payload(), '进入基建', original)
        self.assertEqual(fresh.as_dict()['copilot_options'], {})
        for invalid in ({'use_support': 1}, {'other': True}, None):
            with self.subTest(invalid=invalid), self.assertRaises(TaskContractError):
                parse_task_contract(payload(copilot_options=invalid), '进入基建')

    def test_all_intents_are_explicit_and_do_not_create_permissions(self):
        prior=TaskContract('execute','旧原话','旧目标')
        for intent in INTENTS:
            contract=parse_task_contract(payload(intent), '进入基建', prior)
            self.assertEqual(contract.intent,intent)
            self.assertNotIn('input_allowed',contract.as_dict())
        self.assertFalse(ROUTE_USER_TOOL['function']['parameters']['additionalProperties'])

    def test_exact_user_request_and_constraints_survive_serialization(self):
        request='帮我刷六次\n不用理智药，也不花源石'
        value=payload(request=request,goal='完成六次当前关卡',constraints=['不用理智药','不花源石'],
                      completion_criteria=['六次真实结算后停止'])
        contract=parse_task_contract(json.dumps(value,ensure_ascii=False),request)
        self.assertEqual(contract.user_request,request)
        self.assertEqual(contract.constraints,('不用理智药','不花源石'))
        self.assertIn('六次真实结算后停止',contract.execution_goal())

    def test_changed_or_truncated_original_is_rejected(self):
        with self.assertRaises(TaskContractError):
            parse_task_contract(payload(request='刷六次'),'刷六次，不吃药')

    def test_anaphoric_execute_and_resume_inherit_goal_and_constraints(self):
        prior=TaskContract('draft','旧原话','进入基建换班',('不替换已锁定干员',),('排班页面确认',))
        for intent,request in (('execute','那你去做吧'),('resume','继续')):
            contract=parse_task_contract(payload(intent,request,goal='',completion_criteria=[]),request,prior)
            self.assertEqual(contract.goal,prior.goal)
            self.assertEqual(contract.constraints,prior.constraints)
            self.assertEqual(contract.completion_criteria,prior.completion_criteria)

    def test_modified_contract_keeps_unchanged_constraints(self):
        prior=TaskContract('execute','旧原话','刷六次',('不用药','不花源石'),('六次结算',))
        contract=parse_task_contract(payload('modify','改成八次',goal='刷八次',
                     completion_criteria=['八次结算']), '改成八次',prior)
        self.assertEqual(contract.constraints,prior.constraints)
        self.assertEqual(contract.completion_criteria,('八次结算',))

    def test_only_explicit_removals_change_existing_constraints(self):
        prior=TaskContract('execute','旧原话','刷六次',('最多六次','不用药'))
        contract=parse_task_contract(payload('modify','改成八次',goal='刷八次',
                     constraints=['最多八次'],removed_constraints=['最多六次']), '改成八次',prior)
        self.assertEqual(contract.constraints,('不用药','最多八次'))
        with self.assertRaises(TaskContractError):
            parse_task_contract(payload('modify',removed_constraints=['未有约束']), '进入基建',prior)

    def test_new_execute_does_not_inherit_unrelated_previous_goal(self):
        prior=TaskContract('execute','旧原话','刷六次',('最多六次',))
        contract=parse_task_contract(payload(), '进入基建',prior)
        self.assertEqual(contract.constraints,())

    def test_control_intents_require_an_existing_task_to_resume_or_modify(self):
        for intent in ('resume','modify'):
            with self.assertRaises(TaskContractError):
                parse_task_contract(payload(intent,goal=''), '进入基建')

    def test_empty_observation_goal_is_read_only(self):
        contract=parse_task_contract(payload('observe','看看画面',goal=''), '看看画面')
        self.assertIn('不执行游戏输入',contract.goal)

    def test_public_lookup_is_not_a_screen_observation_and_must_be_rerouted(self):
        request='只读查询TL-10作业JSON和神秘代码，不导入MAA'
        value=payload('observe',request,goal=request,task_spec={'kind':'lookup','stage_code':'TL-10'})
        with self.assertRaisesRegex(TaskContractError,'画面|屏幕'):
            parse_task_contract(value,request)
        self.assertEqual(value['intent'],'observe','Do not silently rewrite a contradictory model contract')
        valid=parse_task_contract({**value,'intent':'execute'},request)
        self.assertEqual(valid.intent,'execute')
        self.assertEqual(valid.task_spec.kind,'lookup')

    def test_screen_observation_still_routes_without_public_research_kind(self):
        request='看看当前游戏画面'
        contract=parse_task_contract(payload('observe',request,goal='分析当前游戏画面'),request)
        self.assertEqual(contract.intent,'observe')

    def test_unknown_fields_or_bad_types_are_rejected(self):
        for changes in ({'input_allowed':True},{'constraints':'不用药'},
                        {'completion_criteria':['']},{'intent':'unknown'}, {'goal':True}):
            with self.subTest(changes=changes), self.assertRaises(TaskContractError):
                parse_task_contract(payload(**changes), '进入基建')

    def test_new_task_spec_separates_stage_runs_and_resource_limits(self):
        request='刷TL-10六次，不使用理智药，不花源石'
        contract=parse_task_contract(payload(request=request,goal='TL-10刷六次',
            task_spec={'kind':'repeat_stage','stage_code':'TL-10','repeat_count':6,
                       'medicine_limit':0,'premium_limit':0}),request)
        self.assertEqual(contract.task_spec.stage_code,'TL-10')
        self.assertEqual(contract.as_dict()['task_spec']['repeat_count'],6)
        self.assertIn('TL-10',contract.execution_goal())
        self.assertNotIn('allow_start',contract.as_dict()['task_spec'])

    def test_old_eight_field_payload_gets_normalized_spec_without_model_rewrite(self):
        request='TL-10刷六次，不吃药'
        contract=parse_task_contract(payload(request=request,goal='TL-10刷六次'),request)
        self.assertEqual(contract.task_spec.repeat_count,6)
        self.assertEqual(contract.task_spec.medicine_limit,0)

    def test_model_goal_cannot_change_literal_stage_or_number(self):
        request='刷TL-10六次'
        with self.assertRaises(TaskContractError):
            parse_task_contract(payload(request=request,goal='刷TL-6六次'),request)
        with self.assertRaises(TaskContractError):
            parse_task_contract(payload(request=request,goal='刷TL-10五次'),request)

    def test_modify_count_retains_known_stage_even_if_model_goal_omits_it(self):
        prior=parse_task_contract(payload(request='刷TL-10六次，不吃药',goal='TL-10刷六次'),
                                  '刷TL-10六次，不吃药')
        contract=parse_task_contract(payload('modify','改成八次',goal='刷八次'), '改成八次',prior)
        self.assertEqual(contract.task_spec.stage_code,'TL-10')
        self.assertEqual(contract.task_spec.repeat_count,8)
        self.assertEqual(contract.task_spec.medicine_limit,0)
        self.assertIn('TL-10',contract.goal)


if __name__=='__main__':
    unittest.main()

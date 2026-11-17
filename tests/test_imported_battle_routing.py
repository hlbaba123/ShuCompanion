"""Imported-job references remain model-selected sources, without GUI/network IO."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from shu.task_contract import (IMPORTED_BATTLE_ROUTE_PROMPT, ROUTE_USER_PROMPT, ROUTE_USER_TOOL,
                               TaskContractError, parse_task_contract)


REQUEST = '现在开始MAA自动战斗，运行刚才导入的这关一次，不使用理智药或源石。'


def fields(request=REQUEST, **changes):
    value = {'intent':'execute', 'user_request':request,
        'goal':'核对当前导入的单作业，导航到对应关卡并启动MAA一次，监控至结束，不使用理智药或源石',
        'constraints':['只运行一次','不使用理智药','不使用源石'],
        'completion_criteria':['核对作业与难度后运行一次并监控至结束'],
        'removed_constraints':[], 'reply':'先核对导入结果，再运行一次。',
        'current_deliverable':'gameplay', 'relation':'new',
        'selected_task_id':None, 'source_task_id':None,
        'task_spec':{'kind':'repeat_stage','repeat_count':1,'medicine_limit':0,'premium_limit':0}}
    value.update(changes)
    return value


def imported(task_id='import-task', **changes):
    value = {'task_id':task_id,'status':'completed','lifecycle':'active',
        'contract':{'current_deliverable':'maa_import','goal':'导入TL-EX-1突袭作业，先不开始',
                    'constraints':['本次不开始作战'],'target_stage_codes':['TL-EX-1'],
                    'task_spec':{'kind':'import','stage_code':'TL-EX-1'},
                    'import_scope':{'kind':'stages','stage_codes':['TL-EX-1'],'difficulty':2}}}
    value.update(changes)
    return value


class ImportedBattleRoutingTests(unittest.TestCase):
    def test_recorded_sixth_turn_requires_import_task_source(self):
        report_path=Path(__file__).resolve().parents[2]/'q45/artifacts/continuous-admin-maamatch/report.json'
        if not report_path.is_file():
            self.skipTest('Recorded continuous admin conversation is not present')
        report=json.loads(report_path.read_text(encoding='utf-8'))
        previous, turn=report['turns'][4:6]
        self.assertEqual(previous['task']['status'],'completed')
        self.assertEqual(previous['contract']['current_deliverable'],'maa_import')
        self.assertEqual(turn['question'],REQUEST)
        allowed=ROUTE_USER_TOOL['function']['parameters']['properties']
        arguments={key:value for key,value in turn['contract'].items() if key in allowed}
        # Persisted TaskContract omits this empty, input-only tool field.
        arguments['removed_constraints']=[]
        known=[{**item['task'],'contract':item['contract']} for item in report['turns'][:5] if item.get('task')]
        with self.assertRaisesRegex(TaskContractError,'followup.*source_task_id'):
            parse_task_contract(arguments,turn['question'],known_tasks=known)

    def test_missing_or_wrong_kind_source_is_rejected_without_rewriting_route(self):
        source=imported()
        query={'task_id':'query-task','status':'completed',
               'contract':{'current_deliverable':'copilot_codes','goal':'查询作业'}}
        for changes in ({}, {'relation':'followup','source_task_id':'query-task'}):
            with self.subTest(changes=changes):
                arguments=fields(**changes)
                before=deepcopy(arguments)
                with self.assertRaisesRegex(TaskContractError,'followup.*source_task_id'):
                    parse_task_contract(arguments,REQUEST,known_tasks=[query,source])
                self.assertEqual(arguments,before)

    def test_query_only_history_cannot_be_presented_as_import_source(self):
        query={'task_id':'query-task','status':'completed',
               'contract':{'current_deliverable':'copilot_codes','goal':'查询作业'}}
        with self.assertRaisesRegex(TaskContractError,'followup.*source_task_id'):
            parse_task_contract(fields(relation='followup',source_task_id='query-task'),REQUEST,
                                known_tasks=[query])

    def test_valid_followup_preserves_new_permission_and_original_import_constraints(self):
        source=imported()
        before=deepcopy(source)
        result=parse_task_contract(fields(relation='followup',source_task_id='import-task'),
                                   REQUEST,known_tasks=[source]).as_dict()
        self.assertEqual(result['relation'],'followup')
        self.assertEqual(result['source_task_id'],'import-task')
        self.assertEqual(result['constraints'],['只运行一次','不使用理智药','不使用源石'])
        self.assertNotIn('本次不开始作战',result['constraints'])
        self.assertEqual(source,before)
        from shu.battle_contract import authorize, authorized
        self.assertTrue(authorized(authorize(result)))

    def test_negated_new_task_phrase_does_not_drop_the_import_reference(self):
        text='不要新建独立任务，运行刚才导入的这关一次'
        with self.assertRaisesRegex(TaskContractError,'followup.*source_task_id'):
            parse_task_contract(fields(text),text,known_tasks=[imported()])

    def test_historical_reference_variants_require_model_selected_source(self):
        for text in ('启动上一轮已导入MAA的作业一次，不用药不用源石',
                     '运行上次加载好的这份作业一次',
                     '把刚刚载入的这关运行一次'):
            with self.subTest(text=text), self.assertRaisesRegex(TaskContractError,'followup.*source_task_id'):
                parse_task_contract(fields(text),text,known_tasks=[imported()])

    def test_one_of_multiple_completed_imports_stays_exactly_model_selected(self):
        tasks=[imported('older-import'),imported('newer-import')]
        for task_id in ('older-import','newer-import'):
            with self.subTest(task_id=task_id):
                result=parse_task_contract(fields(relation='followup',source_task_id=task_id),
                                           REQUEST,known_tasks=tasks)
                self.assertEqual(result.source_task_id,task_id)

    def test_unfinished_or_trashed_import_is_not_a_qualified_source(self):
        for unavailable in (imported('other',status='failed'),
                            imported('other',status='running'),imported('other',lifecycle='trashed')):
            with self.subTest(unavailable=unavailable), self.assertRaisesRegex(TaskContractError,'followup.*source_task_id'):
                parse_task_contract(fields(relation='followup',source_task_id='other'),REQUEST,
                                    known_tasks=[imported(),unavailable])

    def test_standalone_current_loaded_job_does_not_require_local_history(self):
        text='启动当前已加载的MAA作业一次，不用理智药和源石'
        for tasks in ([],[imported()]):
            with self.subTest(tasks=tasks):
                result=parse_task_contract(fields(text),text,known_tasks=tasks)
                self.assertEqual(result.relation,'new')
                self.assertIsNone(result.source_task_id)

    def test_explicit_new_input_mode_does_not_attach_historical_source(self):
        result=parse_task_contract(fields(),REQUEST,known_tasks=[imported()],input_mode='new')
        self.assertEqual(result.relation,'new')
        self.assertIsNone(result.source_task_id)

    def test_model_cannot_supply_the_trusted_input_mode(self):
        with self.assertRaises(TaskContractError):
            parse_task_contract(fields(input_mode='new'),REQUEST,known_tasks=[imported()])

    def test_explicit_new_task_text_and_unrelated_gameplay_remain_independent(self):
        text='新建一个独立任务，运行刚才导入的这关一次'
        result=parse_task_contract(fields(text),text,known_tasks=[imported()])
        self.assertEqual(result.relation,'new')
        text='去基建收取产物，刚才导入作业的事先放着'
        result=parse_task_contract(fields(text,task_spec={'kind':'base'}),text,known_tasks=[imported()])
        self.assertEqual(result.relation,'new')

    def test_chat_about_previous_import_does_not_require_source(self):
        text='刚才导入的作业为什么不运行？'
        result=parse_task_contract(fields(text,intent='chat',current_deliverable='chat',goal='',
            task_spec={'kind':'unknown'},constraints=[],completion_criteria=[]),text,
            known_tasks=[imported()])
        self.assertEqual(result.intent,'chat')

    def test_dialogue_repairs_route_with_source_without_network_or_rewriting_request(self):
        from shu.network_dialogue import run_network_dialogue
        from shu.providers import PersonaSession
        from tests.test_network_dialogue import ScriptedClient, Research, assistant, tool
        corrected=fields(relation='followup',source_task_id='import-task')
        client=ScriptedClient(assistant(tool('bad','route_user_request',**fields())),
                              assistant(tool('fixed','route_user_request',**corrected)))
        persona=PersonaSession(client)
        research=Research()
        tasks=[imported()]
        summary='known_tasks='+json.dumps(tasks,ensure_ascii=False)
        first=run_network_dialogue(persona,REQUEST,task_summary=summary,research_tools=research,
            cancel_event=None,progress=None,guard=None,routing=True,route_only=True)
        arguments=json.loads(first['response']['tool_calls'][0]['function']['arguments'])
        try:
            parse_task_contract(arguments,REQUEST,known_tasks=tasks)
        except TaskContractError as error:
            repair=str(error)
        else:
            self.fail('Dropped import source was accepted instead of requesting model repair')
        persona.record_tool_result('bad',{'ok':False,'error_code':'intent_contract_invalid',
                                         'error':repair,'executed':False})
        second=run_network_dialogue(persona,REQUEST,task_summary=summary+'\n只修复路由：'+repair,
            research_tools=research,cancel_event=None,progress=None,guard=None,
            routing=True,route_only=True)
        arguments=json.loads(second['response']['tool_calls'][0]['function']['arguments'])
        parsed=parse_task_contract(arguments,REQUEST,known_tasks=tasks)
        self.assertEqual(parsed.source_task_id,'import-task')
        self.assertEqual(parsed.user_request,REQUEST)
        self.assertEqual(research.calls,[])
        self.assertEqual(len(client.requests),2)
        context='\n'.join(message.get('content') or '' for message in client.requests[0]['messages']
                          if message['role']=='system')
        self.assertIn(IMPORTED_BATTLE_ROUTE_PROMPT,context)
        self.assertIn(IMPORTED_BATTLE_ROUTE_PROMPT,ROUTE_USER_PROMPT)


if __name__=='__main__':
    unittest.main()

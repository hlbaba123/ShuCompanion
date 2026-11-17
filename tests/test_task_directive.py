"""Task boundaries, not model semantics or network answers."""
import unittest
from shu.task_directive import validate_directive, TaskDirectiveError
from shu.task_contract import parse_task_contract


class DirectiveTests(unittest.TestCase):
    def test_continue_cannot_change_catalog_into_code_delivery(self):
        tasks=[{'task_id':'catalog-task','contract':{'current_deliverable':'stage_catalog'}}]
        with self.assertRaises(TaskDirectiveError):
            validate_directive({'relation':'continue','selected_task_id':'catalog-task',
                'current_deliverable':'copilot_codes'},user_request='用刚才目录找作业',known_tasks=tasks)
        result=validate_directive({'relation':'followup','source_task_id':'catalog-task',
            'current_deliverable':'copilot_codes'},user_request='用刚才目录找作业',known_tasks=tasks)
        self.assertEqual(result['source_task_id'],'catalog-task')

    def test_catalog_is_not_a_job_goal_when_goal_mentions_future_or_negation(self):
        from shu.leader_policy import copilot_research_required
        for goal in ('本次只交付关卡目录，不查询或获取作业。','列出关卡，之后再查作业'):
            self.assertFalse(copilot_research_required({'current_deliverable':'stage_catalog','goal':goal}))
    def test_stage_identifier_in_activity_field_is_not_an_activity_constraint(self):
        request='查询TL-6作业代码'
        value={'intent':'execute','user_request':request,'goal':request,
            'constraints':[],'completion_criteria':[],'removed_constraints':[],'reply':'开始查询',
            'relation':'new','current_deliverable':'copilot_codes','activity_name':'TL-6',
            'task_spec':{'kind':'lookup','stage_code':'TL-6'}}
        result=parse_task_contract(value,request,known_tasks=[])
        self.assertEqual(result.activity_name,'')
        self.assertEqual(result.task_spec.stage_code,'TL-6')

    known = [{'task_id': 'old-tl', 'goal': '查询TL-1到TL-10作业',
              'activity_name': '时间啊，向前！', 'status': 'paused'}]

    def test_future_jobs_do_not_upgrade_current_catalog(self):
        for request in ('查一下愚人号有哪些关卡，方便以后查作业',
                        '列出活动目录，下一步再找代码'):
            result = validate_directive({'relation': 'new', 'current_deliverable': 'stage_catalog',
                'activity_name': '愚人号', 'deferred_intents': ['copilot_codes']},
                user_request=request, known_tasks=self.known)
            self.assertEqual(result['current_deliverable'], 'stage_catalog')
            self.assertIsNone(result['selected_task_id'])

    def test_followup_uses_source_but_continuation_selects_real_task(self):
        for relation, field in (('followup', 'source_task_id'), ('continue', 'selected_task_id'),
                                ('modify', 'selected_task_id')):
            result = validate_directive({'relation': relation, field: 'old-tl',
                'current_deliverable': 'copilot_codes'}, user_request='接着查代码', known_tasks=self.known)
            self.assertEqual(result[field], 'old-tl')
        with self.assertRaises(TaskDirectiveError):
            validate_directive({'relation': 'continue', 'selected_task_id': 'invented',
                'current_deliverable': 'copilot_codes'}, user_request='继续', known_tasks=self.known)

    def test_new_cannot_select_old_and_chat_cannot_select_task(self):
        for relation, delivery in (('new', 'stage_catalog'), ('continue', 'chat')):
            with self.assertRaises(TaskDirectiveError):
                validate_directive({'relation': relation, 'current_deliverable': delivery,
                    'selected_task_id': 'old-tl'}, user_request='你好', known_tasks=self.known)

    def test_new_catalog_goal_has_no_old_stage_or_constraints(self):
        request='查愚人号关卡，之后再查作业'
        value={'intent':'execute','user_request':request,'goal':'查询愚人号真实关卡目录',
               'constraints':[],'completion_criteria':[],'removed_constraints':[],'reply':'我来查目录。',
               'relation':'new','current_deliverable':'stage_catalog',
               'deferred_intents':['copilot_codes'],'task_spec':{'kind':'lookup'}}
        result=parse_task_contract(value,request,{'goal':'查TL-10作业','constraints':['只查TL'],
            'task_spec':{'kind':'lookup','stage_code':'TL-10'}},known_tasks=self.known,request_id='trusted')
        self.assertNotIn('TL',result.execution_goal())
        self.assertIsNone(result.task_spec.stage_code)
        self.assertEqual(result.as_dict()['request_id'],'trusted')

    def test_typed_chat_about_jobs_stays_chat(self):
        request='聊聊找作业的思路'
        result=parse_task_contract({'intent':'chat','user_request':request,'goal':request,
            'constraints':[],'completion_criteria':[],'removed_constraints':[],'reply':'可以。',
            'relation':'new','current_deliverable':'chat','task_spec':{'kind':'lookup'}},request,
            known_tasks=self.known)
        self.assertEqual(result.intent,'chat')

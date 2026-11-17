import unittest
from shu.controller import _current_delivery_repeat, _dialogue_history
from shu.network_dialogue import _defer_job_research
from shu.task_directive import validate_directive,TaskDirectiveError

class ReliabilityRoutingTests(unittest.TestCase):
    def test_explicit_missing_stage_retry_never_uses_repeat_shortcut(self):
        contract={'current_deliverable':'copilot_codes','target_stage_codes':['RS-EX-8']}
        for text in ('再给我找一下RS-EX-8，确认没有合适的吗','再给我找一下RS-EX-8',
                     '给我重新查上一轮没查到的作业'):
            self.assertFalse(_current_delivery_repeat(text,contract))
        self.assertTrue(_current_delivery_repeat('把刚才的作业码再列一遍，不要重新搜索',contract))

    def test_stage_suitability_retry_leaves_public_search_to_workflow(self):
        self.assertTrue(_defer_job_research('再给我找一下RS-EX-8，确认没有合适的吗'))
        self.assertFalse(_defer_job_research('再帮我找一下RS-EX-8的开放时间'))

    def test_modify_cannot_silently_change_stage_scope_on_paused_task(self):
        old={'task_id':'old','contract':{'current_deliverable':'copilot_codes','target_stage_codes':[],
                                     'task_spec':{'stage_code':None}}}
        for relation in ('modify', 'continue'):
            with self.subTest(relation=relation), self.assertRaises(TaskDirectiveError):
                validate_directive({'relation':relation,'selected_task_id':'old','current_deliverable':'copilot_codes',
                    'target_stage_codes':['RS-EX-8']},user_request='再查RS-EX-8',known_tasks=[old])

    def test_history_shortens_only_old_context_not_current_turn_or_saved_memory(self):
        messages=[{'role':'user','content':'旧问题'*2000},{'role':'assistant','content':'旧回答'*2000}]
        result=_dialogue_history(messages)
        self.assertLess(len(str(result)),3000)
        self.assertEqual(len(messages[0]['content']),6000)

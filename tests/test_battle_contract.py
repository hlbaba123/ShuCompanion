"""Battle permission comes from this request, never a model goal or old import."""
import copy
import unittest

from shu.battle_contract import authorize, authorized


class BattleContractTests(unittest.TestCase):
    def test_query_and_execution_keeps_import_and_one_start_in_same_contract(self):
        for text in (
            '给我找TL-EX-3的作业并开始执行',
            '查找TL-EX-3突袭的作业，导入MAA并开始执行',
            '你先去查作业，然后导入MAA，最后运行',
            '查TL-EX-3的作业，导入后开始MAA自动战斗',
        ):
            for route in ('gameplay', 'maa_import'):
                with self.subTest(text=text, route=route):
                    value = self.contract(text)
                    value['current_deliverable'] = route
                    value['task_spec']['stage_code'] = 'TL-EX-3'
                    result = authorize(value)
                    self.assertTrue(authorized(result))
                    self.assertEqual(result['current_deliverable'], 'maa_import')
                    self.assertEqual(result['workflow_scope'], 'maa_import_then_battle')
                    self.assertEqual(result['attempt_limit'], 1)

    def test_compound_request_does_not_start_on_lookup_import_or_deferred_execution(self):
        for text in (
            '给我查TL-EX-3的作业', '给我查TL-EX-3的作业并导入MAA',
            '查作业并导入MAA，先不要执行', '查作业并导入MAA，稍后再运行',
            '查找TL-EX-3作业，然后开始导入MAA自动战斗界面',
            '查找TL-EX-3作业，然后启动MAA自动战斗界面',
            '如果查到作业并导入MAA，开始执行会怎么样？',
            '查TL-EX-3的作业，导入MAA后开始执行，连续运行两次',
            '查TL-EX-3作业，导入MAA并开始执行，再等我确认',
        ):
            with self.subTest(text=text):
                value = self.contract(text)
                value['current_deliverable'] = 'maa_import'
                self.assertFalse(authorized(authorize(value)))

    def contract(self, text='开始 MAA 自动战斗，运行当前已导入的单作业'):
        return {'user_request': text, 'goal': '模型计划开始战斗',
                'current_deliverable': 'gameplay', 'task_spec': {'kind': 'repeat_stage'}}

    def test_positive_current_single_request_authorizes_one_without_mutating(self):
        value = self.contract(); original = copy.deepcopy(value)
        result = authorize(value)
        self.assertEqual(value, original)
        self.assertTrue(authorized(result))
        self.assertEqual(result['attempt_limit'], 1)
        self.assertEqual(result['execution_restrictions']['direct_game_input'], 'navigation_only')

    def test_import_query_base_or_model_goal_cannot_authorize(self):
        for delivery in ('maa_import', 'copilot_codes', 'chat'):
            value = self.contract(); value['current_deliverable'] = delivery
            self.assertFalse(authorized(authorize(value)))
        value = self.contract('查一下作业'); self.assertFalse(authorized(authorize(value)))
        value = self.contract(); value['task_spec']['kind'] = 'base'
        self.assertFalse(authorized(authorize(value)))

    def test_negative_hypothetical_and_future_are_not_current_start_permission(self):
        for text in ('不要开始 MAA 自动战斗', '先不启动MAA自动战斗', '不开始作战，仅导入MAA',
                     '以后再开始MAA自动战斗', '如果点击开始MAA自动战斗会怎样？',
                     '告诉我如何开始MAA自动战斗', 'MAA自动战斗页面有开始按钮'):
            with self.subTest(text=text):
                self.assertFalse(authorized(authorize(self.contract(text))))

    def test_no_silent_reduction_of_repeat_or_clear_event(self):
        value = self.contract('开始MAA自动战斗刷六次'); value['task_spec']['repeat_count'] = 6
        self.assertFalse(authorized(authorize(value)))
        value = self.contract('开始MAA自动战斗刷六次')
        self.assertFalse(authorized(authorize(value)))
        value = self.contract('开始MAA自动战斗清完整个活动'); value['task_spec']['kind'] = 'clear_event'
        self.assertFalse(authorized(authorize(value)))

    def test_original_request_overrides_model_user_request(self):
        value = self.contract(); value['original_user_request'] = '只导入，不开始'
        self.assertFalse(authorized(authorize(value)))

    def test_current_start_can_choose_navigation_tool_conditionally(self):
        for text in (
            '如果MAA能导航就借助MAA，不能就黍，确认后启动MAA自动战斗，现在开始执行',
            '借助MAA打开游戏（如果可以的话，不可以的话就我自己手动打开），到正确关卡后启动MAA自动战斗，现在执行',
            '如果MAA不能导航就由黍导航，再启动MAA自动战斗一次',
            '借助MAA打开游戏（如果可以的话，不可以的话就我自己手动打开），'
            '同样的事，如果借助MAA可以实现，那么借助MAA，如果不可以的话，就黍来操作去到相关界面；'
            '确认到MAA要求的界面后，启动MAA自动战斗的开始，现在开始执行',
        ):
            with self.subTest(text=text):
                value = self.contract(text)
                self.assertTrue(authorized(authorize(value)))
                self.assertEqual(value['user_request'],text)

    def test_tool_condition_does_not_turn_hypothesis_or_question_into_permission(self):
        for text in (
            '如果MAA能导航，就启动MAA自动战斗，会怎样？',
            '如果开始MAA自动战斗，游戏会自己导航吗',
            '假如MAA能导航，之后如何开始MAA自动战斗',
            '比如MAA可以导航并启动MAA自动战斗',
            '以后如果MAA能导航，再启动MAA自动战斗',
            '如果可以的话，启动MAA自动战斗',
            '如果MAA能导航，也不要开始MAA自动战斗',
            '如果有一天MAA能导航，就启动MAA自动战斗',
        ):
            with self.subTest(text=text):
                self.assertFalse(authorized(authorize(self.contract(text))))


if __name__ == '__main__': unittest.main()

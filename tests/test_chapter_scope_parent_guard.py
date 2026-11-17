"""Model parent-as-chapter repair gate; no catalog, network or GUI activity."""
import unittest

from shu.task_contract import TaskContractError, parse_task_contract


def import_fields(request, *, activity='将进酒', chapter=None, stage='IW-EX-1'):
    return {
        'intent': 'execute', 'user_request': request, 'goal': request,
        'constraints': ['不开始作战'], 'completion_criteria': ['导入后核实加载'],
        'removed_constraints': [], 'reply': '查询核对后导入，不开始作战。',
        'relation': 'new', 'current_deliverable': 'maa_import',
        'activity_name': activity, 'chapter_scope': chapter,
        'target_stage_codes': [stage], 'task_spec': {'kind': 'import', 'stage_code': stage},
        'import_scope': {'kind': 'stages', 'stage_codes': [stage], 'difficulty': 3},
    }


class ChapterScopeParentGuardTests(unittest.TestCase):
    def test_new_explicit_stage_rejects_unrequested_parent_as_chapter_for_model_repair(self):
        request = ('新建独立任务：查询将进酒IW-EX-1普通和突袭的MAA作业并导入，按我的Box核对；'
                   '若同一份代码兼容两种难度就用单作业，备选只是替代，不开始作战。')
        with self.assertRaises(TaskContractError):
            parse_task_contract(import_fields(request, chapter={'name': '将进酒', 'ordinal': None}), request)

    def test_repaired_null_scope_preserves_exact_stage_and_both_difficulties(self):
        request = '新建任务：查将进酒IW-EX-1普通和突袭作业并导入MAA，不开始作战'
        result = parse_task_contract(import_fields(request), request).as_dict()
        self.assertNotIn('chapter_scope', result)
        self.assertEqual(result['target_stage_codes'], ['IW-EX-1'])
        self.assertEqual(result['import_scope']['stage_codes'], ['IW-EX-1'])
        self.assertEqual(result['import_scope']['difficulty'], 3)

    def test_explicit_same_named_chapter_is_retained_for_real_directory_verification(self):
        request = '查活动将进酒中名为将进酒的章节里的IW-EX-1作业并导入MAA，不开始作战'
        scope = {'name': '将进酒', 'ordinal': None}
        result = parse_task_contract(import_fields(request, chapter=scope), request).as_dict()
        self.assertEqual(result['chapter_scope'], scope)

    def test_ordinal_chapter_is_not_erased_even_if_name_repeats_parent(self):
        request = '查将进酒第二章IW-EX-1普通和突袭作业并导入MAA，不开始作战'
        scope = {'name': '将进酒', 'ordinal': 2}
        result = parse_task_contract(import_fields(request, chapter=scope), request).as_dict()
        self.assertEqual(result['chapter_scope'], scope)

    def test_distinct_named_chapter_is_retained_for_directory_agent(self):
        request = '查询时间啊向前的长冬初青TL-EX-1普通和突袭作业并导入MAA，不开始作战'
        scope = {'name': '长冬初青', 'ordinal': None}
        result = parse_task_contract(import_fields(request, activity='时间啊，向前！',
                                                   chapter=scope, stage='TL-EX-1'), request).as_dict()
        self.assertEqual(result['chapter_scope'], scope)


if __name__ == '__main__':
    unittest.main()

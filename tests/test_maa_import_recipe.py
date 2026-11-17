import unittest
from unittest.mock import Mock, patch

from tests import test_maa_agents as fixtures


class MaaImportRecipeTests(unittest.TestCase):
    setUp = fixtures.MaaAgentTests.setUp
    tearDown = fixtures.MaaAgentTests.tearDown
    context = fixtures.MaaAgentTests.context
    bind = fixtures.MaaAgentTests.bind

    def candidate(self):
        return {'copilot_id': '123', 'stage_name': 'level-test-1', 'difficulty': 1,
                'title': '真实关卡作业', 'path': str(self.directory / 'candidate.json'),
                'goal_stage': {'code': 'TEST-1', 'difficulty': 1}, 'source_binding': {'step_id': 'step'}}

    def page(self, number, *, value='', title=None, start_enabled=True, extra=()):
        e = fixtures.element
        controls = [e('page', 'MaaWpfGui.ViewModels.UI.CopilotViewModel', ['select'], selected=True),
                    e('mode', '主线/故事集/SideStory', ['select'], selected=True),
                    e('multi', '多作业模式', ['toggle'], toggle=0),
                    {**e('start', '开始', ['invoke']), 'enabled': start_enabled},
                    e('box', '', ['set_value'], automation_id='PART_TextBox', value=value)]
        if title:
            controls.append(e('title', title))
        return fixtures.snapshot(number, *controls, *extra)

    def run_recipe(self, records, *, options=None, prior=None):
        contract = {'allow_start': False, 'current_deliverable': 'maa_import'}
        if options is not None:
            contract['copilot_options'] = options
        self.current = self.store.submit('导入 TEST-1 作业', contract)
        preparation = Mock()
        preparation.submission_guard.return_value = None
        with patch.object(self.roles, '_candidates', return_value=records), \
                patch('shu.import_preparation.ImportPreparation', return_value=preparation):
            result = self.roles._run_import_recipe(self.context('maa', {'maa': prior or {}}), self.cancel)
        self.assertEqual(self.roles.clients['maa'].requests, [])
        self.assertIs(result['battle_start_submitted'], False)
        return result

    def effects(self):
        return [cmd['args'] for cmd in self.roles.supervisor.commands if cmd['op'] == 'act']

    def test_import_uses_exact_local_json_and_fresh_loaded_preview(self):
        record = self.candidate()
        self.bind([self.page(1), self.page(2, value=record['path'], start_enabled=False),
                   self.page(3, value=record['path'], title=record['title'])])
        self.roles.supervisor.revoked = True
        result = self.run_recipe([record])
        self.assertEqual(result['status'], 'import_verified')
        self.assertEqual([(e['action'], e['element_id'], e['value']) for e in self.effects()],
                         [('set_value', 'box', record['path'])])
        self.assertEqual(len(self.roles.supervisor.reconciliations), 1)
        proof = self.roles._read_ref(result['evidence_refs'][-1])
        self.assertEqual(proof['loaded_snapshot_id'], '3')
        self.assertEqual(proof['loaded_path'], record['path'])
        operation = self.store.get_operation(proof['operation_id'])
        self.assertEqual(operation['status'], 'verified')
        self.assertFalse(operation['starts'])

    def test_already_loaded_path_and_title_do_not_repeat_input(self):
        record = self.candidate()
        self.bind([self.page(1, value=record['path'], title=record['title'])])
        result = self.run_recipe([record], prior={'action_outcome': 'unverified'})
        self.assertEqual(result['status'], 'import_verified')
        self.assertTrue(self.roles._read_ref(result['evidence_refs'][-1])['already_loaded'])
        self.assertEqual(self.effects(), [])

    def test_same_title_from_prior_path_verifies_after_two_fresh_readbacks(self):
        record = self.candidate()
        after = [self.page(i, value=record['path'], title=record['title']) for i in (2, 3)]
        for snapshot in after:
            next(item for item in snapshot['elements'] if item['id'] == 'title')['id'] = 'reloaded-title'
        self.bind([self.page(1, value='previous-copy.json', title=record['title']), *after])
        result = self.run_recipe([record])
        self.assertEqual(result['status'], 'import_verified')
        self.assertEqual(len(self.effects()), 1)
        proof = self.roles._read_ref(result['evidence_refs'][-1])
        self.assertEqual(proof['loaded_snapshot_id'], '3')
        self.assertEqual(proof['stable_readbacks'], 2)
        self.assertIn('reloaded-title', proof['loaded_control_ids'])

    def test_old_title_node_and_new_input_value_do_not_prove_reload(self):
        record = self.candidate()
        self.bind([self.page(1, value='previous-copy.json', title=record['title'])]
                  + [self.page(i, value=record['path'], title=record['title']) for i in range(2, 7)])
        result = self.run_recipe([record])
        self.assertEqual(result['code'], 'maa_import_readback_unverified')
        self.assertEqual(len(self.effects()), 1)

    def test_conflicting_difficulty_and_echo_leave_import_unverified(self):
        record = self.candidate()
        conflict = fixtures.element('difficulty', '难度：突袭')
        self.bind([self.page(1)] + [self.page(number, value=record['path'], title=record['title'], extra=(conflict,))
                                   for number in range(2, 7)])
        result = self.run_recipe([record])
        self.assertEqual(result['code'], 'maa_import_readback_unverified')
        self.assertEqual(result['action_outcome'], 'unverified')
        self.assertEqual(len(self.effects()), 1)

    def test_prior_uncertain_effect_is_never_repeated(self):
        self.bind([self.page(1)])
        result = self.run_recipe([self.candidate()], prior={'action_outcome': 'unknown'})
        self.assertEqual(result['code'], 'maa_previous_effect_unverified')
        self.assertEqual(result['action_outcome'], 'unknown')
        self.assertEqual(self.effects(), [])

    def test_unsupported_stage_preview_cannot_verify_import(self):
        record = self.candidate()
        warning = fixtures.element('warning', '不支持的关卡 level-test-1，请等待地图资源更新')
        self.bind([self.page(1, value=record['path'], title=record['title'], extra=(warning,))]
                  + [self.page(i, value=record['path'], title=record['title'], extra=(warning,)) for i in range(2, 6)])
        result = self.run_recipe([record])
        self.assertEqual(result['code'], 'maa_import_readback_unverified')
        self.assertEqual(self.effects(), [])

    def test_unknown_supervisor_state_cannot_be_reopened_by_idle(self):
        self.bind([self.page(1)])
        self.roles.supervisor.revoked = self.roles.supervisor.unknown = True
        result = self.run_recipe([self.candidate()])
        self.assertEqual(result['action_outcome'], 'not_issued')
        self.assertTrue(self.roles.supervisor.unknown)
        self.assertEqual(self.roles.supervisor.reconciliations, [])

    def test_visible_stop_blocks_input_and_reconciliation(self):
        self.bind([self.page(1, extra=(fixtures.element('stop', '停止', ['invoke']),))])
        self.roles.supervisor.revoked = True
        result = self.run_recipe([self.candidate()])
        self.assertEqual(result['code'], 'maa_not_idle')
        self.assertEqual(self.effects(), [])
        self.assertEqual(self.roles.supervisor.reconciliations, [])

    def test_explicit_option_change_is_read_back_before_import(self):
        record = self.candidate()
        opt = fixtures.element('auto', '自动编队', ['toggle'], toggle=0)
        opt_set = {**opt, 'toggle': 1}
        self.bind([self.page(1, extra=(opt,)), self.page(2, extra=(opt_set,)),
                   self.page(3, value=record['path'], title=record['title'], extra=(opt_set,))])
        result = self.run_recipe([record], options={'auto_formation': True})
        self.assertEqual(result['status'], 'import_verified')
        self.assertEqual([(e['action'], e['element_id']) for e in self.effects()],
                         [('toggle', 'auto'), ('set_value', 'box')])


if __name__ == '__main__':
    unittest.main()

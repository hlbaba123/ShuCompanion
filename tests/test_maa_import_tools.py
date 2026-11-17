import threading
import unittest
from unittest.mock import Mock, patch

from shu import maa_agents
from tests import test_maa_agents as fixtures


class MaaImportToolTests(unittest.TestCase):
    setUp = fixtures.MaaAgentTests.setUp
    tearDown = fixtures.MaaAgentTests.tearDown
    context = fixtures.MaaAgentTests.context

    def test_tools_are_available_only_to_research(self):
        names = {'research_import_options', 'research_prepare_import'}
        self.assertTrue(names <= set(maa_agents._ROLE_TOOLS['research']))
        for role in ('leader', 'catalog', 'maa', 'diagnosis'):
            self.assertFalse(names & set(maa_agents._ROLE_TOOLS[role]))

    def test_lookup_cannot_turn_a_code_receipt_into_an_execution_candidate(self):
        context = self.context('research')
        context['contract']['current_deliverable'] = 'copilot_codes'
        with self.assertRaisesRegex(ValueError, '导入|执行'):
            self.roles._execute_tool('research', 'research_prepare_import',
                {'step_id': 's', 'slot': 'primary'}, context, {}, threading.Event())

    def test_preparation_retains_evidence_without_exposing_actions_to_model(self):
        context = self.context('research')
        context['contract'].update(current_deliverable='maa_import', source_task_id='source')
        state = {'candidate_refs': [], 'assessments': {}, 'verified_copilots': []}
        prepared = {'candidate_ref': {'path': 'candidate', 'sha256': 'hash'},
                    'candidate_record': {'copilot_id': '7', 'path': 'full.json', 'sha256': 'bodyhash'},
                    'goal_stage': {'stage_id': 'stage'},
                    'verified_copilot': {'copilot_id': '7', 'code': 'TEST-1', 'mystery_code': 'prts://7'},
                    'assessment': {'classification': 'satisfied'}}
        helper = Mock()
        helper.prepare.return_value = prepared
        with patch('shu.import_preparation.ImportPreparation', return_value=helper):
            result = self.roles._execute_tool('research', 'research_prepare_import',
                {'step_id': 's', 'slot': 'primary'}, context, state, self.cancel)
        self.assertEqual(state['candidate_refs'], [prepared['candidate_ref']])
        self.assertEqual(state['assessments']['7']['classification'], 'satisfied')
        self.assertEqual(result['verified_copilot']['mystery_code'], 'prts://7')
        self.assertNotIn('candidate_record', result)

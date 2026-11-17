"""Keep durable catalog provenance across repeated delegations."""
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch
from shu.maa_agents import MaaRoleAgents


class CatalogFinalizationTests(unittest.TestCase):
    def test_restored_catalog_refs_are_not_replaced_by_another_roles_history(self):
        agent=MaaRoleAgents.__new__(MaaRoleAgents)
        def restore(context,state):
            state.update(research_refs=[{'path':'catalog-proof'}],structure_ref={'path':'structure'},
                         activity_catalog={'ok':True,'stage_catalog_ref':{'path':'catalog'}})
        agent._restore_catalog=restore
        state=agent._role_state('catalog',{'role_results':{'research':{
            'research_refs':[{'path':'unrelated-research'}],
            'activity_catalog':{'ok':True,'stage_catalog_ref':{'path':'other-catalog'}}}}})
        self.assertEqual(state['research_refs'],[{'path':'catalog-proof'}])
        self.assertEqual(state['activity_catalog']['stage_catalog_ref'],{'path':'catalog'})

    def test_verified_directory_delivers_without_another_model_review(self):
        agent=MaaRoleAgents.__new__(MaaRoleAgents)
        agent.store=Mock();agent.directory=Path('.');agent._capabilities=Mock(return_value={})
        agent._guard=Mock()
        context={'task_id':'task','revision':1,'epoch':1,
                 'contract':{'current_deliverable':'stage_catalog'},
                 'role_results':{'catalog':{'structure_ref':{'path':'real'},'evidence_refs':[{'path':'proof'}]}}}
        with patch('shu.planned_agents.verify_planned_completion',return_value={'complete':True,'summary':'已按真实目录核实'}) as verify:
            result=agent._handler('leader')(context,threading.Event())
        self.assertEqual(result['next_role'],'complete')
        verify.assert_called_once()
        agent._guard.assert_called_once()

    def test_verified_codes_deliver_without_losing_research_state(self):
        agent=MaaRoleAgents.__new__(MaaRoleAgents)
        agent.store=Mock();agent.directory=Path('.');agent._capabilities=Mock(return_value={})
        agent._guard=Mock()
        context={'task_id':'task','revision':1,'epoch':1,
            'contract':{'current_deliverable':'copilot_codes'},
            'role_results':{'research':{'closed_not_retryable':True,'status':'researched'}}}
        with patch('shu.maa_agents._planned_precheck',return_value={'complete':True,'closed':True,'summary':'代码凭据已核实'}):
            result=agent._handler('leader')(context,threading.Event())
        self.assertEqual(result['next_role'],'complete')

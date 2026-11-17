"""Advertised availability is separate from the current task's authority."""
import json
from types import SimpleNamespace
import unittest

from shu.battle_contract import authorize
from shu.controller import SessionController
from shu.leader_policy import runtime_capabilities


class BattleCapabilitiesTests(unittest.TestCase):
    def summary(self, contract, *, screen=True, inputs=True):
        stub=SimpleNamespace(
            current_task=lambda:None, game_input_allowed=inputs, screen_capture_allowed=screen,
            windows=SimpleNamespace(bound_window=lambda:None), legacy_tools=False, _task_id=None,
            execution=SimpleNamespace(active=False,contract=lambda task_id:contract))
        if contract:
            stub._task_id='task'
            stub.current_task=lambda:{'id':'task','goal':contract.get('goal','goal')}
        return json.loads(SessionController._task_summary(stub))

    def test_available_loaded_job_start_does_not_mean_general_gameplay(self):
        caps=runtime_capabilities(True,True)
        self.assertTrue(caps['battle_start'])
        self.assertTrue(caps['maa_loaded_job_start'])
        self.assertTrue(caps['game_navigation'])
        self.assertFalse(caps['direct_game_input'])
        self.assertEqual(caps['execution_scope'],'loaded_single_job_once')
        self.assertNotIn('battle_start',caps['unavailable_actions'])
        self.assertIn('direct_game_input',caps['unavailable_actions'])

    def test_missing_screen_or_input_is_still_unavailable(self):
        for screen,inputs in ((False,True),(True,False),(False,False)):
            caps=runtime_capabilities(screen,inputs)
            self.assertFalse(caps['battle_start'])
            self.assertFalse(caps['game_navigation'])

    def test_new_request_advertises_support_without_existing_authority(self):
        facts=self.summary(None)
        self.assertTrue(facts['capabilities']['battle_start'])
        self.assertFalse(facts['battle_start_allowed'])
        self.assertEqual(facts['battle_start_authority'],'current_task_only')

    def test_only_import_task_never_gains_current_battle_authority(self):
        facts=self.summary(authorize({'current_deliverable':'maa_import',
            'user_request':'导入MAA，不开始作战','task_spec':{'kind':'import'}}))
        self.assertTrue(facts['capabilities']['battle_start'])
        self.assertFalse(facts['battle_start_allowed'])

    def test_current_bounded_contract_and_permissions_are_both_required(self):
        contract=authorize({'current_deliverable':'gameplay','user_request':'启动MAA自动战斗一次',
                            'task_spec':{'kind':'repeat_stage','repeat_count':1}})
        self.assertTrue(self.summary(contract)['battle_start_allowed'])
        self.assertFalse(self.summary(contract,inputs=False)['battle_start_allowed'])
        self.assertFalse(self.summary(contract,screen=False)['battle_start_allowed'])


if __name__=='__main__':unittest.main()

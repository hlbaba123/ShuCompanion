"""Capability upgrades cannot grant history a new start permission on Continue."""
import unittest

from shu.battle_contract import authorize
from shu.providers import ProviderError
from tests import test_maa_coordinator as fixtures


class BattleResumeTests(unittest.TestCase):
    setUp=fixtures.MaaCoordinatorTests.setUp
    tearDown=fixtures.MaaCoordinatorTests.tearDown
    make_coordinator=fixtures.MaaCoordinatorTests.make_coordinator

    def test_old_read_only_contract_without_new_default_fields_can_resume(self):
        task=self.store.submit('查目录',{'goal':'查目录','current_deliverable':'stage_catalog'})
        self.store.control(task['task_id'],'pause')
        coordinator=self.make_coordinator()
        self.assertTrue(coordinator.resume(task['task_id'])['accepted'])

    def test_previous_ungranted_battle_never_upgrades_on_resume(self):
        contract=authorize({'goal':'开始MAA自动战斗','user_request':'开始MAA自动战斗',
            'current_deliverable':'gameplay','task_spec':{'kind':'repeat_stage'}})
        contract.update(allow_start=False,attempt_limit=0,workflow_scope='gui_assist',
                        execution_restrictions={'battle_start':False,'direct_game_input':False})
        task=self.store.submit(contract['goal'],contract)
        self.store.control(task['task_id'],'pause')
        coordinator=self.make_coordinator()
        with self.assertRaises(ProviderError):coordinator.resume(task['task_id'])
        self.assertFalse(self.store.current(task['task_id'])['writes_enabled'])

    def test_invalid_persisted_battle_scope_never_resumes(self):
        contract=authorize({'goal':'开始MAA自动战斗','user_request':'开始MAA自动战斗',
            'current_deliverable':'gameplay','task_spec':{'kind':'repeat_stage'}})
        contract['attempt_limit']=100
        task=self.store.submit(contract['goal'],contract)
        self.store.control(task['task_id'],'pause')
        coordinator=self.make_coordinator()
        with self.assertRaises(ProviderError):coordinator.resume(task['task_id'])


if __name__=='__main__':unittest.main()

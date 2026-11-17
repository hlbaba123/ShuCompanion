"""Public MAA settings bound to an exact process; no GUI or game input."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shu import maa_queue_receipt as receipts
from shu.maa_battle_settings import verify_single_run, MaaBattleSettingsError


class BattleSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.maa=Path(self.temp.name)/'MAA';(self.maa/'config').mkdir(parents=True)
        self.exe=self.maa/'MAA.exe';self.exe.write_bytes(b'offline executable')
        (self.maa/'MAA.dll').write_bytes(b'offline version resource')
        self.config=self.maa/'config/gui.new.json'
        self.identity={'hwnd':1,'pid':2,'creation_time':3,'executable':str(self.exe)}
        for name,value in (('_MAA_ROOT',self.maa),('_MAA_EXECUTABLE',self.exe)):
            item=patch.object(receipts,name,value);item.start();self.addCleanup(item.stop)
        item=patch.object(receipts,'_process_matches',return_value=True)
        self.matches=item.start();self.addCleanup(item.stop)
        item=patch.object(receipts,'_product_version',return_value='v6.22.0+901e01c600f4895ac4e7e48a9b471fc9f52af301')
        self.version=item.start();self.addCleanup(item.stop)
        self.document={'Current':'active','Configurations':{
            'Default':{'Copilot':{'LoopTimes':99}},
            'active':{'Copilot':{'LoopTimes':1},'RemoteControl':{'Token':'NEVER_PUBLISH'}}}}
        self.save()

    def save(self):self.config.write_text(json.dumps(self.document),encoding='utf-8')

    def test_current_config_single_limit_delivers_only_whitelisted_proof(self):
        before=self.config.read_bytes();proof=verify_single_run(self.identity)
        self.assertEqual(set(proof),{'settings_sha256','loop_limit'})
        self.assertEqual(proof['loop_limit'],1)
        self.assertRegex(proof['settings_sha256'],r'^[0-9a-f]{64}$')
        self.assertNotIn('NEVER_PUBLISH',repr(proof))
        self.assertEqual(self.config.read_bytes(),before)

    def test_bad_or_nonliteral_loop_limits_are_rejected(self):
        for value in (0,2,100,True,1.,'1',None):
            self.document['Configurations']['active']['Copilot']['LoopTimes']=value;self.save()
            with self.subTest(value=value),self.assertRaises(MaaBattleSettingsError):
                verify_single_run(self.identity)

    def test_missing_loop_limit_never_infers_the_gui_default(self):
        del self.document['Configurations']['active']['Copilot']['LoopTimes'];self.save()
        with self.assertRaises(MaaBattleSettingsError):verify_single_run(self.identity)

    def test_unselected_config_cannot_supply_a_safe_limit(self):
        self.document['Configurations']['active']['Copilot']['LoopTimes']=3
        self.document['Configurations']['Default']['Copilot']['LoopTimes']=1;self.save()
        with self.assertRaises(MaaBattleSettingsError):verify_single_run(self.identity)

    def test_unknown_selected_config_or_missing_copilot_is_rejected(self):
        self.document['Current']='absent';self.save()
        with self.assertRaises(MaaBattleSettingsError):verify_single_run(self.identity)
        self.document['Current']='active';del self.document['Configurations']['active']['Copilot'];self.save()
        with self.assertRaises(MaaBattleSettingsError):verify_single_run(self.identity)

    def test_duplicate_json_keys_are_rejected(self):
        self.config.write_bytes(b'{"Current":"active","Current":"Default","Configurations":{}}')
        with self.assertRaises(MaaBattleSettingsError):verify_single_run(self.identity)

    def test_settings_hash_ignores_unrelated_secrets_and_task_history(self):
        first=verify_single_run(self.identity)
        current=self.document['Configurations']['active']
        current['RemoteControl']['Token']='ANOTHER_SECRET'
        current['Copilot']['TaskList']=[{'Anything':'historical rows do not grant battle rights'}]
        self.save()
        self.assertEqual(verify_single_run(self.identity),first)

    def test_selected_config_change_changes_proof_even_with_same_limit(self):
        first=verify_single_run(self.identity)
        self.document['Configurations']['second']={'Copilot':{'LoopTimes':1}}
        self.document['Current']='second';self.save()
        self.assertNotEqual(first,verify_single_run(self.identity))

    def test_different_or_invalid_process_cannot_read_as_current(self):
        for changes in ({'pid':True},{'creation_time':0},{'executable':str(self.maa/'Other.exe')}):
            with self.subTest(changes=changes),self.assertRaises(MaaBattleSettingsError):
                verify_single_run({**self.identity,**changes})
        self.matches.return_value=False
        with self.assertRaises(MaaBattleSettingsError):verify_single_run(self.identity)

    def test_process_replacement_during_read_is_rejected(self):
        self.matches.side_effect=[True,False]
        with self.assertRaises(MaaBattleSettingsError):verify_single_run(self.identity)

    def test_unreviewed_maa_version_cannot_inherit_audited_single_task_rules(self):
        self.version.return_value='v99.0.0'
        with self.assertRaises(MaaBattleSettingsError):verify_single_run(self.identity)

    def test_missing_unreadable_config_is_rejected(self):
        self.config.unlink()
        with self.assertRaises(MaaBattleSettingsError):verify_single_run(self.identity)


if __name__=='__main__':unittest.main()

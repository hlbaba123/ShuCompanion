"""Final-EXE path layout: public cache belongs to the data profile, not bundle."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock,patch

from shu.maa_battle import BattleRunner
from shu.level_catalog import find_levels as real_find_levels
from tests.test_level_catalog import Transport,payload


class BattleFrozenCatalogTests(unittest.TestCase):
    def test_loaded_job_directory_uses_explicit_profile_boundary_in_frozen_layout(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);profile=base/'separate-profile';bundle=base/'release'/'_internal'
            runner=BattleRunner.__new__(BattleRunner)
            runner.root=profile/'task-private'/'task'/'maa-battle'/'1'
            runner.root.mkdir(parents=True)
            runner.roles=SimpleNamespace(directory=profile)
            runner.store=MagicMock();runner.cancel=threading.Event()
            runner.context={'contract':{}}
            runner.snapshot=MagicMock(return_value={'screenshot_id':'invented-offline-frame'})
            runner._single_mode=MagicMock()
            runner.vision=SimpleNamespace(inspect_maa=MagicMock(return_value={
                'page':'copilot','running':False,'multi_mode':False,'stage_code':'TEST-7',
                'preview_title':'Example TEST-7','mystery_code':'prts://123',
                'start_box':{'x':.1,'y':.1,'width':.1,'height':.1}}))
            document={'stage_name':'test_internal_a','doc':{'title':'Example TEST-7'},
                      'opers':[{'name':'Offline Operator'}],'actions':[{'type':'SpeedUp'}]}
            raw=json.dumps(document).encode();net=Transport(payload())
            runner.publish=MagicMock()
            runner.progress=MagicMock()
            def lookup(query,directory,**kwargs):
                return real_find_levels(query,directory,transport=net,**kwargs)
            with patch('shu.level_catalog.WORKSPACE_ROOT',bundle), \
                 patch('shu.level_catalog.find_levels',side_effect=lookup), \
                 patch('shu.maa_queue_receipt._read_stable',return_value=(raw,{})), \
                 patch('shu.maa_facts.RosterStore') as roster, \
                 patch('shu.activity_public_facts.stage_facts',return_value={'ok':True,'groups':[
                     {'group_id':'public-zone-b','label':'第二地图','stage_ids':['test_internal_a'],
                      'source_refs':[{'path':'zone.json','sha256':'a'*64}]}]}), \
                 patch('shu.roster_match.assess_requirements',return_value={'classification':'satisfied'}):
                roster.return_value.current.return_value={'operators':[],'box_revision':1}
                result=runner._loaded()
            self.assertEqual(result['stage_code'],'TEST-7')
            self.assertEqual(result['stage_id'],'test_internal_a')
            self.assertEqual(result.get('chapter_name'),'第二地图')
            self.assertEqual(len(net.calls),1)
            self.assertTrue((runner.root/'catalog'/'level-catalog-cache.json').is_file())
            self.assertFalse(bundle.exists())


if __name__=='__main__':unittest.main()

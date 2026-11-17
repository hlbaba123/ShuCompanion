import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shu.core import TaskEngine
from shu.workflow_store import WorkflowStore
from shu.catalog_memory import linked_catalog

class CatalogMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.engine=TaskEngine(self.root); self.workflow=WorkflowStore(self.engine)
        self.src=self.workflow.submit('目录', {'activity_name':'示例 活动'})
        self.dst=self.workflow.submit('代码', {'activity_name':'示例活动','source_task_id':self.src['task_id'], 'allow_start':False})
        self.dispatch=self.workflow.ensure_dispatch(self.src['task_id'],'catalog','catalog',{})
        self.catalog={'event_name':'示例 活动','source':'https://prts.maa.plus/arknights/level','raw_sha256':'abc','catalog_complete':True,'has_more':False,'total_stages':1,'stages':[{'code':'X-1'}]}
        self.cref=self.save('catalog.json',self.catalog)
        result={'ok':True,'tool':'activity_stages','event_name':'示例 活动','source':self.catalog['source'],'raw_sha256':'abc','catalog_complete':True,'has_more':False,'total_stages':1,'stages':self.catalog['stages'],'stage_catalog_ref':self.cref,'allow_start':True}
        self.oref=self.save('obs.json', {'task_id':self.src['task_id'],'epoch':self.src['epoch'],'dispatch_id':self.dispatch['dispatch_id'],'read_only':True,'tool':'research_activity_stages','result':result})
        self.workflow.finish_dispatch(self.dispatch['dispatch_id'],{'status':'completed','evidence_refs':[self.oref]})
    def tearDown(self):
        self.engine.close();self.tmp.cleanup()
    def save(self,name,obj):
        raw=json.dumps(obj,ensure_ascii=False).encode(); p=self.root/name;p.write_bytes(raw)
        return {'path':str(p),'sha256':hashlib.sha256(raw).hexdigest()}
    def get(self,**kw): return linked_catalog(self.workflow,self.dst['task_id'],self.root,**kw)
    def test_reuses_only_public_facts_in_new_task_path(self):
        value=self.get();self.assertTrue(value['ok']); self.assertTrue(value['cached'])
        self.assertEqual(value['reused_from']['source_dispatch_id'],self.dispatch['dispatch_id'])
        self.assertTrue(Path(value['stage_catalog_ref']['path']).is_relative_to(self.root/'research'/self.dst['task_id']/'activity-cache'))
        self.assertNotIn('allow_start',value)
        self.assertEqual(Path(value['stage_catalog_ref']['path']).read_bytes(),Path(self.cref['path']).read_bytes())
    def test_expired_and_future_rejected(self):
        self.assertIsNone(self.get(now=datetime.now(timezone.utc)+timedelta(seconds=901)))
        self.assertIsNone(self.get(now=datetime.now(timezone.utc)-timedelta(days=1)))
    def test_source_tamper_rejected(self):
        Path(self.cref['path']).write_text('{}',encoding='utf8');self.assertIsNone(self.get())
    def test_observation_tamper_rejected(self):
        Path(self.oref['path']).write_text('{}',encoding='utf8');self.assertIsNone(self.get())
    def test_different_activity_rejected(self):
        self.workflow.replace_contract(self.dst['task_id'],'新目标',{'activity_name':'其他','source_task_id':self.src['task_id']},expected_revision=1,expected_epoch=1)
        self.assertIsNone(self.get())
    def test_source_revision_changed_rejected(self):
        self.workflow.replace_contract(self.src['task_id'],'改变',{'activity_name':'示例 活动'},expected_revision=1,expected_epoch=1)
        self.assertIsNone(self.get())
    def replace_result(self, ref):
        from shu.workflow_store import _encode
        encoded, digest = _encode({'status':'completed','evidence_refs':[ref]})
        self.engine._db.execute('UPDATE workflow_results SET result=?, payload_hash=? WHERE dispatch_id=?', (encoded,digest,self.dispatch['dispatch_id']))
    def test_mismatched_observation_identity_rejected(self):
        obs=json.loads(Path(self.oref['path']).read_text(encoding='utf8'));obs['dispatch_id']='other'
        bad=self.save('bad-obs.json',obs)
        self.replace_result(bad)
        self.assertIsNone(self.get())
    def test_incomplete_catalog_rejected(self):
        self.catalog['total_stages']=2
        bad=self.save('bad-catalog.json',self.catalog)
        obs=json.loads(Path(self.oref['path']).read_text(encoding='utf8'));obs['result']['stage_catalog_ref']=bad
        obsref=self.save('bad-obs.json',obs)
        self.replace_result(obsref)
        self.assertIsNone(self.get())

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from shu.core import TaskEngine
from shu.workflow_store import WorkflowStore
from shu.leader_plan import PlanStore
from shu.code_delivery import CodeDeliveryStore, CodeTools
from shu.maa_roster_tools import RosterTools
from shu.copilot_pool import CopilotPool
from shu.copilot_catalog import parse_candidate

class Clock:
    def __init__(self): self.t=0
    def __call__(self): return self.t

class PairIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.engine=TaskEngine(self.root); self.addCleanup(self.engine.close)
        self.workflow=WorkflowStore(self.engine); self.rosters=RosterTools(self.engine,self.root)
        self.rosters.facts.publish('local-default',[{'name':'甲','owned':True,'rarity':6,'elite':2}],True,
            {'kind':'user-roster-spreadsheet','observed_at':1000,'evidence_ref':'fixture:roster'},explicit_ownership=True)
        state=self.workflow.submit('fixture',{'current_deliverable':'copilot_codes'}); self.task=state['task_id']
        self.plans=PlanStore(self.workflow); self.store=CodeDeliveryStore(self.workflow,self.root)
        self.stage={'stage_key':'stage1','display_code':'A-1','stage_id':'unit_stage','query_key':'unit_stage',
                    'query_key_verified':True,'difficulty_verified':True,'difficulty':1,'group_id':'g'}
        structure={'ok':True,'catalog_complete':True,'activity_id':'event','activity_name':'活动',
                   'stages':[self.stage], 'groups':[{'group_id':'g','label':'实际分区','stage_keys':['stage1']}], 'excluded_stage_keys':[]}
        raw=json.dumps(structure,ensure_ascii=False).encode(); path=self.root/'structure.json'; path.write_bytes(raw)
        self.plans.register(self.task,expected_revision=1,expected_epoch=1,structure_ref={'path':str(path),'sha256':hashlib.sha256(raw).hexdigest()},ordered_steps=['stage1'],search_policy={})
        dispatch=self.workflow.ensure_dispatch(self.task,'research','fixture',{})
        self.lease=self.plans.claim_next(self.task,expected_revision=1,expected_epoch=1,dispatch_id=dispatch['dispatch_id'])
        self.pool=CopilotPool(self.root/'cache'); self.clock=Clock()
    def candidate(self, ident, name='甲'):
        c=parse_candidate({'id':ident,'available':True,'content':json.dumps({'stage_name':'unit_stage','difficulty':1,'opers':[{'name':name}]})})
        c.update(stage_match_verified=True,difficulty_match='compatible')
        c['candidate_locator']={**c['candidate_locator'],'resolved_stage':self.stage}
        return c
    def ref(self, candidates, complete=False):
        pool={'schema':1,'pool_id':CopilotPool.identity('unit_stage',None),'stage_id':'unit_stage','document':None,
              'candidates':candidates,'unverified_candidates':[], 'sources':['fixture'],
              'coverage':{'collected':len(candidates),'total':len(candidates) if complete else 99,'complete':complete,'exhausted':complete,'next_page':None if complete else 2,'pages_received':1,'raw_fetched':len(candidates),'unverified':0,'total_changed':False}}
        return self.pool._save(pool)
    def screen(self, ref): return self.rosters.screen_pool('local-default',ref)['screen_ref']
    def test_real_receipts_reject_tampering_and_box_changes(self):
        ref=self.ref([self.candidate(1),self.candidate(2)],True); scr=self.screen(ref)
        one=self.store.register_code(self.lease,self.candidate(1)['candidate_locator'],scr)
        self.assertEqual(one,self.store.register_code(self.lease,self.candidate(1)['candidate_locator'],scr))
        two=self.store.register_code(self.lease,self.candidate(2)['candidate_locator'],scr)
        self.assertNotEqual(self.store.read_code(one)['id'],self.store.read_code(two)['id'])
        self.rosters.facts.publish('local-default',[{'name':'甲','owned':False}],True,{'kind':'user-roster-spreadsheet','observed_at':2000,'evidence_ref':'fixture:new'},explicit_ownership=True)
        with self.assertRaises(ValueError): self.store.read_code(one)
        Path(scr['path']).write_text('{}',encoding='utf8')
        with self.assertRaises(ValueError): self.store.read_code(two)
    def test_screening_time_counts_against_backup_deadline(self):
        first=self.ref([self.candidate(1)]); second=self.ref([self.candidate(1),self.candidate(2)],True)
        clock=self.clock
        class Research:
            def candidates_for_stage(_, stage, cache_only=False, before_fetch=None, **kwargs):
                if cache_only: return {'pool_ref':first,'coverage':{'complete':False},'pages_fetched':0}
                before_fetch(2); return {'pool_ref':second,'coverage':{'complete':True},'pages_fetched':1}
        rosters=self.rosters
        class SlowRoster:
            calls=0
            def screen_pool(_, *args):
                _.calls+=1
                if _.calls==2: clock.t+=31
                return rosters.screen_pool(*args)
        result=CodeTools(self.store,self.plans,Research(),SlowRoster()).find_pair(self.lease,clock=clock)
        self.assertEqual(result['status'],'primary_only')
        self.assertGreaterEqual(result['budget_used']['backup_milliseconds'],30000)
    def test_backup_two_page_budget_and_unknown_preserves_primary(self):
        first=self.ref([self.candidate(1)]); later=self.ref([self.candidate(1),self.candidate(3,'未拥有')])
        class Research:
            pages=0
            def candidates_for_stage(_, stage, cache_only=False, before_fetch=None, **kw):
                if cache_only: return {'pool_ref':first,'coverage':{'complete':False},'pages_fetched':0}
                _.pages+=1; before_fetch(_.pages); return {'pool_ref':later,'coverage':{'complete':False},'pages_fetched':1}
        research=Research()
        result=CodeTools(self.store,self.plans,research,self.rosters).find_pair(self.lease,clock=self.clock)
        self.assertEqual(result['status'],'primary_only'); self.assertEqual(research.pages,2)
        self.assertEqual(result['budget_used']['backup_pages'],2)
        self.assertEqual(result['backup_reason'],'budget_exhausted')

    def test_unknown_prevents_no_box_match_after_exhaustion(self):
        ref=self.ref([self.candidate(3,'未拥有')],True)
        class Research:
            def candidates_for_stage(_, *args, **kwargs):
                return {'pool_ref':ref,'coverage':{'complete':True},'pages_fetched':0}
        result=CodeTools(self.store,self.plans,Research(),self.rosters).find_pair(self.lease,clock=self.clock)
        self.assertEqual(result['status'],'search_incomplete')

    def test_primary_budget_exhaustion_never_delivers(self):
        ref=self.ref([])
        class Research:
            pages=0
            def candidates_for_stage(_, stage, cache_only=False, before_fetch=None, **kw):
                if cache_only: return {'pool_ref':ref,'coverage':{'complete':False},'pages_fetched':0}
                _.pages+=1; before_fetch(_.pages); return {'pool_ref':ref,'coverage':{'complete':False},'pages_fetched':1}
        research=Research(); result=CodeTools(self.store,self.plans,research,self.rosters).find_pair(self.lease,clock=self.clock)
        self.assertEqual(result['status'],'search_incomplete'); self.assertEqual(research.pages,4)
        self.assertIsNone(result['primary_ref'])
    def test_backup_registration_time_cannot_exceed_deadline(self):
        ref=self.ref([self.candidate(1),self.candidate(2)],True); clock=self.clock; store=self.store
        class SlowDelivery:
            directory=store.directory
            engine=store.engine
            read_code=store.read_code
            calls=0
            def register_code(_, *args):
                _.calls+=1; result=store.register_code(*args)
                if _.calls==2: clock.t+=31
                return result
        class Research:
            def candidates_for_stage(_, *args, **kwargs):
                return {'pool_ref':ref,'coverage':{'complete':True},'pages_fetched':0}
        result=CodeTools(SlowDelivery(),self.plans,Research(),self.rosters).find_pair(self.lease,clock=clock)
        self.assertEqual(result['status'],'primary_only')
    def test_resume_existing_receipt_after_primary_budget_lost(self):
        candidate=self.candidate(1); pool=self.ref([candidate]); screen=self.screen(pool)
        primary=self.store.register_code(self.lease,candidate['candidate_locator'],screen)
        self.plans.reserve_budget(self.lease,phase='primary',pages=4,milliseconds=30000)
        class Research:
            calls=0
            def candidates_for_stage(_, stage, cache_only=False, before_fetch=None, **kw):
                if cache_only: return {'pool_ref':pool,'coverage':{'complete':False},'pages_fetched':0}
                _.calls+=1; before_fetch(_.calls); return {'pool_ref':pool,'coverage':{'complete':False},'pages_fetched':1}
        research=Research(); result=CodeTools(self.store,self.plans,research,self.rosters).find_pair(self.lease,clock=self.clock)
        self.assertEqual(result['primary_ref'],primary)
        self.assertEqual(research.calls,2)

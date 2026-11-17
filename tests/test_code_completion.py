import unittest
from shu.code_delivery import CodeDeliveryStore

class CompletionTests(unittest.TestCase):
    def test_all_steps_need_verified_primary_and_distinct_backup(self):
        store = CodeDeliveryStore.__new__(CodeDeliveryStore)
        class Plans:
            def current(self, task):
                return {'revision':1, 'stages':[{'stage_key':'a','display_code':'A'}, {'stage_key':'b','display_code':'B'}],
                    'steps':[{'step_id':'1','stage_key':'a','status':'two_verified',
                              'result':{'primary_ref':{'code':'x'},'backup_ref':{'code':'x'}}},
                             {'step_id':'2','stage_key':'b','status':'pending','result':None}]}
        store.plans = Plans()
        store.read_code = lambda ref, verify_current=True: {'task_id':'task','step_id':'1','revision':1,'code':ref['code']}
        summary = store.summary('task')
        self.assertFalse(summary['complete'])
        self.assertEqual(summary['missing'], ['A', 'B'])

from tests.test_code_pair_search import PairIntegration
from shu.job_library import JobLibrary
from shu.core import ActionRejected
from pathlib import Path

class RealCompletionTests(PairIntegration):
    def test_whole_task_needs_durable_research_result_and_current_epoch(self):
        candidate=self.candidate(1); ref=self.ref([candidate],True)
        receipt=self.store.register_code(self.lease,candidate['candidate_locator'],self.screen(ref))
        self.plans.close_step(self.lease,{'status':'primary_only','primary_ref':receipt,'backup_ref':None})
        with self.assertRaises(ActionRejected):
            self.workflow.complete_planned_verified(self.task,expected_epoch=1)
        self.workflow.finish_dispatch(self.lease['dispatch_id'],{'status':'researched','steps':[{'status':'primary_only'}]})
        completed=self.workflow.complete_planned_verified(self.task,expected_epoch=1)
        self.assertEqual(completed['status'],'completed')
        self.assertFalse(self.workflow.current(self.task)['writes_enabled'])
    def test_same_id_different_pool_cannot_close_as_two(self):
        candidate=self.candidate(1)
        a=self.ref([candidate]); first=self.store.register_code(self.lease,candidate['candidate_locator'],self.screen(a))
        b=self.ref([candidate,self.candidate(2)],True); second=self.store.register_code(self.lease,candidate['candidate_locator'],self.screen(b))
        self.assertNotEqual(first,second)
        with self.assertRaises(ActionRejected):
            self.plans.close_step(self.lease,{'status':'two_verified','primary_ref':first,'backup_ref':second})
    def test_real_receipt_primary_only_export_and_summary(self):
        candidate=self.candidate(1); ref=self.ref([candidate],True)
        receipt=self.store.register_code(self.lease,candidate['candidate_locator'],self.screen(ref))
        self.plans.close_step(self.lease,{'status':'primary_only','primary_ref':receipt,'backup_ref':None})
        summary=self.store.summary(self.task)
        self.assertTrue(summary['complete'])
        library=JobLibrary(self.root/'library',self.store)
        output=library.export_step(self.task,self.lease['step_id'])
        self.assertEqual(output,library.export_step(self.task,self.lease['step_id']))
        self.assertEqual(Path(output['primary_path']).read_text(encoding='utf8'),'prts://1\n')
        self.assertIsNone(output['backup_path'])
    def test_two_distinct_real_receipts_close_and_export(self):
        c1,c2=self.candidate(1),self.candidate(2); pool=self.ref([c1,c2],True); screen=self.screen(pool)
        a=self.store.register_code(self.lease,c1['candidate_locator'],screen)
        b=self.store.register_code(self.lease,c2['candidate_locator'],screen)
        self.plans.close_step(self.lease,{'status':'two_verified','primary_ref':a,'backup_ref':b})
        summary=self.store.summary(self.task)
        self.assertTrue(summary['complete']); self.assertEqual(summary['items'][0]['backup_code'],'prts://2')
        library=JobLibrary(self.root/'library',self.store); output=library.export_step(self.task,self.lease['step_id'])
        self.assertEqual(Path(output['backup_path']).read_text(encoding='utf8'),'prts://2\n')
        self.rosters.facts.publish('local-default',[{'name':'甲','owned':False}],True,
            {'kind':'user-roster-spreadsheet','observed_at':2000,'evidence_ref':'fixture:new'},explicit_ownership=True)
        self.assertFalse(self.store.summary(self.task)['complete'])

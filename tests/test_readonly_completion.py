"""Read-only results finish only with current durable tool evidence."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from shu.core import TaskEngine, ActionRejected
from shu.workflow_store import WorkflowStore

class ReadonlyCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.engine=TaskEngine(self.root); self.store=WorkflowStore(self.engine)
        self.state=self.store.submit('只读查询公开资料',{'goal':'只读查询公开资料','task_spec':{'kind':'lookup'}})
    def tearDown(self): self.engine.close(); self.temp.cleanup()
    def evidence(self,*,publish=True):
        s=self.state
        d=self.store.ensure_dispatch(s['task_id'],'research','research-proof',{'contract':s['contract']})
        path=self.root/'evidence/public.json'; path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'task_id':s['task_id'],'epoch':s['epoch'],'dispatch_id':d['dispatch_id'],
            'read_only':True,'tool':'research_search','result':{'ok':True,'data':{'results':[{'url':'https://example.org/public'}]}}}),encoding='utf-8')
        refs=[{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]
        if publish:self.store.finish_dispatch(d['dispatch_id'],{'status':'researched','evidence_refs':refs})
        return refs
    def complete(self,refs):
        return self.store.complete_research_verified(self.state['task_id'],refs,expected_epoch=self.state['epoch'])
    def test_current_durable_research_completes_lookup_without_any_game_runs(self):
        result=self.complete(self.evidence())
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['completed_runs'],0)
        self.assertFalse(self.store.current(self.state['task_id'])['writes_enabled'])
    def test_model_claim_or_unregistered_file_cannot_complete(self):
        with self.assertRaises(ActionRejected):self.complete(self.evidence(publish=False))
    def test_late_result_after_pause_does_not_complete(self):
        refs=self.evidence(); self.store.control(self.state['task_id'],'pause')
        with self.assertRaises(ActionRejected):self.complete(refs)
    def test_changed_proof_file_does_not_complete(self):
        refs=self.evidence(); Path(refs[0]['path']).write_text('changed')
        with self.assertRaises((ValueError,ActionRejected)):self.complete(refs)
    def test_research_cannot_cover_repeated_battle_goal(self):
        self.state=self.store.submit('TL-10刷六次',{'goal':'TL-10刷六次','task_spec':{'kind':'repeat_stage'}})
        with self.assertRaises(ActionRejected):self.complete(self.evidence())

    def copilot_evidence(self, *, match_roster=False):
        goal='只读查询TL-10作业JSON和神秘代码，不导入MAA'
        self.state=self.store.submit(goal,{'goal':goal,'match_roster':match_roster,'task_spec':{'kind':'lookup','stage_code':'TL-10'}})
        s=self.state;d=self.store.ensure_dispatch(s['task_id'],'research','full-copilot',{'contract':s['contract']})
        def save(name,value):
            path=self.root/name;path.write_text(json.dumps(value),encoding='utf-8')
            return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        download=save('copilot.json',{'stage_name':'stage-internal','actions':[{'type':'Deploy'}]})
        stage={'task_id':s['task_id'],'epoch':s['epoch'],'goal':goal,'query':'TL-10',
               'source':'https://prts.maa.plus/arknights/level','raw_sha256':'b'*64,
               'stage_id':'stage-internal','level_id':'level-internal','code':'TL-10'}
        stage={**stage,'evidence_ref':save('stage-proof.json',stage)}
        record={'task_id':s['task_id'],'epoch':s['epoch'],'research_dispatch_id':d['dispatch_id'],
            'copilot_id':'123','path':download['path'],'sha256':download['sha256'],
            'source_url':'https://prts.maa.plus/copilot/get/123','stage_name':'stage-internal',
            'title':'fixture','action_count':1,'goal_stage':stage}
        candidate=save('candidate.json',record)
        proof=save('download-proof.json',{'task_id':s['task_id'],'epoch':s['epoch'],
            'dispatch_id':d['dispatch_id'],'read_only':True,'tool':'research_download',
            'result':{'ok':True,'path':download['path'],'sha256':download['sha256']}})
        facts={k:record[k] for k in ('copilot_id','path','sha256','stage_name','title','source_url')}
        facts.update(code='TL-10',mystery_code='prts://123',detail_url='https://prts.plus/?op=123')
        result={'status':'researched','candidate_refs':[candidate],'evidence_refs':[candidate,proof],
                'verified_copilots':[facts]}
        return d,result,download

    def test_exact_stage_download_and_code_can_complete_readonly_task(self):
        d,result,_=self.copilot_evidence();self.store.finish_dispatch(d['dispatch_id'],result)
        self.assertEqual(self.complete(result['evidence_refs'])['status'],'completed')

    def test_download_tampered_after_role_receipt_cannot_complete(self):
        d,result,download=self.copilot_evidence();self.store.finish_dispatch(d['dispatch_id'],result)
        Path(download['path']).write_text('{}',encoding='utf-8')
        with self.assertRaises((ActionRejected,ValueError)):self.complete(result['evidence_refs'])

    def test_model_code_summary_cannot_replace_actual_download_identity(self):
        d,result,_=self.copilot_evidence()
        result['verified_copilots'][0].update(copilot_id='999',mystery_code='prts://999')
        self.store.finish_dispatch(d['dispatch_id'],result)
        with self.assertRaises(ActionRejected):self.complete(result['evidence_refs'])

    def test_search_only_cannot_finish_specific_stage_json_request(self):
        self.copilot_evidence()
        with self.assertRaises(ActionRejected):self.complete(self.evidence())

if __name__=='__main__':unittest.main()

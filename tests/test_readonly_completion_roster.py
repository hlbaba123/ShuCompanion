"""A real roster-backed completion must close without nested SQLite BEGIN."""
import hashlib
import json
from pathlib import Path
from tests.test_readonly_completion import ReadonlyCompletionTests
from shu.maa_roster_tools import RosterTools


class RosterCompletionTransactionTests(ReadonlyCompletionTests):
    def test_matched_full_document_commits_once_and_rechecks_current_box(self):
        dispatch,result,download=self.copilot_evidence(match_roster=True)
        record=json.loads(Path(result['candidate_refs'][0]['path']).read_text())
        body=json.loads(Path(download['path']).read_text())
        body['opers']=[{'name':'测试干员','skill':1,'requirements':{'elite':2}}]
        Path(download['path']).write_text(json.dumps(body),encoding='utf-8')
        digest=hashlib.sha256(Path(download['path']).read_bytes()).hexdigest()
        download['sha256']=digest;record['sha256']=digest
        candidate=Path(result['candidate_refs'][0]['path'])
        candidate.write_text(json.dumps(record),encoding='utf-8')
        result['candidate_refs'][0]['sha256']=hashlib.sha256(candidate.read_bytes()).hexdigest()
        proof=Path(result['evidence_refs'][1]['path'])
        proof_value=json.loads(proof.read_text());proof_value['result']['sha256']=digest
        proof.write_text(json.dumps(proof_value),encoding='utf-8')
        result['evidence_refs'][1]['sha256']=hashlib.sha256(proof.read_bytes()).hexdigest()
        result['verified_copilots'][0]['sha256']=digest
        tools=RosterTools(self.store,self.root)
        tools.facts.publish('local-default',[{'name':'测试干员','owned':True,'rarity':6,'elite':2}],False,
            {'kind':'user-roster-spreadsheet','observed_at':1000,'evidence_ref':'fixture:test.xlsx'},explicit_ownership=True)
        result['assessments']={'123':tools.assess('local-default',record)}
        self.assertEqual(result['assessments']['123']['classification'],'satisfied')
        self.store.finish_dispatch(dispatch['dispatch_id'],result)
        self.assertEqual(self.complete(result['evidence_refs'])['status'],'completed')


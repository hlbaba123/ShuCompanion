"""Explain structural research gaps without accepting a model's completion prose."""
import copy
import json
import unittest

from shu.leader_policy import research_goal_diagnostics


class ResearchGoalDiagnosticsTests(unittest.TestCase):
    def fixture(self, codes=('GH-1', 'GH-2'), *, activity=False, matching=True):
        goal=('查找当前活动GH-1~GH-2全部作业JSON' if activity else '查找GH-1和GH-2全部作业JSON')
        if matching:goal+='，筛选适合我本地阵容的方案'
        else:goal+='，本轮不筛选不匹配'
        contract={'goal':goal,'user_request':goal,'match_roster':matching,'task_spec':{'kind':'lookup'}}
        receipt={'status':'researched','evidence_refs':[{'path':'download-proof.json','sha256':'a'*64}],
            'candidate_refs':[],'verified_copilots':[],'candidate_pools':[],'assessments':{}}
        for n,code in enumerate(codes,101):
            receipt['candidate_refs'].append({'path':code+'-candidate.json','sha256':'b'*64})
            receipt['verified_copilots'].append({'code':code,'copilot_id':str(n),'mystery_code':'prts://'+str(n)})
            receipt['candidate_pools'].append({'pool_ref':{'path':code+'-pool.json','sha256':'c'*64},
                'coverage':{'complete':True},'stage_resolution':{'levels':[{'code':code,'stage_id':'stage_'+code}]}})
            receipt['assessments'][str(n)]={'classification':'satisfied'}
        if activity:
            ref={'path':'catalog.json','sha256':'d'*64}
            receipt['activity_catalog']={'ok':True,'event_name':'虚构活动','has_more':False,
                'stages':[{'code':code} for code in codes],'stage_catalog_ref':ref}
            receipt['activity_scope']={'event_name':'虚构活动','required_stage_codes':list(codes),
                'catalog_stage_codes':list(codes),'catalog_ref':ref,'coverage_verified':True,'excluded_stages':[]}
        return contract,receipt

    def test_downloads_for_every_stage_do_not_hide_missing_activity_scope(self):
        contract,receipt=self.fixture(activity=True)
        receipt.pop('activity_scope')
        result=research_goal_diagnostics(contract,receipt)
        self.assertFalse(result['satisfied'])
        self.assertTrue(result['missing_activity_scope'])
        self.assertFalse(result['missing_activity_catalog'])
        self.assertEqual(result['missing_stage_codes'],[])
        self.assertEqual(result['incomplete_pool_codes'],[])
        self.assertEqual(result['unmatched_stage_codes'],[])
        self.assertIn('missing_activity_scope',[x['code'] for x in result['issues']])

    def test_missing_download_pool_and_matching_are_separate_per_stage_gaps(self):
        contract,receipt=self.fixture(codes=('GH-1',))
        receipt['assessments']['101']['classification']='unknown'
        result=research_goal_diagnostics(contract,receipt)
        self.assertEqual(result['required_stage_codes'],['GH-1','GH-2'])
        self.assertEqual(result['missing_stage_codes'],['GH-2'])
        self.assertEqual(result['incomplete_pool_codes'],['GH-2'])
        self.assertEqual(result['unmatched_stage_codes'],['GH-1','GH-2'])

    def test_complete_activity_receipt_has_no_structural_gaps(self):
        contract,receipt=self.fixture(activity=True)
        result=research_goal_diagnostics(contract,receipt)
        self.assertTrue(result['satisfied'])
        self.assertEqual(result['issues'],[])
        self.assertFalse(result['missing_evidence'])
        self.assertFalse(result['missing_candidates'])
        self.assertEqual(result['authority'],'structural_only')

    def test_model_prose_cannot_supply_missing_evidence_candidates_or_activity_catalog(self):
        contract,_=self.fixture(activity=True)
        result=research_goal_diagnostics(contract,{'status':'researched','summary':'全部下载且全部匹配成功'})
        self.assertFalse(result['satisfied'])
        self.assertTrue(result['missing_evidence'])
        self.assertTrue(result['missing_candidates'])
        self.assertTrue(result['missing_activity_catalog'])
        self.assertTrue(result['missing_activity_scope'])
        self.assertEqual(result['missing_stage_codes'],['GH-1','GH-2'])

    def test_non_copilot_public_document_query_does_not_require_downloads_or_pools(self):
        result=research_goal_diagnostics({'goal':'查询MAA作业格式文档','task_spec':{'kind':'lookup'}},
            {'status':'researched','evidence_refs':[{'path':'source.json','sha256':'a'*64}]})
        self.assertTrue(result['satisfied'])
        self.assertFalse(result['missing_candidates'])
        self.assertFalse(result['missing_activity_scope'])
        self.assertEqual(result['issues'],[])

    def test_canonical_code_identity_is_required_for_a_stage_to_have_a_download(self):
        contract,receipt=self.fixture()
        receipt['verified_copilots'][1]['mystery_code']='prts://999'
        result=research_goal_diagnostics(contract,receipt)
        self.assertFalse(result['satisfied'])
        self.assertEqual(result['missing_stage_codes'],['GH-2'])

    def test_explicitly_deferred_matching_does_not_create_roster_gaps(self):
        contract,receipt=self.fixture(matching=False)
        receipt.pop('assessments')
        result=research_goal_diagnostics(contract,receipt)
        self.assertTrue(result['satisfied'])
        self.assertFalse(result['roster_required'])
        self.assertEqual(result['unmatched_stage_codes'],[])

    def test_battle_objective_cannot_be_satisfied_by_readonly_delivery(self):
        contract,receipt=self.fixture()
        contract['goal']='GH-1刷6次';contract['user_request']=contract['goal']
        contract['task_spec']={'kind':'repeat_stage','stage_code':'GH-1','repeat_count':6}
        result=research_goal_diagnostics(contract,receipt)
        self.assertFalse(result['satisfied'])
        self.assertIn('not_readonly_goal',[x['code'] for x in result['issues']])

    def test_unknown_malformed_lists_are_diagnosed_without_mutating_the_receipt(self):
        contract,receipt=self.fixture(activity=True)
        receipt.update(verified_copilots='model prose',candidate_pools={'complete':True},assessments=[])
        original=copy.deepcopy(receipt)
        result=research_goal_diagnostics(contract,receipt)
        self.assertFalse(result['satisfied'])
        self.assertEqual(receipt,original)
        json.dumps(result,ensure_ascii=False,allow_nan=False)

    def test_invalid_or_oversized_scope_is_bounded_and_cannot_be_marked_satisfied(self):
        contract={'goal':'查询新活动作业JSON','task_spec':{'kind':'lookup'}}
        receipt={'status':'researched','activity_scope':{'required_stage_codes':[f'GH-{n}' for n in range(1,801)]}}
        result=research_goal_diagnostics(contract,receipt)
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['required_stage_codes']),200)
        self.assertLessEqual(len(result['missing_stage_codes']),200)
        self.assertLessEqual(len(result['issues']),16)
        self.assertFalse(result['satisfied'])
        self.assertLess(len(json.dumps(result)),20000)

    def test_invalid_explicit_range_returns_an_actionable_diagnostic(self):
        contract={'goal':'查GH-1到GH-900作业JSON','task_spec':{'kind':'lookup'}}
        result=research_goal_diagnostics(contract,{})
        self.assertFalse(result['satisfied'])
        self.assertIn('invalid_stage_scope',[x['code'] for x in result['issues']])


if __name__=='__main__':unittest.main()

"""A web hit is not a completed stage-specific copilot lookup."""
import unittest
from shu.leader_policy import research_satisfies_goal, normalize_task_spec, TaskSpecError


class CopilotLookupCoverage(unittest.TestCase):
    def contract(self, text='只读查询TL-10作业，获取完整JSON和神秘代码，不导入MAA'):
        return {'goal':text,'user_request':text,'task_spec':{'kind':'lookup','stage_code':'TL-10'}}

    def receipt(self):
        return {'status':'researched','evidence_refs':[{'path':'search','sha256':'a'*64}]}

    def test_search_or_homepage_without_download_cannot_finish(self):
        self.assertFalse(research_satisfies_goal(self.contract(),self.receipt()))

    def test_wrong_stage_or_no_candidate_reference_cannot_finish(self):
        result=self.receipt()
        result['verified_copilots']=[{'code':'TL-6','copilot_id':'123','mystery_code':'prts://123'}]
        result['candidate_refs']=[{'path':'candidate','sha256':'b'*64}]
        self.assertFalse(research_satisfies_goal(self.contract(),result))
        result['verified_copilots'][0]['code']='TL-10';result['candidate_refs']=[]
        self.assertFalse(research_satisfies_goal(self.contract(),result))

    def test_verified_download_does_not_require_roster_when_matching_is_deferred(self):
        result=self.receipt();result['candidate_refs']=[{'path':'candidate','sha256':'b'*64}]
        result['verified_copilots']=[{'code':'TL-10','copilot_id':'123','mystery_code':'prts://123'}]
        text='只读查询TL-10作业，获取JSON和神秘代码，本轮不筛选干员、不匹配阵容、不导入MAA'
        self.assertTrue(research_satisfies_goal(self.contract(text),result))

    def test_initial_extra_questions_do_not_permanently_block_proved_readonly_deliverable(self):
        result=self.receipt();result['candidate_refs']=[{'path':'candidate','sha256':'b'*64}]
        result['verified_copilots']=[{'code':'TL-10','copilot_id':'123','mystery_code':'prts://123'}]
        contract=self.contract()
        contract['task_spec']['unresolved']=['其他候选未全部下载','活动是否现在开放']
        self.assertTrue(research_satisfies_goal(contract,result))

    def test_activity_announcement_lookup_still_needs_no_copilot(self):
        contract={'goal':'查询当前开放活动公告','task_spec':{'kind':'lookup'}}
        self.assertTrue(research_satisfies_goal(contract,self.receipt()))

    def test_activity_job_search_also_needs_full_candidate_without_explicit_stage(self):
        for text in ('查询新活动作业','查询时间啊向前的MAA作业','查找当前活动的作业'):
            self.assertFalse(research_satisfies_goal({'goal':text,'task_spec':{'kind':'lookup'}},self.receipt()))

    def test_copilot_format_documentation_is_not_a_request_to_download_a_job(self):
        text='查询MAA作业格式文档'
        self.assertTrue(research_satisfies_goal({'goal':text,'task_spec':{'kind':'lookup'}},self.receipt()))

    def test_all_summaries_cannot_complete_with_only_one_download_and_partial_pool(self):
        result=self.receipt();result['candidate_refs']=[{'path':'candidate','sha256':'b'*64}]
        result['verified_copilots']=[{'code':'TL-10','copilot_id':'123','mystery_code':'prts://123'}]
        contract=self.contract('只读收集TL-10全部作业摘要，验证一份完整JSON，不导入')
        result['candidate_pools']=[{'pool_ref':{'path':'pool'},'coverage':{'complete':False},
            'stage_resolution':{'levels':[{'code':'TL-10','stage_id':'offline-tl-10'}]}}]
        self.assertFalse(research_satisfies_goal(contract,result))
        result['candidate_pools'][0]['coverage']['complete']=True
        self.assertTrue(research_satisfies_goal(contract,result))

    def test_saving_for_later_matching_does_not_require_matching_now(self):
        result=self.receipt();result['candidate_refs']=[{'path':'candidate','sha256':'b'*64}]
        result['verified_copilots']=[{'code':'TL-10','copilot_id':'123','mystery_code':'prts://123'}]
        result['candidate_pools']=[{'pool_ref':{'path':'pool'},'coverage':{'complete':True},
            'stage_resolution':{'levels':[{'code':'TL-10','stage_id':'offline-tl-10'}]}}]
        contract=self.contract('收集TL-10全部作业摘要，保存供后续筛选；本轮不匹配、不导入MAA')
        self.assertTrue(research_satisfies_goal(contract,result))

    def test_maa_auto_battle_page_label_is_not_a_battle_start_request(self):
        for goal in ('查询TL-10作业并导入MAA的“自动战斗”页面','查找TL-10通关作业并导入'):
            spec=normalize_task_spec({'kind':'import','stage_code':'TL-10'},
                user_request='查询TL-10作业并导入',goal=goal,intent='execute')
            self.assertEqual(spec.kind,'import')
        with self.assertRaises(TaskSpecError):
            normalize_task_spec({'kind':'import'},user_request='TL-10导入后开始自动战斗',
                goal='导入MAA自动战斗页面，然后开始自动战斗',intent='execute')


if __name__=='__main__':unittest.main()

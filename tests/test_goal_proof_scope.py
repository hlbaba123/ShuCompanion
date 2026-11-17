import unittest
from shu.leader_policy import research_stage_codes,research_satisfies_goal


class ProofScopeTests(unittest.TestCase):
    def test_explicit_activity_range_cannot_complete_after_only_one_stage(self):
        contract={'goal':'查TL-1到TL-10作业','user_request':'查TL-1到TL-10作业',
            'task_spec':{'kind':'lookup','stage_code':'TL-1'}}
        self.assertEqual(research_stage_codes(contract),[f'TL-{i}' for i in range(1,11)])
        receipt={'status':'researched','evidence_refs':[{}],'candidate_refs':[{}],
            'verified_copilots':[{'code':'TL-1','copilot_id':'123','mystery_code':'prts://123'}]}
        self.assertFalse(research_satisfies_goal(contract,receipt))

    def test_activity_job_search_needs_registered_scope_not_model_summary(self):
        contract={'goal':'找新活动的作业','user_request':'找新活动的作业','task_spec':{'kind':'lookup'}}
        receipt={'status':'researched','summary':'TL1-10都拿到了','evidence_refs':[{}],'candidate_refs':[{}],
            'verified_copilots':[{'code':'TEST-6','copilot_id':'123','mystery_code':'prts://123'}]}
        self.assertFalse(research_satisfies_goal(contract,receipt))


if __name__=='__main__':unittest.main()

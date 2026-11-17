import copy
import unittest
from shu.maa_agents import _model_context, _tool_view


class ResearchEfficiencyTests(unittest.TestCase):
    def test_handoff_drops_duplicated_full_reply_and_contract(self):
        context={'contract':{'goal':'TEST-6','reply':'JSON '+('x'*20000)},'role_results':{
            'research':{'summary':'候选已保存','verified_copilots':[{'copilot_id':'123'}],
                        'evidence_refs':[{'path':'file','sha256':'a'*64}]}},
            'handoff_history':[{'summary':'候选已保存','result':{'summary':'x'*10000}}]}
        original=copy.deepcopy(context); view=_model_context(context)
        self.assertNotIn('reply',view['contract'])
        self.assertEqual(view['role_results']['research']['verified_copilots'][0]['copilot_id'],'123')
        self.assertLess(len(str(view)),4000);self.assertEqual(context,original)

    def test_tool_view_omits_full_actions_but_keeps_proof_and_requirements(self):
        result={'ok':True,'tool':'download_copilot','path':'full.json','sha256':'a'*64,
            'action_count':30,'verified_copilot':{'code':'TEST-6','mystery_code':'prts://123'},
            'copilot':{'actions':[{'type':'Click'}]*30},'preview':{'opers':[{'name':'A','skill':2}]}}
        view=_tool_view('research_download',result)
        self.assertEqual(view['path'],'full.json');self.assertNotIn('copilot',view)
        self.assertEqual(result['copilot']['actions'][0]['type'],'Click')
        self.assertEqual(view['preview']['opers'][0]['skill'],2)

    def test_model_completion_prose_does_not_make_precheck_satisfied(self):
        context={'contract':{'goal':'查TEST-6匹配作业代码','task_spec':{'kind':'lookup','stage_code':'TEST-6'}},
            'role_results':{'research':{'status':'researched','summary':'全都找到了并且匹配','candidate_refs':[]}}}
        view=_model_context(context)
        self.assertFalse(view['material_delivery_precheck']['satisfied'])
        self.assertFalse(view['material_delivery_precheck']['game_input_allowed'])


if __name__=='__main__':unittest.main()

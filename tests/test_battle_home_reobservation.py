"""Retry only the explicitly rejected home-banner model proposal, without input."""
import copy
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from shu.maa_battle import BattleRunner
from shu.maa_battle_vision import BattleVisionError


class HomeModelReobservationTests(unittest.TestCase):
    def runner(self, proposals):
        runner=BattleRunner.__new__(BattleRunner)
        runner.target={'stage_code':'UT-EX-1','difficulty':'raid','activity_name':'真实活动'}
        runner.navigation_phase='to_target';runner.cancel=threading.Event()
        runner.cancel.wait=Mock(return_value=False)
        runner.guard=Mock();runner.progress=Mock();runner.input=Mock()
        self.published=[];self.histories=[]
        def publish(kind,payload):
            self.published.append((kind,copy.deepcopy(payload)))
            return {'path':kind+str(len(self.published))+'.json','sha256':'a'*64}
        runner.publish=publish
        frames=[{'screenshot_id':str(n),'image_sha256':str(n)*64} for n in (1,2,3,4)]
        runner.snapshot=Mock(side_effect=frames)
        scripted=iter(proposals)
        def navigate(frame,target,*,history,**kwargs):
            self.histories.append(copy.deepcopy(history))
            proposal=next(scripted)
            if isinstance(proposal,Exception):raise proposal
            return {'page':'home','action':'click','resource_sensitive':False,
                    'screenshot_id':frame['screenshot_id'],'image_sha256':frame['image_sha256'],**proposal}
        def checked(frame,proposal,target):
            if proposal.get('target_label')=='bad':
                return {**proposal,'action':'ask','target_box':None,'model_action_rejected':True,
                        'rejection_code':'home_banner_target_unverified'}
            return proposal
        runner.vision=SimpleNamespace(navigate=Mock(side_effect=navigate))
        runner.local_navigation=SimpleNamespace(observe=Mock(return_value=None),check_model_home_action=checked)
        return runner

    def test_rejected_home_banner_then_valid_proposal_uses_fresh_frame_and_audits_both(self):
        runner=self.runner([{'target_label':'bad','target_box':{'x':.7}},
                            {'target_label':'当前活动banner','target_box':{'x':.1}}])
        frame,view=runner._navigation_observation([])
        self.assertEqual((frame['screenshot_id'],view['target_label']),('2','当前活动banner'))
        self.assertEqual([call.args[0]['screenshot_id'] for call in runner.vision.navigate.call_args_list],['1','2'])
        proposals=[payload for kind,payload in self.published if kind=='model-navigation-proposal']
        guarded=[payload for kind,payload in self.published if kind=='model-navigation-guard']
        self.assertEqual(len(proposals),2);self.assertEqual(len(guarded),2)
        self.assertEqual(proposals[0]['proposal']['target_box'],{'x':.7})
        self.assertIsNone(guarded[0]['guarded']['target_box'])
        correction=json_text(self.histories[1])
        self.assertIn('未派发',correction);self.assertIn('banner',correction)
        self.assertIn('终端',correction);self.assertIn('其他活动',correction)
        runner.input.assert_not_called()

    def test_three_rejected_proposals_return_final_ask_without_fourth_attempt(self):
        runner=self.runner([{'target_label':'bad'}]*3)
        frame,view=runner._navigation_observation([])
        self.assertEqual((frame['screenshot_id'],view['action']),('3','ask'))
        self.assertEqual(runner.vision.navigate.call_count,3)
        self.assertEqual(runner.snapshot.call_count,3)
        runner.input.assert_not_called()

    def test_real_ask_wait_resources_and_other_rejection_are_never_automatically_retried(self):
        for proposal in ({'action':'ask'},{'action':'wait'},{'action':'ask','resource_sensitive':True},
                         {'action':'ask','model_action_rejected':True,'rejection_code':'quick_menu_target_unverified'}):
            with self.subTest(proposal=proposal):
                runner=self.runner([proposal])
                _,view=runner._navigation_observation([])
                self.assertEqual(view['action'],proposal['action'])
                self.assertEqual(runner.vision.navigate.call_count,1)

    def test_format_and_home_rejection_share_one_three_attempt_budget(self):
        invalid=BattleVisionError('battle_vision_invalid_output','invalid format')
        runner=self.runner([invalid,{'target_label':'bad'},{'target_label':'bad'}])
        frame,view=runner._navigation_observation([])
        self.assertEqual((frame['screenshot_id'],view['action']),('3','ask'))
        self.assertEqual(runner.vision.navigate.call_count,3)


def json_text(value):
    import json
    return json.dumps(value,ensure_ascii=False)


if __name__=='__main__':unittest.main()

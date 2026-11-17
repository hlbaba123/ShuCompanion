import copy
import unittest

from shu import maa_battle


class ChapterHandoffTests(unittest.TestCase):
    def setUp(self):
        self.target = {'stage_code':'UT-EX-1', 'stage_id':'zone_ex01', 'difficulty':'raid'}
        self.members = [{'stage_id':'zone_ex01', 'code':'UT-EX-1', 'event':'测试活动', 'name':'测试关卡'}]
        self.refs = [{'path':'zone.json','sha256':'a'*64}]
        self.facts = {'ok':True,'groups':[
            {'group_id':'first','label':'第一分区','stage_ids':['zone_01'],'source_refs':self.refs},
            {'group_id':'second','label':'实际第二分区','stage_ids':['zone_ex01'],'source_refs':self.refs}],
            'navigation_regions':[{'navigation_region_id':'public-zone:second','stage_ids':['zone_ex01'],
                'source_refs':self.refs,'basis':'public_zone_membership','navigation_verified':False}]}

    def resolve(self, facts=None):
        return maa_battle.navigation_target(self.target,self.members,self.facts if facts is None else facts)

    def test_navigation_receives_real_chapter_not_inferred_ex_label(self):
        value=self.resolve()
        self.assertEqual(value['activity_name'],'测试活动')
        self.assertEqual(value['chapter_name'],'实际第二分区')
        self.assertEqual(value['chapter_id'],'second')
        self.assertEqual(value['difficulty'],'raid')
        self.assertEqual(value['chapter_source_refs'],self.refs)
        self.assertFalse(value['account_unlock_verified'])
        self.assertNotIn('chapter_name',self.target)

    def test_missing_or_ambiguous_chapter_stops_before_any_navigation(self):
        for groups in ([],[self.facts['groups'][0]],
                       [self.facts['groups'][1],{**self.facts['groups'][1],'group_id':'third'}],
                       [{**self.facts['groups'][1],'group_id':'catalog-unmapped'}],
                       [{**self.facts['groups'][1],'source_refs':[]}],
                       [{**self.facts['groups'][1],'label':''}]):
            with self.subTest(groups=groups),self.assertRaises(maa_battle.BattleError):
                self.resolve({**self.facts,'groups':groups})

    def test_foreign_stage_must_not_supply_a_chapter(self):
        self.members=[{**self.members[0],'stage_id':'other'}]
        with self.assertRaises(maa_battle.BattleError):self.resolve()

    def test_disagreeing_activity_members_are_not_silently_selected(self):
        self.members.append({**self.members[0],'event':'别的活动'})
        with self.assertRaises(maa_battle.BattleError):self.resolve()

    def test_same_code_with_different_chapter_members_is_rejected(self):
        self.members.append({**self.members[0],'stage_id':'zone_01'})
        with self.assertRaises(maa_battle.BattleError):self.resolve()

    def test_no_battle_authority_or_completion_is_created(self):
        value=self.resolve()
        self.assertNotIn('allow_start',value)
        self.assertNotIn('navigation_verified',value)
        self.assertNotIn('completed',value)


if __name__=='__main__':unittest.main()

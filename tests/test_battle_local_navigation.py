import unittest
from unittest.mock import MagicMock
from shu.maa_battle import BattleRunner


class LocalNavigationTests(unittest.TestCase):
    def setUp(self):
        from shu import battle_local_navigation as nav
        self.nav = nav
        self.target = dict(activity_name='甲活动', chapter_name='春之章', stage_code='AT-EX-1', difficulty='raid')
        self.frame = dict(screenshot_id='frame', image_sha256='a'*64,
                          window=dict(client_rect=dict(width=1000, height=600)))

    def observe(self, words, *, templates=None, **overrides):
        ocr = dict(width=1000, height=600, image_sha256='a'*64,
                   words=[dict(text=t,rect=dict(x=x,y=y,width=w,height=20)) for t,x,y,w in words])
        return self.nav.interpret(self.frame, self.target, {**ocr, **overrides}, templates or {})

    def test_archive_uses_only_visible_return_even_if_it_displays_target_event(self):
        back = dict(x=.02,y=.03,width=.09,height=.05)
        words=[('乐章收录',820,30,90), ('Story Line',350,160,120), ('甲活动',440,300,80)]
        self.assertEqual(self.observe(words, templates={'back':back})['target_box'], back)
        view = self.observe(words)
        self.assertEqual(view['action'], 'ask')

    def test_settings_requires_header_menu_markers_and_matching_return_template(self):
        back=dict(x=.02,y=.03,width=.09,height=.05)
        words=[('设置',200,30,60),('重置设置',820,30,90),
               ('游戏',100,160,60),('声音',100,240,60),('提醒',100,320,60)]
        view=self.observe(words,templates={'back':back})
        self.assertIsNotNone(view)
        self.assertEqual((view['page'],view['target_label'],view['target_box']),('settings','返回',back))
        self.assertIsNone(self.observe(words))
        self.assertIsNone(self.observe(words[1:],templates={'back':back}))
        self.assertIsNone(self.observe(words[:1]+words[2:],templates={'back':back}))
        self.assertIsNone(self.observe(words+[('是否退出',400,300,90)],templates={'back':back}))

    def test_return_home_phase_reports_home_without_clicking_event_or_chapter(self):
        self.target['navigation_phase']='return_home'
        home=[('终端',690,110,40),('干员',865,285,80),('编队',620,285,70),('甲活动',880,180,85)]
        view=self.observe(home)
        self.assertEqual((view['page'],view['action'],view['target_box']),('home','wait',None))
        self.assertEqual(view['screenshot_id'],self.frame['screenshot_id'])
        self.assertIsNone(self.observe([('本章回想',900,10,80),('作战结束时间',700,45,150),('春之章',300,230,90)]))
        self.assertIsNone(self.observe([('AT-EX-1',400,320,75),('AT-EX-2',300,290,80)]))
        self.target['navigation_phase']='to_target'
        self.assertEqual(self.observe(home)['target_label'],'甲活动')

    def test_sparse_quick_menu_row_with_three_home_labels_does_not_confirm_home(self):
        self.target['navigation_phase']='return_home'
        self.assertIsNone(self.observe([('终端',395,80,40),('编队',160,90,40),('干员',225,190,40)]))

    def test_only_matching_banner_can_be_clicked_by_home_local_route(self):
        home = [('终端',690,110,40),('干员',865,285,80),('编队',620,285,70)]
        self.assertIsNone(self.observe(home + [('旧活动',880,180,85)]))
        result = self.observe(home + [('甲活动',880,180,85)])
        self.assertEqual(result['target_label'],'甲活动')
        self.assertAlmostEqual(result['target_box']['x'],.88)

    def test_home_rejection_marks_only_unsubmitted_banner_proposals_for_retry(self):
        observer=self.nav.LocalBattleNavigation('.')
        observer.home_frame_id=self.frame['screenshot_id']
        proposal=dict(action='click',page='home',target_label='甲活动',
                      target_box=dict(x=.50,y=.12,width=.48,height=.28),
                      to_x=None,to_y=None,resource_sensitive=False)
        result=observer.check_model_home_action(self.frame,proposal,self.target)
        self.assertEqual(result['action'],'ask')
        self.assertTrue(result.get('model_action_rejected'))
        self.assertEqual(result.get('rejection_code'),'home_banner_target_unverified')
        self.assertEqual(proposal['action'],'click')
        self.assertEqual(observer.check_model_home_action(self.frame,
            {**proposal,'action':'ask','target_box':None},self.target)['action'],'ask')
        self.assertNotIn('model_action_rejected',observer.check_model_home_action(self.frame,
            {**proposal,'action':'ask','target_box':None},self.target))
        proper={**proposal,'target_box':dict(x=.86,y=.28,width=.11,height=.1)}
        self.assertEqual(observer.check_model_home_action(self.frame,proper,self.target),proper)
        wrong={**proper,'target_label':'旧活动'}
        self.assertEqual(observer.check_model_home_action(self.frame,wrong,self.target)['action'],'ask')
        mislabeled=observer.check_model_home_action(self.frame,
            {**proper,'page':'event_overview'},self.target)
        self.assertEqual((mislabeled['page'],mislabeled['action']),('home','ask'))
        self.assertEqual(mislabeled['rejection_code'],'home_banner_target_unverified')

    def quick_menu_words(self):
        return [('首页',65,190,40),('终端',395,80,40),('基建',515,80,40),
                ('档案',333,190,40),('采购中心',890,190,75)]

    def test_quick_menu_has_priority_over_background_and_home_identity_in_both_phases(self):
        background=[('编队',160,90,40),('干员',225,190,40),('甲活动',875,175,90),
                    ('本章回想',900,14,75),('作战结束时间',665,38,140),('春之章',565,435,90)]
        for phase in ('return_home','to_target'):
            with self.subTest(phase=phase):
                self.target['navigation_phase']=phase
                icon=dict(x=.06,y=.355,width=.05,height=.055)
                view=self.observe(self.quick_menu_words()+background,templates={'quick_home':icon})
                self.assertEqual((view['page'],view['target_label']),('quick_menu','首页'))
                self.assertEqual(view['target_box'],icon)

    def test_missing_ambiguous_or_displaced_quick_menu_home_is_not_clicked(self):
        anchors=self.quick_menu_words()[1:]+[('AT-EX-1',480,385,75),('AT-EX-2',700,340,80)]
        for entries in ([],[('首页',65,190,40),('首页',95,190,40)],[('首页',800,450,40)]):
            with self.subTest(entries=entries):
                view=self.observe(anchors+entries)
                self.assertEqual((view['page'],view['action'],view['target_box']),('quick_menu','ask',None))
        self.assertIsNone(self.observe(self.quick_menu_words()+[('确认购买',400,350,80)]))
        self.assertIsNone(self.observe([('首页',65,190,40),('终端',395,80,40),('基建',515,80,40)]))
        self.assertEqual(self.observe(self.quick_menu_words())['action'],'ask')
        wrong_icon=dict(x=.14,y=.36,width=.05,height=.055)
        self.assertEqual(self.observe(self.quick_menu_words(),templates={'quick_home':wrong_icon})['action'],'ask')

    def test_model_menu_input_requires_current_local_menu_and_exact_home_control(self):
        observer=self.nav.LocalBattleNavigation('.')
        icon=dict(x=.06,y=.355,width=.05,height=.055)
        view=dict(action='click',page='quick_menu',target_label='首页',target_box=icon,to_x=None,to_y=None)
        def check(frame, proposal):
            return observer.check_model_home_action(frame,proposal,self.target)['action']
        self.assertEqual(check(self.frame,view),'ask')
        frame={**self.frame,'image_path':'saved.png'}
        snapshot=dict(width=1000,height=600,image_sha256=self.frame['image_sha256'],
            words=[dict(text=t,rect=dict(x=x,y=y,width=w,height=20)) for t,x,y,w in self.quick_menu_words()])
        observer.ocr=MagicMock()
        observer.ocr.recognize.return_value=snapshot
        observer._buttons=MagicMock(return_value={'quick_home':icon})
        observer.observe(frame,{**self.target,'chapter_source_refs':['evidence']},None)
        self.assertEqual(check(frame,view),'click')
        self.assertEqual(check({**frame,'image_sha256':'b'*64},view),'ask')
        self.assertEqual(check(frame,{**view,'target_box':dict(x=.14,y=.28,width=.02,height=.03)}),'ask')
        behind_menu={**view,'page':'event_overview','target_label':'春之章'}
        self.assertEqual(check(frame,behind_menu),'ask')

    def test_exact_public_chapter_and_overview_anchors_needed(self):
        overview = [('作战结束时间 2026/11/22',700,45,240),('本章回想',900,10,80)]
        chapter = [('春之章',300,230,90)]
        self.assertIsNone(self.observe(chapter))
        result = self.observe(overview + chapter)
        self.assertEqual((result['page'],result['target_label']), ('event_overview','春之章'))

    def test_map_uses_whole_stage_token_without_confusing_it_with_detail(self):
        words = [('AT-EX-10',100,340,100),('AT-EX-2',300,290,80)]
        self.assertIsNone(self.observe(words))
        found = self.observe(words + [('AT-EX-1',400,320,75)])
        self.assertEqual(found['target_box']['x'],.4)
        self.assertEqual(found['image_sha256'],self.frame['image_sha256'])
        self.assertEqual(found['selected_stage_code'],'')
        self.assertIsNone(self.observe(words+[('AT-EX-1',400,320,75),('开始行动',830,545,110)]))

    def test_stale_ocr_size_or_hash_is_not_used(self):
        words = [('AT-EX-1',400,320,75),('AT-EX-2',300,290,80)]
        self.assertIsNone(self.observe(words, image_sha256='b'*64))
        self.assertIsNone(self.observe(words, height=601))

    def test_runner_returns_current_local_result_before_requesting_model(self):
        runner = BattleRunner.__new__(BattleRunner)
        runner.cancel = MagicMock()
        runner.target = self.target
        runner.guard = MagicMock()
        runner.snapshot = MagicMock(return_value=self.frame)
        runner.publish = MagicMock(return_value={'path':'test-evidence','sha256':'a'*64})
        runner.local_navigation = MagicMock()
        runner.local_navigation.observe.return_value = {'page':'stage_map','evidence':'visible AT-EX-1'}
        runner.vision = MagicMock()
        self.assertEqual(runner._navigation_observation([])[1]['page'],'stage_map')
        runner.vision.navigate.assert_not_called()
        runner.local_navigation.observe.return_value = None
        runner._navigation_observation([])
        runner.vision.navigate.assert_called_once()

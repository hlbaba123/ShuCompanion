"""Read-only navigation evidence uses synthetic OCR, never a game window."""
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest


def observation(*lines):
    words = []
    for row, line in enumerate(lines):
        if isinstance(line, str):
            line = [line]
        x = 20
        for text in line:
            width = max(16, len(text) * 18)
            words.append({'text': text, 'rect': {'x': x, 'y': 20 + row * 40,
                          'width': width, 'height': 24}})
            x += width + 3
    return {'width': 1280, 'height': 720, 'words': words,
            'engine': 'windows.media.ocr:zh-Hans-CN', 'elapsed_ms': 8}


TARGET = {'activity_name': '活动甲', 'stage_code': 'TL-2', 'difficulty': '突袭'}


class PageClassificationTests(unittest.TestCase):
    def classify(self, data, target=None):
        from shu.game_navigation import classify_page
        return classify_page(data, target)

    def test_distinct_page_signatures_require_independent_markers(self):
        cases = [
            (['START'], None, 'start'),
            (['开始唤醒'], None, 'wake_up'),
            (['终端', '干员', '基建'], None, 'home'),
            (['活动甲', '第一章'], TARGET, 'event_overview'),
            (['TL-1', 'TL-2', 'TL-3'], TARGET, 'stage_map'),
            (['TL-2', '开始行动'], TARGET, 'stage_detail'),
            (['编队', '开始行动'], TARGET, 'squad'),
            (['行动结束', '作战奖励'], TARGET, 'settlement'),
            (['任务完成', '结算'], None, 'settlement'),
            (['验证码'], None, 'login_required'),
            (['输入密码'], None, 'login_required'),
            (['公告', '关闭'], None, 'dialog'),
            (['每日签到', '知道了'], None, 'dialog'),
        ]
        for text, target, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.classify(observation(*text), target)['page_type'], expected)

    def test_uncertain_and_conflicting_pages_stay_unknown(self):
        for lines in (['准备好了'], ['终端'], ['行动结束'], ['开始行动'],
                      ['活动甲'], ['START', '开始唤醒'],
                      ['终端', '干员', '基建', '验证码']):
            with self.subTest(lines=lines):
                self.assertEqual(self.classify(observation(*lines), TARGET)['page_type'], 'unknown')

    def test_stage_target_uses_complete_token_and_preserves_unverified_difficulty(self):
        for wrong in ('TL-20', 'XTL-2', 'TL-2-1'):
            with self.subTest(wrong=wrong):
                result = self.classify(observation(wrong, '开始行动'), TARGET)
                self.assertEqual(result['page_type'], 'unknown')
                self.assertFalse(result['target_stage_visible'])
        result = self.classify(observation('TL-2', '开始行动', '突袭'), TARGET)
        self.assertEqual(result['page_type'], 'stage_detail')
        self.assertEqual(result['difficulty_status'], 'unverified')
        self.assertFalse(result['actionable'])
        self.assertTrue(result['read_only'])

    def test_segmented_chinese_text_and_code_keep_measured_union_rectangle(self):
        data = observation(['开', '始', '行', '动'], ['TL', '-', '2'])
        result = self.classify(data, TARGET)
        self.assertEqual(result['page_type'], 'stage_detail')
        action = next(item for item in result['candidates'] if item['kind'] == 'start_action')
        self.assertEqual(action['text'], '开始行动')
        self.assertEqual(action['rect'], {'x': 20, 'y': 20, 'width': 81, 'height': 24})
        stage = next(item for item in result['candidates'] if item['kind'] == 'stage')
        self.assertEqual(stage['text'], 'TL-2')
        self.assertTrue(stage['is_target'])

    def test_contiguous_split_twenty_cannot_be_truncated_to_target_two(self):
        data = observation(['TL', '-', '2', '0'], '开始行动')
        result = self.classify(data, TARGET)
        self.assertEqual(result['page_type'], 'unknown')
        self.assertFalse(result['target_stage_visible'])

    def test_start_is_an_independent_token_not_part_of_restart(self):
        result = self.classify(observation(['RE', 'START']))
        self.assertEqual(result['page_type'], 'unknown')
        self.assertEqual(result['candidates'], [])

    def test_dialog_never_invents_an_unlabelled_close_button(self):
        result = self.classify(observation('公告'))
        self.assertEqual(result['page_type'], 'unknown')
        self.assertFalse(any(item['kind'] == 'close' for item in result['candidates']))
        data = observation('公告', '关闭')
        result = self.classify(data)
        close = next(item for item in result['candidates'] if item['kind'] == 'close')
        self.assertEqual(close['text'], '关闭')
        self.assertEqual(close['rect'], data['words'][1]['rect'])

    def test_home_shortcut_is_not_a_sign_in_modal(self):
        result = self.classify(observation('签到', '终端', '采购中心'))
        self.assertEqual(result['page_type'], 'home')
        self.assertFalse(any(item['kind'] == 'close' for item in result['candidates']))

    def test_partial_detail_page_is_unknown_rather_than_a_map(self):
        result = self.classify(observation('TL-2', 'TL-3', '敌方情报', '始行'), TARGET)
        self.assertEqual(result['page_type'], 'unknown')
        self.assertTrue(result['target_stage_visible'])
        self.assertFalse(any(item['kind'] == 'start_action' for item in result['candidates']))

    def test_observation_never_exports_unrelated_account_text_or_mutates_input(self):
        data = observation('终端', '干员', '基建', 'ACCOUNT private-identifier')
        original = copy.deepcopy(data)
        result = self.classify(data)
        self.assertNotIn('private-identifier', str(result))
        self.assertEqual(data, original)
        self.assertEqual(result['coordinate_space'], 'image')

    def test_invalid_or_unbounded_ocr_is_unknown_without_exporting_raw_input(self):
        for data in ({}, {'width': True, 'height': 720, 'words': []},
                     observation('x' * 513),
                     {'width': 1280, 'height': 720, 'words': [
                         {'text': 'private', 'rect': {'x': float('nan'), 'y': 0, 'width': 2, 'height': 2}}]}):
            with self.subTest(data_type=type(data).__name__):
                result = self.classify(data, TARGET)
                self.assertEqual(result['page_type'], 'unknown')
                self.assertEqual(result['candidates'], [])
                self.assertEqual(result['evidence'], [])

    def test_target_must_be_explicit_for_event_or_detail_identity(self):
        self.assertEqual(self.classify(observation('活动甲', '第一章'))['page_type'], 'unknown')
        self.assertEqual(self.classify(observation('TL-2', '开始行动'))['page_type'], 'unknown')


class GameNavigationTests(unittest.TestCase):
    def navigation_data(self, original, region):
        return {'width': 1280, 'height': 720, 'regions': [
            {'observation': original, 'origin': {'x': 0, 'y': 0}, 'scale': {'x': 1., 'y': 1.}},
            {'observation': region, 'origin': {'x': 576, 'y': 324}, 'scale': {'x': 2., 'y': 2.}},
        ]}

    def test_navigation_prefers_batch_and_fuses_only_actual_text_in_source_coordinates(self):
        from shu.game_navigation import GameNavigation
        original = observation('终端', 'private account unrelated')
        region = observation('采购中心')
        region.update(width=1408, height=792)
        data = self.navigation_data(original, region)
        class Ocr:
            def recognize_navigation(self, path, *, cancel_event=None):
                return data
            def recognize(self, path, *, cancel_event=None):
                raise AssertionError('navigation must use its bounded batch path')
        result = GameNavigation(ocr=Ocr()).observe('已有图.png', TARGET)
        self.assertEqual(result['page_type'], 'home')
        shop = next(item for item in result['candidates'] if item['kind'] == 'home_shop')
        self.assertEqual(shop['rect'], {'x': 586., 'y': 334., 'width': 36., 'height': 12.})
        self.assertNotIn('private account', str(result))

    def test_navigation_fusion_still_needs_exact_stage_and_actual_action(self):
        from shu.game_navigation import GameNavigation
        original, region = observation('TL-2', '敌方情报'), observation('开始行动')
        region.update(width=1408, height=792)
        data = self.navigation_data(original, region)
        class Ocr:
            def recognize_navigation(self, path, *, cancel_event=None):
                return data
        navigator = GameNavigation(ocr=Ocr())
        result = navigator.observe('existing.png', TARGET)
        self.assertEqual(result['page_type'], 'stage_detail')
        self.assertFalse(result['actionable'])
        self.assertEqual(result['difficulty_status'], 'unverified')
        self.assertEqual(navigator.observe('existing.png', {'stage_code': 'TL-20'})['page_type'], 'unknown')
        data['regions'][1]['observation'] = {**region, 'words': []}
        self.assertEqual(navigator.observe('existing.png', TARGET)['page_type'], 'unknown')

    def test_navigation_fusion_deduplicates_same_visible_label(self):
        from shu.game_navigation import GameNavigation
        original = observation('START')
        region = observation('START')
        data = self.navigation_data(original, region)
        data['regions'][1].update(origin={'x': 0, 'y': 0}, scale={'x': 1., 'y': 1.})
        class Ocr:
            def recognize_navigation(self, path, *, cancel_event=None):
                return data
        result = GameNavigation(ocr=Ocr()).observe('existing.png')
        self.assertEqual(result['page_type'], 'start')
        self.assertEqual(len(result['candidates']), 1)

    def test_observe_uses_supplied_image_and_cancel_event_without_other_access(self):
        from shu.game_navigation import GameNavigation
        class Ocr:
            def __init__(self):
                self.calls = []
            def recognize(self, path, *, cancel_event=None):
                self.calls.append((path, cancel_event))
                return observation('TL-2', '开始行动')
        ocr, cancel = Ocr(), threading.Event()
        result = GameNavigation(ocr=ocr).observe('已有截图.png', TARGET, cancel)
        self.assertEqual(ocr.calls, [('已有截图.png', cancel)])
        self.assertEqual(result['page_type'], 'stage_detail')
        self.assertFalse(result['actionable'])

    def test_cancellation_during_ocr_does_not_return_stale_observation(self):
        from shu.game_navigation import GameNavigation
        from shu.local_ocr import LocalOcrError
        cancel = threading.Event()
        class Ocr:
            def recognize(self, path, *, cancel_event=None):
                cancel_event.set()
                return observation('START')
        with self.assertRaises(LocalOcrError) as caught:
            GameNavigation(ocr=Ocr()).observe('existing.png', TARGET, cancel)
        self.assertEqual(caught.exception.code, 'ocr_cancelled')


@unittest.skipUnless(os.name == 'nt' and os.environ.get('SHU_TEST_GAME_NAVIGATION_REFERENCES'),
                     'opt-in user images: SHU_TEST_GAME_NAVIGATION_REFERENCES')
class WindowsNavigationSmokeTests(unittest.TestCase):
    def test_real_five_pages_exact_target_and_removed_evidence_controls(self):
        from shu.game_navigation import GameNavigation, classify_page
        from shu.local_ocr import LocalOcr
        root = Path(os.environ['SHU_TEST_GAME_NAVIGATION_REFERENCES'])
        references = {
            'start': 'codex-clipboard-b2d174ff-ef4f-454c-a624-1884546511b5.png',
            'wake_up': 'codex-clipboard-23317cd7-4abb-40d4-ad2c-c33e530ed3b5.png',
            'home': 'codex-clipboard-a89ae469-34b0-439b-9a22-42af8fae34a6.png',
            'stage_map': 'codex-clipboard-ac0b5aea-0e00-4ea7-ba5b-aec7a49af058.png',
            'stage_detail': 'codex-clipboard-417e9bfd-d09a-46e5-99b8-4d72785decad.png',
        }
        observed = {}
        with tempfile.TemporaryDirectory(prefix='shu-nav-test-') as temporary:
            navigation = GameNavigation(ocr=LocalOcr(temp_root=temporary))
            for expected, name in references.items():
                with self.subTest(expected=expected):
                    path = root / name
                    before = hashlib.sha256(path.read_bytes()).digest()
                    result = navigation.observe(path, TARGET)
                    self.assertEqual(result['page_type'], expected)
                    self.assertFalse(result['actionable'])
                    self.assertEqual(result['difficulty_status'], 'unverified')
                    self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)
                    self.assertEqual(list(Path(temporary).iterdir()), [])
                    observed[expected] = result
            wrong = navigation.observe(root / references['stage_detail'], {'stage_code': 'TL-20'})
            self.assertEqual(wrong['page_type'], 'unknown')
            self.assertFalse(wrong['target_stage_visible'])
            for name, removed in [('stage_detail', {'start_action'}),
                                  ('home', {'home_shop', 'home_recruit', 'home_depot'})]:
                value = observed[name]
                words = [{'text': item['text'], 'rect': item['rect']} for item in value['candidates']
                         if item['kind'] not in removed]
                self.assertEqual(classify_page({'width': value['width'], 'height': value['height'],
                                               'words': words}, TARGET)['page_type'], 'unknown')


if __name__ == '__main__':
    unittest.main()

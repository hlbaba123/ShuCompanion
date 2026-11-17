"""Image/OCR import receipts reject echo-only and stale-preview evidence."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from shu.maa_visual_readback import MaaVisualReadback, parse_visual, verify_import


EXPECTED = {'code': 'prts://123456', 'title': '活动甲 - AA-2 - 测试关卡',
            'stage_code': 'AA-2', 'operator_names': ['干员甲', '干员乙']}


def word(text, x, y, width=None, height=18):
    return {'text': text, 'rect': {'x': x, 'y': y,
                                  'width': width or max(16, len(text) * 10), 'height': height}}


def fixture(*, code='prts://123456', title=EXPECTED['title'], running=False, scale=1):
    words = [word('自动战斗', 470, 36), word('主线/故事集/SideStory', 110, 98),
             word(code, 110, 145, 220), word('停止' if running else '开始', 270, 205),
             word('自动编队', 200, 280), word('忽略干员属性要求', 200, 330),
             word(title, 650, 450, 390), word('干员甲 [精2 60] 技能 1', 650, 550, 310),
             word('干员乙 [精2 60] 技能 2', 650, 590, 310)]
    rectangles = [{'x': 100, 'y': 135, 'width': 420, 'height': 38},
                  {'x': 522, 'y': 135, 'width': 35, 'height': 38},
                  {'x': 560, 'y': 135, 'width': 35, 'height': 38}]
    for entry in words:
        for key in entry['rect']:
            entry['rect'][key] *= scale
    for box in rectangles:
        for key in box:
            box[key] *= scale
    return {'width': round(1280 * scale), 'height': round(800 * scale), 'words': words}, rectangles


def frame(id, *, code=EXPECTED['code'], title=EXPECTED['title'], preview='new'):
    ocr, rectangles = fixture(code=code, title=title)
    data = parse_visual(ocr, rectangles=rectangles, expected=EXPECTED,
                        image_sha256=hashlib.sha256(id.encode()).hexdigest(),
                        preview_sha256=preview)
    data['screenshot_id'] = id
    return data


class ParseTests(unittest.TestCase):
    def long_group_preview(self):
        ocr,rectangles=fixture()
        ocr['height']=1080
        for row in ocr['words'][6:]:
            if row['text']==EXPECTED['title']:row['rect']['y']=550
            elif row['text'].startswith('干员甲'):row['rect']['y']=840
            elif row['text'].startswith('干员乙'):row['rect']['y']=875
        ocr['words'] += [word('较长的作业说明与参考视频',650,625,350),
                         word('备选组: 干员丙 / 干员丁',650,910,350),
                         word('共 3 名干员',650,945,140),
                         word('作业怎么样？给个评价吧！',650,995,300)]
        return ocr,rectangles

    def test_long_description_and_group_count_are_kept_in_measured_preview(self):
        ocr,rectangles=self.long_group_preview()
        result=parse_visual(ocr,rectangles=rectangles,expected=EXPECTED)
        self.assertEqual(result['operator_count'],3)
        self.assertIn('共 3 名干员',result['preview_lines'])
        region=result['regions']['preview']
        self.assertGreaterEqual(region['y']+region['height'],963)
        self.assertNotIn('作业怎么样？给个评价吧！',result['preview_lines'])

    def test_preview_count_cannot_come_from_help_left_column_or_below_feedback(self):
        ocr,rectangles=self.long_group_preview()
        ocr['words']=[row for row in ocr['words'] if row['text']!='共 3 名干员']
        ocr['words'] += [word('共 3 名干员',650,350,140),
                         word('共 3 名干员',110,945,140),
                         word('共 3 名干员',650,1040,140)]
        result=parse_visual(ocr,rectangles=rectangles,expected=EXPECTED)
        self.assertIsNone(result['operator_count'])

    def test_competing_preview_counts_remain_unknown(self):
        ocr,rectangles=self.long_group_preview()
        ocr['words'].append(word('共 4 名干员',650,973,140))
        result=parse_visual(ocr,rectangles=rectangles,expected=EXPECTED)
        self.assertIsNone(result['operator_count'])

    def test_right_help_text_cannot_replace_left_stage_tab(self):
        ocr, rectangles = fixture()
        ocr['words'][1:2] = [word('主线', 110, 98), word('故事集/SideSto', 135, 98)]
        ocr['words'].append(word('主线故事集SideStory:', 665, 198, 190))
        result = parse_visual(ocr, rectangles=rectangles, expected=EXPECTED)
        self.assertEqual(result['page'], 'copilot')
        self.assertEqual(result['anchors']['stage_tab']['rect']['x'], 110)

    def test_id_readback_handles_prefix_ocr_without_guessing_digits(self):
        ocr, rectangles = fixture(code='p 戊 s ： / / 123456')
        result = parse_visual(ocr, rectangles=rectangles, expected=EXPECTED)
        self.assertEqual(result['codes'], [])
        self.assertEqual(result['code_id_readback'], '123456')

    def test_dynamic_geometry_is_measured_and_start_is_never_a_target(self):
        for scale in (.65, 1, 1.5):
            with self.subTest(scale=scale):
                ocr, rectangles = fixture(scale=scale)
                result = parse_visual(ocr, rectangles=rectangles, expected=EXPECTED)
                self.assertTrue(result['ok'])
                self.assertEqual(result['page'], 'copilot')
                self.assertTrue(result['idle'])
                self.assertEqual(result['targets']['code_input']['rect'], rectangles[0])
                self.assertEqual(result['targets']['paste_button']['rect'], rectangles[2])
                self.assertNotIn('start', result['targets'])
                self.assertEqual(result['forbidden_regions'][0]['kind'], 'start')
                self.assertFalse(result['input_permission'])

    def test_ocr_split_words_remain_a_measured_anchor(self):
        ocr, rectangles = fixture()
        ocr['words'][:1] = [word('自动', 470, 36), word('战斗', 491, 36)]
        result = parse_visual(ocr, rectangles=rectangles, expected=EXPECTED)
        self.assertEqual(result['anchors']['copilot_tab']['rect']['x'], 470)
        self.assertEqual(result['anchors']['copilot_tab']['rect']['width'], 41)

    def test_no_measured_field_means_no_guessed_input_or_icon_target(self):
        ocr, _ = fixture()
        result = parse_visual(ocr, expected=EXPECTED)
        self.assertNotIn('code_input', result['targets'])
        self.assertNotIn('paste_button', result['targets'])
        self.assertEqual(result['page'], 'unknown')

    def test_ambiguous_fields_fail_closed_but_nested_outline_is_collapsed(self):
        ocr, rectangles = fixture()
        duplicate = {'x': 101, 'y': 136, 'width': 418, 'height': 36}
        result = parse_visual(ocr, rectangles=rectangles + [duplicate])
        self.assertIn('code_input', result['targets'])
        ambiguous = {'x': 100, 'y': 165, 'width': 420, 'height': 38}
        result = parse_visual(ocr, rectangles=rectangles + [ambiguous])
        self.assertNotIn('code_input', result['targets'])

    def test_single_icon_is_not_assumed_to_be_paste(self):
        ocr, rectangles = fixture()
        result = parse_visual(ocr, rectangles=rectangles[:2], expected=EXPECTED)
        self.assertNotIn('paste_button', result['targets'])

    def test_checkbox_geometry_requires_both_square_and_exact_option_label(self):
        ocr, rectangles = fixture()
        square = {'x': 175, 'y': 280, 'width': 18, 'height': 18}
        result = parse_visual(ocr, rectangles=rectangles + [square], expected=EXPECTED)
        self.assertEqual(result['targets']['auto_formation']['rect'], square)
        self.assertEqual(result['options']['auto_formation']['state'], 'unknown')
        self.assertNotIn('ignore_requirements', result['targets'])

    def test_running_and_invalid_word_boxes_do_not_pass(self):
        ocr, rectangles = fixture(running=True)
        result = parse_visual(ocr, rectangles=rectangles, expected=EXPECTED)
        self.assertFalse(result['idle'])
        self.assertTrue(result['running'])
        for bad in (float('nan'), True, -1):
            invalid = copy.deepcopy(ocr)
            invalid['words'][0]['rect']['x'] = bad
            result = parse_visual(invalid, rectangles=rectangles, expected=EXPECTED)
            self.assertFalse(result['ok'])
            self.assertEqual(result['targets'], {})

    def test_preview_and_operators_cannot_come_from_input_or_left_panel(self):
        ocr, rectangles = fixture(title='其他关卡')
        ocr['words'] += [word(EXPECTED['title'], 110, 380, 400)]
        result = parse_visual(ocr, rectangles=rectangles, expected=EXPECTED)
        self.assertFalse(result['expected_title_visible'])
        self.assertFalse(result['expected_stage_visible'])

    def test_stage_and_code_must_match_complete_tokens(self):
        ocr, rectangles = fixture(code='prts://1234567', title='活动甲 - AA-20 - 测试关卡')
        result = parse_visual(ocr, rectangles=rectangles, expected=EXPECTED)
        self.assertEqual(result['codes'], ['prts://1234567'])
        self.assertFalse(result['expected_stage_visible'])
        self.assertFalse(result['expected_title_visible'])

    def test_wrapped_title_and_chinese_spacing_are_supported(self):
        ocr, rectangles = fixture()
        ocr['words'][6:7] = [word('活动甲 - AA-2 -', 650, 450), word('测试关卡', 650, 478)]
        result = parse_visual(ocr, rectangles=rectangles, expected=EXPECTED)
        self.assertTrue(result['expected_title_visible'])
        self.assertTrue(result['expected_stage_visible'])
        self.assertEqual(result['missing_operators'], [])

    def test_errors_are_preserved_and_ocr_never_mutated(self):
        ocr, rectangles = fixture()
        ocr['words'].append(word('下载失败', 650, 700))
        saved = copy.deepcopy(ocr)
        result = parse_visual(ocr, rectangles=rectangles, expected=EXPECTED)
        self.assertEqual(result['errors'], ['下载失败'])
        self.assertEqual(ocr, saved)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.before = frame('before', code='prts://111111', title='其他关卡', preview='old')
        self.entry = frame('entry', preview='old')
        self.after = [frame('after-1'), frame('after-2')]

    def verify(self, **kwargs):
        values = {'expected': EXPECTED, 'code_entry': self.entry}
        values.update(kwargs)
        return verify_import(self.before, self.after, **values)

    def test_success_requires_two_fresh_stable_preview_captures_and_code_echo(self):
        result = self.verify()
        self.assertTrue(result['verified'], result)
        self.assertEqual(result['status'], 'import_verified')
        self.assertFalse(result['evidence']['start_clicked'])

    def test_resolved_maa_cache_path_requires_prior_real_code_echo(self):
        self.after = [frame('after-1', code='D:\\MAA\\cache\\123456.json'),
                      frame('after-2', code='D:\\MAA\\cache\\123456.json')]
        self.assertTrue(self.verify()['verified'])
        self.assertFalse(self.verify(code_entry=None)['verified'])
        self.assertIn('real_code_echo_required', self.verify(code_entry=None)['reasons'])

    def test_old_loaded_same_code_is_reported_separately_from_import(self):
        self.before = frame('before', preview='new')
        result = self.verify()
        self.assertFalse(result['verified'])
        self.assertEqual(result['status'], 'already_loaded')

    def test_input_echo_or_old_title_without_new_preview_never_proves_import(self):
        self.before = frame('before', code='prts://111111', preview='new')
        result = self.verify()
        self.assertFalse(result['verified'])
        self.assertIn('no_new_preview_evidence', result['reasons'])

    def test_single_capture_reused_capture_wrong_title_and_operator_fail(self):
        for mutation, reason in [
            (lambda: setattr(self, 'after', self.after[:1]), 'two_fresh_readbacks_required'),
            (lambda: self.after[1].update(screenshot_id='after-1'), 'readbacks_not_independent'),
            (lambda: self.after[1].update(expected_title_visible=False), 'expected_preview_not_visible'),
            (lambda: self.after[1].update(missing_operators=['干员乙']), 'expected_operators_not_visible'),
            (lambda: self.after[1].update(errors=['下载失败']), 'maa_error_visible'),
            (lambda: self.after[1].update(preview_signature='unstable'), 'preview_not_stable'),
            (lambda: self.after[1].update(running=True), 'copilot_idle_not_verified'),
        ]:
            with self.subTest(reason=reason):
                self.setUp()
                mutation()
                result = self.verify()
                self.assertFalse(result['verified'])
                self.assertIn(reason, result['reasons'])

    def test_no_code_title_stage_or_input_receipt_fail(self):
        for field in ('code', 'title', 'stage_code'):
            with self.subTest(field=field):
                expected = dict(EXPECTED)
                expected[field] = ''
                self.assertFalse(self.verify(expected=expected)['verified'])
        self.assertFalse(self.verify(input_issued=False)['verified'])


class ImageMeasurementTests(unittest.TestCase):
    @staticmethod
    def column_ocr(source, path):
        import cv2
        import numpy as np
        height, width = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), 1).shape[:2]
        split = 640
        left = 0 if path.stem == 'left' else split
        words = []
        for word in source['words']:
            rect = word['rect']
            if left <= rect['x'] and rect['x'] + rect['width'] <= left + split:
                words.append({'text': word['text'], 'rect': {
                    'x': (rect['x'] - left) * width / split, 'y': rect['y'] * height / 800,
                    'width': rect['width'] * width / split, 'height': rect['height'] * height / 800}})
        return {'height': height, 'width': width if source['width'] == 1280 else width - 1, 'words': words}

    def test_image_borders_are_measured_before_ocr_targets_are_emitted(self):
        import cv2
        import numpy as np
        from shu.maa_visual_readback import detect_rectangles
        ocr, rectangles = fixture()
        image = np.full((800, 1280, 3), 255, dtype=np.uint8)
        for box in rectangles:
            cv2.rectangle(image, (box['x'], box['y']),
                          (box['x'] + box['width'] - 1, box['y'] + box['height'] - 1), (180, 180, 180), 1)
        actual = detect_rectangles(image)
        result = parse_visual(ocr, rectangles=actual, expected=EXPECTED)
        self.assertIn('code_input', result['targets'])
        self.assertIn('paste_button', result['targets'])

    def test_observe_uses_local_image_and_real_ocr_geometry(self):
        import cv2
        import numpy as np
        ocr, rectangles = fixture()
        class Ocr:
            def recognize(self, path, *, cancel_event=None):
                return ImageMeasurementTests.column_ocr(ocr, path)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '本地截图.png'
            image = np.full((800, 1280, 3), 255, dtype=np.uint8)
            for box in rectangles:
                cv2.rectangle(image, (box['x'], box['y']),
                              (box['x'] + box['width'] - 1, box['y'] + box['height'] - 1), (180, 180, 180), 1)
            cv2.imencode('.png', image)[1].tofile(str(path))
            result = MaaVisualReadback(ocr=Ocr()).observe(path, expected=EXPECTED)
            self.assertEqual(result['page'], 'copilot')
            self.assertTrue(result['preview_sha256'])
            self.assertEqual(result['image_path'], str(path.resolve()))
            ocr['width'] = 1200
            self.assertEqual(MaaVisualReadback(ocr=Ocr()).observe(path)['reason'], 'image_ocr_geometry_mismatch')

    def test_checkbox_state_comes_from_measured_pixels_not_ocr_label(self):
        import cv2
        import numpy as np
        ocr, rectangles = fixture()
        class Ocr:
            def recognize(self, path, *, cancel_event=None):
                return ImageMeasurementTests.column_ocr(ocr, path)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checks.png'
            for checked in (False, True):
                image = np.full((800, 1280, 3), 255, dtype=np.uint8)
                for box in rectangles:
                    cv2.rectangle(image, (box['x'], box['y']),
                                  (box['x'] + box['width'] - 1, box['y'] + box['height'] - 1), (180, 180, 180), 1)
                color, thickness = ((190, 180, 0), -1) if checked else ((180, 180, 180), 1)
                cv2.rectangle(image, (175, 280), (192, 297), color, thickness)
                cv2.imencode('.png', image)[1].tofile(str(path))
                result = MaaVisualReadback(ocr=Ocr()).observe(path, expected=EXPECTED)
                self.assertIs(result['options']['auto_formation']['state'], checked)

    def test_empty_hint_tolerates_only_a_single_tiny_achromatic_left_caret(self):
        import cv2
        import numpy as np
        ocr, rectangles = fixture(code='作业路径/神秘代码')
        class Ocr:
            def recognize(self, path, *, cancel_event=None):
                return ImageMeasurementTests.column_ocr(ocr, path)
        cases = [
            ('blank', [], True),
            ('two_pixel_caret', [(108, 145, 2, 18, (0, 0, 0))], True),
            ('one_pixel_caret', [(108, 145, 1, 18, (80, 80, 80))], True),
            ('wide_stroke', [(108, 145, 3, 18, (0, 0, 0))], False),
            ('short_mark', [(108, 145, 1, 5, (0, 0, 0))], False),
            ('tall_stroke', [(108, 140, 2, 27, (0, 0, 0))], False),
            ('interior_stroke', [(150, 145, 2, 18, (0, 0, 0))], False),
            ('colored_stroke', [(108, 145, 2, 18, (200, 90, 0))], False),
            ('caret_and_text', [(108, 145, 2, 18, (0, 0, 0)), (125, 146, 6, 15, (0, 0, 0))], False),
            ('two_carets', [(108, 145, 1, 18, (0, 0, 0)), (110, 145, 1, 18, (0, 0, 0))], False),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'caret.png'
            for name, marks, expected_empty in cases:
                with self.subTest(name=name):
                    image = np.full((800, 1280, 3), 255, dtype=np.uint8)
                    for box in rectangles:
                        cv2.rectangle(image, (box['x'], box['y']),
                                      (box['x'] + box['width'] - 1, box['y'] + box['height'] - 1),
                                      (180, 180, 180), 1)
                    for x, y, width, height, color in marks:
                        image[y:y + height, x:x + width] = color
                    cv2.imencode('.png', image)[1].tofile(str(path))
                    result = MaaVisualReadback(ocr=Ocr()).observe(path)
                    self.assertEqual(result['field_empty'], expected_empty)
            # Even a pixel-identical vertical glyph cannot be called empty
            # when OCR sees actual text instead of the empty-field hint.
            image[145:163, 108:111] = 255
            image[145:163, 108:110] = 0
            cv2.imencode('.png', image)[1].tofile(str(path))
            ocr['words'][2]['text'] = 'l'
            self.assertFalse(MaaVisualReadback(ocr=Ocr()).observe(path)['field_empty'])


class ApplicationReceiptVerificationTests(unittest.TestCase):
    def test_same_native_preview_with_model_vertical_and_text_drift_uses_verified_application_channel(self):
        self.after[1]['regions']['preview'].update(y=430,height=230)
        self.after[1]['preview_signature']='different-model-transcript'
        module=SimpleNamespace(verify_receipt=lambda payload,expected:{'verified':True})
        with patch.dict('sys.modules',{'shu.maa_load_receipt':module}):
            result=verify_import(self.before,self.after,expected=self.expected,load_receipt={})
        self.assertTrue(result['verified'],result)

    def test_native_preview_change_and_invalid_roi_cannot_use_application_channel(self):
        import cv2
        import numpy as np
        module=SimpleNamespace(verify_receipt=lambda payload,expected:{'verified':True})
        last=self.after[-1]
        original=Path(last['image_path']).read_bytes()
        image=cv2.imdecode(np.frombuffer(original,np.uint8),cv2.IMREAD_COLOR)
        image[500,700]=100
        raw=cv2.imencode('.png',image)[1].tobytes();Path(last['image_path']).write_bytes(raw)
        last['image_sha256']=hashlib.sha256(raw).hexdigest()
        with patch.dict('sys.modules',{'shu.maa_load_receipt':module}):
            self.assertFalse(verify_import(self.before,self.after,expected=self.expected,load_receipt={})['verified'])
        Path(last['image_path']).write_bytes(original)
        last['image_sha256']=hashlib.sha256(original).hexdigest()
        for box in ({'x':10,'y':440,'width':440,'height':200},
                    {'x':640,'y':float('nan'),'width':440,'height':200},
                    {'x':640,'y':440,'width':440,'height':10000}):
            with self.subTest(box=box), patch.dict('sys.modules',{'shu.maa_load_receipt':module}):
                last['regions']['preview']=box
                self.assertFalse(verify_import(self.before,self.after,expected=self.expected,load_receipt={})['verified'])

    def test_application_native_channel_requires_same_window_and_dpi(self):
        module=SimpleNamespace(verify_receipt=lambda payload,expected:{'verified':True})
        for key,value in (('dpi',144),('hwnd',999),('creation_time',999)):
            altered=copy.deepcopy(self.after);altered[-1]['window'][key]=value
            with self.subTest(key=key),patch.dict('sys.modules',{'shu.maa_load_receipt':module}):
                self.assertFalse(verify_import(self.before,altered,expected=self.expected,load_receipt={})['verified'])

    def test_no_application_receipt_keeps_text_stability_requirement(self):
        self.after[1]['preview_signature']='different-model-transcript'
        for item in self.after:item['expected_title_visible']=True
        result=verify_import(self.before,self.after,expected=self.expected)
        self.assertIn('preview_not_stable',result['reasons'])

    def test_fractional_ocr_roi_uses_outward_physical_pixel_bounds(self):
        module = SimpleNamespace(verify_receipt=lambda payload, expected: {'verified': True})
        for item in self.after:
            item['regions']['preview'] = {'x': 640.25, 'y': 440.125, 'width': 439.5, 'height': 199.75}
        with patch.dict('sys.modules', {'shu.maa_load_receipt': module}):
            self.assertTrue(verify_import(self.before, self.after, expected=self.expected,
                                         load_receipt={})['verified'])

    def setUp(self):
        import cv2
        import numpy as np
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.expected = {**EXPECTED, 'source_document': {'stage_name': 'actual-stage', 'doc': {'title': EXPECTED['title']}}}
        self.before = frame('before', code='作业路径/神秘代码', title='请导入作业')
        self.before.update(field_empty=True, operator_count=None)
        self.after = [frame('one', title='AA-2 模型保留的错误转录'), frame('two', title='AA-2 模型保留的错误转录')]
        identity = {'hwnd': 1, 'pid': 2, 'creation_time': 3, 'executable': 'MAA.exe',
                    'client_rect': {'left': 0, 'top': 0, 'width': 1280, 'height': 800}, 'dpi': 120}
        for index, value in enumerate([self.before, *self.after]):
            image = np.full((800, 1280, 3), 255, dtype=np.uint8)
            if index:
                image[450:610, 650:1040] = 80
            path = self.root / (value['screenshot_id'] + '.png')
            path.write_bytes(cv2.imencode('.png', image)[1].tobytes())
            value.update(image_path=str(path), image_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                         window=copy.deepcopy(identity), single_job=True)
            value['regions']['preview'] = {'x': 640, 'y': 440, 'width': 440, 'height': 200}
            if index:
                value['operator_count'] = 2
                value['preview_signature'] = 'same-actual-region'

    def test_verified_application_receipt_can_confirm_exact_title_without_changing_visual_text(self):
        module = SimpleNamespace(verify_receipt=lambda payload, expected: {'verified': True})
        with patch.dict('sys.modules', {'shu.maa_load_receipt': module}):
            result = verify_import(self.before, self.after, expected=self.expected, load_receipt={'raw': 'receipt'})
        self.assertTrue(result['verified'], result)
        self.assertEqual(result['evidence']['title_verified_by'], 'maa_log_cache')
        self.assertFalse(self.after[-1]['expected_title_visible'])

    def test_unverified_receipt_boolean_and_unchanged_old_pixels_cannot_confirm_load(self):
        module = SimpleNamespace(verify_receipt=lambda payload, expected: {'verified': False})
        with patch.dict('sys.modules', {'shu.maa_load_receipt': module}):
            result = verify_import(self.before, self.after, expected=self.expected, load_receipt={'verified': True})
        self.assertFalse(result['verified'])

    def test_application_channel_rejects_truthy_visual_flags_and_noninteger_count(self):
        module = SimpleNamespace(verify_receipt=lambda payload, expected: {'verified': True})
        for key, value in (('ok', 1), ('idle', 1), ('running', 0), ('expected_stage_visible', 1)):
            with self.subTest(key=key), patch.dict('sys.modules', {'shu.maa_load_receipt': module}):
                altered = copy.deepcopy(self.after)
                altered[-1][key] = value
                self.assertFalse(verify_import(self.before, altered, expected=self.expected, load_receipt={})['verified'])
        expected = {**self.expected, 'operator_names': ['干员甲']}
        altered = copy.deepcopy(self.after)
        for item in altered:
            item['operator_count'] = True
        with patch.dict('sys.modules', {'shu.maa_load_receipt': module}):
            self.assertFalse(verify_import(self.before, altered, expected=expected, load_receipt={})['verified'])

    def test_application_title_must_equal_the_verified_source_title(self):
        module = SimpleNamespace(verify_receipt=lambda payload, expected: {'verified': True})
        expected = copy.deepcopy(self.expected)
        expected['source_document']['doc']['title'] = '另一份来源标题'
        with patch.dict('sys.modules', {'shu.maa_load_receipt': module}):
            self.assertFalse(verify_import(self.before, self.after, expected=expected, load_receipt={})['verified'])
        self.before['image_path'] = self.after[0]['image_path']
        self.before['image_sha256'] = self.after[0]['image_sha256']
        module.verify_receipt = lambda payload, expected: {'verified': True}
        with patch.dict('sys.modules', {'shu.maa_load_receipt': module}):
            result = verify_import(self.before, self.after, expected=self.expected, load_receipt={})
        self.assertFalse(result['verified'])


if __name__ == '__main__':
    unittest.main()

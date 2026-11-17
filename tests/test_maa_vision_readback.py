"""Screenshot-only model observation keeps deterministic import evidence local."""
import base64
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from shu.maa_vision_readback import (
    MAX_VISION_WORDS, MaaVisionReadback, VisionOcr, VisionOcrError, preview_region_from_count,
)
from shu.maa_visual_readback import MaaVisualReadback, verify_import
from shu.providers import ProviderCancelled, ProviderConfig, ProviderError


def word(text, x=2, y=3, width=25, height=16):
    return {'text': text, 'rect': {'x': x, 'y': y, 'width': width, 'height': height}}


class Client:
    def __init__(self, result, *, finish_reason='stop', after=None):
        self.result, self.after = result, after
        self.last_finish_reason = finish_reason
        self.last_metrics = {'request_number': 1, 'latency_seconds': .125,
                             'request_bytes': 2500, 'response_bytes': 450,
                             'usage': {'prompt_tokens': 120, 'completion_tokens': 50,
                                       'total_tokens': 170}, 'private': 'do not export'}
        self.config = ProviderConfig(model='deepseek-flash', max_tokens=8192)
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((copy.deepcopy(messages), kwargs))
        if self.after:
            self.after()
        if isinstance(self.result, Exception):
            raise self.result
        value = self.result
        if isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False)
        return {'role': 'assistant', 'content': value}


class VisionOcrTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='黍 视觉 ')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "截图 空格 ';&$().png"
        self.image = np.full((80, 140, 3), (181, 202, 211), dtype=np.uint8)
        cv2.imencode('.png', self.image)[1].tofile(str(self.path))
        self.result = {'width': 140, 'height': 80, 'words': [word('自动战斗')]}

    def assert_error(self, code, operation):
        with self.assertRaises(VisionOcrError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def recognize(self, result=None, **kwargs):
        return VisionOcr(client=Client(self.result if result is None else result), **kwargs).recognize(self.path)

    def test_one_request_attaches_original_png_at_native_resolution_without_tools(self):
        client = Client(self.result)
        cancel = threading.Event()
        observed = VisionOcr(client=client).recognize(self.path, cancel_event=cancel)
        self.assertEqual(observed['words'], self.result['words'])
        self.assertEqual((observed['width'], observed['height']), (140, 80))
        self.assertEqual(len(client.calls), 1)
        messages, options = client.calls[0]
        self.assertEqual([message['role'] for message in messages], ['system', 'user'])
        blocks = messages[1]['content']
        pictures = [block for block in blocks if block['type'] == 'image_url']
        self.assertEqual(len(pictures), 1)
        url = pictures[0]['image_url']['url']
        self.assertTrue(url.startswith('data:image/png;base64,'))
        self.assertEqual(pictures[0]['image_url']['detail'], 'original')
        self.assertEqual(base64.b64decode(url.split(',', 1)[1]), self.path.read_bytes())
        self.assertIs(options['cancel_event'], cancel)
        self.assertEqual(options['tool_choice'], 'none')
        self.assertNotIn('tools', options)
        instruction = messages[0]['content']
        self.assertIn('不可信', instruction)
        self.assertIn('无执行权限', instruction)
        self.assertIn('像素', instruction)
        self.assertIn('140', blocks[0]['text'])
        self.assertIn('80', blocks[0]['text'])

    def test_config_constructs_independent_client_without_mutating_it(self):
        config = ProviderConfig(model='deepseek-flash', max_tokens=8192)
        client = Client(self.result)
        with patch('shu.maa_vision_readback.DeepSeekClient', return_value=client) as factory:
            observer = VisionOcr(config=config)
            observer.recognize(self.path)
        factory.assert_called_once_with(config)
        self.assertEqual(client.config.max_tokens, 8192)
        self.assertIs(observer.client, client)

    def test_response_must_be_one_complete_json_object_without_repair(self):
        encoded = json.dumps(self.result, ensure_ascii=False)
        bad = [encoded[:-1], '```json\n' + encoded + '\n```', '观察如下：' + encoded,
               encoded + '\n{}', '[]', '{}',
               encoded.replace('"width": 140', '"width": 1, "width": 140'),
               encoded.replace('"x": 2', '"x": NaN')]
        for value in bad:
            with self.subTest(value=value[:80]):
                self.assert_error('vision_invalid_output', lambda: self.recognize(value))

    def test_dimensions_coordinates_text_and_list_are_validated_as_a_whole(self):
        bad = []
        for changes in ({'width': 139}, {'height': 81}, {'width': True},
                        {'words': None}, {'words': [word('a')] * (MAX_VISION_WORDS + 1)},
                        {'input_permission': True}, {'words': [word('')]},
                        {'words': [word(' ')]}, {'words': [word('a' * 513)]},
                        {'words': [word('\ud800')]}):
            value = copy.deepcopy(self.result)
            value.update(changes)
            bad.append(value)
        for key, value in [('x', -1), ('x', True), ('x', float('inf')),
                           ('x', float('nan')), ('x', 10 ** 500), ('width', 0),
                           ('width', 141), ('height', -1), ('y', 79)]:
            result = copy.deepcopy(self.result)
            result['words'].append(word('valid prefix'))
            result['words'][-1]['rect'][key] = value
            bad.append(result)
        for result in bad:
            with self.subTest(result=str(result)[:100]):
                self.assert_error('vision_invalid_output', lambda: self.recognize(result))

    def test_model_output_cannot_supply_approval_targets_or_checkbox_states(self):
        for key, value in [('verified', True), ('targets', {'start': {'x': 1, 'y': 1}}),
                           ('options', {'auto_formation': {'state': True}})]:
            result = copy.deepcopy(self.result)
            result[key] = value
            self.assert_error('vision_invalid_output', lambda: self.recognize(result))

    def test_empty_words_and_valid_fractional_pixel_boxes_are_preserved(self):
        self.assertEqual(self.recognize({'width': 140, 'height': 80, 'words': []})['words'], [])
        result = copy.deepcopy(self.result)
        result['words'][0]['rect']['x'] = 2.5
        self.assertEqual(self.recognize(result)['words'], result['words'])

    def test_truncated_response_is_rejected_even_if_it_contains_parseable_json(self):
        client = Client(self.result, finish_reason='length')
        self.assert_error('vision_invalid_output', lambda: VisionOcr(client=client).recognize(self.path))

    def test_invalid_response_evidence_is_safe_typed_metadata_without_model_text(self):
        for client, stage in [(Client('sk-DO-NOT-EXPORT {'), 'vision_json_invalid'),
                              (Client(self.result, finish_reason='length'), 'vision_response_envelope_invalid'),
                              (Client(dict(self.result, width=139)), 'vision_geometry_invalid'),
                              (Client([]), 'vision_content_type_invalid')]:
            with self.subTest(stage=stage):
                error = self.assert_error('vision_invalid_output', lambda: VisionOcr(client=client).recognize(self.path))
                self.assertEqual(error.evidence['stage'], stage)
                self.assertEqual((error.evidence['width'], error.evidence['height']), (140, 80))
                self.assertEqual(error.evidence['finish_reason_type'], 'str')
                self.assertIn('content_type', error.evidence)
                self.assertNotIn('DO-NOT-EXPORT', json.dumps(error.evidence))

    def test_tool_requests_and_oversized_responses_are_not_observations(self):
        class ToolClient(Client):
            def complete(self, messages, **kwargs):
                return {'content': json.dumps(self.result), 'tool_calls': [
                    {'type': 'function', 'function': {'name': 'start', 'arguments': '{}'}}]}
        self.assert_error('vision_invalid_output', lambda: VisionOcr(client=ToolClient(self.result)).recognize(self.path))
        self.assert_error('vision_invalid_output', lambda: self.recognize(' ' * (512 * 1024 + 1)))

    def test_cancellation_before_request_and_late_result_are_discarded(self):
        cancel = threading.Event()
        cancel.set()
        client = Client(self.result)
        self.assert_error('vision_cancelled', lambda: VisionOcr(client=client).recognize(self.path, cancel_event=cancel))
        self.assertEqual(client.calls, [])
        cancel.clear()
        client = Client(self.result, after=cancel.set)
        self.assert_error('vision_cancelled', lambda: VisionOcr(client=client).recognize(self.path, cancel_event=cancel))

    def test_provider_failures_use_safe_errors_and_do_not_export_raw_content(self):
        for error, code in [(ProviderError('secret key sk-DO-NOT-EXPORT'), 'vision_model_failed'),
                            (ProviderCancelled('secret text'), 'vision_cancelled')]:
            client = Client(error)
            caught = self.assert_error(code, lambda: VisionOcr(client=client).recognize(self.path))
            self.assertNotIn('secret', str(caught))
            self.assertNotIn('DO-NOT', str(caught))

    def test_invalid_or_oversized_image_never_reaches_client(self):
        client = Client(self.result)
        observer = VisionOcr(client=client)
        self.assert_error('vision_image_missing', lambda: observer.recognize(self.path.with_name('missing.png')))
        self.path.write_bytes(b'not an image')
        self.assert_error('vision_image_unreadable', lambda: observer.recognize(self.path))
        with self.path.open('wb') as stream:
            stream.truncate(32 * 1024 * 1024 + 1)
        self.assert_error('vision_image_too_large', lambda: observer.recognize(self.path))
        self.assertEqual(client.calls, [])

    def test_progress_and_metrics_report_observation_without_image_or_response(self):
        progress, metrics = [], []
        client = Client(self.result)
        VisionOcr(client=client, progress_callback=progress.append,
                  metrics_callback=metrics.append).recognize(self.path)
        self.assertEqual([event['phase'] for event in progress], ['started', 'completed'])
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0]['agent'], 'maa_vision')
        self.assertEqual(metrics[0]['status'], 'completed')
        self.assertEqual(metrics[0]['usage']['total_tokens'], 170)
        self.assertEqual(metrics[0]['input_images'], 1)
        self.assertEqual(metrics[0]['word_count'], 1)
        self.assertEqual(len(metrics[0]['image_sha256']), 64)
        exported = json.dumps(progress + metrics, ensure_ascii=False)
        for private in ('data:image', '自动战斗', 'do not export'):
            self.assertNotIn(private, exported)

    def test_bad_json_reports_failed_observation_and_callback_failure_is_nonfatal(self):
        progress, metrics = [], []
        self.assert_error('vision_invalid_output', lambda: VisionOcr(
            client=Client('partial JSON'), progress_callback=progress.append,
            metrics_callback=metrics.append).recognize(self.path))
        self.assertEqual([event['phase'] for event in progress], ['started', 'failed'])
        self.assertEqual(metrics[0]['status'], 'failed')
        def broken(event):
            raise RuntimeError('callback unavailable')
        self.assertEqual(self.recognize(progress_callback=broken, metrics_callback=broken)['words'],
                         self.result['words'])


EXPECTED = {'code': 'prts://123456', 'title': '活动甲 - AA-2 - 测试关卡',
            'stage_code': 'AA-2', 'operator_names': ['干员甲', '干员乙']}


def layout(*, code=EXPECTED['code'], title=EXPECTED['title']):
    words = [word('自动战斗', 470, 36, 80, 18), word('主线/故事集/SideStory', 110, 98, 220, 18),
             word(code, 110, 145, 220, 18), word('开始', 270, 205, 40, 18),
             word('自动编队', 200, 280, 80, 18),
             word(title, 650, 450, 390, 18), word('干员甲', 650, 550, 70, 18),
             word('干员乙', 650, 590, 70, 18), word('共2名干员', 650, 620, 120, 18)]
    return {'width': 1280, 'height': 800, 'words': words}


class MaaVisionReadbackTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'MAA截图.png'
        self.image = np.full((800, 1280, 3), 255, dtype=np.uint8)
        for x, y, width, height in [(100, 135, 420, 38), (522, 135, 35, 38),
                                   (560, 135, 35, 38), (175, 280, 18, 18)]:
            cv2.rectangle(self.image, (x, y), (x + width - 1, y + height - 1), (180, 180, 180), 1)
        cv2.imencode('.png', self.image)[1].tofile(str(self.path))

    def local_refiner(self, *, mutate=None, code=EXPECTED['code']):
        source = layout(code=code)
        source['words'] = [entry for entry in source['words'] if not entry['text'].startswith('共')]
        for text, y in [('忽略干员属性要求', 330), ('借助战', 380), ('多作业模式', 430)]:
            source['words'].append(word(text, 200, y, 180, 18))
            cv2.rectangle(self.image, (175, y), (192, y + 17), (180, 180, 180), 1)
        cv2.imencode('.png', self.image)[1].tofile(str(self.path))
        class ColumnOcr:
            def recognize(self, path, *, cancel_event=None):
                image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
                height, width = image.shape[:2]
                left = 0 if path.stem == 'left' else 640
                words = []
                for entry in source['words']:
                    box = entry['rect']
                    if left <= box['x'] and box['x'] + box['width'] <= left + 640:
                        words.append({'text': entry['text'], 'rect': {
                            'x': (box['x'] - left) * width / 640, 'y': box['y'] * height / 800,
                            'width': box['width'] * width / 640, 'height': box['height'] * height / 800}})
                return {'width': width, 'height': height, 'words': words}
        actual = MaaVisualReadback(ocr=ColumnOcr())
        class Refiner:
            def __init__(self):
                self.calls = 0
            def observe(self, path, **kwargs):
                self.calls += 1
                result = actual.observe(path, **kwargs)
                if mutate:
                    mutate(result)
                return result
        return Refiner()

    def test_one_full_frame_request_keeps_local_geometry_pixels_and_denied_start(self):
        client = Client(layout())
        observer = MaaVisionReadback(client=client)
        self.assertIsInstance(observer, MaaVisualReadback)
        self.assertTrue(observer.requires_frame_refresh)
        self.assertEqual(observer.max_observe_seconds, 45)
        self.assertEqual(observer.import_readback_timeout, 120)
        result = observer.observe(self.path, expected=EXPECTED)
        self.assertEqual(len(client.calls), 1)
        blocks = client.calls[0][0][1]['content']
        png = base64.b64decode(next(block for block in blocks if block['type'] == 'image_url')['image_url']['url'].split(',', 1)[1])
        sent = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        np.testing.assert_array_equal(sent, self.image)
        self.assertEqual(result['page'], 'copilot')
        self.assertIn('code_input', result['targets'])
        self.assertIn('paste_button', result['targets'])
        self.assertNotIn('start', result['targets'])
        self.assertEqual(result['forbidden_regions'][0]['kind'], 'start')
        self.assertIs(result['options']['auto_formation']['state'], False)
        self.assertEqual(result['options']['auto_formation']['measurement'], 'local_checkbox_pixels')
        self.assertTrue(result['preview_sha256'])
        self.assertTrue(result['expected_title_visible'])
        self.assertEqual(result['missing_operators'], [])
        self.assertFalse(result['input_permission'])

    def test_expected_job_is_only_used_locally_and_never_sent_to_visual_model(self):
        client = Client(layout())
        expected = dict(EXPECTED, title='SOURCE_ONLY_TITLE', code='prts://987654',
                        operator_names=['SOURCE_ONLY_OPERATOR'], stage_code='SOURCE_ONLY_STAGE')
        result = MaaVisionReadback(client=client).observe(self.path, expected=expected)
        request = json.dumps(client.calls[0][0], ensure_ascii=False)
        for secret in ('SOURCE_ONLY_TITLE', 'prts://987654', 'SOURCE_ONLY_OPERATOR', 'SOURCE_ONLY_STAGE'):
            self.assertNotIn(secret, request)
        self.assertFalse(result['expected_title_visible'])

    def test_injected_recognizer_wrong_frame_dimensions_fail_closed(self):
        class Ocr:
            def recognize(self, path, *, cancel_event=None):
                return dict(layout(), width=1200)
        result = MaaVisionReadback(ocr=Ocr()).observe(self.path, expected=EXPECTED)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'image_ocr_geometry_mismatch')

    def test_optional_local_refiner_recovers_missing_controls_from_same_image(self):
        local = self.local_refiner()
        client = Client(layout())
        result = MaaVisionReadback(client=client, local_refiner=local).observe(self.path, expected=EXPECTED)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(local.calls, 1)
        self.assertTrue(result['ok'])
        self.assertTrue(result['single_job'])
        self.assertIn('ignore_requirements', result['targets'])
        self.assertIn('use_support', result['targets'])
        self.assertEqual(result['options']['multiple_jobs']['measurement'], 'local_checkbox_pixels')
        self.assertEqual(result['recognition_engine'], 'deepseek_vision+local_geometry')
        self.assertTrue(result['expected_title_visible'])
        self.assertFalse(result['input_permission'])

    def test_local_refiner_rejects_identity_page_idle_and_same_box_state_conflicts(self):
        for mutation in [lambda result: result.update(image_sha256='different'),
                         lambda result: result.update(width=1279),
                         lambda result: result.update(page='unknown'),
                         lambda result: result.update(idle=False, running=True),
                         lambda result: result['options']['auto_formation'].update(state=True)]:
            with self.subTest(mutation=mutation):
                local = self.local_refiner(mutate=mutation)
                result = MaaVisionReadback(client=Client(layout()), local_refiner=local).observe(self.path, expected=EXPECTED)
                self.assertFalse(result['ok'])
                self.assertEqual(result['targets'], {})
                self.assertFalse(result['single_job'])
                self.assertEqual(result['reason'], 'vision_local_conflict')

    def test_local_preview_ocr_does_not_veto_independent_model_readback(self):
        local = self.local_refiner(mutate=lambda result: result.update(
            expected_title_visible=False, expected_stage_visible=False,
            expected_operators_visible=[], missing_operators=EXPECTED['operator_names']))
        result = MaaVisionReadback(client=Client(layout()), local_refiner=local).observe(self.path, expected=EXPECTED)
        self.assertTrue(result['expected_title_visible'])
        self.assertTrue(result['expected_stage_visible'])
        self.assertEqual(result['missing_operators'], [])

    def test_local_preview_cannot_supply_content_absent_from_model(self):
        local = self.local_refiner()
        result = MaaVisionReadback(client=Client(layout(title='其他关卡')),
                                 local_refiner=local).observe(self.path, expected=EXPECTED)
        self.assertFalse(result['expected_title_visible'])

    def test_local_exact_label_repairs_model_checkbox_associated_with_wrong_square(self):
        local = self.local_refiner()
        cv2.rectangle(self.image, (175, 380), (192, 397), (190, 180, 0), -1)
        cv2.imencode('.png', self.image)[1].tofile(str(self.path))
        model = layout()
        model['words'].append(word('借助战', 200, 280, 60, 18))
        result = MaaVisionReadback(client=Client(model), local_refiner=local).observe(self.path, expected=EXPECTED)
        self.assertTrue(result['ok'])
        self.assertTrue(result['options']['use_support']['state'])
        self.assertGreater(result['targets']['use_support']['rect']['y'], 370)

    def test_field_empty_is_remeasured_after_local_hint_geometry_calibration(self):
        hint = '作业路径/神秘代码'
        local = self.local_refiner(code=hint)
        self.image[149:159, 114:280] = (180, 180, 180)
        self.image[145:163, 108:110] = (0, 0, 0)
        cv2.imencode('.png', self.image)[1].tofile(str(self.path))
        model = layout(code=hint)
        # The model has transcribed the real hint, but placed it above the
        # measured input. The field's non-white hint then fails blank pixels.
        model['words'][2]['rect']['y'] = 110
        uncalibrated = MaaVisionReadback(client=Client(model)).observe(self.path)
        self.assertFalse(uncalibrated['field_empty'])
        self.assertEqual(uncalibrated['code_text'], '')
        result = MaaVisionReadback(client=Client(model), local_refiner=local).observe(self.path)
        self.assertTrue(result['ok'], result.get('local_refinement_reason'))
        self.assertTrue(result['field_empty'])
        self.assertEqual(result['field_empty_measurement'], 'local_geometry_and_pixels')
        self.assertEqual(result['recognition_engine'], 'deepseek_vision+local_geometry')

    def test_local_empty_geometry_cannot_override_model_real_text_or_code_conflicts(self):
        hint = '作业路径/神秘代码'
        for code in ('prts://999999', 'l', 'other.json'):
            with self.subTest(code=code):
                local = self.local_refiner(code=hint)
                result = MaaVisionReadback(client=Client(layout(code=code)), local_refiner=local).observe(self.path)
                self.assertFalse(result['ok'])
                self.assertEqual(result['targets'], {})
                self.assertEqual(result['local_refinement_reason'], 'field_contents_disagree')
        local = self.local_refiner()
        result = MaaVisionReadback(client=Client(layout(code='prts://999999')),
                                 local_refiner=local).observe(self.path)
        self.assertFalse(result['ok'])
        self.assertEqual(result['local_refinement_reason'], 'code_text_disagrees')

    def test_local_nonempty_input_is_never_promoted_by_model_empty_hint(self):
        local = self.local_refiner()
        result = MaaVisionReadback(client=Client(layout(code='作业路径/神秘代码')),
                                 local_refiner=local).observe(self.path)
        self.assertFalse(result['field_empty'])

    def test_model_hint_and_measured_pixels_recover_local_placeholder_ocr_errors(self):
        local = self.local_refiner(code='篾业路平神沁代码')
        self.image[149:159, 114:280] = (180, 180, 180)
        self.image[145:163, 108:110] = (0, 0, 0)
        cv2.imencode('.png', self.image)[1].tofile(str(self.path))
        model = layout(code='作业路径/神秘代码')
        self.assertFalse(local.observe(self.path)['field_empty'])
        result = MaaVisionReadback(client=Client(model), local_refiner=local).observe(self.path)
        self.assertTrue(result['ok'])
        self.assertTrue(result['field_empty'])
        self.assertEqual(result['field_empty_measurement'], 'model_hint_and_local_geometry_pixels')

    def test_model_hint_needs_real_hint_pixels_and_rejects_actual_dark_text(self):
        for gray_hint, dark_text in ((False, False), (True, True)):
            with self.subTest(gray_hint=gray_hint, dark_text=dark_text):
                local = self.local_refiner(code='篾业路平神沁代码')
                self.image[140:170, 105:400] = 255
                if gray_hint:
                    self.image[149:159, 114:280] = (180, 180, 180)
                self.image[145:163, 108:110] = (0, 0, 0)
                if dark_text:
                    self.image[146:161, 125:134] = (0, 0, 0)
                cv2.imencode('.png', self.image)[1].tofile(str(self.path))
                result = MaaVisionReadback(client=Client(layout(code='作业路径/神秘代码')),
                                         local_refiner=local).observe(self.path)
                self.assertFalse(result['field_empty'])

    def test_local_refinement_is_opt_in_and_not_used_for_complete_model_controls(self):
        local = self.local_refiner()
        complete = layout()
        complete['words'] += [word('忽略干员属性要求', 200, 330, 180, 18),
                              word('借助战', 200, 380, 60, 18), word('多作业模式', 200, 430, 100, 18)]
        result = MaaVisionReadback(client=Client(complete), local_refiner=local).observe(self.path, expected=EXPECTED)
        self.assertTrue(result['single_job'])
        self.assertEqual(local.calls, 0)
        self.assertEqual(result['recognition_engine'], 'deepseek_vision')

    def test_model_observation_cannot_verify_without_two_fresh_deterministic_receipts(self):
        def observed(code, title):
            return MaaVisionReadback(client=Client(layout(code=code, title=title))).observe(self.path, expected=EXPECTED)
        before = observed('prts://111111', '其他关卡')
        before['screenshot_id'] = 'before'
        entry = observed(EXPECTED['code'], '其他关卡')
        entry['screenshot_id'] = 'entry'
        after = observed(EXPECTED['code'], EXPECTED['title'])
        after['screenshot_id'] = 'after'
        result = verify_import(before, [after], expected=EXPECTED, code_entry=entry)
        self.assertFalse(result['verified'])
        self.assertIn('two_fresh_readbacks_required', result['reasons'])


class PreviewCloseupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'loaded.png'
        self.image = np.full((800, 1280, 3), 255, dtype=np.uint8)
        for x, y, width, height in [(100, 135, 420, 38), (522, 135, 35, 38), (560, 135, 35, 38)]:
            cv2.rectangle(self.image, (x, y), (x + width - 1, y + height - 1), (180, 180, 180), 1)
        for text, y in [('TITLE AA-2', 465), ('TITLE AA-2', 493), ('Alpha skill 1', 565),
                        ('Beta skill 2', 605), ('2 operators', 635)]:
            cv2.putText(self.image, text, (650, y), cv2.FONT_HERSHEY_SIMPLEX, .5, (33, 33, 33), 1)
        cv2.line(self.image, (650, 520), (1080, 520), (33, 33, 33), 1)
        self.source = layout()
        self.source['words'].append(word(EXPECTED['title'], 650, 478, 390, 18))
        self.source['words'].append(word('动态帮助文字', 700, 200, 150, 18))
        self.save()

    def save(self):
        cv2.imencode('.png', self.image)[1].tofile(str(self.path))

    def test_count_and_actual_separator_bound_stable_preview_pixels_without_expected(self):
        region = preview_region_from_count(self.image, self.source)
        self.assertIsNotNone(region)
        rect = region['rect']
        self.assertLess(rect['y'], 455)
        self.assertGreater(rect['y'], 420)
        self.assertGreater(rect['y'] + rect['height'], 635)
        self.assertLess(rect['y'] + rect['height'], 650)
        altered = self.image.copy()
        cv2.putText(altered, 'changing help and timestamp', (650, 350), cv2.FONT_HERSHEY_SIMPLEX, .5, (33, 33, 33), 1)
        cv2.putText(altered, 'changing review and footer', (650, 740), cv2.FONT_HERSHEY_SIMPLEX, .5, (33, 33, 33), 1)
        again = preview_region_from_count(altered, self.source)
        self.assertEqual(region['rect'], again['rect'])
        self.assertEqual(region['sha256'], again['sha256'])
        altered[560, 700] = (0, 0, 0)
        self.assertNotEqual(region['sha256'], preview_region_from_count(altered, self.source)['sha256'])

    def test_ambiguous_count_or_missing_measured_structure_cannot_create_preview(self):
        source = copy.deepcopy(self.source)
        source['words'].append(word('共3名干员', 650, 680, 120, 18))
        self.assertIsNone(preview_region_from_count(self.image, source))
        image = self.image.copy()
        image[520, 650:1081] = 255
        self.assertIsNone(preview_region_from_count(image, self.source))

    def test_hough_line_arrays_work_in_both_supported_opencv_shapes(self):
        expected = preview_region_from_count(self.image, self.source)
        original = cv2.HoughLinesP
        def flattened(*args, **kwargs):
            lines = original(*args, **kwargs)
            return None if lines is None else np.asarray(lines).reshape(-1, 4)
        with patch('cv2.HoughLinesP', side_effect=flattened):
            self.assertEqual(preview_region_from_count(self.image, self.source), expected)

    def test_preview_signature_binds_position_as_well_as_identical_content(self):
        original = preview_region_from_count(self.image, self.source)
        shifted = self.image.copy()
        shifted[430:660, 640:1100] = 255
        shifted[440:670, 640:1100] = self.image[430:660, 640:1100]
        source = copy.deepcopy(self.source)
        for entry in source['words']:
            if entry['rect']['x'] >= 640 and entry['rect']['y'] >= 400:
                entry['rect']['y'] += 10
        moved = preview_region_from_count(shifted, source)
        self.assertEqual(moved['rect']['y'], original['rect']['y'] + 10)
        self.assertEqual(moved['sha256'], original['sha256'])
        self.assertNotEqual(moved['signature'], original['signature'])

    def test_count_bbox_on_previous_operator_row_cannot_truncate_count_pixels(self):
        expected = preview_region_from_count(self.image, self.source)
        inaccurate = copy.deepcopy(self.source)
        next(word for word in inaccurate['words'] if word['text'] == '共2名干员')['rect']['y'] = 590
        actual = preview_region_from_count(self.image, inaccurate)
        self.assertEqual(actual['rect'], expected['rect'])
        self.assertEqual(actual['signature'], expected['signature'])

    def test_disabling_closeup_keeps_one_unhinted_read_and_actual_structure_proof(self):
        region = preview_region_from_count(self.image, self.source)
        client = Client(self.source)
        result = MaaVisionReadback(client=client, preview_closeup=False).observe(self.path, expected=EXPECTED)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result['preview_readback'], 'count_anchored_model')
        self.assertTrue(result['preview_structure_verified'])
        self.assertEqual(result['regions']['preview'], region['rect'])
        self.assertEqual(result['preview_signature'], region['signature'])
        self.assertEqual(result['preview_sha256'], region['sha256'])
        self.assertTrue(result['expected_title_visible'])
        self.assertEqual(result['operator_count'], 2)
        inaccurate = copy.deepcopy(self.source)
        next(word for word in inaccurate['words'] if word['text'] == '共2名干员')['rect']['y'] = 590
        client = Client(inaccurate)
        result = MaaVisionReadback(client=client, preview_closeup=False).observe(self.path, expected=EXPECTED)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result['operator_count'], 2)
        self.assertEqual(result['preview_signature'], region['signature'])

    def test_disabled_closeup_cannot_add_missing_semantics_or_promote_missing_count(self):
        source = copy.deepcopy(self.source)
        for entry in source['words']:
            if entry['text'] == EXPECTED['title']:
                entry['text'] = '另一个标题'
        source['words'] = [entry for entry in source['words'] if entry['text'] != '干员乙']
        result = MaaVisionReadback(client=Client(source), preview_closeup=False).observe(self.path, expected=EXPECTED)
        self.assertFalse(result['expected_title_visible'])
        self.assertIn('干员乙', result['missing_operators'])
        source['words'] = [entry for entry in source['words'] if entry['text'] != '共2名干员']
        client = Client(source)
        result = MaaVisionReadback(client=client, preview_closeup=False).observe(self.path, expected=EXPECTED)
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(result.get('preview_structure_verified', False))
        self.assertNotEqual(result.get('preview_readback'), 'count_anchored_model')

    def test_second_model_read_is_unhinted_mapped_and_uses_structure_signature(self):
        region = preview_region_from_count(self.image, self.source)
        rect = region['rect']
        close_words = [entry for entry in self.source['words']
                       if rect['x'] <= entry['rect']['x'] and rect['y'] <= entry['rect']['y']
                       and entry['rect']['y'] + entry['rect']['height'] <= rect['y'] + rect['height']]
        class CloseupClient(Client):
            def complete(inner, messages, **kwargs):
                if not inner.calls:
                    return super().complete(messages, **kwargs)
                url = next(block['image_url']['url'] for block in messages[1]['content'] if block['type'] == 'image_url')
                image = cv2.imdecode(np.frombuffer(base64.b64decode(url.split(',', 1)[1]), np.uint8), 1)
                height, width = image.shape[:2]
                mapped = []
                for entry in close_words:
                    box = entry['rect']
                    mapped.append({'text': entry['text'], 'rect': {
                        'x': (box['x'] - rect['x']) * width / rect['width'],
                        'y': (box['y'] - rect['y']) * height / rect['height'],
                        'width': box['width'] * width / rect['width'],
                        'height': box['height'] * height / rect['height']}})
                inner.result = {'width': width, 'height': height, 'words': mapped}
                return super().complete(messages, **kwargs)
        client = CloseupClient(self.source)
        result = MaaVisionReadback(client=client).observe(self.path, expected=EXPECTED)
        self.assertEqual(len(client.calls), 2)
        self.assertIn('预览局部', client.calls[1][0][1]['content'][0]['text'])
        for messages, _ in client.calls:
            request = json.dumps(messages, ensure_ascii=False)
            self.assertNotIn(EXPECTED['title'], request)
            self.assertNotIn(EXPECTED['code'], request)
        self.assertEqual(result['preview_signature'], region['signature'])
        self.assertEqual(result['preview_sha256'], region['sha256'])
        self.assertEqual(result['regions']['preview'], rect)
        self.assertTrue(result['preview_structure_verified'])
        self.assertEqual(result['preview_readback'], 'count_anchored_model_closeup')
        self.assertTrue(result['expected_title_visible'])
        self.assertTrue(result['expected_stage_visible'])
        self.assertEqual(result['operator_count'], 2)
        self.assertEqual(result['missing_operators'], [])
        self.assertNotIn('动态帮助文字', result['preview_lines'])

        class MissingCountClient(CloseupClient):
            def complete(inner, messages, **kwargs):
                response = super().complete(messages, **kwargs)
                if len(inner.calls) == 2:
                    value = json.loads(response['content'])
                    value['words'] = [entry for entry in value['words'] if entry['text'] != '共2名干员']
                    response['content'] = json.dumps(value, ensure_ascii=False)
                return response
        with self.assertRaises(VisionOcrError) as caught:
            MaaVisionReadback(client=MissingCountClient(self.source)).observe(self.path, expected=EXPECTED)
        evidence = caught.exception.evidence
        self.assertEqual(evidence['stage'], 'preview_count_mismatch')
        self.assertEqual(evidence['crop']['rect'], region['rect'])
        self.assertEqual(len(evidence['crop']['sha256']), 64)
        self.assertEqual(evidence['full_frame']['words'], self.source['words'])
        self.assertEqual(len(evidence['closeup']['words']), len(close_words) - 1)
        self.assertNotIn('base64', json.dumps(evidence))


if __name__ == '__main__':
    unittest.main()

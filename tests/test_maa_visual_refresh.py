"""Fresh native frames must locally justify reuse of slow visual recognition."""
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from shu.maa_visual_import import VisualCodeImporter, VisualImportError, _compare_frame_images


IDENTITY = {'hwnd': 10, 'pid': 20, 'creation_time': 30, 'executable': 'fixture-maa.exe'}
EXPECTED = {'code': 'prts://42', 'title': '测试 A-1', 'stage_code': 'A-1', 'operator_names': ['甲']}


class RefreshSupervisor:
    target_identity = IDENTITY

    def __init__(self, frames):
        self.frames = list(frames)
        self.commands = []

    def request(self, command, **kwargs):
        self.commands.append(command)
        if command != {'op': 'snapshot'}:
            raise AssertionError(command)
        return {'status': 'completed', 'result': self.frames.pop(0)}


class RefreshTests(unittest.TestCase):
    def run_focus_import(self, *, preflight=False, acquired=True, interrupt=None):
        """Replace only native/vision boundaries; exercise the import recipe."""
        frames = {}
        events, submitted = [], []
        cancel = threading.Event()
        outer = self

        class Supervisor:
            target_identity = IDENTITY
            foreground = False
            multiple = True
            typed = False
            sequence = 0
            focused = False

            def health(self):
                return {}

            def request(self, command, **kwargs):
                outer.assertEqual(command, {'op': 'snapshot'})
                self.sequence += 1
                frame = outer.frame('focus-' + str(self.sequence))
                frame['window']['foreground'] = self.foreground
                frame['single_job'] = not self.multiple
                frame['options'] = {'multiple_jobs': {'state': self.multiple}}
                frame['targets']['multiple_jobs'] = {
                    'rect': {'x': 70, 'y': 15, 'width': 12, 'height': 12},
                    'point': {'x': 76, 'y': 21}}
                if self.focused and interrupt == 'page':
                    frame['page'] = 'settings'
                if self.focused and interrupt == 'permission':
                    frame['permission']['input_allowed'] = False
                if self.typed:
                    frame.update(field_empty=False, codes=[EXPECTED['code']],
                                 expected_title_visible=True, expected_stage_visible=True,
                                 missing_operators=[], preview_signature='loaded',
                                 image_sha256='loaded-native-hash')
                events.append(('capture', frame['screenshot_id']))
                frames[frame['image_path']] = copy.deepcopy(frame)
                return {'status': 'completed', 'result': frame}

        supervisor = Supervisor()

        class Readback:
            def observe(self, path, **kwargs):
                return frames[str(path)]

        def submit(args, frame, event):
            events.append(('action', args['action']))
            submitted.append((copy.deepcopy(args), copy.deepcopy(frame)))
            if args['action'] == 'focus':
                supervisor.focused = True
                supervisor.foreground = acquired
                if interrupt == 'cancel':
                    cancel.set()
                receipt = {'focus_acquired': acquired, 'caption_click': acquired}
            else:
                receipt = {}
                if args.get('target') == 'multiple_jobs':
                    supervisor.multiple = False
                if args['action'] == 'replace_code':
                    supervisor.typed = True
            return {'status': 'completed', 'submission_entered': True,
                    'operation_id': 'operation-' + str(len(submitted)), 'result': receipt}

        def publish(kind, payload):
            self.published.append((kind, copy.deepcopy(payload)))
            return {'kind': kind, 'sequence': len(self.published)}

        importer = VisualCodeImporter(supervisor, readback=Readback(), submit=submit,
                                      publish=publish, activate_before_capture=preflight)
        # The removed manual wait must never poll desktop discovery. Native
        # activation is already bounded by the supervisor's action timeout.
        with patch('shu.maa_visual_native.discover_maa_windows',
                   side_effect=AssertionError('manual foreground polling')), \
                patch.object(cancel, 'wait', side_effect=lambda seconds: cancel.is_set()):
            result = importer.run(EXPECTED, cancel_event=cancel)
        return result, submitted, events

    def test_background_mode_switch_activates_and_recaptures_before_input(self):
        result, submitted, events = self.run_focus_import()
        self.assertEqual(result['status'], 'import_verified', result)
        self.assertEqual([(args['action'], args.get('target')) for args, _ in submitted],
                         [('focus', None), ('click', 'multiple_jobs'),
                          ('click', 'code_input'), ('replace_code', None)])
        self.assertEqual(events[:4], [('capture', 'focus-1'), ('action', 'focus'),
                                     ('capture', 'focus-2'), ('action', 'click')])
        self.assertTrue(all(frame['window']['foreground'] for args, frame in submitted[1:]))
        proof = next(payload for kind, payload in self.published if kind == 'maa-visual-import-proof')
        self.assertEqual([action['operation_id'] for action in proof['actions']],
                         ['operation-1', 'operation-2', 'operation-3', 'operation-4'])
        self.assertTrue(proof['actions'][0]['receipt']['caption_click'])
        self.assertIs(result['battle_start_submitted'], False)

    def test_denied_activation_never_toggles_mode_or_waits_for_user(self):
        for preflight in (False, True):
            with self.subTest(preflight=preflight):
                result, submitted, _ = self.run_focus_import(preflight=preflight, acquired=False)
                self.assertEqual(result['code'], 'maa_visual_not_foreground', result)
                self.assertEqual([args['action'] for args, _ in submitted], ['focus'])
                self.assertEqual(result['action_outcome'], 'not_issued')

    def test_cancel_during_activation_blocks_mode_and_code_input(self):
        result, submitted, _ = self.run_focus_import(preflight=True, interrupt='cancel')
        self.assertEqual(result['code'], 'maa_visual_cancelled', result)
        self.assertEqual([args['action'] for args, _ in submitted], ['focus'])
        self.assertEqual(result['action_outcome'], 'unverified')

    def test_activation_rechecks_page_and_permission_before_mode_input(self):
        for interrupt, code in [('page', 'maa_visual_page'), ('permission', 'maa_visual_integrity')]:
            with self.subTest(interrupt=interrupt):
                result, submitted, _ = self.run_focus_import(preflight=True, interrupt=interrupt)
                self.assertEqual(result['code'], code, result)
                self.assertEqual([args['action'] for args, _ in submitted], ['focus'])

    def test_preflight_activation_precedes_first_capture_without_fake_snapshot(self):
        calls=[]
        importer=self.importer([])
        importer.activate_before_capture=True
        def submit(args,frame,cancel):
            calls.append((args,frame))
            return {'status':'completed','submission_entered':True,'result':{'focus_acquired':True,'caption_click':True}}
        importer.submit=submit
        def observe(*args,**kwargs):
            self.assertEqual(calls[0][0]['action'],'focus')
            self.assertIsNone(calls[0][0]['screenshot_id'])
            self.assertTrue(calls[0][1]['native_identity_only'])
            raise RuntimeError('end after checking order')
        importer.observe=observe
        result=importer.run(EXPECTED)
        self.assertNotEqual(result['status'],'import_verified')
        self.assertEqual(result['action_outcome'],'unverified')
        self.assertEqual(len(calls),1)

    def test_transient_non_control_change_requires_new_interpretation_and_fresh_confirmation(self):
        from unittest.mock import Mock
        changed = self.image.copy()
        changed[65:68,100:102] = 100
        source = self.frame('source')
        newer = self.frame('newer',changed)
        importer = self.importer([self.frame('fresh',changed),self.frame('stable',changed)])
        importer.observe = Mock(return_value=newer)
        result = importer._refresh_for_action(source,expected=EXPECTED)
        self.assertEqual(result['screenshot_id'],'stable')
        self.assertEqual(len(importer.supervisor.commands),2)
        importer.observe.assert_called_once()

    def test_reobservation_with_changed_field_never_reuses_old_action(self):
        from unittest.mock import Mock
        changed = self.image.copy()
        changed[65:68,100:102] = 100
        source = self.frame('source')
        newer = self.frame('newer',changed)
        newer['code_text'] = 'prts://999'
        importer = self.importer([self.frame('fresh',changed)])
        importer.observe = Mock(return_value=newer)
        with self.assertRaises(VisualImportError):
            importer._refresh_for_action(source,expected=EXPECTED)
        self.assertEqual(len(importer.supervisor.commands),1)

    def test_reobservation_cannot_rebind_changed_native_geometry_or_permission(self):
        from unittest.mock import Mock
        changed = self.image.copy()
        changed[65:68, 100:102] = 100
        mutations = [lambda frame: frame['window']['client_rect'].update(left=1),
                     lambda frame: frame['window']['client_rect'].update(top=1),
                     lambda frame: frame['window'].update(dpi=120),
                     lambda frame: frame['window'].update(executable='another-maa.exe'),
                     lambda frame: frame['permission'].update(input_allowed=False)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                source = self.frame('source')
                renewed = self.frame('renewed', changed)
                confirmation = self.frame('confirmation', changed)
                mutate(renewed)
                # A second capture at the new position cannot grant a new
                # binding to an action planned against the original window.
                confirmation['window'] = copy.deepcopy(renewed['window'])
                importer = self.importer([self.frame('fresh', changed), confirmation])
                importer.observe = Mock(return_value=renewed)
                with self.assertRaises(VisualImportError) as raised:
                    importer._refresh_for_action(source, expected=EXPECTED)
                self.assertEqual(raised.exception.code, 'maa_visual_refresh_changed')
                self.assertEqual(len(importer.supervisor.commands), 1)

    def run_remeasured_click(self, mutate=None):
        from unittest.mock import Mock
        changed = self.image.copy()
        changed[65:68, 100:102] = 100
        source, renewed = self.frame('source'), self.frame('renewed', changed)
        source['options'] = {'auto_formation': {'state': True, 'rect': {'x': 65, 'y': 15}}}
        renewed['options'] = {'auto_formation': {'state': True, 'rect': {'x': 66, 'y': 15}}}
        renewed['targets']['code_input']['rect']['x'] = 6
        renewed['targets']['code_input']['point']['x'] = 24
        renewed['forbidden_regions'][0]['rect']['y'] = 39
        if mutate:
            mutate(renewed)
        native = []
        for name in ('fresh', 'stable'):
            captured = self.frame(name, changed)
            # Native capture carries geometry and pixels, not OCR targets.
            native.append({key: captured[key] for key in ('ok', 'screenshot_id', 'image_path',
                'image_sha256', 'width', 'height', 'window', 'permission')})
        importer = self.importer(native)
        importer.observe = Mock(side_effect=[source, renewed, RuntimeError('end after click')])
        submitted = []
        def submit(args, frame, cancel):
            submitted.append((args, frame))
            return {'status': 'completed', 'submission_entered': True, 'result': {}}
        importer.submit = submit
        importer.supervisor.health = lambda: {}
        return importer.run(EXPECTED), submitted, self.published

    def test_reobservation_remeasures_click_after_coordinate_jitter(self):
        result, submitted, published = self.run_remeasured_click()
        self.assertEqual(len(submitted), 1, result)
        self.assertEqual((submitted[0][0]['x'], submitted[0][0]['y']), (24, 22))
        self.assertEqual(submitted[0][1]['screenshot_id'], 'stable')
        self.assertTrue(any(kind == 'maa-visual-refresh-reobservation' for kind, _ in published))

    def test_reobservation_changed_semantics_or_forbidden_geometry_never_click(self):
        mutations = [lambda f: f.update(page='settings'),
                     lambda f: f['options']['auto_formation'].update(state=False),
                     lambda f: f.update(code_text='prts://999', codes=['prts://999']),
                     lambda f: f['forbidden_regions'][0].update(kind='stop')]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                result, submitted, _ = self.run_remeasured_click(mutation)
                self.assertEqual(result['code'], 'maa_visual_refresh_changed', result)
                self.assertEqual(submitted, [])

    def test_remeasured_click_is_checked_against_current_forbidden_area(self):
        def unsafe(frame):
            frame['targets']['code_input'] = {
                'rect': {'x': 5, 'y': 40, 'width': 50, 'height': 15},
                'point': {'x': 20, 'y': 45}}
        result, submitted, _ = self.run_remeasured_click(unsafe)
        self.assertEqual(result['code'], 'maa_visual_unsafe_target', result)
        self.assertEqual(submitted, [])

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.image = np.full((90, 120, 3), 255, dtype=np.uint8)
        self.image[40:55, 5:55] = 190
        self.published = []

    def frame(self, name, image=None):
        path = self.root / (name + '.png')
        path.write_bytes(cv2.imencode('.png', self.image if image is None else image)[1].tobytes())
        return {'ok': True, 'screenshot_id': name, 'image_path': str(path),
                'image_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'width': 120, 'height': 90,
                'window': {**IDENTITY, 'client_rect': {'left': 0, 'top': 0, 'width': 120, 'height': 90},
                           'dpi': 96, 'foreground': True, 'minimized': False},
                'permission': {'input_allowed': True}, 'page': 'copilot', 'idle': True,
                'running': False, 'single_job': True, 'field_empty': True, 'codes': [],
                'expected_title_visible': False, 'expected_stage_visible': False,
                'missing_operators': ['甲'], 'regions': {'code_input': {'x': 5, 'y': 15, 'width': 50, 'height': 15}},
                'targets': {'code_input': {'rect': {'x': 5, 'y': 15, 'width': 50, 'height': 15},
                                           'point': {'x': 20, 'y': 22}}},
                'forbidden_regions': [{'kind': 'start', 'rect': {'x': 5, 'y': 40, 'width': 50, 'height': 15}}]}

    def importer(self, fresh, *, visual=True):
        class Readback:
            requires_frame_refresh = visual
        supervisor = RefreshSupervisor(fresh)
        def publish(kind, payload):
            self.published.append((kind, copy.deepcopy(payload)))
            return {'kind': kind, 'sequence': len(self.published)}
        return VisualCodeImporter(supervisor, readback=Readback(), publish=publish)

    def test_refresh_uses_fresh_native_id_and_keeps_comparison_evidence(self):
        source, fresh = self.frame('source'), self.frame('fresh')
        importer = self.importer([fresh])
        refreshed = importer._refresh_for_action(source, target='code_input')
        self.assertEqual(refreshed['screenshot_id'], 'fresh')
        self.assertTrue(refreshed['field_empty'])
        comparison = [value for kind, value in self.published if kind == 'maa-visual-refresh-comparison'][0]
        self.assertTrue(comparison['ok'], comparison)
        self.assertEqual(comparison['source']['screenshot_id'], 'source')
        self.assertEqual(comparison['fresh']['screenshot_id'], 'fresh')
        self.assertTrue(refreshed['visual_refresh_refs'])

    def test_default_ocr_retains_existing_pathless_test_seam(self):
        importer = self.importer([], visual=False)
        frame = {'screenshot_id': 'fixture-without-path'}
        self.assertIs(importer._refresh_for_action(frame), frame)
        self.assertEqual(importer.supervisor.commands, [])

    def test_invalid_interpretation_keeps_native_frame_and_failure_evidence(self):
        source = self.frame('invalid-source')
        importer = self.importer([source])
        class InvalidReadback:
            def observe(self, *args, **kwargs):
                return {'ok': False, 'reason': 'vision_local_conflict',
                        'local_refinement_reason': 'field_contents_disagree'}
        importer.readback = InvalidReadback()
        with self.assertRaises(VisualImportError) as raised:
            importer.observe()
        self.assertTrue(raised.exception.evidence_refs)
        kind, payload = self.published[-1]
        self.assertEqual(kind, 'maa-visual-invalid-observation')
        self.assertEqual(payload['native']['screenshot_id'], source['screenshot_id'])
        self.assertEqual(payload['interpretation']['local_refinement_reason'], 'field_contents_disagree')
        self.assertEqual(importer.supervisor.commands, [{'op': 'snapshot'}])

    def test_interpretation_exception_keeps_safe_crop_diagnostics(self):
        source = self.frame('failed-crop')
        importer = self.importer([source])
        error = RuntimeError('crop count mismatch')
        error.evidence = {'stage': 'preview_closeup', 'count': 2}
        class FailedReadback:
            def observe(self, *args, **kwargs):
                raise error
        importer.readback = FailedReadback()
        with self.assertRaises(RuntimeError) as raised:
            importer.observe()
        self.assertTrue(raised.exception.evidence_refs)
        self.assertEqual(self.published[-1][1]['interpretation_error'], error.evidence)

    def test_visual_refresh_never_accepts_missing_or_tampered_original_image(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                source, fresh = self.frame('source'), self.frame('fresh')
                if missing:
                    source.pop('image_path')
                else:
                    Path(source['image_path']).write_bytes(b'tampered')
                importer = self.importer([fresh])
                with self.assertRaises(VisualImportError) as raised:
                    importer._refresh_for_action(source, target='code_input')
                self.assertEqual(raised.exception.code, 'maa_visual_refresh_changed')
                self.assertTrue(raised.exception.evidence_refs)

    def test_geometry_identity_and_reused_ids_fail_closed(self):
        for mutation in (lambda f: f['window']['client_rect'].update(left=1),
                         lambda f: f['window'].update(dpi=120),
                         lambda f: f['window'].update(pid=21),
                         lambda f: f.update(screenshot_id='source')):
            source, fresh = self.frame('source'), self.frame('fresh')
            mutation(fresh)
            with self.assertRaises(VisualImportError):
                self.importer([fresh])._refresh_for_action(source, target='code_input')

    def test_target_forbidden_and_preview_pixel_changes_are_rejected(self):
        for point in ((20, 22), (10, 45), (100, 70)):
            with self.subTest(point=point):
                altered = self.image.copy()
                altered[point[1], point[0]] = 0
                with self.assertRaises(VisualImportError):
                    self.importer([self.frame('fresh', altered)])._refresh_for_action(
                        self.frame('source'), target='code_input')

    def test_keyboard_allows_only_tiny_caret_inside_input(self):
        caret = self.image.copy()
        caret[18:28, 40] = 0
        refreshed = self.importer([self.frame('fresh', caret)])._refresh_for_action(self.frame('source'))
        self.assertEqual(refreshed['screenshot_id'], 'fresh')
        changed_text = self.image.copy()
        changed_text[18:28, 37:45] = 0
        with self.assertRaises(VisualImportError):
            self.importer([self.frame('new-text', changed_text)])._refresh_for_action(self.frame('source'))

    def test_code_input_click_allows_only_existing_tiny_caret_exception(self):
        caret = self.image.copy()
        caret[18:28, 40] = 0
        refreshed = self.importer([self.frame('fresh', caret)])._refresh_for_action(
            self.frame('source'), target='code_input')
        self.assertEqual(refreshed['screenshot_id'], 'fresh')
        comparison = self.published[-1][1]
        self.assertTrue(comparison['caret_exception'])

    def queue_frame(self, name, image=None):
        frame = self.frame(name, image)
        viewport = {'x': 62, 'y': 33, 'width': 50, 'height': 42}
        frame['queue_mode'] = True
        frame['regions']['queue_list'] = viewport
        frame['targets']['queue_list'] = {'rect': viewport}
        frame['targets']['row_delete:old'] = {'rect': {'x': 95, 'y': 68, 'width': 8, 'height': 6}}
        frame['targets']['queue_add'] = {'rect': {'x': 72, 'y': 76, 'width': 8, 'height': 8}}
        frame['forbidden_regions'].append(
            {'kind': 'queue_clear', 'rect': {'x': 92, 'y': 75, 'width': 12, 'height': 10}})
        return frame

    def test_scrolled_queue_under_footer_halo_is_ignored_only_for_batch_code_input(self):
        pixels = self.image.copy()
        pixels[68:75, 95:103] = 90  # row changed, including the footer's 3px guard band
        pixels[18:28, 40] = 0      # independent caret blink
        source, fresh = self.queue_frame('source'), self.queue_frame('fresh', pixels)
        result = _compare_frame_images(source, fresh, target='code_input', allow_queue_viewport=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['ignored_queue_pixels'], 56)
        self.assertTrue(result['caret_exception'])
        for target, allow in (('code_input', False), ('queue_list', True), ('row_delete:old', True)):
            with self.subTest(target=target, allow=allow):
                self.assertFalse(_compare_frame_images(source, fresh, target=target,
                                                       allow_queue_viewport=allow)['ok'])

    def test_batch_code_input_still_rejects_start_footer_and_viewport_outline_changes(self):
        source = self.queue_frame('source')
        for x, y in ((10, 45), (95, 76), (75, 79), (62, 45), (100, 33)):
            with self.subTest(x=x, y=y):
                pixels = self.image.copy()
                pixels[68:75, 95:103] = 90
                pixels[y, x] = 0
                fresh = self.queue_frame('fresh', pixels)
                result = _compare_frame_images(source, fresh, target='code_input', allow_queue_viewport=True)
                self.assertFalse(result['ok'], result)

    def test_forged_viewport_may_not_mask_an_actual_control(self):
        for control in ('start', 'queue_clear', 'queue_add'):
            with self.subTest(control=control):
                source = self.queue_frame('source')
                protected = {'x': 68, 'y': 55, 'width': 12, 'height': 10}
                if control == 'queue_add':
                    source['targets'][control]['rect'] = protected
                else:
                    next(region for region in source['forbidden_regions'] if region['kind'] == control)['rect'] = protected
                pixels = self.image.copy()
                pixels[57:59, 70:74] = 0
                result = _compare_frame_images(source, self.queue_frame('fresh', pixels), target='code_input',
                                               allow_queue_viewport=True)
                self.assertFalse(result['ok'], result)

    def test_wheel_refresh_accepts_scrollbar_hover_recolor_and_caret_only(self):
        normal, hovered = self.image.copy(), self.image.copy()
        normal[38:66, 107:109] = 205
        hovered[38:66, 107:109] = 77
        # Most pixels match a solid core, with a handful of antialiased pixels.
        normal[38:40, 107:109], hovered[38:40, 107:109] = 214, 91
        hovered[18:28, 40] = 0
        source, fresh = self.queue_frame('source', normal), self.queue_frame('fresh', hovered)
        result = _compare_frame_images(source, fresh, target='queue_list')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['ignored_scrollbar_pixels'], 56)
        self.assertTrue(result['caret_exception'])
        for target in ('queue_add', 'row_delete:old', 'code_input'):
            with self.subTest(target=target):
                self.assertFalse(_compare_frame_images(source, fresh, target=target)['ok'])

    def test_wheel_refresh_allows_thumb_motion_but_rejects_changed_row_body(self):
        normal, shifted, row_changed = (self.image.copy() for _ in range(3))
        normal[38:66, 107:109] = 205
        shifted[43:71, 107:109] = 77
        row_changed[38:66, 107:109] = 77
        row_changed[50:53, 80:87] = 0
        source = self.queue_frame('source', normal)
        self.assertTrue(_compare_frame_images(source,self.queue_frame('shifted',shifted),target='queue_list')['ok'])
        self.assertFalse(_compare_frame_images(source,self.queue_frame('row',row_changed),target='queue_list')['ok'])
        self.assertFalse(_compare_frame_images(source,self.queue_frame('shifted',shifted),target='queue_add')['ok'])

    def test_other_click_targets_do_not_allow_a_caret_change(self):
        caret = self.image.copy()
        caret[18:28, 40] = 0
        source = self.frame('source')
        source['targets']['auto_formation'] = {'rect': {'x': 80, 'y': 70, 'width': 10, 'height': 10},
                                              'point': {'x': 85, 'y': 75}}
        with self.assertRaises(VisualImportError):
            self.importer([self.frame('fresh', caret)])._refresh_for_action(source, target='auto_formation')

    def caption_frame(self, name, image):
        frame = self.frame(name, image)
        tab = {'text': '自动战斗', 'rect': {'x': 70, 'y': 50, 'width': 40, 'height': 8}}
        frame['anchors'] = {'copilot_tab': copy.deepcopy(tab)}
        frame['targets']['copilot_tab'] = {**copy.deepcopy(tab), 'point': {'x': 90, 'y': 54}}
        field = {'x': 5, 'y': 60, 'width': 50, 'height': 15}
        frame['regions']['code_input'] = field
        frame['targets']['code_input'] = {'rect': field, 'point': {'x': 20, 'y': 67}}
        frame['forbidden_regions'] = [{'kind': 'start', 'rect': {'x': 5, 'y': 80, 'width': 50, 'height': 9}}]
        return frame

    def caption_pair(self):
        source, fresh = self.image.copy(), self.image.copy()
        source[5:10, 10:25] = (70, 170, 100)
        fresh[5:10, 10:25] = (200, 140, 50)
        return source, fresh

    def test_only_caption_recolor_and_input_caret_are_ignored_with_counts(self):
        source, fresh = self.caption_pair()
        fresh[63:72, 40:42] = 0
        refreshed = self.importer([self.caption_frame('fresh', fresh)])._refresh_for_action(
            self.caption_frame('source', source), target='code_input')
        self.assertEqual(refreshed['screenshot_id'], 'fresh')
        comparison = self.published[-1][1]
        self.assertEqual(comparison['changed_pixels'], 93)
        self.assertEqual(comparison['ignored_caption_pixels'], 75)
        self.assertEqual(comparison['remaining_changed_pixels'], 18)
        self.assertLessEqual(comparison['caption_region']['height'], 40)
        self.assertTrue(comparison['caret_exception'])

    def test_caption_recolor_is_disabled_without_a_matching_measured_tab(self):
        for missing in ('anchor', 'target', 'conflict'):
            with self.subTest(missing=missing):
                source, fresh = self.caption_pair()
                frame = self.caption_frame('source', source)
                if missing == 'anchor':
                    frame.pop('anchors')
                elif missing == 'target':
                    frame['targets'].pop('copilot_tab')
                else:
                    frame['anchors']['copilot_tab']['rect']['x'] += 1
                with self.assertRaises(VisualImportError):
                    self.importer([self.caption_frame('fresh', fresh)])._refresh_for_action(frame, target='code_input')

    def test_measured_checkbox_allows_only_exact_input_caret_blink(self):
        source, fresh = self.caption_pair()
        fresh[63:72, 40:42] = 0
        frame = self.caption_frame('source', source)
        checkbox = {'rect': {'x': 80, 'y': 60, 'width': 8, 'height': 8}, 'kind':'checkbox'}
        frame['targets']['multiple_jobs'] = checkbox
        other = self.caption_frame('fresh', fresh)
        comparison = _compare_frame_images(frame, other, target='multiple_jobs')
        self.assertTrue(comparison['ok'], comparison)
        self.assertTrue(comparison['caret_exception'])
        self.assertEqual(comparison['ignored_caption_pixels'],75)
        # The same option changing its pixels is still a real layout/state change.
        fresh[62:64,82:84] = 0
        other = self.caption_frame('changed_option',fresh)
        self.assertFalse(_compare_frame_images(frame,other,target='multiple_jobs')['ok'])

    def test_caption_color_changes_below_40_or_in_controls_remain_strict(self):
        for mutation in ('below', 'target', 'forbidden'):
            with self.subTest(mutation=mutation):
                source, fresh = self.caption_pair()
                frame = self.caption_frame('source', source)
                if mutation == 'below':
                    source[41:44, 80:85] = (70, 170, 100)
                    fresh[41:44, 80:85] = (200, 140, 50)
                    frame = self.caption_frame('source', source)
                elif mutation == 'target':
                    frame['targets']['header_control'] = {'rect': {'x': 10, 'y': 5, 'width': 15, 'height': 5}}
                else:
                    frame['forbidden_regions'].append({'rect': {'x': 10, 'y': 5, 'width': 15, 'height': 5}})
                with self.assertRaises(VisualImportError):
                    self.importer([self.caption_frame('fresh', fresh)])._refresh_for_action(frame, target='code_input')

    def test_caption_new_or_moved_text_background_and_nonuniform_recolor_are_rejected(self):
        for mutation in ('added', 'moved', 'background', 'achromatic', 'nonuniform'):
            with self.subTest(mutation=mutation):
                source, fresh = self.caption_pair()
                if mutation == 'added':
                    fresh[5:10, 30:35] = (200, 140, 50)
                elif mutation == 'moved':
                    fresh[5:10, 10:25] = 255
                    fresh[6:11, 10:25] = (200, 140, 50)
                elif mutation == 'background':
                    fresh[0, 0] = 250
                elif mutation == 'achromatic':
                    fresh[5:10, 10:25] = 100
                else:
                    fresh[5:10, 20:25] = (210, 150, 60)
                with self.assertRaises(VisualImportError):
                    self.importer([self.caption_frame('fresh', fresh)])._refresh_for_action(
                        self.caption_frame('source', source), target='code_input')

    def test_slow_model_actions_use_new_ids_and_resolved_preview_keeps_real_entry(self):
        result, supervisor, published = self.run_simulated_import(delay=30, echo=True)
        self.assertEqual(result['status'], 'import_verified', result)
        self.assertEqual([args['action'] for args in supervisor.actions], ['click', 'replace_code'])
        self.assertTrue(all(args['screenshot_id'] == fresh for args, fresh in supervisor.action_frames))
        proof = next(value for kind, value in published if kind == 'maa-visual-import-proof')
        self.assertEqual(proof['code_entry'], supervisor.entry_id)
        self.assertNotIn(supervisor.entry_id, proof['after'])
        self.assertEqual(len(proof['after']), 2)
        self.assertTrue(all(item.get('source_snapshot') for item in proof['actions']))

    def test_expected_argument_is_never_fabricated_as_code_echo(self):
        result, supervisor, _ = self.run_simulated_import(delay=1, echo=False)
        self.assertEqual(result['status'], 'unknown', result)
        self.assertIn('real_code_echo_required', result['summary'])
        self.assertEqual(len(supervisor.actions), 2)

    def test_changed_native_refresh_blocks_input_and_retains_failed_comparison(self):
        result, supervisor, published = self.run_simulated_import(delay=30, echo=True, mutate_refresh=True)
        self.assertEqual(result['code'], 'maa_visual_refresh_changed', result)
        self.assertEqual(supervisor.actions, [])
        comparison = next(value for kind, value in published if kind == 'maa-visual-refresh-comparison')
        self.assertFalse(comparison['ok'])
        self.assertTrue(result['evidence_refs'])

    def test_invalid_total_budget_is_rejected_before_any_input(self):
        result, supervisor, _ = self.run_simulated_import(delay=1, echo=True, import_timeout=121)
        self.assertEqual(result['code'], 'maa_visual_readback_limit', result)
        self.assertEqual(supervisor.actions, [])

    def test_total_readback_budget_does_not_restart_or_retype(self):
        result, supervisor, _ = self.run_simulated_import(delay=44, echo=True)
        self.assertEqual(result['code'], 'maa_visual_recognition_timeout', result)
        self.assertEqual([args['action'] for args in supervisor.actions], ['click', 'replace_code'])

    def test_load_baseline_failure_prevents_typing_the_code(self):
        class Monitor:
            def capture_baseline(self, *args):
                raise RuntimeError('baseline unavailable')
        result, supervisor, _ = self.run_simulated_import(delay=1, echo=True, load_monitor=Monitor())
        self.assertEqual([item['action'] for item in supervisor.actions], ['click'])
        self.assertEqual(result['status'], 'unknown')

    def test_one_invalid_readonly_transcription_retries_without_repeating_input(self):
        result, supervisor, _ = self.run_simulated_import(delay=1, echo=True, invalid_count=1)
        self.assertEqual(result['status'], 'import_verified', result)
        self.assertEqual([item['action'] for item in supervisor.actions], ['click', 'replace_code'])
        result, supervisor, _ = self.run_simulated_import(delay=1, echo=True, invalid_count=2)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(supervisor.actions, [])

    def test_loading_conflicts_after_code_entry_recapture_without_retyping(self):
        result, supervisor, published = self.run_simulated_import(
            delay=1, echo=True, loading_conflicts=2)
        self.assertEqual(result['status'], 'import_verified', result)
        self.assertEqual([item['action'] for item in supervisor.actions], ['click', 'replace_code'])
        rejected = [value for kind, value in published if kind == 'maa-visual-invalid-observation']
        self.assertEqual(len(rejected), 2)
        self.assertEqual(len({value['native']['screenshot_id'] for value in rejected}), 2)
        proof = next(value for kind, value in published if kind == 'maa-visual-import-proof')
        self.assertEqual(proof['code_entry'], supervisor.entry_id)

    def test_loading_conflict_recapture_is_bounded_to_two_extra_observations(self):
        result, supervisor, published = self.run_simulated_import(
            delay=1, echo=True, loading_conflicts=4)
        self.assertEqual(result['code'], 'maa_visual_observation_invalid', result)
        self.assertEqual(supervisor.post_snapshots, 3)
        self.assertEqual([item['action'] for item in supervisor.actions], ['click', 'replace_code'])
        self.assertEqual(sum(kind == 'maa-visual-invalid-observation' for kind, _ in published), 3)

    def test_loading_conflict_reuses_original_import_deadline(self):
        result, supervisor, _ = self.run_simulated_import(
            delay=8, echo=True, import_timeout=12, loading_conflicts=2)
        self.assertEqual(result['code'], 'maa_visual_recognition_timeout', result)
        self.assertEqual(supervisor.post_snapshots, 2)
        self.assertEqual([item['action'] for item in supervisor.actions], ['click', 'replace_code'])

    def test_loading_conflict_cannot_hide_changed_image_or_identity(self):
        for mutation in ('image', 'identity', 'different_image', 'same_checkbox_pixels_disagree'):
            with self.subTest(mutation=mutation):
                result, supervisor, _ = self.run_simulated_import(
                    delay=1, echo=True, loading_conflicts=2, loading_mutation=mutation)
                self.assertEqual(result['code'], 'maa_visual_observation_invalid', result)
                self.assertEqual(supervisor.post_snapshots, 1)

    def test_loading_conflict_before_code_entry_still_blocks_all_input(self):
        result, supervisor, _ = self.run_simulated_import(
            delay=1, echo=True, loading_conflicts=2, loading_mutation='before_entry')
        self.assertEqual(result['code'], 'maa_visual_observation_invalid', result)
        self.assertEqual(supervisor.actions, [])

    def test_cancellation_during_loading_conflict_never_recaptures_or_retypes(self):
        result, supervisor, _ = self.run_simulated_import(
            delay=1, echo=True, loading_conflicts=2, loading_mutation='cancel')
        self.assertEqual(result['code'], 'maa_visual_cancelled', result)
        self.assertEqual(supervisor.post_snapshots, 1)
        self.assertEqual([item['action'] for item in supervisor.actions], ['click', 'replace_code'])

    def test_focus_lost_during_recognition_is_reacquired_before_single_input(self):
        result, supervisor, published = self.run_simulated_import(delay=8, echo=True, focus_lost=True)
        self.assertEqual(result['status'], 'import_verified', result)
        self.assertEqual([args['action'] for args in supervisor.actions], ['focus', 'click', 'replace_code'])
        proof = next(payload for kind, payload in published if kind == 'maa-visual-import-proof')
        self.assertEqual([action['action'] for action in proof['actions']], ['focus', 'click', 'replace_code'])
        self.assertTrue(proof['actions'][0]['receipt']['caption_click'])
        self.assertEqual(proof['actions'][1]['source_snapshot'], 'native-1')
        self.assertEqual(proof['actions'][1]['snapshot'], 'native-3')
        comparisons = [payload for kind, payload in published if kind == 'maa-visual-refresh-comparison']
        self.assertEqual(comparisons[0]['reason'], 'native_identity_geometry_or_permission_changed')
        self.assertTrue(comparisons[1]['ok'])

    def test_focus_reacquisition_denial_is_bounded_without_page_input(self):
        result, supervisor, _ = self.run_simulated_import(
            delay=8, echo=True, focus_lost=True, focus_acquired=False)
        self.assertEqual(result['code'], 'maa_visual_refresh_changed', result)
        self.assertEqual([args['action'] for args in supervisor.actions], ['focus'])
        self.assertEqual(supervisor.sequence, 3)
        self.assertEqual(result['action_outcome'], 'not_issued')

    def test_focus_reacquisition_never_accepts_changed_geometry(self):
        for moment, actions in [('geometry_before', []), ('geometry_after', ['focus'])]:
            with self.subTest(moment=moment):
                result, supervisor, _ = self.run_simulated_import(
                    delay=8, echo=True, focus_lost=True, focus_interrupt=moment)
                self.assertEqual(result['code'], 'maa_visual_refresh_changed', result)
                self.assertEqual([args['action'] for args in supervisor.actions], actions)

    def test_cancel_during_focus_reacquisition_never_submits_page_input(self):
        result, supervisor, _ = self.run_simulated_import(
            delay=8, echo=True, focus_lost=True, focus_interrupt='cancel')
        self.assertEqual(result['code'], 'maa_visual_cancelled', result)
        self.assertEqual([args['action'] for args in supervisor.actions], ['focus'])
        self.assertEqual(result['action_outcome'], 'unverified')

    def run_simulated_import(self, *, delay, echo, mutate_refresh=False, import_timeout=120,
                             load_monitor=None, invalid_count=0, focus_lost=False,
                             focus_acquired=True, focus_interrupt=None,
                             loading_conflicts=0, loading_mutation=None):
        outer = self
        cancel = threading.Event()
        class Clock:
            value = 1000.
            def monotonic(self):
                return self.value
        clock = Clock()
        class Supervisor:
            target_identity = IDENTITY
            def __init__(self):
                self.frames, self.actions, self.action_frames = {}, [], []
                self.typed, self.post_snapshots, self.sequence = False, 0, 0
                self.latest, self.entry_id = None, None
                self.foreground, self.focused = True, False
            def request(self, command, **kwargs):
                if command['op'] != 'snapshot':
                    raise AssertionError(command)
                self.sequence += 1
                name = 'native-' + str(self.sequence)
                image = outer.image.copy()
                if mutate_refresh and self.sequence % 2 == 0:
                    image[75, 100] = 0
                if self.typed:
                    self.post_snapshots += 1
                    image[70:75, 90:110] = 100
                frame = outer.frame(name, image)
                if focus_lost and self.sequence == 2:
                    self.foreground = False
                frame['window']['foreground'] = self.foreground
                if ((focus_interrupt == 'geometry_before' and self.sequence == 2)
                        or (focus_interrupt == 'geometry_after' and self.focused)):
                    frame['window']['client_rect']['left'] = 8
                if self.typed:
                    entry = self.post_snapshots == loading_conflicts + 1 and echo
                    frame.update(field_empty=False, codes=[EXPECTED['code']] if entry else [],
                                 resolved_path='' if entry else 'D:\\cache\\42.json',
                                 expected_title_visible=not entry, expected_stage_visible=not entry,
                                 missing_operators=[] if not entry else ['甲'],
                                 preview_signature='loaded' if not entry else 'empty')
                    if entry:
                        self.entry_id = name
                    if self.post_snapshots <= loading_conflicts and loading_mutation == 'identity':
                        frame['window']['pid'] = 999
                self.frames[str(frame['image_path'])] = copy.deepcopy(frame)
                self.latest = name
                self.captured_at = clock.value
                return {'status': 'completed', 'result': frame}
            def health(self):
                return {}
        supervisor = Supervisor()
        class Readback:
            requires_frame_refresh = True
            max_observe_seconds = 45
            import_readback_timeout = import_timeout
            calls = 0
            def observe(self, path, **kwargs):
                clock.value += delay
                self.calls += 1
                if self.calls <= invalid_count:
                    error = RuntimeError('Invalid model transcription')
                    error.code = 'vision_invalid_output'
                    raise error
                result = copy.deepcopy(supervisor.frames[str(path)])
                if loading_conflicts and (supervisor.typed and supervisor.post_snapshots <= loading_conflicts
                                          or loading_mutation == 'before_entry'):
                    result.update(ok=False, page='unknown', reason='vision_local_conflict',
                                  local_refinement_reason='page_or_running_state_disagrees')
                    if loading_mutation == 'image':
                        result['image_sha256'] = 'different-image'
                    elif loading_mutation in ('different_image', 'same_checkbox_pixels_disagree'):
                        result['local_refinement_reason'] = loading_mutation
                    elif loading_mutation == 'cancel':
                        cancel.set()
                return result
        def submit(args, frame, cancel):
            outer.assertLessEqual(clock.value - supervisor.captured_at, 10)
            supervisor.actions.append(args)
            supervisor.action_frames.append((args, supervisor.latest))
            if args['action'] == 'replace_code':
                supervisor.typed = True
            receipt = {}
            if args['action'] == 'focus':
                supervisor.focused = True
                supervisor.foreground = focus_acquired
                receipt = {'focus_acquired': focus_acquired, 'caption_click': focus_acquired}
                if focus_interrupt == 'cancel':
                    cancel.set()
            return {'status': 'completed', 'submission_entered': True, 'result': receipt}
        published = []
        def publish(kind, payload):
            published.append((kind, copy.deepcopy(payload)))
            return {'kind': kind, 'sequence': len(published)}
        importer = VisualCodeImporter(supervisor, readback=Readback(), submit=submit, publish=publish,
                                      load_monitor=load_monitor)
        with patch('shu.maa_visual_import.time.monotonic', side_effect=clock.monotonic):
            expected = {**EXPECTED, 'source_document': {'stage_name': 'fixture'}} if load_monitor else EXPECTED
            result = importer.run(expected, cancel_event=cancel)
        return result, supervisor, published

    def test_model_call_has_a_local_deadline_and_cancellation(self):
        frame = self.frame('source')
        stopped = threading.Event()
        class Readback:
            requires_frame_refresh = True
            max_observe_seconds = .05
            def observe(self, path, *, cancel_event=None, **kwargs):
                if cancel_event.wait(2):
                    stopped.set()
                return frame
        importer = VisualCodeImporter(RefreshSupervisor([frame]), readback=Readback())
        started = time.monotonic()
        with self.assertRaises(VisualImportError) as raised:
            importer.observe(EXPECTED)
        self.assertEqual(raised.exception.code, 'maa_visual_recognition_timeout')
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(stopped.wait(.5))

    def test_user_cancellation_reaches_inflight_model_and_returns_no_observation(self):
        frame = self.frame('source')
        entered, stopped, cancel = threading.Event(), threading.Event(), threading.Event()
        class Readback:
            requires_frame_refresh = True
            def observe(self, path, *, cancel_event=None, **kwargs):
                entered.set()
                if cancel_event.wait(2):
                    stopped.set()
                return frame
        def cancel_when_started():
            if entered.wait(1):
                cancel.set()
        watcher = threading.Thread(target=cancel_when_started, daemon=True)
        watcher.start()
        importer = VisualCodeImporter(RefreshSupervisor([frame]), readback=Readback())
        with self.assertRaises(VisualImportError) as raised:
            importer.observe(EXPECTED, cancel_event=cancel)
        self.assertEqual(raised.exception.code, 'maa_visual_cancelled')
        self.assertTrue(stopped.wait(.5))

    def run_option_settling_import(self, states, *, interrupt=None):
        outer, cancel = self, threading.Event()
        class Supervisor:
            target_identity = IDENTITY
            def __init__(self):
                self.frames, self.actions = {}, []
                self.directory = outer.root
                self.clicked, self.typed = False, False
                self.polls, self.sequence, self.loaded_frames = 0, 0, 0
            def health(self):
                return {}
            def request(self, command, **kwargs):
                outer.assertEqual(command, {'op': 'snapshot'})
                self.sequence += 1
                image = outer.image.copy()
                if self.typed:
                    image[70:75, 90:110] = 100
                frame = outer.frame('option-' + str(self.sequence), image)
                state = False
                if self.clicked:
                    state = states[min(self.polls, len(states) - 1)]
                    self.polls += 1
                frame['options'] = {'ignore_requirements': {'state': state}}
                frame['targets']['ignore_requirements'] = {
                    'rect': {'x': 70, 'y': 15, 'width': 12, 'height': 12},
                    'point': {'x': 76, 'y': 21}}
                if self.clicked and not self.typed:
                    if interrupt == 'window':
                        frame['window']['pid'] = 999
                    elif interrupt == 'permission':
                        frame['permission']['input_allowed'] = False
                    elif interrupt == 'page':
                        frame['page'] = 'settings'
                if self.typed:
                    self.loaded_frames += 1
                    entry = self.loaded_frames == 1
                    frame.update(field_empty=False, codes=[EXPECTED['code']] if entry else [],
                                 resolved_path='' if entry else 'D:\\cache\\42.json',
                                 expected_title_visible=not entry, expected_stage_visible=not entry,
                                 missing_operators=['甲'] if entry else [],
                                 preview_signature='empty' if entry else 'loaded')
                self.frames[frame['image_path']] = copy.deepcopy(frame)
                return {'status': 'completed', 'result': frame}
        supervisor = Supervisor()
        class Readback:
            requires_frame_refresh = False
            def observe(self, path, **kwargs):
                if supervisor.clicked and not supervisor.typed and interrupt == 'cancel':
                    cancel.set()
                return copy.deepcopy(supervisor.frames[str(path)])
        def submit(args, frame, event):
            supervisor.actions.append(args)
            if args['action'] == 'click' and args.get('target') == 'ignore_requirements':
                supervisor.clicked = True
            if args['action'] == 'replace_code':
                supervisor.typed = True
            return {'status': 'completed', 'submission_entered': True, 'result': {}}
        importer = VisualCodeImporter(supervisor, readback=Readback(), submit=submit)
        waits = []
        def wait(seconds):
            waits.append(seconds)
            return cancel.is_set()
        with patch.object(cancel, 'wait', side_effect=wait):
            result = importer.run(EXPECTED, {'ignore_requirements': True}, cancel_event=cancel)
        return result, supervisor, waits

    def test_option_click_waits_for_fresh_render_without_reclicking(self):
        result, supervisor, waits = self.run_option_settling_import([False, True])
        self.assertEqual(result['status'], 'import_verified', result)
        self.assertEqual(sum(item.get('target') == 'ignore_requirements'
                             for item in supervisor.actions), 1)
        self.assertEqual(sum(item['action'] == 'replace_code' for item in supervisor.actions), 1)
        self.assertEqual(waits[:2], [.2, .2])

    def test_option_unsettled_is_bounded_and_never_reclicked_or_typed(self):
        result, supervisor, _ = self.run_option_settling_import([False])
        self.assertEqual(result['code'], 'maa_visual_option_unverified', result)
        self.assertEqual(supervisor.polls, 3)
        self.assertEqual([item.get('target') for item in supervisor.actions], ['ignore_requirements'])

    def test_option_poll_cancellation_stops_before_following_input(self):
        result, supervisor, _ = self.run_option_settling_import([True], interrupt='cancel')
        self.assertEqual(result['code'], 'maa_visual_cancelled', result)
        self.assertEqual([item.get('target') for item in supervisor.actions], ['ignore_requirements'])

    def test_option_poll_rejects_replaced_window(self):
        result, supervisor, _ = self.run_option_settling_import([True], interrupt='window')
        self.assertEqual(result['code'], 'maa_visual_observation_invalid', result)
        self.assertEqual([item.get('target') for item in supervisor.actions], ['ignore_requirements'])

    def test_option_poll_rechecks_page_and_input_permission(self):
        for changed, expected_code in [('page', 'maa_visual_page'), ('permission', 'maa_visual_integrity')]:
            with self.subTest(changed=changed):
                result, supervisor, _ = self.run_option_settling_import([True], interrupt=changed)
                self.assertEqual(result['code'], expected_code, result)
                self.assertEqual([item.get('target') for item in supervisor.actions], ['ignore_requirements'])


class RecordedFocusRefreshTests(unittest.TestCase):
    """Replay the three .44 failure frames; never access a native window."""

    def setUp(self):
        evidence = (Path(__file__).resolve().parents[2] / 'q44' / 'artifacts'
                    / 'qa-compound-elevated' / 'data' / 'evidence')
        source_path = evidence / 'maa-visual-refresh-source-2e7fc63d26db3039bf5091a51f1f69adc1bcd0d3d17411568752777b28fc7b42.json'
        fresh_path = evidence / 'maa-visual-refresh-native-1e40e79d9a43dedc7668e51764f40130dec9b3833d3d2f31dcdde125b1cab31c.json'
        unfocused_path = evidence / 'maa-visual-refresh-native-022915d61e40e938e7fcae0138dee1977ff47dbab33e1b626d2e007bd7fa9e9a.json'
        if not all(path.is_file() for path in (source_path, fresh_path, unfocused_path)):
            self.skipTest('Recorded .44 focus-refresh evidence is not present in this checkout')
        self.source = json.loads(source_path.read_text(encoding='utf-8'))
        self.fresh = json.loads(fresh_path.read_text(encoding='utf-8'))
        self.unfocused = json.loads(unfocused_path.read_text(encoding='utf-8'))
        for frame in (self.source, self.fresh, self.unfocused):
            path = Path(frame['image_path'])
            self.assertTrue(path.is_file(), path)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), frame['image_sha256'])
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.published = []

    def importer(self, renewed_mutation=None, *, background_first=False, confirmation_mutation=None):
        # Only the simulated reobservation/confirmation captures are temporary
        # copies. All three original regression PNGs retain their actual hashes.
        captures = []
        for name in ('reobserved-native', 'independent-confirmation'):
            frame = copy.deepcopy(self.fresh)
            path = self.root / (name + '.png')
            path.write_bytes(Path(self.fresh['image_path']).read_bytes())
            frame.update(screenshot_id=name, image_path=str(path))
            captures.append(frame)
        if confirmation_mutation:
            confirmation_mutation(captures[-1])
        supervisor = RefreshSupervisor(([self.unfocused] if background_first else [])
                                       + [self.fresh, *captures])
        supervisor.target_identity = {key: self.source['window'][key]
                                      for key in ('hwnd', 'pid', 'creation_time')}
        outer = self
        class Readback:
            requires_frame_refresh = True
            calls = 0
            def observe(self, path, **kwargs):
                self.calls += 1
                outer.assertEqual(Path(path), Path(captures[0]['image_path']))
                renewed = {**copy.deepcopy(outer.source), **copy.deepcopy(captures[0])}
                if renewed_mutation:
                    renewed_mutation(renewed)
                return renewed
        def publish(kind, payload):
            self.published.append((kind, copy.deepcopy(payload)))
            return {'kind': kind, 'sequence': len(self.published)}
        importer = VisualCodeImporter(supervisor, readback=Readback(), publish=publish)
        return importer

    def change_pixel(self, frame, point):
        image = cv2.imdecode(np.frombuffer(Path(frame['image_path']).read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        x, y = point
        image[y, x] ^= 255
        path = self.root / (frame['screenshot_id'] + '-changed.png')
        path.write_bytes(cv2.imencode('.png', image)[1].tobytes())
        frame.update(image_path=str(path), image_sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    def test_recorded_focus_border_needs_reobservation_instead_of_start_change(self):
        comparison = _compare_frame_images(self.source, self.fresh, target='code_input')
        self.assertFalse(comparison['ok'])
        self.assertEqual(comparison['changed_pixels'], 3949)
        self.assertEqual(comparison['reason'], 'frame_pixels_changed', comparison)
        self.assertEqual(comparison['edit_outline_pixels_requiring_reobservation'], 173)

    def test_recorded_focus_outline_requires_matching_local_edit_geometry(self):
        mutations = [lambda frame: frame['targets']['code_input'].pop('kind'),
                     lambda frame: frame['targets']['code_input'].update(source='model_guess'),
                     lambda frame: frame['regions']['code_input'].update(width=382)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                source = copy.deepcopy(self.source)
                mutate(source)
                comparison = _compare_frame_images(source, self.fresh, target='code_input')
                self.assertFalse(comparison['ok'])
                self.assertEqual(comparison['reason'], 'forbidden_region_changed')

    def test_recorded_focus_border_uses_renewed_semantics_and_independent_pixels(self):
        for target in (None, 'code_input'):
            with self.subTest(target=target):
                importer = self.importer()
                refreshed = importer._refresh_for_action(self.source, target=target, expected={'code': 'prts://109538'})
                self.assertEqual(refreshed['screenshot_id'], 'independent-confirmation')
                self.assertEqual(importer.readback.calls, 1)
                self.assertEqual(importer.supervisor.commands, [{'op': 'snapshot'}] * 3)
                self.assertTrue(any(kind == 'maa-visual-refresh-reobservation' for kind, _ in self.published))

    def test_recorded_unfocused_frame_requires_focus_then_new_interpretation(self):
        importer = self.importer(background_first=True)
        focused = []
        refreshed = importer._refresh_for_action(
            self.source, expected={'code': 'prts://109538'},
            reacquire_focus=lambda frame: focused.append(frame['screenshot_id']))
        self.assertEqual(focused, ['127d799b82a248b48459eb9a6ace6f4f'])
        self.assertEqual(refreshed['screenshot_id'], 'independent-confirmation')
        self.assertEqual(importer.readback.calls, 1)
        self.assertEqual(importer.supervisor.commands, [{'op': 'snapshot'}] * 4)

    def test_recorded_outline_alone_does_not_grant_input_authority(self):
        # Isolate exactly one pixel of the measured edit outline in Start's
        # halo. Dropping it as "cosmetic" would incorrectly authorize input.
        fresh = copy.deepcopy(self.source)
        fresh['screenshot_id'] = 'outline-only'
        self.change_pixel(fresh, (220, 183))
        comparison = _compare_frame_images(self.source, fresh, target='code_input')
        self.assertFalse(comparison['ok'], comparison)
        self.assertEqual(comparison['changed_pixels'], 1)
        self.assertEqual(comparison['reason'], 'frame_pixels_changed')
        self.assertEqual(comparison['edit_outline_pixels_requiring_reobservation'], 1)
        importer = self.importer()
        importer.supervisor.frames[0] = fresh
        with self.assertRaises(VisualImportError) as raised:
            importer._refresh_for_action(self.source, target='code_input')
        self.assertEqual(raised.exception.code, 'maa_visual_refresh_changed')
        self.assertEqual(importer.readback.calls, 0)
        self.assertEqual(importer.supervisor.commands, [{'op': 'snapshot'}])

    def test_recorded_reobservation_does_not_retry_a_second_changed_frame(self):
        importer = self.importer(confirmation_mutation=lambda frame: self.change_pixel(frame, (34, 183)))
        with self.assertRaises(VisualImportError) as raised:
            importer._refresh_for_action(self.source, expected={'code': 'prts://109538'})
        self.assertEqual(raised.exception.code, 'maa_visual_refresh_changed')
        self.assertEqual(importer.readback.calls, 1)
        self.assertEqual(importer.supervisor.commands, [{'op': 'snapshot'}] * 3)

    def test_recorded_focus_reobservation_cannot_hide_changed_preview(self):
        importer = self.importer(lambda frame: frame.update(preview_signature='another loaded job'))
        with self.assertRaises(VisualImportError):
            importer._refresh_for_action(self.source, target='code_input', expected={'code': 'prts://109538'})
        self.assertEqual(importer.readback.calls, 1)
        self.assertEqual(importer.supervisor.commands, [{'op': 'snapshot'}] * 2)

    def test_recorded_focus_border_does_not_exempt_start_or_other_controls(self):
        for name, point, reason in [('start', (220, 200), 'forbidden_region_changed'),
                                    ('start_halo_not_outline', (220, 182), 'forbidden_region_changed'),
                                    ('auto_formation', (165, 280), 'target_region_changed')]:
            with self.subTest(control=name):
                frame = copy.deepcopy(self.fresh)
                self.change_pixel(frame, point)
                comparison = _compare_frame_images(self.source, frame, target='code_input')
                self.assertFalse(comparison['ok'])
                self.assertEqual(comparison['reason'], reason, comparison)


if __name__ == '__main__':
    unittest.main()

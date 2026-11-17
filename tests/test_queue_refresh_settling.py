"""A settling input needs renewed recognition and a second independent frame."""
import copy
import threading
import unittest
from unittest.mock import Mock, patch

from shu.maa_queue_import import QueueCodeImporter, QueueImportBlocked


class QueueRefreshSettlingTests(unittest.TestCase):
    def setup_importer(self, changed=None):
        frame = {'window':{'foreground':True},'page':'copilot','idle':True,'running':False,
                 'queue_mode':True,'field_empty':True,'code_text':'','codes':[],
                 'targets':{'code_input':{'point':{'x':20,'y':30}}},'regions':{},
                 'options':{},'forbidden_regions':[],
                 'queue_rows':[{'key':'old-frame-key','name':'A-1','is_checked':True}],
                 'visible_complete':True}
        snapshot = {'projection_sha256':'same','rows':[],'evidence_ref':{},'screenshot_refs':[],
                    'visible_complete':True}
        renewed = copy.deepcopy(frame)
        renewed['queue_rows'][0]['key'] = 'renewed-frame-key'
        if changed:
            renewed.update(changed)
        importer = object.__new__(QueueCodeImporter)
        importer._native_frame = Mock(return_value={'window':{'foreground':True}})
        importer._pin_frame = Mock(return_value={'path':'fresh','sha256':'a'*64})
        importer._observe = Mock(return_value=(renewed,copy.deepcopy(snapshot)))
        importer.publish = Mock(return_value={})
        importer.store = Mock()
        importer.monitor = Mock()
        importer.monitor.capture_queue_snapshot.return_value = copy.deepcopy(snapshot)
        importer._snapshot_refs = Mock(return_value=[])
        return importer,frame,snapshot

    def test_one_renewed_recognition_must_pass_further_pixel_check(self):
        importer,frame,snapshot = self.setup_importer()
        with patch('shu.maa_queue_import._compare_frame_images',side_effect=[
                {'ok':False,'reason':'frame_pixels_changed'}, {'ok':True}]):
            importer._refresh({},frame,snapshot,{},None,threading.Event(),target='code_input')
        self.assertEqual(importer._observe.call_count,1)
        self.assertEqual(importer._native_frame.call_count,2)

    def test_row_animation_needs_same_renewed_semantics_and_stable_native_pixels(self):
        importer,frame,snapshot = self.setup_importer()
        with patch('shu.maa_queue_import._compare_frame_images',side_effect=[
                {'ok':False,'reason':'target_region_changed'}, {'ok':True}]):
            importer._refresh({},frame,snapshot,{},None,threading.Event(),target='code_input')
        self.assertEqual(importer._observe.call_count,1)
        self.assertEqual(importer._native_frame.call_count,2)

    def test_changed_controls_or_persistent_pixels_are_rejected(self):
        for changed in ({'code_text':'other'}, {'forbidden_regions':[{'x':1}]},
                        {'queue_rows':[{'name':'other'}]}, {'targets':{}}, {}):
            with self.subTest(changed=changed):
                importer,frame,snapshot = self.setup_importer(changed)
                with patch('shu.maa_queue_import._compare_frame_images',return_value={
                        'ok':False,'reason':'frame_pixels_changed'}):
                    with self.assertRaises(QueueImportBlocked):
                        importer._refresh({},frame,snapshot,{},None,threading.Event(),target='code_input')
                self.assertEqual(importer._observe.call_count,1)
                self.assertLessEqual(importer._native_frame.call_count,2)

    def test_viewport_shift_keeps_covering_frames_and_drops_stale_row_coordinates(self):
        importer,frame,snapshot = self.setup_importer()
        frame['queue_scan_ref'] = {'path':'scan','sha256':'c'*64}
        frame['targets']['row_delete:row'] = {'point':{'x':45,'y':50}}
        snapshot['screenshot_refs'] = [{'path':path,'sha256':'b'*64} for path in ('scan-top','scan-bottom')]
        original_refs = copy.deepcopy(snapshot['screenshot_refs'])
        with patch('shu.maa_queue_import._compare_frame_images', return_value={'ok':True,'ignored_queue_pixels':99}):
            refreshed,_ = importer._refresh({},frame,snapshot,{},None,threading.Event(),target='code_input')
        kwargs = importer.monitor.capture_queue_snapshot.call_args.kwargs
        self.assertEqual(kwargs['screenshot_refs'], [*original_refs, importer._pin_frame.return_value])
        self.assertTrue(kwargs['visible_complete'])
        self.assertIn('code_input', refreshed['targets'])
        self.assertNotIn('row_delete:row', refreshed['targets'])

    def test_viewport_shift_blocks_input_if_fresh_queue_projection_or_body_changed(self):
        for changed in ({'projection_sha256':'new'}, {'rows':[{'body_sha256':'new'}]}):
            with self.subTest(changed=changed):
                importer,frame,snapshot = self.setup_importer()
                importer.monitor.capture_queue_snapshot.return_value.update(changed)
                importer.submit = Mock()
                with patch('shu.maa_queue_import._compare_frame_images', return_value={'ok':True,'ignored_queue_pixels':99}):
                    with self.assertRaises(QueueImportBlocked) as failure:
                        importer._focus_field({},frame,snapshot,{},None,threading.Event())
                self.assertEqual(failure.exception.code, 'queue_projection_changed')
                importer.store.prepare_item_action.assert_not_called()
                importer.submit.assert_not_called()

    def test_post_input_scrollbar_refresh_failures_retry_within_one_deadline(self):
        importer,_,_ = self.setup_importer()
        importer.store.workflow.get_operation.return_value = {'submitted_at':'2026-11-11T00:00:00+00:00'}
        importer._observe = Mock(side_effect=QueueImportBlocked('queue_visual_refresh_changed'))
        importer.submit = Mock()
        cancel = Mock()
        cancel.wait.return_value = False
        with patch('shu.maa_queue_import.time.monotonic', side_effect=[10, 11, 189, 190]):
            with self.assertRaises(QueueImportBlocked) as failure:
                importer._wait_effect({}, {'operation_id':'code-op'}, {}, {}, None, cancel)
        self.assertEqual(failure.exception.code, 'queue_parse_completion_unverified')
        self.assertEqual([call.kwargs['deadline'] for call in importer._observe.call_args_list], [190, 190])
        importer.submit.assert_not_called()
        importer.store.prepare_item_action.assert_not_called()
        importer.store.record_reconciliation.assert_not_called()

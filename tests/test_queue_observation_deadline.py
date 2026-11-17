"""A long-list scan shares the original input's absolute deadline."""
import threading
import unittest
from unittest.mock import Mock, patch

from shu.maa_queue_import import QueueCodeImporter, QueueImportBlocked


class QueueObservationDeadlineTests(unittest.TestCase):
    def test_pixel_settling_reads_until_two_consecutive_stable_pairs_without_input(self):
        importer = object.__new__(QueueCodeImporter)
        importer._native_frame = Mock(side_effect=[{'screenshot_id':str(i)} for i in range(5)])
        importer._pin_frame = Mock()
        importer.publish = Mock(return_value={'path':'fixture','sha256':'a'*64})
        importer.store = Mock()
        importer.submit = Mock()
        cancel = Mock()
        cancel.wait.return_value = False
        checks=[{'ok':True},{'ok':False,'reason':'frame_pixels_changed'},{'ok':True},{'ok':True}]
        with patch('shu.maa_queue_import.time.monotonic',return_value=100),patch(
                'shu.maa_queue_import._compare_frame_images',side_effect=checks):
            importer._wait_scroll_stable({}, {}, None, cancel, deadline=103)
        self.assertEqual(importer._native_frame.call_count,5)
        self.assertEqual(importer._pin_frame.call_count,5)
        importer.submit.assert_not_called()

    def test_pixel_settling_retains_absolute_deadline_and_rejects_owner_change(self):
        importer = object.__new__(QueueCodeImporter)
        importer._native_frame = Mock(return_value={})
        importer._pin_frame = Mock()
        importer.publish = Mock(return_value={})
        importer.store = Mock()
        cancel = Mock()
        cancel.wait.return_value = False
        with patch('shu.maa_queue_import.time.monotonic',side_effect=[100,104]):
            with self.assertRaisesRegex(QueueImportBlocked,'queue_observation_timeout'):
                importer._wait_scroll_stable({}, {}, None, cancel, deadline=103)
        with patch('shu.maa_queue_import.time.monotonic',return_value=100),patch(
                'shu.maa_queue_import._compare_frame_images',return_value={
                    'ok':False,'reason':'native_identity_geometry_or_permission_changed'}):
            with self.assertRaisesRegex(QueueImportBlocked,'queue_visual_refresh_changed'):
                importer._wait_scroll_stable({}, {}, None, cancel, deadline=103)

    def test_expired_deadline_stops_before_first_capture(self):
        importer = object.__new__(QueueCodeImporter)
        importer._observe_one = Mock()
        with patch('shu.maa_queue_import.time.monotonic', return_value=100):
            with self.assertRaisesRegex(QueueImportBlocked,'queue_observation_timeout'):
                importer._observe({}, {}, None, threading.Event(), deadline=99)
        importer._observe_one.assert_not_called()

    def test_long_scan_receives_same_deadline_and_remaining_model_budget(self):
        importer = object.__new__(QueueCodeImporter)
        frame = {'queue_mode':True,'window':{'foreground':True},
                 'permission':{'input_allowed':True}}
        snapshot = {'visible_complete':False}
        importer._observe_one = Mock(return_value=(frame,snapshot))
        importer._scan_queue = Mock(return_value=('full','snapshot'))
        cancel = threading.Event()
        with patch('shu.maa_queue_import.time.monotonic', return_value=100):
            self.assertEqual(importer._observe({}, {}, None, cancel, deadline=103),('full','snapshot'))
        self.assertEqual(importer._observe_one.call_args.kwargs['recognition_timeout'],3)
        self.assertEqual(importer._scan_queue.call_args.kwargs['deadline'],103)

    def test_expired_scan_cannot_submit_a_scroll(self):
        importer = object.__new__(QueueCodeImporter)
        importer.submit = Mock()
        with patch('shu.maa_queue_import.time.monotonic', return_value=100):
            with self.assertRaisesRegex(QueueImportBlocked,'queue_observation_timeout'):
                importer._scroll({}, {}, {}, {}, None, threading.Event(),1,deadline=99)
        importer.submit.assert_not_called()

    def test_unreadable_post_scroll_frame_is_recaptured_without_a_second_scroll(self):
        importer = object.__new__(QueueCodeImporter)
        frame = {'screenshot_id':'before','queue_mode':True,'window':{'foreground':True}}
        snapshot = {'projection_sha256':'a'*64}
        importer._require_page = Mock()
        importer._refresh = Mock(return_value=(frame,snapshot))
        importer._point = Mock(return_value=(30,60))
        importer._control_response = Mock()
        importer.submit = Mock()
        importer.store = Mock()
        importer.progress = Mock()
        importer._observe_one = Mock(side_effect=[QueueImportBlocked('queue_vision_unverified'),
                                                  ('freshly_read_view',snapshot)])
        with patch('shu.maa_queue_import.time.monotonic',side_effect=[100,101,102,104]):
            result = importer._scroll({},frame,snapshot,{},None,threading.Event(),-1,deadline=110)
        self.assertEqual(result,('freshly_read_view',snapshot))
        importer.submit.assert_called_once()
        self.assertEqual([call.kwargs['recognition_timeout'] for call in importer._observe_one.call_args_list],[8,6])

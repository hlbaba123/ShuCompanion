"""The diagnostic observes the product's capture route without admitting input."""
import importlib.util
import hashlib
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib


SPEC = importlib.util.spec_from_file_location('queue_probe',
    Path(__file__).resolve().parents[1]/'tools'/'maa_queue_probe.py')


class QueueProbeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.output = self.root/'artifacts'/'probe'
        self.module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(self.module)

    def test_no_window_finishes_without_profile_key_model_or_helper(self):
        with patch.object(self.module,'discover_maa_windows',return_value=[]), \
             patch.object(self.module,'GuiSupervisor') as supervisor, \
             patch.object(self.module,'_vision_observer') as vision:
            result = self.module.run_probe(self.output,project_root=self.root,with_vision=True)
        self.assertEqual(result['code'],'maa_window_unavailable')
        self.assertEqual(result['gui_inputs'],0)
        self.assertEqual(result['model_requests'],0)
        supervisor.assert_not_called()
        vision.assert_not_called()
        self.assertTrue((self.output/'report.json').is_file())

    def test_minimized_and_ambiguous_windows_never_restore_or_start_model(self):
        for windows,code in (([{'minimized':True}],'maa_window_minimized'),
                             ([{},{}],'maa_window_ambiguous')):
            with self.subTest(code=code):
                output = self.output/code
                with patch.object(self.module,'discover_maa_windows',return_value=windows), \
                     patch.object(self.module,'GuiSupervisor') as supervisor, \
                     patch.object(self.module,'_vision_observer') as vision:
                    result = self.module.run_probe(output,project_root=self.root,with_vision=True)
                self.assertEqual(result['code'],code)
                supervisor.assert_not_called()
                vision.assert_not_called()

    def test_failed_capture_closes_helper_and_never_initializes_key(self):
        window = {'hwnd':1,'pid':2,'creation_time':3,'executable':'MAA.exe','minimized':False}
        helper = Mock()
        helper.request.return_value = {'status':'not_issued','code':'permission_mismatch'}
        with patch.object(self.module,'discover_maa_windows',return_value=[window]), \
             patch.object(self.module,'GuiSupervisor',return_value=helper), \
             patch.object(self.module,'discover_queue_binding',return_value={'fixed':'binding'}), \
             patch.object(self.module,'_vision_observer') as vision:
            result = self.module.run_probe(self.output,project_root=self.root,with_vision=True)
        self.assertEqual(result['code'],'permission_mismatch')
        helper.request.assert_called_once_with({'op':'snapshot'},timeout=12)
        helper.close.assert_called_once_with()
        helper.request_stop.assert_not_called()
        vision.assert_not_called()

    def test_existing_output_is_not_overwritten(self):
        self.output.mkdir(parents=True)
        sentinel = self.output/'report.json'
        sentinel.write_text('keep',encoding='utf-8')
        with self.assertRaises(FileExistsError):
            self.module.run_probe(self.output,project_root=self.root)
        self.assertEqual(sentinel.read_text(encoding='utf-8'),'keep')

    def test_output_must_be_inside_project_artifacts(self):
        with self.assertRaises(ValueError):
            self.module.run_probe(self.root/'user-data',project_root=self.root)
        self.assertFalse((self.root/'user-data').exists())

    def captured_helper(self):
        window = {'hwnd':1,'pid':2,'creation_time':3,'executable':'MAA.exe','minimized':False}
        helper = Mock()

        def capture(command, **kwargs):
            self.assertEqual(command, {'op':'snapshot'})
            directory = Path(helper.directory)/'maa-visual-captures'
            directory.mkdir()
            identifier = 'a'*32
            image = directory/(identifier+'.png')

            def chunk(kind,body):
                return struct.pack('>I',len(body))+kind+body+struct.pack('>I',zlib.crc32(kind+body))

            raw = (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',8,8,8,2,0,0,0))
                   +chunk(b'IDAT',zlib.compress((b'\0'+b'\xff'*24)*8))+chunk(b'IEND',b''))
            image.write_bytes(raw)
            frame = {'screenshot_id':identifier,'image_path':str(image),
                'image_sha256':hashlib.sha256(raw).hexdigest(),
                'window':{**window,'client_rect':{'width':8,'height':8}},'method':'win32_printwindow'}
            helper.frame = frame
            return {'status':'completed','result':frame}

        helper.request.side_effect = capture

        def factory(identity, **kwargs):
            helper.directory = kwargs['directory']
            return helper

        return window,helper,factory

    def test_capture_only_publishes_readonly_evidence_without_profile_or_model(self):
        window,helper,factory = self.captured_helper()
        snapshot = {'evidence_ref':{'path':'unit snapshot','sha256':'a'*64},
                    'projection_sha256':'p','rows':[], 'visible_complete':False}
        with patch.object(self.module,'discover_maa_windows',return_value=[window]), \
             patch.object(self.module,'GuiSupervisor',side_effect=factory), \
             patch.object(self.module,'discover_queue_binding',return_value={'fixed':'binding'}), \
             patch.object(self.module.receipt,'capture_queue_snapshot',return_value=snapshot) as capture, \
             patch.object(self.module,'_vision_observer') as vision:
            result = self.module.run_probe(self.output,project_root=self.root)
        self.assertTrue(result['passed'])
        self.assertFalse(result['queue_verified'])
        self.assertEqual(result['code'],'queue_snapshot_captured')
        self.assertEqual(capture.call_args.kwargs['visible_complete'],False)
        vision.assert_not_called()
        helper.request.assert_called_once_with({'op':'snapshot'},timeout=12)
        helper.close.assert_called_once_with()
        helper.request_stop.assert_not_called()

    def test_vision_matches_snapshot_and_obeys_snapshot_only_helper(self):
        window,helper,factory = self.captured_helper()
        snapshot = {'evidence_ref':{'path':'unit snapshot','sha256':'a'*64},
                    'projection_sha256':'p','rows':[], 'visible_complete':False}
        after = {**snapshot,'visible_complete':True}

        def observer_factory(project_root, measured):
            def observe(image, **kwargs):
                self.assertEqual(kwargs['expected'],{'projection_sha256':'p','rows':[]})
                measured({'input_images':1})
                return {'schema':'maa_queue_vision/v1','ok':True,'image_sha256':helper.frame['image_sha256'],
                        'width':8,'height':8,'visible_complete':True,'queue_mode':True,'page':'copilot'}
            return SimpleNamespace(observe=observe,requires_frame_refresh=True,max_observe_seconds=45)

        with patch.object(self.module,'discover_maa_windows',return_value=[window]), \
             patch.object(self.module,'GuiSupervisor',side_effect=factory), \
             patch.object(self.module,'discover_queue_binding',return_value={'fixed':'binding'}), \
             patch.object(self.module.receipt,'capture_queue_snapshot',side_effect=[snapshot,after]), \
             patch.object(self.module,'_vision_observer',side_effect=observer_factory):
            result = self.module.run_probe(self.output,project_root=self.root,with_vision=True)
        self.assertTrue(result['passed'])
        self.assertTrue(result['queue_verified'])
        self.assertEqual(result['model_requests'],1)
        self.assertEqual(helper.request.call_count,1)
        helper.request_stop.assert_not_called()

    def test_exception_and_error_payload_do_not_publish_sensitive_values(self):
        with patch.object(self.module,'discover_maa_windows',side_effect=RuntimeError('sk-do-not-publish')):
            result = self.module.run_probe(self.output,project_root=self.root)
        self.assertEqual(result['code'],'queue_probe_failed')
        self.assertNotIn('sk-do-not-publish',(self.output/'report.json').read_text(encoding='utf-8'))


if __name__=='__main__':
    unittest.main()

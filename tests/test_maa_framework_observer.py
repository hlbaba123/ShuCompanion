from contextlib import contextmanager
import math
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from shu.maa_framework_observer import MaaFrameworkOcr, MaaFrameworkOcrError, project_words, LocalFirstReadback


@contextmanager
def fake_native_observer(*, stuck=None, stop_error=False):
    """Exercise native job lifetime without loading a DLL or using a desktop."""
    complete = SimpleNamespace(done=True, succeeded=True)
    pending = SimpleNamespace(done=False, succeeded=False)
    resource = Mock()
    resource.post_bundle.return_value = pending if stuck == 'resource' else complete
    resource_factory = Mock(return_value=resource)
    connection = pending if stuck == 'connection' else complete
    controllers = []

    class Controller:
        def __init__(self):
            controllers.append(self)

        def post_connection(self):
            return connection

    tasker = Mock()
    tasker.bind.return_value = True
    tasker.post_recognition.return_value = pending
    if stop_error:
        tasker.post_stop.side_effect = RuntimeError('stop request failed')
    tasker_factory = Mock(return_value=tasker)
    image = SimpleNamespace(shape=(20, 30, 3))
    image.copy = lambda: image

    def module(name, **attributes):
        value = ModuleType(name)
        value.__dict__.update(attributes)
        return value

    modules = {
        'maa': module('maa', __path__=[]),
        'maa.library': module('maa.library', Library=SimpleNamespace(version=lambda: 'v5.15.0')),
        'maa.resource': module('maa.resource', Resource=resource_factory),
        'maa.toolkit': module('maa.toolkit', Toolkit=SimpleNamespace(init_option=Mock())),
        'maa.controller': module('maa.controller', CustomController=Controller),
        'maa.pipeline': module('maa.pipeline', JOCR=lambda **kwargs: kwargs,
                               JRecognitionType=SimpleNamespace(OCR='OCR')),
        'maa.tasker': module('maa.tasker', Tasker=tasker_factory),
        'cv2': module('cv2', IMREAD_COLOR=1, imdecode=lambda *args: image),
        'numpy': module('numpy', uint8=object(), frombuffer=lambda *args, **kwargs: object()),
    }
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root/'runtime'/'maa').mkdir(parents=True)
        (root/'resource').mkdir()
        image_path = root/'fixed.png'
        image_path.write_bytes(b'fixed image decoded by fake cv2')
        # Leave enough scheduling margin for unrelated build/GUI activity;
        # the selected pending native job still always hits the real deadline.
        observer = MaaFrameworkOcr(root/'runtime', root/'resource', root/'logs', timeout=.5)
        local, fallback = Mock(), Mock()
        def observe(path, *, expected=None, cancel_event=None):
            return observer.recognize(path, cancel_event=cancel_event)
        local.observe.side_effect = observe
        fallback.observe.return_value = {'ok': False, 'page': 'unknown'}
        with patch.dict(sys.modules, modules), patch.object(sys, 'path', list(sys.path)), \
                patch('shu.maa_framework_observer._NATIVE_QUARANTINE', []):
            yield SimpleNamespace(observer=observer, reader=LocalFirstReadback(local, fallback),
                                  path=image_path, resource=resource, job=pending,
                                  resource_factory=resource_factory, tasker=tasker,
                                  tasker_factory=tasker_factory, controllers=controllers,
                                  fallback=fallback)


class FrameworkWordsTests(unittest.TestCase):
    def test_loaded_preview_without_real_roster_count_uses_model_fallback(self):
        # Actual 0.3.33 mainline frame: title/name passed local OCR, but the
        # real count scored .843720 and was omitted at the .85 threshold.
        local, fallback = Mock(), Mock()
        frame = {'ok':True, 'page':'copilot', 'field_empty':False,
                 'codes':['prts://63012'], 'expected_title_visible':True,
                 'expected_stage_visible':True, 'missing_operators':[],
                 'operator_count':None,
                 'targets':{'code_input':{},'paste_button':{}},
                 'options':{key:{'state':False} for key in
                            ('auto_formation','ignore_requirements','use_support','multiple_jobs')}}
        local.observe.return_value=frame
        fallback.observe.return_value={'ok':True, 'operator_count':1}
        expected={'code':'prts://63012','source_document':{'opers':[{'name':'Logos'}],'groups':[]}}
        reader=LocalFirstReadback(local,fallback)
        result=reader.observe('same.png',expected=expected)
        self.assertEqual(result['operator_count'],1)
        self.assertIsNone(frame['operator_count'])
        self.assertEqual(result['local_fallback_reason'],'local_evidence_incomplete')
        fallback.observe.assert_called_once_with('same.png',expected=expected,cancel_event=None)

    def test_loaded_preview_count_must_match_source_operators_and_groups(self):
        local, fallback = Mock(), Mock()
        frame={'ok':True,'page':'copilot','field_empty':False,
               'codes':['prts://9'],'expected_title_visible':True,
               'expected_stage_visible':True,'missing_operators':[],
               'targets':{'code_input':{},'paste_button':{}},
               'options':{key:{'state':False} for key in
                          ('auto_formation','ignore_requirements','use_support','multiple_jobs')}}
        expected={'code':'prts://9','source_document':{'opers':[{'name':'A'}],'groups':[{'name':'B'}]}}
        reader=LocalFirstReadback(local,fallback)
        for count in (None,True,0,1,3,'2'):
            with self.subTest(count=count):
                local.observe.return_value={**frame,'operator_count':count}
                fallback.observe.reset_mock()
                fallback.observe.return_value={'ok':False,'page':'unknown'}
                reader.observe('same.png',expected=expected)
                fallback.observe.assert_called_once()
        local.observe.return_value={**frame,'operator_count':2}
        fallback.observe.reset_mock()
        result=reader.observe('same.png',expected=expected)
        self.assertEqual(result['operator_count'],2)
        self.assertEqual(result['recognition_engine'],'maafw_ocr+local_geometry')
        fallback.observe.assert_not_called()

    def test_local_unknown_uses_same_image_fallback_and_cancel_does_not(self):
        local=Mock()
        fallback=Mock()
        fallback.observe.return_value={'ok':False,'page':'unknown'}
        local.observe.return_value={'ok':True,'page':'unknown'}
        reader=LocalFirstReadback(local,fallback)
        result=reader.observe('same.png',expected={'code':'prts://1'})
        self.assertEqual(result['local_fallback_reason'],'local_evidence_incomplete')
        fallback.observe.assert_called_once_with('same.png',expected={'code':'prts://1'},cancel_event=None)
        fallback.reset_mock()
        local.observe.side_effect=MaaFrameworkOcrError('framework_cancelled')
        with self.assertRaises(MaaFrameworkOcrError):
            reader.observe('same.png')
        fallback.observe.assert_not_called()

    def test_native_empty_zero_confidence_detection_is_not_a_word(self):
        result=project_words({'all':[{'box':[0,0,8,8],'text':'','score':0},
                                    {'box':[2,3,7,8],'text':'自动战斗','score':.99}]},100,100,.85)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['rect'],{'x':2,'y':3,'width':7,'height':8})

    def test_complete_local_frame_avoids_model_and_native_timeout_falls_back(self):
        local, fallback = Mock(), Mock()
        frame = {'ok':True, 'page':'copilot','field_empty':True,
                 'targets':{'code_input':{},'paste_button':{}},
                 'options':{key:{'state':False} for key in
                            ('auto_formation','ignore_requirements','use_support','multiple_jobs')}}
        local.observe.return_value=frame
        reader=LocalFirstReadback(local,fallback)
        self.assertEqual(reader.observe('same.png')['recognition_engine'],'maafw_ocr+local_geometry')
        fallback.observe.assert_not_called()
        local.observe.side_effect=MaaFrameworkOcrError('framework_timeout')
        fallback.observe.return_value={'ok':False}
        self.assertEqual(reader.observe('same.png')['local_fallback_reason'],'framework_timeout')
        fallback.observe.assert_called_once()

    def test_invalid_confident_geometry_and_metadata_remain_unknown(self):
        for row in ({'text':'x','score':.99,'box':[95,0,10,1]},
                    {'text':'x','score':float('nan'),'box':[0,0,10,1]},
                    {'text':'x','score':1.1,'box':[0,0,10,1]},
                    {'text':'','score':.99,'box':[0,0,10,1]}):
            with self.subTest(row=row), self.assertRaises(MaaFrameworkOcrError):
                project_words({'all':[row]},100,100,.85)

    def test_cancelled_job_does_not_load_native_runtime(self):
        cancel=threading.Event()
        cancel.set()
        ocr=MaaFrameworkOcr(Path('missing'),Path('missing'),Path('missing'))
        with self.assertRaises(MaaFrameworkOcrError) as raised:
            ocr.recognize('missing',cancel_event=cancel)
        self.assertEqual(raised.exception.code,'framework_cancelled')
        self.assertIsNone(ocr._resource)

    def test_native_constructor_failure_becomes_fallback_eligible(self):
        ocr=MaaFrameworkOcr(Path('missing'),Path('missing'),Path('missing'))
        ocr._load=Mock(side_effect=RuntimeError('native allocation failed'))
        with self.assertRaises(MaaFrameworkOcrError) as raised:
            ocr.recognize('unused.png')
        self.assertEqual(raised.exception.code,'framework_job_failed')
        self.assertTrue(ocr._lock.acquire(blocking=False))
        ocr._lock.release()

    def assert_quarantined_timeout(self, fixture):
        started = time.monotonic()
        result = fixture.reader.observe(fixture.path)
        self.assertLess(time.monotonic()-started, 1)
        self.assertEqual(result['local_fallback_reason'], 'framework_timeout')
        retained = fixture.observer._quarantined
        self.assertTrue(any(item is fixture.resource for item in retained))
        self.assertTrue(any(item is fixture.job for item in retained))
        self.assertTrue(fixture.observer._lock.acquire(blocking=False))
        fixture.observer._lock.release()
        repeated = fixture.reader.observe(fixture.path)
        self.assertEqual(repeated['local_fallback_reason'], 'framework_unavailable')
        self.assertIs(fixture.observer._quarantined, retained)
        fixture.resource_factory.assert_called_once()
        self.assertEqual(fixture.fallback.observe.call_count, 2)
        for call in fixture.fallback.observe.call_args_list:
            self.assertEqual(call.args, (fixture.path,))

    def test_pending_resource_initialization_is_retained_and_not_retried(self):
        with fake_native_observer(stuck='resource') as fixture:
            self.assert_quarantined_timeout(fixture)
            fixture.tasker_factory.assert_not_called()
            self.assertEqual(fixture.controllers, [])

    def test_pending_connection_is_retained_and_not_retried(self):
        with fake_native_observer(stuck='connection') as fixture:
            self.assert_quarantined_timeout(fixture)
            self.assertEqual(len(fixture.controllers), 1)
            self.assertTrue(any(item is fixture.controllers[0]
                                for item in fixture.observer._quarantined))
            fixture.tasker_factory.assert_not_called()

    def test_quarantine_outlives_observer_and_new_instances_cannot_load_sdk(self):
        from shu import maa_framework_observer as backend
        with fake_native_observer(stuck='resource') as fixture:
            self.assert_quarantined_timeout(fixture)
            retained = fixture.observer._quarantined
            self.assertTrue(any(item is retained for item in backend._NATIVE_QUARANTINE))
            other=MaaFrameworkOcr(fixture.observer.runtime,fixture.observer.resource_path,
                                  fixture.observer.log_directory,timeout=.03)
            with self.assertRaises(MaaFrameworkOcrError) as error:
                other.recognize(fixture.path)
            self.assertEqual(error.exception.code,'framework_unavailable')
            fixture.resource_factory.assert_called_once()

    def test_failing_stop_preserves_timeout_and_keeps_native_objects_alive(self):
        with fake_native_observer(stuck='recognition', stop_error=True) as fixture:
            self.assert_quarantined_timeout(fixture)
            fixture.tasker_factory.assert_called_once()
            fixture.tasker.post_stop.assert_called_once()
            self.assertTrue(any(item is fixture.tasker
                                for item in fixture.observer._quarantined))


if __name__=='__main__':
    unittest.main()

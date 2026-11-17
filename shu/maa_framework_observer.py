"""Optional, read-only MaaFramework OCR for the existing visual interpreter."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import sys
import threading
import time

from .local_ocr import MAX_DIMENSION, MAX_IMAGE_BYTES, MAX_WORDS

# MaaFramework destructors synchronously destroy native handles. Keep failed
# unfinished jobs alive for this process and stop admitting new native OCR
# after the first such failure. All observations then use the existing fallback.
_NATIVE_QUARANTINE = []
_NATIVE_QUARANTINE_LOCK = threading.Lock()


class MaaFrameworkOcrError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fixed_controller(controller_type, image):
    class FixedImageController(controller_type):
        def __init__(self):
            self.input_attempts = 0
            super().__init__()

        def connect(self):
            return True

        def request_uuid(self):
            return 'shu-readonly-fixed-image'

        def screencap(self):
            return image.copy()

        def reject_input(self, *args):
            self.input_attempts += 1
            return False

        start_app = stop_app = click = swipe = reject_input
        touch_down = touch_move = touch_up = reject_input
        click_key = input_text = key_down = key_up = reject_input
        scroll = relative_move = shell = reject_input
    return FixedImageController()


def project_words(detail, width, height, confidence):
    """Discard no geometry silently: invalid native output stops this backend."""
    rows = detail.get('all') if isinstance(detail, dict) else None
    if not isinstance(rows, list) or len(rows) > MAX_WORDS:
        raise MaaFrameworkOcrError('framework_output_invalid')
    words = []
    for row in rows:
        text, box, score = row.get('text'), row.get('box'), row.get('score')
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise MaaFrameworkOcrError('framework_output_invalid')
        if score < confidence:
            continue
        if (not isinstance(text, str) or not text.strip() or len(text) > 512
                or not isinstance(box, list) or len(box) != 4
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)
                or type(score) not in (int, float) or not math.isfinite(score)):
            raise MaaFrameworkOcrError('framework_output_invalid')
        x, y, w, h = box
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > width or y+h > height:
            raise MaaFrameworkOcrError('framework_output_geometry')
        words.append({'text': text, 'rect': dict(zip(('x','y','width','height'), box))})
    return words


class MaaFrameworkOcr:
    def __init__(self, runtime, resource, log_directory, *, timeout=10, confidence=.85):
        self.runtime, self.resource_path = Path(runtime).resolve(), Path(resource).resolve()
        self.log_directory = Path(log_directory)
        if not .01 <= timeout <= 30 or not 0 <= confidence <= 1:
            raise ValueError('invalid OCR limits')
        self.timeout, self.confidence = timeout, confidence
        self._lock = threading.Lock()
        self._resource = None
        self._quarantined = None
        self._pending_resource = None
        self._pending_job = None

    def _check(self, cancel, deadline):
        if cancel is not None and cancel.is_set():
            raise MaaFrameworkOcrError('framework_cancelled')
        if time.monotonic() >= deadline:
            raise MaaFrameworkOcrError('framework_timeout')

    def _wait(self, job, cancel, deadline):
        while not job.done:
            self._check(cancel, deadline)
            time.sleep(.01)
        self._check(cancel, deadline)
        if not job.succeeded:
            raise MaaFrameworkOcrError('framework_job_failed')
        return job

    def _load(self, cancel, deadline):
        with _NATIVE_QUARANTINE_LOCK:
            if _NATIVE_QUARANTINE:
                raise MaaFrameworkOcrError('framework_unavailable')
        if self._resource is not None:
            return
        if not (self.runtime/'maa').is_dir() or not self.resource_path.is_dir():
            raise MaaFrameworkOcrError('framework_unavailable')
        if str(self.runtime) not in sys.path:
            sys.path.insert(0, str(self.runtime))
        from maa.library import Library
        from maa.resource import Resource
        from maa.toolkit import Toolkit
        if Library.version() != 'v5.15.0':
            raise MaaFrameworkOcrError('framework_version_mismatch')
        self.log_directory.mkdir(parents=True, exist_ok=True)
        Toolkit.init_option(self.log_directory, {})
        resource = Resource()
        self._pending_resource = resource
        resource.use_cpu()
        self._pending_job = resource.post_bundle(self.resource_path)
        self._wait(self._pending_job, cancel, deadline)
        self._resource = resource
        self._pending_resource = self._pending_job = None

    def _quarantine(self, tasker, controller):
        # Native destructors can block while unfinished jobs still exist.
        # Retain initialization jobs too; this observer then becomes unavailable.
        self._quarantined = (self._resource, self._pending_resource,
                             controller, tasker, self._pending_job)
        if any(item is not None for item in self._quarantined):
            with _NATIVE_QUARANTINE_LOCK:
                _NATIVE_QUARANTINE.append(self._quarantined)
        if tasker is not None:
            try:
                tasker.post_stop()
            except RuntimeError:
                pass

    def recognize(self, image_path, *, cancel_event=None):
        started = time.monotonic()
        deadline = started+self.timeout
        while not self._lock.acquire(timeout=.025):
            self._check(cancel_event, deadline)
        tasker = controller = None
        try:
            self._check(cancel_event, deadline)
            if self._quarantined is not None:
                raise MaaFrameworkOcrError('framework_unavailable')
            self._load(cancel_event, deadline)
            import cv2
            import numpy
            from maa.controller import CustomController
            from maa.pipeline import JOCR, JRecognitionType
            from maa.tasker import Tasker
            raw = Path(image_path).read_bytes()
            if not raw or len(raw) > MAX_IMAGE_BYTES:
                raise MaaFrameworkOcrError('framework_image_invalid')
            image = cv2.imdecode(numpy.frombuffer(raw, dtype=numpy.uint8), cv2.IMREAD_COLOR)
            if image is None or max(image.shape[:2]) > MAX_DIMENSION:
                raise MaaFrameworkOcrError('framework_image_invalid')
            height, width = image.shape[:2]
            controller = fixed_controller(CustomController, image)
            self._pending_job = controller.post_connection()
            self._wait(self._pending_job, cancel_event, deadline)
            tasker = Tasker()
            if not tasker.bind(self._resource, controller):
                raise MaaFrameworkOcrError('framework_binding_failed')
            job = tasker.post_recognition(JRecognitionType.OCR,
                JOCR(roi=[0,0,width,height], threshold=self.confidence), image)
            self._pending_job = job
            self._wait(job, cancel_event, deadline)
            detail = job.get()
            nodes = detail.nodes if detail else []
            if len(nodes) != 1 or not nodes[0].recognition:
                raise MaaFrameworkOcrError('framework_output_invalid')
            words = project_words(nodes[0].recognition.raw_detail, width, height, self.confidence)
            if controller.input_attempts:
                raise MaaFrameworkOcrError('framework_input_rejected')
            self._pending_job = None
            return {'width':width, 'height':height, 'words':words,
                    'text':'\n'.join(word['text'] for word in words), 'engine':'maafw_ocr/v1',
                    'elapsed_ms':(time.monotonic()-started)*1000,
                    'image_sha256':hashlib.sha256(raw).hexdigest()}
        except MaaFrameworkOcrError:
            if self._quarantined is None:
                self._quarantine(tasker, controller)
            raise
        except RuntimeError:
            self._quarantine(tasker, controller)
            raise MaaFrameworkOcrError('framework_job_failed') from None
        finally:
            self._lock.release()


class LocalFirstReadback:
    requires_frame_refresh = True
    import_readback_timeout = 120

    def __init__(self, local, fallback):
        self.local, self.fallback = local, fallback

    def observe(self, image_path, *, expected=None, cancel_event=None):
        reason = 'local_unavailable'
        try:
            frame = self.local.observe(image_path,expected=expected,cancel_event=cancel_event)
            options = frame.get('options',{})
            controls_ready = all(name in frame.get('targets',{}) for name in ('code_input','paste_button'))
            options_ready = all(type(options.get(name,{}).get('state')) is bool
                                for name in ('auto_formation','ignore_requirements','use_support','multiple_jobs'))
            other_code = bool(expected and frame.get('codes')
                              and expected.get('code') not in frame['codes'])
            source = expected.get('source_document', {}) if expected else {}
            operators = source.get('opers') if isinstance(source, dict) else None
            groups = source.get('groups', []) if isinstance(source, dict) else None
            expected_count = (len(operators) + len(groups)
                              if isinstance(operators, list) and isinstance(groups, list) else None)
            # Text can pass the native confidence threshold while the visible
            # count does not. Do not declare that partial preview complete and
            # trap verification in repeated local reads: use the existing model
            # on this same image, without lowering OCR confidence or inventing
            # the source's count in the observation.
            count_ready = (type(frame.get('operator_count')) is int
                           and type(expected_count) is int and expected_count > 0
                           and frame['operator_count'] == expected_count)
            preview_ready = (not expected or frame.get('field_empty') is True
                or other_code
                or (frame.get('expected_title_visible') is True and frame.get('expected_stage_visible') is True
                    and not frame.get('missing_operators') and count_ready))
            if (frame.get('ok') is True and frame.get('page') == 'copilot'
                    and controls_ready and options_ready and preview_ready):
                return {**frame,'recognition_engine':'maafw_ocr+local_geometry'}
            reason = 'local_evidence_incomplete'
        except MaaFrameworkOcrError as error:
            if error.code == 'framework_cancelled':
                raise
            reason = error.code
        except (ImportError, OSError):
            reason = 'local_unavailable'
        result = self.fallback.observe(image_path,expected=expected,cancel_event=cancel_event)
        return {**result,'local_fallback_reason':reason}

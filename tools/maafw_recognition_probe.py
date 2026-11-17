"""Recognize pinned screenshots with the optional MaaFramework SDK.

This developer probe has no desktop controller. The SDK sees immutable images;
all input methods of its fixed-image controller reject calls. Recognition boxes
remain screenshot pixels until a later live observation supplies a transform.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time


def checked_roi(roi, shape):
    if (not isinstance(roi, (list, tuple)) or len(roi) != 4
            or any(type(value) is not int for value in roi)):
        raise ValueError('roi_invalid')
    x, y, width, height = roi
    if (x < 0 or y < 0 or width <= 0 or height <= 0
            or x + width > shape[1] or y + height > shape[0]):
        raise ValueError('roi_outside_image')
    return tuple(roi)


def classify_results(raw, roi, shape, *, confidence):
    """Unknown is preserved for absent, ambiguous or out-of-bounds results."""
    result = {'status':'unknown', 'reason':'no_unique_match', 'box':None}
    candidates = raw.get('filtered') if isinstance(raw, dict) else None
    if not isinstance(candidates, list) or len(candidates) != 1:
        return result
    candidate = candidates[0]
    box = candidate.get('box')
    score = candidate.get('score')
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not confidence <= score <= 1:
        return {**result, 'reason':'confidence_below_threshold'}
    try:
        x, y, width, height = checked_roi(box, shape)
    except ValueError:
        return {**result, 'reason':'result_box_invalid'}
    if x < roi[0] or y < roi[1] or x + width > roi[0] + roi[2] or y + height > roi[1] + roi[3]:
        return {**result, 'reason':'result_outside_roi'}
    return {'status':'recognized', 'reason':'unique_roi_match', 'box':[x,y,width,height],
            'score':score, 'text':candidate.get('text'),
            'normalized_box':[x/shape[1],y/shape[0],width/shape[1],height/shape[0]]}


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

        start_app = reject_input
        stop_app = reject_input
        click = reject_input
        swipe = reject_input
        touch_down = reject_input
        touch_move = reject_input
        touch_up = reject_input
        click_key = reject_input
        input_text = reject_input
        key_down = reject_input
        key_up = reject_input
        scroll = reject_input
        relative_move = reject_input
        shell = reject_input

    return FixedImageController()


def run_probe(runtime, resource_path, manifest_path, output):
    runtime, resource_path = Path(runtime).resolve(), Path(resource_path).resolve()
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('output_must_be_new')
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8-sig'))
    if manifest.get('schema') != 'maafw_fixed_image_cases/v1' or not manifest.get('cases'):
        raise ValueError('case_manifest_invalid')
    sys.path.insert(0, str(runtime))
    import cv2
    import numpy
    from maa.controller import CustomController
    from maa.pipeline import JOCR, JRecognitionType, JTemplateMatch
    from maa.resource import Resource
    from maa.tasker import Tasker
    from maa.toolkit import Toolkit
    from maa.library import Library

    output.mkdir(parents=True)
    Toolkit.init_option(output/'native-logs', {})
    resource = Resource()
    resource.use_cpu()
    if not resource.post_bundle(resource_path).wait().succeeded:
        raise RuntimeError('resource_load_failed')
    records = []
    input_attempts = 0
    for case in manifest['cases']:
        path = Path(case['image']['path']).resolve()
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != case['image']['sha256']:
            raise ValueError('screenshot_hash_changed')
        image = cv2.imdecode(numpy.frombuffer(raw, dtype=numpy.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError('screenshot_decode_failed')
        roi = checked_roi(case['roi'], image.shape)
        controller = fixed_controller(CustomController, image)
        if not controller.post_connection().wait().succeeded:
            raise RuntimeError('fixed_image_controller_failed')
        tasker = Tasker()
        if not tasker.bind(resource, controller):
            raise RuntimeError('tasker_bind_failed')
        threshold = case.get('threshold', .85)
        if case['algorithm'] == 'OCR':
            kind = JRecognitionType.OCR
            parameters = JOCR(expected=case['expected'], roi=roi, threshold=threshold,
                              only_rec=case.get('only_rec', False))
        elif case['algorithm'] == 'TemplateMatch':
            kind = JRecognitionType.TemplateMatch
            parameters = JTemplateMatch(template=case['templates'], roi=roi, threshold=[threshold])
        else:
            raise ValueError('unsupported_recognition')
        started = time.monotonic()
        job = tasker.post_recognition(kind, parameters, image).wait()
        detail = job.get()
        nodes = detail.nodes if detail else []
        recognition = nodes[0].recognition if len(nodes) == 1 else None
        raw_result = recognition.raw_detail if recognition else {}
        decision = classify_results(raw_result, roi, image.shape, confidence=threshold)
        record = {'case':case['name'], 'algorithm':case['algorithm'], 'image':case['image'],
                  'image_size':[image.shape[1],image.shape[0]], 'roi':list(roi),
                  'expected':case.get('expected', []), 'templates':case.get('templates', []),
                  'threshold':threshold, 'elapsed_seconds':round(time.monotonic()-started,4),
                  'sdk_job_succeeded':job.succeeded, 'sdk_result':raw_result, 'decision':decision,
                  'expected_status':case['expected_status'],
                  'source_kind':case.get('source_kind','historical_MAA_capture'),
                  'desktop_transform':None, 'dpi':None, 'live_input_eligible':False}
        record['passed'] = decision['status'] == case['expected_status']
        records.append(record)
        input_attempts += controller.input_attempts
        tasker.post_stop().wait()
        del tasker, controller
        (output/'results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    report = {'schema':'maafw_readonly_recognition_report/v1', 'sdk_version':Library.version(),
              'mode':'fixed_images_recognition_only', 'resource':str(resource_path),
              'cases':records, 'input_attempts':input_attempts, 'os_input_calls':0,
              'model_requests':0, 'passed':all(item['passed'] for item in records) and input_attempts == 0}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--resource',type=Path,required=True)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    report = run_probe(args.runtime,args.resource,args.manifest,args.output)
    print(json.dumps({'passed':report['passed'],'cases':len(report['cases']),
                      'input_attempts':report['input_attempts']}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

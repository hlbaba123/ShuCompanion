"""Read-only diagnostic of the installed MAA queue through the product adapters."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for component in ('.agent-runtime', '.capture-runtime', '.gui-runtime', '.companion-runtime'):
    sys.path.insert(0, str(ROOT.parent/component))

from shu import maa_queue_receipt as receipt
from shu.gui_supervisor import GuiSupervisor
from shu.maa_queue_readback import QueueVisionReadback
from shu.maa_visual_import import VisualCodeImporter
from shu.maa_visual_native import discover_maa_windows

discover_queue_binding = receipt.discover_queue_binding


def _vision_observer(project_root, measured):
    from shu.maa_vision_readback import DeepSeekClient
    from shu.providers import ProviderConfig

    profile = json.loads((project_root/'workspace-profile.json').read_text(encoding='utf-8'))
    reference = profile['key_file']
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError('Invalid key file reference')
    key_file = (project_root/reference).resolve(strict=True)
    config = ProviderConfig(api_key_file=key_file, model='deepseek-flash', thinking=False,
                            max_tokens=8192, timeout_seconds=45)
    client = DeepSeekClient(config)
    client.TRANSIENT_RETRIES = 0
    return QueueVisionReadback(client=client, metrics_callback=measured)


def _frame_ref(frame, identity, directory):
    identifier = frame['screenshot_id']
    if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise receipt.MaaQueueReceiptError('native_capture_invalid')
    path = directory/'maa-visual-captures'/(identifier+'.png')
    if Path(frame['image_path']) != path:
        raise receipt.MaaQueueReceiptError('native_capture_invalid')
    if any(frame['window'][field] != identity[field]
           for field in ('hwnd', 'pid', 'creation_time', 'executable')):
        raise receipt.MaaQueueReceiptError('binding_changed')
    raw, _ = receipt._read_stable(path)
    dimensions = frame['window']['client_rect']
    if (len(raw) < 24 or raw[:8] != b'\x89PNG\r\n\x1a\n' or raw[12:16] != b'IHDR'
            or struct.unpack('>II', raw[16:24]) != (dimensions['width'], dimensions['height'])
            or hashlib.sha256(raw).hexdigest() != frame['image_sha256']
            or frame.get('method') not in {'win32_printwindow', 'windows_graphics_capture'}):
        raise receipt.MaaQueueReceiptError('native_capture_invalid')
    return {'path':str(path), 'sha256':frame['image_sha256']}


def _code(value, default):
    return value if isinstance(value, str) and re.fullmatch(r'[a-z_]{1,64}', value) else default


def run_probe(output, *, project_root=ROOT, with_vision=False):
    project_root = Path(project_root).resolve()
    output = receipt._no_links(Path(output)).resolve()
    artifacts = (project_root/'artifacts').resolve()
    if output == artifacts or not output.is_relative_to(artifacts):
        raise ValueError('Probe output must be a new directory inside project artifacts')
    if output.exists():
        raise FileExistsError('Probe output already exists')
    output.mkdir(parents=True)
    report = {'schema':'maa_queue_probe/v1', 'mode':'read_only', 'passed':False,
              'code':'probe_incomplete', 'gui_inputs':0, 'model_requests':0,
              'key_copied':False, 'queue_verified':False, 'evidence_refs':[]}
    supervisor = None
    started = time.monotonic()
    try:
        windows = discover_maa_windows()
        report['window_count'] = len(windows)
        if not windows:
            report['code'] = 'maa_window_unavailable'
            return report
        if len(windows) != 1:
            report['code'] = 'maa_window_ambiguous'
            return report
        window = windows[0]
        if window.get('minimized'):
            report['code'] = 'maa_window_minimized'
            return report
        identity = {key:window[key] for key in ('hwnd','pid','creation_time','executable')}
        binding = discover_queue_binding(identity)
        identifier = 'probe-'+uuid.uuid4().hex
        directory = output/'task-private'/identifier/'maa-queue'/'1'
        directory.mkdir(parents=True)
        supervisor = GuiSupervisor(identity, backend='visual', directory=directory)
        response = supervisor.request({'op':'snapshot'}, timeout=12)
        if response.get('status') != 'completed':
            report['code'] = _code(response.get('code') or response.get('error'), 'native_capture_unavailable')
            return report
        frame = response['result']
        image_ref = _frame_ref(frame, identity, directory)
        snapshot = receipt.capture_queue_snapshot(binding, directory, screenshot_refs=[image_ref],
                                                  visible_complete=False)
        report.update(binding=binding, native_window=frame['window'],
                      snapshot_ref=snapshot['evidence_ref'], row_count=len(snapshot['rows']))
        report['evidence_refs'].extend([image_ref, snapshot['evidence_ref']])
        report['code'] = 'queue_snapshot_captured'
        report['passed'] = True
        if with_vision:
            def measured(metrics):
                report['model_requests'] += int(metrics.get('input_images', 0) > 0)

            observer = _vision_observer(project_root, measured)
            reader = VisualCodeImporter(supervisor, readback=observer)
            view = reader._interpret(frame, {
                'projection_sha256':snapshot['projection_sha256'], 'rows':snapshot['rows']}, None,
                recognition_timeout=45)
            view_ref = receipt._publish(directory, 'maa-queue-probe-vision', view)
            report['evidence_refs'].append(view_ref)
            if (view.get('ok') is not True or view.get('image_sha256') != image_ref['sha256']
                    or view.get('width') != frame['window']['client_rect']['width']
                    or view.get('height') != frame['window']['client_rect']['height']):
                report.update(passed=False, code=_code(view.get('reason'), 'queue_vision_unverified'))
                return report
            after = receipt.capture_queue_snapshot(binding, directory, screenshot_refs=[image_ref],
                visible_complete=view.get('visible_complete') is True and view.get('queue_mode') is True)
            report['evidence_refs'].append(after['evidence_ref'])
            if after['projection_sha256'] != snapshot['projection_sha256'] or after['rows'] != snapshot['rows']:
                report.update(passed=False, code='queue_projection_changed')
                return report
            report.update(snapshot_ref=after['evidence_ref'], queue_verified=after['visible_complete'],
                          page=view.get('page'), queue_mode=view.get('queue_mode'),
                          queue_add_enabled=view.get('queue_add_enabled'),
                          scroll_state=view.get('scroll_state'),
                          code='queue_visible_verified' if after['visible_complete'] else 'queue_visibility_pending',
                          passed=after['visible_complete'])
        return report
    except Exception as error:
        report.update(passed=False, code=_code(getattr(error, 'code', None), 'queue_probe_failed'))
        return report
    finally:
        if supervisor is not None:
            try:
                supervisor.close()
            except Exception:
                report.update(passed=False, code='probe_helper_exit_unconfirmed')
        report['elapsed_seconds'] = round(time.monotonic()-started, 3)
        (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--with-vision', action='store_true', help='Use the configured vision model after a valid capture')
    args = parser.parse_args()
    try:
        report = run_probe(args.output, with_vision=args.with_vision)
    except (OSError, ValueError):
        print(json.dumps({'passed':False, 'code':'probe_output_rejected'}))
        return 2
    print(json.dumps({key:report[key] for key in
                     ('passed','code','gui_inputs','model_requests','elapsed_seconds')}))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())

"""Explicit read-only MAA readiness probe, with no model or input operation."""
from __future__ import annotations

import json
import ntpath
import re
from pathlib import Path

from .maa_uia import MAA_EXECUTABLE, MaaUia


def probe_maa(output, *, windows=None, uia_factory=None, readiness=None):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if readiness is None:
        from .maa_readiness import inspect_maa_readiness
        readiness = inspect_maa_readiness
    if windows is None:
        from .windows import WindowController
        windows = WindowController(output).list_windows(False)
    identities = [item.to_dict() if hasattr(item, 'to_dict') else dict(item) for item in windows]
    matches = [item for item in identities if ntpath.normcase(ntpath.normpath(item.get('executable', '')))
               == ntpath.normcase(ntpath.normpath(MAA_EXECUTABLE))]
    report = {'passed': False, 'mode': 'read_only_maa_readiness',
              'model_requests': 0, 'gui_inputs': 0, 'game_inputs': 0}
    if len(matches) != 1:
        report.update(code='maa_window_not_unique', matching_windows=len(matches))
    else:
        identity = matches[0]
        report['identity'] = {key: identity[key] for key in ('hwnd', 'pid', 'executable')}
        report['identity']['minimized'] = identity.get('minimized') is True
        preflight = readiness(identity)
        report['readiness'] = preflight
        permission = preflight.get('permission', {})
        if permission.get('requires_elevation') or permission.get('confirmed_mismatch'):
            report['code'] = preflight.get('code', 'elevation_required')
        elif identity.get('minimized') is True:
            report.update(code='maa_window_minimized',
                          error_message='请先将 MAA 从任务栏恢复为正常窗口，再重试控件读取。')
        else:
            try:
                snapshot = (uia_factory or MaaUia)(identity['hwnd'], identity['pid']).snapshot()
                report['snapshot'] = snapshot
                report['readiness'] = readiness(identity, snapshot)
                report['passed'] = report['readiness'].get('status') == 'ready'
                report['code'] = report['readiness'].get('code', 'ready' if report['passed'] else 'not_ready')
            except (OSError, RuntimeError, ValueError) as error:
                report['code'] = getattr(error, 'code', 'uia_unavailable')
                report['error_type'] = type(error).__name__
                report['error_stage'] = getattr(error, 'stage', None)
                report['error_message'] = re.sub(r'(?i)\bsk-[A-Za-z0-9_.-]+|bearer\s+\S+',
                                                 '[redacted]', str(error))[:1000]
    destination = output / 'maa-readiness.json'
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(destination)
    return report

"""Explicit paid network acceptance through the desktop's actual chat entry.

No UI, game, microphone or input automation. The existing key path is consumed
by DeepSeekClient; its value and provider reasoning are never written here.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for directory in ('.agent-runtime', '.capture-runtime', '.gui-runtime'):
    path = ROOT.parent / directory
    if path.is_dir():
        sys.path.insert(0, str(path))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--question', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=240)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / 'artifacts').resolve()) or output.exists():
        raise SystemExit('Use a fresh output under this project artifacts directory')
    output.mkdir(parents=True)
    os.environ['TEMP'] = os.environ['TMP'] = str(ROOT.parent / '.pet-temp')
    from PySide6.QtCore import QCoreApplication
    from shu.controller import SessionController
    app = QCoreApplication.instance() or QCoreApplication([])
    profile = json.loads((ROOT / 'workspace-profile.json').read_text(encoding='utf-8'))
    original_data = (ROOT / profile['data_dir']).resolve()
    key_file = (ROOT / profile['key_file']).resolve()
    if not original_data.is_relative_to(ROOT) or not key_file.is_relative_to(ROOT):
        raise SystemExit('Configured profile path is outside this project')
    settings = json.loads((original_data / 'settings.json').read_text(encoding='utf-8'))
    data = output / 'data'
    data.mkdir()
    selected = {k: settings[k] for k in ('model', 'base_url', 'game_model', 'game_thinking') if k in settings}
    (data / 'settings.json').write_text(json.dumps(selected), encoding='utf-8')
    controller = SessionController(ROOT, data, key_path=key_file)
    messages, errors, progress = [], [], []
    controller.message.connect(lambda role, text: messages.append({'role': role, 'text': text}))
    controller.error.connect(errors.append)
    def on_progress(event):
        progress.append(event)
        print(json.dumps(event, ensure_ascii=False), flush=True)
    controller.network_progress.connect(on_progress)
    started = time.monotonic()
    clock = lambda: datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
    started_at = clock()
    try:
        controller.submit(args.question)
        while controller.is_busy and time.monotonic() - started < args.timeout:
            app.processEvents()
            time.sleep(.02)
        app.processEvents()
        timed_out = controller.is_busy
        if timed_out:
            controller.stop(silent=True)
        answer = '\n\n'.join(m['text'] for m in messages if m['role'] == 'assistant')
        report = {'question': args.question, 'started_at': started_at, 'completed_at': clock(),
            'elapsed_seconds': round(time.monotonic() - started, 3),
            'timed_out': timed_out, 'errors': errors, 'answer': answer,
            'network': controller.last_network_report, 'progress': progress,
            'model_metrics': list(controller.client.metrics_history),
            'screen_permission': controller.screen_capture_allowed,
            'input_permission': controller.game_input_allowed,
            'created_tasks': len(controller.engine.list_tasks())}
        success = bool(answer and report['network'].get('sources') and not errors and not timed_out)
        report['status'] = 'network_returned_sources' if success else 'needs_review'
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        (output / 'answer.md').write_text(answer or '未取得可展示回答。', encoding='utf-8')
        print(json.dumps({'status': report['status'], 'elapsed_seconds': report['elapsed_seconds'],
                          'source_count': len(report['network'].get('sources', [])),
                          'report_path': str(output / 'report.json')}, ensure_ascii=False), flush=True)
        return 0 if success else 1
    finally:
        controller.close()


if __name__ == '__main__':
    raise SystemExit(main())

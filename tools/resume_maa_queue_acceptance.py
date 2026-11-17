"""Explicitly resume one existing isolated import-only acceptance task.

Uses the product Continue action. It neither submits a new request nor edits
contracts, researched candidates, old reports or evidence artifacts.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for name in ('.capture-runtime', '.gui-runtime', '.agent-runtime', '.companion-runtime'):
    sys.path.insert(0, str(ROOT.parent / name))


def checked_path(value, *, existing=False):
    path = Path(value).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('Links are not accepted')
    path = path.resolve(strict=existing)
    artifacts = (ROOT / 'artifacts').resolve(strict=True)
    if path == artifacts or not path.is_relative_to(artifacts):
        raise ValueError('Path must be inside project artifacts')
    return path


def original_task(data, task_id):
    """Validate before constructing a controller or loading a provider."""
    if data.name != 'data' or data.parent.name != 'conversation' or not data.is_dir():
        raise ValueError('Expected an existing acceptance conversation/data directory')
    database = data / 'tasks.sqlite3'
    checked_path(database, existing=True)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        ids = [row[0] for row in db.execute('SELECT task_id FROM workflow_tasks')]
        if ids != [task_id]:
            raise ValueError('Acceptance profile must contain exactly the selected task')
        row = db.execute('SELECT t.status,w.revision,w.epoch,w.writes_enabled,c.contract '
            'FROM tasks t JOIN workflow_tasks w ON w.task_id=t.id '
            'JOIN workflow_contracts c ON c.task_id=w.task_id AND c.revision=w.revision '
            'WHERE t.id=?', (task_id,)).fetchone()
        if not row or row['status'] not in {'paused', 'needs_input'} or row['writes_enabled']:
            raise ValueError('Only a stopped, non-writing acceptance task may resume')
        contract = json.loads(row['contract'])
        from shu.leader_policy import import_satisfies_goal
        if (contract.get('current_deliverable') != 'maa_import'
                or contract.get('allow_start') is not False
                or contract.get('attempt_limit', 0) != 0 or not import_satisfies_goal(contract)):
            raise ValueError('Task must remain import-only')
        if db.execute("SELECT 1 FROM workflow_messages WHERE status IN ('accepted','pending','processing') LIMIT 1").fetchone():
            raise ValueError('Pending dialogue must finish before acceptance resume')
        message = db.execute("SELECT text FROM workflow_messages WHERE task_id=? AND relation='new' "
            "AND status='applied' ORDER BY rowid LIMIT 1", (task_id,)).fetchone()
        if not message or not isinstance(message['text'], str) or not message['text'].strip():
            raise ValueError('Original admitted user request is missing')
        operations = [row[0] for row in db.execute(
            'SELECT operation_id FROM workflow_operations WHERE submitted_at IS NOT NULL')]
        if db.execute('SELECT 1 FROM workflow_operations WHERE submitted_at IS NOT NULL '
                      'AND (starts<>0 OR stop_only<>0) LIMIT 1').fetchone():
            raise ValueError('Profile contains input outside import-only acceptance')
        return {'task_id': task_id, 'revision': row['revision'], 'epoch': row['epoch'],
                'status': row['status'], 'original_request': message['text'],
                'submitted_operation_ids': operations}


def immutable_reports(data):
    paths = [data.parent / 'report.json', data.parent / 'progress.jsonl',
             data.parent.parent / 'report.json']
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.is_file()}


def trace_recovery_exceptions(events):
    """Trace only exception metadata crossing the recovery module's frames.

    Never inspect frame locals, arguments, other modules or credential values.
    New worker threads inherit this trace until the returned restore callback.
    """
    from shu.maa_queue_acceptance import _redact
    source = str((ROOT / 'shu/maa_queue_recovery.py').resolve())
    previous = (sys.gettrace(), threading.gettrace())
    seen = set()
    def trace(frame, event, arg):
        if frame.f_code.co_filename != source:
            return None
        if event == 'exception' and len(events) < 64:
            kind, error, _ = arg
            if issubclass(kind, (KeyError, ValueError, TypeError, OSError, RuntimeError)):
                entry = {'filename': frame.f_code.co_filename, 'line': frame.f_lineno,
                         'type': kind.__name__, 'message': _redact(str(error))[:200]}
                key = (entry['line'], entry['type'], entry['message'])
                if key not in seen:
                    seen.add(key)
                    events.append(entry)
        return trace
    sys.settrace(trace)
    threading.settrace(trace)
    def restore():
        sys.settrace(previous[0])
        threading.settrace(previous[1])
    return restore


def run(args):
    if not args.allow_queue_import:
        raise ValueError('Explicit --allow-queue-import is required')
    if not 1 <= args.timeout <= 1800 or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', args.task_id):
        raise ValueError('Invalid task identifier or timeout')
    data = checked_path(args.data_dir, existing=True)
    output = checked_path(args.output)
    if output.exists() or output.is_relative_to(data.parent.parent) or data.is_relative_to(output):
        raise ValueError('Output must be new and separate from the original acceptance run')
    initial = original_task(data, args.task_id)
    preserved = immutable_reports(data)
    profile = json.loads((ROOT / 'workspace-profile.json').read_text(encoding='utf-8'))
    key = (ROOT / profile['key_file']).resolve(strict=True)
    if not key.is_file() or not key.is_relative_to(ROOT):
        raise ValueError('Expected the existing project credential reference')
    # Force the Qt surface offscreen; native MAA observation/input stays real.
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QApplication
    from shu.controller import SessionController
    from shu.maa_agents import MaaRoleAgents
    from shu.maa_queue_acceptance import _inspect_queue_evidence, _redact, _usage
    from main import APP_VERSION
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationVersion(APP_VERSION)
    lock = QLockFile(str(data / 'session.lock'))
    if not lock.tryLock(100):
        raise ValueError('Acceptance profile is already locked')
    output.mkdir(parents=True)
    controller = None
    roles, events, results, errors, messages = [], [], [], [], []
    recovery_exceptions = []
    restore_trace = (trace_recovery_exceptions(recovery_exceptions)
                     if getattr(args, 'trace_recovery', False) else lambda: None)
    started = time.monotonic()
    report = {'schema': 'maa_queue_resume_acceptance/v1', 'passed': False,
        'code': 'resume_incomplete', 'scope': 'existing task MAA queue import only',
        'data_dir': str(data), 'initial': initial, 'key_copied': False,
        'runtime': {'version': APP_VERSION, 'executable': sys.executable,
                    'frozen': bool(getattr(sys, 'frozen', False)), 'resource_root': str(ROOT)},
        'original_reports_before': preserved, 'timed_out': False,
        'trace_recovery': bool(getattr(args, 'trace_recovery', False)),
        'recovery_exceptions': recovery_exceptions}
    def progress(event):
        value = _redact(event)
        events.append(value)
        with (output / 'progress.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + '\n')
    def role_factory(*values, **options):
        role = MaaRoleAgents(*values, **options)
        roles.append(role)
        return role
    try:
        controller = SessionController(ROOT, data, key_path=key)
        controller.execution.queue_import_enabled = True
        controller.screen_capture_allowed = True
        controller.game_input_allowed = True
        controller.execution.role_factory = role_factory
        controller.activity_progress.connect(progress)
        controller.execution.finished.connect(results.append)
        controller.error.connect(errors.append)
        controller.message.connect(lambda role, text: messages.append({'role': role, 'text': text}))
        # This is the normal product action; no submit/new chat is performed.
        report['continue_result'] = controller.continue_task(args.task_id)
        while time.monotonic() - started < args.timeout:
            app.processEvents()
            state = controller.execution.store.current(args.task_id)
            if (not controller.is_busy and not controller.execution.active
                    and state['status'] in {'completed', 'needs_input', 'paused', 'failed', 'cancelled', 'stopped'}):
                break
            time.sleep(.025)
        else:
            report['timed_out'] = True
        report['state_before_close'] = controller.execution.store.current(args.task_id)
        report['intent_metrics'] = list(controller.client.metrics_history)
        report['role_metrics'] = {role: [metric for obj in roles if role in obj.clients
            for metric in obj.clients[role].metrics_history]
            for role in ('leader', 'catalog', 'research', 'maa', 'vision', 'diagnosis')}
        report['results'], report['errors'], report['messages'] = results, errors, messages
    except Exception as error:
        report['code'] = 'resume_runtime_error'
        report['error'] = {'type': type(error).__name__, 'message': _redact(str(error))[:1000]}
    finally:
        try:
            if controller is not None:
                controller.close()
            app.processEvents()
            evidence = _inspect_queue_evidence(data, initial['original_request'])
            previous = set(initial['submitted_operation_ids'])
            report.update(evidence=evidence, operations=evidence['operations'],
                new_operations=[item for item in evidence['operations'] if item['operation_id'] not in previous],
                battle_start_submitted=evidence['battle_start_submitted'],
                original_reports_unchanged=immutable_reports(data) == preserved,
                elapsed_seconds=round(time.monotonic() - started, 3), usage=_usage(report))
            report['passed'] = bool(evidence['verified'] and not report['timed_out']
                and not errors and report['original_reports_unchanged']
                and report.get('code') != 'resume_runtime_error')
            if report['code'] != 'resume_runtime_error':
                report['code'] = 'resume_timed_out' if report['timed_out'] else evidence['code']
            (output / 'report.json').write_text(json.dumps(_redact(report), ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            restore_trace()
            lock.unlock()
    print(json.dumps({'passed': report['passed'], 'code': report['code'],
        'new_gui_operations': len(report['new_operations']), 'report': str(output / 'report.json')}, ensure_ascii=False))
    return 0 if report['passed'] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--allow-queue-import', action='store_true')
    parser.add_argument('--timeout', type=int, default=1800)
    parser.add_argument('--trace-recovery', action='store_true',
                        help='Record only recovery-module exception filename/line/type/message; no locals')
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        print(json.dumps({'passed': False, 'code': 'resume_arguments_rejected',
                          'error_type': type(error).__name__}))
        return 2


if __name__ == '__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())

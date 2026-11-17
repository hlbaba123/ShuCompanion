"""Explicit final-EXE overlap test on the user's existing import-only profile.

Uses the product's real routing, models, checkpoints, tools and GUI adapters.
No candidate, answer, task contract or screenshot recognition is pre-seeded.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import time


def run_overlap_acceptance(resource_root, output, key_path, data_dir, *, version):
    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QApplication
    from .controller import SessionController
    from .code_delivery import CodeDeliveryStore
    from .conversation_acceptance import import_verification

    output, data = Path(output).resolve(), Path(data_dir).resolve()
    if output.exists() or output.is_relative_to(data):
        raise ValueError('Use a new evidence directory outside the live profile')
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationVersion(version)
    lock = QLockFile(str(data/'session.lock'))
    if not lock.tryLock(100):
        raise RuntimeError('Profile is in use; exit the other pet before the acceptance')
    output.mkdir(parents=True)
    backup = output/'before'; backup.mkdir()
    report = {'version': version, 'frozen': bool(getattr(sys,'frozen',False)),
              'scope': 'real overlapping requests; existing profile; MAA import only; no battle',
              'requests': ['新建独立任务：查找孤星 CW-1 的 MAA 作业并导入 MAA，不开始作战。',
                           '新建独立任务：查询将进酒 IW-1 的 MAA 作业神秘代码，不导入、不开始作战。'],
              'checks': {}, 'errors': [], 'routes': [], 'messages': [], 'results': []}
    c = None

    def save():
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

    try:
        for name in ('tasks.sqlite3','conversation.sqlite3','maa-graph.sqlite3','task-runtime.sqlite3'):
            source = data/name
            if source.exists():
                with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as db:
                    with sqlite3.connect(backup/name) as dest:
                        db.backup(dest)
        with sqlite3.connect((data/'tasks.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
            db.row_factory = sqlite3.Row
            # A live-profile test must not implicitly resume historical work
            # through the product's ordinary startup recovery timers.
            if db.execute('SELECT 1 FROM workflow_tasks WHERE writes_enabled=1 LIMIT 1').fetchone():
                raise RuntimeError('Historical task still has write authority; stop it before this test')
            if db.execute("SELECT 1 FROM workflow_messages WHERE status IN ('accepted','pending','processing') LIMIT 1").fetchone():
                raise RuntimeError('Historical message is still pending; do not replay it as part of this test')
            if db.execute('SELECT 1 FROM workflow_cleanup_jobs LIMIT 1').fetchone():
                raise RuntimeError('Historical cleanup records exist; do not resume cleanup as part of this test')
            report['old_tasks_before_startup'] = {row['id']:dict(row) for row in db.execute('SELECT * FROM tasks')}
        c = SessionController(Path(resource_root),data,key_path=Path(key_path))
        c._task_id = c.execution.current_task_id = None
        c.execution.queue_import_enabled = True
        c.screen_capture_allowed = c.game_input_allowed = True
        store = c.execution.store
        original = store.pending_operations()
        report['old_operations'] = original
        report['old_tasks'] = {op['task_id']:store.current(op['task_id']) for op in original}
        c.error.connect(report['errors'].append)
        c.message.connect(lambda role,text:report['messages'].append({'role':role,'text':text}))
        c.message_routed.connect(report['routes'].append)
        c.execution.finished.connect(report['results'].append)
        def progress(event):
            with (output/'progress.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(event,ensure_ascii=False)+'\n')
        c.activity_progress.connect(progress)
        start = time.monotonic()
        c.set_input_target(None,mode='new')
        c.submit(report['requests'][0])
        second = False
        while time.monotonic()-start < 600:
            app.processEvents(); time.sleep(.02)
            if not second and not c.is_busy and c.execution.active:
                report['first_active_at_second_submission'] = True
                report['first_job_at_second_submission'] = c.execution._job.task_id if c.execution._job else None
                c.set_input_target(None,mode='new')
                c.submit(report['requests'][1])
                second = True
                save()
            if not c.is_busy and not c.execution.active and time.monotonic()-start > 1:
                break
        report['elapsed_seconds'] = round(time.monotonic()-start,3)
        report['timed_out'] = c.is_busy or c.execution.active
        ids = list(dict.fromkeys(r.get('task_id') for r in report['routes'] if r.get('task_id')))
        report['tasks'] = {tid:store.current(tid) for tid in ids}
        report['deliveries'] = {tid:CodeDeliveryStore(store,c.execution.directory).summary(tid,verify_current=True)
                                for tid in ids}
        report['old_after'] = {op['operation_id']:store.get_operation(op['operation_id']) for op in original}
        report['old_tasks_after'] = {tid:store.current(tid) for tid in report['old_tasks']}
        report['slot_after'] = store.slot_state()
        report['imports'] = {tid:import_verification(store,tid) for tid in ids
                             if report['tasks'][tid]['contract'].get('current_deliverable') == 'maa_import'}
        with c.engine._lock:
            report['starts'] = c.engine._db.execute('SELECT COUNT(*) FROM workflow_operations WHERE starts=1 '
                'AND submitted_at IS NOT NULL').fetchone()[0]
            report['new_inputs'] = [dict(row) for row in c.engine._db.execute(
                'SELECT operation_id,task_id,status,action FROM workflow_operations WHERE task_id IN ('+
                ','.join('?' for _ in ids)+') AND submitted_at IS NOT NULL',ids)] if ids else []
        checks = report['checks']
        checks['overlap_actually_exercised'] = second and report.get('first_active_at_second_submission') is True
        checks['distinct_task_identities'] = len(ids) == 2
        checks['old_tasks_not_resumed_or_changed'] = report['old_tasks'] == report['old_tasks_after']
        with c.engine._lock:
            after = {row['id']:dict(row) for row in c.engine._db.execute('SELECT * FROM tasks')}
        checks['all_previous_task_rows_unchanged'] = all(after.get(tid)==row
            for tid,row in report['old_tasks_before_startup'].items())
        checks['old_fence_settled_without_claiming_success'] = bool(original) and all(
            op['status'] == 'failed' and (op['result'] or {}).get('session_retired') is True
            and (op['result'] or {}).get('outcome') == 'unknown' for op in report['old_after'].values())
        checks['desktop_released'] = report['slot_after'].get('barrier_operation_id') is None
        checks['both_tasks_completed'] = len(ids)==2 and all(s['status']=='completed' for s in report['tasks'].values())
        checks['cw1_import_really_verified'] = len(report['imports'])==1 and all(v.get('verified') is True
            for v in report['imports'].values()) and all(
                {i['stage_code'] for i in report['deliveries'][tid].get('items',[])} == {'CW-1'}
                for tid in report['imports'])
        lookup = [tid for tid in ids if tid not in report['imports']]
        checks['independent_iw1_only'] = len(lookup)==1 and report['deliveries'][lookup[0]].get('complete') is True \
            and {i['stage_code'] for i in report['deliveries'][lookup[0]].get('items',[])}=={'IW-1'} \
            and not any(row['task_id']==lookup[0] for row in report['new_inputs'])
        checks['no_battle_or_errors_or_timeout'] = report['starts']==0 and not report['errors'] and not report['timed_out']
        report['passed'] = all(checks.values())
        save()
        return 0 if report['passed'] else 2
    except Exception as error:
        report['fatal_error'] = {'type':type(error).__name__,'message':str(error)}
        save()
        raise
    finally:
        try:
            if c:
                c.close()
            if report.get('old_tasks_before_startup') is not None:
                with sqlite3.connect((data/'tasks.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
                    db.row_factory = sqlite3.Row
                    after = {row['id']:dict(row) for row in db.execute('SELECT * FROM tasks')}
                unchanged = all(after.get(tid)==row for tid,row in report['old_tasks_before_startup'].items())
                report['checks']['old_tasks_unchanged_after_close'] = unchanged
                report['passed'] = report.get('passed',False) and unchanged
                save()
                if not unchanged:
                    raise RuntimeError('Historical task rows changed during acceptance shutdown')
        finally:
            lock.unlock()

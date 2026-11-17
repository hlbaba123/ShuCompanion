"""Resolve global import fences without replaying another task's GUI input.

An exited MAA instance cannot complete a late UI mutation. This retires only
that execution session; its business outcome remains unknown, never verified.
For the same live instance the ordinary queue receipt recovery is required.
"""
from __future__ import annotations

import threading
import json

from . import maa_queue_receipt as receipts
from .core import ActionRejected
from .maa_queue_store import QueueStore
from .task_cleanup import TaskCleanup
from .workflow_store import _encode


def _exited(process, observation):
    return (observation.get('status') == 'exited'
            and all(observation.get(key) == process[key]
                    for key in ('pid', 'creation_time', 'executable')))


def retire_exited_queue_inputs(workflow, supervisor, *, task_id, guard, cancel_event=None):
    from .maa_visual_native import inspect_process_instance

    cancel = cancel_event if cancel_event is not None else threading.Event()
    recovered, references = [], []
    scope = None

    def result(status, code):
        return {'status': status, 'code': code, 'recovered_operation_ids': recovered,
                'evidence_refs': references, 'gui_inputs': 0}

    def check():
        nonlocal scope
        if cancel.is_set():
            raise ActionRejected('recovery cancelled')
        guard()
        workflow.management.require_active(task_id)
        state = workflow.current(task_id)
        identity = tuple(state[key] for key in ('task_id', 'revision', 'epoch', 'control_seq'))
        if (not state['writes_enabled'] or state['status'] in
                {'paused', 'cancelled', 'completed', 'failed', 'needs_input'}
                or scope is not None and identity != scope):
            raise ActionRejected('recovery requester changed')
        scope = identity
        return state

    try:
        check()
        pending = workflow.pending_operations()
        if not pending:
            return result('reconciled', 'no_pending_inputs')
        journal = QueueStore(workflow)
        cleanup = TaskCleanup(workflow)
        frame = None
        for operation in pending:
            check()
            action = operation['action']
            phase = (action.get('queue_guard') or {}).get('phase')
            if (operation['slot'] != 'desktop-maa' or operation['starts'] or operation['stop_only']
                    or operation['status'] not in {'submitted', 'unknown'} or not operation['submitted_at']
                    or not operation['effect_key'].startswith('queue:') or action.get('kind') != 'queue_gui'
                    or phase not in {'code', 'add', 'delete', 'scroll'}):
                continue
            original = {key: operation[key] for key in ('task_id', 'revision', 'epoch')}
            with workflow.engine._lock:
                row = workflow._db.execute('SELECT payload,payload_hash FROM workflow_queue_runs '
                    'WHERE task_id=? AND revision=? AND epoch=?', tuple(original.values())).fetchone()
                if not row:
                    continue
                run = json.loads(row['payload'])
                if _encode(run)[1] != row['payload_hash']:
                    raise ValueError('historical queue run changed')
                source = workflow.current(operation['task_id'])
                if (source['writes_enabled'] and source['revision'] == operation['revision']
                        and source['epoch'] == operation['epoch']):
                    continue  # Never retire an executing generation.
            snapshot = journal._read_snapshot(original, action['snapshot_ref'])
            binding = run.get('baseline', {}).get('binding')
            if not binding or snapshot['binding'] != binding:
                raise ValueError('historical binding changed')
            process = binding['process']
            lifetime = inspect_process_instance(process)
            if not _exited(process, lifetime):
                continue
            if frame is None:
                # GuiSupervisor obtains both desktop ownership locks before
                # starting a screenshot helper. No abandoned input helper can
                # still own the desktop when this succeeds.
                response = supervisor.request({'op': 'snapshot'}, timeout=12, cancel_event=cancel)
                check()
                if response.get('status') != 'completed':
                    return result('pending', 'recovery_capture_unavailable')
                frame = response['result']
                if (any(frame.get('window', {}).get(key) != supervisor.target_identity[key]
                        for key in ('hwnd', 'pid', 'creation_time', 'executable'))
                        or frame.get('method') not in {'win32_printwindow', 'windows_graphics_capture'}):
                    raise ValueError('recovery frame binding mismatch')
                health = supervisor.health()
                if health.get('pending') or health.get('stop_pending') or health.get('closed'):
                    return result('pending', 'recovery_native_pending')
            # Only local bounded reads under the transaction. Stop/admission
            # and this commit share the same durable authorization boundary.
            with workflow.engine._transaction():
                check()
                current = workflow.get_operation(operation['operation_id'])
                slot = workflow.slot_state()
                live = inspect_process_instance(process)
                source = workflow.current(operation['task_id'])
                if (current != operation or not _exited(process, live)
                        or workflow.management._lifecycle(operation['task_id'])['state'] not in {'active', 'trashed'}
                        or source['writes_enabled'] and source['revision'] == operation['revision']
                            and source['epoch'] == operation['epoch']
                        or slot.get('barrier_operation_id') not in (None, operation['operation_id'])):
                    return result('pending', 'recovery_ledger_changed')
                proof = {'schema': 'maa_exited_session/v1', 'operation_id': operation['operation_id'],
                    'original_task_id': operation['task_id'], 'request_task_id': task_id,
                    'original_revision': operation['revision'], 'original_epoch': operation['epoch'],
                    'submitted_at': operation['submitted_at'], 'operation_sha256': operation['payload_hash'],
                    'original_binding': binding, 'lifetime': live,
                    # A lifetime/lease witness, not a claim about screenshot
                    # contents. Do not cross-link private screenshot files.
                    'observation': {key: frame[key] for key in
                                    ('window', 'method', 'screenshot_id', 'image_sha256')},
                    'outcome': 'unknown', 'session_retired': True, 'gui_inputs': 0}
                copies = {}
                for owner in dict.fromkeys((operation['task_id'], task_id)):
                    ref = receipts._publish(workflow.engine.data_dir/'task-private'/owner/
                                            'maa-input-recovery', 'maa-session-retired', proof)
                    private_path = cleanup._safe_path(owner, ref['path'])
                    workflow._db.execute('INSERT OR IGNORE INTO workflow_cleanup_files(task_id,path,sha256) '
                        'VALUES(?,?,?)', (owner, str(private_path), ref['sha256']))
                    copies[owner] = ref
                reference = copies[operation['task_id']]
                encoded, digest = _encode({'session_retired': True, 'outcome': 'unknown',
                    'request_task_id': task_id, 'evidence_ref': reference, 'gui_inputs': 0})
                workflow._db.execute("UPDATE workflow_operations SET status='failed',result=? "
                                     'WHERE operation_id=?', (encoded, operation['operation_id']))
                workflow._db.execute('UPDATE workflow_slots SET barrier_operation_id=NULL '
                    'WHERE slot=? AND barrier_operation_id=?', (operation['slot'], operation['operation_id']))
                workflow._publish_event(operation['task_id'], 'operation.session_retired',
                    {'operation_id': operation['operation_id'], 'status': 'failed',
                     'outcome': 'unknown', 'evidence_ref': reference, 'request_task_id': task_id},
                    event_key='retired:'+operation['operation_id']+':'+digest,
                    revision=operation['revision'], epoch=operation['epoch'])
                recovered.append(operation['operation_id'])
                references.append(copies[task_id])
        if workflow.pending_operations():
            return result('pending', 'prior_live_inputs_require_receipt')
        health = supervisor.health()
        if recovered and (health.get('unknown') or health.get('revoked')):
            check()
            supervisor.reconcile({'reconciled': True, 'evidence_ref': references[-1],
                'kind': 'exited_maa_session', 'task_id': task_id}, resume=True)
        return result('reconciled', 'prior_sessions_retired')
    except (ActionRejected, KeyError, ValueError, TypeError, OSError, RuntimeError):
        return result('blocked', 'input_recovery_revoked_or_unverified')

"""Read-only reconciliation of committed queue inputs from prior task epochs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import threading
import time

from . import maa_queue_receipt as receipts
from .core import ActionRejected
from .maa_queue_store import QueueStore
from .maa_visual_import import VisualCodeImporter, VisualImportError
from .task_cleanup import TaskCleanup
from .workflow_store import _encode


def _result(status, code, recovered, references, **facts):
    return {'status':status,'code':code,'recovered_operation_ids':recovered,
            'evidence_refs':references,'gui_inputs':0,**facts}


class _RecoveryPending(RuntimeError):
    pass


def _helper_exited(health):
    return (health.get('helper_exit_confirmed') is True and not health.get('child_alive')
            and not health.get('pending') and not health.get('stop_pending'))


def recover_queue_inputs(workflow, supervisor, readback, *, task_id, guard, request_task_id=None,
                         cancel_event=None, timeout_seconds=45):
    """Read current evidence for this task's existing inputs, without replaying them.

    The caller supplies the current task/permission guard. Original epoch data
    is consulted only to settle an already committed effect. No run, item,
    dispatch or ordinary action is created for that old epoch.
    """
    # The request owns permission; task_id owns the historical input/evidence.
    # A new task may reconcile an old effect, never resume the old task.
    request_task_id = request_task_id or task_id
    recovered, references = [], []
    if (type(timeout_seconds) not in (int,float) or not 0 < timeout_seconds <= 45
            or not isinstance(task_id,str) or not task_id):
        raise ValueError('invalid queue recovery limits')
    cancel = cancel_event if cancel_event is not None else threading.Event()
    journal = QueueStore(workflow)
    cleanup = TaskCleanup(workflow)
    started = time.monotonic()
    current_scope = None
    old_helper_exited = False

    def check():
        nonlocal current_scope
        if cancel.is_set():
            raise ActionRejected('queue recovery cancelled')
        guard()
        workflow.management.require_active(request_task_id)
        current = workflow.current(request_task_id)
        if not current['writes_enabled'] or current['status'] in {'paused','cancelled','completed','failed','needs_input'}:
            raise ActionRejected('queue recovery task revoked')
        scope = tuple(current[key] for key in ('task_id','revision','epoch','control_seq'))
        if current_scope is not None and current_scope != scope:
            raise ActionRejected('queue recovery owner changed')
        current_scope = scope
        return current

    def remaining():
        check()
        value = timeout_seconds-(time.monotonic()-started)
        if value <= 0:
            raise _RecoveryPending('queue_recovery_timeout')
        return value

    @contextmanager
    def publish_guard(directory):
        # Only bounded local file IO is inside this gate. Native capture and
        # network vision happen outside it, so cancellation can revoke them.
        with workflow.engine._lock:
            check()
            before = set(directory.iterdir())
            try:
                yield
            finally:
                check()
                for path in set(directory.iterdir())-before:
                    if path.is_file() and re.fullmatch(r'maa-queue-[a-z-]+-[0-9a-f]{64}\.json',path.name):
                        raw = receipts._no_links(path).read_bytes()
                        if hashlib.sha256(raw).hexdigest() != path.stem.rsplit('-',1)[-1]:
                            raise ValueError('queue recovery evidence changed')
                        cleanup.register_file(task_id,path)

    def observe(binding, directory):
        capture_started_ns = int(time.time()*1e9)
        answer = supervisor.request({'op':'snapshot'},timeout=min(12,remaining()),cancel_event=cancel)
        check()
        if answer.get('status') != 'completed':
            raise _RecoveryPending('queue_recovery_capture_failed')
        frame = answer['result']
        identifier = frame['screenshot_id']
        native_path = Path(supervisor.directory).resolve()/'maa-visual-captures'/(identifier+'.png')
        if (not isinstance(identifier,str) or not re.fullmatch('[0-9a-f]{32}',identifier)
                or Path(frame['image_path']) != native_path
                or any(frame['window'].get(key) != supervisor.target_identity[key]
                       for key in ('hwnd','pid','creation_time','executable'))
                or frame.get('method') not in {'win32_printwindow','windows_graphics_capture'}):
            raise _RecoveryPending('queue_recovery_capture_invalid')
        raw, file_state = receipts._read_stable(native_path)
        geometry = frame['window']['client_rect']
        if (len(raw)<24 or raw[:8] != b'\x89PNG\r\n\x1a\n' or raw[12:16] != b'IHDR'
                or struct.unpack('>II',raw[16:24]) != (geometry['width'],geometry['height'])
                or hashlib.sha256(raw).hexdigest() != frame['image_sha256']
                or not capture_started_ns-2_000_000 <= file_state['mtime_ns'] <= int(time.time()*1e9)+2_000_000):
            raise _RecoveryPending('queue_recovery_capture_invalid')
        image_path = receipts._no_links(directory/('recovery-frame-'+identifier+'.png'))
        with workflow.engine._lock:
            check()
            with image_path.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.utime(image_path,ns=(file_state['mtime_ns'],file_state['mtime_ns']))
            cleanup.register_file(task_id,image_path)
            native_ref = {'image_path':str(native_path),'image_sha256':frame['image_sha256'],
                          'screenshot_id':identifier}
            native_index = receipts._publish(directory,'maa-queue-rec-capture',native_ref)
            cleanup.register_file(task_id,native_index['path'])
            references.append(native_index)
        image_ref = {'path':str(image_path),'sha256':frame['image_sha256']}
        with publish_guard(directory):
            snapshot = receipts.capture_queue_snapshot(binding,directory,
                screenshot_refs=[image_ref],visible_complete=False)
        observer = VisualCodeImporter(supervisor,guard=check,readback=readback)
        view = observer._interpret(frame,{'projection_sha256':snapshot['projection_sha256'],
            'rows':snapshot['rows']},cancel,min(remaining(),45))
        check()
        if (view.get('schema') != 'maa_queue_vision/v1' or view.get('ok') is not True
                or view.get('page') != 'copilot' or view.get('queue_mode') is not True
                or view.get('idle') is not True
                or view.get('running') is not False
                or view.get('image_sha256') != frame['image_sha256']
                or (view.get('width'),view.get('height')) != (geometry['width'],geometry['height'])
                or view.get('projection_sha256') != snapshot['projection_sha256']):
            raise _RecoveryPending('queue_recovery_visual_unverified')
        if view.get('visible_complete') is not True:
            # Prior-epoch permission can settle a committed effect, never
            # authorize new scrolling to discover hidden queue members.
            raise _RecoveryPending('queue_recovery_full_visual_required')
        with publish_guard(directory):
            complete = receipts.capture_queue_snapshot(binding,directory,
                screenshot_refs=[image_ref],visible_complete=True)
        if (complete['projection_sha256'] != snapshot['projection_sha256']
                or complete['rows'] != snapshot['rows']):
            raise _RecoveryPending('queue_recovery_projection_changed')
        return image_ref,complete

    def settle_observation(operation, run, binding, directory):
        action, operation_guard = operation['action'],operation['action']['queue_guard']
        parent = workflow.get_operation(action['queue_observation_of'])
        prior = parent['action'].get('queue_guard') or {}
        if (parent['status'] != 'verified' or not parent.get('submitted_at')
                or parent['action'].get('kind') != 'queue_gui'
                or prior.get('phase') not in {'code','add','delete'}
                or not parent['effect_key'].startswith('queue:') or parent['starts'] or parent['stop_only']
                or operation_guard.get('target_key') is not None or operation_guard.get('row_identity') is not None
                or prior.get('plan_sha256') != run['plan']['plan_sha256']
                or any(parent[key] != operation[key] for key in ('task_id','revision','epoch','dispatch_id','slot'))
                or (parent.get('result') or {}).get('queue_verification') is not True):
            raise _RecoveryPending('queue_recovery_observation_parent_unverified')
        parent_ref = parent['result']['evidence_ref']
        parent_proof = journal._private_ref(operation,parent_ref,decode=True)
        # Revalidate the exact trusted parent receipt. This is idempotent for a
        # verified operation and cannot establish an unrelated source effect.
        with workflow.engine._lock:
            check()
            journal.record_reconciliation(task_id,original_epoch=parent['epoch'],
                operation_id=parent['operation_id'],evidence_ref=parent_ref,result={'status':'verified'})
        if not old_helper_exited:
            raise _RecoveryPending('queue_recovery_native_pending')
        image_ref,snapshot = observe(binding,directory)
        parent_snapshot = parent_proof['current_snapshot']
        if (snapshot['projection_sha256'] != parent_snapshot['projection_sha256']
                or snapshot['rows'] != parent_snapshot['rows']):
            raise _RecoveryPending('queue_recovery_projection_changed')
        payload = {'schema':'maa_queue_observation_recovery/v1',
            'operation':{key:operation[key] for key in
                         ('operation_id','task_id','revision','epoch','dispatch_id','payload_hash','submitted_at')},
            'parent_operation_id':parent['operation_id'],'parent_receipt_ref':parent_ref,
            'parent_result_sha256':_encode(parent['result'])[1],'current_snapshot':snapshot,
            'old_helper_exit_confirmed':True,'gui_inputs':0}
        with publish_guard(directory):
            reference = receipts._publish(directory,'maa-queue-obs-recovery',payload)
            references.append(reference)
        with workflow.engine._lock:
            check()
            if workflow.get_operation(operation['operation_id']) != operation or workflow.get_operation(parent['operation_id']) != parent:
                raise ActionRejected('queue recovery source changed')
            workflow.finish_operation(operation['operation_id'],'verified',
                {'evidence_ref':reference,'queue_observation_verified':True,
                 'parent_operation_id':parent['operation_id'],'gui_inputs':0})

    try:
        check()
        pending = workflow.pending_operations(task_id)
        if any((op['action'].get('queue_guard') or {}).get('phase')=='scroll' for op in pending):
            # A later snapshot may create a new read-only helper. Pin the old
            # input worker's exit before any captures, not after that restart.
            old_helper_exited = _helper_exited(supervisor.health())
        # Mutations settle first, regardless of a stale checkpoint's ordering.
        pending.sort(key=lambda op:(op['action'].get('queue_guard') or {}).get('phase')=='scroll')
        for operation in pending:
            check()
            action = operation.get('action') or {}
            operation_guard = action.get('queue_guard') or {}
            phase = operation_guard.get('phase')
            if (operation['status'] not in {'submitted','unknown'} or not operation.get('submitted_at')
                    or not operation['effect_key'].startswith('queue:') or action.get('kind') != 'queue_gui'
                    or phase not in {'code','add','delete','scroll'}
                    or (phase=='scroll' and not action.get('queue_observation_of'))):
                raise _RecoveryPending('queue_recovery_effect_unsupported')
            with workflow.engine._lock:
                check()
                row = workflow._db.execute('SELECT payload,payload_hash FROM workflow_queue_runs '
                    'WHERE task_id=? AND revision=? AND epoch=?',
                    (task_id,operation['revision'],operation['epoch'])).fetchone()
                run = json.loads(row['payload']) if row else None
                if not run or _encode(run)[1] != row['payload_hash'] or not run.get('baseline'):
                    raise _RecoveryPending('queue_recovery_run_missing')
            old_context = {key:operation[key] for key in ('task_id','revision','epoch')}
            if (operation_guard.get('plan_sha256') != run['plan']['plan_sha256']
                    or any(operation_guard.get(key) != operation[key]
                           for key in ('task_id','revision','epoch','dispatch_id'))
                    or operation_guard.get('snapshot_sha256') != action['snapshot_ref']['sha256']):
                raise _RecoveryPending('queue_recovery_operation_mismatch')
            original_snapshot = journal._read_snapshot(old_context,action['snapshot_ref'])
            directory = Path(action['snapshot_ref']['path']).parent
            binding = receipts.discover_queue_binding(supervisor.target_identity)
            if binding != run['baseline']['binding'] or binding != original_snapshot.get('binding'):
                raise _RecoveryPending('queue_recovery_binding_changed')
            if phase=='scroll':
                settle_observation(operation,run,binding,directory)
                recovered.append(operation['operation_id'])
                continue
            baseline_ref = action.get('input_baseline_ref' if phase=='code' else 'effect_baseline_ref')
            baseline = {**journal._private_ref(old_context,baseline_ref,decode=True),'evidence_ref':baseline_ref}
            if (baseline.get('schema') != ('maa_queue_input_baseline/v1' if phase=='code' else 'maa_queue_effect_baseline/v1')
                    or baseline.get('snapshot',{}).get('evidence_ref') != action.get('snapshot_ref')
                    or baseline.get('binding') != binding):
                raise _RecoveryPending('queue_recovery_baseline_mismatch')
            image_ref,snapshot = observe(binding,directory)
            if phase=='code':
                targets = [target for target in run['plan']['targets']
                           if target.get('target_key') == operation_guard.get('target_key')]
                if len(targets) != 1:
                    raise _RecoveryPending('queue_recovery_target_mismatch')
                committed = {key:operation[key] for key in
                             ('operation_id','task_id','revision','epoch','submitted_at','payload_hash')}
                committed.update(baseline=baseline,document_ref=targets[0]['document_ref'])
                with publish_guard(directory):
                    observed = receipts.collect_queue_recovery_receipt(committed,binding,directory,
                        screenshot_refs=[image_ref],visible_complete=True)
            else:
                # Deletion/absence cannot be inferred from one frame. Never
                # replay the historical click, even if its result is missing.
                if cancel.wait(min(.12,remaining())):
                    check()
                image_ref,current = observe(binding,directory)
                submitted = datetime.fromisoformat(operation['submitted_at'])
                if submitted.tzinfo is None:
                    raise _RecoveryPending('queue_recovery_submission_invalid')
                with publish_guard(directory):
                    observed = receipts.collect_queue_effect_receipt(baseline,binding,directory,
                        previous_snapshot=snapshot,screenshot_refs=[image_ref],visible_complete=True,
                        operation_id=operation['operation_id'],submitted_at=submitted.timestamp())
                snapshot = current
            if observed.get('receipt_ref'):
                references.append(observed['receipt_ref'])
            if observed.get('verified') is not True or not observed.get('receipt_ref'):
                raise _RecoveryPending(observed.get('code') or 'queue_recovery_effect_unverified')
            proof = journal._private_ref(old_context,observed['receipt_ref'],decode=True)
            if (proof['current_snapshot']['projection_sha256'] != snapshot['projection_sha256']
                    or proof['current_snapshot']['rows'] != snapshot['rows']):
                raise _RecoveryPending('queue_recovery_projection_changed')
            with workflow.engine._lock:
                check()
                journal.record_reconciliation(task_id,original_epoch=operation['epoch'],
                    operation_id=operation['operation_id'],evidence_ref=observed['receipt_ref'],result=observed)
                recovered.append(operation['operation_id'])
        check()
        if workflow.pending_operations(task_id):
            return _result('pending','queue_recovery_remaining_effects',recovered,references)
        if recovered and not workflow.pending_operations():
            health = supervisor.health()
            if health.get('revoked') or health.get('unknown'):
                # The completed recovery snapshot already acquired the
                # exclusive desktop/helper locks. Its new read-only child may
                # remain alive; that is not the abandoned input worker.
                if health.get('pending') or health.get('stop_pending') or health.get('closed'):
                    return _result('pending','queue_recovery_native_pending',recovered,references)
                supervisor.reconcile({'reconciled':True,'evidence_ref':references[-1],
                    'kind':'verified_queue_inputs','task_id':task_id},resume=True)
        return _result('reconciled','queue_inputs_reconciled',recovered,references)
    except _RecoveryPending as error:
        return _result('pending',str(error),recovered,references)
    except (ActionRejected,KeyError,ValueError,TypeError,OSError,RuntimeError,VisualImportError):
        return _result('blocked','queue_recovery_revoked_or_unverified',recovered,references)

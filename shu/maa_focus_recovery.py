"""Repair an old focus-only worker fence from explicit, verified local evidence.

Never infer that a submitted game/MAA content action succeeded. The source
profile is read-only and keeps its original unknown operation unchanged.
"""
from __future__ import annotations

import hashlib
import ctypes
import json
import math
import ntpath
from pathlib import Path
import re
import sqlite3
import struct
import threading
import time
from contextlib import closing

from .core import ActionRejected
from . import maa_queue_receipt as receipts
from .task_cleanup import TaskCleanup
from .workflow_store import _encode


def process_identity_retired(identity,*,native_backend=None):
    """Read-only Win32 proof of exit/PID reuse; access denied is not exit."""
    if (not isinstance(identity,dict) or type(identity.get('pid')) is not int or identity['pid']<=0
            or type(identity.get('creation_time')) is not int or identity['creation_time']<=0):
        return False
    try:
        from ctypes import wintypes as w
        if native_backend is None:
            from .maa_visual_native import _NativeMaaBackend
            native_backend=_NativeMaaBackend()
        kernel=native_backend.native.kernel
        kernel.GetExitCodeProcess.argtypes=[w.HANDLE,ctypes.POINTER(w.DWORD)]
        kernel.GetExitCodeProcess.restype=w.BOOL
        ctypes.set_last_error(0)
        process=kernel.OpenProcess(0x1000,False,identity['pid'])
        # ERROR_INVALID_PARAMETER is the documented non-existing PID result;
        # access denied/unknown are deliberately never treated as retirement.
        if not process:return ctypes.get_last_error()==87
        try:
            code=w.DWORD()
            if kernel.GetExitCodeProcess(process,ctypes.byref(code)) and code.value!=259:
                return True
            created,exited,cpu_kernel,cpu_user=(w.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(process,ctypes.byref(created),ctypes.byref(exited),
                    ctypes.byref(cpu_kernel),ctypes.byref(cpu_user)):
                return False
            value=(int(created.dwHighDateTime)<<32)|int(created.dwLowDateTime)
            return value>0 and value!=identity['creation_time']
        finally:kernel.CloseHandle(process)
    except (OSError,RuntimeError,TypeError,AttributeError,ValueError):
        return False


def _hash_file(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def _source_receipt(prior_profile, maa_identity, game_identity, retired_identity=None):
    profile=receipts._no_links(Path(prior_profile).absolute()).resolve(strict=True)
    database=receipts._no_links(profile/'tasks.sqlite3')
    journal=receipts._no_links(profile/'tasks.sqlite3-wal')
    if journal.exists() and journal.stat().st_size:
        raise ActionRejected('source database still has an uncheckpointed journal')
    before=_hash_file(database)
    # The old process has exited and its WAL is empty. Immutable read-only mode
    # avoids creating/changing its journal/SHM sidecars during recovery.
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN')
        # A source profile with any actual start is outside this narrowly known
        # failure. Its global fence must use the ordinary effect settlement.
        if db.execute('SELECT 1 FROM workflow_operations WHERE starts<>0 AND submitted_at IS NOT NULL LIMIT 1').fetchone():
            raise ActionRejected('source profile contains a battle start')
        rows=db.execute("SELECT * FROM workflow_operations WHERE status IN ('planned','issued','submitted','unknown') LIMIT 2").fetchall()
        if len(rows)!=1:raise ActionRejected('source focus failure is not unique')
        operation=dict(rows[0]);action=json.loads(operation['action']);result=json.loads(operation['result'] or '{}')
        if (operation['status']!='unknown' or operation['submitted_at'] is None
                or operation['starts'] or operation['stop_only']
                or action.get('kind')!='battle_navigation' or action.get('action')!='focus'
                or action.get('surface') not in {'maa','game'}
                or set(action)!={'kind','action','surface','maa_identity','game_identity'}
                or _encode(action)[1]!=operation['payload_hash']
                or result.get('input_dispatched') is not False):
            raise ActionRejected('not the known focus-only source failure')
        retired=[]
        for name,current in (('maa_identity',maa_identity),('game_identity',game_identity)):
            old=action[name]
            if (not isinstance(old,dict) or set(old)!={'hwnd','pid','creation_time','executable'}
                    or any(type(old.get(k)) is not int or old[k]<=0 for k in ('hwnd','pid','creation_time'))
                    or not isinstance(old.get('executable'),str) or not ntpath.isabs(old['executable'])
                    or ntpath.normcase(old['executable'])!=ntpath.normcase(current['executable'])):
                raise ActionRejected('source focus process identity is invalid')
            if old!=current:
                if retired_identity is None or retired_identity(old) is not True:
                    raise ActionRejected('original focus process has not been proven retired')
                retired.append(old)
        state=db.execute('SELECT revision,epoch,writes_enabled FROM workflow_tasks WHERE task_id=?',
                         (operation['task_id'],)).fetchone()
        if state is None or state['writes_enabled']:
            raise ActionRejected('source task is still authorized')
        reference=result.get('evidence_ref') or {}
        path=receipts._no_links(Path(reference['path']).absolute()).resolve(strict=True)
        private=profile/'task-private'/operation['task_id']
        if not path.is_relative_to(private):raise ActionRejected('source evidence is outside its owner')
        raw,_=receipts._read_stable(path,maximum=128*1024)
        if hashlib.sha256(raw).hexdigest()!=reference.get('sha256'):
            raise ActionRejected('source evidence hash changed')
        proof=json.loads(raw.decode('utf-8'))
        response=proof.get('response') or {}
        if (proof.get('operation_id')!=operation['operation_id']
                or proof.get('args')!={'action':'focus','surface':action['surface']}
                or response.get('status')!='unknown' or response.get('error')!='focus_denied'
                or response.get('helper_exit_confirmed') is not True
                or response.get('submission_entered') is not True
                or response.get('result') is not None):
            raise ActionRejected('source failure receipt does not prove a denied focus')
        db.commit()
    after=_hash_file(database)
    if before!=after or journal.exists() and journal.stat().st_size:
        raise ActionRejected('source profile changed during recovery read')
    # Store only immutable identity/hash metadata. Do not make the new task
    # depend on another task's private evidence files or prohibit their purge.
    return {'source_profile':str(profile),'source_database_sha256':before,
            'source_operation_id':operation['operation_id'],'source_task_id':operation['task_id'],
            'source_revision':operation['revision'],'source_epoch':operation['epoch'],
            'source_payload_hash':operation['payload_hash'],'source_receipt_sha256':reference['sha256'],
            'focus_surface':action['surface'],'source_business_outcome':'unknown',
            'retired_source_identities':retired,
            'known_failure':'focus_denied_before_content_input'}


def _frame_witness(workflow, supervisor, frame, identity, *, surface):
    if (any(frame.get('window',{}).get(key)!=identity[key]
            for key in ('hwnd','pid','creation_time','executable'))
            or frame.get('method') not in {'win32_printwindow','windows_graphics_capture'}):
        raise ActionRejected('recovery snapshot identity or transport changed')
    path=receipts._no_links(Path(frame['image_path']).absolute()).resolve(strict=True)
    directory=Path(supervisor.directory).resolve()
    if not path.is_relative_to(directory):raise ActionRejected('recovery image outside the bound profile')
    raw,_=receipts._read_stable(path,maximum=32*1024*1024)
    workflow._battle_reference({'path':str(path),'sha256':frame.get('image_sha256')},image=True)
    rect=frame['window']['client_rect']
    if (len(raw)<24 or raw[:8]!=b'\x89PNG\r\n\x1a\n' or raw[12:16]!=b'IHDR'
            or struct.unpack('>II',raw[16:24])!=(rect['width'],rect['height'])
            or hashlib.sha256(raw).hexdigest()!=frame.get('image_sha256')):
        raise ActionRejected('recovery image bytes changed')
    return {'surface':surface,'window':frame['window'],'screenshot_id':frame['screenshot_id'],
            'image_sha256':frame['image_sha256'],'method':frame['method']}


def _worker_fence(supervisor,expected):
    if not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected):
        raise ActionRejected('legacy focus fence requires an explicit trusted state binding')
    raw,_=receipts._read_stable(supervisor._state_path,maximum=16*1024)
    if hashlib.sha256(raw).hexdigest()!=expected:
        raise ActionRejected('legacy worker fence changed')
    value=receipts._json(raw)
    if (not isinstance(value,dict) or set(value)!={'unknown','revoked','reason','updated_at'}
            or value['unknown'] is not True or value['revoked'] is not True
            or value['reason'] not in {'focus_denied','submission_unconfirmed'}
            or type(value['updated_at']) not in (int,float) or not math.isfinite(value['updated_at'])):
        raise ActionRejected('legacy worker fence has an unsupported shape')
    return {'state_sha256':expected,'state':value,
            'binding_authority':'explicit_trusted_acceptance_parameter',
            'attribution':'legacy_state_has_no_operation_id_not_inferred_from_time'}


def reconcile_known_focus_failure(workflow,supervisor,*,context,guard,prior_profile,
                                  game_identity,cancel_event=None,expected_worker_state_sha256=None,
                                  retired_identity=None):
    """Retire one legacy focus fence explicitly attributed by a trusted caller.

    The old state format contains no operation ID. The supplied SHA is a manual
    binding after independent receipt review, never something an agent/model
    may derive from an arbitrary currently blocked worker. Without it, fail
    closed. Two fresh read-only captures must leave that exact fence unchanged.
    """
    cancel=cancel_event or threading.Event();scope=None

    def check():
        nonlocal scope
        if cancel.is_set():raise ActionRejected('focus recovery cancelled')
        guard()
        state=workflow.current(context['task_id'])
        identity=tuple(state[key] for key in ('task_id','revision','epoch','control_seq'))
        if (state['revision']!=context['revision'] or state['epoch']!=context['epoch']
                or not state['writes_enabled'] or state['contract'].get('workflow_scope')!='maa_battle_start'
                or scope is not None and scope!=identity):
            raise ActionRejected('focus recovery request changed')
        workflow.management.require_active(context['task_id'])
        scope=identity

    try:
        check();health=supervisor.health()
        if not health.get('unknown') and not health.get('revoked'):
            return {'status':'not_needed','code':'worker_open','gui_inputs':0,'evidence_refs':[]}
        reasons={'focus_denied','submission_unconfirmed'}
        if (health.get('reason') not in reasons or health.get('helper_exit_confirmed') is not True
                or health.get('child_alive') or health.get('pending') or health.get('stop_pending')
                or health.get('closed') or workflow.pending_operations()):
            raise ActionRejected('not a recoverable isolated focus fence')
        with supervisor._gate:
            supervisor._own_slot()
            binding=_worker_fence(supervisor,expected_worker_state_sha256)
        source=_source_receipt(prior_profile,supervisor.target_identity,game_identity,retired_identity)
        witnesses=[]
        for surface,identity in (('maa',supervisor.target_identity),('game',game_identity)):
            check()
            reply=supervisor.request({'op':'snapshot','args':{'surface':surface}},timeout=12,cancel_event=cancel)
            check()
            if reply.get('status')!='completed':raise ActionRejected('focus recovery snapshot unavailable')
            witnesses.append(_frame_witness(workflow,supervisor,reply['result'],identity,surface=surface))
        health=supervisor.health()
        if (health.get('reason') not in reasons or not health.get('unknown')
                or health.get('pending') or health.get('stop_pending') or health.get('closed')):
            raise ActionRejected('worker changed while reading focus evidence')
        # The successful snapshots acquired the exclusive two-byte desktop
        # lease. Their new read-only helper may be alive; the original input
        # helper is no longer a possible concurrent mouse writer.
        check()
        if _source_receipt(prior_profile,supervisor.target_identity,game_identity,retired_identity)!=source:
            raise ActionRejected('source focus proof changed')
        payload={'schema':'maa_known_focus_fence_recovery/v1',
            **{key:context[key] for key in ('task_id','revision','epoch')},
            'original_worker_reason':health['reason'],'source':source,'observations':witnesses,
            'trusted_legacy_fence_binding':binding,
            'gui_inputs':0,'source_business_outcome':'unknown','captured_at':time.time()}
        directory=workflow.engine.data_dir/'task-private'/context['task_id']/'maa-input-recovery'
        reference=receipts._publish(directory,'maa-known-focus-recovery',payload)
        cleanup=TaskCleanup(workflow)
        with workflow.engine._transaction():
            check()
            if workflow.pending_operations():raise ActionRejected('current profile gained a pending input')
            private=cleanup._safe_path(context['task_id'],reference['path'])
            workflow._db.execute('INSERT OR IGNORE INTO workflow_cleanup_files(task_id,path,sha256) VALUES(?,?,?)',
                (context['task_id'],str(private),reference['sha256']))
        check()
        with supervisor._gate:
            check()
            final=supervisor.health()
            if (final.get('pending') or final.get('stop_pending') or final.get('closed')
                    or supervisor._current is not None or workflow.pending_operations()
                    or _worker_fence(supervisor,expected_worker_state_sha256)!=binding):
                raise ActionRejected('worker changed before focus-fence retirement')
            supervisor.reconcile({'reconciled':True,'evidence_ref':reference,
                'kind':'verified_focus_only_failure','task_id':context['task_id'],'epoch':context['epoch']},resume=True)
        return {'status':'reconciled','code':'known_focus_fence_recovered','gui_inputs':0,
                'evidence_refs':[reference]}
    except (ActionRejected,ValueError,KeyError,TypeError,OSError,RuntimeError,sqlite3.Error):
        return {'status':'blocked','code':'known_focus_recovery_unverified','gui_inputs':0,'evidence_refs':[]}

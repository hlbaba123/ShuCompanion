"""Durable application protocol in TaskEngine's existing SQLite transaction.

Graph checkpoints are a separate consumer: inbox acceptance is a receipt, not
proof that a checkpoint committed. GUI admission is another short transaction;
no GUI or model call belongs inside any transaction in this module.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import struct
from typing import Any
import uuid
import zlib

from .core import ActionRejected, TaskEngine, TaskNotFound, _nonempty, _utc_now


_FORBIDDEN_FIELDS = frozenset({
    "api_key", "apikey", "authorization", "password", "secret", "access_token",
    "refresh_token", "reasoning", "reasoning_content", "messages", "raw_response",
    "image_url", "screenshot_bytes", "credentials",
})
_DISPATCH_TERMINAL = frozenset({"completed", "failed", "unknown", "interrupted"})
_OPERATION_TERMINAL = frozenset({"verified", "not_issued", "unknown", "failed"})
_RESEARCH_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def _process_identity(pid: int) -> str | None:
    """Return OS creation identity; None proves absence, errors prove nothing."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        handle = kernel.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error in (87, 1168):
                return None
            raise OSError(error, 'Cannot establish process identity')
        try:
            waited = kernel.WaitForSingleObject(handle, 0)
            if waited == 0:  # Exited object may still exist while another handle is open.
                return None
            if waited != 0x102:
                raise ctypes.WinError(ctypes.get_last_error())
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                raise ctypes.WinError(ctypes.get_last_error())
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    # Linux's boot id and start ticks distinguish process creation and reboot.
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return boot + ':' + fields[19]
    except FileNotFoundError:
        return None


def _encode(value: Any) -> tuple[str, str]:
    """Reject non-JSON objects and sensitive/raw transport fields before disk."""
    def check(item: Any, depth: int = 0) -> None:
        if depth > 24:
            raise ValueError("workflow JSON is too deeply nested")
        if item is None or type(item) in (bool, int):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("workflow JSON requires finite numbers")
            return
        if type(item) is str:
            if item.lstrip().lower().startswith(("data:", "bearer ")):
                raise ValueError("media/credential transport values cannot be persisted")
            return
        if type(item) is list:
            for child in item:
                check(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise TypeError("workflow JSON object keys must be strings")
                if key.casefold().replace("-", "_") in _FORBIDDEN_FIELDS:
                    raise ValueError(f"sensitive workflow field: {key}")
                check(child, depth + 1)
            return
        raise TypeError("workflow state accepts JSON primitives only")
    check(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 512_000:
        raise ValueError("workflow JSON is too large; publish an artifact reference")
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class WorkflowStore:
    """Atomic contracts, replayable results and conservative GUI effect fences.

    The engine owns connection lifetime. Multiple stores/connections may share
    tasks.sqlite3; every write uses the engine's BEGIN IMMEDIATE transaction.
    Construction does not grant or revoke authority. The process coordinator
    explicitly revokes persisted authority before recovery on actual startup.
    """

    def __init__(self, engine: TaskEngine):
        self.engine = engine
        self._db = engine._db
        statements = (
            """CREATE TABLE IF NOT EXISTS workflow_contracts (
                task_id TEXT NOT NULL REFERENCES tasks(id), revision INTEGER NOT NULL,
                command_id TEXT NOT NULL UNIQUE, goal TEXT NOT NULL, contract TEXT NOT NULL,
                payload_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(task_id,revision))""",
            """CREATE TABLE IF NOT EXISTS workflow_tasks (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id), revision INTEGER NOT NULL,
                epoch INTEGER NOT NULL, writes_enabled INTEGER NOT NULL,
                control_seq INTEGER NOT NULL DEFAULT 0, control_kind TEXT NOT NULL DEFAULT 'submit')""",
            """CREATE TABLE IF NOT EXISTS workflow_commands (
                command_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                kind TEXT NOT NULL, revision INTEGER NOT NULL, epoch INTEGER NOT NULL,
                payload TEXT NOT NULL, payload_hash TEXT NOT NULL, created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS workflow_request_receipts (
                request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                operation TEXT NOT NULL, payload_hash TEXT NOT NULL,
                revision INTEGER NOT NULL, epoch INTEGER NOT NULL, created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS workflow_outbox (
                event_id TEXT PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
                task_id TEXT REFERENCES tasks(id), revision INTEGER NOT NULL, epoch INTEGER NOT NULL,
                type TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
                acked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS workflow_inbox (
                consumer TEXT NOT NULL, event_id TEXT NOT NULL REFERENCES workflow_outbox(event_id),
                payload_hash TEXT NOT NULL, accepted_at TEXT NOT NULL, PRIMARY KEY(consumer,event_id))""",
            """CREATE TABLE IF NOT EXISTS workflow_dispatches (
                dispatch_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                revision INTEGER NOT NULL, epoch INTEGER NOT NULL, role TEXT NOT NULL,
                dispatch_key TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', owner TEXT, created_at TEXT NOT NULL,
                UNIQUE(task_id,dispatch_key))""",
            """CREATE TABLE IF NOT EXISTS workflow_results (
                dispatch_id TEXT PRIMARY KEY REFERENCES workflow_dispatches(dispatch_id),
                result TEXT NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS workflow_runners (
                owner TEXT PRIMARY KEY, pid INTEGER NOT NULL, process_identity TEXT NOT NULL,
                registered_at TEXT NOT NULL, released_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS workflow_research_artifacts (
                dispatch_id TEXT PRIMARY KEY REFERENCES workflow_dispatches(dispatch_id),
                references_json TEXT NOT NULL, payload_hash TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS workflow_research_adoptions (
                dispatch_id TEXT PRIMARY KEY REFERENCES workflow_dispatches(dispatch_id),
                source_dispatch_id TEXT NOT NULL REFERENCES workflow_dispatches(dispatch_id),
                source_result_hash TEXT NOT NULL, input_hash TEXT NOT NULL, created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS workflow_slots (
                slot TEXT PRIMARY KEY, barrier_operation_id TEXT,
                attempts_reserved INTEGER NOT NULL DEFAULT 0,
                control_seq INTEGER NOT NULL DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS workflow_operations (
                operation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                dispatch_id TEXT REFERENCES workflow_dispatches(dispatch_id),
                revision INTEGER NOT NULL, epoch INTEGER NOT NULL,
                control_seq INTEGER NOT NULL, slot_seq INTEGER NOT NULL,
                effect_key TEXT NOT NULL, action TEXT NOT NULL, payload_hash TEXT NOT NULL,
                slot TEXT NOT NULL REFERENCES workflow_slots(slot), starts INTEGER NOT NULL,
                stop_only INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'planned',
                issued_at TEXT, submitted_at TEXT, result TEXT, created_at TEXT NOT NULL,
                UNIQUE(slot,effect_key))""",
            "CREATE INDEX IF NOT EXISTS workflow_events_pending ON workflow_outbox(acked,task_id)",
            "CREATE INDEX IF NOT EXISTS workflow_operations_task ON workflow_operations(task_id,status)",
        )
        with engine._transaction():
            # executescript would implicitly commit and break the shared boundary.
            for statement in statements:
                self._db.execute(statement)
            self._ensure_slot("desktop-maa")
        from .task_management import TaskManagement
        self.management = TaskManagement(self)

    def _ensure_slot(self, slot: str) -> sqlite3.Row:
        self._db.execute("INSERT OR IGNORE INTO workflow_slots(slot) VALUES(?)", (slot,))
        return self._db.execute("SELECT * FROM workflow_slots WHERE slot=?", (slot,)).fetchone()

    def _state(self, task_id: str) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT w.*,t.goal,t.status,t.version AS task_epoch,c.contract,c.payload_hash AS contract_hash "
            "FROM workflow_tasks w JOIN tasks t ON t.id=w.task_id "
            "JOIN workflow_contracts c ON c.task_id=w.task_id AND c.revision=w.revision "
            "WHERE w.task_id=?", (task_id,),
        ).fetchone()
        if row is None:
            raise TaskNotFound(f"找不到工作流任务：{task_id}")
        result = dict(row)
        result["contract"] = json.loads(result["contract"])
        if _encode(result["contract"])[1] != result.pop("contract_hash"):
            raise ValueError("workflow contract payload hash mismatch; read-only reconciliation required")
        result["writes_enabled"] = bool(result["writes_enabled"]) and result["epoch"] == result["task_epoch"]
        return result

    def current(self, task_id: str) -> dict[str, Any]:
        with self.engine._lock:
            self.engine._ensure_open()
            return self._state(task_id)

    def research_results(self, task_id):
        """Current-epoch durable outputs for product delivery and follow-ups."""
        with self.engine._lock:
            state=self._state(task_id)
            rows=self._db.execute("SELECT d.dispatch_id FROM workflow_dispatches d JOIN workflow_results r "
                "ON r.dispatch_id=d.dispatch_id WHERE d.task_id=? AND d.role='research' AND d.epoch=? "
                "ORDER BY d.rowid DESC LIMIT 32",(task_id,state['epoch'])).fetchall()
            return [self.dispatch_result(row['dispatch_id']) for row in rows]

    def research_display_results(self, task_id):
        """Read-only delivery view; never makes an old dispatch current again.

        A needs-input suspension alone advances the epoch after its results
        have committed. That immediately preceding run may still be displayed.
        Stop, pause, resume and contract replacement do not inherit this view.
        """
        with self.engine._lock:
            state=self._state(task_id)
            source_epoch=state['epoch']
            if state['status']=='needs_input' and state['control_kind'] in {'needs_input', 'blocked'}:
                source_epoch-=1
            elif state['status'] in {'paused','cancelled','failed'}:
                return []
            rows=self._db.execute("SELECT d.dispatch_id FROM workflow_dispatches d JOIN workflow_results r "
                "ON r.dispatch_id=d.dispatch_id WHERE d.task_id=? AND d.role='research' AND d.epoch=? "
                "AND d.revision=? AND d.status='completed' AND r.status='completed' "
                "ORDER BY d.rowid DESC LIMIT 32",(task_id,source_epoch,state['revision'])).fetchall()
            generation={'task_id':task_id,'revision':state['revision'],
                'current_epoch':state['epoch'],'source_epoch':source_epoch,
                'display_only':True,'execution_eligible':False}
            results=[]
            for row in rows:
                dispatch=self.get_dispatch(row['dispatch_id'])
                if dispatch['payload'].get('contract')!=state['contract']:
                    continue
                result=self.dispatch_result(row['dispatch_id'],current_only=False)
                if result is not None:
                    results.append({'receipt':result,'generation':{
                        **generation,'dispatch_id':row['dispatch_id']}})
            return results

    def _fresh(self, record: dict | sqlite3.Row, *, writes: bool = False) -> dict[str, Any]:
        if writes:
            self.management.require_active(record['task_id'])
        state = self._state(record["task_id"])
        if (record["epoch"] != state["epoch"] or record["revision"] != state["revision"]
                or state["epoch"] != state["task_epoch"]):
            raise ActionRejected("工作流代际已改变，旧操作被撤销")
        if writes and not state["writes_enabled"]:
            raise ActionRejected("普通操作权限已撤销")
        return state

    def submit(self, goal: str, contract: dict, task_id: str | None = None) -> dict[str, Any]:
        goal = _nonempty(goal, "任务目标")
        if type(contract) is not dict:
            raise TypeError("contract must be a JSON object")
        encoded, digest = _encode(contract)
        command_id, now = uuid.uuid4().hex, _utc_now()
        with self.engine._transaction():
            request = self._request_identity(contract, 'submit', goal, task_id)
            replay = self._replayed_request(request)
            if replay is not None:
                return replay
            if task_id is None:
                task_id = uuid.uuid4().hex
                self._db.execute(
                    "INSERT INTO tasks(id,goal,version,status,created_at,updated_at) VALUES(?,?,1,'draft',?,?)",
                    (task_id, goal, now, now),
                )
                revision = 1
                row = self.engine._task_row(task_id)
            else:
                self.management.require_active(task_id)
                self.engine._task_row(task_id)
                previous = self._db.execute("SELECT revision FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
                revision = previous["revision"] + 1 if previous else 1
                row = self.engine._invalidate(task_id, status="draft", goal=goal)
                self._revoke_operations(task_id)
            self._db.execute(
                "INSERT INTO workflow_contracts VALUES(?,?,?,?,?,?,?)",
                (task_id, revision, command_id, goal, encoded, digest, now),
            )
            self._db.execute(
                "INSERT INTO workflow_tasks(task_id,revision,epoch,writes_enabled) VALUES(?,?,?,1) "
                "ON CONFLICT(task_id) DO UPDATE SET revision=excluded.revision,epoch=excluded.epoch,"
                "writes_enabled=1,control_seq=workflow_tasks.control_seq+1,control_kind='submit'",
                (task_id, revision, row["version"]),
            )
            self._db.execute("UPDATE workflow_tasks SET blocked_reason='' WHERE task_id=?", (task_id,))
            state = self._state(task_id)
            payload = {"goal": goal, "contract": contract, "command_id": command_id}
            self._command(state, command_id, "submit", payload)
            self.engine._append_event(row, "workflow_submitted", "已保存 MAA 工作流合同", {"revision": revision})
            self._publish_event(task_id, "command.submitted", payload, event_key=command_id)
            self._record_request(request, state, contract)
            return state

    @staticmethod
    def _request_identity(contract, operation, goal, task_id):
        identifier = contract.get('request_id')
        if not identifier:
            return None
        identifier = _nonempty(identifier, 'request id')
        _, digest = _encode({'operation': operation, 'target_task_id': task_id,
                             'goal': goal, 'contract': contract})
        return identifier, operation, digest

    def request_receipt(self, request_id):
        """Return the content-free receipt, including for a deleted task tombstone."""
        with self.engine._lock:
            self.engine._ensure_open()
            row = self._db.execute('SELECT * FROM workflow_request_receipts WHERE request_id=?',
                                   (request_id,)).fetchone()
            return dict(row) if row else None

    def _replayed_request(self, request):
        if request is None:
            return None
        identifier, operation, digest = request
        row = self._db.execute('SELECT * FROM workflow_request_receipts WHERE request_id=?',
                               (identifier,)).fetchone()
        if row is None:
            return None
        if (row['operation'], row['payload_hash']) != (operation, digest):
            raise ActionRejected('同一消息编号已经用于不同的任务要求')
        self.management.require_active(row['task_id'])
        return {**self._state(row['task_id']), 'request_replayed': True}

    def _record_request(self, request, state, contract, *, relation=None, summary='任务要求已保存'):
        """Called in the contract transaction; never leave an applied task unbound."""
        if request is None:
            return
        identifier, operation, digest = request
        if self._db.execute('SELECT 1 FROM workflow_messages WHERE request_id=?', (identifier,)).fetchone():
            self.management._finish_message(identifier, task_id=state['task_id'],
                relation=relation or contract.get('relation') or 'new', summary=summary)
        self._db.execute('INSERT INTO workflow_request_receipts VALUES(?,?,?,?,?,?,?)',
            (identifier, state['task_id'], operation, digest, state['revision'], state['epoch'], _utc_now()))

    def _command(self, state: dict, command_id: str, kind: str, payload: dict) -> None:
        encoded, digest = _encode(payload)
        self._db.execute("INSERT INTO workflow_commands VALUES(?,?,?,?,?,?,?,?)", (
            command_id, state["task_id"], kind, state["revision"], state["epoch"], encoded, digest, _utc_now(),
        ))

    def replace_contract(self, task_id, goal, contract, *, expected_revision, expected_epoch):
        """CAS edit without converting an edit of a paused task into Resume."""
        encoded, digest = _encode(contract)
        command_id = uuid.uuid4().hex
        with self.engine._transaction():
            request = self._request_identity(contract, 'modify', goal, task_id)
            replay = self._replayed_request(request)
            if replay is not None:
                return replay
            self.management.require_active(task_id)
            previous = self._state(task_id)
            if previous['revision'] != expected_revision or previous['epoch'] != expected_epoch:
                raise ActionRejected('目标版本已变化，不能覆盖更新后的任务')
            if previous['status'] in {'completed', 'failed', 'cancelled'}:
                raise ActionRejected('已结束任务不能直接修改并重新运行')
            row = self.engine._invalidate(task_id, status=previous['status'], goal=_nonempty(goal, '目标'))
            revision = previous['revision'] + 1
            self._db.execute('INSERT INTO workflow_contracts VALUES(?,?,?,?,?,?,?)',
                (task_id, revision, command_id, goal, encoded, digest, _utc_now()))
            self._db.execute('UPDATE workflow_tasks SET revision=?,epoch=?,control_seq=control_seq+1 WHERE task_id=?',
                (revision, row['version'], task_id))
            self._revoke_operations(task_id)
            current = self._state(task_id)
            payload = {'kind': 'modify', 'command_id': command_id, 'request_id': contract.get('request_id', '')}
            self._command(current, command_id, 'modify', payload)
            self._publish_event(task_id, 'command.modified', payload, event_key=command_id)
            self._record_request(request, current, contract)
            return current

    def _revoke_operations(self, task_id: str) -> None:
        # Admission and this revocation serialize on the same SQLite writer.
        self._db.execute(
            "UPDATE workflow_operations SET status=CASE WHEN submitted_at IS NULL THEN 'not_issued' ELSE 'unknown' END "
            "WHERE task_id=? AND stop_only=0 AND status IN ('planned','issued','submitted')", (task_id,),
        )
        self._db.execute(
            "UPDATE workflow_slots SET barrier_operation_id=NULL WHERE barrier_operation_id IN "
            "(SELECT operation_id FROM workflow_operations WHERE task_id=? AND status='not_issued')", (task_id,),
        )
        self._db.execute("UPDATE workflow_dispatches SET status='cancelled' WHERE task_id=? AND status='pending'", (task_id,))

    def control(self, task_id: str, kind: str, *, expected_epoch: int | None = None) -> dict[str, Any]:
        if kind not in {"pause", "stop", "resume"}:
            raise ValueError("control must be pause, stop or resume")
        with self.engine._transaction():
            self.management.require_active(task_id)
            before = self._state(task_id)
            if expected_epoch is not None and (before['epoch'] != expected_epoch or
                    not before['writes_enabled'] or before['status'] in {'completed','failed','cancelled'}):
                raise ActionRejected('旧工作流不能暂停当前任务')
            row = self.engine._invalidate(task_id, status={"pause": "paused", "stop": "cancelled", "resume": "draft"}[kind])
            self._db.execute(
                "UPDATE workflow_tasks SET epoch=?,writes_enabled=?,control_seq=control_seq+1,control_kind=? WHERE task_id=?",
                (row["version"], int(kind == "resume"), kind, task_id),
            )
            self._revoke_operations(task_id)
            if kind == 'resume':
                self._db.execute("UPDATE workflow_tasks SET blocked_reason='' WHERE task_id=?", (task_id,))
            if kind != "resume":
                self._db.execute(
                    "UPDATE workflow_slots SET control_seq=control_seq+1 WHERE barrier_operation_id IN "
                    "(SELECT operation_id FROM workflow_operations WHERE task_id=?)", (task_id,),
                )
            state = self._state(task_id)
            command_id = uuid.uuid4().hex
            payload = {"kind": kind, "control_seq": state["control_seq"], "command_id": command_id}
            self._command(state, command_id, kind, payload)
            self.engine._append_event(row, "workflow_control", "已更新 MAA 本地操作权限", payload)
            self._publish_event(task_id, "command.control", payload, event_key=command_id)
            return state

    def suspend(self, task_id: str, *, expected_epoch: int, message: str, kind: str = 'blocked',
                preparation_dispatch_id: str | None = None) -> dict[str, Any]:
        """End a current run needing input and atomically revoke ordinary writes."""
        with self.engine._transaction():
            state = self._state(task_id)
            if state['epoch'] != expected_epoch or state['status'] in {'completed', 'failed', 'cancelled'}:
                raise ActionRejected('旧工作流不能改变当前任务')
            self._fresh(state, writes=True)
            preparation = None
            if preparation_dispatch_id is not None:
                dispatch = self.get_dispatch(preparation_dispatch_id)
                research = self.dispatch_result(preparation_dispatch_id)
                if (kind != 'blocked' or dispatch['task_id'] != task_id
                        or dispatch['epoch'] != expected_epoch or dispatch['revision'] != state['revision']
                        or dispatch['role'] != 'research' or dispatch['status'] != 'completed'
                        or dispatch['payload'].get('contract') != state['contract']
                        or not research or research.get('status') != 'researched'
                        or not research.get('candidate_refs')):
                    raise ActionRejected('准备交接的当前任务或研究凭据已变化')
                preparation = {'research_dispatch_id': preparation_dispatch_id,
                               'research_epoch': expected_epoch,
                               'candidate_refs': research['candidate_refs']}
                if research.get('queue_plan_ref'):
                    preparation['queue_plan_ref'] = research['queue_plan_ref']
            row = self.engine._invalidate(task_id, status='needs_input')
            self._db.execute(
                "UPDATE workflow_tasks SET epoch=?,writes_enabled=0,control_seq=control_seq+1,"
                "control_kind='needs_input',blocked_reason='' WHERE task_id=?", (row['version'], task_id),
            )
            self._revoke_operations(task_id)
            state = self._state(task_id)
            self.engine._append_event(row, 'workflow_needs_input', message[:1600])
            self._publish_event(task_id, 'workflow.needs_input', {'summary': message[:1600]},
                                event_key=f"needs-input:{task_id}:{state['epoch']}")
            if kind == 'business':
                self.management._open_question(task_id, message, kind=kind)
            else:
                self._db.execute("UPDATE workflow_tasks SET control_kind='blocked',blocked_reason=? WHERE task_id=?", (message[:1600], task_id))
                state = self._state(task_id)
            if preparation is not None:
                self._publish_event(task_id, 'import.preparation_verified', preparation,
                    event_key=f"import-preparation:{preparation_dispatch_id}:{state['epoch']}")
            return state

    def complete_import_verified(self, task_id: str, evidence_refs: list, *,
                                 expected_epoch: int) -> dict[str, Any]:
        """Accept an adapter's durable import readback without counting a battle.

        The caller cannot turn a Leader sentence into completion: references
        must match the last current MAA result and the files' actual hashes.
        File reads are bounded and happen before the short SQLite transaction.
        """
        # Pixel refreshes and repeated readbacks each retain their own evidence.
        # A valid single import can exceed 32 refs (a39 produced 34). Keep the
        # flat receipt contract and validate every file below, with a finite cap.
        if not isinstance(evidence_refs, list) or not 0 < len(evidence_refs) <= 128:
            raise ActionRejected('导入完成缺少可核实的产物引用')
        root = self.engine.data_dir.resolve()
        for ref in evidence_refs:
            if (not isinstance(ref, dict) or set(ref) != {'path', 'sha256'}
                    or not isinstance(ref['path'], str) or not isinstance(ref['sha256'], str)
                    or len(ref['sha256']) != 64):
                raise ValueError('导入证据引用格式无效')
            try:
                path = Path(ref['path']).resolve(strict=True)
                if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError('导入证据路径或大小无效')
                if hashlib.sha256(path.read_bytes()).hexdigest() != ref['sha256']:
                    raise ValueError('导入证据内容已改变')
            except OSError as exc:
                raise ValueError('导入证据文件不可读') from exc
        with self.engine._transaction():
            state = self._state(task_id)
            if state['epoch'] != expected_epoch:
                raise ActionRejected('旧导入回执不能完成当前任务')
            row = self._db.execute(
                "SELECT d.dispatch_id FROM workflow_dispatches d JOIN workflow_results r "
                "ON r.dispatch_id=d.dispatch_id WHERE d.task_id=? AND d.role='maa' "
                "AND d.epoch=? AND d.revision=? ORDER BY d.created_at DESC,d.rowid DESC LIMIT 1",
                (task_id, expected_epoch, state['revision']),
            ).fetchone()
            receipt = self.dispatch_result(row['dispatch_id']) if row else None
            if (not receipt or receipt.get('status') != 'import_verified'
                    or receipt.get('battle_start_submitted') is not False
                    or receipt.get('evidence_refs') != evidence_refs):
                raise ActionRejected('没有匹配当前代际的持久 MAA 导入回执')
            if state['status'] == 'completed' and state['control_kind'] == 'import_verified':
                return self.engine._task_dict(self.engine._task_row(task_id))
            self._fresh(state, writes=True)
            if state['status'] in {'paused', 'cancelled', 'failed', 'completed'}:
                raise ActionRejected('本地执行已停止，不能接受晚到完成')
            self._db.execute("UPDATE tasks SET status='completed',updated_at=? WHERE id=?", (_utc_now(), task_id))
            self._db.execute(
                "UPDATE workflow_tasks SET writes_enabled=0,control_seq=control_seq+1,"
                "control_kind='import_verified' WHERE task_id=?", (task_id,),
            )
            self._revoke_operations(task_id)
            task = self.engine._task_row(task_id)
            details = {'dispatch_id': row['dispatch_id'], 'evidence_refs': evidence_refs,
                       'battle_start_submitted': False}
            self.engine._append_event(task, 'maa_import_verified', 'MAA 作业导入已通过真实 GUI 回读核实', details)
            self._publish_event(task_id, 'workflow.import_verified', details,
                                event_key=f"import-verified:{task_id}:{expected_epoch}")
            return self.engine._task_dict(task)

    def _battle_reference(self, reference, *, image=False):
        """Read one bounded, hash-bound private artifact without following links."""
        if (not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}
                or not isinstance(reference['path'], str)
                or not isinstance(reference['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', reference['sha256'])):
            raise ActionRejected('作战回读缺少有效文件引用')
        path = Path(reference['path']).absolute()
        try:
            for ancestor in (path, *path.parents):
                info = ancestor.lstat()
                if (ancestor.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400):
                    raise ValueError('linked evidence')
            path = path.resolve(strict=True)
            if not path.is_relative_to(self.engine.data_dir.resolve()) or not path.is_file():
                raise ValueError('evidence outside profile')
            before = path.stat()
            if not 0 < before.st_size <= (32 if image else 4) * 1024 * 1024:
                raise ValueError('evidence size')
            raw = path.read_bytes()
            after = path.stat()
            if ((before.st_ino, before.st_size, before.st_mtime_ns) !=
                    (after.st_ino, after.st_size, after.st_mtime_ns)
                    or hashlib.sha256(raw).hexdigest() != reference['sha256']):
                raise ValueError('evidence changed')
            if image:
                # Native captures are PNGs. Verify structure and chunk CRCs,
                # not merely a JSON assertion that pixels were observed.
                if raw[:8] != b'\x89PNG\r\n\x1a\n':
                    raise ValueError('not PNG')
                offset, kinds = 8, []
                while offset + 12 <= len(raw):
                    length = struct.unpack('>I', raw[offset:offset+4])[0]
                    end = offset + 12 + length
                    if end > len(raw):raise ValueError('truncated PNG')
                    kind = raw[offset+4:offset+8]
                    body = raw[offset+8:offset+8+length]
                    crc = struct.unpack('>I', raw[offset+8+length:end])[0]
                    if zlib.crc32(kind+body) & 0xffffffff != crc:
                        raise ValueError('PNG CRC')
                    kinds.append(kind)
                    if len(kinds) == 1:
                        if kind != b'IHDR' or length != 13:raise ValueError('PNG IHDR')
                        width, height = struct.unpack('>II', body[:8])
                        if not 0 < width <= 16384 or not 0 < height <= 16384:
                            raise ValueError('PNG dimensions')
                    offset = end
                    if kind == b'IEND':
                        if length or offset != len(raw):raise ValueError('PNG tail')
                        break
                if not kinds or kinds[-1] != b'IEND' or b'IDAT' not in kinds:
                    raise ValueError('incomplete PNG')
                return raw
            return json.loads(raw.decode('utf-8'))
        except (OSError, ValueError, TypeError, UnicodeError, struct.error, RecursionError) as error:
            raise ActionRejected('作战回读文件不可读、越界或哈希不一致') from error

    def _battle_idle_proof(self, operation, reference):
        """Pure evidence validation. No model or native call runs in a transaction."""
        proof = self._battle_reference(reference)
        identity = operation['action'].get('maa_identity')
        from .battle_contract import authorized
        with self.engine._lock:
            original = self._db.execute('SELECT contract,payload_hash FROM workflow_contracts '
                'WHERE task_id=? AND revision=?',(operation['task_id'],operation['revision'])).fetchone()
            contract = json.loads(original['contract']) if original else None
        if not original or _encode(contract)[1] != original['payload_hash'] or not authorized(contract):
            raise ActionRejected('原任务并未授权本阶段的 MAA 开战')
        from .maa_visual_native import MAA_EXECUTABLE
        import ntpath
        if (not isinstance(identity, dict) or set(identity) != {'hwnd', 'pid', 'creation_time', 'executable'}
                or any(type(identity.get(k)) is not int or identity[k] <= 0
                       for k in ('hwnd', 'pid', 'creation_time'))
                or not isinstance(identity.get('executable'), str)
                or ntpath.normcase(ntpath.normpath(identity['executable'])) != ntpath.normcase(MAA_EXECUTABLE)):
            raise ActionRejected('原开始操作缺少明确 MAA 进程身份')
        if (operation['action'].get('kind') != 'start' or not operation['starts'] or operation['stop_only']
                or operation['slot'] != 'desktop-maa' or not operation['submitted_at']
                or operation['status'] not in {'submitted', 'unknown', 'verified'}
                or not isinstance(proof, dict) or proof.get('schema') != 'maa_battle_idle/v1'
                or any(type(proof.get(k)) is not int for k in ('revision','epoch'))
                or any(proof.get(k) != operation[k] for k in ('task_id', 'revision', 'epoch'))
                or proof.get('start_operation_id') != operation['operation_id']
                or proof.get('start_payload_hash') != operation['payload_hash']
                or proof.get('maa_identity') != identity
                or proof.get('battle_start_submitted') is not True
                or proof.get('external_idle_confirmed') is not True
                or type(proof.get('control_seq')) is not int):
            raise ActionRejected('MAA 空闲回执不属于已提交的本次开始操作')
        idle_refs = proof.get('idle_observation_refs')
        if not isinstance(idle_refs, list) or len(idle_refs) != 2:
            raise ActionRejected('需要两次独立的原生空闲回读')
        refs = [proof.get('observed_running_ref'), *idle_refs]
        captures, paths = [], []
        submitted = datetime.fromisoformat(operation['submitted_at'])
        if submitted.tzinfo is None:raise ActionRejected('开始时间缺少时区')
        now = datetime.now(timezone.utc).timestamp()
        for index, ref in enumerate(refs):
            observation = self._battle_reference(ref)
            if (not isinstance(observation, dict) or observation.get('schema') != 'maa_battle_observation/v1'
                    or any(type(observation.get(k)) is not int for k in ('revision','epoch'))
                    or any(observation.get(k) != operation[k] for k in ('task_id', 'revision', 'epoch'))
                    or observation.get('start_operation_id') != operation['operation_id']
                    or observation.get('maa_identity') != identity
                    or observation.get('method') not in {'win32_printwindow', 'windows_graphics_capture'}
                    or observation.get('running') is not (index == 0)
                    or observation.get('idle') is not (index != 0)):
                raise ActionRejected('MAA 原生回读身份或运行状态不一致')
            captured = observation.get('captured_at')
            image = observation.get('screenshot_ref')
            if (type(captured) not in (int, float) or not math.isfinite(captured)
                    or captured < submitted.timestamp() or captured > now+2
                    or not isinstance(image, dict) or observation.get('image_sha256') != image.get('sha256')):
                raise ActionRejected('MAA 回读时间或图像引用不一致')
            self._battle_reference(image, image=True)
            captures.append(captured)
            paths.append(str(Path(image['path']).resolve()))
        if (not captures[0] < captures[1] < captures[2] or captures[2]-captures[1] < .1
                or len(set(paths)) != 3):
            raise ActionRejected('MAA 回读没有独立的运行后稳定空闲证据')
        return proof

    def settle_battle_idle(self, operation_id: str, evidence_ref: dict) -> dict:
        """Release one start lease after observed running then stable idle.

        A stop may revoke the task first. Read-only settling then retires only
        the original external effect and never restores old write authority.
        """
        operation = self.get_operation(operation_id)
        proof = self._battle_idle_proof(operation, evidence_ref)
        result = {'battle_idle_verified': True, 'evidence_ref': evidence_ref,
                  'battle_start_submitted': True, 'external_idle_confirmed': True,
                  'completion_scope': 'maa_run_ended_not_victory'}
        encoded, digest = _encode(result)
        with self.engine._transaction():
            row = self._operation_row(operation_id)
            if self._operation(row) != operation:
                raise ActionRejected('核对期间开始操作已变化')
            state = self._state(operation['task_id'])
            if self.management._lifecycle(operation['task_id'])['state'] not in {'active', 'trashed'}:
                raise ActionRejected('任务已进入清理，不能登记作战结果')
            if (proof['control_seq'] != state['control_seq'] or state['revision'] != operation['revision']):
                raise ActionRejected('任务控制或目标已变化，不能接受旧空闲回执')
            current = (state['epoch'] == operation['epoch'] == state['task_epoch']
                       and state['writes_enabled'] and state['status'] not in {'paused','cancelled','failed','completed'})
            revoked = (not state['writes_enabled'] and state['control_kind'] in {'stop','pause'}
                       and state['epoch'] >= operation['epoch'])
            if not current and not revoked:
                raise ActionRejected('旧开始效果不能改变新一代任务')
            prior = operation.get('result') or {}
            if prior.get('battle_idle_verified'):
                if operation['result'] != result:
                    raise ActionRejected('已核实的作战回执不能改写')
                return operation
            slot = self._ensure_slot(row['slot'])
            if slot['barrier_operation_id'] != operation_id:
                raise ActionRejected('桌面控制槽不属于本次开始效果')
            self._db.execute("UPDATE workflow_operations SET status='verified',result=? WHERE operation_id=?",
                             (encoded, operation_id))
            self._db.execute('UPDATE workflow_slots SET barrier_operation_id=NULL '
                             'WHERE slot=? AND barrier_operation_id=?', (row['slot'], operation_id))
            self._publish_event(row['task_id'], 'operation.battle_idle_verified',
                {'operation_id': operation_id, 'result': result},
                event_key=f'battle-idle:{operation_id}:{digest}', revision=row['revision'], epoch=row['epoch'])
            return self._operation(self._operation_row(operation_id))

    def complete_battle_verified(self, task_id: str, evidence_refs: list, *, expected_epoch: int) -> dict:
        """Complete a bounded MAA run, not a claim that the stage was won."""
        from .battle_contract import authorized
        if not isinstance(evidence_refs, list) or not 0 < len(evidence_refs) <= 64:
            raise ActionRejected('作战结束缺少持久证据')
        for reference in evidence_refs:self._battle_reference(reference)
        before = self.current(task_id)
        with self.engine._lock:
            dispatch = self._db.execute('SELECT d.dispatch_id FROM workflow_dispatches d '
                'JOIN workflow_results r ON r.dispatch_id=d.dispatch_id '
                "WHERE d.task_id=? AND d.revision=? AND d.epoch=? AND d.role='maa' "
                'ORDER BY d.created_at DESC,d.rowid DESC LIMIT 1',
                (task_id, before['revision'], expected_epoch)).fetchone()
            receipt = self.dispatch_result(dispatch['dispatch_id']) if dispatch else None
        if (not authorized(before['contract']) or before['epoch'] != expected_epoch
                or not receipt or receipt.get('status') != 'battle_finished'
                or receipt.get('battle_start_submitted') is not True
                or receipt.get('external_idle_confirmed') is not True
                or receipt.get('evidence_refs') != evidence_refs):
            raise ActionRejected('没有匹配当前任务的持久 MAA 运行结束回执')
        operation = self.get_operation(receipt.get('start_operation_id'))
        proof_ref = (operation.get('result') or {}).get('evidence_ref')
        if (operation['task_id'] != task_id or operation['revision'] != before['revision']
                or operation['epoch'] != expected_epoch or operation['dispatch_id'] != dispatch['dispatch_id']
                or operation['status'] != 'verified' or (operation.get('result') or {}).get('battle_idle_verified') is not True
                or proof_ref not in evidence_refs):
            raise ActionRejected('开始效果尚未通过原生运行和空闲回读核验')
        self._battle_idle_proof(operation, proof_ref)
        with self.engine._transaction():
            state = self._state(task_id)
            if state != before or self.get_operation(operation['operation_id']) != operation:
                raise ActionRejected('任务或开始凭据在完成前已变化')
            latest = self._db.execute('SELECT d.dispatch_id FROM workflow_dispatches d '
                'JOIN workflow_results r ON r.dispatch_id=d.dispatch_id '
                "WHERE d.task_id=? AND d.revision=? AND d.epoch=? AND d.role='maa' "
                'ORDER BY d.created_at DESC,d.rowid DESC LIMIT 1',
                (task_id, state['revision'], expected_epoch)).fetchone()
            if not latest or latest['dispatch_id'] != dispatch['dispatch_id']:
                raise ActionRejected('MAA 委派已被更新')
            if state['status'] == 'completed' and state['control_kind'] == 'battle_finished':
                return self.engine._task_dict(self.engine._task_row(task_id))
            self._fresh(state, writes=True)
            if state['status'] in {'paused','cancelled','failed','completed','needs_input'}:
                raise ActionRejected('任务已经停止，不能接受迟到作战完成')
            self._db.execute("UPDATE tasks SET status='completed',updated_at=? WHERE id=?", (_utc_now(),task_id))
            self._db.execute("UPDATE workflow_tasks SET writes_enabled=0,control_seq=control_seq+1,"
                             "control_kind='battle_finished' WHERE task_id=?", (task_id,))
            self._revoke_operations(task_id)
            task = self.engine._task_row(task_id)
            details = {'dispatch_id': dispatch['dispatch_id'], 'start_operation_id':operation['operation_id'],
                       'evidence_refs':evidence_refs, 'completion_scope':'maa_run_ended_not_victory'}
            self.engine._append_event(task,'maa_battle_finished','MAA 已确认运行后结束；未据此判断关卡胜利',details)
            self._publish_event(task_id,'workflow.battle_finished',details,
                                event_key=f'battle-finished:{task_id}:{expected_epoch}')
            return self.engine._task_dict(task)

    def complete_planned_verified(self, task_id: str, *, expected_epoch: int):
        """Commit typed completion from actual durable receipts, never prose."""
        from .leader_plan import PlanStore
        from .planned_agents import verify_planned_completion
        before = self.current(task_id)
        kind = before['contract'].get('current_deliverable')
        if kind not in {'stage_catalog', 'copilot_codes'}:
            raise ActionRejected('此任务不是计划式只读交付')
        box_ref = None
        if kind == 'copilot_codes':
            from .code_delivery import CodeDeliveryStore
            from .roster_match import roster_ref
            deliveries = CodeDeliveryStore(self, self.engine.data_dir)
            box_ref = roster_ref(deliveries.roster_store.current('local-default') or {})
        roles = {}
        with self.engine._lock:
            for role in ('catalog', 'research'):
                row = self._db.execute('SELECT d.dispatch_id FROM workflow_dispatches d '
                    'JOIN workflow_results r ON r.dispatch_id=d.dispatch_id WHERE d.task_id=? '
                    'AND d.revision=? AND d.epoch=? AND d.role=? ORDER BY d.rowid DESC LIMIT 1',
                    (task_id, before['revision'], expected_epoch, role)).fetchone()
                if row:
                    roles[role] = self.dispatch_result(row['dispatch_id'])
        check = verify_planned_completion(self, {'task_id': task_id, 'epoch': expected_epoch,
                                               'role_results': roles}, kind)
        if not check.get('complete') or not roles.get('catalog' if kind == 'stage_catalog' else 'research'):
            raise ActionRejected('缺少当前持久角色回执或完整目标证据')
        plans = PlanStore(self)
        plan = plans.current(task_id) if kind == 'copilot_codes' else None
        steps_hash = _encode(plan['steps'])[1] if plan else None
        with self.engine._transaction():
            state = self._state(task_id)
            if state['epoch'] != expected_epoch or state['revision'] != before['revision']:
                raise ActionRejected('旧只读结果不能完成新任务版本')
            if state['status'] == 'completed' and state['control_kind'] == 'planned_verified':
                return self.engine._task_dict(self.engine._task_row(task_id))
            self._fresh(state, writes=True)
            if kind == 'copilot_codes':
                if (_encode(plans.current(task_id)['steps'])[1] != steps_hash
                        or roster_ref(deliveries.roster_store.current('local-default') or {}) != box_ref):
                    raise ActionRejected('完成核验期间计划或本地阵容发生变化')
            self._db.execute("UPDATE tasks SET status='completed',updated_at=? WHERE id=?", (_utc_now(), task_id))
            self._db.execute("UPDATE workflow_tasks SET writes_enabled=0,control_seq=control_seq+1,"
                             "control_kind='planned_verified' WHERE task_id=?", (task_id,))
            self._revoke_operations(task_id)
            self._publish_event(task_id, 'workflow.planned_verified',
                {'current_deliverable': kind, 'game_input_sent': False},
                event_key=f'planned-verified:{task_id}:{expected_epoch}')
            return self.engine._task_dict(self.engine._task_row(task_id))

    def complete_research_verified(self, task_id: str, evidence_refs: list, *, expected_epoch: int):
        """Finish a read-only objective using a current, tool-produced receipt.

        File checking happens outside the writer transaction; the committed
        dispatch, objective, epoch and exact references are checked inside it.
        This never counts a game run or grants input authority.
        """
        from .leader_policy import research_satisfies_goal, copilot_research_required
        if not isinstance(evidence_refs,list) or not 0<len(evidence_refs)<=256:
            raise ActionRejected('资料完成缺少实际工具证据')
        root=self.engine.data_dir.resolve()
        proofs=[];proof_refs={}
        for ref in evidence_refs:
            if not isinstance(ref,dict) or set(ref)!={'path','sha256'}:
                raise ValueError('资料证据格式无效')
            path=Path(ref['path']).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size>4*1024*1024:
                raise ValueError('资料证据路径或大小无效')
            raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=ref['sha256']:
                raise ValueError('资料证据内容已变化')
            value=json.loads(raw)
            if (isinstance(value,dict) and value.get('read_only') is True
                    and value.get('tool') in {'research_search','research_fetch','research_catalog','research_candidates','research_activity_stages','research_levels','research_download'}
                    and value.get('task_id')==task_id and value.get('epoch')==expected_epoch
                    and isinstance(value.get('result'),dict) and value['result'].get('ok') is True):
                proofs.append(value)
                proof_refs[(ref['path'],ref['sha256'])]=value
        # Download bytes and nested stage evidence are checked before taking
        # the writer lock used by Stop. A matching immutable receipt is checked
        # again inside the commit transaction below.
        with self.engine._lock:
            before=self._state(task_id)
            prior_row=self._db.execute('SELECT d.dispatch_id FROM workflow_dispatches d JOIN workflow_results r '
                'ON r.dispatch_id=d.dispatch_id WHERE d.task_id=? AND d.role=\'research\' '
                'AND d.epoch=? AND d.revision=? ORDER BY d.rowid DESC LIMIT 1',
                (task_id,expected_epoch,before['revision'])).fetchone()
            prior=self.dispatch_result(prior_row['dispatch_id']) if prior_row else None
        # A follow-up research dispatch may reuse immutable current-epoch
        # observations without repeating network IO. Every accepted source
        # must still be referenced by its own registered research receipt.
        registered_proofs=[];registered_proof_refs={}
        for key,proof in proof_refs.items():
            identifier=proof.get('dispatch_id')
            try:
                dispatch=self.get_dispatch(identifier)
                source_receipt=self.dispatch_result(identifier)
            except (KeyError,TypeError):continue
            reference={'path':key[0],'sha256':key[1]}
            if (dispatch.get('role')=='research' and dispatch.get('task_id')==task_id
                    and dispatch.get('epoch')==expected_epoch and dispatch.get('revision')==before['revision']
                    and source_receipt and reference in source_receipt.get('evidence_refs',[])):
                registered_proofs.append(proof);registered_proof_refs[key]=proof
        proofs=registered_proofs
        if copilot_research_required(before['contract']):
            if not prior or not prior.get('candidate_refs'):
                raise ActionRejected('作业资料目标缺少完整下载凭据')
            self._research_artifact_refs(prior)
            summaries=[];stage_ids_by_code={}
            for ref in prior['candidate_refs']:
                record=json.loads(Path(ref['path']).read_text(encoding='utf-8-sig'))
                stage=record.get('goal_stage') or {}
                stage_proof=json.loads(Path(stage['evidence_ref']['path']).read_text(encoding='utf-8-sig'))
                body=json.loads(Path(record['path']).read_text(encoding='utf-8-sig'))
                identifier=str(record.get('copilot_id',''))
                if (record.get('task_id')!=task_id or record.get('epoch')!=expected_epoch
                        or not self.dispatch_result(record.get('research_dispatch_id',''))
                        or ref not in self.dispatch_result(record['research_dispatch_id']).get('candidate_refs',[])
                        or stage_proof!={k:v for k,v in stage.items() if k!='evidence_ref'}
                        or stage.get('task_id')!=task_id or stage.get('epoch')!=expected_epoch
                        or stage.get('goal')!=before['contract'].get('goal')
                        or not re.fullmatch(r'[1-9][0-9]{0,11}',identifier)
                        or record.get('source_url')!='https://prts.maa.plus/copilot/get/'+identifier
                        or body.get('stage_name')!=record.get('stage_name')
                        or str(body.get('stage_name','')).casefold() not in
                           {str(stage.get('stage_id','')).casefold(),str(stage.get('level_id','')).casefold()}
                        or not body.get('actions') or len(body['actions'])!=record.get('action_count')
                        or not any(p.get('dispatch_id')==record['research_dispatch_id'] and p.get('tool')=='research_download'
                            and p['result'].get('path')==record['path'] and p['result'].get('sha256')==record['sha256'] for p in proofs)):
                    raise ActionRejected('完整作业与本次关卡或下载来源不一致')
                facts={k:record[k] for k in ('copilot_id','path','sha256','stage_name','title','source_url')}
                facts.update(code=stage['code'],mystery_code='prts://'+identifier,
                             detail_url='https://prts.plus/?op='+identifier)
                summaries.append(facts)
                stage_ids_by_code.setdefault(stage['code'],set()).update(
                    {str(stage['stage_id']).casefold(),str(stage['level_id']).casefold()})
            if summaries!=prior.get('verified_copilots'):
                raise ActionRejected('作业摘要与完整下载凭据不一致')
            from .leader_policy import activity_job_goal,activity_scope_complete,research_stage_codes
            if activity_job_goal(before['contract']):
                scope=prior.get('activity_scope') or {};ref=scope.get('catalog_ref') or {}
                path=Path(ref.get('path','')).resolve(strict=True)
                if not path.is_relative_to(root) or path.stat().st_size>8*1024*1024:
                    raise ActionRejected('活动范围目录路径无效')
                raw=path.read_bytes()
                if hashlib.sha256(raw).hexdigest()!=ref.get('sha256'):raise ActionRejected('活动范围目录已变化')
                catalog=json.loads(raw)
                catalog_codes=[s.get('code') for s in catalog.get('stages',[]) if isinstance(s,dict) and s.get('code')]
                if (catalog.get('source')!='https://prts.maa.plus/arknights/level'
                        or scope.get('event_name')!=catalog.get('event_name')
                        or not activity_scope_complete(scope,catalog_codes)):
                    raise ActionRejected('活动范围不来自真实关卡目录')
                from .leader_policy import opening_exclusion_supported
                for excluded in scope['excluded_stages']:
                    if excluded['reason_kind']=='not_ordinary':continue
                    source_ref=excluded.get('source_ref') or {}
                    proof=registered_proof_refs.get((source_ref.get('path'),source_ref.get('sha256')))
                    if not proof or proof.get('tool') not in {'research_search','research_fetch'}:
                        raise ActionRejected('活动排除项缺少本任务实际公告来源凭据')
                    if not opening_exclusion_supported(excluded,proof):
                        raise ActionRejected('活动排除项缺少真实原文、明确关卡范围或实际未来开放时间')
            from .leader_policy import roster_required
            if roster_required(before['contract']):
                from .maa_roster_tools import RosterTools
                roster=RosterTools(self,root)
                for ref in prior['candidate_refs']:
                    record=json.loads(Path(ref['path']).read_text(encoding='utf-8'))
                    original=(prior.get('assessments') or {}).get(record['copilot_id'])
                    if not original or original.get('classification')!='satisfied':continue
                    current=roster.assess(before['contract'].get('profile_id','local-default'),record)
                    if (current.get('classification')!='satisfied' or current.get('roster_ref')!=original.get('roster_ref')
                            or current.get('policy_sha256')!=original.get('policy_sha256')):
                        raise ActionRejected('作业对应的当前干员事实或策略已变化')
            required_codes=research_stage_codes(before['contract'],prior)
            complete_codes=set()
            for pool in prior.get('candidate_pools',[]):
                ref=pool.get('pool_ref') or {};path=Path(ref.get('path','')).resolve(strict=True)
                if not path.is_relative_to(root) or path.stat().st_size>32*1024*1024:
                    raise ActionRejected('候选池不在本地资料目录或过大')
                raw=path.read_bytes()
                if hashlib.sha256(raw).hexdigest()!=ref.get('sha256'):
                    raise ActionRejected('候选池哈希已变化')
                value=json.loads(raw)
                if (value.get('pool_id')!=ref.get('pool_id') or value.get('coverage')!=pool.get('coverage')
                        or len(value.get('candidates',[]))!=pool['coverage'].get('collected')):
                    raise ActionRejected('候选池覆盖数不一致')
                resolution=pool.get('stage_resolution') or {}
                bound_codes={str(s['code']).upper() for s in resolution.get('levels',[]) if isinstance(s,dict)
                    and s.get('code') and s.get('stage_id')==value.get('stage_id')}
                requested=set(required_codes)&bound_codes
                if requested and (any(
                        c.get('stage_name')!=value.get('stage_id') for c in value.get('candidates',[]))
                        or any(str(value.get('stage_id','')).casefold() not in stage_ids_by_code.get(code,set())
                               for code in requested)):
                    raise ActionRejected('候选池关卡与用户目标不一致')
                if pool['coverage'].get('complete') is True:complete_codes.update(requested)
            text='\n'.join(str(before['contract'].get(k) or '') for k in ('goal','user_request'))
            all_summaries=bool(re.search(r'(?:全部|所有|全量).{0,20}(?:作业|候选|摘要)|(?:作业|候选|摘要).{0,12}(?:全部|所有|全量)',text))
            if all_summaries and set(required_codes)-complete_codes:
                raise ActionRejected('全部摘要目标仍有未完整取得候选池的关卡')
        with self.engine._transaction():
            state=self._state(task_id)
            if state['epoch']!=expected_epoch:
                raise ActionRejected('旧资料回执不能完成当前目标')
            row=self._db.execute('SELECT d.dispatch_id FROM workflow_dispatches d JOIN workflow_results r '
                'ON r.dispatch_id=d.dispatch_id WHERE d.task_id=? AND d.role=\'research\' '
                'AND d.epoch=? AND d.revision=? ORDER BY d.rowid DESC LIMIT 1',
                (task_id,expected_epoch,state['revision'])).fetchone()
            receipt=self.dispatch_result(row['dispatch_id']) if row else None
            if (not receipt or receipt!=prior or state['contract']!=before['contract']
                    or receipt.get('evidence_refs')!=evidence_refs
                    or not proofs
                    or not research_satisfies_goal(state['contract'],receipt)):
                raise ActionRejected('没有匹配当前只读目标的持久工具回执')
            if copilot_research_required(state['contract']):
                from .leader_policy import roster_required
                if roster_required(state['contract']):
                    # The facts view is initialized before this transaction.
                    # Constructing it here would try BEGIN inside BEGIN and
                    # prevent every otherwise-valid roster proof from closing.
                    box=roster.current(state['contract'].get('profile_id','local-default'))
                    if not box or any(a.get('box_revision')!=box['box_revision'] for a in
                            (prior.get('assessments') or {}).values() if a.get('classification')=='satisfied'):
                        raise ActionRejected('提交前干员事实代际已更新')
            if state['status']=='completed' and state['control_kind']=='research_verified':
                return self.engine._task_dict(self.engine._task_row(task_id))
            self._fresh(state,writes=True)
            self._db.execute("UPDATE tasks SET status='completed',updated_at=? WHERE id=?",(_utc_now(),task_id))
            self._db.execute("UPDATE workflow_tasks SET writes_enabled=0,control_seq=control_seq+1,"
                "control_kind='research_verified' WHERE task_id=?",(task_id,))
            self._revoke_operations(task_id)
            task=self.engine._task_row(task_id)
            details={'dispatch_id':row['dispatch_id'],'evidence_refs':evidence_refs,'game_input_sent':False}
            self.engine._append_event(task,'research_verified','只读资料目标已有实际工具来源；未进行游戏操作',details)
            self._publish_event(task_id,'workflow.research_verified',details,
                event_key=f'research-verified:{task_id}:{expected_epoch}')
            return self.engine._task_dict(task)

    @staticmethod
    def _event(row: sqlite3.Row) -> dict:
        event = dict(row)
        event["payload"] = json.loads(event["payload"])
        encoded, digest = _encode(event["payload"])
        if digest != event["payload_hash"]:
            raise ValueError("workflow event payload hash mismatch")
        event["acked"] = bool(event["acked"])
        return event

    def _publish_event(self, task_id: str | None, event_type: str, payload: dict, *,
                       event_key: str | None = None, revision: int | None = None,
                       epoch: int | None = None) -> dict:
        """Publish within an ALREADY OPEN engine transaction (including facts)."""
        if not self._db.in_transaction:
            raise RuntimeError("outbox publication requires an engine transaction")
        encoded, digest = _encode(payload)
        if task_id is not None and (revision is None or epoch is None):
            state = self._state(task_id)
            revision = state["revision"] if revision is None else revision
            epoch = state["epoch"] if epoch is None else epoch
        revision, epoch = revision or 0, epoch or 0
        event_key = event_key or uuid.uuid4().hex
        previous = self._db.execute("SELECT * FROM workflow_outbox WHERE event_key=?", (event_key,)).fetchone()
        if previous:
            if (previous["payload_hash"], previous["type"], previous["task_id"], previous["revision"], previous["epoch"]) != (
                    digest, event_type, task_id, revision, epoch):
                raise ValueError("event key collision with different payload")
            return self._event(previous)
        event_id = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO workflow_outbox(event_id,event_key,task_id,revision,epoch,type,payload,payload_hash,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (event_id, event_key, task_id, revision, epoch, event_type, encoded, digest, _utc_now()),
        )
        return self._event(self._db.execute("SELECT * FROM workflow_outbox WHERE event_id=?", (event_id,)).fetchone())

    def pending_events(self, task_id: str | None = None) -> list[dict]:
        with self.engine._lock:
            self.engine._ensure_open()
            query, args = "SELECT * FROM workflow_outbox WHERE acked=0", ()
            if task_id is not None:
                query, args = query + " AND task_id=?", (task_id,)
            return [self._event(row) for row in self._db.execute(query + " ORDER BY rowid", args)]

    def accept_event(self, consumer: str, event_id: str) -> bool:
        consumer = _nonempty(consumer, "consumer")
        with self.engine._transaction():
            event = self._db.execute("SELECT * FROM workflow_outbox WHERE event_id=?", (event_id,)).fetchone()
            if event is None:
                raise KeyError(event_id)
            self._event(event)
            if event['task_id'] and (event['type'].startswith(('command.', 'dispatch.', 'workflow.'))):
                self.management.require_active(event['task_id'])
                self._fresh(event)
            receipt = self._db.execute("SELECT * FROM workflow_inbox WHERE consumer=? AND event_id=?", (consumer, event_id)).fetchone()
            if receipt:
                if receipt["payload_hash"] != event["payload_hash"]:
                    raise ValueError("inbox event payload collision")
                return False
            self._db.execute("INSERT INTO workflow_inbox VALUES(?,?,?,?)", (consumer, event_id, event["payload_hash"], _utc_now()))
            return True

    def ack_event(self, event_id: str) -> None:
        """Caller must synchronously commit its graph checkpoint before ACK."""
        with self.engine._transaction():
            if not self._db.execute("UPDATE workflow_outbox SET acked=1 WHERE event_id=?", (event_id,)).rowcount:
                raise KeyError(event_id)

    @staticmethod
    def _dispatch(row: sqlite3.Row) -> dict:
        record = dict(row)
        record["payload"] = json.loads(record["payload"])
        if _encode(record['payload'])[1] != record['payload_hash']:
            raise ValueError('dispatch input payload hash mismatch')
        record["key"] = record["dispatch_key"]
        return record

    def _dispatch_row(self, dispatch_id: str) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM workflow_dispatches WHERE dispatch_id=?", (dispatch_id,)).fetchone()
        if row is None:
            raise KeyError(dispatch_id)
        return row

    def get_dispatch(self, dispatch_id: str) -> dict:
        with self.engine._lock:
            self.engine._ensure_open()
            return self._dispatch(self._dispatch_row(dispatch_id))

    def dispatch_by_key(self, task_id: str, key: str) -> dict | None:
        with self.engine._lock:
            self.engine._ensure_open()
            row = self._db.execute('SELECT * FROM workflow_dispatches WHERE task_id=? AND dispatch_key=?',
                                   (task_id, key)).fetchone()
            return self._dispatch(row) if row else None

    def ensure_dispatch(self, task_id: str, role: str, key: str, payload: dict) -> dict:
        with self.engine._transaction():
            return self._ensure_dispatch(task_id, role, key, payload)

    def _ensure_dispatch(self, task_id: str, role: str, key: str, payload: dict) -> dict:
        self.management.require_active(task_id)
        role, key = _nonempty(role, "role"), _nonempty(key, "dispatch key")
        encoded, digest = _encode(payload)
        previous = self._db.execute("SELECT * FROM workflow_dispatches WHERE task_id=? AND dispatch_key=?", (task_id, key)).fetchone()
        if previous:
            if previous["payload_hash"] != digest or previous["role"] != role:
                raise ValueError("dispatch key collision with different payload/role")
            return self._dispatch(previous)
        state = self._state(task_id)
        self._fresh(state, writes=True)
        dispatch_id = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO workflow_dispatches(dispatch_id,task_id,revision,epoch,role,dispatch_key,payload,payload_hash,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (dispatch_id, task_id, state["revision"], state["epoch"], role, key, encoded, digest, _utc_now()),
        )
        self._publish_event(task_id, "dispatch.created", {"dispatch_id": dispatch_id, "role": role, "key": key}, event_key=f"dispatch:{dispatch_id}")
        return self._dispatch(self._dispatch_row(dispatch_id))

    def claim_dispatch(self, dispatch_id: str, owner: str) -> dict | None:
        owner = _nonempty(owner, "owner")
        with self.engine._transaction():
            row = self._dispatch_row(dispatch_id)
            try:
                self._fresh(row, writes=True)
            except ActionRejected:
                return None
            if row["status"] != "pending":
                return None
            self._db.execute("UPDATE workflow_dispatches SET status='running',owner=? WHERE dispatch_id=?", (owner, dispatch_id))
            return self._dispatch(self._dispatch_row(dispatch_id))

    def register_runner(self, owner: str) -> None:
        """Register this process, never a caller-supplied PID or claimed timeout."""
        owner = _nonempty(owner, 'runner owner')
        pid, identity = os.getpid(), _process_identity(os.getpid())
        if identity is None:
            raise RuntimeError('Cannot identify the registering process')
        with self.engine._transaction():
            old = self._db.execute('SELECT * FROM workflow_runners WHERE owner=?', (owner,)).fetchone()
            if old:
                if old['pid'] != pid or old['process_identity'] != identity or old['released_at']:
                    raise ValueError('runner owner identity collision')
                return
            self._db.execute('INSERT INTO workflow_runners VALUES(?,?,?,?,NULL)',
                             (owner, pid, identity, _utc_now()))

    def release_runner(self, owner: str) -> None:
        """Caller has joined its execution lock; no handler can still be running."""
        with self.engine._transaction():
            row = self._db.execute('SELECT * FROM workflow_runners WHERE owner=?', (owner,)).fetchone()
            if (row is None or row['pid'] != os.getpid()
                    or row['process_identity'] != _process_identity(os.getpid())):
                raise ActionRejected('只有已确认退出执行区的当前运行器可释放自身')
            self._db.execute('UPDATE workflow_runners SET released_at=? WHERE owner=?', (_utc_now(), owner))

    def audit_abandoned_dispatches(self, task_id: str) -> list[dict]:
        """Only OS absence/replaced creation or joined-runner release proves exit.

        Missing registration, access denied and a live original process keep the
        running claim intact. MAA results become unknown; no GUI operation or
        effect barrier is cleared and no replacement role is auto-dispatched.
        Recycled tasks receive only a historical retirement receipt, without
        publishing ordinary dispatch work or reopening their execution authority.
        """
        recovered = []
        with self.engine._transaction():
            lifecycle = self.management._lifecycle(task_id)['state']
            if lifecycle not in {'active', 'trashed', 'purge_pending'}:
                return recovered
            rows = self._db.execute(
                "SELECT d.*,r.pid,r.process_identity,r.released_at FROM workflow_dispatches d "
                "LEFT JOIN workflow_runners r ON r.owner=d.owner WHERE d.task_id=? AND d.status='running'",
                (task_id,),
            ).fetchall()
            for row in rows:
                if row['pid'] is None:
                    continue
                try:
                    gone = bool(row['released_at']) or _process_identity(row['pid']) != row['process_identity']
                except OSError:
                    continue
                if not gone:
                    continue
                status = 'unknown' if row['role'] == 'maa' else 'interrupted'
                # Keep the effect journal conservative even when restart did
                # not first pass through the product's pause transaction.
                self._db.execute("UPDATE workflow_operations SET status=CASE WHEN submitted_at IS NULL "
                    "THEN 'not_issued' ELSE 'unknown' END WHERE dispatch_id=? "
                    "AND status IN ('planned','issued','submitted')", (row['dispatch_id'],))
                self._db.execute("UPDATE workflow_slots SET barrier_operation_id=NULL WHERE barrier_operation_id IN "
                    "(SELECT operation_id FROM workflow_operations WHERE dispatch_id=? AND status='not_issued')",
                    (row['dispatch_id'],))
                result = {'status': status, 'code': 'owner_exited', 'requires_diagnosis': True,
                          'dispatch_id': row['dispatch_id'], 'role': row['role'],
                          'summary': '原委派执行者已确认退出；结果未提交，须由 Leader 诊断后决定恢复'}
                if lifecycle in {'trashed','purge_pending'}:
                    # Owner exit is proven above under this same writer lock.
                    # Ordinary late role results remain forbidden by lifecycle;
                    # close only the abandoned claim so recycle cleanup can
                    # proceed. Unknown MAA receipts and barriers stay protected.
                    encoded, digest = _encode(result)
                    self._db.execute('INSERT INTO workflow_results VALUES(?,?,?,?,?)',
                        (row['dispatch_id'], encoded, digest, status, _utc_now()))
                    self._db.execute('UPDATE workflow_dispatches SET status=? WHERE dispatch_id=?',
                        (status, row['dispatch_id']))
                else:
                    self._finish_dispatch(row['dispatch_id'], result, status=status)
                recovered.append(self.get_dispatch(row['dispatch_id']))
        return recovered

    def _research_artifact_refs(self, result: dict) -> list[dict]:
        """Check registered local provenance and, when present, its download."""
        refs = result.get('candidate_refs', [])
        if not isinstance(refs, list) or not 0 < len(refs) <= 100:
            raise ActionRejected('研究采用缺少已登记的完整本地产物')
        root, checked = self.engine.data_dir.resolve(), []

        def verify(ref):
            if (not isinstance(ref, dict) or set(ref) != {'path', 'sha256'}
                    or not isinstance(ref['path'], str) or not isinstance(ref['sha256'], str)
                    or len(ref['sha256']) != 64):
                raise ValueError('研究产物引用无效')
            path = Path(ref['path']).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('研究产物路径或大小无效')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != ref['sha256']:
                raise ValueError('研究产物哈希已变化')
            if ref not in checked:
                checked.append(ref)
            if len(checked) > 400:
                raise ValueError('研究产物引用过多')
            return raw

        def nested_refs(value, depth=0):
            if depth > 12:
                raise ValueError('研究产物引用嵌套过深')
            if isinstance(value, dict):
                if 'path' in value and 'sha256' in value:
                    verify({'path': value['path'], 'sha256': value['sha256']})
                for child in value.values():
                    nested_refs(child, depth + 1)
            elif isinstance(value, list):
                for child in value:
                    nested_refs(child, depth + 1)

        for ref in refs:
            raw = verify(ref)
            document = json.loads(raw.decode('utf-8-sig'))
            nested_refs(document)
        return checked

    def historical_research(self, task_id: str) -> list[dict]:
        """Bounded review candidates only; no historical result enters role_results."""
        with self.engine._lock:
            state = self._state(task_id)
            rows = self._db.execute(
                "SELECT d.* FROM workflow_dispatches d JOIN workflow_research_artifacts a "
                "ON a.dispatch_id=d.dispatch_id WHERE d.task_id=? AND d.role='research' "
                "AND NOT EXISTS(SELECT 1 FROM workflow_research_adoptions x WHERE x.dispatch_id=d.dispatch_id) "
                "AND d.status='completed' AND d.epoch<>? AND d.revision=? ORDER BY d.rowid DESC LIMIT 8",
                (task_id, state['epoch'], state['revision']),
            ).fetchall()
            return [{'dispatch_id': row['dispatch_id'], 'input_task': self._dispatch(row)['payload'].get('task', ''),
                     'epoch': row['epoch'], 'revision': row['revision'], 'created_at': row['created_at'],
                     'summary': str((self.dispatch_result(row['dispatch_id'], current_only=False) or {}).get('summary', ''))[:1000]}
                    for row in rows]

    def research_account(self, task_id: str) -> dict:
        """Snapshot the registered roster input; missing facts stay explicit."""
        with self.engine._lock:
            profile = self._state(task_id)['contract'].get('profile_id')
            if profile is None:
                return {}
            exists = self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_roster_current'").fetchone()
            row = self._db.execute('SELECT b.* FROM workflow_roster_current c JOIN workflow_roster_batches b '
                'ON b.batch_id=c.batch_id WHERE c.profile_id=?', (profile,)).fetchone() if exists else None
            if row is not None and hashlib.sha256(row['payload'].encode('utf-8')).hexdigest() != row['content_hash']:
                raise ValueError('registered roster content hash mismatch')
            return {'profile_id': profile, 'box_revision': row['box_revision'] if row else None}

    def interrupted_dispatches(self, task_id: str) -> list[dict]:
        """Historical interruptions remain visible to a new-epoch Leader."""
        with self.engine._lock:
            rows = self._db.execute("SELECT d.* FROM workflow_dispatches d JOIN workflow_results r "
                "ON r.dispatch_id=d.dispatch_id WHERE d.task_id=? AND r.status IN ('unknown','interrupted') "
                "ORDER BY d.rowid DESC LIMIT 8", (task_id,)).fetchall()
            return [{'dispatch_id': row['dispatch_id'], 'role': row['role'], 'epoch': row['epoch'],
                     'status': row['status'], 'requires_diagnosis': True} for row in rows]

    def adopt_research(self, task_id: str, source_dispatch_id: str, key: str, *, input_task: str) -> dict:
        """A new Leader review of completed read-only research, never a UI replay."""
        original = self.dispatch_result(source_dispatch_id, current_only=False)
        if not original or original.get('adoption') or original.get('status') in {'failed', 'unknown', 'interrupted'}:
            raise ActionRejected('历史研究没有原始已完成结果')
        # File reads precede the short writer transaction; admission reads the
        # immutable registrations again, and MAA rechecks the files at use time.
        refs = self._research_artifact_refs(original)
        with self.engine._transaction():
            current, source = self._state(task_id), self._dispatch(self._dispatch_row(source_dispatch_id))
            self._fresh(current, writes=True)
            payload = source['payload']
            if (source['task_id'] != task_id or source['role'] != 'research' or source['status'] != 'completed'
                    or source['revision'] != current['revision'] or source['epoch'] == current['epoch']
                    or payload.get('contract') != current['contract'] or payload.get('task') != input_task):
                raise ActionRejected('历史研究与当前合同、输入或代际不一致')
            created = datetime.fromisoformat(source['created_at'].replace('Z', '+00:00'))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - created).total_seconds()
            if age < 0 or age > _RESEARCH_MAX_AGE_SECONDS:
                raise ActionRejected('历史研究已超出本地采用新鲜度，需重新核实来源')
            if self.dispatch_result(source_dispatch_id, current_only=False) != original:
                raise ActionRejected('历史研究结果已改变')
            profile = payload.get('profile_id', payload.get('contract', {}).get('profile_id'))
            box_revision = payload.get('box_revision', payload.get('contract', {}).get('box_revision'))
            if profile is not None:
                if profile != current['contract'].get('profile_id'):
                    raise ActionRejected('研究账号已改变或未核实')
                exists = self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_roster_current'").fetchone()
                box = self._db.execute('SELECT b.box_revision FROM workflow_roster_current c JOIN workflow_roster_batches b '
                    'ON b.batch_id=c.batch_id WHERE c.profile_id=?', (profile,)).fetchone() if exists else None
                if not box or box['box_revision'] != box_revision:
                    raise ActionRejected('研究账号档案版本已改变或未核实')
            elif box_revision is not None:
                raise ActionRejected('研究档案版本缺少已核实账号')
            registered = self._db.execute('SELECT * FROM workflow_research_artifacts WHERE dispatch_id=?',
                                          (source_dispatch_id,)).fetchone()
            if registered is None or _encode(refs)[1] != registered['payload_hash']:
                raise ActionRejected('历史研究没有匹配的已登记产物哈希')
            # Exact contract equality also checks profile/box versions; never infer
            # these from a model summary or adopt unknown GUI effects.
            adopted = self._ensure_dispatch(task_id, 'research', key,
                {'contract': current['contract'], 'task': input_task, 'epoch': current['epoch'],
                 'revision': current['revision'], 'source_dispatch_id': source_dispatch_id})
            result_hash = _encode(original)[1]
            self._db.execute('INSERT OR IGNORE INTO workflow_research_adoptions VALUES(?,?,?,?,?)',
                (adopted['dispatch_id'], source_dispatch_id, result_hash, _encode(input_task)[1], _utc_now()))
            result = {**original, 'adoption': {'source_dispatch_id': source_dispatch_id,
                      'dispatch_id': adopted['dispatch_id'], 'epoch': current['epoch']}}
            self._finish_dispatch(adopted['dispatch_id'], result, artifact_refs=refs)
            self._publish_event(task_id, 'research.adopted', {'dispatch_id': adopted['dispatch_id'],
                'source_dispatch_id': source_dispatch_id, 'source_result_hash': result_hash},
                event_key='adoption:' + adopted['dispatch_id'])
            return self.get_dispatch(adopted['dispatch_id'])

    def research_adoption_matches(self, dispatch_id: str, source_dispatch_id: str, reference: dict) -> bool:
        """Adapter admission: original provenance is usable only via this current review."""
        with self.engine._lock:
            row = self._db.execute('SELECT * FROM workflow_research_adoptions WHERE dispatch_id=? AND source_dispatch_id=?',
                                  (dispatch_id, source_dispatch_id)).fetchone()
            if row is None:
                return False
            result, source = self.dispatch_result(dispatch_id), self.dispatch_result(source_dispatch_id, current_only=False)
            if (not result or not source or reference not in result.get('candidate_refs', [])
                    or _encode(source)[1] != row['source_result_hash']):
                return False
            source_dispatch = self.get_dispatch(source_dispatch_id)
            adopted = self.get_dispatch(dispatch_id)
        # No file hashing under the lock required by trusted stop revocation.
        refs = self._research_artifact_refs(source)
        with self.engine._lock:
            if self.dispatch_result(dispatch_id) != result:
                return False
            account = self.research_account(adopted['task_id'])
            payload = source_dispatch['payload']
            profile = payload.get('profile_id', payload.get('contract', {}).get('profile_id'))
            box = payload.get('box_revision', payload.get('contract', {}).get('box_revision'))
            if (profile != account.get('profile_id') or box != account.get('box_revision')
                    or (profile is not None and box is None)):
                return False
            registered = self._db.execute('SELECT payload_hash FROM workflow_research_artifacts WHERE dispatch_id=?',
                                         (source_dispatch_id,)).fetchone()
            return bool(registered and registered['payload_hash'] == _encode(refs)[1])

    def finish_dispatch(self, dispatch_id: str, result: dict, status: str = "completed") -> dict:
        refs = None
        if (status == 'completed' and result.get('candidate_refs')
                and self.get_dispatch(dispatch_id)['role'] == 'research'):
            refs = self._research_artifact_refs(result)
        with self.engine._transaction():
            return self._finish_dispatch(dispatch_id, result, status, artifact_refs=refs)

    def _finish_dispatch(self, dispatch_id: str, result: dict, status: str = "completed", *, artifact_refs=None) -> dict:
        if status not in _DISPATCH_TERMINAL:
            raise ValueError("invalid dispatch result status")
        encoded, digest = _encode(result)
        row = self._dispatch_row(dispatch_id)
        self.management.require_active(row['task_id'])
        previous = self._db.execute("SELECT * FROM workflow_results WHERE dispatch_id=?", (dispatch_id,)).fetchone()
        if previous:
            if (previous["payload_hash"], previous["status"]) != (digest, status):
                raise ValueError("immutable dispatch result collision")
            return self._dispatch(row)
        if row['role'] == 'research' and status == 'completed' and result.get('candidate_refs'):
            if artifact_refs is None:
                raise ValueError('research artifact hashes must be checked before entering the transaction')
            encoded_refs, refs_hash = _encode(artifact_refs)
            self._db.execute('INSERT INTO workflow_research_artifacts VALUES(?,?,?)',
                             (dispatch_id, encoded_refs, refs_hash))
        self._db.execute("INSERT INTO workflow_results VALUES(?,?,?,?,?)", (dispatch_id, encoded, digest, status, _utc_now()))
        self._db.execute("UPDATE workflow_dispatches SET status=? WHERE dispatch_id=?", (status, dispatch_id))
        self._publish_event(row["task_id"], "dispatch.result", {"dispatch_id": dispatch_id, "status": status, "result": result},
                            event_key=f"result:{dispatch_id}", revision=row["revision"], epoch=row["epoch"])
        return self._dispatch(self._dispatch_row(dispatch_id))

    def dispatch_result(self, dispatch_id: str, current_only: bool = True) -> dict | None:
        with self.engine._lock:
            self.engine._ensure_open()
            row = self._dispatch_row(dispatch_id)
            if current_only:
                try:
                    self._fresh(row)
                except ActionRejected:
                    return None
            result = self._db.execute("SELECT * FROM workflow_results WHERE dispatch_id=?", (dispatch_id,)).fetchone()
            if result is None:
                return None
            decoded = json.loads(result["result"])
            if _encode(decoded)[1] != result["payload_hash"]:
                raise ValueError("dispatch result payload hash mismatch")
            return decoded

    def _operation_row(self, operation_id: str) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM workflow_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return row

    @staticmethod
    def _operation(row: sqlite3.Row) -> dict:
        record = dict(row)
        record["action"] = json.loads(record["action"])
        record["result"] = json.loads(record["result"]) if record["result"] is not None else None
        record["starts"], record["stop_only"] = bool(record["starts"]), bool(record["stop_only"])
        return record

    def get_operation(self, operation_id: str) -> dict:
        with self.engine._lock:
            self.engine._ensure_open()
            return self._operation(self._operation_row(operation_id))

    def pending_operations(self, task_id: str | None = None) -> list[dict]:
        with self.engine._lock:
            self.engine._ensure_open()
            query = "SELECT * FROM workflow_operations WHERE status IN ('planned','issued','submitted','unknown')"
            args = ()
            if task_id is not None:
                query += " AND task_id=?"
                args = (task_id,)
            return [self._operation(row) for row in self._db.execute(query + " ORDER BY rowid", args)]

    def slot_state(self, slot: str = "desktop-maa") -> dict:
        with self.engine._lock:
            self.engine._ensure_open()
            row = self._db.execute("SELECT * FROM workflow_slots WHERE slot=?", (slot,)).fetchone()
            if row is None:
                return {"slot": slot, "barrier_operation_id": None, "attempts_reserved": 0, "control_seq": 0}
            state = dict(row)
            state["status"] = self._operation_row(state["barrier_operation_id"])["status"] if state["barrier_operation_id"] else "idle"
            return state

    @staticmethod
    def _start_limit(contract: dict) -> int:
        limit = contract.get("attempt_limit", contract.get("max_attempts", 0))
        if contract.get("allow_start") is not True or type(limit) is not int or limit <= 0:
            raise ActionRejected("当前合同禁止开始或没有尝试额度")
        return limit

    def require_start_attempt_available(self, task_id: str) -> None:
        with self.engine._lock:
            limit = self._start_limit(self._state(task_id)['contract'])
            used = self._db.execute('SELECT COUNT(*) FROM workflow_operations '
                'WHERE task_id=? AND starts=1 AND issued_at IS NOT NULL', (task_id,)).fetchone()[0]
            if used >= limit:
                raise ActionRejected('尝试额度已预留或用尽')

    def _check_compound_start(self, state, dispatch_id, action):
        if state['contract'].get('workflow_scope') != 'maa_import_then_battle':
            return
        from .battle_contract import compound_authorized
        from .battle_import_binding import current_import_receipt, ImportBindingError

        dispatch = self._dispatch(self._dispatch_row(dispatch_id))
        payload = dispatch['payload']
        import_dispatch_id = payload.get('import_dispatch_id')
        if (not compound_authorized(state['contract']) or payload.get('contract') != state['contract']
                or any(dispatch[key] != state[key] for key in ('task_id','revision','epoch'))
                or dispatch['role'] != 'maa' or dispatch['status'] != 'running'
                or payload.get('execution_step') != 'battle'
                or not isinstance(import_dispatch_id, str)
                or action.get('kind') != 'start' or action.get('target') != 'battle_start'
                or action.get('import_dispatch_id') != import_dispatch_id):
            raise ActionRejected('当前委派没有导入后启动 MAA 的权限')
        try:
            receipt = current_import_receipt(self, state, import_dispatch_id)
        except ImportBindingError as error:
            raise ActionRejected(str(error)) from error
        if action.get('import_proof_ref') != receipt['receipt_ref']:
            raise ActionRejected('开始操作缺少当前单作业导入证据')
        proof = self._battle_reference(receipt['receipt_ref'])
        if (not isinstance(proof, dict) or proof.get('transport') != 'screenshot_ocr'
                or proof.get('start_clicked') is not False or proof.get('code') != receipt.get('mystery_code')
                or not isinstance(proof.get('actions'), list)):
            raise ActionRejected('当前导入证据无法用于启动 MAA')
        rows = self._db.execute('SELECT operation_id, action FROM workflow_operations WHERE dispatch_id=? '
            'AND task_id=? AND revision=? AND epoch=? AND starts=0 AND stop_only=0 '
            "AND status='verified' AND submitted_at IS NOT NULL", (import_dispatch_id, state['task_id'],
                state['revision'], state['epoch']))
        for operation_id, encoded in rows:
            operation = json.loads(encoded)
            if (operation.get('kind') == 'maa_import' and operation.get('action') == 'replace_code'
                    and operation.get('code') == receipt['mystery_code']
                    and any(isinstance(item, dict) and item.get('operation_id') == operation_id
                            and item.get('action') == 'replace_code' for item in proof['actions'])):
                return
        raise ActionRejected('导入证据没有对应的真实代码输入账本')

    def _check_cross_task_battle_source(self, state, dispatch_id, action):
        """CAS a worker-verified single import under this same SQLite writer.

        The worker verifies artifact bytes before admission. This gate rechecks
        the exact source generation and receipt in the DB, never reading files
        or searching another task for an import. New gameplay authority is
        independent of the source import's intentionally disabled start flag.
        """
        contract = state['contract']
        related = any(key in action for key in (
            'source_import_binding', 'source_import_task_id'))
        if contract.get('workflow_scope') != 'maa_battle_start':
            if related:
                raise ActionRejected('跨任务导入凭据仅用于明确授权的单次作战')
            return
        source_id = contract.get('source_task_id')
        if source_id is None and not related:
            return  # The existing explicitly loaded-job path has no source lookup.
        from .battle_contract import authorized
        try:
            binding = action.get('source_import_binding')
            if (not authorized(contract) or contract.get('current_deliverable') != 'gameplay'
                    or not isinstance(source_id, str) or not source_id or source_id == state['task_id']
                    or action.get('kind') != 'start' or action.get('target') != 'battle_start'
                    or not isinstance(binding, dict)
                    or set(binding) != {'task_id', 'revision', 'epoch', 'dispatch_id', 'receipt_ref'}
                    or binding['task_id'] != source_id or action.get('source_import_task_id') != source_id
                    or any(type(binding[key]) is not int or binding[key] <= 0 for key in ('revision', 'epoch'))
                    or not isinstance(binding['dispatch_id'], str) or not binding['dispatch_id']):
                raise ValueError('source binding')
            reference = binding['receipt_ref']
            if (not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}
                    or not isinstance(reference['path'], str) or not Path(reference['path']).is_absolute()
                    or not isinstance(reference['sha256'], str)
                    or not re.fullmatch('[0-9a-f]{64}', reference['sha256'])
                    or action.get('import_proof_ref') != reference):
                raise ValueError('source proof reference')
            target_dispatch = self._dispatch(self._dispatch_row(dispatch_id))
            if (any(target_dispatch[key] != state[key] for key in ('task_id', 'revision', 'epoch'))
                    or target_dispatch['role'] != 'maa' or target_dispatch['status'] != 'running'
                    or target_dispatch['payload'].get('contract') != contract):
                raise ValueError('gameplay dispatch')
            self.management.require_active(source_id)
            source = self._state(source_id)
            if (any(source[key] != binding[key] for key in ('revision', 'epoch'))
                    or source['epoch'] != source['task_epoch'] or source['writes_enabled']
                    or source['status'] != 'completed' or source['control_kind'] != 'import_verified'
                    or source['contract'].get('current_deliverable') != 'maa_import'):
                raise ValueError('source generation or completion')
            latest = self._db.execute('SELECT * FROM workflow_dispatches '
                "WHERE task_id=? AND revision=? AND epoch=? AND role='maa' ORDER BY rowid DESC LIMIT 1",
                (source_id, source['revision'], source['epoch'])).fetchone()
            if latest is None or latest['dispatch_id'] != binding['dispatch_id']:
                raise ValueError('source dispatch replaced')
            dispatch = self._dispatch(latest)
            payload = dispatch['payload']
            import_step = (payload.get('execution_step') == 'import'
                or 'execution_step' not in payload
                and source['contract'].get('workflow_scope') != 'maa_import_then_battle')
            if (dispatch['status'] != 'completed' or not import_step
                    or payload.get('contract') != source['contract']):
                raise ValueError('source dispatch not a completed import')
            receipt = self.dispatch_result(dispatch['dispatch_id'])
            if (not receipt or receipt.get('status') != 'import_verified'
                    or receipt.get('transport') != 'screenshot_keyboard'
                    or receipt.get('battle_start_submitted') is not False
                    or receipt.get('queue_receipt_ref') or receipt.get('single_document_plan_ref')
                    or receipt.get('receipt_ref') != reference
                    or reference not in (receipt.get('evidence_refs') or [])
                    or not isinstance(receipt.get('mystery_code'), str)
                    or not re.fullmatch(r'prts://[1-9][0-9]{0,11}', receipt['mystery_code'])):
                raise ValueError('source single-import receipt')
            rows = self._db.execute('SELECT action,payload_hash FROM workflow_operations '
                'WHERE task_id=? AND revision=? AND epoch=? AND dispatch_id=? '
                "AND starts=0 AND stop_only=0 AND slot='desktop-maa' AND status='verified' AND submitted_at IS NOT NULL",
                (source_id, source['revision'], source['epoch'], dispatch['dispatch_id']))
            for row in rows:
                imported = json.loads(row['action'])
                if _encode(imported)[1] != row['payload_hash']:
                    raise ValueError('source import action changed')
                if (imported.get('kind') == 'maa_import' and imported.get('action') == 'replace_code'
                        and imported.get('code') == receipt['mystery_code']):
                    return
            raise ValueError('source import input missing')
        except (ValueError, TypeError, KeyError):
            raise ActionRejected('来源单作业导入在开始提交前已变化或未核实') from None

    @staticmethod
    def _queue_observation_scroll(action, parent):
        """Allow only the same dispatch's queue-list observation under its effect."""
        guard = action.get('queue_guard') or {}
        parent_action = json.loads(parent['action'])
        prior = parent_action.get('queue_guard') or {}
        return (action.get('kind') == parent_action.get('kind') == 'queue_gui'
                and action.get('queue_observation_of') == parent['operation_id']
                and guard.get('phase') == 'scroll' and guard.get('target_key') is None
                and guard.get('row_identity') is None
                and prior.get('phase') in {'code','add','delete'}
                and parent['status'] in {'submitted','unknown'} and parent['submitted_at'] is not None
                and parent['effect_key'].startswith('queue:') and not parent['starts'] and not parent['stop_only']
                and guard.get('plan_sha256') == prior.get('plan_sha256')
                and all(guard.get(key) == parent[key] for key in ('task_id','revision','epoch','dispatch_id')))

    def _prepare(self, task_id: str, dispatch_id: str | None, effect_key: str, action: dict,
                 slot: str, starts: bool, stop_only: bool, control_seq: int | None = None) -> dict:
        effect_key, slot = _nonempty(effect_key, "effect key"), _nonempty(slot, "control slot")
        if type(action) is not dict:
            raise TypeError("operation action must be a JSON object")
        if action.get("kind") in {"start", "begin_battle", "start_battle"} and not starts:
            raise ActionRejected("开始动作必须明确声明并预留尝试额度")
        encoded, digest = _encode(action)
        with self.engine._transaction():
            if not stop_only:
                self.management.require_active(task_id)
            previous = self._db.execute("SELECT * FROM workflow_operations WHERE slot=? AND effect_key=?", (slot, effect_key)).fetchone()
            if previous:
                if (previous["task_id"], previous["dispatch_id"], previous["payload_hash"], bool(previous["starts"]), bool(previous["stop_only"])) != (
                        task_id, dispatch_id, digest, starts, stop_only):
                    raise ValueError("effect key collision with different operation")
                return self._operation(previous)
            state = self._state(task_id)
            slot_state = self._ensure_slot(slot)
            if stop_only:
                self._check_inactive_stop_owner(task_id, slot_state)
                if (control_seq != state["control_seq"] or state["control_kind"] not in {"pause", "stop"}
                        or state["writes_enabled"] or type(action) is not dict or action.get("kind") != "stop"):
                    raise ActionRejected("没有当前受信 stop-only 指令")
            else:
                self._fresh(state, writes=True)
                dispatch = self._dispatch_row(dispatch_id)
                self._fresh(dispatch, writes=True)
                if dispatch["task_id"] != task_id or dispatch["role"] != "maa":
                    raise ActionRejected("只有当前 MAA 委派可以准备 GUI 操作")
                if slot_state["barrier_operation_id"]:
                    parent = self._operation_row(slot_state['barrier_operation_id'])
                    if (slot != 'desktop-maa' or not effect_key.startswith('queue:') or starts
                            or not self._queue_observation_scroll(action,parent)):
                        raise ActionRejected("控制槽有未决或活动效果，须先对账")
                if starts:
                    self._start_limit(state["contract"])
                    self._check_compound_start(state, dispatch_id, action)
                    self._check_cross_task_battle_source(state, dispatch_id, action)
            operation_id = uuid.uuid4().hex
            self._db.execute(
                "INSERT INTO workflow_operations(operation_id,task_id,dispatch_id,revision,epoch,control_seq,slot_seq,"
                "effect_key,action,payload_hash,slot,starts,stop_only,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (operation_id, task_id, dispatch_id, state["revision"], state["epoch"], state["control_seq"], slot_state["control_seq"],
                 effect_key, encoded, digest, slot, int(starts), int(stop_only), _utc_now()),
            )
            return self._operation(self._operation_row(operation_id))

    def prepare_operation(self, task_id: str, dispatch_id: str, effect_key: str, action: dict,
                          slot: str = "desktop-maa", starts: bool = False) -> dict:
        return self._prepare(task_id, dispatch_id, effect_key, action, slot, starts, False)

    def prepare_control_operation(self, task_id: str, control_seq: int, effect_key: str,
                                  action: dict, slot: str = "desktop-maa") -> dict:
        return self._prepare(task_id, None, effect_key, action, slot, False, True, control_seq)

    def _admissible(self, row: sqlite3.Row) -> tuple[dict, sqlite3.Row]:
        state = self._state(row["task_id"])
        slot = self._ensure_slot(row["slot"])
        if row["stop_only"]:
            self._check_inactive_stop_owner(row['task_id'], slot)
            self._fresh(row)
            if (state["control_seq"] != row["control_seq"] or state["control_kind"] not in {"pause", "stop"}
                    or state["writes_enabled"]):
                raise ActionRejected("受信停止指令已经失效")
        else:
            self._fresh(row, writes=True)
            if state["control_seq"] != row["control_seq"] or slot["control_seq"] != row["slot_seq"]:
                raise ActionRejected("停止控制序号已改变")
            if slot["barrier_operation_id"] not in (None, row["operation_id"]):
                parent = self._operation_row(slot['barrier_operation_id'])
                if (row['slot'] != 'desktop-maa' or not row['effect_key'].startswith('queue:')
                        or not self._queue_observation_scroll(json.loads(row['action']),parent)):
                    raise ActionRejected("控制槽已有未决效果")
            observation_parent = json.loads(row['action']).get('queue_observation_of')
            if observation_parent and slot['barrier_operation_id'] != observation_parent:
                raise ActionRejected('原队列输入的观察屏障已经变化')
        return state, slot

    def _check_inactive_stop_owner(self, task_id, slot):
        """Recycled tasks may reconcile their own input, never stop a peer."""
        lifecycle = self.management._lifecycle(task_id)['state']
        if slot['barrier_operation_id']:
            barrier = self._operation_row(slot['barrier_operation_id'])
            if barrier['task_id'] != task_id:
                raise ActionRejected('控制槽属于其他任务，不能代其停止')
        if lifecycle == 'active':
            return
        if lifecycle not in {'trashed','purge_pending'} or not slot['barrier_operation_id']:
            raise ActionRejected('已回收任务没有可停止的当前输入')

    def issue_operation(self, operation_id: str) -> dict:
        with self.engine._transaction():
            row = self._operation_row(operation_id)
            if row["status"] != "planned":
                raise ActionRejected("操作已经认领或不可重放")
            state, slot = self._admissible(row)
            if row["starts"]:
                self._check_compound_start(state, row['dispatch_id'], json.loads(row['action']))
                self._check_cross_task_battle_source(state, row['dispatch_id'], json.loads(row['action']))
                self.require_start_attempt_available(row['task_id'])
                self._db.execute("UPDATE workflow_slots SET attempts_reserved=attempts_reserved+1 WHERE slot=?", (row["slot"],))
            self._db.execute("UPDATE workflow_operations SET status='issued',issued_at=? WHERE operation_id=?", (_utc_now(), operation_id))
            observation = json.loads(row['action']).get('queue_observation_of')
            if (not row["stop_only"] or slot["barrier_operation_id"] is None) and not observation:
                self._db.execute("UPDATE workflow_slots SET barrier_operation_id=? WHERE slot=?", (operation_id, row["slot"]))
            return self._operation(self._operation_row(operation_id))

    def _check_import_guard(self, operation, guard):
        """CAS the source facts whose external bytes were checked before BEGIN."""
        if any(guard.get(key) != operation[key] for key in ('task_id', 'revision', 'epoch', 'dispatch_id')):
            raise ActionRejected('导入准备校验不属于当前操作')
        state = self._state(operation['task_id'])
        dispatch = self._dispatch(self._dispatch_row(operation['dispatch_id']))
        if (state['status'] in {'paused', 'cancelled', 'completed', 'failed', 'needs_input'}
                or _encode(state['contract'])[1] != guard['contract_sha256']
                or any(dispatch[key] != operation[key] for key in ('task_id', 'revision', 'epoch'))
                or dispatch['role'] != 'maa' or dispatch['status'] != 'running'
                or ('contract' in dispatch['payload'] and dispatch['payload']['contract'] != state['contract'])):
            raise ActionRejected('导入合同在提交前已变化')
        if (state['contract'].get('workflow_scope') == 'maa_import_then_battle'
                and dispatch['payload'].get('execution_step') != 'import'):
            raise ActionRejected('当前委派无权提交导入操作')
        binding = guard['source_binding']
        source_id, revision = binding['source_task_id'], binding['source_revision']
        self.management.require_active(source_id)
        source = self._state(source_id)
        expected_source = state['contract'].get('source_task_id')
        if not expected_source and state['contract'].get('current_deliverable') == 'maa_import':
            expected_source = operation['task_id']
        if (source['revision'] != revision or _encode(source['contract'])[1] != binding['source_contract_sha256']
                or expected_source != source_id):
            raise ActionRejected('导入来源合同在提交前已变化')
        plan = self._db.execute('SELECT payload,payload_hash FROM leader_plans WHERE task_id=? AND revision=?',
                                (source_id, revision)).fetchone()
        value = json.loads(plan['payload']) if plan else None
        if (not plan or _encode(value)[1] != plan['payload_hash']
                or value.get('structure_ref') != binding['structure_ref']
                or [stage for stage in value['stages'] if stage['stage_key'] == binding['resolved_stage']['stage_key']]
                   != [binding['resolved_stage']]):
            raise ActionRejected('导入来源计划在提交前已变化')
        step = self._db.execute('SELECT stage_key,status,result FROM leader_steps '
            'WHERE task_id=? AND revision=? AND step_id=?', (source_id, revision, binding['step_id'])).fetchone()
        result = json.loads(step['result']) if step and step['result'] else None
        if (not step or result != binding['step_result']
                or step['status'] != binding['step_result']['status']
                or step['stage_key'] != binding['resolved_stage']['stage_key']
                or result.get(binding['slot'] + '_ref') != binding['code_ref']):
            raise ActionRejected('导入所选来源步骤在提交前已变化')
        code = self._db.execute('SELECT * FROM code_receipts WHERE id=?',
                                 (binding['code_ref']['receipt_id'],)).fetchone()
        payload = json.loads(code['payload']) if code else None
        if (not code or code['hash'] != binding['code_ref']['sha256'] or _encode(payload)[1] != code['hash']
                or code['task_id'] != source_id or code['revision'] != revision or code['step_id'] != binding['step_id']
                or payload.get('resolved_stage') != binding['resolved_stage']):
            raise ActionRejected('导入代码凭据在提交前已变化')
        box = self._db.execute('SELECT b.* FROM workflow_roster_current c JOIN workflow_roster_batches b '
            'ON b.batch_id=c.batch_id WHERE c.profile_id=?', ('local-default',)).fetchone()
        roster = json.loads(box['payload']) if box else {}
        if box:
            roster['content_hash'] = box['content_hash']
        if (not box or hashlib.sha256(box['payload'].encode('utf-8')).hexdigest() != box['content_hash']
                or {key: roster.get(key) for key in ('profile_id', 'batch_id', 'box_revision', 'content_hash', 'source')}
                   != binding['roster_ref']):
            raise ActionRejected('导入本地 Box 在提交前已变化')
        expected_key = _encode({'task_id': operation['task_id'], 'revision': operation['revision'],
            'source_task_id': source_id, 'step_id': binding['step_id'], 'slot': binding['slot']})[1]
        cached = self._db.execute('SELECT payload,payload_hash FROM import_preparations WHERE cache_key=?',
                                   (expected_key,)).fetchone()
        material = json.loads(cached['payload']) if cached else None
        if (guard['cache_key'] != expected_key or not cached or cached['payload_hash'] != guard['cache_payload_hash']
                or _encode(material)[1] != cached['payload_hash'] or material.get('source_binding') != binding):
            raise ActionRejected('导入准备凭据在提交前已变化')

    def enter_submission(self, operation_id: str, *, import_guard: dict | None = None,
                         queue_guard: dict | None = None) -> dict:
        # This COMMIT is the last durable gate before a caller invokes GUI input.
        with self.engine._transaction():
            row = self._operation_row(operation_id)
            if row["status"] != "issued" or row["submitted_at"] is not None:
                raise ActionRejected("操作未认领、已撤销或已经进入提交区")
            state, _ = self._admissible(row)
            if row['starts']:
                self._check_compound_start(state, row['dispatch_id'], json.loads(row['action']))
                self._check_cross_task_battle_source(state, row['dispatch_id'], json.loads(row['action']))
            if row['effect_key'].startswith('queue:'):
                from .maa_queue_store import check_queue_guard_in_transaction
                check_queue_guard_in_transaction(self, row, queue_guard, import_guard)
            elif queue_guard is not None:
                raise ActionRejected('队列校验不能用于其他操作')
            if import_guard is not None:
                self._check_import_guard(row, import_guard)
            self._db.execute("UPDATE workflow_operations SET status='submitted',submitted_at=? WHERE operation_id=?", (_utc_now(), operation_id))
            return self._operation(self._operation_row(operation_id))

    def finish_operation(self, operation_id: str, status: str, result: dict) -> dict:
        if status not in _OPERATION_TERMINAL:
            raise ValueError("invalid operation result status")
        encoded, digest = _encode(result)
        with self.engine._transaction():
            row = self._operation_row(operation_id)
            if row['result'] and json.loads(row['result']).get('session_retired') is True:
                raise ActionRejected('旧 MAA 会话已结束，迟到结果不能重新打开输入屏障')
            if row["status"] == "not_issued" and status != "not_issued":
                raise ActionRejected("已确认未提交的操作不能改写为在途效果")
            if status == "not_issued" and row["submitted_at"] is not None:
                raise ActionRejected("已经跨入输入提交区，不能声称未提交")
            if status == "verified" and row["submitted_at"] is None:
                raise ActionRejected("尚未提交的操作不能记为已核验效果")
            if (status == 'verified' and row['effect_key'].startswith('queue:')
                    and json.loads(row['action']).get('queue_guard', {}).get('phase') in {'code', 'add', 'delete'}):
                raise ActionRejected('队列异步效果需要可信回读对账')
            if row["status"] in {"verified", "not_issued"} and row["result"] is not None:
                if row["status"] != status or row["result"] != encoded:
                    raise ValueError("immutable operation receipt collision")
                return self._operation(row)
            self._db.execute("UPDATE workflow_operations SET status=?,result=? WHERE operation_id=?", (status, encoded, operation_id))
            state = self._state(row["task_id"])
            current_operation = (row["control_seq"] == state["control_seq"]
                                 and row["revision"] == state["revision"]
                                 and row["epoch"] == state["epoch"] == state["task_epoch"])
            current_stop = current_operation and not state["writes_enabled"]
            if row["stop_only"] and status == "verified" and result.get("stopped") is True and current_stop:
                # A fresh idle/stop observation cannot erase an unresolved start.
                slot = self._ensure_slot(row["slot"])
                barrier = self._operation_row(slot["barrier_operation_id"]) if slot["barrier_operation_id"] else None
                queue_effect = barrier is not None and barrier['effect_key'].startswith('queue:')
                battle_effect = (barrier is not None and barrier['starts']
                                 and json.loads(barrier['action']).get('maa_identity') is not None)
                # A bounded battle run must settle through its native running
                # and stable-idle proof, not the older generic Stop assertion.
                if not queue_effect and not battle_effect and (barrier is None or not barrier["starts"] or (
                        result.get("reconciled_operation_id") == barrier["operation_id"] and result.get("evidence_ref"))):
                    self._db.execute("UPDATE workflow_slots SET barrier_operation_id=NULL WHERE slot=?", (row["slot"],))
            elif status == "not_issued" or (status == "verified" and not row["starts"]
                                            and not row["stop_only"] and current_operation):
                self._db.execute("UPDATE workflow_slots SET barrier_operation_id=NULL WHERE slot=? AND barrier_operation_id=?", (row["slot"], operation_id))
            self._publish_event(row["task_id"], "operation.result", {"operation_id": operation_id, "status": status, "result": result},
                                event_key=f"operation:{operation_id}:{status}:{digest}", revision=row["revision"], epoch=row["epoch"])
            return self._operation(self._operation_row(operation_id))

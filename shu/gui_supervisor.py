"""One bound MAA helper, JSON IPC and a stop gate independent of COM waits.

Only this module's child is terminated. The stable desktop slot and its durable
unknown barrier deliberately outlive tasks, target restarts and output folders.
Callbacks run in the parent: issue the durable operation before request(), and
pass enter_submission as admit_callback. A successful RPC is a receipt, not proof
that MAA completed the requested work or stopped.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import multiprocessing
import ntpath
import os
from pathlib import Path
import threading
import time
import uuid
from typing import Any, Callable, Mapping

_MAX_MESSAGE = 128 * 1024
_POLL_SECONDS = .02


def _encode(value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(data) > _MAX_MESSAGE:
        raise ValueError("GUI message exceeds size limit")
    return data


def _receive(connection):
    value = json.loads(connection.recv_bytes(_MAX_MESSAGE).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("GUI message must be an object")
    return value


def _lock_byte(handle, offset, acquire):
    handle.seek(offset)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.lockf(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN, 1, offset)


def _open_slot(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b", buffering=0)
    if path.stat().st_size < 2:
        handle.write(b"00")
    return handle


def _default_lock_path():
    session = os.environ.get("SESSIONNAME", "desktop")
    if os.name == "nt":
        import ctypes
        identifier = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(identifier)):
            session = str(identifier.value)
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    return base / "ShuCompanion" / "control" / f"maa-desktop-{session}.lock"


class _GateRejected(RuntimeError):
    code = "submission_rejected"


class _BoundBackend:
    """Wrap only native identity and final perform guard; COM stays in child."""
    def __init__(self, identity, gate):
        from .maa_uia import NativeUiaBackend
        self.native, self.identity, self.gate = NativeUiaBackend(), identity, gate

    @contextmanager
    def session(self):
        with self.native.session() as native:
            identity, gate = self.identity, self.gate

            class BoundSession:
                def __getattr__(self, name):
                    return getattr(native, name)

                def window(self, hwnd):
                    info = native.window(hwnd)
                    if (info.hwnd != identity["hwnd"] or info.pid != identity["pid"]
                            or info.creation_time != identity["creation_time"]):
                        from .maa_uia import MaaUiaError
                        raise MaaUiaError("Bound MAA identity changed; automatic rebinding is forbidden", "binding_mismatch")
                    return info

                def perform(self, element, action, value, *, guard):
                    def final_guard():
                        guard()
                        gate()  # Native perform calls this after blocking COM queries.
                    return native.perform(element, action, value, guard=final_guard)

            yield BoundSession()


class _MaaHandler:
    def __init__(self, identity):
        from .maa_uia import MaaUia
        self.gate = None
        self.adapter = MaaUia(identity["hwnd"], identity["pid"],
                              backend=_BoundBackend(identity, lambda: self.gate()))
        self.last = None

    def __call__(self, command, enter_submission):
        self.gate = enter_submission
        args = command.get("args", {})
        if command["op"] == "snapshot":
            self.last = self.adapter.snapshot()
            return self.last
        if command.get("stop_only"):
            # After retiring a blocked helper, observation tokens from its
            # adapter cannot be reused. Stop always obtains its own fresh token.
            snapshot = self.adapter.snapshot()
            names = {"停止", "停止任务", "stop", "stop task", "stoptask", "stopbutton"}
            candidates = [item for item in snapshot.get("elements", [])
                          if {str(item.get(key, "")).strip().casefold() for key in ("name", "automation_id")} & names
                          and item.get("enabled") and not item.get("offscreen")
                          and "invoke" in item.get("patterns", [])]
            if args.get("action") != "invoke" or len(candidates) != 1:
                raise _GateRejected("Stop-only request requires an observed MAA stop control")
            args = {"snapshot_id": snapshot["snapshot_id"], "element_id": candidates[0]["id"], "action": "invoke"}
        result = self.adapter.act(**args)
        self.last = result.get("after")
        return result


class _MaaVisualHandler:
    """Screenshot and physical input only; no COM/UIA provider is opened."""
    def __init__(self, identity, directory):
        from .maa_visual_native import MaaVisualNative
        self.adapter = MaaVisualNative(identity, directory)

    def __call__(self, command, enter_submission):
        if command["op"] == "snapshot":
            return self.adapter.snapshot()
        if command.get("stop_only"):
            raise _GateRejected("Visual import never starts MAA and cannot claim an external stop")
        args = command.get("args", {})
        action = args.get("action")
        screenshot_id = args.get("screenshot_id")
        if action == "focus":
            return self.adapter.focus(guard=enter_submission)
        if action == "click":
            button = args.get('button', 'left')
            target = args.get('target')
            if button == 'right' and target == 'queue_add':
                return self.adapter.click(args.get('x'), args.get('y'), screenshot_id,
                    button=button, target=target, guard=enter_submission)
            if button != 'left' or target in ('start', 'stop', 'queue_clear', 'queue_clear_unchecked'):
                raise _GateRejected('Unregistered visual click target or button')
            return self.adapter.click(args.get("x"), args.get("y"), screenshot_id, guard=enter_submission)
        if action == 'scroll_queue':
            notches = args.get('notches')
            if args.get('target') != 'queue_list' or type(notches) is not int or not 1 <= abs(notches) <= 3:
                raise _GateRejected('Unregistered queue scroll target or amount')
            return self.adapter.scroll_queue(args.get('x'), args.get('y'), notches, screenshot_id,
                target='queue_list', guard=enter_submission)
        if action == "replace_code":
            return self.adapter.replace_code(args.get("code"), screenshot_id, guard=enter_submission)
        if action == "clear_code":
            return self.adapter.clear_code(screenshot_id, guard=enter_submission)
        raise _GateRejected("Unregistered visual input action")


def _helper_main(connection, identity_json, lock_path, test_handler, backend="uia", directory=None):
    """Spawn bootstrap only; all runtime messages use send_bytes/recv_bytes."""
    slot = None
    try:
        slot = _open_slot(Path(lock_path))
        # A separate child-owned byte prevents handover if the parent dies while
        # COM is still running. A new owner must successfully lock both bytes.
        _lock_byte(slot, 1, True)
        if test_handler is not None:
            handler = test_handler
        elif backend == 'battle':
            from .maa_battle_native import MaaBattleHandler
            handler = MaaBattleHandler(json.loads(identity_json),directory)
        else:
            handler = (_MaaVisualHandler(json.loads(identity_json), directory)
                       if backend == "visual" else _MaaHandler(json.loads(identity_json)))
        connection.send_bytes(_encode({"type": "ready"}))
        while True:
            frame = _receive(connection)
            if frame.get("type") == "close":
                return
            request_id = frame["id"]

            def enter_submission():
                connection.send_bytes(_encode({"type": "enter_submission", "id": request_id}))
                reply = _receive(connection)
                if reply.get("id") != request_id or not reply.get("admitted"):
                    raise _GateRejected("Submission was revoked")

            try:
                result = handler(frame["command"], enter_submission)
                response = {"type": "result", "id": request_id, "ok": True, "result": result}
                encoded = _encode(response)
            except Exception as error:
                encoded = _encode({"type": "result", "id": request_id, "ok": False,
                                   "code": str(getattr(error, "code", "helper_error")),
                                   "error": str(error)[:1000]})
            connection.send_bytes(encoded)
    except (EOFError, OSError):
        pass
    finally:
        connection.close()
        if slot is not None:
            slot.close()


@dataclass
class _Request:
    command: dict
    deadline: float
    cancel_event: Any = None
    admit_callback: Callable | None = None
    stop_only: bool = False
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None
    entered: bool = False
    revoked: bool = False
    drag_cleanup_armed: bool = False


class GuiSupervisor:
    """Blocking caller API; a daemon supervisor thread never runs COM itself.

    test_handler(command, enter_submission) is an importable, spawn-safe callable
    replacing only the child GUI adapter. It must call enter_submission before
    any simulated input. Production exposes only snapshot and bound UIA act.
    """
    def __init__(self, target_identity: Mapping, *, lock_path=None, test_handler=None,
                 backend="uia", directory=None, navigation_identity=None,navigation_mode='background',
                 drag_button=None):
        if backend not in {"uia", "visual", "battle"}:
            raise ValueError("Unsupported MAA GUI backend")
        if backend in {"visual", "battle"} and directory is None:
            raise ValueError("Visual helper requires its evidence directory")
        if navigation_mode not in {'background','foreground'}:
            raise ValueError('Unsupported game navigation transport')
        if navigation_mode!='background' and (backend!='battle' or navigation_identity is None):
            raise ValueError('Foreground game transport requires a bound battle game')
        self.backend = backend
        self.navigation_mode=navigation_mode
        self._drag_button=drag_button
        self._pointer_cleanup_pending=False
        self._pointer_cleanup_owned=False
        self._pointer_cleanup_request_id=None
        self._pointer_cleanup_receipt=None
        self.directory = str(Path(directory).resolve()) if directory is not None else None
        identity = dict(target_identity)
        for key in ("hwnd", "pid", "creation_time"):
            if type(identity.get(key)) is not int or identity[key] <= 0:
                raise ValueError(f"Bound target requires positive {key}")
        from .maa_uia import MAA_EXECUTABLE
        if "executable" in identity and ntpath.normcase(ntpath.normpath(identity["executable"])) != ntpath.normcase(MAA_EXECUTABLE):
            raise ValueError("Only the fixed MAA executable may be bound")
        if set(identity)&{'_game','_navigation_mode'}:
            raise ValueError('Pass game transport explicitly, never in the MAA target')
        self.target_identity = json.loads(_encode(identity))
        if navigation_identity is not None:
            if backend != 'battle':
                raise ValueError('Game navigation identity requires the battle backend')
            from .maa_battle_native import validate_game_identity
            identity['_game'] = validate_game_identity(navigation_identity)
        if backend=='battle':identity['_navigation_mode']=navigation_mode
        self._identity_json = _encode(identity).decode("utf-8")
        self.lock_path = Path(lock_path) if lock_path is not None else _default_lock_path()
        self._state_path = self.lock_path.with_suffix(self.lock_path.suffix + ".state.json")
        self._handler = test_handler
        self._gate = threading.RLock()
        self._condition = threading.Condition(self._gate)
        self._queue = deque()
        self._stop_queue = deque()
        self._slot = self._child = self._connection = self._current = None
        self._closed = False
        self._unknown = self._revoked = False
        self._reason = ""
        self._exit_confirmed = True
        self._retiring = False
        self._timestamps = {}
        self._read_state()
        self._thread = threading.Thread(target=self._run, name="shu-gui-supervisor", daemon=True)
        self._thread.start()

    def _read_state(self):
        if not self._state_path.exists():
            return
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            # This exact failure occurs inside _start_child before it can
            # receive any act request or cross the parent's submission gate.
            # Migrate its old overly conservative flag; retain revocation until
            # the caller supplies a fresh idle readback and current admission.
            before_admission = state.get('reason') == 'helper_exited_during_start'
            self._unknown = self._unknown or bool(state["unknown"]) and not before_admission
            self._revoked = self._revoked or bool(state["revoked"])
            self._reason = state.get("reason", "")
            if state.get('pointer_cleanup_pending'):
                self._pointer_cleanup_pending=True
                self._pointer_cleanup_request_id=state.get('pointer_cleanup_request_id')
                self._unknown=self._revoked=True
        except (OSError, ValueError, KeyError, TypeError):
            self._unknown = self._revoked = True
            self._reason = "control_state_unreadable"

    def _persist(self, *, inflight=None):
        if self._slot is None or self._slot.closed:
            raise RuntimeError('desktop_control_slot_not_owned')
        if inflight is None:
            inflight = self._current is not None and self._current.entered
        value={"unknown": self._unknown or inflight or self._pointer_cleanup_pending,
               "revoked": self._revoked or self._pointer_cleanup_pending,
                              "reason": "submission_unconfirmed" if inflight else self._reason,
                              "updated_at": time.time()}
        if self._pointer_cleanup_pending:
            value.update(pointer_cleanup_pending=True,pointer_cleanup_kind='foreground_game_drag',
                         pointer_cleanup_request_id=self._pointer_cleanup_request_id)
        payload = json.dumps(value, allow_nan=False)
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        # The desktop byte-0 lock serializes every writer, including older
        # versions. Keep their one shared barrier path: some Windows directories
        # allow durable file writes but reject atomic replacement. Never unlink
        # this file; a crash leaving empty/partial JSON is fail-closed in
        # _read_state. Admission waits for the complete write and fsync below.
        with self._state_path.open("w", encoding="utf-8") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())

    @staticmethod
    def _command(command):
        command = json.loads(_encode(command))
        if not isinstance(command, dict) or command.get("op") not in {"snapshot", "act"}:
            raise ValueError("Only snapshot and act commands are supported")
        if not isinstance(command.get("args", {}), dict):
            raise ValueError("GUI args must be an object")
        if set(command) - {"op", "args"}:
            raise ValueError("Unexpected GUI command fields")
        return command

    @staticmethod
    def _answer(request, status, **extra):
        return {"status": status, "submission_entered": request.entered, **extra}

    def request(self, command, timeout=None, cancel_event=None, admit_callback=None):
        command = self._command(command)
        timeout = (3.0 if command["op"] == "snapshot" else 5.0) if timeout is None else float(timeout)
        if not 0 < timeout <= 300:
            raise ValueError("GUI deadline must be between zero and 300 seconds")
        request = _Request(command, time.monotonic() + timeout, cancel_event, admit_callback)
        with self._condition:
            if self._closed or (command["op"] == "act" and (self._revoked or self._unknown)):
                return self._answer(request, "rejected", error="ordinary_submissions_closed")
            if cancel_event is not None and cancel_event.is_set():
                return self._answer(request, "not_issued", error="cancelled")
            self._queue.append(request)
            self._condition.notify()
        request.done.wait()  # Worker owns the deadline and confirms helper exit.
        return request.result

    def request_stop(self, command=None, *, revoke_callback=None, timeout=5.0, admit_callback=None):
        command = self._command(command) if command is not None else None
        if command is not None and command["op"] != "act":
            raise ValueError("Stop command must be a stop-control act")
        with self._condition:
            self._timestamps["stop_received"] = time.time()
            self._revoked = True
            if self._current is not None and not self._current.stop_only:
                self._current.revoked = True
            while self._queue:
                queued = self._queue.popleft()
                queued.result = self._answer(queued, "not_issued", error="stopped_before_submission")
                queued.done.set()
            try:
                if revoke_callback is not None:
                    revoke_callback()  # Same short gate as enter_submission.
                # A non-owner may revoke its own/store authorization, but must
                # never overwrite another owner's durable in-flight barrier.
                self._own_slot()
                self._persist()
            except Exception as error:
                self._unknown = True
                self._reason = "revoke_persistence_failed"
                return {"status": "unknown", "external_stop_confirmed": False, "error": str(error)}
            self._timestamps["authorization_revoked"] = time.time()
            if command is None:
                self._condition.notify()
                return {"status": "stopping", "external_stop_confirmed": False}
            command["stop_only"] = True
            request = _Request(command, time.monotonic() + float(timeout),
                               admit_callback=admit_callback, stop_only=True)
            self._stop_queue.append(request)
            self._condition.notify()
        request.done.wait()
        result = dict(request.result)
        if result["status"] == "completed":
            result["status"] = "stopping"
        result["external_stop_confirmed"] = False
        return result

    def health(self):
        with self._gate:
            child = self._child
            return {"unknown": self._unknown, "revoked": self._revoked, "reason": self._reason,
                    "child_pid": child.pid if child is not None else None,
                    "child_alive": bool(child is not None and child.is_alive()),
                    "helper_exit_confirmed": self._exit_confirmed,
                    "pending": len(self._queue), "stop_pending": len(self._stop_queue),
                    'pointer_cleanup_pending':self._pointer_cleanup_pending,
                    'pointer_cleanup_receipt':self._pointer_cleanup_receipt,
                    "timestamps": dict(self._timestamps), "closed": self._closed}

    def reconcile(self, evidence, *, resume=False):
        """Trusted caller supplies persisted readback; model output is insufficient."""
        if not isinstance(evidence, dict) or evidence.get("reconciled") is not True or not evidence.get("evidence_ref"):
            raise ValueError("Explicit reconciliation evidence is required")
        _encode(evidence)
        with self._gate:
            if self._pointer_cleanup_pending:raise RuntimeError('pointer_release_unconfirmed')
            if self._current is not None:
                raise RuntimeError("Cannot reconcile while a GUI request is active")
            self._own_slot()
            self._unknown = False
            self._reason = ""
            if resume:
                self._revoked = False
            self._persist()
            return {"status": "reconciled", "revoked": self._revoked, "evidence": evidence}

    def _own_slot(self):
        if self._slot is not None:
            return
        handle = _open_slot(self.lock_path)
        try:
            _lock_byte(handle, 0, True)
            _lock_byte(handle, 1, True)
            _lock_byte(handle, 1, False)
        except OSError:
            handle.close()
            raise RuntimeError("desktop_control_slot_busy") from None
        self._slot = handle
        self._read_state()  # A previous owner may have changed it since __init__.

    def _start_child(self, request):
        with self._gate:
            self._own_slot()
            if self._retiring and not self._exit_confirmed:
                raise RuntimeError("helper_exit_unconfirmed")
            if self._child is not None:
                if self._child.is_alive():
                    return
                if not self._retire_child():
                    raise RuntimeError("helper_exit_unconfirmed")
            if not self._exit_confirmed:
                raise RuntimeError("helper_exit_unconfirmed")
            parent, child = multiprocessing.get_context("spawn").Pipe(duplex=True)
            process = multiprocessing.get_context("spawn").Process(
                target=_helper_main, args=(child, self._identity_json,
                                          str(self.lock_path), self._handler, self.backend, self.directory),
                name="shu-maa-gui-helper", daemon=True)
            process.start()
            self._allow_child_foreground(process.pid)
            child.close()
            self._child, self._connection = process, parent
            self._exit_confirmed = False
        while time.monotonic() < request.deadline:
            if self._connection.poll(_POLL_SECONDS):
                if _receive(self._connection).get("type") != "ready":
                    raise RuntimeError("invalid_helper_ready")
                return
            if request.revoked or self._closed or (request.cancel_event is not None and request.cancel_event.is_set()):
                raise _GateRejected("cancelled_during_helper_start")
            if not process.is_alive():
                raise RuntimeError("helper_exited_during_start")
        raise TimeoutError("helper_start_timeout")

    @staticmethod
    def _allow_child_foreground(pid):
        """Delegate only to our helper when the parent is eligible on Windows."""
        if os.name != 'nt' or type(pid) is not int or pid<=0:
            return False
        import ctypes
        from ctypes import wintypes
        function=ctypes.WinDLL('user32',use_last_error=True).AllowSetForegroundWindow
        function.argtypes=[wintypes.DWORD];function.restype=wintypes.BOOL
        return bool(function(pid))

    def _retire_child(self):
        process = self._child
        if process is None:
            return self._exit_confirmed
        self._retiring = True
        try:
            if process.is_alive():
                process.terminate()
            process.join(.5)
            if process.is_alive():
                process.kill()
                process.join(.5)
        except (OSError, ValueError):
            self._exit_confirmed = False
            return False
        if process.is_alive():
            self._exit_confirmed = False
            return False
        with self._gate:
            connection = self._connection
            self._child = self._connection = None
            self._exit_confirmed = True
            self._retiring = False
            self._timestamps["helper_exit_confirmed"] = time.time()
        if connection is not None:
            connection.close()
        process.close()
        return True

    def _foreground_drag(self,request):
        args=request.command.get('args',{})
        return (self.backend=='battle' and self.navigation_mode=='foreground' and not request.stop_only
                and request.command.get('op')=='act' and args.get('surface')=='game'
                and args.get('action')=='drag')

    def _button(self):
        if self._drag_button is None:
            from .drag_button_cleanup import ReleaseOnlyLeftButton
            self._drag_button=ReleaseOnlyLeftButton()
        return self._drag_button

    def _arm_drag_cleanup(self,request):
        # In the production foreground handler the first gate precedes any
        # pointer movement/down. Refuse an already-held user button. Persist
        # this narrowly scoped intent before acknowledging child admission.
        if self._button().is_down() is not False:raise _GateRejected('left_button_already_down')
        request.drag_cleanup_armed=True
        self._pointer_cleanup_pending=self._pointer_cleanup_owned=True
        self._pointer_cleanup_request_id=request.request_id

    def _cooperative_drag_cancel(self,request):
        """Deny the next native guard so a live drag can run its mouse-up finally."""
        if not request.entered or not request.drag_cleanup_armed:return False
        until=time.monotonic()+.35
        try:
            while self._connection is not None and time.monotonic()<until:
                if not self._connection.poll(min(.02,max(0,until-time.monotonic()))):
                    if self._child is None or not self._child.is_alive():return False
                    continue
                message=_receive(self._connection)
                if message.get('id')!=request.request_id:return False
                if message.get('type')=='enter_submission':
                    self._connection.send_bytes(_encode({'id':request.request_id,'admitted':False}))
                elif message.get('type')=='result':return True
                else:return False
        except (OSError,EOFError,ValueError,RuntimeError):pass
        return False

    def _finish_drag_cleanup(self):
        """The original owner may send LEFTUP only after its helper has exited."""
        if not self._pointer_cleanup_pending:return True
        if not self._pointer_cleanup_owned or not self._exit_confirmed:return False
        receipt={'release_only':True,'button_released':False,'input_sent':False,
                 'helper_exit_confirmed':True,'request_id':self._pointer_cleanup_request_id}
        try:
            if self._button().is_down() is False:
                receipt.update(button_released=True,status='already_up')
            else:
                result=self._button().release_after_exit()
                if (not isinstance(result,dict) or result.get('release_only') is not True
                        or result.get('button_released') is not True or self._button().is_down() is not False):
                    raise RuntimeError('pointer_release_unconfirmed')
                receipt.update(button_released=True,input_sent=result.get('input_sent') is True,status='released')
        except Exception as error:
            receipt.update(status='unverified',error=type(error).__name__)
        receipt['observed_at']=time.time()
        with self._gate:
            self._pointer_cleanup_receipt=receipt
            if receipt['button_released']:
                self._pointer_cleanup_pending=self._pointer_cleanup_owned=False
            else:
                self._unknown=self._revoked=True;self._reason='pointer_release_unconfirmed'
            try:self._persist(inflight=False)
            except (OSError,RuntimeError):
                # The button may already be up, but the durable pending bit
                # has not been cleared. Keep this owner's narrow retry right.
                self._pointer_cleanup_pending=self._pointer_cleanup_owned=True
                self._unknown=self._revoked=True;self._reason='pointer_release_unconfirmed'
        return not self._pointer_cleanup_pending

    def _unknown_result(self, request, reason):
        if reason=='helper_exited_during_start' and not request.entered and self._retire_child():
            with self._gate:
                self._revoked=True
                self._reason=reason
                try:self._persist(inflight=False)
                except (OSError,RuntimeError):self._reason='control_state_unwritable'
            return self._answer(request,'not_issued',error=reason,helper_exit_confirmed=True)
        with self._gate:
            self._unknown = self._revoked = True
            self._reason = reason
            try:
                self._persist()
            except (OSError, RuntimeError):
                self._reason = "control_state_unwritable"
        # Never hold admission gate while waiting for a process to exit.
        cooperative=False
        if reason in {'cancelled_after_submission','helper_unresponsive'}:
            cooperative=self._cooperative_drag_cancel(request)
        confirmed = self._retire_child()
        if confirmed and request.drag_cleanup_armed:self._finish_drag_cleanup()
        return self._answer(request, "unknown", error=reason, helper_exit_confirmed=confirmed,
                            cooperative_cancelled=cooperative,
                            pointer_cleanup=self._pointer_cleanup_receipt if request.drag_cleanup_armed else None)

    def _execute(self, request):
        try:
            if time.monotonic() >= request.deadline:
                return self._answer(request, "not_issued", error="deadline_before_dispatch")
            self._start_child(request)
            with self._gate:
                if not request.stop_only and (request.revoked or (request.command["op"] == "act" and (self._unknown or self._revoked))):
                    return self._answer(request, "not_issued", error="ordinary_submissions_closed")
            if request.command.get('args',{}).get('action') == 'focus':
                self._allow_child_foreground(self._child.pid)
            self._connection.send_bytes(_encode({"type": "request", "id": request.request_id, "command": request.command}))
            while True:
                if time.monotonic() >= request.deadline:
                    return self._unknown_result(request, "helper_unresponsive")
                cancelled = (self._closed or request.revoked or
                             (request.cancel_event is not None and request.cancel_event.is_set()))
                if cancelled:
                    if request.entered:
                        return self._unknown_result(request, "cancelled_after_submission")
                    self._retire_child()
                    return self._answer(request, "not_issued", error="cancelled_before_submission")
                if not self._connection.poll(_POLL_SECONDS):
                    if not self._child.is_alive():
                        return self._unknown_result(request, "helper_exited_without_receipt")
                    continue
                message = _receive(self._connection)
                if message.get("id") != request.request_id:
                    return self._unknown_result(request, "invalid_helper_response")
                if message.get("type") == "enter_submission":
                    with self._gate:
                        admitted = not self._closed and not request.revoked
                        admitted = admitted and (request.cancel_event is None or not request.cancel_event.is_set())
                        admitted = admitted and (request.stop_only or not (self._revoked or self._unknown))
                        try:
                            if admitted and not request.entered:
                                if self._foreground_drag(request):self._arm_drag_cleanup(request)
                                if request.admit_callback is not None and request.admit_callback() is False:
                                    admitted = False
                                if admitted:
                                    self._persist(inflight=True)
                                    request.entered = True
                                    self._timestamps["submission_entered"] = time.time()
                        except Exception:
                            admitted = False
                        if not admitted and not request.entered and request.drag_cleanup_armed:
                            request.drag_cleanup_armed=False
                            self._pointer_cleanup_pending=self._pointer_cleanup_owned=False
                        self._connection.send_bytes(_encode({"id": request.request_id, "admitted": admitted}))
                    continue
                if message.get("type") != "result":
                    return self._unknown_result(request, "invalid_helper_response")
                if not message.get("ok"):
                    if message.get("code") == "uia_timeout" or request.entered:
                        return self._unknown_result(request, message.get("code", "helper_error"))
                    return self._answer(request, "not_issued", error=message.get("code", "helper_error"))
                if request.command["op"] == "act" and not request.entered:
                    return self._unknown_result(request, "helper_bypassed_submission_gate")
                if request.drag_cleanup_armed:
                    try:released=self._button().is_down() is False
                    except Exception:released=False
                    if not released:return self._unknown_result(request,'drag_release_unconfirmed')
                    self._pointer_cleanup_pending=self._pointer_cleanup_owned=False
                if request.command['op']=='act':
                    with self._gate:
                        try:self._persist(inflight=False)  # Only an input receipt clears its transient barrier.
                        except (OSError,RuntimeError):
                            if request.drag_cleanup_armed:
                                self._pointer_cleanup_pending=self._pointer_cleanup_owned=True
                            raise
                return self._answer(request, "completed", result=message.get("result"))
        except _GateRejected as error:
            self._retire_child()
            return self._answer(request, "not_issued", error=str(error))
        except TimeoutError as error:
            return self._unknown_result(request, str(error))
        except RuntimeError as error:
            if str(error) == "desktop_control_slot_busy":
                return self._answer(request, "rejected", error=str(error))
            return self._unknown_result(request, str(error))
        except Exception as error:
            return self._unknown_result(request, type(error).__name__)

    def _run(self):
        while True:
            with self._condition:
                while not self._closed and not self._stop_queue and not self._queue:
                    self._condition.wait()
                if self._closed:
                    break
                request = self._stop_queue.popleft() if self._stop_queue else self._queue.popleft()
                self._current = request
            result = self._execute(request)
            with self._condition:
                request.result = result
                self._current = None
                request.done.set()
        confirmed = self._retire_child()
        if confirmed and self._pointer_cleanup_pending:self._finish_drag_cleanup()
        if confirmed and not self._pointer_cleanup_pending and self._slot is not None:
            self._slot.close()
            self._slot = None

    def close(self):
        with self._condition:
            if not self._closed:
                self._closed = True
                for request in (*self._queue, *self._stop_queue):
                    request.result = self._answer(request, "not_issued", error="supervisor_closed")
                    request.done.set()
                self._queue.clear()
                self._stop_queue.clear()
                self._condition.notify()
        self._thread.join(2)
        if self._thread.is_alive():
            raise RuntimeError('helper_exit_unconfirmed')
        # A prior termination denial can be retried by the same owner after
        # the dispatcher exits; never equate a joined Python thread with an
        # exited GUI child or release its control slot prematurely.
        if not self._exit_confirmed and not self._retire_child():
            raise RuntimeError('helper_exit_unconfirmed')
        if self._pointer_cleanup_pending:
            self._finish_drag_cleanup()
            if self._pointer_cleanup_pending:raise RuntimeError('pointer_release_unconfirmed')
        if self._slot is not None:
            self._slot.close()
            self._slot = None

"""Bound MAA GUI accessibility adapter; no game/API, keyboard or mouse access.

Import and construction are inert. COM is created only inside operation-local MTA
threads. ``UiaBackend``/``UiaSession`` are public injection seams for fake tests.
UIA pattern success means dispatch, not completion of the work shown by MAA.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import ctypes
import gc
import importlib
import ntpath
import queue
import sys
import threading
import time
import traceback
from typing import Any, Callable, ContextManager, Iterable, Protocol
import uuid

MAA_EXECUTABLE = r"D:\MAA\MAA-v6.21.4-win-x64\MAA.exe"
MAX_ELEMENTS = 250
MAX_DEPTH = 12
SNAPSHOT_SECONDS = 2.0
MAX_SNAPSHOT_AGE = 10.0
_ACTIONS = frozenset({"invoke", "toggle", "set_value", "select"})
_IMPORT_LOCK = threading.Lock()


class MaaUiaError(RuntimeError):
    def __init__(self, message: str, code: str = "uia_error"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WindowIdentity:
    hwnd: int
    pid: int
    executable: str
    title: str = ""
    owner: int = 0
    creation_time: int = 0


class UiaSession(Protocol):
    """Handles remain inside a session and must never cross COM threads.

    ``read`` returns primitive data: runtime_id, pid, name, automation_id,
    control_type, enabled, offscreen, rect, patterns, password, read_only,
    value, toggle and selected. Password values MUST NOT be queried.
    ``roots`` includes only the bound HWND and same-PID owned windows/dialogs.
    """
    def window(self, hwnd: int) -> WindowIdentity: ...
    def roots(self, hwnd: int, pid: int) -> Iterable[Any]: ...
    def children(self, element: Any) -> Iterable[Any]: ...
    def read(self, element: Any) -> dict[str, Any]: ...
    def perform(self, element: Any, action: str, value: str | None, *, guard: Callable[[], None]) -> None: ...


class UiaBackend(Protocol):
    def session(self) -> ContextManager[UiaSession]: ...


def _path(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value))


def _public(raw: dict[str, Any]) -> dict[str, Any]:
    runtime_id = tuple(int(n) for n in raw["runtime_id"])
    if not runtime_id:
        raise MaaUiaError("控件缺少稳定运行时标识。", "element_unavailable")
    password = bool(raw.get("password"))
    patterns = sorted(set(raw.get("patterns", ())) & _ACTIONS)
    if password:
        patterns = []  # Password controls are observable but never actionable.
    return {"id": "uia:" + ":".join(map(str, runtime_id)),
            "name": str(raw.get("name", "")),
            "automation_id": str(raw.get("automation_id", "")),
            "control_type": str(raw.get("control_type", "")),
            "enabled": bool(raw.get("enabled")),
            "offscreen": bool(raw.get("offscreen", True)),
            "rect": dict(raw["rect"]), "patterns": patterns,
            "password": password, "read_only": bool(raw.get("read_only", False)),
            "value": None if password else raw.get("value"),
            "toggle": None if password else raw.get("toggle"),
            "selected": None if password else raw.get("selected")}


class MaaUia:
    def __init__(self, hwnd: int, pid: int, *, backend: UiaBackend | None = None,
                 monotonic=time.monotonic, clock=time.time):
        if isinstance(hwnd, bool) or isinstance(pid, bool) or not isinstance(hwnd, int) or not isinstance(pid, int) or min(hwnd, pid) <= 0:
            raise ValueError("hwnd and pid must be positive integers")
        self.hwnd, self.pid = hwnd, pid
        self._backend = backend if backend is not None else NativeUiaBackend()
        self._monotonic, self._clock = monotonic, clock
        self._lock = threading.Lock()
        self._last: dict[str, Any] | None = None
        self._creation_time: int | None = None

    def _validate(self, session: UiaSession) -> WindowIdentity:
        info = session.window(self.hwnd)
        if info.hwnd != self.hwnd or info.pid != self.pid or _path(info.executable) != _path(MAA_EXECUTABLE):
            raise MaaUiaError("绑定窗口或进程身份不匹配。", "binding_mismatch")
        if self._creation_time is not None and info.creation_time != self._creation_time:
            raise MaaUiaError("绑定进程已重新启动，请重新绑定。", "binding_mismatch")
        return info

    def _run(self, operation, seconds: float):
        """Bound caller waiting even if an external accessibility provider hangs.

        A timed out read never becomes evidence. A timed out action consumes its
        token and reports unknown outcome; it must not be automatically retried.
        The daemon checks cancellation again immediately before dispatch.
        """
        output: queue.Queue = queue.Queue(maxsize=1)
        cancelled = threading.Event()

        def work():
            try:
                with self._backend.session() as session:
                    result = operation(session, cancelled)
                output.put((True, result))
            except Exception as exc:
                # Do not move exception tracebacks containing COM handles between threads.
                code = getattr(exc, "code", "uia_unavailable")
                if getattr(exc, 'hresult', None) in (-2146233083, 0x80131505):
                    code = 'uia_provider_timeout'
                failure = MaaUiaError(str(exc), code)
                frames = traceback.extract_tb(exc.__traceback__)
                failure.stage = next((frame.name for frame in reversed(frames)
                                      if frame.filename == __file__), 'provider')
                output.put((False, failure))
            finally:
                gc.collect()

        threading.Thread(target=work, name="shu-maa-uia", daemon=True).start()
        try:
            ok, result = output.get(timeout=seconds)
        except queue.Empty:
            cancelled.set()
            raise MaaUiaError("UIA 超时；动作如已派发，需重新观察确认结果。", "uia_timeout") from None
        if not ok:
            raise result
        return result

    def _walk(self, session: UiaSession, deadline: float, cancelled: threading.Event, stats=None):
        """Lazy depth-first walk: no unbounded FindAll, desktop root or sibling list."""
        stats = stats if stats is not None else {}
        stack = [(iter(session.roots(self.hwnd, self.pid)), 0)]
        seen: set[str] = set()
        examined = 0
        while stack and examined < MAX_ELEMENTS:
            if cancelled.is_set() or self._monotonic() >= deadline:
                stats["truncated"] = True
                return
            children, depth = stack[-1]
            try:
                element = next(children)
            except StopIteration:
                stack.pop()
                continue
            examined += 1
            stats["examined"] = examined
            if cancelled.is_set() or self._monotonic() >= deadline:
                stats["truncated"] = True
                return
            try:
                raw = session.read(element)
                if raw["pid"] != self.pid:
                    continue  # Never traverse an embedded foreign process.
                public = _public(raw)
            except MaaUiaError:
                raise
            except Exception:
                stats['truncated'] = True
                continue  # A disappearing element is not evidence or an action target.
            if public["id"] in seen:
                continue
            seen.add(public["id"])
            yield element, public
            if depth < MAX_DEPTH:
                stack.append((iter(session.children(element)), depth + 1))
            else:
                stats["truncated"] = True
        if stack:
            stats["truncated"] = True

    def _read_snapshot(self, session, started, cancelled):
        info = self._validate(session)
        stats = {}
        deadline = started + SNAPSHOT_SECONDS - 0.15
        elements = [data for _, data in self._walk(session, deadline, cancelled, stats)]
        self._validate(session)
        supported = len(elements) > 1 or any(e["patterns"] for e in elements)
        result = {"window": {"hwnd": info.hwnd, "pid": info.pid, "executable": info.executable,
                             "title": info.title, "owner": info.owner},
                  "elements": elements, "timestamp": self._clock(), "snapshot_id": uuid.uuid4().hex,
                  "snapshot_status": "ok" if supported else "unsupported",
                  "limits": {"max_elements": MAX_ELEMENTS, "max_depth": MAX_DEPTH,
                             "max_seconds": SNAPSHOT_SECONDS},
                  "examined": stats.get("examined", 0),
                  "possibly_truncated": bool(stats.get("truncated"))}
        if not supported:
            result["reason"] = "UIA 未暴露绑定窗口中的可用控件；不会回退到桌面输入。"
        return info, result

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._last = None
            started = self._monotonic()
            def collect(session, cancelled):
                return self._read_snapshot(session, started, cancelled)

            info, result = self._run(collect, SNAPSHOT_SECONDS)
            self._creation_time = info.creation_time
            self._last = {"snapshot": deepcopy(result), "started": started}
            return result

    def act(self, snapshot_id: str, element_id: str, action: str, value=None,
            cancel_event: threading.Event | None = None) -> dict[str, Any]:
        with self._lock:
            if action not in _ACTIONS:
                raise MaaUiaError("不支持的 UIA 动作。", "action_not_allowed")
            if (action == "set_value" and (not isinstance(value, str) or len(value) > 4096)) or (action != "set_value" and value is not None):
                raise MaaUiaError("动作参数无效。", "invalid_value")
            previous = self._last
            if previous is None or previous["snapshot"]["snapshot_id"] != snapshot_id:
                raise MaaUiaError("需要最近一次 UIA 观察。", "snapshot_mismatch")
            age = self._monotonic() - previous["started"]
            if age < 0 or age >= MAX_SNAPSHOT_AGE:
                self._last = None
                raise MaaUiaError("UIA 观察已过期，请重新观察。", "snapshot_stale")
            expected = next((e for e in previous["snapshot"]["elements"] if e["id"] == element_id), None)
            if expected is None:
                raise MaaUiaError("控件不在最近一次观察中。", "element_unknown")
            self._last = None  # One attempt per observation, including failed/unknown outcomes.
            deadline = self._monotonic() + SNAPSHOT_SECONDS - 0.15

            def apply(session, cancelled):
                def guard():
                    if cancelled.is_set() or (cancel_event is not None and cancel_event.is_set()):
                        raise MaaUiaError("UIA 操作已取消。", "cancelled")
                    if self._monotonic() >= deadline or self._monotonic() - previous["started"] >= MAX_SNAPSHOT_AGE:
                        raise MaaUiaError("操作前观察已过期，请重新观察。", "snapshot_stale")

                guard()
                self._validate(session)
                target = None
                for element, current in self._walk(session, deadline, cancelled):
                    if current["id"] == element_id:
                        target = element
                        break
                if target is None:
                    raise MaaUiaError("控件已消失或遍历超时，请重新观察。", "element_unavailable")
                # A final read after reacquisition closes the traversal/dispatch gap.
                raw = session.read(target)
                if raw["pid"] != self.pid or _public(raw) != expected:
                    raise MaaUiaError("控件位置、身份或属性已改变，请重新观察。", "element_changed")
                if expected["password"] or not expected["enabled"] or expected["offscreen"]:
                    raise MaaUiaError("控件不可操作。", "element_not_actionable")
                if action not in expected["patterns"] or (action == "set_value" and expected["read_only"]):
                    raise MaaUiaError("控件不支持该操作。", "pattern_not_supported")
                self._validate(session)
                guard()
                session.perform(target, action, value, guard=guard)
                after_started = self._monotonic()
                try:
                    _, after = self._read_snapshot(session, after_started, cancelled)
                except Exception:
                    after = None
                return {"ok": True, "result": "dispatched", "action": action,
                        "element_id": element_id, "snapshot_id": snapshot_id,
                        "after": after, "timestamp": self._clock(), "verification_required": True}, after_started

            result, after_started = self._run(apply, SNAPSHOT_SECONDS * 2 + 1.0)
            if result["after"] is not None:
                self._last = {"snapshot": deepcopy(result["after"]), "started": after_started}
            return result


class NativeUiaBackend:
    """Windows UIAutomationCore + limited Win32 window/process identity queries."""
    @contextmanager
    def session(self):
        if sys.platform != "win32":
            raise MaaUiaError("Windows UIA 仅支持 Windows。", "uia_unavailable")
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        ole32.CoInitializeEx.restype = ctypes.c_long
        hr = ole32.CoInitializeEx(None, 0)  # MTA, this operation's worker only.
        if hr < 0:
            raise MaaUiaError("无法初始化 UIA COM 线程。", "uia_unavailable")
        native = None
        try:
            with _IMPORT_LOCK:
                first_import = "comtypes" not in sys.modules
                old_flags = getattr(sys, "coinit_flags", None)
                try:
                    sys.coinit_flags = 0
                    comtypes = importlib.import_module("comtypes")
                    client = importlib.import_module("comtypes.client")
                finally:
                    if old_flags is None:
                        del sys.coinit_flags
                    else:
                        sys.coinit_flags = old_flags
                if first_import:
                    comtypes.CoUninitialize()  # Balance comtypes' implicit import-time init.
                module = client.GetModule("UIAutomationCore.dll")
            automation = client.CreateObject(module.CUIAutomation8, interface=module.IUIAutomation2)
            # Bound the provider wait without rejecting a cold WPF provider at 200ms.
            # The independent snapshot/helper deadlines still bound total work.
            automation.ConnectionTimeout = 1000
            automation.TransactionTimeout = 1000
            native = _NativeSession(automation, module)
            del automation
            yield native
        finally:
            if native is not None:
                native.close()
            gc.collect()
            ole32.CoUninitialize()


class _NativeSession:
    def __init__(self, automation, module):
        from ctypes import wintypes
        self.automation, self.module = automation, module
        # WPF's raw tree includes template/layout nodes. Control view retains
        # semantic text and disabled controls needed for state verification.
        self.walker = automation.ControlViewWalker
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetWindow.restype = wintypes.HWND
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.kernel32.GetProcessTimes.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self._enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.EnumWindows.argtypes = [self._enum_callback, wintypes.LPARAM]
        self.user32.EnumWindows.restype = wintypes.BOOL

    def close(self):
        self.walker = self.automation = self.module = None

    def window(self, hwnd):
        from ctypes import wintypes
        if not self.user32.IsWindow(hwnd):
            raise MaaUiaError("绑定窗口已关闭。", "binding_mismatch")
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = self.kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION only.
        if not handle:
            raise MaaUiaError("无法核验 MAA 进程身份。", "binding_mismatch")
        try:
            size = wintypes.DWORD(32768)
            path = ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                raise MaaUiaError("无法核验 MAA 可执行文件。", "binding_mismatch")
            times = [wintypes.FILETIME() for _ in range(4)]
            if not self.kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                raise MaaUiaError("无法核验 MAA 进程启动时间。", "binding_mismatch")
            creation = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            self.kernel32.CloseHandle(handle)
        title = ctypes.create_unicode_buffer(1024)
        self.user32.GetWindowTextW(hwnd, title, len(title))
        return WindowIdentity(hwnd, pid.value, path.value, title.value,
                              int(self.user32.GetWindow(hwnd, 4) or 0), creation)

    def roots(self, hwnd, pid):
        from ctypes import wintypes
        related = [hwnd]
        bound = self.window(hwnd)
        # Same-PID owner ancestry is the only way to admit other top-level HWNDs.
        ancestors = {hwnd}
        owner = bound.owner
        for _ in range(MAX_DEPTH):
            if not owner or owner in ancestors:
                break
            info = self.window(owner)
            if info.pid != pid or _path(info.executable) != _path(MAA_EXECUTABLE):
                break
            ancestors.add(owner)
            related.append(owner)
            owner = info.owner

        @self._enum_callback
        def consider(candidate, _):
            if len(related) >= MAX_ELEMENTS:
                return False
            candidate = int(candidate)
            other_pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(candidate, ctypes.byref(other_pid))
            if other_pid.value != pid or candidate in related:
                return True
            owner = int(self.user32.GetWindow(candidate, 4) or 0)
            for _ in range(MAX_DEPTH):
                if not owner:
                    break
                if owner in ancestors:
                    related.append(candidate)
                    break
                owner_pid = wintypes.DWORD()
                self.user32.GetWindowThreadProcessId(owner, ctypes.byref(owner_pid))
                if owner_pid.value != pid:
                    break
                owner = int(self.user32.GetWindow(owner, 4) or 0)
            return True

        self.user32.EnumWindows(consider, 0)
        for candidate in related:
            info = self.window(candidate)
            if info.pid != pid or info.creation_time != bound.creation_time or _path(info.executable) != _path(MAA_EXECUTABLE):
                continue
            element = self.automation.ElementFromHandle(candidate)
            if element:
                yield element

    def children(self, element):
        child = self.walker.GetFirstChildElement(element)
        while child:
            yield child
            child = self.walker.GetNextSiblingElement(child)

    def _pattern(self, element, action):
        patterns = {"invoke": (10000, "IUIAutomationInvokePattern"),
                    "toggle": (10015, "IUIAutomationTogglePattern"),
                    "set_value": (10002, "IUIAutomationValuePattern"),
                    "select": (10010, "IUIAutomationSelectionItemPattern")}
        number, interface = patterns[action]
        pattern = element.GetCurrentPattern(number)
        return pattern.QueryInterface(getattr(self.module, interface)) if pattern else None

    def read(self, element):
        # Check password before requesting any pattern or value.
        password = bool(element.CurrentIsPassword)
        rect = element.CurrentBoundingRectangle
        result = {"runtime_id": tuple(element.GetRuntimeId()), "pid": int(element.CurrentProcessId),
                  "name": str(element.CurrentName or ""), "automation_id": str(element.CurrentAutomationId or ""),
                  "control_type": str(element.CurrentControlType), "enabled": bool(element.CurrentIsEnabled),
                  "offscreen": bool(element.CurrentIsOffscreen), "password": password,
                  "rect": {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom,
                           "width": rect.right - rect.left, "height": rect.bottom - rect.top},
                  "patterns": [], "value": None, "toggle": None, "selected": None, "read_only": False}
        if not password:
            for action in sorted(_ACTIONS):
                try:
                    pattern = self._pattern(element, action)
                    if not pattern:
                        continue
                    result["patterns"].append(action)
                    if action == "set_value":
                        result["read_only"] = bool(pattern.CurrentIsReadOnly)
                        result["value"] = str(pattern.CurrentValue or "")
                    elif action == "toggle":
                        result["toggle"] = int(pattern.CurrentToggleState)
                    elif action == "select":
                        result["selected"] = bool(pattern.CurrentIsSelected)
                except Exception:
                    # Pattern unavailability is normal, especially for labels.
                    if action in result["patterns"]:
                        result["patterns"].remove(action)
        return result

    def perform(self, element, action, value, *, guard):
        # Only these four semantic interfaces exist; no keyboard/mouse fallback.
        if bool(element.CurrentIsPassword) or not bool(element.CurrentIsEnabled) or bool(element.CurrentIsOffscreen):
            raise MaaUiaError("控件已不可操作。", "element_not_actionable")
        pattern = self._pattern(element, action)
        if not pattern:
            raise MaaUiaError("控件操作接口已消失。", "pattern_not_supported")
        if action == "set_value" and pattern.CurrentIsReadOnly:
            raise MaaUiaError("控件为只读。", "pattern_not_supported")
        guard()  # STOP/expiry check after all potentially blocking COM property queries.
        if action == "invoke":
            pattern.Invoke()
        elif action == "toggle":
            pattern.Toggle()
        elif action == "select":
            pattern.Select()
        elif action == "set_value":
            pattern.SetValue(value)

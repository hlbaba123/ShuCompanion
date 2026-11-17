"""MAA-only screenshot and physical-input transport, without accessibility APIs.

No OS calls happen at import time. Run this adapter in GuiSupervisor's disposable
helper: PrintWindow is synchronous and only the supervisor can bound a hung
rendering call. Import uses guarded foreground input. Single-battle buttons
can use a separate MaaFramework transport bound to the MAA window.

The caller owns visual interpretation, allowed targets, the durable action ledger
and final verification. An accepted input is *never* reported as a loaded job.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import hashlib
import math
import ntpath
from pathlib import Path
import re
import struct
import sys
import threading
import time
from typing import Any, Mapping
import uuid

from .windows import WindowError, WindowInfo, _NativeWindows, _geometry
from .gui_text_input import _Win32Keyboard


MAA_EXECUTABLE = r"D:\MAA\MAA-v6.21.4-win-x64\MAA.exe"
_CODE = re.compile(r"prts://[1-9][0-9]{0,11}\Z", re.ASCII)
_KEYS = {"CTRL+A": (0x11, 0x41), "BACKSPACE": (0x08,), "ENTER": (0x0D,), "ESC": (0x1B,), "TAB": (0x09,)}
_KEYUP, _UNICODE = 0x0002, 0x0004


class MaaVisualError(WindowError):
    """Short, typed transport failure; partial input is explicit in details."""


def _same_path(a, b):
    return (isinstance(a, str) and isinstance(b, str)
            and ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(ntpath.normpath(b)))


def _positive(value):
    return type(value) is int and value > 0


def inspect_process_instance(process):
    """Query lifetime, never memory. A missing window alone proves nothing.

    Access denied and API errors stay unknown. Creation time prevents a reused
    PID from being mistaken for the original MAA process.
    """
    if (not isinstance(process, dict) or not _positive(process.get('pid'))
            or not _positive(process.get('creation_time'))
            or not _same_path(process.get('executable'), MAA_EXECUTABLE)):
        raise ValueError('invalid MAA process identity')
    result = {key: process[key] for key in ('pid', 'creation_time', 'executable')}
    if sys.platform != 'win32':
        return {**result, 'status': 'unknown', 'reason': 'unsupported_os'}
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
    kernel.WaitForSingleObject.argtypes, kernel.WaitForSingleObject.restype = [w.HANDLE, w.DWORD], w.DWORD
    kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    kernel.GetProcessTimes.restype = w.BOOL
    handle = kernel.OpenProcess(0x1000 | 0x00100000, False, process['pid'])
    if not handle:
        error = ctypes.get_last_error()
        return {**result, 'status': 'exited' if error == 87 else 'unknown',
                'reason': 'pid_absent' if error == 87 else 'process_unreadable', 'winerror': error}
    try:
        times = [w.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return {**result, 'status': 'unknown', 'reason': 'creation_time_unreadable'}
        created = (int(times[0].dwHighDateTime) << 32) | int(times[0].dwLowDateTime)
        if created != process['creation_time']:
            return {**result, 'status': 'exited', 'reason': 'pid_reused', 'observed_creation_time': created}
        wait = kernel.WaitForSingleObject(handle, 0)
        return {**result, 'status': 'exited' if wait == 0 else 'alive' if wait == 258 else 'unknown',
                'reason': 'process_signaled' if wait == 0 else 'original_process_alive' if wait == 258 else 'wait_failed'}
    finally:
        kernel.CloseHandle(handle)


class _NativeMaaBackend:
    """Read-only identity/privilege queries plus existing guarded Win32 input."""

    def __init__(self):
        if sys.platform != "win32":
            raise MaaVisualError("MAA 窗口操作需要 Windows。", "unsupported_os")
        self.native = _NativeWindows()
        self.keyboard = _Win32Keyboard(self.native)
        self.use_wgc = False
        kernel = self.native.kernel
        kernel.GetCurrentProcess.argtypes, kernel.GetCurrentProcess.restype = [], w.HANDLE
        kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
        kernel.GetProcessTimes.restype = w.BOOL
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
        self.advapi.OpenProcessToken.restype = w.BOOL
        self.advapi.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                   w.DWORD, ctypes.POINTER(w.DWORD)]
        self.advapi.GetTokenInformation.restype = w.BOOL
        self.advapi.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        self.advapi.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        self.advapi.GetSidSubAuthority.argtypes = [ctypes.c_void_p, w.DWORD]
        self.advapi.GetSidSubAuthority.restype = ctypes.POINTER(w.DWORD)
        self.native.user.GetDpiForWindow.argtypes = [w.HWND]
        self.native.user.GetDpiForWindow.restype = w.UINT

    def get_window(self, hwnd):
        return self.native.get_window(hwnd)

    def verify_message_target(self, hwnd):
        """Match MaaFramework MessageInput's actual receiver, not just its owner.

        v5.15.0 redirects mouse messages to a visible last-active owned popup.
        A PrintWindow image of the main client cannot authorize that popup.
        """
        user = self.native.user
        for name in ('IsWindow', 'IsWindowEnabled', 'IsWindowVisible'):
            function = getattr(user, name)
            function.argtypes, function.restype = [w.HWND], w.BOOL
        user.GetAncestor.argtypes, user.GetAncestor.restype = [w.HWND, w.UINT], w.HWND
        user.GetLastActivePopup.argtypes, user.GetLastActivePopup.restype = [w.HWND], w.HWND
        if not user.IsWindow(hwnd) or not user.IsWindowEnabled(hwnd):
            raise MaaVisualError('MAA 主窗口当前不可接收输入。', 'message_target_changed')
        root = int(user.GetAncestor(hwnd, 3) or 0)  # GA_ROOTOWNER, as in the SDK.
        if not root:
            raise MaaVisualError('无法核对 MAA 消息接收窗口。', 'message_target_changed')
        popup = int(user.GetLastActivePopup(root) or 0)
        if popup and popup != hwnd and user.IsWindowVisible(popup):
            raise MaaVisualError('MAA 存在活动弹窗，需要重新观察。', 'message_target_changed')

    def list_windows(self):
        return self.native.list_windows()

    def creation_time(self, pid):
        kernel = self.native.kernel
        process = kernel.OpenProcess(0x1000, False, pid)
        if not process:
            raise MaaVisualError("无法读取 MAA 进程身份。", "identity_unreadable")
        try:
            created, exited, cpu_kernel, cpu_user = (w.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(process, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(cpu_kernel), ctypes.byref(cpu_user)):
                raise MaaVisualError("无法核对 MAA 进程启动时间。", "identity_unreadable")
            return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        finally:
            kernel.CloseHandle(process)

    def dpi(self, hwnd):
        value = int(self.native.user.GetDpiForWindow(hwnd))
        if value <= 0:
            raise MaaVisualError("无法读取 MAA 窗口缩放比例。", "dpi_unreadable")
        return value

    def _integrity(self, process):
        token = w.HANDLE()
        if not self.advapi.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            return None
        try:
            needed = w.DWORD()
            self.advapi.GetTokenInformation(token, 25, None, 0, ctypes.byref(needed))
            if not 1 <= needed.value <= 65536:
                return None
            buffer = ctypes.create_string_buffer(needed.value)
            if not self.advapi.GetTokenInformation(token, 25, buffer, len(buffer), ctypes.byref(needed)):
                return None
            # TOKEN_MANDATORY_LABEL begins with SID_AND_ATTRIBUTES.Sid.
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
            if not sid:
                return None
            count = int(self.advapi.GetSidSubAuthorityCount(sid).contents.value)
            if not 1 <= count <= 15:
                return None
            return int(self.advapi.GetSidSubAuthority(sid, count - 1).contents.value)
        finally:
            self.native.kernel.CloseHandle(token)

    def permission(self, pid):
        kernel = self.native.kernel
        own = self._integrity(kernel.GetCurrentProcess())
        process = kernel.OpenProcess(0x1000, False, pid)
        try:
            target = self._integrity(process) if process else None
        finally:
            if process:
                kernel.CloseHandle(process)
        known = own is not None and target is not None
        return {"own_integrity": own, "target_integrity": target,
                "known": known, "input_allowed": known and own >= target,
                "requires_elevation": known and own < target}

    def capture(self, info, path):
        if not self.use_wgc:
            try:
                return self.native._capture_printwindow(info, path)
            except WindowError:
                path.unlink(missing_ok=True)
        frame = self.native.capture_background(info, path)
        self.use_wgc = True
        return frame

    def focus(self, hwnd):
        self.native.focus_window(hwnd)

    def focus_guarded(self, hwnd, guard):
        """Activate only MAA; if Windows denies it, click a verified caption.

        No keyboard tricks, accessibility calls, game input, or content clicks.
        WM_NCHITTEST must identify HTCAPTION both before and after admission.
        """
        try:
            self.native.focus_window(hwnd)
            return {'focus_method':'set_foreground','caption_click':False}
        except WindowError as error:
            if error.code != 'focus_denied':
                raise
        user = self.native.user
        user.SetWindowPos.argtypes=[w.HWND,w.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,w.UINT]
        user.SetWindowPos.restype=w.BOOL
        user.SendMessageTimeoutW.argtypes=[w.HWND,w.UINT,w.WPARAM,w.LPARAM,w.UINT,w.UINT,ctypes.POINTER(ctypes.c_size_t)]
        user.SendMessageTimeoutW.restype=w.LPARAM
        user.WindowFromPoint.argtypes=[w.POINT];user.WindowFromPoint.restype=w.HWND
        user.GetAncestor.argtypes=[w.HWND,w.UINT];user.GetAncestor.restype=w.HWND
        user.GetAsyncKeyState.argtypes=[ctypes.c_int];user.GetAsyncKeyState.restype=ctypes.c_short
        with self.native._physical_pixels():
            guard()
            # Raise without activation or resize, then verify the nonclient hit.
            if not user.SetWindowPos(hwnd,0,0,0,0,0,0x0253):
                raise MaaVisualError('无法安全抬起 MAA 窗口。','focus_denied')
            rectangle=w.RECT()
            if not user.GetWindowRect(hwnd,ctypes.byref(rectangle)):
                raise MaaVisualError('无法核对 MAA 标题栏。','focus_denied')
            def caption_hit(x,y):
                hit=ctypes.c_size_t()
                point=user.WindowFromPoint(w.POINT(x,y))
                if int(user.GetAncestor(point,2) or 0)!=hwnd:
                    return False
                packed=ctypes.c_int32(((y & 0xffff)<<16)|(x & 0xffff)).value
                return bool(user.SendMessageTimeoutW(hwnd,0x84,0,packed,0x0002,200,ctypes.byref(hit))) and hit.value==2
            width=rectangle.right-rectangle.left
            point=next(((rectangle.left+round(width*f),rectangle.top+offset)
                        for offset in (12,18,24,30) for f in (.3,.4,.5)
                        if caption_hit(rectangle.left+round(width*f),rectangle.top+offset)),None)
            if point is None:
                raise MaaVisualError('MAA 标题栏未能唯一安全定位，未发送点击。','focus_denied')
            x,y=point
            self.native._pointer(x,y,0)
            time.sleep(.016)
            guard()
            actual=w.POINT()
            user.GetPhysicalCursorPos.argtypes=[ctypes.POINTER(w.POINT)]
            user.GetPhysicalCursorPos.restype=w.BOOL
            pointer_ok=(user.GetPhysicalCursorPos(ctypes.byref(actual))
                        and actual.x==x and actual.y==y)
            if (not pointer_ok or not caption_hit(actual.x,actual.y)
                    or any(user.GetAsyncKeyState(key)&0x8000 for key in (1,16,17,18))):
                raise MaaVisualError('MAA 标题栏或鼠标状态已改变，未按下。','focus_denied')
            sent=self._send_mouse((0x0002,0),(0x0004,0))
            if sent!=2:
                if sent==1:self._send_mouse((0x0004,0))
                raise MaaVisualError('MAA 标题栏激活输入未完整提交。','partial_mouse_input',
                                     {'partial_input':sent>0,'input_sent':sent>0})
            deadline=time.monotonic()+1
            while time.monotonic()<deadline:
                if int(user.GetForegroundWindow() or 0)==hwnd:
                    return {'focus_method':'verified_caption','caption_click':True,
                            'caption_point':{'x':x,'y':y}}
                time.sleep(.025)
        raise MaaVisualError('MAA 标题栏激活后未成为前台。','focus_after_input_unknown',{'input_sent':True})

    def click(self, x, y, guard):
        def final_guard():
            guard()
            # Parent authorization can wait on IPC. Verify the physical cursor
            # and covering HWND again after that wait, directly before down.
            self.native._verify_pointer_target(x, y)
        return self.native.click_guarded(x, y, final_guard)

    def click_battle(self, x, y, guard):
        """WPF battle buttons need no Unity-style held-button interval."""
        button = self.native.user.GetAsyncKeyState
        button.argtypes, button.restype = [ctypes.c_int], ctypes.c_short
        if button(1) & 0x8000:
            raise MaaVisualError('鼠标左键已按下，未发送作战按钮输入。', 'button_already_down')
        self._place_queue_pointer(x, y, guard)
        if button(1) & 0x8000:
            raise MaaVisualError('鼠标左键状态已变化，未发送作战按钮输入。', 'button_already_down')
        # Reuse the queue/caption input primitive. Both events enter Windows
        # in one call; cancellation cannot kill a Python sleep between them.
        sent = self._send_mouse((0x0002,0), (0x0004,0))
        if not sent:
            raise MaaVisualError('系统未接收作战按钮输入。', 'input_failed')
        if sent == 1:
            released = self._send_mouse((0x0004,0)) == 1
            raise MaaVisualError('作战按钮输入未完整提交。', 'partial_mouse_input',
                                 {'partial_input':True,'input_sent':True,'button_released':released})
        return {'pointer_verified':True, 'button_release_submitted':True}

    def _place_queue_pointer(self, x, y, guard):
        self.native._pointer(x, y, 0)
        time.sleep(.016)
        self.native._verify_pointer_target(x, y)
        guard()
        self.native._verify_pointer_target(x, y)

    def right_click(self, x, y, guard):
        self._place_queue_pointer(x, y, guard)
        button = self.native.user.GetAsyncKeyState
        button.argtypes, button.restype = [ctypes.c_int], ctypes.c_short
        if button(2) & 0x8000:
            raise MaaVisualError('鼠标右键已按下，未发送队列输入。', 'button_already_down')
        # WPF handles the queued down/up events. Send them as one batch so
        # there is no IPC/cancellation wait or user movement between events.
        sent = self._send_mouse((0x0008,0), (0x0010,0))
        if not sent:
            raise MaaVisualError('系统未接收队列右键输入。', 'input_failed')
        if sent == 1:
            released = self._send_mouse((0x0010,0)) == 1
            raise MaaVisualError('队列右键输入未完整提交。', 'partial_mouse_input',
                                 {'partial_input':True,'input_sent':True,'button_released':released})
        return {'pointer_verified':True, 'button_release_submitted':True}

    def _send_mouse(self, *commands):
        class MouseInput(ctypes.Structure):
            _fields_ = [('dx',w.LONG), ('dy',w.LONG), ('mouseData',w.DWORD),
                        ('dwFlags',w.DWORD), ('time',w.DWORD), ('dwExtraInfo',ctypes.c_size_t)]

        class Input(ctypes.Structure):
            # The mouse variant is the largest member of the Win32 INPUT union.
            _fields_ = [('type',w.DWORD), ('mouse',MouseInput)]

        events = (Input*len(commands))(*(Input(0, MouseInput(0,0,delta & 0xFFFFFFFF,flags,0,0))
                                         for flags,delta in commands))
        send_input = self.native.user.SendInput
        send_input.argtypes, send_input.restype = [w.UINT, ctypes.POINTER(Input), ctypes.c_int], w.UINT
        return send_input(len(events), events, ctypes.sizeof(Input))

    def _send_wheel(self, notches):
        if self._send_mouse((0x0800,notches*120)) != 1:
            raise MaaVisualError('系统未接收队列滚动。', 'input_failed')

    def scroll_queue(self, x, y, notches, guard):
        self._place_queue_pointer(x, y, guard)
        self._send_wheel(notches)
        return {'pointer_verified':True}

    def send_keyboard(self, vk, scan, flags):
        return self.keyboard.send_keyboard(vk, scan, flags)


def discover_maa_windows(*, backend=None):
    """Read-only enumeration of the fixed installed MAA, not arbitrary windows."""
    native = backend or _NativeMaaBackend()
    results = []
    for info in native.list_windows():
        if not _same_path(info.executable, MAA_EXECUTABLE):
            continue
        try:
            results.append({"hwnd": info.hwnd, "pid": info.pid, "executable": info.executable,
                            "creation_time": native.creation_time(info.pid), "title": info.title,
                            "minimized": info.minimized, "foreground": info.foreground,
                            "client_rect": _rect(info), "dpi": native.dpi(info.hwnd),
                            "permission": native.permission(info.pid)})
        except WindowError:
            continue
    return results


def _rect(info):
    return {"left": info.left, "top": info.top, "width": info.width, "height": info.height}


class MaaVisualNative:
    """One immutable MAA process binding; screenshots are single-use input tokens.

    The optional backend is an explicit test seam; production uses no UIA, COM,
    game memory, hooks, remote MAA API or target configuration writes. Native
    capture must remain in a supervised subprocess, never the Qt thread.
    """

    def __init__(self, identity: Mapping, data_dir, *, backend=None,
                 monotonic=time.monotonic, clock=time.time, max_age=10.):
        identity = dict(identity)
        if (any(not _positive(identity.get(k)) for k in ("hwnd", "pid", "creation_time"))
                or not _same_path(identity.get("executable"), MAA_EXECUTABLE)):
            raise ValueError("A complete fixed MAA identity is required")
        if type(max_age) not in (int, float) or not math.isfinite(max_age) or not 0 < max_age <= 30:
            raise ValueError("Screenshot age must be in (0, 30] seconds")
        self.identity = {key: identity[key] for key in ("hwnd", "pid", "creation_time", "executable")}
        self.capture_dir = Path(data_dir).resolve() / "maa-visual-captures"
        self.backend = backend or _NativeMaaBackend()
        self._monotonic, self._clock, self._max_age = monotonic, clock, max_age
        self._last = None
        self._lock = threading.RLock()
        self._permission_cache = None
        self._battle_input = None

    @staticmethod
    def _cancel(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise MaaVisualError("MAA 操作已取消。", "stopped")

    def _current(self, *, foreground=False, permit_minimized=False):
        identity, native = self.identity, self.backend
        info = native.get_window(identity["hwnd"])
        if (info is None or info.hwnd != identity["hwnd"] or info.pid != identity["pid"]
                or not _same_path(info.executable, identity["executable"])
                or native.creation_time(info.pid) != identity["creation_time"]):
            raise MaaVisualError("MAA 窗口或进程已经改变，请重新绑定。", "binding_mismatch")
        if info.minimized and not permit_minimized:
            raise MaaVisualError("请先恢复 MAA 窗口；本通道不把最小化截图视为有效证据。", "minimized")
        if not info.minimized and (info.width <= 0 or info.height <= 0 or info.width * info.height > 40_000_000):
            raise MaaVisualError("MAA 客户区尺寸无效。", "invalid_geometry")
        if foreground and not info.foreground:
            raise MaaVisualError("MAA 不是前台窗口，本次未输入。", "not_foreground")
        return info

    def _permission(self, *, required=False):
        if self._permission_cache is None:
            self._permission_cache = dict(self.backend.permission(self.identity["pid"]))
        report = dict(self._permission_cache)
        if required and not report.get("known"):
            raise MaaVisualError("无法确认 MAA 与执行器的输入权限，已停止输入。", "integrity_unknown", report)
        if required and not report.get("input_allowed"):
            raise MaaVisualError("MAA 的 Windows 权限高于执行器；截图键鼠方式同样不能绕过权限边界。",
                                 "integrity_mismatch", report)
        return report

    def focus(self, *, guard=None, cancel_event=None):
        """Restore/activate MAA; a verified caption click may acquire focus.

        Never maximizes or clicks client content. All focus effects cross the
        same task admission gate and require a new content observation.
        """
        with self._lock:
            self._last = None
            self._cancel(cancel_event)
            info = self._current(permit_minimized=True)
            self._permission(required=True)
            if guard:
                guard()
            self._cancel(cancel_event)
            self._current(permit_minimized=True)
            try:
                def focus_guard():
                    self._cancel(cancel_event)
                    self._current(permit_minimized=True)
                    self._permission(required=True)
                    if guard:guard()
                guarded=getattr(self.backend,'focus_guarded',None)
                details = guarded(info.hwnd,focus_guard) if callable(guarded) else self.backend.focus(info.hwnd)
            except WindowError as error:
                if error.code != 'focus_denied':
                    raise
                current = self._current(permit_minimized=True)
                return {"ok": True, "action": "focus", "window": self._public(current),
                        "focus_acquired": False, "state": "awaiting_foreground",
                        "verification_required": True}
            info = self._current(foreground=True)
            return {"ok": True, "action": "focus", "window": self._public(info), "focus_acquired": True,
                    "state": "unverified", "verification_required": True,
                    **(details if isinstance(details,dict) else {})}

    def _public(self, info):
        return {**self.identity, "title": info.title, "client_rect": _rect(info),
                "dpi": self.backend.dpi(info.hwnd), "foreground": info.foreground,
                "minimized": info.minimized}

    def snapshot(self, *, cancel_event=None):
        with self._lock:
            self._last = None
            self._cancel(cancel_event)
            info = self._current()
            dpi = self.backend.dpi(info.hwnd)
            self.capture_dir.mkdir(parents=True, exist_ok=True)
            identifier = uuid.uuid4().hex
            path = self.capture_dir / f"{identifier}.png"
            started, captured_at = self._monotonic(), self._clock()
            try:
                result = self.backend.capture(info, path)
                self._cancel(cancel_event)
                after = self._current()
                if _geometry(after) != _geometry(info) or self.backend.dpi(after.hwnd) != dpi:
                    raise MaaVisualError("截图期间 MAA 窗口位置、尺寸或缩放改变。", "geometry_changed")
                if (result.get("method") not in {"win32_printwindow", "windows_graphics_capture"}
                        or result.get("requires_foreground")
                        or result.get("width") != info.width or result.get("height") != info.height):
                    raise MaaVisualError("MAA 窗口截图来源或尺寸不正确。", "capture_failed")
                payload = path.read_bytes()
                if (not 24 <= len(payload) <= 80_000_000 or payload[:8] != b"\x89PNG\r\n\x1a\n"
                        or payload[12:16] != b"IHDR"
                        or struct.unpack(">II", payload[16:24]) != (info.width, info.height)):
                    raise MaaVisualError("MAA 截图文件无效。", "invalid_image")
                snapshot = {"screenshot_id": identifier, "image_path": str(path),
                            "image_sha256": hashlib.sha256(payload).hexdigest(),
                            "window": self._public(after), "permission": self._permission(),
                            "coordinate_space": "client_physical_pixels", "captured_at": captured_at,
                            "method": result["method"], "background_capture": True,
                            "recognition_verified": False, "verification_required": True}
                self._last = {"snapshot": snapshot, "info": info, "dpi": dpi, "at": started}
                return snapshot
            except Exception:
                path.unlink(missing_ok=True)
                raise

    def _evidence(self, screenshot_id, cancel_event, *, foreground=True):
        self._cancel(cancel_event)
        last = self._last
        if not last or not isinstance(screenshot_id, str) or screenshot_id != last["snapshot"]["screenshot_id"]:
            raise MaaVisualError("截图已被使用或不是本通道的最新截图。", "observation_mismatch")
        age = self._monotonic() - last["at"]
        if not math.isfinite(age) or not 0 <= age <= self._max_age:
            raise MaaVisualError("MAA 截图已经过期，请重新观察。", "observation_expired")
        info = self._current(foreground=foreground)
        if _geometry(info) != _geometry(last["info"]) or self.backend.dpi(info.hwnd) != last["dpi"]:
            raise MaaVisualError("MAA 窗口位置、尺寸或缩放改变，请重新观察。", "geometry_changed")
        self._permission(required=True)
        return last, info

    def _operation(self, screenshot_id, action, operation, *, guard=None, cancel_event=None, **details):
        with self._lock:
            last, info = self._evidence(screenshot_id, cancel_event)
            self._last = None  # Every issued operation consumes its token, including failures.
            pressed = 0

            def final_guard():
                self._cancel(cancel_event)
                age = self._monotonic() - last["at"]
                if not math.isfinite(age) or not 0 <= age <= self._max_age:
                    raise MaaVisualError("MAA 截图已经过期，请重新观察。", "observation_expired")
                current = self._current(foreground=True)
                if _geometry(current) != _geometry(info) or self.backend.dpi(current.hwnd) != last["dpi"]:
                    raise MaaVisualError("MAA 窗口在输入前改变。", "geometry_changed")
                self._permission(required=True)
                if guard:
                    guard()
                self._cancel(cancel_event)
                age = self._monotonic() - last["at"]
                if not math.isfinite(age) or not 0 <= age <= self._max_age:
                    raise MaaVisualError("授权等待期间 MAA 截图已经过期。", "observation_expired")
                # The authorization callback may itself wait on IPC. Recheck all
                # native state *after* it; no stale rect/focus can authorize input.
                final = self._current(foreground=True)
                if _geometry(final) != _geometry(info) or self.backend.dpi(final.hwnd) != last["dpi"]:
                    raise MaaVisualError("授权等待期间 MAA 窗口改变。", "geometry_changed")

            def send(vk, scan=0, flags=0):
                nonlocal pressed
                self.backend.send_keyboard(vk, scan, flags)
                if not flags & _KEYUP:
                    pressed += 1

            try:
                final_guard()
                result = operation(info, final_guard, send)
            except Exception as error:
                if isinstance(error, WindowError) and pressed:
                    error.details.update(partial_input=True, input_sent=True, key_down_count=pressed)
                raise
            return {"ok": True, "action": action, **details, "window_id": info.hwnd,
                    "screenshot_id": screenshot_id, "executed_at": self._clock(),
                    "state": "unverified", "verification_required": True,
                    "native_evidence": result if isinstance(result, dict) else {}}

    def click_battle(self, x, y, screenshot_id, *, target, guard):
        """Dispatch a verified single-battle button, keeping ordinary import clicks unchanged."""
        from .maa_background_input import MaaClientButtonInput
        from .game_executor import compare_target_patch
        if target not in {'battle_start', 'battle_stop', 'navigation_option'}:
            raise MaaVisualError('未登记的作战按钮。', 'target_not_allowed')
        if target in {'battle_start', 'battle_stop'}:
            # The runner activates MAA and takes a fresh observation first.
            # Native click rechecks foreground again after the durable gate;
            # loss of focus must not fall back to an ineffective PostMessage.
            return self.click(x, y, screenshot_id, target=target, guard=guard)
        with self._lock:
            last, info = self._evidence(screenshot_id, None, foreground=False)
            if info.foreground:
                return self.click(x, y, screenshot_id, target=target, guard=guard)
            self._last = None
            if (type(x) is not int or type(y) is not int or not 0 <= x < info.width
                    or not 0 <= y < info.height or min(info.width, info.height) < 2):
                raise MaaVisualError('作战按钮坐标超出 MAA 客户区。', 'invalid_coordinates')
            def window():
                self._permission(required=True)
                current = self._current()
                self.backend.verify_message_target(current.hwnd)
                return self._public(current)
            window()
            if self._battle_input is None:
                self._battle_input = MaaClientButtonInput(
                    self.identity, self.capture_dir.parent/'maa-button-input', check_window=window)
            def patch(frame):
                return {'path': frame['image_path'], 'width': info.width, 'height': info.height,
                        'window_id': info.hwnd, 'window_pid': info.pid}
            def admit():
                window()
                try:
                    fresh = self.snapshot()
                finally:
                    self._last = None
                point = {'kind': 'click', 'x': x/(info.width-1), 'y': y/(info.height-1)}
                if (fresh['window']['client_rect'] != _rect(info) or fresh['window']['dpi'] != last['dpi']
                        or not 0 <= self._monotonic() - last['at'] <= self._max_age
                        or not compare_target_patch(patch(last['snapshot']), patch(fresh), point)):
                    raise MaaVisualError('MAA 作战按钮已变化，需要重新观察。', 'target_changed')
                guard()
                window()
            receipt = self._battle_input.act('click', [(x, y)], gate=admit, expected_geometry=_rect(info))
            return {**receipt, 'target': target, 'window_id': info.hwnd, 'screenshot_id': screenshot_id}

    def click(self, x, y, screenshot_id, *, button='left', target=None, guard=None, cancel_event=None):
        if type(x) is not int or type(y) is not int:
            raise MaaVisualError("点击需要客户区整数像素。", "invalid_coordinates")
        if button not in ('left', 'right') or (button == 'right' and target != 'queue_add'):
            raise MaaVisualError('该按钮不属于 MAA 队列输入白名单。', 'target_not_allowed')

        def operation(info, final_guard, send):
            if not (0 <= x < info.width and 0 <= y < info.height):
                raise MaaVisualError("点击超出 MAA 客户区。", "out_of_bounds")
            # Existing native implementation checks physical cursor position and
            # WindowFromPoint occlusion before pressing; it always releases.
            if button == 'right':
                click = self.backend.right_click
            elif target in {'battle_start', 'battle_stop'}:
                click = self.backend.click_battle
            else:
                click = self.backend.click
            return click(info.left + x, info.top + y, final_guard)

        return self._operation(screenshot_id, "click", operation, guard=guard,
                               cancel_event=cancel_event, x=x, y=y,
                               **({'button':button,'target':target} if target is not None else {}))

    def scroll_queue(self, x, y, notches, screenshot_id, *, target='queue_list',
                     guard=None, cancel_event=None):
        if target != 'queue_list':
            raise MaaVisualError('只允许滚动已识别的 MAA 作业队列。', 'target_not_allowed')
        if type(notches) is not int or not 1 <= abs(notches) <= 3:
            raise MaaVisualError('队列滚动每次限一至三个刻度。', 'invalid_scroll')
        if type(x) is not int or type(y) is not int:
            raise MaaVisualError('队列滚动需要客户区整数像素。', 'invalid_coordinates')

        def operation(info, final_guard, send):
            if not (0 <= x < info.width and 0 <= y < info.height):
                raise MaaVisualError('滚动点超出 MAA 客户区。', 'out_of_bounds')
            return self.backend.scroll_queue(info.left + x, info.top + y, notches, final_guard)

        return self._operation(screenshot_id, 'scroll_queue', operation, guard=guard,
                               cancel_event=cancel_event, target=target, x=x, y=y, notches=notches)

    @staticmethod
    def _tap(vk, final_guard, send, scan=0, flags=0):
        final_guard()
        try:
            send(vk, scan, flags)
        finally:
            # Releasing an already pressed key must not wait for cancellation,
            # focus or the parent IPC gate, otherwise Ctrl can remain held down.
            send(vk, scan, flags | _KEYUP)

    @classmethod
    def _combo(cls, key, final_guard, send):
        keys = _KEYS[key]
        if len(keys) == 1:
            cls._tap(keys[0], final_guard, send)
        else:
            final_guard()
            try:
                send(keys[0])
                cls._tap(keys[1], final_guard, send)
            finally:
                send(keys[0], 0, _KEYUP)

    def key(self, key, screenshot_id, *, guard=None, cancel_event=None):
        if not isinstance(key, str) or key not in _KEYS:
            raise MaaVisualError("此按键不属于 MAA 导入白名单。", "key_not_allowed")
        return self._operation(screenshot_id, "key",
            lambda info, final_guard, send: self._combo(key, final_guard, send),
            guard=guard, cancel_event=cancel_event, key=key)

    def replace_code(self, code, screenshot_id, *, guard=None, cancel_event=None):
        """Replace a visually selected input with one exact code; never Enter."""
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            raise MaaVisualError("此入口只接受 prts://数字 神秘代码。", "invalid_copilot_code")

        def operation(info, final_guard, send):
            self._combo("CTRL+A", final_guard, send)
            for character in code:
                self._tap(0, final_guard, send, ord(character), _UNICODE)

        return self._operation(screenshot_id, "replace_code", operation, guard=guard,
                               cancel_event=cancel_event, code=code, characters=len(code))

    def clear_code(self, screenshot_id, *, guard=None, cancel_event=None):
        def operation(info, final_guard, send):
            self._combo("CTRL+A", final_guard, send)
            self._combo("BACKSPACE", final_guard, send)

        return self._operation(screenshot_id, "clear_code", operation, guard=guard, cancel_event=cancel_event)

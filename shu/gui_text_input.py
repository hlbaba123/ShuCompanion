"""Experimental, MAA-only keyboard input for an isolated GUI acceptance runner.

This deliberately reuses private WindowController guards; it is not a general
desktop keyboard API. Callers fix the allowed texts locally, focus the selected
MAA window, and obtain a new observation before *each* call. A successful call
consumes that observation and still requires visual verification.

An injectable ``controller._gui_text_input_backend`` may implement
``send_keyboard(vk, scan, flags)``. The production default uses Win32 SendInput.
No keyboard or window API is invoked merely by importing this module.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import math

from .windows import WindowController, WindowError, WindowInfo, _geometry


_KEYS = {'CTRL+A': (0x11, 0x41), 'ENTER': (0x0D,), 'ESC': (0x1B,),
         'TAB': (0x09,), 'BACKSPACE': (0x08,)}
_KEYUP = 0x0002
_UNICODE = 0x0004


def _is_maa(info: WindowInfo) -> bool:
    return info.executable.replace('\\', '/').rsplit('/', 1)[-1].casefold() == 'maa.exe'


def _owner(native, hwnd: int) -> int:
    if hasattr(native, 'get_owner'):
        return int(native.get_owner(hwnd) or 0)
    native.user.GetWindow.argtypes = [w.HWND, w.UINT]
    native.user.GetWindow.restype = w.HWND
    return int(native.user.GetWindow(hwnd, 4) or 0)  # GW_OWNER


def _owned_by(native, hwnd: int, owner: int) -> bool:
    seen = {hwnd}
    for _ in range(16):
        hwnd = _owner(native, hwnd)
        if hwnd == owner:
            return True
        if not hwnd or hwnd in seen:
            return False
        seen.add(hwnd)
    return False


def confirm_maa_dialog(controller: WindowController, dialog_hwnd: int, *, maa_hwnd: int) -> WindowInfo:
    """Record a caller-selected dialog only after same-PID and owner checks.

    This does not bind, focus, capture, enable input, or send an event. The runner
    must bind the returned HWND and capture fresh evidence before using it.
    """
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
           for value in (dialog_hwnd, maa_hwnd)) or dialog_hwnd == maa_hwnd:
        raise WindowError('MAA 对话框身份无效。', 'maa_dialog_unconfirmed')
    with controller._lock:
        native = controller._native()
        main, dialog = native.get_window(maa_hwnd), native.get_window(dialog_hwnd)
        if (main is None or dialog is None or not _is_maa(main) or main.pid != dialog.pid
                or not _owned_by(native, dialog_hwnd, maa_hwnd)):
            raise WindowError('尚未确认此窗口是同一 MAA 进程拥有的对话框。', 'maa_dialog_unconfirmed')
        confirmed = dict(getattr(controller, '_maa_confirmed_dialogs', {}))
        confirmed[dialog_hwnd] = (dialog.pid, main.hwnd, main.executable)
        controller._maa_confirmed_dialogs = confirmed
        return dialog


def _require_scope(controller: WindowController, info: WindowInfo) -> None:
    if _is_maa(info):
        return
    record = getattr(controller, '_maa_confirmed_dialogs', {}).get(info.hwnd)
    if record and info.pid == record[0]:
        native = controller._native()
        main = native.get_window(record[1])
        if (main is not None and main.pid == info.pid and _is_maa(main)
                and main.executable == record[2] and _owned_by(native, info.hwnd, main.hwnd)):
            return
    raise WindowError('键盘输入仅限 MAA.exe 或已确认的同进程对话框。', 'maa_scope_required')


class _Win32Keyboard:
    def __init__(self, native):
        self.user = native.user

    def send_keyboard(self, vk: int, scan: int, flags: int) -> None:
        class MouseInput(ctypes.Structure):
            _fields_ = [('dx', w.LONG), ('dy', w.LONG), ('mouseData', w.DWORD),
                        ('dwFlags', w.DWORD), ('time', w.DWORD), ('dwExtraInfo', ctypes.c_size_t)]

        class KeyboardInput(ctypes.Structure):
            _fields_ = [('wVk', w.WORD), ('wScan', w.WORD), ('dwFlags', w.DWORD),
                        ('time', w.DWORD), ('dwExtraInfo', ctypes.c_size_t)]

        class HardwareInput(ctypes.Structure):
            _fields_ = [('uMsg', w.DWORD), ('wParamL', w.WORD), ('wParamH', w.WORD)]

        class InputUnion(ctypes.Union):
            _fields_ = [('mi', MouseInput), ('ki', KeyboardInput), ('hi', HardwareInput)]

        class Input(ctypes.Structure):
            _fields_ = [('type', w.DWORD), ('value', InputUnion)]

        event = Input()
        event.type = 1  # INPUT_KEYBOARD
        event.value.ki = KeyboardInput(vk, scan, flags, 0, 0)
        self.user.SendInput.argtypes = [w.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.user.SendInput.restype = w.UINT
        if self.user.SendInput(1, ctypes.byref(event), ctypes.sizeof(Input)) != 1:
            raise WindowError('Windows 未接收 MAA 键盘输入。', 'keyboard_input_failed',
                              {'win32_error': ctypes.get_last_error()})


def _execute(controller, observation_id, action, operation, cancel_event=None, **details):
    controller._check_stop(cancel_event)
    with controller._lock:
        controller._check_stop(cancel_event)
        latest, evidence = controller._latest, controller._latest_info
        if not latest or observation_id != latest['id'] or evidence is None:
            raise WindowError('观察记录已失效，请重新截图。', 'observation_mismatch')
        info = controller._require_window()

        def guard():
            controller._check_stop(cancel_event)
            age = controller._monotonic() - controller._latest_at
            if not math.isfinite(age) or age < 0 or age > controller._max_age:
                raise WindowError('截图已过期，请重新截图。', 'observation_expired')
            current = controller._require_window()
            if ((current.hwnd, current.pid) != (info.hwnd, info.pid)
                    or _geometry(current) != _geometry(evidence)):
                raise WindowError('MAA 窗口身份或客户区已变化。', 'geometry_changed')
            _require_scope(controller, current)
            controller._require_input_permission(current)

        guard()
        backend = getattr(controller, '_gui_text_input_backend', None)
        if backend is None:
            backend = _Win32Keyboard(controller._native())
        # A partial or failed input must never be retried with this evidence.
        controller._latest = controller._latest_info = None
        sent_down = 0

        def send(vk, scan, flags):
            nonlocal sent_down
            backend.send_keyboard(vk, scan, flags)
            if not flags & _KEYUP:
                sent_down += 1

        try:
            operation(guard, send)
        except WindowError as error:
            if sent_down:
                error.details.update(partial_input=True, input_sent=True)
            raise
        return {'ok': True, 'action': action, **details, 'window_id': info.hwnd,
                'observation_id': observation_id, 'executed_at': controller._clock(),
                'state': 'unverified', 'verification_required': True}


def _tap(guard, send, vk: int, scan: int = 0, flags: int = 0):
    guard()
    try:
        send(vk, scan, flags)
    finally:
        # Releases deliberately bypass cancellation/focus checks.
        send(vk, scan, flags | _KEYUP)


def guarded_type(controller: WindowController, text: str, observation_id: str, *,
                 allowed_texts: set[str], cancel_event=None) -> dict:
    """Send one exact locally whitelisted text, never paste or arbitrary code."""
    if (not isinstance(text, str) or not 1 <= len(text) <= 512
            or any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in text)):
        raise WindowError('MAA 输入文本必须是 1 到 512 字的普通文本。', 'invalid_text')
    if not isinstance(allowed_texts, (set, frozenset)) or text not in allowed_texts:
        raise WindowError('此文本不在本次 MAA 任务的固定白名单中。', 'text_not_allowed')
    try:
        encoded = text.encode('utf-16-le', errors='strict')
    except UnicodeError:
        raise WindowError('MAA 输入文本编码无效。', 'invalid_text') from None

    def type_text(guard, send):
        for offset in range(0, len(encoded), 2):
            _tap(guard, send, 0, int.from_bytes(encoded[offset:offset + 2], 'little'), _UNICODE)

    return _execute(controller, observation_id, 'type', type_text, cancel_event, characters=len(text))


def guarded_key(controller: WindowController, key: str, observation_id: str, cancel_event=None) -> dict:
    """Send only CTRL+A, ENTER, ESC, TAB, or BACKSPACE to the guarded MAA HWND."""
    if not isinstance(key, str) or key not in _KEYS:
        raise WindowError('此按键不在 MAA 任务的固定白名单中。', 'key_not_allowed')

    def press_key(guard, send):
        keys = _KEYS[key]
        if len(keys) == 1:
            _tap(guard, send, keys[0])
            return
        guard()
        try:
            send(keys[0], 0, 0)
            _tap(guard, send, keys[1])
        finally:
            send(keys[0], 0, _KEYUP)

    return _execute(controller, observation_id, 'key', press_key, cancel_event, key=key)

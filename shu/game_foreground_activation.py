"""Guarded adaptation of MaaFramework v5.15.0 ForegroundUtils.h.

Only window activation/Z-order APIs are used. In contrast to SDK SeizeInput,
the result reports the actual foreground HWND; this module never clicks or
presses a key. The caller must require a true result before physical input.
https://github.com/MaaXYZ/MaaFramework/blob/v5.15.0/source/MaaWin32ControlUnit/Base/ForegroundUtils.h
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import time

from .windows import WindowError


def _prototype(function, arguments, result):
    # Real ctypes functions need pointer-width declarations; test facades do
    # not. Never change the process-global desktop input API or hook anything.
    if isinstance(function,ctypes._CFuncPtr):
        function.argtypes=arguments
        function.restype=result


def activate_game(native, hwnd, guard):
    """Attempt official activation, preserving the user's topmost setting.

    ``guard`` is the existing identity/permission/durable-admission gate. It is
    checked before and after every normal desktop mutation. If revoked while
    the window is temporarily topmost, only restoring the original Z-order
    flag is allowed without further admission. That cleanup is limited to the
    same live HWND/PID/thread and sends no game content input.
    """
    if type(hwnd) is not int or hwnd<=0:
        raise WindowError('Invalid game HWND','game_binding_changed',{'input_sent':False})
    user=native.user
    position=user.SetWindowPos
    style=getattr(user,'GetWindowLongPtrW',None) or user.GetWindowLongW
    _prototype(position,[wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,
                         ctypes.c_int,ctypes.c_int,wintypes.UINT],wintypes.BOOL)
    _prototype(style,[wintypes.HWND,ctypes.c_int],ctypes.c_ssize_t)
    _prototype(user.GetWindowThreadProcessId,[wintypes.HWND,
               ctypes.POINTER(wintypes.DWORD)],wintypes.DWORD)

    def owner():
        if not user.IsWindow(hwnd):return None
        pid=wintypes.DWORD()
        thread=int(user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid)))
        return (pid.value,thread) if pid.value and thread else None

    guard()
    original_owner=owner()
    if original_owner is None:
        raise WindowError('Bound game window disappeared','game_binding_changed',{'input_sent':False})
    temporary_topmost=False

    def checked():
        guard()
        if owner()!=original_owner:
            raise WindowError('Bound game window changed','game_binding_changed',{'input_sent':False})

    def mutate(function,*args,required=False):
        checked()
        value=function(*args)
        checked()
        if required and not value:
            raise WindowError('Window activation API failed','focus_activation_failed',{'input_sent':False})
        return value

    with native._physical_pixels():
        original_topmost=bool(int(style(hwnd,-20))&0x8)  # GWL_EXSTYLE / WS_EX_TOPMOST.

        def place(z_order,flags):
            nonlocal temporary_topmost
            checked()
            # Track the attempted temporary change before the API: a failed
            # return or a revoked post-call guard cannot skip safe restoration.
            if z_order==-1 and not original_topmost:temporary_topmost=True
            succeeded=position(hwnd,z_order,0,0,0,0,flags)
            if succeeded and z_order==-2:temporary_topmost=False
            checked()
            if not succeeded:
                raise WindowError('Window activation API failed','focus_activation_failed',{'input_sent':False})

        try:
            checked()
            if user.IsIconic(hwnd):
                # A false ShowWindow return indicates the previous visibility,
                # not failure. The following identity/foreground readbacks are
                # authoritative; no game content is touched by SW_RESTORE.
                mutate(user.ShowWindow,hwnd,9)
            if user.GetForegroundWindow()!=hwnd:
                # Same bounded ordering and delays as the pinned SDK helper.
                place(0,0x43)  # HWND_TOP, SWP_NOMOVE|NOSIZE|SHOWWINDOW.
                time.sleep(.005)
                mutate(user.SetForegroundWindow,hwnd)
                time.sleep(.010)
                checked()
                if user.GetForegroundWindow()!=hwnd:
                    place(-1,0x03)  # HWND_TOPMOST, NOMOVE|NOSIZE.
                    if not original_topmost:
                        place(-2,0x03)  # Restore HWND_NOTOPMOST before returning.
                    time.sleep(.005)
            checked()
            return {'focus_acquired':bool(user.GetForegroundWindow()==hwnd),
                    'focus_method':'maafw_official_activation','caption_click':False,
                    'input_sent':False}
        finally:
            if temporary_topmost and owner()==original_owner:
                # Cleanup after revocation cannot run the normal guard again.
                # It restores only our temporary flag, never activates/clicks.
                if not position(hwnd,-2,0,0,0,0,0x13):
                    raise WindowError('Unable to restore temporary topmost setting',
                        'focus_activation_cleanup_failed',{'input_sent':False})

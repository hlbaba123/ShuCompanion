"""Release-only cleanup after an owned foreground-drag helper has exited.

The caller MUST prove exclusive ownership, that the button was initially up,
that its admitted drag could press it, and that the helper has actually exited.
This class cannot establish those facts itself and must never be used to tidy
arbitrary mouse input. It only queries state or sends one LEFTUP with no motion.

Fixed reference: MaaFramework v5.15.0 SeizeInput.cpp touch_up; Microsoft SendInput
and GetAsyncKeyState. The latter's zero return is ambiguous on inaccessible
desktops, so desktop and foreground-integrity checks are required before any
claim that the button is up. No focus/desktop switch or keyboard APIs are used.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import time

from .windows import WindowError


class DragCleanupError(WindowError):
    def __init__(self, message):
        super().__init__(message,'drag_release_unverified',{'input_sent':False})


class _MouseInput(ctypes.Structure):
    _fields_=[('dx',w.LONG),('dy',w.LONG),('mouseData',w.DWORD),
              ('dwFlags',w.DWORD),('time',w.DWORD),('dwExtraInfo',ctypes.c_size_t)]


class _Input(ctypes.Structure):
    # MOUSEINPUT is the largest INPUT union member and preserves its alignment.
    _fields_=[('type',w.DWORD),('mi',_MouseInput)]


def _prototype(function,arguments,result):
    if isinstance(function,ctypes._CFuncPtr):
        function.argtypes,function.restype=arguments,result


class ReleaseOnlyLeftButton:
    def __init__(self,*,native=None,clock=time.monotonic,sleep=time.sleep):
        if native is None:
            from .maa_visual_native import _NativeMaaBackend
            native=_NativeMaaBackend()
        self.native=native
        self.user,self.kernel=native.native.user,native.native.kernel
        self.clock,self.sleep=clock,sleep
        u=self.user
        _prototype(u.OpenInputDesktop,[w.DWORD,w.BOOL,w.DWORD],w.HANDLE)
        _prototype(u.CloseDesktop,[w.HANDLE],w.BOOL)
        _prototype(u.GetThreadDesktop,[w.DWORD],w.HANDLE)
        _prototype(u.GetProcessWindowStation,[],w.HANDLE)
        _prototype(u.GetUserObjectInformationW,[w.HANDLE,ctypes.c_int,ctypes.c_void_p,
                   w.DWORD,ctypes.POINTER(w.DWORD)],w.BOOL)
        _prototype(u.GetForegroundWindow,[],w.HWND)
        _prototype(u.GetWindowThreadProcessId,[w.HWND,ctypes.POINTER(w.DWORD)],w.DWORD)
        _prototype(u.GetSystemMetrics,[ctypes.c_int],ctypes.c_int)
        _prototype(u.GetAsyncKeyState,[ctypes.c_int],ctypes.c_short)
        _prototype(u.SendInput,[w.UINT,ctypes.POINTER(_Input),ctypes.c_int],w.UINT)
        _prototype(self.kernel.GetCurrentThreadId,[],w.DWORD)

    def _name(self,handle):
        if not handle:raise DragCleanupError('Desktop handle unavailable')
        needed=w.DWORD()
        self.user.GetUserObjectInformationW(handle,2,None,0,ctypes.byref(needed))
        if not 2<=needed.value<=2048:
            raise DragCleanupError('Desktop identity unavailable')
        value=ctypes.create_unicode_buffer(needed.value//ctypes.sizeof(ctypes.c_wchar)+1)
        if not self.user.GetUserObjectInformationW(handle,2,value,ctypes.sizeof(value),ctypes.byref(needed)):
            raise DragCleanupError('Desktop identity unavailable')
        return value.value.casefold()

    def _context(self):
        # DESKTOP_READOBJECTS | DESKTOP_HOOKCONTROL: GetAsyncKeyState documents
        # failure when foreground input access is unavailable on the desktop.
        desktop=self.user.OpenInputDesktop(0,False,0x0009)
        if not desktop:raise DragCleanupError('Active input desktop inaccessible')
        try:
            active=self._name(desktop)
            current=self._name(self.user.GetThreadDesktop(self.kernel.GetCurrentThreadId()))
            station=self._name(self.user.GetProcessWindowStation())
            if (active!='default' or current!=active or station!='winsta0'):
                raise DragCleanupError('Current thread is not on the normal active input desktop')
            hwnd=int(self.user.GetForegroundWindow() or 0)
            pid=w.DWORD()
            if not hwnd or not self.user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid)) or not pid.value:
                raise DragCleanupError('Foreground input owner unavailable')
            permission=self.native.permission(pid.value)
            if permission.get('known') is not True or permission.get('input_allowed') is not True:
                raise DragCleanupError('Foreground input integrity is not verified')
            if self.user.GetSystemMetrics(23):
                raise DragCleanupError('Swapped mouse-button mapping is not verified for release cleanup')
            return (active,current,station,hwnd,pid.value)
        finally:
            self.user.CloseDesktop(desktop)

    def is_down(self):
        before=self._context()
        state=self.user.GetAsyncKeyState(1)  # VK_LBUTTON: physical left button.
        if self._context()!=before:
            raise DragCleanupError('Input desktop or foreground changed during button-state query')
        return bool(state&0x8000)

    def release_after_exit(self):
        """One release, only after the caller has confirmed its helper exited."""
        if not self.is_down():
            return {'button_released':True,'release_only':True,'input_sent':False}
        # Recheck immediately before the sole input event. No coordinate,
        # absolute/relative movement, button-down, wheel or key flags exist here.
        self._context()
        event=_Input(0,_MouseInput(0,0,0,0x0004,0,0))
        sent=self.user.SendInput(1,ctypes.byref(event),ctypes.sizeof(_Input))==1
        if not sent:
            return {'button_released':False,'release_only':True,'input_sent':False}
        deadline=self.clock()+.2
        while True:
            try:released=not self.is_down()
            except DragCleanupError:released=False;break
            if released or self.clock()>=deadline:break
            self.sleep(.01)
        return {'button_released':released,'release_only':True,'input_sent':True}

"""Read-only Windows input privilege preflight.

This module never requests elevation, starts an executable, changes permissions,
or sends input. Loading an EXE as a data/image resource does not execute it.
Token elevation is evidence of an administrator level difference, not a proof
that game input will work (focus, integrity policy, and game behavior also matter).
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
from typing import Mapping
import xml.etree.ElementTree as ET


_LEVELS = {'asInvoker', 'highestAvailable', 'requireAdministrator'}


def parse_manifest_level(payload: bytes) -> str | None:
    """Parse only the declared execution level, including namespaced manifests."""
    if not isinstance(payload, bytes) or not payload or len(payload) > 1024 * 1024:
        return None
    try:
        if payload.startswith((b'\xff\xfe', b'\xfe\xff')):
            source = payload.decode('utf-16').rstrip('\x00 \r\n\t')
        elif payload.startswith(b'<\x00'):
            source = payload.decode('utf-16-le').rstrip('\x00 \r\n\t')
        elif payload.startswith(b'\x00<'):
            source = payload.decode('utf-16-be').rstrip('\x00 \r\n\t')
        else:
            source = payload.rstrip(b'\x00 \r\n\t')
        # Manifest resources have no need for entity declarations.
        if (b'<!DOCTYPE' in source.upper() if isinstance(source, bytes)
                else '<!DOCTYPE' in source.upper()):
            return None
        root = ET.fromstring(source)
        for element in root.iter():
            if element.tag.rsplit('}', 1)[-1] == 'requestedExecutionLevel':
                level = element.get('level')
                return level if level in _LEVELS else None
    except (UnicodeError, ET.ParseError, ValueError):
        pass
    return None


class _NativePermissions:
    def __init__(self):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.advapi = ctypes.WinDLL('advapi32', use_last_error=True)
        k, a = self.kernel, self.advapi
        k.GetCurrentProcess.argtypes = []
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        a.OpenProcessToken.restype = wintypes.BOOL
        a.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        a.GetTokenInformation.restype = wintypes.BOOL
        k.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
        k.LoadLibraryExW.restype = wintypes.HMODULE
        k.FreeLibrary.argtypes = [wintypes.HMODULE]
        k.FreeLibrary.restype = wintypes.BOOL
        k.FindResourceW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p]
        k.FindResourceW.restype = wintypes.HANDLE
        k.SizeofResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
        k.SizeofResource.restype = wintypes.DWORD
        k.LoadResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
        k.LoadResource.restype = wintypes.HANDLE
        k.LockResource.argtypes = [wintypes.HANDLE]
        k.LockResource.restype = ctypes.c_void_p
        self._resource_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE,
                                                     ctypes.c_void_p, ctypes.c_void_p,
                                                     wintypes.LPARAM)
        k.EnumResourceNamesW.argtypes = [wintypes.HMODULE, ctypes.c_void_p,
                                        self._resource_callback, wintypes.LPARAM]
        k.EnumResourceNamesW.restype = wintypes.BOOL

    def _elevation(self, process) -> tuple[bool | None, int | None]:
        token = wintypes.HANDLE()
        if not self.advapi.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return None, ctypes.get_last_error()
        try:
            elevation = wintypes.DWORD()
            returned = wintypes.DWORD()
            if not self.advapi.GetTokenInformation(token, 20, ctypes.byref(elevation),
                                                   ctypes.sizeof(elevation), ctypes.byref(returned)):
                return None, ctypes.get_last_error()
            return bool(elevation.value), None
        finally:
            self.kernel.CloseHandle(token)

    def own_elevation(self) -> tuple[bool | None, int | None]:
        return self._elevation(self.kernel.GetCurrentProcess())

    def target_elevation(self, pid: int) -> tuple[bool | None, int | None]:
        if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= 0xffffffff:
            return None, 87  # ERROR_INVALID_PARAMETER; never query another PID accidentally.
        process = self.kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not process:
            return None, ctypes.get_last_error()
        try:
            return self._elevation(process)
        finally:
            self.kernel.CloseHandle(process)

    def manifest_level(self, executable: str) -> str | None:
        if not isinstance(executable, str) or not executable or '\x00' in executable:
            return None
        # AS_DATAFILE | AS_IMAGE_RESOURCE: map resources without invoking DllMain,
        # resolving imported code, or starting the target application.
        module = self.kernel.LoadLibraryExW(executable, None, 0x00000002 | 0x00000020)
        if not module:
            return None
        try:
            levels = []

            def collect(handle, resource_type, resource_name, parameter):
                try:
                    resource = self.kernel.FindResourceW(handle, resource_name, resource_type)
                    if not resource:
                        return True
                    size = self.kernel.SizeofResource(handle, resource)
                    if not 0 < size <= 1024 * 1024:
                        return True
                    loaded = self.kernel.LoadResource(handle, resource)
                    pointer = self.kernel.LockResource(loaded) if loaded else None
                    if pointer:
                        level = parse_manifest_level(ctypes.string_at(pointer, size))
                        if level:
                            levels.append(level)
                    return not levels
                except (OSError, ValueError, TypeError):
                    return False

            callback = self._resource_callback(collect)
            self.kernel.EnumResourceNamesW(module, ctypes.c_void_p(24), callback, 0)  # RT_MANIFEST
            return levels[0] if levels else None
        finally:
            self.kernel.FreeLibrary(module)


def _report(own: bool | None, target: bool | None, target_error: int | None,
            manifest: str | None, own_error: int | None = None) -> dict:
    # An observed target token overrides a manifest. Compatibility settings can
    # run an executable asInvoker despite its embedded declaration.
    declaration_only = target is None and manifest == 'requireAdministrator'
    requires = own is False and (target is True or declaration_only)
    if own is False and target is True:
        message = '游戏当前以管理员权限运行，桌宠未提升权限，Windows 可能阻止游戏鼠标输入。需要你确认后以管理员身份重启桌宠。'
    elif requires:
        message = '游戏程序声明需要管理员权限，但本次未能读取其运行令牌，尚未确认游戏的实际权限。当前桌宠未提升，建议你确认后以管理员身份重启桌宠再测试。'
    elif own is True:
        message = '桌宠当前已提升权限；没有发现需要再次提升的管理员权限差异。游戏输入是否生效仍需通过实际结果核验。'
    elif own is False and target is False:
        message = '桌宠和游戏当前均未提升权限，没有检测到管理员权限层级差异。'
    elif own is None:
        message = '暂时无法确认桌宠自身的运行权限，不能据此判断是否需要提升权限。'
    else:
        message = '暂时无法读取游戏的运行权限，没有足够证据判断是否需要提升权限。'
    return {'own_elevated': own, 'target_elevated': target,
            'target_token_error': target_error, 'own_token_error': own_error,
            'manifest_level': manifest, 'requires_elevation': requires,
            'message': message}


def inspect_game_input_permission(window, *, native=None) -> dict:
    """Inspect a WindowInfo or observation dict without changing OS state.

    ``native`` is an injectable read-only adapter for tests. Failures remain
    unknown; access denied alone does not establish target elevation.
    """
    if native is None and sys.platform != 'win32':
        result = _report(None, None, None, None)
        result['message'] = '当前系统不支持 Windows 游戏输入权限诊断。'
        return result
    if isinstance(window, Mapping):
        pid = window.get('pid', window.get('window_pid'))
        executable = window.get('executable', window.get('window_executable', ''))
    else:
        pid = getattr(window, 'pid', None)
        executable = getattr(window, 'executable', '')
    try:
        selected = native if native is not None else _NativePermissions()
    except (OSError, AttributeError):
        return _report(None, None, None, None)
    try:
        own, own_error = selected.own_elevation()
    except (OSError, ValueError):
        own, own_error = None, None
    try:
        target, target_error = selected.target_elevation(pid)
    except (OSError, ValueError):
        target, target_error = None, None
    try:
        manifest = selected.manifest_level(executable)
    except (OSError, ValueError):
        manifest = None
    return _report(own, target, target_error, manifest, own_error)

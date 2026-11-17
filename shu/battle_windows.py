"""Open the verified local applications and wait for their actual windows."""
from __future__ import annotations

import csv
import io
import ntpath
import os
from pathlib import Path
import subprocess
import time

from .maa_visual_native import MAA_EXECUTABLE
from .windows import WindowError

_PROGRAMS = {'maa': Path(MAA_EXECUTABLE), 'game': Path(r'D:\Arknights\Arknights.exe')}
_NAMES = {'maa': 'MAA', 'game': '明日方舟'}


def allow_maa_startup(settings):
    try:
        direct = settings['Configurations'][settings['Current']]['Gui']['StartUpSettings']['RunDirectly']
    except (KeyError, TypeError):
        direct = None
    if direct is not False:
        raise WindowError('MAA 的启动后自动执行尚未确认关闭，无法安全拉起客户端。', 'maa_startup_queue_unverified')


def _running(surface):
    # Include processes that are still initializing or hidden in the tray.
    # tasklist returns OEM-encoded CSV; executable names here are ASCII.
    exe = _PROGRAMS[surface].name
    system = Path(os.environ['SystemRoot']) / 'System32' / 'tasklist.exe'
    try:
        completed = subprocess.run([str(system), '/fi', 'imagename eq ' + exe, '/fo', 'csv', '/nh'],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
                                   timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as error:
        raise WindowError(f'无法核对{_NAMES[surface]}进程，启动已暂停。', 'startup_process_unknown') from error
    rows = csv.reader(io.StringIO(completed.stdout.decode('ascii', errors='replace')))
    return any(row and row[0].casefold() == exe.casefold() for row in rows)


def _launch(surface):
    from . import maa_queue_receipt as receipts
    executable = receipts._no_links(_PROGRAMS[surface])
    if not executable.is_file():
        raise WindowError(f'{_NAMES[surface]} 的本地程序不存在：{executable}', 'startup_executable_missing')
    if surface == 'maa':
        raw, _ = receipts._read_stable(executable.parent / 'config/gui.new.json')
        allow_maa_startup(receipts._json(raw))
    # Shell launch also handles executables with requireAdministrator manifests.
    # The calling process never acknowledges the Windows UAC prompt.
    os.startfile(str(executable), 'open', cwd=str(executable.parent), show_cmd=4)


def wait_for_window(surface, discover, guard, *, running=_running, launch=_launch,
                    clock=time.monotonic, sleep=time.sleep, timeout=60):
    deadline = clock() + timeout
    checked = False
    while True:
        guard()
        windows = discover()
        if len(windows) == 1:
            return windows[0]
        if len(windows) > 1:
            raise WindowError(f'发现多个{_NAMES[surface]}窗口，无法确定目标。', 'ambiguous_startup_window')
        if not checked:
            if not running(surface):
                guard()
                launch(surface)
            checked = True
        if clock() >= deadline:
            raise WindowError(f'{_NAMES[surface]}进程尚未出现可绑定窗口。', 'startup_window_timeout')
        sleep(.25)


def ensure_maa_window(guard):
    from .maa_visual_native import discover_maa_windows
    return wait_for_window('maa', discover_maa_windows, guard)


def ensure_game_window(guard):
    from .windows import _NativeWindows
    native = _NativeWindows()

    def discover():
        return [w for w in native.list_windows() if w.is_game_client and
                ntpath.isabs(w.executable) and ntpath.basename(w.executable).casefold() == 'arknights.exe']

    return wait_for_window('game', discover, guard)

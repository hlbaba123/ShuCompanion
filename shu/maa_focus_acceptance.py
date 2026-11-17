"""Exercise the production window handoff and idle monitor without starting a battle."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import ntpath
from pathlib import Path


def _require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _capture_idle(supervisor, reader, maa, *, foreground):
    reply = supervisor.request({'op': 'snapshot', 'args': {'surface': 'maa'}}, timeout=15)
    _require(reply['status'] == 'completed', 'MAA snapshot: ' + str(reply.get('error')))
    frame = reply['result']
    window = frame['window']
    _require(all(window.get(key) == value for key, value in maa.items())
             and not window['minimized'] and frame['background_capture'] is True,
             'MAA capture is not bound to the original visible window')
    if foreground is not None:
        _require(window['foreground'] is foreground, 'Unexpected foreground state during MAA capture')
    path = Path(frame['image_path'])
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    _require(digest == frame['image_sha256'], 'Captured image changed')
    observed = reader.observe(path)
    _require(observed.get('image_sha256') == digest and observed.get('page') == 'copilot'
             and observed.get('idle') is True and observed.get('running') is False,
             'Idle Copilot screen not verified by local OCR')
    return {'screenshot_id': frame['screenshot_id'], 'image_path': str(path), 'sha256': digest,
            'method': frame['method'], 'foreground': window['foreground'], 'idle': True,
            'code': observed.get('code_text', ''), 'client_rect': window['client_rect']}


def _request_focus(supervisor, surface, **extra):
    command = {'op': 'act', 'args': {'surface': surface, 'action': 'focus', **extra}}
    reply = supervisor.request(command, timeout=15)
    _require(reply['status'] == 'completed', surface + ' focus: ' + str(reply.get('error')))
    return reply['result']


def _visible_game(native, game, maa):
    """Verify z-order on an actual overlapping client pixel, without moving the pointer."""
    g = native.get_window(game['hwnd'])
    m = native.get_window(maa['hwnd'])
    _require(g and m and g.pid == game['pid'] and m.pid == maa['pid']
             and not g.minimized and not m.minimized, 'Window binding or visibility changed')
    left, top = max(g.left,m.left), max(g.top,m.top)
    right, bottom = min(g.left+g.width,m.left+m.width), min(g.top+g.height,m.top+m.height)
    overlap = right > left and bottom > top
    point = ((left+right)//2, (top+bottom)//2) if overlap else (g.left+g.width//2,g.top+g.height//2)
    with native.native._physical_pixels():
        user = native.native.user
        user.WindowFromPoint.argtypes, user.WindowFromPoint.restype = [wintypes.POINT], wintypes.HWND
        user.GetAncestor.argtypes, user.GetAncestor.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
        owner = user.GetAncestor(user.WindowFromPoint(wintypes.POINT(*point)), 2)
    _require(owner == g.hwnd and not m.foreground, 'Game is not visible in front of MAA')
    return {'point': list(point), 'game_hwnd': g.hwnd, 'maa_foreground': m.foreground,
            'game_foreground': g.foreground, 'sample_in_both_clients': overlap}


def run_focus_acceptance(output, resources, *, version):
    from .gui_supervisor import GuiSupervisor
    from .maa_visual_native import _NativeMaaBackend, discover_maa_windows
    from .maa_background_input import _runtime_path
    from .maa_framework_observer import MaaFrameworkOcr
    from .maa_visual_readback import MaaVisualReadback
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'version': version, 'passed': False, 'battle_start_submissions': 0,
              'scope': 'real foreground handoff and background idle readback only'}
    supervisor = None
    try:
        native = _NativeMaaBackend()
        maas = discover_maa_windows(backend=native)
        games = [w for w in native.list_windows() if w.is_game_client
                 and ntpath.isabs(w.executable) and ntpath.basename(w.executable).lower() == 'arknights.exe']
        _require(len(maas) == len(games) == 1, 'One existing MAA and one game window required')
        maa = {k: maas[0][k] for k in ('hwnd','pid','creation_time','executable')}
        game = {k: getattr(games[0],k) for k in ('hwnd','pid','executable')}
        game['creation_time'] = native.creation_time(game['pid'])
        supervisor = GuiSupervisor(maa, backend='battle', navigation_identity=game, directory=output)
        reader = MaaVisualReadback(ocr=MaaFrameworkOcr(_runtime_path(),resources/'assets/maa-framework',
                                                        output/'ocr',timeout=20))
        report['initial'] = _capture_idle(supervisor, reader, maa, foreground=None)
        report['maa_focus'] = _request_focus(supervisor, 'maa')
        report['before'] = _capture_idle(supervisor, reader, maa, foreground=True)
        report['presentation'] = _request_focus(supervisor, 'game', target='show_battle_game')
        report['visibility_before_capture'] = _visible_game(native,game,maa)
        report['after'] = _capture_idle(supervisor, reader, maa, foreground=False)
        report['visibility_after_capture'] = _visible_game(native,game,maa)
        _require(report['after']['screenshot_id'] != report['before']['screenshot_id']
                 and report['after']['code'] == report['before']['code'], 'Job changed or stale frame')
        report['passed'] = True
    except Exception as error:
        report['error'] = str(error)
    finally:
        if supervisor is not None:
            supervisor.close()
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['passed'] else 2

"""Bound MAA and game surfaces inside one supervised desktop writer.

This transport knows no task goal and grants no authority. Every input crosses
the parent's durable admission gate. Game input is navigation only; parent OCR
and vision must supply the measured forbidden regions of its source frame.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import hashlib
import math
import ntpath
from pathlib import Path
import re
import time

from .game_executor import compare_target_patch
from .maa_visual_native import MaaVisualNative, _NativeMaaBackend
from .windows import WindowController, WindowError, _geometry


class BattleTransportError(WindowError):
    def __init__(self, message, code='battle_transport_rejected'):
        super().__init__(message, code)


def validate_game_identity(value):
    if not isinstance(value, dict):
        raise ValueError('Game identity must be an object')
    if any(type(value.get(key)) is not int or value[key] <= 0
           for key in ('hwnd', 'pid', 'creation_time')):
        raise ValueError('Game identity requires positive HWND/PID/creation time')
    executable = value.get('executable')
    if (not isinstance(executable,str) or not ntpath.isabs(executable)
            or ntpath.basename(executable).casefold() != 'arknights.exe'):
        raise ValueError('Only an exact absolute Arknights.exe identity may be bound')
    return {key:value[key] for key in ('hwnd','pid','creation_time','executable')}


def _same_path(left, right):
    return isinstance(left,str) and ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right))


class _GameBackend:
    """Reuse existing guarded Win32 input, never a second helper or UIA."""
    supports_background_capture = True
    def __init__(self, identity):
        self.identity = identity
        self.native = _NativeMaaBackend()
        self.gate = lambda: (_ for _ in ()).throw(BattleTransportError('No input admission'))
        self.focus_result = {}

    def creation_time(self, pid):return self.native.creation_time(pid)
    def permission(self, pid):return self.native.permission(pid)

    def _check(self):
        info = self.get_window(self.identity['hwnd'])
        if info is None or self.creation_time(self.identity['pid']) != self.identity['creation_time']:
            raise BattleTransportError('Bound game process changed', 'game_binding_changed')
        if (info.pid != self.identity['pid'] or not _same_path(info.executable,self.identity['executable'])):
            raise BattleTransportError('Bound game executable changed', 'game_binding_changed')
        report = self.permission(info.pid)
        if report.get('known') is not True or report.get('input_allowed') is not True:
            raise BattleTransportError('Cannot confirm game input permission', 'game_integrity_mismatch')
        return info

    def get_window(self, hwnd):return self.native.get_window(hwnd)
    def capture_background(self, info, path):return self.native.capture(info,path)
    def capture(self, info, path):return self.native.capture(info,path)

    def restore_window(self, hwnd):
        # SW_SHOWNOACTIVATE displays the previous size without taking focus.
        self.native.native.user.ShowWindow(hwnd, 4)

    def presentation_state(self, game, maa):
        native = self.native.native
        user = native.user
        user.WindowFromPoint.argtypes, user.WindowFromPoint.restype = [w.POINT], w.HWND
        user.GetAncestor.argtypes, user.GetAncestor.restype = [w.HWND, w.UINT], w.HWND
        style = user.GetWindowLongPtrW
        style.argtypes, style.restype = [w.HWND, ctypes.c_int], ctypes.c_ssize_t
        left, top, width, height = _geometry(game)
        right, bottom = left + width, top + height
        with native._physical_pixels():
            # Probe the part the MAA window could actually cover, including its
            # caption and border. Without overlap, sample the game client.
            outer = w.RECT()
            if not user.GetWindowRect(maa.hwnd, ctypes.byref(outer)):
                raise BattleTransportError('Cannot read MAA window bounds', 'maa_geometry_unavailable')
            overlap = (max(left, outer.left), max(top, outer.top),
                       min(right, outer.right), min(bottom, outer.bottom))
            if not maa.minimized and overlap[0] < overlap[2] and overlap[1] < overlap[3]:
                left, top, right, bottom = overlap
            samples = []
            for fx, fy in ((.5, .5), (.2, .2), (.8, .2), (.2, .8), (.8, .8)):
                x = left + int((right - left - 1) * fx)
                y = top + int((bottom - top - 1) * fy)
                hit = user.WindowFromPoint(w.POINT(x, y))
                root = int(user.GetAncestor(hit, 2) or 0)
                owner = int(user.GetAncestor(hit, 3) or 0)
                samples.append({'x':x, 'y':y, 'root_hwnd':root, 'owner_hwnd':owner})
            return {'game_visible':all(p['root_hwnd'] == game.hwnd for p in samples),
                    'maa_obstructs_game':any(maa.hwnd in (p['root_hwnd'], p['owner_hwnd']) for p in samples),
                    'focus_acquired':user.GetForegroundWindow() == game.hwnd,
                    'maa_minimized':maa.minimized, 'maa_topmost':bool(style(maa.hwnd,-20) & 8),
                    'game_topmost':bool(style(game.hwnd,-20) & 8), 'sampled_points':samples}

    def place_maa_behind(self, game, maa):
        position = self.native.native.user.SetWindowPos
        position.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT]
        position.restype = w.BOOL
        # Both windows are already bound; change only their relative order.
        # NOMOVE | NOSIZE | NOACTIVATE | NOOWNERZORDER.
        return bool(position(maa.hwnd, game.hwnd, 0, 0, 0, 0, 0x0213))

    def focus_window(self, hwnd):
        self._check();self.gate();self._check()
        def guard():
            self._check();self.gate();self._check()
        from .game_foreground_activation import activate_game
        try:
            # Fullscreen Unity windows have no safe WPF caption. Use the
            # pinned SDK's window-only activation recipe; never click content.
            self.focus_result = activate_game(self.native.native,hwnd,guard)
        except WindowError as error:
            known=(error.code=='focus_denied' or error.code=='focus_activation_failed'
                   and error.details.get('input_sent') is False)
            if (not known or error.details.get('partial_input')
                    or error.details.get('input_sent')):
                raise
            # The window-only activation path explicitly proves no content
            # click was issued. Preserve a known failed focus result
            # instead of poisoning the desktop with an unknown game effect.
            self.focus_result = {'focus_acquired':False,'focus_method':'known_focus_denied',
                                 'caption_click':False}

    def click_guarded(self, x, y, window_guard):
        def guard():
            window_guard();self._check();self.gate();window_guard();self._check()
        # _NativeMaaBackend.click rechecks physical pointer/covering HWND after
        # the IPC guard and directly before the mouse-down event.
        return self.native.click(x,y,guard)

    def drag(self, x1, y1, x2, y2, duration, window_guard):
        calls = 0
        def guard():
            nonlocal calls
            window_guard();self._check();self.gate();window_guard();self._check()
            calls += 1
            if calls == 2:
                self.native.native._verify_pointer_target(x1,y1)
        return self.native.native.drag(x1,y1,x2,y2,duration,guard)


def _segment_hits_box(a,b,box):
    # Liang-Barsky clipping; covers the whole drag, not just its endpoints.
    low, high = 0.,1.
    for axis,lo,hi in ((0,box['x'],box['x']+box['width']),
                       (1,box['y'],box['y']+box['height'])):
        start, delta = a[axis], b[axis]-a[axis]
        if delta == 0:
            if not lo <= start <= hi:return False
            continue
        first,last = sorted(((lo-start)/delta,(hi-start)/delta))
        low, high = max(low,first), min(high,last)
        if low > high:return False
    return True


class MaaBattleHandler:
    """Child-side serial transport. Dependency injection is for offline tests."""
    SOURCE_MAX_AGE = 120.

    def __init__(self, identity, directory, *, maa_adapter=None, game_windows=None,
                 game_backend=None, game_input=None, patch_compare=None, clock=time.monotonic):
        identity = dict(identity)
        game = identity.pop('_game', None)
        self.navigation_mode=identity.pop('_navigation_mode','background')
        if self.navigation_mode not in {'background','foreground'}:
            raise ValueError('Unsupported game navigation transport')
        self.maa_identity = identity
        self.directory = Path(directory)
        self.maa = maa_adapter if maa_adapter is not None else MaaVisualNative(identity,self.directory)
        self.game_identity = validate_game_identity(game) if game is not None else None
        self.windows = self.game_backend = self.game_input = None
        self._last_game = None
        self._last_game_at = None
        self._last_maa = None
        self._last_maa_at = None
        self.clock, self.patch_compare = clock, patch_compare or compare_target_patch
        if self.game_identity is not None:
            self.game_backend = game_backend if game_backend is not None else _GameBackend(self.game_identity)
            self.windows = game_windows if game_windows is not None else WindowController(
                self.directory/'game-navigation',backend=self.game_backend,prefer_maafw=False)
            self._check_game()
            self.windows.bind(self.game_identity['hwnd'])
            if self.navigation_mode=='background':
                if game_input is None:
                    from .maa_background_input import MaaBackgroundInput
                    game_input=MaaBackgroundInput(self.game_identity,self.directory/'game-background-input',
                                                   check_window=self._background_window)
                self.game_input=game_input
            elif game_input is not None:
                raise ValueError('Foreground navigation cannot use a background SDK adapter')

    def _check_game(self, *, input_required=False):
        if self.windows is None:
            raise BattleTransportError('Game is not bound; no automatic rebinding', 'game_unavailable')
        bound = self.game_identity
        try:
            info = self.game_backend.get_window(bound['hwnd'])
            if (info is None or (info.hwnd,info.pid) != (bound['hwnd'],bound['pid'])
                    or not _same_path(info.executable,bound['executable'])
                    or self.game_backend.creation_time(bound['pid']) != bound['creation_time']):
                raise BattleTransportError('Game process/window identity changed', 'game_binding_changed')
            if input_required:
                permission = self.game_backend.permission(bound['pid'])
                if permission.get('known') is not True or permission.get('input_allowed') is not True:
                    raise BattleTransportError('Game input permission unavailable', 'game_integrity_mismatch')
            return info
        except BattleTransportError:raise
        except (OSError,RuntimeError,ValueError) as error:
            raise BattleTransportError('Cannot verify bound game identity', 'game_binding_changed') from error

    def _snapshot_game(self):
        info = self._check_game()
        observation = self.windows.capture(background_only=True)
        after = self._check_game()
        if _geometry(info) != _geometry(after):
            raise BattleTransportError('Game geometry changed during capture', 'game_geometry_changed')
        raw = Path(observation['path']).read_bytes()
        frame = {**observation,'image_path':observation['path'], 'screenshot_id':observation['id'],
            'image_sha256':hashlib.sha256(raw).hexdigest(),
            'creation_time':self.game_identity['creation_time'],
            'window':{**self.game_identity,'client_rect':observation['client_rect'],
                      'foreground':after.foreground,'minimized':after.minimized},
            'recognition_verified':False,'verification_required':True,'surface':'game'}
        self._last_game,self._last_game_at = frame,self.clock()
        return frame

    def _background_window(self):
        """Raw client identity for the fixed SDK, without requiring foreground."""
        info=self._check_game(input_required=True)
        if info.minimized or min(info.width,info.height)<=1:
            raise BattleTransportError('Background navigation requires a non-minimized valid client',
                                       'game_geometry_changed')
        return {**self.game_identity,'client_rect':dict(zip(('left','top','width','height'),_geometry(info))),
                'minimized':info.minimized}

    def _focus_game(self, gate):
        self._last_game = None
        self._check_game(input_required=True)
        self.game_backend.gate = gate
        # Activation is an admitted effect. A denied foreground request is a
        # known no-content-input result, not an unknown submitted game click.
        gate();self._check_game(input_required=True)
        self.game_backend.focus_result={}
        self.game_backend.focus_window(self.game_identity['hwnd'])
        info=self._check_game(input_required=True)
        details=getattr(self.game_backend,'focus_result',{})
        return {**details,'ok':True,'action':'focus','surface':'game',
                'focus_acquired':bool(info.foreground and not info.minimized),
                'window':{**self.game_identity,'client_rect':{'left':info.left,'top':info.top,
                    'width':info.width,'height':info.height}}}

    def _present_game_after_start(self, gate):
        """Move the bound game in front while leaving MAA available to PrintWindow."""
        self._focus_game(gate)

        def windows():
            return self._check_game(input_required=True), self.maa._current(permit_minimized=True)

        game, maa = windows()
        state = self.game_backend.presentation_state(game, maa)
        if state['maa_obstructs_game'] and not (state['maa_topmost'] or state['game_topmost']):
            # A denied activation may leave MAA above the game. Lower only
            # these normal-band windows; keep any user's topmost preference.
            gate()
            game, maa = windows()
            if not maa.minimized:
                self.maa._permission(required=True)
                self.game_backend.place_maa_behind(game, maa)
            game, maa = windows()
            state = self.game_backend.presentation_state(game, maa)
        if not state['game_visible']:
            state['reason'] = ('maa_topmost_occlusion' if state['maa_obstructs_game'] and state['maa_topmost']
                               else 'game_window_occluded')
        return state

    def _foreground_window(self):
        info=self._check_game(input_required=True)
        if info.minimized or not info.foreground or min(info.width,info.height)<=1:
            raise BattleTransportError('Physical navigation requires the bound game in foreground',
                                       'game_not_foreground')
        return info

    def _foreground_act(self,action,points,duration,source,fresh,decision,gate):
        """Reuse the existing physical pointer, overlay and local-stop checks."""
        geometry=fresh['window']['client_rect']
        self._foreground_window()
        final=self._snapshot_game();self._last_game=None
        if (final['window']['client_rect']!=geometry or not self.patch_compare(fresh,final,decision)):
            raise BattleTransportError('Foreground navigation target changed before admission','game_target_changed')
        admitted_at=self.clock()
        def guarded_admission():
            def check():
                info=self._foreground_window()
                if dict(zip(('left','top','width','height'),_geometry(info)))!=geometry:
                    raise BattleTransportError('Game moved before foreground input','game_geometry_changed')
                age=self.clock()-admitted_at
                if not 0<=age<=5:
                    raise BattleTransportError('Fresh foreground observation expired','game_observation_stale')
            check();gate();check()
        # Admit before moving the physical cursor as well as at every native
        # button/drag boundary. Native click rechecks occlusion after this IPC.
        guarded_admission()
        self.game_backend.gate=guarded_admission
        self.windows.set_input_enabled(True)
        try:
            if action=='click':
                result=self.windows.controlled_click(*points[0],final['screenshot_id'])
            else:
                result=self.windows.controlled_drag(*points[0],*points[1],duration,final['screenshot_id'])
        finally:
            self.windows.set_input_enabled(False)
            self._last_game=None
        return {**result,'method':'win32_foreground_guarded','input_dispatched':True,
                'effect_verified':False,'surface':'game','source_screenshot_id':source['screenshot_id'],
                'fresh_screenshot_id':final['screenshot_id'],'creation_time':self.game_identity['creation_time']}

    @staticmethod
    def _regions(args, frame, points):
        regions = args.get('forbidden_regions')
        if not isinstance(regions,list) or len(regions)>64:
            raise BattleTransportError('Navigation requires explicit measured forbidden regions')
        for region in regions:
            box = region.get('rect') if isinstance(region,dict) else None
            if (not isinstance(box,dict) or any(type(box.get(k)) not in (int,float)
                    or not math.isfinite(box[k]) for k in ('x','y','width','height'))
                    or box['x']<0 or box['y']<0 or box['width']<=0 or box['height']<=0
                    or box['x']+box['width']>frame['width'] or box['y']+box['height']>frame['height']):
                raise BattleTransportError('Invalid forbidden region')
            if _segment_hits_box(points[0],points[-1],box):
                raise BattleTransportError('Navigation cannot touch game battle-start region','game_start_forbidden')

    def _game_act(self,args,gate):
        self._check_game(input_required=True)
        action = args.get('action')
        if action == 'restore':
            self._last_game = None
            gate()
            info = self._check_game(input_required=True)
            if info.minimized:
                self.game_backend.restore_window(info.hwnd)
            after = self._check_game(input_required=True)
            if after.minimized or min(after.width, after.height) <= 1:
                raise BattleTransportError('游戏窗口恢复后仍不可截图。', 'game_restore_unverified')
            return {'ok': True, 'restored': True, 'surface': 'game'}
        if action == 'focus':
            if args.get('target') == 'show_battle_game':
                return self._present_game_after_start(gate)
            return self._focus_game(gate)
        if action not in {'click','drag'}:
            raise BattleTransportError('Only bounded navigation clicks/drags are available')
        label = str(args.get('target',''))
        if re.search(r'开始行动|开始作战|开始战斗|battle[_ -]?start|start[_ -]?(?:action|battle)',label,re.I):
            raise BattleTransportError('Game battle-start is never a navigation target','game_start_forbidden')
        source,self._last_game = self._last_game,None
        if (not source or args.get('screenshot_id') != source['screenshot_id']
                or self._last_game_at is None or not 0<=self.clock()-self._last_game_at<=self.SOURCE_MAX_AGE):
            raise BattleTransportError('Navigation source screenshot is missing, stale or consumed','game_observation_stale')
        points=[(args.get('x'),args.get('y'))]
        if action=='drag':points.append((args.get('to_x'),args.get('to_y')))
        if any(type(v) is not int for point in points for v in point) or any(
                not 0<=x<source['width'] or not 0<=y<source['height'] for x,y in points):
            raise BattleTransportError('Navigation coordinates must be physical client integers')
        duration=args.get('duration_seconds')
        if action=='drag' and (type(duration) not in (int,float) or not math.isfinite(duration)
                               or not .15<=duration<=1.5):
            raise BattleTransportError('Navigation drag duration must be between .15 and 1.5 seconds')
        self._regions(args,source,points)
        fresh = self._snapshot_game()
        self._last_game = None
        # Foreground/Z-order may change while the model is thinking. It does
        # not change HWND-specific PrintWindow pixels or raw client coordinates.
        if (source['window']['client_rect'] != fresh['window']['client_rect']
                or source['creation_time'] != fresh['creation_time']):
            raise BattleTransportError('Game layout or identity changed after model observation','game_geometry_changed')
        decision={'kind':action,'x':points[0][0]/(source['width']-1),
                  'y':points[0][1]/(source['height']-1)}
        if action=='drag':decision.update(to_x=points[1][0]/(source['width']-1),to_y=points[1][1]/(source['height']-1))
        if not self.patch_compare(source,fresh,decision):
            raise BattleTransportError('Navigation target pixels changed; reobserve before input','game_target_changed')
        if self.navigation_mode=='foreground':
            return self._foreground_act(action,points,duration,source,fresh,decision,gate)
        geometry=fresh['window']['client_rect'];final=fresh
        def dispatch_guard():
            nonlocal final
            current=self._background_window()
            if current['client_rect']!=geometry:
                raise BattleTransportError('Game geometry changed before background input','game_geometry_changed')
            # SDK connection/capture may take time. Recheck the target after
            # those read-only operations, directly before durable admission.
            try:final=self._snapshot_game()
            finally:self._last_game=None
            if (final['window']['client_rect']!=geometry
                    or not self.patch_compare(fresh,final,decision)):
                raise BattleTransportError('Navigation target changed before background input','game_target_changed')
            gate()
            if self._background_window()['client_rect']!=geometry:
                raise BattleTransportError('Game moved at background admission','game_geometry_changed')
        result=self.game_input.act(action,points,duration_seconds=duration if action=='drag' else None,
                                   gate=dispatch_guard,expected_geometry=geometry)
        if not isinstance(result,dict) or result.get('input_dispatched') is not True:
            raise BattleTransportError('Background SDK did not confirm input dispatch','game_input_unverified')
        return {**result,'surface':'game','source_screenshot_id':source['screenshot_id'],
                'fresh_screenshot_id':final['screenshot_id'],'creation_time':self.game_identity['creation_time'],
                'state':'unverified','verification_required':True,'effect_verified':False}

    def __call__(self, command, enter_submission):
        args = command.get('args',{})
        surface = args.get('surface','maa')
        if surface not in {'maa','game'}:
            raise BattleTransportError('Unknown bound surface')
        if command.get('stop_only') and not (command.get('op')=='act' and surface=='maa'
                and ((args.get('action')=='click' and args.get('target')=='battle_stop')
                     or (args.get('action')=='focus' and args.get('target')=='battle_stop_focus'))):
            raise BattleTransportError('Stop-only can operate only a fresh MAA stop target')
        if command.get('op')=='snapshot':
            if surface=='game':return self._snapshot_game()
            self._last_maa=self.maa.snapshot()
            self._last_maa_at=self.clock()
            return self._last_maa
        if command.get('op')!='act':raise BattleTransportError('Unknown command')
        if surface=='game':return self._game_act(args,enter_submission)
        action=args.get('action')
        if action=='focus':
            self._last_maa=None
            return self.maa.focus(guard=enter_submission)
        if action!='click' or args.get('target') not in {'battle_start','battle_stop','navigation_option'}:
            raise BattleTransportError('Unregistered battle MAA action')
        source,self._last_maa=self._last_maa,None
        if (not source or source.get('screenshot_id')!=args.get('screenshot_id')
                or self._last_maa_at is None or not 0<=self.clock()-self._last_maa_at<=8):
            raise BattleTransportError('MAA battle input requires a fresh unconsumed screenshot','maa_battle_observation_stale')
        result = self.maa.click_battle(args.get('x'),args.get('y'),args.get('screenshot_id'),
                                      target=args['target'],guard=enter_submission)
        if args['target'] == 'battle_start':
            # Both native button events are already delivered. A window-only
            # handoff has no authority to change that receipt or replay Start.
            try:
                presentation = self._present_game_after_start(enter_submission)
            except Exception as error:
                presentation = {'game_visible':False, 'reason':getattr(error, 'code', type(error).__name__)}
            result = {**result, 'game_presentation':presentation}
        return result

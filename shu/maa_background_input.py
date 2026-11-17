"""MaaFramework AnchoredTouch for one already-admitted game action.

This is an SDK input adapter, not a gameplay agent or a second GUI worker. Its
caller supplies measured client coordinates and enforces target-pixel and
forbidden-region checks. The SDK completion only proves message dispatch; the
caller must observe the game again to verify the intended transition.

Fixed reference: MaaXYZ/MaaFramework v5.15.0, ControllerAgent.cpp
(`preproc_touch_point`, `calc_target_image_size`) and Input/AnchoredTouchInput.cpp.
The SDK delivers a Windows synthetic touch to the bound game window.
"""
from __future__ import annotations

import math
import ntpath
from pathlib import Path
import sys
import time

from .windows import WindowError


_QUARANTINE = []  # Unfinished native handles must not block helper teardown.


class MaaBackgroundInputError(WindowError):
    def __init__(self, code, *, input_sent=False):
        super().__init__(code, code, {'input_sent':bool(input_sent),
            'partial_input':bool(input_sent), 'effect_verified':False})


def _runtime_path():
    bundle = getattr(sys, '_MEIPASS', None)
    if bundle:
        return Path(bundle)/'optional-runtime'/'5.15.0'
    return Path(__file__).resolve().parents[2]/'.maafw-runtime'/'5.15.0'


def _controller(runtime, log_directory, **options):
    """Import only the pinned optional runtime, without a global input fallback."""
    runtime = Path(runtime).resolve()
    if not (runtime/'maa').is_dir():
        raise MaaBackgroundInputError('background_framework_unavailable')
    if str(runtime) not in sys.path:
        sys.path.insert(0,str(runtime))
    from maa.library import Library
    from maa.toolkit import Toolkit
    from maa.controller import Win32Controller
    if Library.version() != 'v5.15.0':
        raise MaaBackgroundInputError('background_framework_version_mismatch')
    Path(log_directory).mkdir(parents=True,exist_ok=True)
    if not Toolkit.init_option(Path(log_directory), {}):
        raise MaaBackgroundInputError('background_framework_init_failed')
    return Win32Controller(**options)


class MaaBackgroundInput:
    """Bound, serial SDK adapter; use exclusively inside GuiSupervisor helper.

    ``check_window`` must independently verify the bound process identity and
    integrity permissions and return physical client geometry. It is called
    before initialization, after screenshot, immediately after the durable
    ``gate``, and after SDK window restoration. No foreground requirement is
    imposed, and a minimized window is never restored silently.
    """
    _mouse_method = 1024  # MaaWin32InputMethod_AnchoredTouch
    _input_method = 'maafw_anchored_touch'

    def __init__(self, identity, log_directory, *, runtime=None,
                 controller_factory=None, check_window, timeout=8.,
                 clock=time.monotonic, sleep=time.sleep):
        if not isinstance(identity,dict) or any(type(identity.get(k)) is not int or identity[k]<=0
                for k in ('hwnd','pid','creation_time')):
            raise ValueError('Invalid bound game identity')
        executable=identity.get('executable')
        if not self._allows_executable(executable):
            raise ValueError('Unsupported background input executable')
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not .1<=timeout<=8:
            raise ValueError('Invalid background timeout')
        self.identity=dict(identity)
        self.log_directory=Path(log_directory)
        self.runtime=Path(runtime) if runtime is not None else _runtime_path()
        self.factory=controller_factory
        self.check_window=check_window
        self.timeout,self.clock,self.sleep=timeout,clock,sleep
        self._controller=None
        self._pending=None
        self._poisoned=False

    @staticmethod
    def _allows_executable(path):
        return (isinstance(path,str) and ntpath.isabs(path)
                and ntpath.basename(path).casefold()=='arknights.exe')

    @staticmethod
    def _fail(code,sent=False):
        raise MaaBackgroundInputError(code,input_sent=sent)

    def _check(self,cancel,deadline,sent=False):
        if cancel is not None and cancel.is_set():self._fail('background_cancelled',sent)
        if self.clock()>=deadline:self._fail('background_timeout',sent)

    def _window(self,expected,sent=False):
        info=self.check_window()
        if (not isinstance(info,dict) or any(info.get(k)!=self.identity[k]
                for k in ('hwnd','pid','creation_time'))
                or not isinstance(info.get('executable'),str)
                or ntpath.normcase(ntpath.normpath(info['executable']))!=
                   ntpath.normcase(ntpath.normpath(self.identity['executable']))):
            self._fail('background_binding_changed',sent)
        if info.get('minimized') is not False:self._fail('background_window_minimized',sent)
        if info.get('client_rect')!=expected:self._fail('background_geometry_changed',sent)

    def _wait(self,job,cancel,deadline,sent=False):
        self._pending=job
        while not job.done:
            self._check(cancel,deadline,sent)
            self.sleep(.01)
        self._check(cancel,deadline,sent)
        self._pending=None
        if not job.succeeded:self._fail('background_job_failed',sent)
        return job

    def _initialize(self,cancel,deadline):
        if self._controller is not None:return self._controller
        options=dict(hWnd=self.identity['hwnd'],screencap_method=16,
                     mouse_method=self._mouse_method,keyboard_method=2)
        controller=(self.factory(**options) if self.factory is not None else
                    _controller(self.runtime,self.log_directory,**options))
        self._controller=controller
        # Native coordinate preprocessing uses raw/target dimensions. Without
        # this option the SDK defaults to a scaled screenshot coordinate space.
        if not controller.set_screenshot_use_raw_size(True):
            self._fail('background_raw_size_unavailable')
        self._wait(controller.post_connection(),cancel,deadline)
        return controller

    def act(self,action,points,*,duration_seconds=None,gate,
            expected_geometry,cancel_event=None):
        if self._poisoned:self._fail('background_transport_quarantined')
        if (not isinstance(expected_geometry,dict)
                or set(expected_geometry)!=set(('left','top','width','height'))
                or any(type(v) is not int for v in expected_geometry.values())
                or not 1<=expected_geometry['width']<=16384
                or not 1<=expected_geometry['height']<=16384):
            self._fail('background_geometry_invalid')
        if (action not in ('click','drag') or not isinstance(points,(list,tuple))
                or len(points)!=(1 if action=='click' else 2)
                or any(not isinstance(p,(list,tuple)) or len(p)!=2
                    or any(type(v) is not int for v in p)
                    or not 0<=p[0]<expected_geometry['width']
                    or not 0<=p[1]<expected_geometry['height'] for p in points)):
            self._fail('background_coordinates_invalid')
        if action=='drag' and (type(duration_seconds) not in (int,float)
                or not math.isfinite(duration_seconds) or not .05<=duration_seconds<=2.):
            self._fail('background_duration_invalid')
        deadline=self.clock()+self.timeout
        sent=False; input_job=None; restored=False
        try:
            self._check(cancel_event,deadline)
            self._window(expected_geometry)
            controller=self._initialize(cancel_event,deadline)
            # Seed and verify the SDK's own raw coordinate metadata. This is
            # not visual recognition and never grants permission to click.
            image=self._wait(controller.post_screencap(),cancel_event,deadline).get()
            expected_size=(expected_geometry['width'],expected_geometry['height'])
            if (image is None or len(getattr(image,'shape',()))<2
                    or tuple(image.shape[:2])!=expected_size[::-1]
                    or tuple(controller.resolution)!=expected_size):
                self._fail('background_capture_geometry')
            self._window(expected_geometry)
            self._check(cancel_event,deadline)
            gate()  # Includes the parent's fresh pixels and durable admission.
            self._window(expected_geometry)
            self._check(cancel_event,deadline)
            # The asynchronous SDK enqueue is the conservative effect boundary.
            # Failure or cancellation beyond it must never be replayed as if no
            # input occurred, even if the SDK reports a failed job.
            sent=True
            if action=='click':input_job=controller.post_click(*points[0])
            else:
                input_job=controller.post_swipe(*points[0],*points[1],
                    int(round(duration_seconds*1000)))
            try:
                self._wait(input_job,cancel_event,deadline,True)
            finally:
                # SDK inactive releases any per-controller window state.
                # Do not cancel this
                # bounded cleanup; it neither starts a new navigation action
                # nor moves the cursor. A hung input remains quarantined.
                if input_job.done:
                    self._wait(controller.post_inactive(),None,deadline,True)
                    restored=True
            self._window(expected_geometry,True)
            return {'ok':True,'action':action,'input_dispatched':True,
                'effect_verified':False,'verification_required':True,
                'input_method':self._input_method,
                'screencap_method':'PrintWindow','framework_version':'v5.15.0',
                'coordinates':'physical_client','window_restored':restored}
        except Exception:
            if sent or (self._pending is not None and not self._pending.done):
                self._poisoned=True
                _QUARANTINE.append((self._controller,self._pending,input_job))
            else:
                # An unsuccessful raw-size/connection setup is not a reusable
                # controller. A later action must repeat all initialization.
                self._controller=self._pending=None
            raise


class MaaClientButtonInput(MaaBackgroundInput):
    """Plain mouse messages for the installed MAA WPF client only.

    MaaFramework v5.15.0 MaaDef.h documents that AnchoredTouch cannot raise
    UpdateLayeredWindow-based windows. The real MAA button attempt failed
    SetLayeredWindowAttributes (87) before touch injection. MessageInput's
    plain PostMessage path sends client-coordinate mouse move/down/up messages
    without layered styles, window repositioning or moving the physical mouse.
    PostMessage also avoids blocking the SDK on the target's WndProc. It still
    proves only dispatch; the caller must observe MAA's running/idle state.
    No alternate transport is tried after a failed or uncertain input.
    """
    _mouse_method = 4  # MaaWin32InputMethod_PostMessage, no With*Pos flags
    _input_method = 'maafw_post_message'

    @staticmethod
    def _allows_executable(path):
        from .maa_visual_native import MAA_EXECUTABLE, _same_path
        return _same_path(path, MAA_EXECUTABLE)

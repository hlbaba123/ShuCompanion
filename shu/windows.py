"""Target-window Windows observations and explicitly enabled, guarded input.

Coordinates always refer to the *client content* in physical pixels. A screenshot
is evidence of pixels, not recognition that the game has reached a known state.
Only an explicit focus_for_capture request restores/activates the bound window.
Capturing never clicks game content, launches a process, or reads game memory.

Optional capture API: https://github.com/MaaXYZ/MaaFramework/tree/main/source/binding/Python/maa
Native geometry: https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-getclientrect
Native capture: https://learn.microsoft.com/windows/win32/api/wingdi/nf-wingdi-bitblt
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib.util
import math
import os
from pathlib import Path
import struct
import sys
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any
import uuid
import zlib


class WindowError(RuntimeError):
    """An actionable, short message suitable for the desktop UI."""

    def __init__(self, message: str, code: str = "window_error", details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    left: int
    top: int
    width: int
    height: int
    foreground: bool
    minimized: bool
    executable: str = ""
    window_class: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_game_client(self) -> bool:
        executable = self.executable.replace('\\', '/').rsplit('/', 1)[-1].casefold()
        return executable == 'arknights.exe' or (self.window_class == 'UnityWndClass'
                                                and self.title.strip().casefold() in {'明日方舟', 'arknights'})


def _maa_available() -> bool:
    try:
        return importlib.util.find_spec("maa") is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _wgc_available() -> bool:
    try:
        return importlib.util.find_spec("windows_capture") is not None
    except (ImportError, ValueError, AttributeError):
        return False


def discover_capabilities() -> dict[str, Any]:
    windows = sys.platform == "win32"
    maa = _maa_available()
    wgc = windows and _wgc_available()
    return {
        "os": sys.platform,
        "windows": windows,
        "window_enumeration": windows,
        "foreground_capture": windows,
        "capture_methods": (["windows_graphics_capture"] if wgc else [])
        + (["maafw_framepool"] if windows and maa else [])
        + (["win32_printwindow", "win32_gdi_foreground"] if windows else []),
        "maafw_installed": maa,
        "maafw_framepool_available": windows and maa,
        "maafw_framepool_verified": False,
        "client_compatibility_verified": False,
        "input_default_enabled": False,
        "requires_foreground": False if wgc else True,
        "wgc_available": wgc,
        "background_capture_available": wgc,
        "background_capture_verified": False,
        "foreground_fallback_requires_foreground": True,
        "game_recognition": False,
        "warnings": ([] if maa else ["MaaFramework 未安装，尚未验证 FramePool 捕获。"])
        + ([] if windows else ["本机不是 Windows，原生窗口捕获与输入不可用。"]),
    }


def _geometry(window: WindowInfo) -> tuple[int, int, int, int]:
    return window.left, window.top, window.width, window.height


def _crop_bgra(pixels: bytes, frame_width: int, frame_height: int,
               left: int, top: int, width: int, height: int) -> bytes:
    """Keep client coordinates exact when WGC includes the non-client frame."""
    if (len(pixels) != frame_width * frame_height * 4 or min(left, top) < 0
            or min(width, height) <= 0 or left + width > frame_width or top + height > frame_height):
        raise WindowError("窗口帧与客户区边界不一致，请重新绑定。", "wgc_client_bounds")
    if (left, top, width, height) == (0, 0, frame_width, frame_height):
        return pixels
    return b"".join(pixels[((top+y)*frame_width+left)*4:((top+y)*frame_width+left+width)*4]
                    for y in range(height))


class _WgcCapture:
    """An HWND-specific WGC session. No desktop crop or foreground activation."""

    def capture(self, info: WindowInfo, path: Path, crop_geometry: Any) -> dict[str, Any]:
        try:
            from windows_capture import WindowsCapture
        except (ImportError, OSError) as exc:
            raise WindowError("后台捕获组件未能加载，请使用完整解压的发行包。", "wgc_unavailable") from exc
        done = threading.Event()
        captured: dict[str, Any] = {}
        errors: list[Exception] = []
        control = None
        try:
            # Keep draw_border at the Windows default. Disabling it can require
            # an extra capture permission and is unnecessary for observations.
            session = WindowsCapture(window_hwnd=info.hwnd, cursor_capture=False, draw_border=None)

            @session.event
            def on_frame_arrived(frame, capture_control):
                try:
                    # The ndarray is a native mapped view. Copy it in this callback.
                    pixels = frame.frame_buffer.tobytes()
                    stride = max(4, (len(pixels)//4096//4)*4)
                    sample = b"".join(pixels[c::stride] for c in range(3))
                    if not sample or max(sample) < 5:
                        return  # Initial/protected black frames never count as a screenshot.
                    captured.update(pixels=pixels, width=int(frame.width), height=int(frame.height))
                except Exception as exc:
                    errors.append(exc)
                capture_control.stop()
                done.set()

            @session.event
            def on_closed():
                done.set()

            control = session.start_free_threaded()
            if not done.wait(8.0):
                raise WindowError("后台捕获未返回有效画面。游戏需保持打开且未最小化。", "wgc_timeout")
            control.wait()
            control = None
            if errors:
                raise WindowError("后台窗口帧读取失败，请重新观察。", "wgc_frame_failed") from errors[0]
            if not captured:
                raise WindowError("游戏窗口在后台截图完成前关闭。", "window_missing")
            left, top = crop_geometry(info, captured['width'], captured['height'])
            pixels = _crop_bgra(captured['pixels'], captured['width'], captured['height'],
                                left, top, info.width, info.height)
            path.write_bytes(_encode_png(info.width, info.height, pixels))
            return {"method": "windows_graphics_capture", "width": info.width, "height": info.height,
                    "requires_foreground": False, "background_capture": True,
                    "warnings": [], "source_frame_size": [captured['width'], captured['height']],
                    "client_crop": {"left": left, "top": top}}
        except WindowError:
            raise
        except Exception as exc:
            raise WindowError("Windows 后台捕获会话创建失败。请正常启动发行包，沙箱或系统策略可能限制窗口捕获。",
                              "wgc_session_failed") from exc
        finally:
            if control is not None:
                try:
                    control.stop()
                except Exception:
                    pass


def _encode_png(width: int, height: int, pixels: bytes, channels: int = 4) -> bytes:
    """Encode top-down BGR(A) pixels without Qt/Pillow or a GUI event loop."""
    if width <= 0 or height <= 0 or channels not in (3, 4):
        raise WindowError("截图尺寸无效。", "invalid_image")
    if len(pixels) != width * height * channels:
        raise WindowError("截图像素不完整。", "invalid_image")
    rgb = bytearray(width * height * 3)
    rgb[0::3] = pixels[2::channels]
    rgb[1::3] = pixels[1::channels]
    rgb[2::3] = pixels[0::channels]
    stride = width * 3
    rows = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


class _MaaFramePool:
    """Optional real MaaFramework capture, never its input or task APIs."""

    def __init__(self, hwnd: int):
        from maa.controller import Win32Controller
        from maa.define import MaaWin32InputMethodEnum, MaaWin32ScreencapMethodEnum

        self.controller = Win32Controller(
            hwnd,
            screencap_method=MaaWin32ScreencapMethodEnum.FramePool,
            mouse_method=MaaWin32InputMethodEnum.Seize,
            keyboard_method=MaaWin32InputMethodEnum.Seize,
        )
        self._wait(self.controller.post_connection())

    @staticmethod
    def _wait(job: Any, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not job.done:
            if time.monotonic() >= deadline:
                raise WindowError("FramePool 截图超时。", "maafw_timeout")
            time.sleep(0.025)
        if not job.succeeded:
            raise WindowError("FramePool 捕获失败。", "maafw_failed")

    def capture(self, info: WindowInfo, path: Path) -> dict[str, Any]:
        job = self.controller.post_screencap()
        self._wait(job)
        frame = job.get(wait=False)
        if frame is None or len(frame.shape) != 3 or frame.shape[2] not in (3, 4):
            raise WindowError("FramePool 没有返回有效画面。", "maafw_frame")
        height, width, channels = map(int, frame.shape)
        # Never silently mix Maa's possibly scaled coordinates with client pixels.
        if (width, height) != (info.width, info.height):
            raise WindowError("FramePool 画面尺寸与客户区不符，改用前台捕获。", "maafw_scaled")
        path.write_bytes(_encode_png(width, height, frame.tobytes(order="C"), channels))
        return {"method": "maafw_framepool", "warnings": [], "width": width, "height": height}


class WindowController:
    """Bound-window observations with a separate, default-off input permission.

    ``backend`` and clocks are injection points for non-Windows tests. Production
    callers only need ``data_dir``. Each successful click consumes its observation:
    another action must be preceded by a new screenshot and game-state validation.
    """

    OBSERVATION_MAX_AGE = 5.0

    def __init__(self, data_dir: Path, *, backend: Any = None,
                 clock: Any = None, monotonic: Any = None,
                 prefer_maafw: bool = True, observation_max_age: float = 5.0):
        self.data_dir = Path(data_dir)
        self.capture_dir = self.data_dir / "captures"
        self._backend = backend
        self._clock = clock or time.time
        self._monotonic = monotonic or time.monotonic
        self._prefer_maafw = prefer_maafw
        self._max_age = float(observation_max_age)
        if not math.isfinite(self._max_age) or not 0 < self._max_age <= 30:
            raise ValueError("observation_max_age must be in (0, 30]")
        self._bound: tuple[int, int] | None = None
        self._latest: dict[str, Any] | None = None
        self._latest_at = 0.0
        self._latest_info: WindowInfo | None = None
        self._input_enabled = False
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._capture_lock = threading.Lock()
        self._binding_revision = 0
        self._maa: _MaaFramePool | None = None
        self._maa_failure: str | None = None
        self._maa_verified = False
        self._background_verified = False

    def _native(self) -> Any:
        if self._backend is None:
            if sys.platform != "win32":
                raise WindowError("此功能需要 Windows。", "unsupported_os")
            self._backend = _NativeWindows()
        return self._backend

    def list_windows(self, game_only: bool = True) -> list[WindowInfo]:
        windows = self._native().list_windows()
        if game_only:
            real_clients = [window for window in windows if window.is_game_client]
            windows = real_clients or [w for w in windows if "明日方舟" in w.title or "arknights" in w.title.casefold()]
        return windows

    def bind(self, hwnd: int) -> WindowInfo:
        if isinstance(hwnd, bool) or not isinstance(hwnd, int) or hwnd <= 0:
            raise WindowError("请选择有效窗口。", "invalid_window")
        self.stop_capture_session()
        with self._lock:
            info = self._native().get_window(hwnd)
            if info is None:
                raise WindowError("窗口已关闭，请重新选择。", "window_missing")
            self._bound = info.hwnd, info.pid
            self._binding_revision += 1
            self._latest = self._latest_info = None
            self._maa = None
            self._maa_failure = None
            self._maa_verified = False
            self._background_verified = False
            self._input_enabled = False
            return info

    def bound_window(self) -> WindowInfo | None:
        with self._lock:
            if self._bound is None:
                return None
            info = self._native().get_window(self._bound[0])
            if info is None or (info.hwnd, info.pid) != self._bound:
                self._latest = self._latest_info = None
                self._input_enabled = False
                return None
            return info

    def _require_window(self, require_foreground: bool = True) -> WindowInfo:
        info = self.bound_window()
        if info is None:
            raise WindowError("请重新绑定游戏窗口。", "window_missing")
        if info.minimized:
            raise WindowError("游戏窗口已最小化，请恢复后重拍。", "minimized")
        if require_foreground and not info.foreground:
            raise WindowError("请切回绑定的游戏窗口，再重新截图。", "not_foreground")
        if info.width <= 0 or info.height <= 0:
            raise WindowError("游戏客户区尺寸无效。", "invalid_geometry")
        return info

    def focus_for_capture(self) -> WindowInfo:
        """Bring the bound game forward after an explicit capture request.

        This restores/activates the selected HWND only; it never presses a key,
        clicks game content, or enables input. Call on a worker, not the Qt thread.
        """
        info = self.bound_window()
        if info is None:
            raise WindowError("请先绑定已打开的游戏窗口。", "window_missing")
        if not info.foreground or info.minimized:
            activate = getattr(self._native(), "focus_window", None)
            if activate is None:
                raise WindowError("请切回绑定的游戏窗口后重试。", "not_foreground")
            activate(info.hwnd)
        return self._require_window()

    def capture(self, *, background_only: bool = False) -> dict[str, Any]:
        # Native rendering can block in the target process. Do not hold the state
        # lock across it: stop, rebind and UI status must remain responsive.
        if not self._capture_lock.acquire(blocking=False):
            raise WindowError("上一次截图仍在处理中，请稍后重试。", "capture_in_progress")
        try:
            return self._capture_once(background_only=background_only)
        finally:
            self._capture_lock.release()

    def _capture_once(self, *, background_only: bool = False) -> dict[str, Any]:
        with self._lock:
            # An unsuccessful new capture must not leave older evidence actionable.
            self._latest = self._latest_info = None
            capture_in_background = bool(getattr(self._native(), "supports_background_capture", False))
            if background_only and not capture_in_background:
                raise WindowError("此截图组件不支持后台窗口捕获。", "background_capture_unavailable")
            info = self._require_window(require_foreground=not capture_in_background)
            revision = self._binding_revision
        # No OS input is sent anywhere in this block.
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        observation_id = uuid.uuid4().hex
        path = self.capture_dir / f"{observation_id}.png"
        warnings: list[str] = []
        captured_at = self._clock()
        capture_started = self._monotonic()
        try:
            result = None
            if background_only and hasattr(self._native(), "capture_background"):
                result = self._native().capture_background(info, path)
            elif self._prefer_maafw and _maa_available() and self._maa_failure is None:
                try:
                    if self._maa is None:
                        self._maa = _MaaFramePool(info.hwnd)
                    result = self._maa.capture(info, path)
                    self._maa_verified = True
                except Exception as exc:
                    # Do not include arbitrary native/library messages in UI/logs.
                    self._maa_failure = str(exc) if isinstance(exc, WindowError) else "FramePool 初始化或捕获失败。"
                    self._maa = None
                    self._maa_verified = False
            if result is None:
                if self._prefer_maafw:
                    warnings.append(self._maa_failure or "MaaFramework 未安装，使用前台可见区域捕获。")
                # Recheck after an optional slow FramePool attempt.
                current = self._require_window(require_foreground=not capture_in_background)
                if _geometry(current) != _geometry(info):
                    raise WindowError("窗口尺寸或位置已变化，请重新截图。", "geometry_changed")
                result = self._native().capture(info, path)
            if background_only and result.get('requires_foreground', False):
                raise WindowError("已拒绝将前台屏幕裁剪当成后台截图。", "background_capture_unavailable")
            after = self._require_window(require_foreground=bool(result.get("requires_foreground", not capture_in_background)))
            if _geometry(after) != _geometry(info):
                raise WindowError("截图期间窗口发生变化，请重新截图。", "geometry_changed")
            if result.get("width", info.width) != info.width or result.get("height", info.height) != info.height:
                raise WindowError("截图尺寸与客户区不符。", "invalid_image")
            if not path.is_file() or path.stat().st_size == 0:
                raise WindowError("未生成有效截图。", "capture_failed")
            observation = {
                "id": observation_id,
                "captured_at": result.get('captured_at',captured_at),
                "window_id": info.hwnd,
                "window_pid": info.pid,
                "path": str(path.resolve()),
                "width": info.width,
                "height": info.height,
                "method": result["method"],
                "warnings": warnings + list(result.get("warnings", [])),
                "state": "unknown",
                "client_rect": {"left": info.left, "top": info.top, "width": info.width, "height": info.height},
                "coordinate_space": "client_physical_pixels",
                "background_capture": not bool(result.get('requires_foreground', not capture_in_background)),
                "window_executable": info.executable,
                "window_class": info.window_class,
            }
            for field in ('frame_sequence','stream_generation','captured_monotonic'):
                if field in result:observation[field]=result[field]
            with self._lock:
                if revision != self._binding_revision or self._bound != (info.hwnd, info.pid):
                    raise WindowError("截图期间绑定窗口已变化，请重新观察。", "binding_changed")
                self._latest = observation
                self._latest_info = info
                # Encoding/library setup time counts against the evidence age.
                self._latest_at = result.get('captured_monotonic',capture_started)
                self._background_verified = observation['background_capture']
            return dict(observation)
        except WindowError:
            path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            path.unlink(missing_ok=True)
            raise WindowError("窗口截图失败，请确认游戏可见后重试。", "capture_failed") from exc

    @property
    def input_enabled(self) -> bool:
        return self._input_enabled and not self._stop.is_set()

    def set_input_enabled(self, enabled: bool) -> None:
        with self._lock:
            if enabled and self._stop.is_set():
                raise WindowError("请先解除停止状态。", "stopped")
            self._input_enabled = bool(enabled)

    def set_stop(self) -> None:
        # Event is set without acquiring the capture lock: stop stays responsive.
        self._stop.set()
        self._input_enabled = False
        self.stop_capture_session()

    def stop_capture_session(self)->None:
        backend=self._backend
        if backend is not None and hasattr(backend,'stop_capture_session'):backend.stop_capture_session()

    def start_capture_session(self,guard=None)->bool:
        if guard:guard()
        info=self._require_window(require_foreground=False)
        backend=self._native()
        if not hasattr(backend,'start_capture_session'):return False
        backend.start_capture_session(info,guard)
        if guard:guard()
        return True

    def peek_observation_sample(self,*,after_sequence=0,not_before=None,max_age=.35,timeout=.5,cancel_event=None):
        backend=self._native()
        if not hasattr(backend,'peek_observation_sample'):return None
        info=self._require_window(require_foreground=False)
        sample=backend.peek_observation_sample(info,after_sequence=after_sequence,not_before=not_before,
            max_age=max_age,timeout=timeout,cancel_event=cancel_event)
        after=self._require_window(require_foreground=False)
        if _geometry(info)!=_geometry(after):raise WindowError('窗口几何已经变化，请重新观察。','geometry_changed')
        return sample

    def reset_stop(self) -> None:
        self._stop.clear()
        # Resetting a stop is not authorization to resume input.
        self._input_enabled = False

    def _check_stop(self, cancel_event: Any = None) -> None:
        if self._stop.is_set() or (cancel_event is not None and cancel_event.is_set()):
            raise WindowError("操作已停止。", "stopped")
        if not self._input_enabled:
            raise WindowError("输入尚未开启，请先允许执行单步操作。", "input_disabled")

    def controlled_click(self, x: int, y: int, observation_id: str,
                         cancel_event: Any = None) -> dict[str, Any]:
        self._check_stop(cancel_event)
        with self._lock:
            self._check_stop(cancel_event)
            if not self._latest or observation_id != self._latest["id"] or self._latest_info is None:
                raise WindowError("观察记录已失效，请重新截图。", "observation_mismatch")
            age = self._monotonic() - self._latest_at
            if age < 0 or age > self._max_age:
                raise WindowError("截图已过期，请重新截图。", "observation_expired")
            if (isinstance(x, bool) or isinstance(y, bool)
                    or not isinstance(x, int) or not isinstance(y, int)):
                raise WindowError("点击坐标必须是客户区整数像素。", "invalid_coordinates")
            info = self._require_window()
            self._require_input_permission(info)
            if _geometry(info) != _geometry(self._latest_info):
                raise WindowError("窗口已移动或缩放，请重新截图。", "geometry_changed")
            if not (0 <= x < info.width and 0 <= y < info.height):
                raise WindowError("点击位置超出游戏客户区。", "out_of_bounds")
            # Immediately before the input, check identity, client rect and focus again.
            final = self._require_window()
            if _geometry(final) != _geometry(info):
                raise WindowError("执行前窗口发生变化，请重新截图。", "geometry_changed")
            if self._monotonic() - self._latest_at > self._max_age:
                raise WindowError("截图已过期，请重新截图。", "observation_expired")
            self._check_stop(cancel_event)
            # A consumed/failed input is never retried with the same observation.
            self._latest = self._latest_info = None
            native=self._native()
            def guard():
                self._check_stop(cancel_event)
                if _geometry(self._require_window())!=_geometry(info):
                    raise WindowError('点击前窗口已变化。','geometry_changed')
            guarded=getattr(native,'click_guarded',None)
            native_evidence=(guarded(info.left+x,info.top+y,guard) if guarded
                             else native.click(info.left+x,info.top+y))
            return {
                "ok": True, "action": "click", "x": x, "y": y,
                "screen_x": info.left + x, "screen_y": info.top + y,
                "window_id": info.hwnd, "observation_id": observation_id,
                "executed_at": self._clock(), "state": "unverified",
                "verification_required": True,
                "native_evidence":native_evidence if isinstance(native_evidence,dict) else {},
            }

    def discover_capabilities(self) -> dict[str, Any]:
        result = discover_capabilities()
        result["maafw_framepool_verified"] = self._maa_verified
        result["background_capture_verified"] = self._background_verified
        result["input_enabled"] = self.input_enabled
        result["stopped"] = self._stop.is_set()
        if self._maa_failure:
            result["warnings"] = [*result["warnings"], self._maa_failure]
        return result

    def controlled_drag(self,x1:int,y1:int,x2:int,y2:int,duration_seconds:float,
                        observation_id:str,cancel_event:Any=None)->dict[str,Any]:
        """A single bounded drag; each intermediate sample observes local stop."""
        self._check_stop(cancel_event)
        if (isinstance(duration_seconds,bool) or not isinstance(duration_seconds,(int,float))
                or not math.isfinite(duration_seconds) or not .15<=duration_seconds<=1.5):
            raise WindowError("拖动时间需要在 0.15–1.5 秒之间。","invalid_duration")
        with self._lock:
            if not self._latest or self._latest['id']!=observation_id or self._latest_info is None:
                raise WindowError("观察已失效，请重新截图。","observation_mismatch")
            if self._monotonic()-self._latest_at>self._max_age:
                raise WindowError("截图已过期，请重新观察。","observation_expired")
            info=self._require_window()
            self._require_input_permission(info)
            if _geometry(info)!=_geometry(self._latest_info):
                raise WindowError("窗口已移动或缩放，请重新观察。","geometry_changed")
            for x,y in ((x1,y1),(x2,y2)):
                if (isinstance(x,bool) or isinstance(y,bool) or not isinstance(x,int) or not isinstance(y,int)
                    or not 0<=x<info.width or not 0<=y<info.height):
                    raise WindowError("拖动坐标超出游戏客户区。","invalid_coordinates")
            self._check_stop(cancel_event)
            self._latest=self._latest_info=None
        def guard():
            self._check_stop(cancel_event)
            current=self._require_window()
            if current.hwnd!=info.hwnd or current.pid!=info.pid or _geometry(current)!=_geometry(info):
                raise WindowError("拖动期间游戏窗口已变化。","geometry_changed")
        self._native().drag(info.left+x1,info.top+y1,info.left+x2,info.top+y2,
                            duration_seconds,guard)
        return {'ok':True,'action':'drag','x1':x1,'y1':y1,'x2':x2,'y2':y2,
                'window_id':info.hwnd,'observation_id':observation_id,
                'executed_at':self._clock(),'verification_required':True,'state':'unverified'}

    def input_permission_status(self)->dict:
        from .permissions import inspect_game_input_permission
        info=self.bound_window()
        if info is None:return {'requires_elevation':False,'message':'绑定实际游戏窗口后检查输入权限。'}
        # Process elevation is fixed for a process lifetime; invalidate by PID
        # and executable rather than reading the manifest for every UI refresh.
        identity=(info.pid,info.executable)
        if getattr(self,'_permission_identity',None)!=identity:
            self._permission_report=inspect_game_input_permission(info)
            self._permission_identity=identity
        return dict(self._permission_report)

    def _require_input_permission(self,info:WindowInfo)->None:
        # Test backends do not represent an actual OS process.
        if not isinstance(self._native(),_NativeWindows):return
        report=self.input_permission_status()
        if report.get('requires_elevation'):
            raise WindowError('游戏需要更高的输入权限。请从桌宠「设置 → 屏幕与游戏操作」选择「以管理员身份重启」，自行确认 Windows 提示后再执行。',
                              'integrity_mismatch',report)

    def require_input_permission(self)->None:
        self._require_input_permission(self._require_window(require_foreground=False))


class _NativeWindows:
    """Small Win32 implementation. It is instantiated only on Windows."""

    supports_background_capture = True

    def __init__(self):
        from ctypes import wintypes as w

        self.w = w
        self._stream_lock=threading.Lock()
        self._capture_stream=None
        self._capture_guard=None
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        self._callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        prototypes = [
            (self.user, "EnumWindows", [self._callback, w.LPARAM], w.BOOL),
            (self.user, "IsWindow", [w.HWND], w.BOOL),
            (self.user, "IsWindowVisible", [w.HWND], w.BOOL),
            (self.user, "IsIconic", [w.HWND], w.BOOL),
            (self.user, "ShowWindow", [w.HWND, ctypes.c_int], w.BOOL),
            (self.user, "SetForegroundWindow", [w.HWND], w.BOOL),
            (self.user, "GetForegroundWindow", [], w.HWND),
            (self.user, "GetWindowTextLengthW", [w.HWND], ctypes.c_int),
            (self.user, "GetWindowTextW", [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.user, "GetClassNameW", [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.user, "GetWindowRect", [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            (self.user, "GetWindowThreadProcessId", [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            (self.user, "GetClientRect", [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            (self.user, "ClientToScreen", [w.HWND, ctypes.POINTER(w.POINT)], w.BOOL),
            (self.user, "GetSystemMetrics", [ctypes.c_int], ctypes.c_int),
            (self.user, "GetDC", [w.HWND], w.HDC),
            (self.user, "PrintWindow", [w.HWND, w.HDC, w.UINT], w.BOOL),
            (self.user, "IsHungAppWindow", [w.HWND], w.BOOL),
            (self.user, "ReleaseDC", [w.HWND, w.HDC], ctypes.c_int),
            (self.gdi, "CreateCompatibleDC", [w.HDC], w.HDC),
            (self.gdi, "CreateCompatibleBitmap", [w.HDC, ctypes.c_int, ctypes.c_int], w.HBITMAP),
            (self.gdi, "SelectObject", [w.HDC, w.HGDIOBJ], w.HGDIOBJ),
            (self.gdi, "DeleteObject", [w.HGDIOBJ], w.BOOL),
            (self.gdi, "DeleteDC", [w.HDC], w.BOOL),
            (self.gdi, "BitBlt", [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 w.HDC, ctypes.c_int, ctypes.c_int, w.DWORD], w.BOOL),
            (self.gdi, "GetDIBits", [w.HDC, w.HBITMAP, w.UINT, w.UINT,
                                    ctypes.c_void_p, ctypes.c_void_p, w.UINT], ctypes.c_int),
            (self.kernel, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (self.kernel, "CloseHandle", [w.HANDLE], w.BOOL),
            (self.kernel, "QueryFullProcessImageNameW", [w.HANDLE, w.DWORD, w.LPWSTR,
                                                       ctypes.POINTER(w.DWORD)], w.BOOL),
            (self.dwm, "DwmGetWindowAttribute", [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD], ctypes.c_long),
        ]
        for library, name, args, restype in prototypes:
            fn = getattr(library, name)
            fn.argtypes, fn.restype = args, restype
        self._dpi_fn = getattr(self.user, "SetThreadDpiAwarenessContext", None)
        if self._dpi_fn:
            self._dpi_fn.argtypes = [ctypes.c_void_p]
            self._dpi_fn.restype = ctypes.c_void_p

    @contextlib.contextmanager
    def _physical_pixels(self):
        previous = self._dpi_fn(ctypes.c_void_p(-4)) if self._dpi_fn else None
        try:
            yield
        finally:
            if previous:
                self._dpi_fn(previous)

    def get_window(self, hwnd: int) -> WindowInfo | None:
        with self._physical_pixels():
            if not self.user.IsWindow(hwnd) or not self.user.IsWindowVisible(hwnd):
                return None
            pid = self.w.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            # Same-process GetWindowText sends synchronous WM_GETTEXT to Qt's
            # GUI thread. Worker enumeration must never wait on our own UI.
            if pid.value == os.getpid():
                return None
            length = self.user.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(length + 1)
            self.user.GetWindowTextW(hwnd, title, length + 1)
            window_class = ctypes.create_unicode_buffer(256)
            self.user.GetClassNameW(hwnd, window_class, len(window_class))
            executable = ''
            process = self.kernel.OpenProcess(0x1000, False, pid.value)
            if process:
                try:
                    image_path = ctypes.create_unicode_buffer(32768)
                    count = self.w.DWORD(len(image_path))
                    if self.kernel.QueryFullProcessImageNameW(process, 0, image_path, ctypes.byref(count)):
                        executable = image_path.value
                finally:
                    self.kernel.CloseHandle(process)
            rect, origin = self.w.RECT(), self.w.POINT(0, 0)
            if not self.user.GetClientRect(hwnd, ctypes.byref(rect)) or not self.user.ClientToScreen(hwnd, ctypes.byref(origin)):
                return None
            return WindowInfo(int(hwnd), int(pid.value), title.value, origin.x, origin.y,
                              rect.right - rect.left, rect.bottom - rect.top,
                              self.user.GetForegroundWindow() == hwnd, bool(self.user.IsIconic(hwnd)),
                              executable, window_class.value)

    def list_windows(self) -> list[WindowInfo]:
        windows: list[WindowInfo] = []

        @self._callback
        def callback(hwnd: int, _param: int) -> bool:
            info = self.get_window(hwnd)
            if info and info.title and (info.width > 0 or info.minimized):
                windows.append(info)
            return True

        if not self.user.EnumWindows(callback, 0):
            raise WindowError("无法枚举桌面窗口。", "enumeration_failed")
        return windows

    def _virtual_rect(self) -> tuple[int, int, int, int]:
        return tuple(self.user.GetSystemMetrics(i) for i in (76, 77, 78, 79))

    def focus_window(self, hwnd: int) -> None:
        if self.user.IsIconic(hwnd):
            self.user.ShowWindow(hwnd, 9)  # SW_RESTORE, no game content interaction.
        if self.user.GetForegroundWindow() == hwnd:
            return
        self.user.SetForegroundWindow(hwnd)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if self.user.GetForegroundWindow() == hwnd:
                return
            time.sleep(0.025)
        raise WindowError("Windows 没有允许切换前台，请手动切回游戏后再次观察。", "focus_denied")

    def capture(self, info: WindowInfo, path: Path) -> dict[str, Any]:
        try:
            if _wgc_available():
                return self.capture_background(info, path)
            return self._capture_printwindow(info, path)
        except WindowError as exc:
            path.unlink(missing_ok=True)
            current = self.get_window(info.hwnd)
            if current is None or not current.foreground:
                raise WindowError("窗口后台捕获暂不可用。请允许切换到游戏前台，或手动切回游戏后再次观察。", "background_capture_failed") from exc
            result = self._capture_foreground(info, path)
            result["warnings"].insert(0, str(exc) + " 已回退到前台可见客户区。")
            result["requires_foreground"] = True
            return result

    def capture_background(self, info: WindowInfo, path: Path) -> dict[str, Any]:
        with self._stream_lock:stream=self._capture_stream
        if stream is not None:
            from .capture_stream import CaptureStreamError,export_frame
            try:
                self._stream_guard(info)
                frame=stream.read_frame(after_sequence=stream.latest_sequence,not_before=time.monotonic(),
                    timeout=3,guard=lambda:self._stream_guard(info))
                result=export_frame(frame,path,info,self._client_crop_geometry)
                result['method']='windows_graphics_capture'
                result['background_capture']=True
                return result
            except CaptureStreamError as exc:raise WindowError(str(exc),exc.code) from None
        if not _wgc_available():
            raise WindowError('未找到 Windows 后台捕获组件，请使用完整解压的新版发行包。', 'wgc_unavailable')
        return _WgcCapture().capture(info, path, self._client_crop_geometry)

    def _stream_guard(self,info):
        guard=self._capture_guard
        if guard:guard()
        current=self.get_window(info.hwnd)
        if current is None or current.pid!=info.pid:raise WindowError('捕获窗口身份已改变。','window_missing')
        if _geometry(current)!=_geometry(info):raise WindowError('窗口几何已改变，请重新观察。','geometry_changed')
        if current.minimized:raise WindowError('游戏窗口已最小化。','minimized')

    def start_capture_session(self,info,guard=None):
        from .capture_stream import CaptureStream,CaptureStreamError
        self.stop_capture_session()
        stream=CaptureStream(info.hwnd,info.pid,(info.left,info.top,info.width,info.height))
        with self._stream_lock:self._capture_stream=stream;self._capture_guard=guard
        try:stream.start(guard)
        except CaptureStreamError as exc:
            self.stop_capture_session();raise WindowError(str(exc),exc.code) from None

    def stop_capture_session(self):
        with self._stream_lock:
            stream,self._capture_stream=self._capture_stream,None
            self._capture_guard=None
        if stream:stream.close()

    def peek_observation_sample(self,info,**options):
        from .capture_stream import CaptureStreamError
        with self._stream_lock:stream=self._capture_stream
        if stream is None:return None
        try:
            self._stream_guard(info)
            return stream.peek_sample(info,self._client_crop_geometry,guard=lambda:self._stream_guard(info),**options)
        except CaptureStreamError as exc:raise WindowError(str(exc),exc.code) from None

    def _client_crop_geometry(self, info: WindowInfo, frame_width: int, frame_height: int) -> tuple[int, int]:
        if (frame_width, frame_height) == (info.width, info.height):
            return 0, 0
        with self._physical_pixels():
            extended, outer = self.w.RECT(), self.w.RECT()
            candidates = []
            if self.dwm.DwmGetWindowAttribute(info.hwnd, 9, ctypes.byref(extended), ctypes.sizeof(extended)) == 0:
                candidates.append(extended)
            if self.user.GetWindowRect(info.hwnd, ctypes.byref(outer)):
                candidates.append(outer)
            for bounds in candidates:
                if (bounds.right-bounds.left, bounds.bottom-bounds.top) == (frame_width, frame_height):
                    left, top = info.left-bounds.left, info.top-bounds.top
                    if min(left, top) >= 0 and left+info.width <= frame_width and top+info.height <= frame_height:
                        return left, top
        raise WindowError('后台帧尺寸与客户区坐标不符，请重新绑定。', 'wgc_client_bounds')

    def _capture_printwindow(self, info: WindowInfo, path: Path) -> dict[str, Any]:
        """Render the selected HWND only. Occluding windows are not included."""
        with self._physical_pixels():
            if info.width * info.height > 40_000_000 or info.width <= 0 or info.height <= 0:
                raise WindowError("窗口尺寸无效，无法截图。", "invalid_geometry")
            if self.user.IsHungAppWindow(info.hwnd):
                raise WindowError("游戏窗口暂未响应，请稍后重试。", "window_unresponsive")
            screen, memory, bitmap, old = None, None, None, None
            try:
                screen = self.user.GetDC(None)
                memory = self.gdi.CreateCompatibleDC(screen) if screen else None
                bitmap = self.gdi.CreateCompatibleBitmap(screen, info.width, info.height) if memory else None
                if not bitmap:
                    raise WindowError("无法创建窗口截图。", "capture_failed")
                old = self.gdi.SelectObject(memory, bitmap)
                if not old or old == ctypes.c_void_p(-1).value:
                    raise WindowError("无法读取窗口画面。", "capture_failed")
                # PW_CLIENTONLY=1; PW_RENDERFULLCONTENT=2 (Windows 8.1+).
                ctypes.set_last_error(0)
                if not self.user.PrintWindow(info.hwnd, memory, 3):
                    error = ctypes.get_last_error()
                    raise WindowError(f"目标窗口没有返回渲染画面（Windows 错误 {error}）。", "printwindow_failed")
                self.gdi.SelectObject(memory, old)
                old = None
                header = struct.pack("<IiiHHIIiiII", 40, info.width, -info.height,
                                     1, 32, 0, info.width * info.height * 4, 0, 0, 0, 0)
                bitmap_info = ctypes.create_string_buffer(header + b"\x00" * 16)
                pixels = ctypes.create_string_buffer(info.width * info.height * 4)
                rows = self.gdi.GetDIBits(screen, bitmap, 0, info.height, pixels, bitmap_info, 0)
                if rows != info.height:
                    raise WindowError("窗口截图不完整。", "capture_failed")
                raw = pixels.raw
                sample_stride = max(4, (len(raw) // 4096 // 4) * 4)
                sample = b"".join(raw[channel::sample_stride] for channel in range(3))
                if not sample or max(sample) < 8 or max(sample) - min(sample) < 3:
                    raise WindowError("窗口返回空白或黑色画面。", "blank_frame")
                path.write_bytes(_encode_png(info.width, info.height, raw))
                return {"method": "win32_printwindow", "width": info.width, "height": info.height,
                        "requires_foreground": False,
                        "warnings": ["目标窗口客户区捕获；仍需检查画面是否为最新游戏状态。"]}
            finally:
                if old and memory:
                    self.gdi.SelectObject(memory, old)
                if bitmap:
                    self.gdi.DeleteObject(bitmap)
                if memory:
                    self.gdi.DeleteDC(memory)
                if screen:
                    self.user.ReleaseDC(None, screen)

    def _capture_foreground(self, info: WindowInfo, path: Path) -> dict[str, Any]:
        with self._physical_pixels():
            vx, vy, vw, vh = self._virtual_rect()
            if (info.left < vx or info.top < vy or info.left + info.width > vx + vw
                    or info.top + info.height > vy + vh):
                raise WindowError("游戏窗口超出屏幕边缘，请完整移入屏幕后重拍。", "offscreen")
            if info.width * info.height > 40_000_000:
                raise WindowError("窗口尺寸过大，无法截图。", "invalid_geometry")
            screen, memory, bitmap, old = None, None, None, None
            try:
                screen = self.user.GetDC(None)
                memory = self.gdi.CreateCompatibleDC(screen) if screen else None
                bitmap = self.gdi.CreateCompatibleBitmap(screen, info.width, info.height) if memory else None
                if not bitmap:
                    raise WindowError("无法创建窗口截图。", "capture_failed")
                old = self.gdi.SelectObject(memory, bitmap)
                if not old or old == ctypes.c_void_p(-1).value:
                    raise WindowError("无法读取窗口画面。", "capture_failed")
                if not self.gdi.BitBlt(memory, 0, 0, info.width, info.height, screen,
                                       info.left, info.top, 0x00CC0020):
                    raise WindowError("前台画面读取失败。", "capture_failed")
                # GetDIBits requires the bitmap to be deselected from the DC.
                self.gdi.SelectObject(memory, old)
                old = None
                header = struct.pack("<IiiHHIIiiII", 40, info.width, -info.height,
                                     1, 32, 0, info.width * info.height * 4, 0, 0, 0, 0)
                bitmap_info = ctypes.create_string_buffer(header + b"\x00" * 16)
                pixels = ctypes.create_string_buffer(info.width * info.height * 4)
                rows = self.gdi.GetDIBits(screen, bitmap, 0, info.height, pixels, bitmap_info, 0)
                if rows != info.height:
                    raise WindowError("窗口截图不完整。", "capture_failed")
                path.write_bytes(_encode_png(info.width, info.height, pixels.raw))
                warnings = ["前台可见区域捕获；请关闭遮挡游戏的置顶窗口。", "尚未识别游戏界面，启动器也可能出现在候选窗口中。"]
                sample_stride = max(4, (len(pixels.raw) // 4096 // 4) * 4)
                sample = b"".join(pixels.raw[channel::sample_stride] for channel in range(3))
                if sample and max(sample) < 8:
                    warnings.append("画面接近黑色，请人工检查截图是否正常。")
                return {"method": "win32_gdi_foreground", "warnings": warnings,
                        "width": info.width, "height": info.height}
            finally:
                if old and memory:
                    self.gdi.SelectObject(memory, old)
                if bitmap:
                    self.gdi.DeleteObject(bitmap)
                if memory:
                    self.gdi.DeleteDC(memory)
                if screen:
                    self.user.ReleaseDC(None, screen)

    def _pointer(self, screen_x: int, screen_y: int,flags:int) -> None:
        from ctypes import wintypes as w

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
                        ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                        ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]

        class INPUT_UNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", w.DWORD), ("value", INPUT_UNION)]

        with self._physical_pixels():
            vx, vy, vw, vh = self._virtual_rect()
            if not (vx <= screen_x < vx + vw and vy <= screen_y < vy + vh) or vw < 2 or vh < 2:
                raise WindowError("点击位置不在可见屏幕内。", "offscreen")
            # Position explicitly in physical pixels and verify before pressing;
            # an accepted input call alone is not evidence of pointer movement.
            if flags==0:
                self.user.SetCursorPos.argtypes=[ctypes.c_int,ctypes.c_int]
                self.user.SetCursorPos.restype=w.BOOL
                if not self.user.SetCursorPos(screen_x,screen_y):
                    code=ctypes.get_last_error()
                    raise WindowError(f'系统未能移动鼠标到游戏目标（Windows 错误 {code}），本次没有按下按钮。请检查游戏输入权限。','pointer_move_failed',{'win32_error':code,'button_pressed':False})
                return
            self.user.SendInput.argtypes = [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
            self.user.SendInput.restype = w.UINT
            event=INPUT();event.value.mi=MOUSEINPUT(0,0,0,flags,0,0)
            if self.user.SendInput(1,ctypes.byref(event),ctypes.sizeof(INPUT))!=1:
                raise WindowError("系统未接收游戏输入，请检查窗口权限。","input_failed")

    def click_guarded(self,screen_x:int,screen_y:int,guard:Any)->dict:
        return self.click(screen_x,screen_y,guard)

    def click(self,screen_x:int,screen_y:int,guard:Any=None)->dict:
        # Unity polls mouse state on frames. A down/up pair in one frame may
        # be received by Windows yet never appear as a press in the game.
        self._pointer(screen_x,screen_y,0)
        time.sleep(.016)
        self._verify_pointer_target(screen_x,screen_y)
        if guard:guard()
        pressed=False
        try:
            self._pointer(screen_x,screen_y,0x0002)
            pressed=self._mouse_button_down()
            time.sleep(.060)
        finally:self._pointer(screen_x,screen_y,0x0004)
        return {'pointer_verified':True,'button_down_observed':pressed,
                'button_released':not self._mouse_button_down()}

    def _mouse_button_down(self)->bool:
        self.user.GetAsyncKeyState.argtypes=[ctypes.c_int]
        self.user.GetAsyncKeyState.restype=ctypes.c_short
        return bool(self.user.GetAsyncKeyState(1)&0x8000)

    def _verify_pointer_target(self,screen_x:int,screen_y:int)->None:
        """Reject displaced cursors or an overlay before pressing any button."""
        with self._physical_pixels():
            point=self.w.POINT()
            self.user.GetPhysicalCursorPos.argtypes=[ctypes.POINTER(self.w.POINT)]
            self.user.GetPhysicalCursorPos.restype=self.w.BOOL
            if not self.user.GetPhysicalCursorPos(ctypes.byref(point)):
                raise WindowError('无法核对实际鼠标位置，未按下按钮。','pointer_unverified')
            if abs(point.x-screen_x)>3 or abs(point.y-screen_y)>3:
                raise WindowError(f'鼠标未到达目标（预期 {screen_x},{screen_y}，实际 {point.x},{point.y}），未按下按钮。','pointer_displaced')
            self.user.WindowFromPoint.argtypes=[self.w.POINT]
            self.user.WindowFromPoint.restype=self.w.HWND
            self.user.GetAncestor.argtypes=[self.w.HWND,self.w.UINT]
            self.user.GetAncestor.restype=self.w.HWND
            hit=self.user.WindowFromPoint(point)
            hit_root=self.user.GetAncestor(hit,2) if hit else None
            if hit_root!=self.user.GetForegroundWindow():
                raise WindowError('目标位置被其他窗口遮挡，未按下按钮。请移开遮挡窗口后重试。','target_occluded')

    def drag(self,x1:int,y1:int,x2:int,y2:int,duration:float,guard:Any)->None:
        """Play a bounded local trajectory without a cloud round trip per sample."""
        guard();last_x,last_y=x1,y1
        try:
            self._pointer(x1,y1,0)
            time.sleep(.016)
            self._verify_pointer_target(x1,y1)
            guard()
            self._pointer(x1,y1,0x0002)
            started=time.monotonic()
            while True:
                guard()
                progress=min(1.,(time.monotonic()-started)/duration)
                last_x=round(x1+(x2-x1)*progress);last_y=round(y1+(y2-y1)*progress)
                self._pointer(last_x,last_y,0)
                if progress>=1:return
                time.sleep(.008)
        finally:self._pointer(last_x,last_y,0x0004)

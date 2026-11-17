"""Read-only, bounded Windows OCR for an already captured local image.

No capture or input APIs are used here. Windows PowerShell 5.1 hosts the OS's
installed Chinese OCR engine; every call owns and cleans up only its own child.
``elapsed_ms`` is the helper's decode/recognition time, excluding host startup.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 512 * 1024
MAX_NAV_OUTPUT_BYTES = 2 * MAX_OUTPUT_BYTES + 128
MAX_ERROR_BYTES = 16 * 1024
MAX_WORDS = 2048
MAX_DIMENSION = 10000
_ENGINE = "windows.media.ocr:zh-Hans-CN"
_MESSAGES = {
    "ocr_cancelled": "本地文字识别已取消。",
    "ocr_timeout": "本地文字识别超时。",
    "ocr_image_missing": "识别图片不存在或不是文件。",
    "ocr_image_unreadable": "无法读取识别图片。",
    "ocr_image_too_large": "识别图片超过大小限制。",
    "ocr_language_unavailable": "系统尚未安装中文文字识别语言。",
    "ocr_host_unavailable": "本地文字识别运行环境不可用。",
    "ocr_host_policy_blocked": "系统脚本策略阻止了本地文字识别。",
    "ocr_host_failed": "本地文字识别进程未正常完成。",
    "ocr_output_too_large": "本地文字识别结果超过大小限制。",
    "ocr_invalid_output": "本地文字识别结果无效。",
    "ocr_failed": "本地文字识别失败。",
}
_SCRIPT_CODES = frozenset({"ocr_image_missing", "ocr_image_unreadable",
                          "ocr_image_too_large", "ocr_language_unavailable",
                          "ocr_output_too_large", "ocr_failed"})


class LocalOcrError(RuntimeError):
    """Only a fixed safe code/message crosses the process boundary."""

    def __init__(self, code: str):
        self.code = code if code in _MESSAGES else "ocr_failed"
        super().__init__(_MESSAGES[self.code])


class _PipeReader:
    def __init__(self, pipe, limit: int):
        self.data = bytearray()
        self.overflow = False
        self.failed = False
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._read, args=(pipe, limit), daemon=True)
        self.thread.start()

    def _read(self, pipe, limit):
        try:
            while True:
                chunk = pipe.read(min(4096, limit - len(self.data) + 1))
                if not chunk:
                    break
                if len(self.data) + len(chunk) > limit:
                    self.overflow = True
                    break
                self.data.extend(chunk)
        except Exception:
            self.failed = True
        finally:
            try:
                pipe.close()
            except Exception:
                pass
            self.done.set()


def _number(value, *, minimum=0, maximum=None):
    try:
        return (type(value) in (int, float) and math.isfinite(value)
                and value >= minimum and (maximum is None or value <= maximum))
    except OverflowError:
        return False


def _project(raw):
    invalid = LocalOcrError("ocr_invalid_output")
    if not isinstance(raw, dict):
        raise invalid
    if raw.get("ok") is False:
        code = raw.get("code")
        raise LocalOcrError(code if isinstance(code, str) and code in _SCRIPT_CODES else "ocr_failed")
    width, height = raw.get("width"), raw.get("height")
    if (type(width) is not int or type(height) is not int
            or not 0 < width <= MAX_DIMENSION or not 0 < height <= MAX_DIMENSION
            or raw.get("engine") != _ENGINE or not _number(raw.get("elapsed_ms"))):
        raise invalid
    words = raw.get("words")
    if not isinstance(words, list) or len(words) > MAX_WORDS:
        raise invalid
    projected = []
    for word in words:
        if not isinstance(word, dict):
            raise invalid
        text, rect = word.get("text"), word.get("rect")
        if not isinstance(text, str) or not text.strip() or len(text) > 512 or not isinstance(rect, dict):
            raise invalid
        x, y, w, h = (rect.get(key) for key in ("x", "y", "width", "height"))
        if (not all(_number(value) for value in (x, y, w, h))
                or w <= 0 or h <= 0 or x + w > width or y + h > height):
            raise invalid
        projected.append({"text": text, "rect": {"x": x, "y": y, "width": w, "height": h}})
    return {"width": width, "height": height, "words": projected,
            "engine": _ENGINE, "elapsed_ms": raw["elapsed_ms"]}


def _stop_child(process):
    try:
        if process.poll() is None:
            process.kill()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.wait(timeout=0.5)
    except (OSError, subprocess.SubprocessError):
        pass


class LocalOcr:
    def __init__(self, *, process_factory=None, timeout: float = 10., clock=time.monotonic, temp_root=None):
        if not _number(timeout, minimum=0.01, maximum=120.):
            raise ValueError("timeout must be between 0.01 and 120 seconds")
        self._factory = process_factory or subprocess.Popen
        self._timeout, self._clock = timeout, clock
        self._temp_root = temp_root
        self._script = Path(__file__).resolve().parent.parent / "tools" / "winrt_ocr.ps1"
        self._host = (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
                      / "WindowsPowerShell" / "v1.0" / "powershell.exe")

    def _check(self, cancel_event, started=None):
        if cancel_event is not None and cancel_event.is_set():
            raise LocalOcrError("ocr_cancelled")
        if started is not None and self._clock() - started >= self._timeout:
            raise LocalOcrError("ocr_timeout")

    def _input(self, image_path):
        try:
            image = Path(image_path).resolve(strict=True)
            if not image.is_file():
                raise LocalOcrError("ocr_image_missing")
            if image.stat().st_size > MAX_IMAGE_BYTES:
                raise LocalOcrError("ocr_image_too_large")
        except FileNotFoundError:
            raise LocalOcrError("ocr_image_missing") from None
        except (OSError, ValueError, TypeError):
            raise LocalOcrError("ocr_image_unreadable") from None
        return image

    def recognize(self, image_path, *, cancel_event=None) -> dict:
        self._check(cancel_event)
        image = self._input(image_path)
        return _project(self._run_host(image, cancel_event=cancel_event, started=self._clock()))

    def recognize_navigation(self, image_path, *, cancel_event=None) -> dict:
        """Original plus one generic, inverted lower-right technical OCR input.

        Raw text stays in memory. Regions carry only locally constructed pixel
        transforms; ``GameNavigation`` exports matched labels after fusion.
        Temporary BMPs are removed on success, cancellation and every failure.
        """
        self._check(cancel_event)
        started = self._clock()
        image_path = self._input(image_path)
        try:
            from PySide6.QtCore import Qt
            from PySide6.QtGui import QImageReader
            reader = QImageReader(str(image_path))
            size = reader.size()
            width, height = size.width(), size.height()
            # Bound allocation before decoding. Windows OCR supports at most
            # 2600 px per side; the helper also checks the actual OS limit.
            if width <= 0 or height <= 0:
                raise LocalOcrError("ocr_image_unreadable")
            if max(width, height) > 2600 or width * height * 4 > MAX_IMAGE_BYTES:
                raise LocalOcrError("ocr_image_too_large")
            self._check(cancel_event, started)
            image = reader.read()
            if image.isNull() or image.width() != width or image.height() != height:
                raise LocalOcrError("ocr_image_unreadable")
            x, y = round(width * .45), round(height * .45)
            crop = image.copy(x, y, width - x, height - y)
            factor = min(2., 2500 / max(crop.width(), crop.height()))
            processed = crop.scaled(round(crop.width() * factor), round(crop.height() * factor),
                                    Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            processed.invertPixels()
            self._check(cancel_event, started)
            with tempfile.TemporaryDirectory(prefix="shu-ocr-navigation-", dir=self._temp_root) as temporary:
                path = Path(temporary) / "region.bmp"
                if not processed.save(str(path), "BMP") or path.stat().st_size > MAX_IMAGE_BYTES:
                    raise LocalOcrError("ocr_image_unreadable")
                self._check(cancel_event, started)
                raw = self._run_host(image_path, navigation_image=path, cancel_event=cancel_event,
                                     started=started, max_output=MAX_NAV_OUTPUT_BYTES)
                self._check(cancel_event, started)
                if isinstance(raw, dict) and raw.get("ok") is False:
                    _project(raw)  # Fixed error-code projection, no raw host text.
                values = raw.get("observations") if isinstance(raw, dict) else None
                if not isinstance(values, list) or len(values) != 2:
                    raise LocalOcrError("ocr_invalid_output")
                original, region = map(_project, values)
                if ((original['width'], original['height']) != (width, height)
                        or (region['width'], region['height']) != (processed.width(), processed.height())):
                    raise LocalOcrError("ocr_invalid_output")
                return {'width': width, 'height': height, 'engine': _ENGINE,
                        'elapsed_ms': original['elapsed_ms'] + region['elapsed_ms'], 'regions': [
                            {'observation': original, 'origin': {'x': 0, 'y': 0}, 'scale': {'x': 1., 'y': 1.}},
                            {'observation': region, 'origin': {'x': x, 'y': y},
                             'scale': {'x': processed.width() / crop.width(), 'y': processed.height() / crop.height()}},
                        ]}
        except LocalOcrError:
            raise
        except (OSError, ValueError, RuntimeError, ImportError):
            raise LocalOcrError("ocr_image_unreadable") from None

    def _run_host(self, image, *, cancel_event, started, navigation_image=None, max_output=MAX_OUTPUT_BYTES):
        arguments = [str(self._host), "-NoProfile", "-NonInteractive", "-File",
                     str(self._script), "-ImagePath", str(image)]
        if navigation_image is not None:
            arguments.extend(["-NavigationImagePath", str(navigation_image)])
        try:
            process = self._factory(arguments, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, ValueError, subprocess.SubprocessError):
            raise LocalOcrError("ocr_host_unavailable") from None
        readers = []
        try:
            readers.append(_PipeReader(process.stdout, max_output))
            readers.append(_PipeReader(process.stderr, MAX_ERROR_BYTES))
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise LocalOcrError("ocr_cancelled")
                if any(reader.overflow for reader in readers):
                    raise LocalOcrError("ocr_output_too_large")
                if any(reader.failed for reader in readers):
                    raise LocalOcrError("ocr_host_failed")
                returncode = process.poll()
                if returncode is not None and all(reader.done.is_set() for reader in readers):
                    break
                if self._clock() - started >= self._timeout:
                    raise LocalOcrError("ocr_timeout")
                # The caller's event wakes this wait immediately upon cancellation.
                if cancel_event is not None:
                    cancel_event.wait(0.02)
                else:
                    time.sleep(0.02)
            # A reader may finish between the checks above and the EOF check.
            if any(reader.overflow for reader in readers):
                raise LocalOcrError("ocr_output_too_large")
            if any(reader.failed for reader in readers):
                raise LocalOcrError("ocr_host_failed")
            if returncode:
                stderr = bytes(readers[1].data).lower()
                policy_blocked = any(token in stderr for token in (
                    b"pssecurityexception", b"unauthorizedaccess", b"executionpolicy", b"scripts is disabled"))
                raise LocalOcrError("ocr_host_policy_blocked" if policy_blocked else "ocr_host_failed")
            try:
                raw = json.loads(readers[0].data.decode("utf-8-sig"))
            except (UnicodeError, ValueError, RecursionError):
                raise LocalOcrError("ocr_invalid_output") from None
            return raw
        finally:
            _stop_child(process)
            for reader in readers:
                reader.thread.join(timeout=0.2)

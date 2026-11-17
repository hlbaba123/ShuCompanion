"""The bounded, screenshot-based MAA mystery-code import recipe."""
from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path
import re
import threading
import time

from .maa_visual_readback import MaaVisualReadback, verify_import


class VisualImportError(RuntimeError):
    def __init__(self, code, message, *, evidence_refs=()):
        self.code = code
        self.evidence_refs = list(evidence_refs)
        super().__init__(message)


class _UnsettledVisualObservation(VisualImportError):
    """A same-image page-state disagreement, never valid for GUI input."""


def _frame_reference(frame):
    return {key: frame.get(key) for key in ('screenshot_id', 'image_path', 'image_sha256')}


def _compare_frame_images(source, fresh, *, target=None, allow_queue_viewport=False):
    """Bind an old interpretation to fresh native pixels, without model claims.

    Exceptions are a tiny input caret, recoloring of unchanged colored text
    above every control, opt-in list scrolling for batch code input, and
    the slim right rail for the wheel-only queue target.
    Other controls and forbidden regions stay exact; images are never resized.
    """
    import cv2
    import numpy as np

    report = {'schema': 'maa_visual_refresh/v1', 'ok': False, 'reason': 'invalid_frame',
              'source': _frame_reference(source), 'fresh': _frame_reference(fresh),
              'target': target, 'changed_pixels': None, 'caret_exception': False,
              'ignored_caption_pixels': 0, 'remaining_changed_pixels': None, 'caption_region': None}
    try:
        old_window, new_window = source['window'], fresh['window']
        if (not source.get('screenshot_id') or not fresh.get('screenshot_id')
                or source['screenshot_id'] == fresh['screenshot_id']):
            report['reason'] = 'capture_not_independent'
            return report
        if (any(old_window[key] != new_window[key]
                for key in ('hwnd', 'pid', 'creation_time', 'executable', 'dpi', 'client_rect'))
                or not new_window.get('foreground') or new_window.get('minimized')
                or fresh.get('permission', {}).get('input_allowed') is not True):
            report['reason'] = 'native_identity_geometry_or_permission_changed'
            return report
        geometry = old_window['client_rect']
        width, height = geometry['width'], geometry['height']
        images, paths = [], []
        for frame in (source, fresh):
            path = Path(frame['image_path']).resolve(strict=True)
            if not path.is_file() or not 24 <= path.stat().st_size <= 32 * 1024 * 1024:
                raise ValueError('invalid image file')
            raw = path.read_bytes()
            if (raw[:8] != b'\x89PNG\r\n\x1a\n'
                    or hashlib.sha256(raw).hexdigest() != frame['image_sha256']):
                raise ValueError('image hash mismatch')
            image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.shape != (height, width, 3):
                raise ValueError('image geometry mismatch')
            images.append(image)
            paths.append(path)
        if paths[0] == paths[1]:
            raise ValueError('capture path reused')
        changed = np.any(images[0] != images[1], axis=2)
        report.update(width=width, height=height, changed_pixels=int(changed.sum()))

        def bounds(box):
            values = [box[key] for key in ('x', 'y', 'width', 'height')]
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
                raise ValueError('invalid rectangle')
            x, y, w, h = values
            if min(x, y) < 0 or min(w, h) <= 0 or x + w > width or y + h > height:
                raise ValueError('rectangle out of bounds')
            return math.floor(x), math.floor(y), math.ceil(x + w), math.ceil(y + h)

        # Typing into the separate code field cannot press a queue row. WPF
        # may scroll its list when this field receives focus. This exception
        # is opt-in for the batch importer, which must still compare a fresh
        # full configuration/body projection before issuing that input.
        report['ignored_queue_pixels'] = 0
        forbidden = source['forbidden_regions']
        if not isinstance(forbidden, list) or not forbidden:
            raise ValueError('forbidden regions required')
        targets = source['targets']
        viewport = None
        if source.get('queue_mode') is True and (target == 'queue_list' or
                                                 (target == 'code_input' and allow_queue_viewport)):
            regions = source['regions']
            ql, qt, qr, qb = bounds(regions['queue_list'])
            _, _, _, fb = bounds(regions['code_input'])
            if qt <= fb or qr - ql <= 1 or qb - qt <= 1:
                raise ValueError('queue and code field must be disjoint')
            controls = [region.get('rect', region) for region in forbidden]
            controls.extend(control['rect'] for name, control in targets.items()
                            if name != 'queue_list' and not name.startswith('row_delete:'))
            for control in controls:
                left, top, right, bottom = bounds(control)
                if max(left, ql + 1) < min(right, qr) and max(top, qt + 1) < min(bottom, qb):
                    raise ValueError('queue interior overlaps a control')
            viewport = ql, qt, qr, qb
        if viewport and target == 'queue_list':
            # WPF both fades and widens its scrollbar on hover; its translucent
            # pixels blend with ordinary/raid row backgrounds. Only the slim
            # right interior is irrelevant to this wheel-only operation. Row
            # bodies, the outline and every actual footer/button stay exact.
            # The expanded WPF thumb is up to 12 device-independent pixels;
            # derive that band from the already bound native DPI.
            rail_width = max(8, math.ceil(12 * new_window['dpi'] / 96))
            strip_x = max(ql + 1, qr - rail_width)
            report['ignored_scrollbar_pixels'] = int(changed[qt + 1:qb, strip_x:qr - 1].sum())
            changed[qt + 1:qb, strip_x:qr - 1] = False
        if viewport and target == 'code_input':
            # All actual button pixels, options and everything outside the
            # measured viewport stay exact.
            report['ignored_queue_pixels'] = int(changed[qt+1:qb, ql+1:qr].sum())
            changed[qt+1:qb, ql+1:qr] = False

        # A locally measured edit outline lies one physical pixel outside its
        # interior. At 125% DPI that outline can touch a nearby Start guard's
        # extra 3px halo without changing the Start region itself. Such pixels
        # remain in `changed`: they require a new interpretation and another
        # native frame below, rather than inheriting the old click authority.
        edit_outline = None
        field_target = targets.get('code_input', {})
        field_region = source.get('regions', {}).get('code_input')
        if (target in (None, 'code_input') and source.get('page') == 'copilot'
                and field_target.get('kind') == 'edit'
                and field_target.get('source') == 'measured_border_and_stage_label'
                and field_region == field_target.get('rect')):
            fl, ft, fr, fb = bounds(field_region)
            edit_outline = np.zeros_like(changed)
            edit_outline[max(0, ft-1):min(height, fb+1),
                         max(0, fl-1):min(width, fr+1)] = True
            edit_outline[ft:fb, fl:fr] = False
        report['edit_outline_pixels_requiring_reobservation'] = 0
        sensitive_tops = []
        for region in forbidden:
            left, top, right, bottom = bounds(region.get('rect', region))
            sensitive_tops.append(max(0, top - 3))
            if changed[top:bottom, left:right].any():
                report['reason'] = 'forbidden_region_changed'
                return report
            pl, pt, pr, pb = max(0, left-3), max(0, top-3), min(width, right+3), min(height, bottom+3)
            halo_changes = changed[pt:pb, pl:pr]
            if edit_outline is not None:
                outline_changes = halo_changes & edit_outline[pt:pb, pl:pr]
                report['edit_outline_pixels_requiring_reobservation'] += int(outline_changes.sum())
                halo_changes = halo_changes & ~edit_outline[pt:pb, pl:pr]
            if halo_changes.any():
                report['reason'] = 'forbidden_region_changed'
                return report
        if target is not None and target not in targets:
            raise ValueError('target missing')
        caret_allowed = (target in (None, 'code_input', 'queue_list')
                         or targets.get(target, {}).get('kind') == 'checkbox')
        for name, control in targets.items():
            left, top, right, bottom = bounds(control['rect'])
            sensitive_tops.append(top)
            if caret_allowed and name == 'code_input':
                continue
            if changed[top:bottom, left:right].any():
                report['reason'] = 'target_region_changed'
                return report
        field = source.get('regions', {}).get('code_input') or targets['code_input']['rect']
        left, top, right, bottom = bounds(field)
        sensitive_tops.append(top)
        tab = source.get('anchors', {}).get('copilot_tab', {})
        tab_target = targets.get('copilot_tab', {})
        if (tab.get('text') == '自动战斗' and tab.get('rect')
                and tab['rect'] == tab_target.get('rect')):
            _, tab_top, _, _ = bounds(tab['rect'])
            # The entire band is above all measured targets and padded deny
            # regions. No field, option, tab, preview or other page area is
            # exempted merely for being near the top of a small window.
            caption_bottom = max(0, min(40, tab_top - 3, min(sensitive_tops) - 3))
            if caption_bottom:
                report['caption_region'] = {'x': 0, 'y': 0, 'width': width, 'height': caption_bottom}
                caption_changed = changed[:caption_bottom]
                if caption_changed.any():
                    a, b = images[0][:caption_bottom], images[1][:caption_bottom]
                    backgrounds, fractions = [], []
                    for image in (a, b):
                        colors, counts = np.unique(image.reshape(-1, 3), axis=0, return_counts=True)
                        modal = int(counts.argmax())
                        backgrounds.append(colors[modal])
                        fractions.append(counts[modal] / (caption_bottom * width))
                    background = backgrounds[0]
                    same_background = (np.array_equal(background, backgrounds[1]) and min(fractions) >= .75
                                       and background.min() >= 220 and np.ptp(background.astype(np.int16)) <= 8)
                    same_glyphs = np.array_equal(np.any(a != background, axis=2), np.any(b != background, axis=2))
                    old_colors, new_colors = a[caption_changed], b[caption_changed]
                    colored = (np.all(np.ptp(old_colors.astype(np.int16), axis=1) > 8)
                               and np.all(np.ptp(new_colors.astype(np.int16), axis=1) > 8))
                    pairs = np.unique(np.concatenate((old_colors, new_colors), axis=1), axis=0)
                    uniform_recolor = (len(pairs) == len(np.unique(old_colors, axis=0))
                                       == len(np.unique(new_colors, axis=0)))
                    if same_background and same_glyphs and colored and uniform_recolor:
                        report['ignored_caption_pixels'] = int(caption_changed.sum())
                        changed = changed.copy()
                        changed[:caption_bottom] = False
        report['remaining_changed_pixels'] = int(changed.sum())
        if changed.any():
            ys, xs = np.nonzero(changed)
            x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
            w, h = x1 - x0 + 1, y1 - y0 + 1
            caret = (caret_allowed and left + 2 <= x0 <= x1 < right - 2
                     and top + 2 <= y0 <= y1 < bottom - 2
                     and 1 <= w <= 3 and 3 <= h <= 32 and w * h <= 96
                     and report['remaining_changed_pixels'] == w * h)
            if not caret:
                report['reason'] = 'frame_pixels_changed'
                return report
            # A caret is an achromatic line appearing/disappearing on a light
            # input background. Colored toggles/text cannot use this exception.
            a, b = images[0][y0:y1 + 1, x0:x1 + 1], images[1][y0:y1 + 1, x0:x1 + 1]
            if (np.max(np.ptp(a.astype(np.int16), axis=2)) > 8
                    or np.max(np.ptp(b.astype(np.int16), axis=2)) > 8
                    or not ((a.min() >= 180 and b.max() <= 160)
                            or (b.min() >= 180 and a.max() <= 160))):
                report['reason'] = 'non_caret_pixels_changed'
                return report
            report['caret_exception'] = True
        reason = 'input_caret_only' if changed.any() else 'same_native_pixels'
        if report['ignored_caption_pixels']:
            reason = 'caption_recolor_and_input_caret' if changed.any() else 'caption_recolor_only'
        report.update(ok=True, reason=reason)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        report['reason'] = 'image_or_region_unverifiable'
    return report


def probe_visual_import(output, *, wait_for_window=0):
    """Read the current client without raising it or sending any input."""
    from .gui_supervisor import GuiSupervisor
    from .maa_visual_native import discover_maa_windows

    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"passed": False, "mode": "maa_visual_readiness", "gui_inputs": 0, "model_requests": 0}
    supervisor = None
    try:
        deadline = time.monotonic() + wait_for_window
        while True:
            targets = discover_maa_windows()
            if (len(targets) == 1 and not targets[0].get("minimized")) or time.monotonic() >= deadline:
                break
            time.sleep(.25)
        if len(targets) != 1:
            raise VisualImportError("maa_window_not_unique", "需要唯一已打开的 MAA 客户端窗口。")
        report["window"] = targets[0]
        # A read-only probe can run with the user's active import control slot
        # occupied. It cannot submit any native action.
        supervisor = GuiSupervisor(targets[0], backend="visual", directory=output,
                                   lock_path=output / "read-only-probe.lock")
        frame = VisualCodeImporter(supervisor).observe()
        report["observation"] = frame
        report["recognition_ok"] = frame.get("page") == "copilot" and frame.get("idle") and frame.get("single_job") is True
        report["input_permission_ok"] = frame["permission"].get("input_allowed") is True
        report["passed"] = report["recognition_ok"] and report["input_permission_ok"]
        report["code"] = ("ready" if report["passed"] else
                          "maa_visual_page" if not report["recognition_ok"] else "maa_visual_integrity")
        if not report["passed"]:
            report["error_message"] = ("请让 MAA 停在空闲的自动战斗单作业页面。"
                                       if not report["recognition_ok"] else
                                       "已读取 MAA 画面。当前执行器的 Windows 权限不足以输入。")
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, ImportError) as error:
        report.update(code=getattr(error, "code", "maa_visual_probe_failed"), error_message=str(error)[:500])
    finally:
        if supervisor:
            supervisor.close()
    (output / "maa-readiness.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                               encoding="utf-8")
    return report


class VisualCodeImporter:
    def __init__(self, supervisor, *, guard=None, submit=None, publish=None,
                 progress=None, readback=None, foreground_timeout=40, load_monitor=None,
                 activate_before_capture=False):
        self.supervisor = supervisor
        self.guard = guard or (lambda: None)
        self.submit = submit or self._submit
        self.publish = publish or self._publish
        self.progress = progress or (lambda name, stage, result: None)
        self.readback = readback or MaaVisualReadback()
        self.foreground_timeout = foreground_timeout
        self.load_monitor = load_monitor
        self.activate_before_capture = activate_before_capture

    def _publish(self, kind, payload):
        from hashlib import sha256
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        digest = sha256(data).hexdigest()
        path = Path(self.supervisor.directory) / f"{kind}-{digest}.json"
        path.write_bytes(data)
        return {"path": str(path), "sha256": digest}

    def _submit(self, args, frame, cancel):
        return self.supervisor.request({"op": "act", "args": args}, timeout=8,
                                       cancel_event=cancel, admit_callback=self.guard)

    def _refresh_for_action(self, frame, target=None, *, cancel_event=None,
                            expected=None, reobserved=False, reacquire_focus=None):
        if getattr(self.readback, 'requires_frame_refresh', False) is not True:
            return frame
        self.guard()
        response = self.supervisor.request({'op': 'snapshot'}, timeout=12, cancel_event=cancel_event)
        if response.get('status') != 'completed':
            raise VisualImportError('maa_visual_refresh_failed', '输入前无法重新取得 MAA 截图。')
        fresh = response['result']
        refs = [self.publish('maa-visual-refresh-source', frame),
                self.publish('maa-visual-refresh-native', fresh)]
        comparison = _compare_frame_images(frame, fresh, target=target)
        identity = self.supervisor.target_identity
        if any(fresh.get('window', {}).get(key) != identity[key]
               for key in ('hwnd', 'pid', 'creation_time')):
            comparison.update(ok=False, reason='native_binding_changed')
        comparison['observation_refs'] = list(refs)
        refs.append(self.publish('maa-visual-refresh-comparison', comparison))
        source_window, fresh_window = frame.get('window', {}), fresh.get('window', {})
        if (comparison['reason'] == 'native_identity_geometry_or_permission_changed'
                and callable(reacquire_focus) and fresh_window.get('foreground') is False
                and not fresh_window.get('minimized')
                and fresh.get('permission', {}).get('input_allowed') is True
                and all(source_window.get(key) == fresh_window.get(key) for key in
                        ('hwnd', 'pid', 'creation_time', 'executable', 'dpi', 'client_rect'))):
            # Slow recognition may outlive foreground ownership. Reuse the
            # same supervised focus action once, then compare another native
            # capture against the original interpreted pixels. Never retry a
            # page input, relax geometry, or trust the focus receipt as proof.
            try:
                reacquire_focus(fresh)
                if cancel_event is not None and cancel_event.is_set():
                    raise VisualImportError('maa_visual_cancelled', '导入已取消。')
                self.guard()
                validated = self._refresh_for_action(frame, target=target, cancel_event=cancel_event,
                                                     expected=expected, reobserved=reobserved)
            except (OSError, RuntimeError, ValueError, TypeError, KeyError, ImportError) as error:
                error.evidence_refs = refs + list(getattr(error, 'evidence_refs', []))
                raise
            validated['visual_refresh_refs'] = refs + validated['visual_refresh_refs']
            return validated
        if (not comparison['ok'] and comparison['reason'] == 'frame_pixels_changed'
                and expected is not None and not reobserved):
            # Re-read a settling interface once, before any input. Reusing the
            # old interpretation is still forbidden; the new target, state and
            # a further independent native frame must all agree.
            renewed = self.observe(expected,cancel_event=cancel_event,recognition_timeout=45)
            refs.append(self.publish('maa-visual-refresh-reobservation', renewed))
            stable_fields = ('page','idle','running','single_job','field_empty','code_text',
                             'codes','preview_signature')
            def option_states(value):
                return {key: item.get('state') for key, item in value.get('options', {}).items()}
            def forbidden_kinds(value):
                return sorted(str(item.get('kind', '')) for item in value.get('forbidden_regions', []))
            # OCR measurements can jitter, but the action retains its original
            # native window/geometry binding. A new interpretation must not
            # silently authorize a moved window or a denied input session.
            # Protected pixels are then checked against another native frame
            # before using the newly measured target.
            if (all(renewed.get('window', {}).get(key) == source_window.get(key) for key in
                    ('hwnd', 'pid', 'creation_time', 'executable', 'dpi', 'client_rect'))
                    and renewed.get('permission', {}).get('input_allowed') is True
                    and all(renewed.get(key) == frame.get(key) for key in stable_fields)
                    and option_states(renewed) == option_states(frame)
                    and forbidden_kinds(renewed) == forbidden_kinds(frame)):
                validated = self._refresh_for_action(renewed,target=target,cancel_event=cancel_event,
                                                     expected=expected,reobserved=True,
                                                     reacquire_focus=reacquire_focus)
                validated['visual_refresh_refs'] = refs+validated['visual_refresh_refs']
                return validated
        if not comparison['ok']:
            raise VisualImportError('maa_visual_refresh_changed',
                                    '模型识别后 MAA 画面已改变，已停止输入，请重新观察。', evidence_refs=refs)
        self.guard()
        return {**frame, **fresh, 'visual_refresh_refs': refs,
                'visual_refresh': {'source': _frame_reference(frame), 'comparison_ref': refs[-1]}}

    def _interpret(self, frame, expected, cancel_event, recognition_timeout=None):
        if getattr(self.readback, 'requires_frame_refresh', False) is not True:
            return self.readback.observe(frame['image_path'], expected=expected, cancel_event=cancel_event)
        limit = getattr(self.readback, 'max_observe_seconds', 45)
        if type(limit) not in (int, float) or not math.isfinite(limit) or not 0 < limit <= 45:
            raise VisualImportError('maa_visual_recognition_limit', '视觉识别时限必须在 45 秒以内。')
        if recognition_timeout is not None:
            limit = min(limit, recognition_timeout)
        if limit <= 0:
            raise VisualImportError('maa_visual_recognition_timeout', 'MAA 视觉识别已超时。')
        finished, local_cancel, outcome = threading.Event(), threading.Event(), {}

        def recognize():
            try:
                outcome['result'] = self.readback.observe(frame['image_path'], expected=expected,
                                                          cancel_event=local_cancel)
            except Exception as error:
                outcome['error'] = error
            finally:
                finished.set()

        deadline = time.monotonic() + limit
        # This worker is read-only. A timed-out network call cannot block the
        # caller or later submit GUI input, and receives its own cancellation.
        threading.Thread(target=recognize, name='maa-visual-readback', daemon=True).start()
        while True:
            done = finished.wait(.025)
            if cancel_event is not None and cancel_event.is_set():
                local_cancel.set()
                raise VisualImportError('maa_visual_cancelled', 'MAA 视觉识别已取消。')
            if time.monotonic() > deadline:
                local_cancel.set()
                raise VisualImportError('maa_visual_recognition_timeout', 'MAA 视觉识别已超时，已停止后续输入。')
            if done:
                if 'error' in outcome:
                    raise outcome['error']
                return outcome['result']

    def observe(self, expected=None, *, cancel_event=None, recognition_timeout=None):
        self.guard()
        observation_deadline = (time.monotonic() + recognition_timeout
                                if recognition_timeout is not None else None)
        response = self.supervisor.request({"op": "snapshot"},
                                           timeout=min(12, recognition_timeout) if recognition_timeout is not None else 12,
                                           cancel_event=cancel_event)
        if response.get("status") != "completed":
            code = response.get("code") or response.get("error") or "capture_failed"
            raise VisualImportError(code, f"MAA 截图失败（{code}）。请确认窗口已恢复且可正常显示。")
        frame = response["result"]
        remaining = observation_deadline - time.monotonic() if observation_deadline is not None else None
        try:
            interpreted = self._interpret(frame, expected, cancel_event, remaining)
        except RuntimeError as error:
            diagnostics = getattr(error, 'evidence', None)
            if isinstance(diagnostics, dict):
                ref = self.publish('maa-visual-invalid-observation',
                                   {'native': frame, 'interpretation_error': diagnostics})
                error.evidence_refs = [*getattr(error, 'evidence_refs', []), ref]
            raise
        identity = self.supervisor.target_identity
        window = frame["window"]
        geometry = window["client_rect"]
        identity_matches = (
            interpreted.get("image_sha256") == frame.get("image_sha256")
            and (interpreted.get("width"), interpreted.get("height"))
            == (geometry["width"], geometry["height"])
            and all(window.get(key) == identity[key] for key in ("hwnd", "pid", "creation_time")))
        if not interpreted.get("ok") or not identity_matches:
            details = {key: interpreted.get(key) for key in (
                'ok', 'reason', 'local_refinement_reason', 'width', 'height', 'image_sha256',
                'page', 'idle', 'running', 'field_empty', 'code_text', 'recognition_engine')}
            ref = self.publish('maa-visual-invalid-observation', {'native': frame, 'interpretation': details})
            error_type = (_UnsettledVisualObservation
                          if identity_matches
                          and interpreted.get('reason') == 'vision_local_conflict'
                          and interpreted.get('local_refinement_reason') == 'page_or_running_state_disagrees'
                          else VisualImportError)
            raise error_type("maa_visual_observation_invalid", "无法核对 MAA 截图身份或图像识别结果。",
                             evidence_refs=[ref])
        self.guard()
        return {**interpreted, **frame}

    def run(self, expected, options=None, cancel_event=None):
        cancel = cancel_event if cancel_event is not None else threading.Event()
        refs, actions = [], []
        action_outcome = "not_issued"
        last_check = None
        load_baseline, load_receipt_ref = None, None
        recognition_retries = 1
        options = {} if options is None else options

        def observe(*, recognition_timeout=None):
            nonlocal recognition_retries
            limit = recognition_timeout
            if getattr(self.readback, 'requires_frame_refresh', False) is True:
                limit = min(45, limit) if limit is not None else 45
            deadline = time.monotonic() + limit if limit is not None else None
            while True:
                remaining = deadline - time.monotonic() if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    raise VisualImportError('maa_visual_recognition_timeout', 'MAA 视觉观察预算已用尽。')
                try:
                    frame = self.observe(expected, cancel_event=cancel, recognition_timeout=remaining)
                    refs.append(self.publish("maa-visual-observation", frame))
                    return frame
                except RuntimeError as error:
                    if getattr(error, 'code', None) != 'vision_invalid_output' or recognition_retries <= 0:
                        raise
                    refs.extend(getattr(error, 'evidence_refs', []))
                    recognition_retries -= 1
                    self.guard()
                    if cancel.is_set():
                        raise VisualImportError('maa_visual_cancelled', '导入已取消。')
                    self.progress('vision_observe', 'tool_started',
                                  {'summary': '视觉转录格式无效，正在重新读取一次 MAA 截图；不重复输入。'})

        def checked_page(frame, *, mode_change=False):
            if (frame.get("page") != "copilot" or not frame.get("idle") or frame.get("running")
                    or not mode_change and frame.get("single_job") is not True):
                raise VisualImportError("maa_visual_page", "请让 MAA 停在空闲的自动战斗单作业页面。")

        def action(frame, name, **values):
            nonlocal action_outcome, load_baseline
            if cancel.is_set():
                raise VisualImportError("maa_visual_cancelled", "导入已取消。")
            self.guard()
            source_snapshot = frame['screenshot_id']
            if name in {'click', 'replace_code', 'clear_code'}:
                previous = frame
                frame = self._refresh_for_action(frame, target=values.get('target') if name == 'click' else None,
                                                 cancel_event=cancel,expected=expected,
                                                 reacquire_focus=lambda fresh: action(fresh, 'focus'))
                refs.extend(frame.get('visual_refresh_refs', []))
                if name == 'click':
                    values['x'], values['y'] = click_position(frame, values['target'], previous=previous)
            args = {"action": name, "screenshot_id": frame["screenshot_id"], **values}
            if name == 'replace_code' and self.load_monitor is not None:
                load_baseline = self.load_monitor.capture_baseline(
                    frame['window'], expected['source_document'], expected['code'], getattr(self.supervisor, 'directory', None))
                if load_baseline.get('evidence_ref'):
                    refs.append(load_baseline['evidence_ref'])
            response = self.submit(args, frame, cancel)
            if response.get("evidence_ref"):
                refs.append(response["evidence_ref"])
            receipt = response.get("result") or {}
            if response.get("status") != "completed" or not response.get("submission_entered"):
                if response.get("submission_entered") or response.get("status") == "unknown":
                    action_outcome = "unknown"
                code = response.get("code") or response.get("error") or "maa_visual_action_failed"
                messages = {
                    "not_foreground": "MAA 自动激活后未保持前台，导入已停止。",
                    "target_occluded": "MAA 输入位置被遮挡，导入已停止。",
                    "ordinary_submissions_closed": "当前 MAA 控制权限已撤销，或有上次操作待核对。",
                    "geometry_changed": "MAA 窗口位置或尺寸改变，导入已停止。",
                    "observation_expired": "MAA 画面已过期，导入已停止。",
                }
                raise VisualImportError(code, messages.get(code, f"MAA 键鼠操作未确认（{code}）。"))
            if name != 'focus' or receipt.get('caption_click') is True:
                action_outcome = "unverified"
            actions.append({"action": name, "snapshot": frame["screenshot_id"],
                            "source_snapshot": source_snapshot, "refresh": frame.get('visual_refresh'),
                            "operation_id": response.get("operation_id"),
                            "code": args.get("code"), "receipt": receipt})

        def click_position(frame, target, *, previous=None):
            checked_page(frame,mode_change=target=='multiple_jobs')
            control = frame.get("targets", {}).get(target)
            if not control:
                raise VisualImportError("maa_visual_target", f"截图中无法唯一定位 MAA 控件（{target}）。")
            point = control["point"]
            x, y = round(point["x"]), round(point["y"])
            box = control['rect']
            if not (box['x'] <= x < box['x'] + box['width']
                    and box['y'] <= y < box['y'] + box['height']):
                raise VisualImportError("maa_visual_target", f"MAA 控件位置不在当前测量区域（{target}）。")
            forbidden = list(frame['forbidden_regions'])
            if previous is not None:
                forbidden.extend(previous['forbidden_regions'])
            if any(rect["x"] - 3 <= x <= rect["x"] + rect["width"] + 3
                   and rect["y"] - 3 <= y <= rect["y"] + rect["height"] + 3
                   for region in forbidden for rect in (region.get("rect", region),)):
                raise VisualImportError("maa_visual_unsafe_target", "识别位置与开始或停止控件相交。")
            return x, y

        def click(frame, target):
            x, y = click_position(frame, target)
            action(frame, "click", x=x, y=y, target=target)

        try:
            visual = getattr(self.readback, 'requires_frame_refresh', False) is True
            readback_timeout = getattr(self.readback, 'import_readback_timeout', 120) if visual else 22
            if (type(readback_timeout) not in (int, float) or not math.isfinite(readback_timeout)
                    or not 0 < readback_timeout <= (120 if visual else 22)):
                raise VisualImportError('maa_visual_readback_limit', 'MAA 导入回读时限无效。')
            code = expected.get("code", "")
            if (not re.fullmatch(r"prts://[1-9][0-9]{0,11}", code)
                    or not expected.get("title") or not expected.get("stage_code")):
                raise VisualImportError("maa_visual_candidate", "缺少已核对的神秘代码、关卡或作业标题。")
            if (not isinstance(options, dict)
                    or any(key not in {"auto_formation", "ignore_requirements", "use_support"}
                           or type(value) is not bool for key, value in options.items())):
                raise VisualImportError("maa_visual_options", "编队选项必须来自当前任务的明确设置。")
            if self.activate_before_capture:
                # Identity-only focus may restore a minimized client. It cannot
                # address page controls and has no invented screenshot ID.
                self.guard()
                health=self.supervisor.health() if callable(getattr(self.supervisor,'health',None)) else {}
                if health.get('revoked') and not health.get('unknown'):
                    frame=observe()
                    checked_page(frame,mode_change=True)
                else:
                    frame={'screenshot_id':None,'window':dict(self.supervisor.target_identity),
                           'native_identity_only':True,'idle':False}
                self.progress('inspect_maa','tool_started',{'summary':'正在自动恢复并激活 MAA 窗口。'})
                action(frame,'focus')
            frame = observe()
            checked_page(frame,mode_change=True)
            if frame["permission"].get("input_allowed") is not True:
                raise VisualImportError("maa_visual_integrity",
                                        "当前执行器的 Windows 权限不足以向 MAA 发送键鼠输入。")
            if not frame["window"].get("foreground") and not self.activate_before_capture:
                self.progress('inspect_maa','tool_started',{'summary':'正在自动恢复并激活 MAA 窗口。'})
                action(frame, "focus")
                frame = observe()
                checked_page(frame,mode_change=True)
            if cancel.is_set():
                raise VisualImportError("maa_visual_cancelled", "导入已取消。")
            if frame["permission"].get("input_allowed") is not True:
                raise VisualImportError("maa_visual_integrity",
                                        "当前执行器的 Windows 权限不足以向 MAA 发送键鼠输入。")
            # Native focus already tries SetForegroundWindow and a verified
            # caption click within its bounded action. Only a fresh foreground
            # capture permits page input, including changing multi-job mode.
            # A denied attempt must not become a manual foreground wait/retry.
            if not frame["window"].get("foreground") or frame["window"].get("minimized"):
                raise VisualImportError("maa_visual_not_foreground", "MAA 自动激活未确认，导入已停止。")
            # Switching the visible mode preserves the existing MAA queue;
            # never press its clear/delete controls to obtain a single field.
            multiple=frame.get('options',{}).get('multiple_jobs',{}).get('state')
            if multiple is True:
                click(frame,'multiple_jobs')
                # SendInput acknowledges dispatch, not WPF's next render.
                # Observe a bounded number of fresh frames; never click again
                # merely because the first post-click capture is still old.
                for _ in range(3):
                    if cancel.wait(.15):
                        raise VisualImportError('maa_visual_cancelled','导入已取消。')
                    frame=observe()
                    checked_page(frame,mode_change=True)
                    if frame.get('single_job') is True:break
            checked_page(frame)
            if frame["permission"].get("input_allowed") is not True:
                raise VisualImportError("maa_visual_integrity",
                                        "当前执行器的 Windows 权限不足以向 MAA 发送键鼠输入。")
            health = self.supervisor.health()
            if health.get("reason") == "focus_denied" and health.get("helper_exit_confirmed"):
                # This failure can only occur on the old focus-only action,
                # which calls SetForegroundWindow without mouse or keyboard.
                self.supervisor.reconcile({"reconciled": True, "evidence_ref": refs[-1],
                                           "kind": "focus_only_failed"}, resume=True)
            for key, desired in options.items():
                state = frame.get("options", {}).get(key, {}).get("state")
                if type(state) is not bool:
                    raise VisualImportError("maa_visual_option_unknown", f"无法读回编队开关（{key}）。")
                if state != desired:
                    click(frame, key)
                    # A native click receipt precedes WPF's asynchronous render.
                    # Match the fixed MaaFramework pipeline's post-action delay,
                    # then read fresh frames only; never toggle a second time.
                    for _ in range(3):
                        if cancel.wait(.2):
                            raise VisualImportError("maa_visual_cancelled", "导入已取消。")
                        frame = observe()
                        if cancel.is_set():
                            raise VisualImportError("maa_visual_cancelled", "导入已取消。")
                        self.guard()
                        checked_page(frame)
                        if frame.get("permission", {}).get("input_allowed") is not True:
                            raise VisualImportError("maa_visual_integrity",
                                                    "当前执行器的 Windows 权限不足以向 MAA 发送键鼠输入。")
                        if frame.get("options", {}).get(key, {}).get("state") is desired:
                            break
                    if frame.get("options", {}).get(key, {}).get("state") is not desired:
                        raise VisualImportError("maa_visual_option_unverified", f"编队开关未确认（{key}）。")
            self.progress("maa_act", "tool_started", {})
            click(frame, "code_input")
            frame = observe()
            checked_page(frame)
            if (frame.get("field_empty") is not True or frame.get("expected_title_visible")
                    or frame.get('operator_count') is not None):
                action(frame, "clear_code")
                for _ in range(3):
                    frame = observe()
                    checked_page(frame)
                    if (frame.get('field_empty') is True and not frame.get('expected_title_visible')
                            and frame.get('operator_count') is None):
                        break
            if (frame.get("field_empty") is not True or frame.get("expected_title_visible")
                    or frame.get('operator_count') is not None):
                raise VisualImportError("maa_visual_old_preview", "尚未看到空输入框和旧作业预览清除。")
            before = frame
            action(before, "replace_code", code=code)
            self.progress("maa_act", "tool_finished", {"status": "control_dispatched"})
            self.progress("verify_import", "tool_started", {})
            frames, code_entry = [], None
            # MAA downloads asynchronously. Poll screenshots only, never retype.
            deadline = time.monotonic() + readback_timeout
            settling_retries = 2
            while len(frames) < 5 and time.monotonic() < deadline:
                if cancel.wait(.25):
                    raise VisualImportError("maa_visual_cancelled", "导入已取消，需查看 MAA 当前状态。")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    frame = observe(recognition_timeout=remaining if visual else None)
                except _UnsettledVisualObservation as error:
                    # Import may temporarily disable controls while downloading.
                    # Keep the rejected evidence, but obtain at most two fresh
                    # observations within the original deadline. No input retry.
                    if settling_retries <= 0:
                        raise
                    refs.extend(error.evidence_refs)
                    settling_retries -= 1
                    self.guard()
                    self.progress('verify_import', 'tool_started', {
                        'summary': 'MAA 加载画面尚未稳定，正在重新读取截图核对；不重复输入作业。'})
                    continue
                frames.append(frame)
                # Only an actual captured/recognized field echo can bind code
                # entry to later previews whose field became a local cache path.
                if code_entry is None and code in frame.get('codes', []):
                    code_entry = frame
                    refs.append(self.publish('maa-visual-code-entry', frame))
                application_receipt = None
                if self.load_monitor is not None:
                    load = self.load_monitor.collect_receipt(
                        load_baseline, frame['window'], self.supervisor.directory)
                    for ref in load.get('evidence_refs', []):
                        if ref not in refs:
                            refs.append(ref)
                    if load.get('status') == 'pending':
                        last_check = {'verified': False, 'reasons': [load.get('code', 'application_load_pending')]}
                        continue
                    if load.get('verified') is not True or load.get('status') != 'verified':
                        raise VisualImportError('maa_application_receipt_rejected',
                                                'MAA 本次新增的作业加载凭据未核实：' + str(load.get('code', 'unknown')))
                    load_receipt_ref = load['receipt_ref']
                    receipt_path = Path(load_receipt_ref['path']).resolve(strict=True)
                    if (not receipt_path.is_relative_to(Path(self.supervisor.directory).resolve())
                            or receipt_path.stat().st_size > 32 * 1024 * 1024):
                        raise VisualImportError('maa_application_receipt_invalid', 'MAA 加载凭据路径或大小无效。')
                    receipt_raw = receipt_path.read_bytes()
                    if hashlib.sha256(receipt_raw).hexdigest() != load_receipt_ref['sha256']:
                        raise VisualImportError('maa_application_receipt_invalid', 'MAA 加载凭据发生变化。')
                    application_receipt = json.loads(receipt_raw)
                    if load_receipt_ref not in refs:
                        refs.append(load_receipt_ref)
                last_check = verify_import(before, frames, expected=expected, code_entry=code_entry,
                                           load_receipt=application_receipt)
                if frame.get("errors"):
                    break
                if last_check["verified"] and all(frame.get("options", {}).get(k, {}).get("state") is v
                                                  for k, v in options.items()):
                    proof = self.publish("maa-visual-import-proof", {
                        **last_check["evidence"], "actions": actions, "expected": expected,
                        **({'load_receipt_ref': load_receipt_ref} if load_receipt_ref is not None else {}),
                        "observation_refs": list(refs), "battle_start_submitted": False})
                    self.progress("verify_import", "tool_finished", {"status": "import_verified"})
                    return {"status": "import_verified", "transport": "screenshot_keyboard",
                            "summary": f"MAA 已加载 {expected['stage_code']} 作业 {code}，已截图核对新预览。",
                            "mystery_code": code, "receipt_ref": proof, "evidence_refs": [*refs, proof],
                            "battle_start_submitted": False, "action_outcome": "verified"}
            reasons = ", ".join((last_check or {}).get("reasons", []))
            raise VisualImportError("maa_visual_readback_unverified",
                                    f"已发送神秘代码，但新预览尚未核实。{reasons}")
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, ImportError) as error:
            refs.extend(getattr(error, 'evidence_refs', []))
            self.progress("verify_import", "tool_finished", {"status": "failed", "summary": str(error)[:500]})
            return {"status": "unknown", "transport": "screenshot_keyboard",
                    "code": getattr(error, "code", "maa_visual_import_blocked"),
                    "summary": str(error)[:600], "evidence_refs": refs,
                    "battle_start_submitted": False, "action_outcome": action_outcome}

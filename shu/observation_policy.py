"""Local pixel-change gating; never a game-state or success recognizer.

Inspired by MaaFramework's bounded wait-for-freeze and recognition rate limits:
https://github.com/MaaXYZ/MaaFramework/blob/main/docs/en_us/3.1-PipelineProtocol.md
Original captures/coordinates remain untouched. Only tiny RGB samples are cached.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
from pathlib import Path
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from .windows import WindowError


def window_identity(observation: dict) -> dict:
    return {key: observation.get(key) for key in ('window_id', 'window_pid', 'width', 'height', 'client_rect')}


class ObservationPolicy:
    """Wait locally for useful new pixels before paying for another decision.

    Similar pixels are neither semantic recognition nor proof of completion.
    Loading references are learned only from explicit model loading labels and
    only influence a bounded read-only wait, never an input decision.
    """

    def __init__(self, *, clock=time.monotonic, poll_seconds=.18,
                 settle_seconds=1.4, unchanged_seconds=2., loading_seconds=6.):
        self.clock = clock
        self.poll_seconds = max(.05, min(.5, poll_seconds))
        self.settle_seconds = max(.1, min(3., settle_seconds))
        self.unchanged_seconds = max(.1, min(5., unchanged_seconds))
        self.loading_seconds = max(.1, min(10., loading_seconds))
        self._samples = OrderedDict()
        self._loading = []

    def sample(self, observation):
        sample = observation.get('sample_rgb')
        if isinstance(sample, (bytes, bytearray, memoryview)) and len(sample) == 64*36*3:
            pixels = bytes(sample)
            return {'pixels': pixels, 'fingerprint': hashlib.blake2b(pixels, digest_size=12).hexdigest()}
        path = Path(observation.get('path', ''))
        try:
            stat = path.stat()
            key = (str(path), stat.st_size, stat.st_mtime_ns)
        except (OSError, ValueError):
            return None
        if key in self._samples:
            self._samples.move_to_end(key)
            return self._samples[key]
        image = QImage(str(path))
        if image.isNull() or (image.width(), image.height()) != (observation.get('width'), observation.get('height')):
            return None
        image = image.scaled(64, 36, Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation).convertToFormat(QImage.Format.Format_RGB888)
        pixels = bytes(image.constBits())
        value = {'pixels': pixels, 'fingerprint': hashlib.blake2b(pixels, digest_size=12).hexdigest()}
        self._samples[key] = value
        while len(self._samples) > 32:
            self._samples.popitem(last=False)
        return value

    @staticmethod
    def _delta(left, right):
        differences = [abs(a-b) for a, b in zip(left['pixels'], right['pixels'])]
        mean = sum(differences)/(len(differences)*255.)
        changed = sum(sum(differences[i:i+3]) > 54 for i in range(0, len(differences), 3))/(len(differences)//3)
        return {'delta': round(mean, 5), 'changed_fraction': round(changed, 5),
                'changed': mean > .04 or changed > .12}

    def compare(self, previous, current):
        if window_identity(previous) != window_identity(current):
            return {'available': False, 'identity_changed': True}
        left, right = self.sample(previous), self.sample(current)
        if left is None or right is None:
            return {'available': False, 'identity_changed': False}
        return {'available': True, 'identity_changed': False, **self._delta(left, right),
                'fingerprint': right['fingerprint']}

    def mark_loading(self, observation):
        value = self.sample(observation)
        if value is not None:
            self._loading.append(value)
            del self._loading[:-8]

    def settle(self, initial, capture, check, wait, *, baseline=None, mode='after_input', peek=None):
        """Return a fresh capture plus telemetry, or original if pixels unavailable.

        capture/check/wait are owned by the executor and carry cancellation,
        identity and total-deadline enforcement. No OS input or model is called.
        """
        first = self.sample(initial)
        if first is None:
            return initial, {'available': False, 'local_polls': 0}
        check()
        loading = any(not self._delta(item, first)['changed'] for item in self._loading)
        need_change = mode in {'wait', 'loading'} or loading
        budget = self.loading_seconds if mode == 'loading' or loading else (
            self.unchanged_seconds if mode == 'wait' else self.settle_seconds)
        started = self.clock()
        current, anchor = initial, baseline or initial
        stable = False
        polls = 0
        sample_polls = 0
        sample_mode_used = False
        sequence = initial.get('frame_sequence', 0)
        generation = initial.get('stream_generation')
        relation = self.compare(anchor, current)
        if relation.get('identity_changed'):
            raise WindowError('本地观察期间窗口身份或几何发生变化，请重新观察。', 'geometry_changed')
        while self.clock()-started < budget:
            check()
            wait(min(self.poll_seconds, max(0., budget-(self.clock()-started))))
            if peek is not None:
                try:
                    fresh = peek(after_sequence=sequence, not_before=started)
                except WindowError as exc:
                    check()
                    if exc.code != 'capture_stream_timeout':
                        raise
                    sample_mode_used = True
                    continue
                check()
                if fresh is None:
                    peek = None
                    fresh = capture()
                else:
                    if fresh.get('actionable') is not False or self.sample(fresh) is None:
                        raise WindowError('后台采样返回无效数据，未将其作为游戏观察。', 'invalid_sample')
                    if generation is not None and fresh.get('generation') != generation:
                        raise WindowError('本地采样期间捕获会话已变化。', 'binding_changed')
                    sequence = fresh['sequence']
                    sample_polls += 1
                    sample_mode_used = True
            else:
                fresh = capture()
            polls += 1
            step = self.compare(current, fresh)
            relation = self.compare(anchor, fresh)
            if step.get('identity_changed') or relation.get('identity_changed'):
                raise WindowError('本地观察期间窗口身份或几何发生变化，请重新观察。', 'geometry_changed')
            if not step.get('available') or not relation.get('available'):
                current = fresh
                break
            current = fresh
            stable = not step['changed']
            if stable and (not need_change or relation['changed']):
                break
        check()
        if sample_mode_used:
            # Samples have no observation IDs and are never actionable. Always
            # obtain one actual fresh evidence image before the next decision.
            final = capture()
            step, relation = self.compare(current, final), self.compare(anchor, final)
            if step.get('identity_changed') or relation.get('identity_changed'):
                raise WindowError('正式观察与后台采样的窗口已变化。', 'geometry_changed')
            stable = bool(step.get('available') and not step['changed'])
            current = final
        return current, {'available': True, 'local_polls': polls, 'stable_pixels': stable,
                         'sample_only_polls': sample_polls,
                         'semantic_success': False, 'wait_reason': 'loading' if loading or mode == 'loading' else mode,
                         'elapsed_seconds': round(self.clock()-started, 3), **relation}

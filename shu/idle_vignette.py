"""A bounded, interruptible idle scene; independent of normal atlas actions."""
from __future__ import annotations


class IdleVignetteClock:
    """Enter once after quiet time, hold, exit, then require quiet time again.

    All timestamps are supplied by the caller, so playback and the two-minute
    threshold can be verified without sleeping or changing production timing.
    """
    def __init__(self, *, now: float = 0.0, idle_seconds: float = 120.0,
                 enter_frames: int = 20, exit_frames: int = 10,
                 enter_frame_ms: int = 100, exit_frame_ms: int = 100,
                 hold_ms: int = 4000, hold_frames: int = 0,
                 hold_frame_ms: int = 125):
        if (idle_seconds <= 0 or enter_frames < 1 or exit_frames < 1
                or enter_frame_ms < 1 or exit_frame_ms < 1 or hold_ms < 0
                or hold_frames < 0 or hold_frame_ms < 1):
            raise ValueError('Invalid idle vignette timing')
        self.idle_seconds = float(idle_seconds)
        self.enter_frames, self.exit_frames = enter_frames, exit_frames
        self.enter_step, self.exit_step = enter_frame_ms / 1000, exit_frame_ms / 1000
        self.hold_seconds = hold_ms / 1000
        self.hold_frames, self.hold_step = hold_frames, hold_frame_ms / 1000
        self.interact(now)

    def interact(self, now: float) -> None:
        self.phase, self.frame = 'idle', 0
        self.deadline = None
        self.hold_until = None
        self.quiet_since = self.last_tick = float(now)

    @property
    def frame_reference(self) -> tuple[str, int] | None:
        if self.phase == 'idle':return None
        if self.phase == 'hold' and not self.hold_frames:return ('enter', self.enter_frames - 1)
        return self.phase, self.frame

    def advance(self, now: float, *, eligible: bool = True) -> tuple[str, int] | None:
        now = float(now)
        if not eligible or now < self.last_tick or (self.phase != 'idle' and now - self.last_tick > 5):
            # Hidden/busy time is not credit; long stalls must not flash a
            # skipped scene or leave a cat stranded in the pet's hands.
            self.interact(now)
            return None
        self.last_tick = now
        if self.phase == 'idle':
            if now + 1e-9 < self.quiet_since + self.idle_seconds:return None
            self.phase, self.frame, self.deadline = 'enter', 0, now + self.enter_step
            return self.frame_reference
        while now + 1e-9 >= self.deadline:
            if self.phase == 'enter':
                self.frame += 1
                if self.frame == self.enter_frames:
                    self.phase, self.frame = 'hold', 0
                    self.hold_until = self.deadline + self.hold_seconds
                    self.deadline = (min(self.deadline + self.hold_step, self.hold_until)
                                     if self.hold_frames else self.hold_until)
                else:self.deadline += self.enter_step
            elif self.phase == 'hold':
                # Frame looping never changes the total hold deadline. A late
                # timer tick therefore cannot extend the visit or trap the cat.
                if now + 1e-9 >= self.hold_until:
                    self.phase, self.frame = 'exit', 0
                    self.deadline = self.hold_until + self.exit_step
                else:
                    self.frame = (self.frame + 1) % self.hold_frames
                    self.deadline = min(self.deadline + self.hold_step, self.hold_until)
            else:
                self.frame += 1
                if self.frame == self.exit_frames:
                    self.interact(now)
                    return None
                self.deadline += self.exit_step
        return self.frame_reference

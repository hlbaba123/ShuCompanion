"""Sprite layout, playback and portable preferences for Shu Desktop Pet."""
from __future__ import annotations

import json
import math
from pathlib import Path

CELL_WIDTH, CELL_HEIGHT = 192, 208
ATLAS_SIZE = (1536, 2288)
SIZES = (220, 280, 340)
DEFAULT_SIZE = 280
STATES = {
    "idle": (0, (280, 110, 110, 140, 140, 320)),
    "running-right": (1, (120,) * 8),
    "running-left": (2, (120,) * 8),
    "waving": (3, (140, 140, 140, 280)),
    "jumping": (4, (140, 140, 140, 140, 280)),
    "failed": (5, (140,) * 7 + (240,)),
    "waiting": (6, (150,) * 5 + (260,)),
    "running": (7, (120,) * 5 + (220,)),
    "review": (8, (150,) * 5 + (280,)),
}
LABELS = {
    "idle": "安静陪伴", "waving": "挥手打招呼", "jumping": "跳一下",
    "running-right": "向右跑步", "running-left": "向左跑步",
    "failed": "有点失落", "waiting": "等你回应", "running": "认真思考", "review": "检查成果",
}


def look_cell(dx: float, dy: float) -> tuple[int, int]:
    """Clockwise sectors in screen coordinates: up=0, right=90 degrees."""
    degrees = math.degrees(math.atan2(dx, -dy)) % 360
    index = int((degrees + 11.25) // 22.5) % 16
    return 9 + index // 8, index % 8


def clamp_position(x: int, y: int, width: int, height: int,
                   area: tuple[int, int, int, int]) -> tuple[int, int]:
    left, top, area_width, area_height = area
    return (max(left, min(x, left + max(0, area_width - width))),
            max(top, min(y, top + max(0, area_height - height))))


class Animation:
    def __init__(self, now: float = 0.0):
        self.play("idle", now, loop=True)

    def play(self, state: str, now: float, loop: bool = False) -> None:
        if state not in STATES:
            raise ValueError(f"Unknown state: {state}")
        # Pointer motion can request the same looping action many times between
        # two frames. Keep its clock running rather than restarting frame zero.
        if loop and getattr(self, "loop", False) and getattr(self, "state", None) == state:
            return
        self.state, self.frame, self.loop = state, 0, loop
        self.deadline = now + STATES[state][1][0] / 1000

    @property
    def cell(self) -> tuple[int, int]:
        return STATES[self.state][0], self.frame

    def advance(self, now: float) -> None:
        if now - self.deadline > 5:
            # Resume calmly after sleep or a long system stall.
            self.deadline = now + STATES[self.state][1][self.frame] / 1000
            return
        while now >= self.deadline:
            self.frame += 1
            durations = STATES[self.state][1]
            if self.frame == len(durations):
                if not self.loop:
                    self.play("idle", now, loop=True)
                    return
                self.frame = 0
            self.deadline += durations[self.frame] / 1000


def load_preferences(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
    except (OSError, ValueError):
        return {}
    result = {}
    if raw.get("size") in SIZES:
        result["size"] = raw["size"]
    for key in ("animated", "follow_cursor", "always_on_top"):
        if isinstance(raw.get(key), bool):
            result[key] = raw[key]
    pos = raw.get("position")
    if (isinstance(pos, list) and len(pos) == 2
            and all(type(v) is int and -100000 <= v <= 100000 for v in pos)):
        result["position"] = pos
    return result


def save_preferences(path: Path, preferences: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(".tmp")
    staging.write_text(json.dumps(preferences, ensure_ascii=False, indent=2), encoding="utf-8")
    staging.replace(path)

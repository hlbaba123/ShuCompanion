"""Local skin resources and a bounded nine-frame outfit change.

The original atlas is always retained as a fallback. Appearance preferences
have their own file so changing an outfit never rewrites model/voice settings.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile


LEGACY_SKIN = "legacy-original"
TRANSITION_OUT_FRAMES = 4
TRANSITION_IN_FRAMES = 5
TRANSITION_FRAME_MS = 100


@dataclass(frozen=True)
class SpriteClip:
    path: Path
    columns: int
    frames: int
    frame_ms: int = 100


@dataclass(frozen=True)
class IdleVignetteSpec:
    enter: SpriteClip
    exit: SpriteClip
    idle_seconds: float = 120
    hold_ms: int = 4000
    hold: SpriteClip | None = None


@dataclass(frozen=True)
class Skin:
    id: str
    label: str
    atlas: Path
    transition_out: Path | None = None
    transition_in: Path | None = None
    idle_vignette: IdleVignetteSpec | None = None


def _local_resource(root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        return None
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        return None
    return path


def _clip(root: Path, raw: object) -> SpriteClip | None:
    if not isinstance(raw, dict):return None
    path = _local_resource(root, raw.get('file'))
    columns, frames, frame_ms = raw.get('columns'), raw.get('frames'), raw.get('frame_ms', 100)
    if (path is None or type(columns) is not int or not 1 <= columns <= 16
            or type(frames) is not int or not 1 <= frames <= 128
            or columns > frames or type(frame_ms) is not int or not 20 <= frame_ms <= 1000):return None
    return SpriteClip(path, columns, frames, frame_ms)


def _idle_vignette(root: Path, raw: object) -> IdleVignetteSpec | None:
    if not isinstance(raw, dict):return None
    enter, exit = _clip(root, raw.get('enter')), _clip(root, raw.get('exit'))
    hold_clip = _clip(root, raw.get('hold')) if raw.get('hold') is not None else None
    wait, hold_ms = raw.get('idle_seconds', 120), raw.get('hold_ms', 4000)
    if (enter is None or exit is None or (raw.get('hold') is not None and hold_clip is None)
            or type(wait) not in (int, float)
            or not 30 <= wait <= 3600 or type(hold_ms) is not int or not 0 <= hold_ms <= 30000):return None
    return IdleVignetteSpec(enter, exit, float(wait), hold_ms, hold_clip)


class SkinCatalog:
    def __init__(self, original_atlas: Path, manifest: Path | None = None):
        original_atlas = Path(original_atlas).resolve()
        self.legacy = Skin(LEGACY_SKIN, "原版 · 保留旧动作", original_atlas)
        self.skins = {LEGACY_SKIN: self.legacy}
        self.default_id = LEGACY_SKIN
        manifest = manifest or original_atlas.parent / "skins" / "manifest.json"
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict) or raw.get("version") != 1:
                return
            entries = raw.get("skins", [])
            if not isinstance(entries, list):
                return
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                skin_id, label = entry.get("id"), entry.get("label")
                if (not isinstance(skin_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", skin_id)
                        or skin_id in self.skins or not isinstance(label, str) or not label.strip()):
                    continue
                atlas = _local_resource(manifest.parent, entry.get("atlas"))
                if atlas is None:
                    continue
                self.skins[skin_id] = Skin(skin_id, label.strip(), atlas,
                    _local_resource(manifest.parent, entry.get("transition_out")),
                    _local_resource(manifest.parent, entry.get("transition_in")),
                    _idle_vignette(manifest.parent, entry.get('idle_vignette')))
            if raw.get("default_skin") in self.skins:
                self.default_id = raw["default_skin"]
            # Public editions can omit the historical outfit from selection.
            # Keep the base atlas as an emergency fallback, even when hidden.
            if raw.get("include_legacy") is False and len(self.skins) > 1:
                self.skins.pop(LEGACY_SKIN)
                if self.default_id == LEGACY_SKIN:
                    self.default_id = next(iter(self.skins))
        except (OSError, ValueError, TypeError):
            pass

    def get(self, skin_id: str) -> Skin | None:
        return self.skins.get(skin_id)

    def ordered(self) -> list[Skin]:
        return [skin for key, skin in self.skins.items() if key != LEGACY_SKIN] + (
            [self.legacy] if LEGACY_SKIN in self.skins else [])


class SkinChange:
    """Source frames 0..3 followed by destination frames 0..4, once."""
    def __init__(self, now: float):
        self.frame = 0
        self.done = False
        self.deadline = now + TRANSITION_FRAME_MS / 1000

    def advance(self, now: float) -> None:
        while not self.done and now >= self.deadline:
            self.frame += 1
            self.done = self.frame >= TRANSITION_OUT_FRAMES + TRANSITION_IN_FRAMES
            self.deadline += TRANSITION_FRAME_MS / 1000


def load_skin_preference(path: Path) -> str | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        value = raw.get("skin_id") if isinstance(raw, dict) else None
        return value if isinstance(value, str) else None
    except (OSError, ValueError):
        return None


def save_skin_preference(path: Path, skin_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"skin_id": skin_id}, ensure_ascii=False, indent=2)
    temporary = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                prefix=f".{path.name}.", suffix=".tmp", delete=False) as staging:
            temporary = Path(staging.name)
            staging.write(data)
        temporary.replace(path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

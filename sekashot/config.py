"""Persistent configuration for Sekashot.

Settings live in ``~/.config/sekashot/config.json`` and mirror the options
Lightshot exposes (default colour, pen width, save folder, ...).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import APP_NAME


def _xdg(env_var: str, fallback: Path) -> Path:
    value = os.environ.get(env_var)
    return Path(value) if value else fallback


def config_dir() -> Path:
    path = _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    path = _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share") / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_dir() -> Path:
    path = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_save_dir() -> str:
    pictures = Path.home() / "Pictures"
    if pictures.is_dir():
        return str(pictures)
    return str(Path.home())


@dataclass
class Config:
    """All tunable options. Unknown keys on disk are ignored, new keys get defaults."""

    # --- saving -------------------------------------------------------
    save_dir: str = ""
    save_format: str = "png"            # "png" | "jpg"
    jpg_quality: int = 92
    auto_save: bool = False
    auto_save_dir: str = ""

    # --- drawing defaults (Lightshot defaults to a red pen) -----------
    color: str = "#ff0000"
    pen_width: int = 2
    marker_width: int = 14
    marker_alpha: int = 110
    blur_radius: int = 12
    font_family: str = "Sans"
    font_size: int = 18
    rect_fill: bool = False

    # --- overlay ------------------------------------------------------
    dim_alpha: int = 140
    show_size_badge: bool = True
    marching_ants: bool = True
    capture_focused_output: bool = True

    # --- capture ------------------------------------------------------
    #: Accept a capture that comes back entirely black.  Off by default: an
    #: all-black frame almost always means the capture backend failed (no
    #: compositor on X11, or no 'grim'/portal helper on Wayland) rather than
    #: that the screen really is black.
    allow_black_capture: bool = False

    # --- misc ---------------------------------------------------------
    copy_format: str = "image"          # "image" | "file-url"
    play_sound: bool = True
    hotkey: str = "Print"

    def __post_init__(self) -> None:
        if not self.save_dir:
            self.save_dir = default_save_dir()
        if not self.auto_save_dir:
            self.auto_save_dir = self.save_dir

    # ------------------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return config_dir() / "config.json"

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self) -> None:
        path = self.config_path
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls) -> "Config":
        path = config_dir() / "config.json"
        data: dict = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in known}
        try:
            return cls(**clean)
        except TypeError:
            return cls()

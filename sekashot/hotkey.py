"""Global hotkey handling.

Qt cannot register system-wide shortcuts on Wayland, so the strategy is:

* **X11**   - grab the key with python-xlib (preferred) or pynput.
* **Wayland** - the compositor owns the key; we can't grab it.  Instead the
  user (or :func:`install_compositor_keybind`) binds a key to
  ``sekashot --capture``.  We detect the compositor and emit ready-to-paste
  config snippets.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from .capture import compositor_name, is_wayland

try:
    from Xlib import X, display as xdisplay, XK  # type: ignore

    _HAVE_XLIB = True
except Exception:
    _HAVE_XLIB = False


def _keysym_to_keycode(display, name: str) -> int | None:
    from Xlib import XK as _XK  # type: ignore

    attr = f"XK_{name.lower()}"
    keysym = getattr(_XK, attr, None)
    if keysym is None:
        special = {
            "print": 0xFF61,
            "printscreen": 0xFF61,
            "ctrl+print": 0xFF61,
        }
        keysym = special.get(name.lower())
    if keysym is None:
        return None
    return display.keysym_to_keycode(keysym)


class X11Hotkey:
    """Polls an X11 key grab in a background thread."""

    #: key combinations to try, most natural first
    COMBOS: tuple[tuple[str, int], ...] = (
        ("", 0),
        ("Alt+", X.Mod1Mask),
        ("Mod3+", X.Mod3Mask),
        ("Super+", X.Mod4Mask),
        ("Alt+Super+", X.Mod1Mask | X.Mod4Mask),
    )

    def __init__(self, key_name: str, callback: Callable[[], None]) -> None:
        self.key_name = key_name
        self.callback = callback
        self._thread = None
        self._display = None
        self._running = False
        #: the combinations that are actually ours, e.g. ``["Alt+Print"]``
        self.combos: list[str] = []

    def start(self) -> bool:
        if not _HAVE_XLIB:
            return False
        import threading

        try:
            self._display = xdisplay.Display()
        except Exception:
            return False

        screen = self._display.screen()
        root = screen.root
        keycode = None
        candidates = (self.key_name, self.key_name.lower(), "Print", "PrintScreen")
        for candidate in dict.fromkeys(candidates):  # keep the order, drop duplicates
            keycode = _keysym_to_keycode(self._display, candidate)
            if keycode:
                break
        if not keycode:
            return False

        # Another client - usually the desktop environment, which binds Print
        # Screen itself - may already own the key.  XGrabKey does not fail at
        # the call: the server answers later, at sync() time, with BadAccess.
        # The default handler only prints that error, so the grab would look
        # successful while it is not.  Collect the errors instead.
        errors: list[object] = []
        self._display.set_error_handler(lambda err, _req: errors.append(err))

        grabbed: list[str] = []
        for label, mod in self.COMBOS:
            errors.clear()
            try:
                root.grab_key(keycode, mod, True, X.GrabModeAsync, X.GrabModeAsync)
                self._display.sync()
            except Exception:
                continue
            if errors:
                # the key combination belongs to somebody else
                continue
            grabbed.append(f"{label}{self.key_name}")

        if not grabbed:
            try:
                self._display.close()
            except Exception:
                pass
            self._display = None
            return False

        self.combos = grabbed
        self._running = True

        def loop() -> None:
            while self._running:
                try:
                    event = self._display.next_event()
                except Exception:
                    break
                # a grab also delivers the key release - only the press counts
                if event.type != X.KeyPress:
                    continue
                try:
                    self.callback()
                except Exception:
                    pass

        self._thread = threading.Thread(target=loop, name="sekashot-hotkey", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        try:
            if self._display:
                self._display.close()
        except Exception:
            pass


class PynputHotkey:
    def __init__(self, key_name: str, callback: Callable[[], None]) -> None:
        self.key_name = key_name
        self.callback = callback
        self._listener = None

    def start(self) -> bool:
        try:
            from pynput import keyboard  # type: ignore
        except Exception:
            return False
        spec = self.key_name.lower()
        if spec in ("print", "printscreen", "print screen"):
            spec = "print_screen"
        try:
            self._listener = keyboard.GlobalHotKeys({f"<{spec}>": self.callback})
            self._listener.daemon = True
            self._listener.start()
            return True
        except Exception:
            return False

    def stop(self) -> None:
        try:
            if self._listener:
                self._listener.stop()
        except Exception:
            pass


def install_global_hotkey(key_name: str, callback: Callable[[], None]):
    """Return ``(backend, message)``; *backend* is ``None`` when unsupported."""
    if is_wayland():
        return None, wayland_hotkey_hint()
    backend = X11Hotkey(key_name, callback)
    if backend.start():
        combos = backend.combos
        if combos and combos[0] == key_name:
            return backend, f"Global hotkey '{key_name}' grabbed (X11)."
        # the desktop already owns the plain key - say which ones do work
        return backend, (
            f"'{key_name}' is already taken by the desktop environment.\n"
            f"Global hotkey grabbed instead: {', '.join(combos)} (X11)."
        )
    backend = PynputHotkey(key_name, callback)
    if backend.start():
        return backend, f"Global hotkey '{key_name}' grabbed (pynput)."
    return None, (
        "Could not grab a global hotkey. The desktop environment may already\n"
        "own Print Screen, or python3-xlib and python3-pynput are missing.\n"
        "Install python3-xlib, or bind a key in your desktop environment to\n"
        "run 'sekashot --capture'."
    )


# ----------------------------------------------------------------------
# Wayland / compositor specific
# ----------------------------------------------------------------------
NIRI_BIND_FILE = "sekashot.kdl"

#: preferred key combos, most natural first
NIRI_CANDIDATE_KEYS = ("Print", "Mod+Print", "Mod+Shift+Print")


def _key_is_bound(config_text: str, combo: str) -> bool:
    """True when *combo* already has a binding in a niri config."""
    return bool(re.search(r"^\s*" + re.escape(combo) + r"\s*\{", config_text, re.M))


def _existing_bind_text(cfg_dir: Path, own_file: Path) -> str:
    """Every binding defined in the user's niri config, excluding our own file.

    Excluding ``own_file`` matters: otherwise a second run of the installer would
    see the key it wrote last time, treat it as someone else's binding and keep
    escalating to a different key.
    """
    chunks = []
    for path in sorted(cfg_dir.glob("*.kdl")):
        if path == own_file:
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return "\n".join(chunks)


def niri_reload_command() -> str:
    """The reload command for the installed niri version."""
    try:
        proc = subprocess.run(
            ["niri", "msg", "action", "--help"], capture_output=True, timeout=5, check=False
        )
        text = proc.stdout.decode(errors="replace")
        if "load-config-file" in text:
            return "niri msg action load-config-file"
        if "reload-config" in text:
            return "niri msg action reload-config"
    except (OSError, subprocess.SubprocessError):
        pass
    return "niri msg action load-config-file"


def wayland_hotkey_hint() -> str:
    comp = compositor_name()
    if comp == "niri":
        return (
            "Wayland session (niri): the compositor owns global keys, so an app\n"
            "cannot grab Print Screen itself.  Run:\n\n"
            "    sekashot --install-hotkey\n\n"
            "which writes ~/.config/niri/sekashot.kdl and tells you to add\n"
            '    include "sekashot.kdl"\n'
            "to ~/.config/niri/config.kdl."
        )
    snippets = {
        "hyprland": "bind = , Print, exec, sekashot --capture",
        "sway": 'bindsym --to-code Print exec sekashot --capture',
        "river": "riverctl map normal None Print spawn 'sekashot --capture'",
    }
    body = snippets.get(comp)
    if body:
        return f"Wayland session ({comp}): add this to your compositor config:\n\n    {body}"
    return (
        "Wayland session: apps cannot grab global hotkeys.  Bind a key in your\n"
        "desktop settings (Keyboard > Custom Shortcuts) to run:\n\n"
        "    sekashot --capture"
    )


def niri_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "niri"
    return base / "config.kdl"


def install_compositor_keybind(force: bool = False) -> str:
    """Install the compositor keybind where we know how; return a report.

    Existing bindings are respected: niri treats a duplicate key as a conflict,
    so we only emit combos that are currently free.
    """
    comp = compositor_name()
    if comp != "niri":
        return wayland_hotkey_hint()

    cfg_dir = niri_config_path().parent
    cfg_dir.mkdir(parents=True, exist_ok=True)
    bind_file = cfg_dir / NIRI_BIND_FILE

    main = niri_config_path()
    existing = _existing_bind_text(cfg_dir, bind_file)

    conflicts = [k for k in NIRI_CANDIDATE_KEYS if _key_is_bound(existing, k)]
    available = [k for k in NIRI_CANDIDATE_KEYS if k not in conflicts]

    lines: list[str] = []
    if not available:
        # every candidate is taken - fall back to a combo nobody uses
        available = ["Mod+Alt+Print"]
        lines.append("All preferred keys were already bound; using Mod+Alt+Print.")

    # a single binding is enough - take the most natural free key
    chosen = available[0]
    if "Print" in conflicts:
        lines.append(
            "NOTE: 'Print' is already bound in your niri config, so it was left alone\n"
            "      (that key still runs your existing command).\n"
            "      To hand 'Print' over to Sekashot, remove/comment the other binding\n"
            "      and re-run: sekashot --install-hotkey --force"
        )

    content = (
        "// Added by sekashot --install-hotkey\n"
        "// Opens the Sekashot screenshot overlay.\n"
        "binds {\n"
        f'    {chosen} {{ spawn "sekashot" "--capture"; }}\n'
        "}\n"
    )

    existed = bind_file.is_file()
    if not existed or force or bind_file.read_text(encoding="utf-8") != content:
        if existed and not force:
            backup = bind_file.with_suffix(".kdl.bak")
            backup.write_text(bind_file.read_text(encoding="utf-8"), encoding="utf-8")
        bind_file.write_text(content, encoding="utf-8")

    if main.is_file():
        text = main.read_text(encoding="utf-8")
        if NIRI_BIND_FILE not in text:
            backup = main.with_suffix(".kdl.sekashot-bak")
            backup.write_text(text, encoding="utf-8")
            main.write_text(f'{text.rstrip()}\n\ninclude "{NIRI_BIND_FILE}"\n', encoding="utf-8")
            lines.append(f"Added include line to {main} (backup: {backup})")
        else:
            lines.append(f"{main} already includes {NIRI_BIND_FILE}.")
    else:
        lines.append(f"No niri config found at {main}.")

    lines.append(f"Wrote {bind_file}")
    lines.append(f"Binding installed: {chosen}")
    if conflicts:
        lines.append("Left untouched (already bound): " + ", ".join(conflicts))
    lines.append("Reload niri with: " + niri_reload_command())
    if shutil.which("niri") is None:
        lines.append("(note: 'niri' CLI not on PATH, reload from your compositor.)")
    return "\n".join(lines)

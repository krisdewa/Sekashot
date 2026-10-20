"""Screen capture backends.

Wayland sessions cannot be read with Qt's ``grabWindow`` (it returns black), so
we shell out to the compositor's screencopy tool.  The order of preference is:

  Wayland : grim  ->  xdg-desktop-portal Screenshot
  X11     : Qt grabWindow on every screen, composited into one image

The two families are never mixed: on Wayland the X11 grab is *not* a fallback,
because it does not fail loudly - it returns a black rectangle that used to be
handed to the overlay as if it were a real screenshot.  When no backend
produces a usable frame we raise :class:`CaptureError` with an actionable
message instead.

A frame that is entirely black is treated as a failed capture for the same
reason, unless the user opted in with ``allow_black_capture``.

Everything returns a single :class:`QImage` covering the whole virtual desktop,
which is exactly what the overlay needs to freeze the screen.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtGui import QGuiApplication, QImage

try:  # optional, only used for the portal fallback
    from PyQt6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage  # type: ignore

    _HAVE_DBUS = True
except Exception:  # pragma: no cover - QtDBus may be absent in slim builds
    _HAVE_DBUS = False


class CaptureError(RuntimeError):
    pass


def is_wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"


def session_type() -> str:
    return "wayland" if is_wayland() else "x11"


def compositor_name() -> str:
    val = " ".join(
        os.environ.get(k, "") for k in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION")
    ).lower()
    for name in ("niri", "hyprland", "sway", "river", "gnome", "kde", "plasma", "cosmic", "weston", "labwc", "wayfire"):
        if name in val:
            return "kde" if name in ("plasma", "kde") else name
    return val.strip() or "unknown"


@dataclass
class Screenshot:
    image: QImage
    #: geometry of the screenshot in *logical* desktop coordinates, when known.
    #: On Wayland the compositor does not expose this, so it stays ``None`` and
    #: the overlay falls back to scaling the whole image into the window.
    virtual_geometry: QRect | None = None


# ----------------------------------------------------------------------
# Wayland
# ----------------------------------------------------------------------
def _grim_capture(include_cursor: bool = False, output: str | None = None) -> QImage | None:
    """Grab the screen with ``grim``.

    The image is asked for as raw **PPM**, not PNG.  grim spends ~200 ms
    zlib-compressing a 1080p frame while the uncompressed bytes cross the pipe
    and decode in a couple of milliseconds, so PPM is roughly 7x faster end to
    end (30 ms vs 230 ms for one 1080p monitor).  PNG is only used as a fallback
    for a grim build that cannot write PPM.
    """
    if not shutil.which("grim"):
        return None

    scope: list[str] = []
    if not include_cursor:
        scope.append("-c")  # exclude the cursor
    if output:
        scope += ["-o", output]

    def run(fmt: str) -> QImage | None:
        try:
            proc = subprocess.run(
                ["grim", "-t", fmt, *scope, "-"],  # "-" writes to stdout
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0 or not proc.stdout:
            return None
        img = QImage()
        if not img.loadFromData(proc.stdout, fmt.upper()) or img.isNull():
            return None
        return img

    return run("ppm") or run("png")


def _run_json(cmd: list[str]) -> object | None:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        return json.loads(proc.stdout.decode(errors="replace"))
    except ValueError:
        return None


def focused_output_name() -> str | None:
    """Name of the output the cursor/focus is on, when the compositor can tell us.

    Wayland compositors do not expose screen geometry to clients, so knowing the
    focused output is what lets us capture a single monitor and display a 1:1
    overlay on it (which is what Lightshot does per monitor).
    """
    comp = compositor_name()

    if comp == "niri" and shutil.which("niri"):
        try:
            proc = subprocess.run(
                ["niri", "msg", "focused-output"], capture_output=True, timeout=10, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode == 0:
            match = re.search(r'^Output\s+".*"\s+\(([^)]+)\)', proc.stdout.decode(errors="replace"), re.M)
            if match:
                return match.group(1).strip()

    if comp == "sway" and shutil.which("swaymsg"):
        data = _run_json(["swaymsg", "-t", "get_outputs"])
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("focused") and item.get("name"):
                    return str(item["name"])

    if comp in ("hyprland",) and shutil.which("hyprctl"):
        data = _run_json(["hyprctl", "-j", "monitors"])
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("focused") and item.get("name"):
                    return str(item["name"])

    return None



def _grim_outputs() -> list[str]:
    try:
        proc = subprocess.run(["grim", "-t", "png", "-"], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return [l for l in proc.stderr.decode(errors="replace").splitlines() if l.strip()]


def _portal_capture(timeout_ms: int = 15000) -> QImage | None:
    """xdg-desktop-portal Screenshot (non-interactive), via QtDBus if present."""
    if not _HAVE_DBUS:
        return None
    try:
        bus = QDBusConnection.sessionBus()
        iface = QDBusInterface(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Screenshot",
            bus,
        )
        if not iface.isValid():
            return None
        msg = iface.call("Screenshot", "", {"interactive": False, "modal": False})
        if msg.type() != QDBusMessage.MessageType.ReplyMessage or not msg.arguments():
            return None
        handle = str(msg.arguments()[0])
        # Wait for the Request::Response signal and pull the file URI out of it.
        from PyQt6.QtCore import QEventLoop, QTimer  # local import keeps startup light

        loop = QEventLoop()
        state: dict = {"uri": None}

        def on_response(code: int, results: dict) -> None:
            if code == 0:
                state["uri"] = results.get("uri")
            loop.quit()

        bus.connect("org.freedesktop.portal.Desktop", handle, "org.freedesktop.portal.Request", "Response", on_response)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(timeout_ms)
        loop.exec()

        uri = state["uri"]
        if not uri:
            return None
        path = uri.replace("file://", "")
        img = QImage()
        if img.load(path):
            return img
    except Exception:
        return None
    return None


# ----------------------------------------------------------------------
# X11
# ----------------------------------------------------------------------
def _x11_capture() -> Screenshot | None:
    """Grab the X11 root window of every screen, in device pixels.

    ``QScreen.grabWindow()`` returns the *panel's native resolution*: on a
    HiDPI X11 screen a 3840x2160 monitor yields a 3840x2160 image even
    though its logical geometry is only 1920x1080.  The canvas is therefore
    built in device pixels too.  A canvas sized from the logical geometry
    would silently downscale the grab by the scale factor and export a
    1920x1080 copy of a 3840x2160 screen.
    """
    app = QGuiApplication.instance()
    if app is None:
        return None
    screens = QGuiApplication.screens()
    if not screens:
        return None

    virtual = screens[0].geometry()
    for screen in screens[1:]:
        virtual = virtual.united(screen.geometry())

    # One scale factor for the whole desktop.  Qt maps the virtual desktop
    # linearly, so a single factor is exact whenever the screens share the
    # same scale - the normal case.
    dpr = max((s.devicePixelRatio() for s in screens), default=1.0) or 1.0

    canvas = QImage(
        QSize(round(virtual.width() * dpr), round(virtual.height() * dpr)),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    canvas.fill(Qt.GlobalColor.black)

    from PyQt6.QtGui import QPainter

    grabbed = 0
    painter = QPainter(canvas)
    for screen in screens:
        geo = screen.geometry()
        shot = screen.grabWindow(0, 0, 0, geo.width(), geo.height())
        if shot.isNull():
            continue
        # drop the device-pixel-ratio attribute so the pixmap's raw pixels are
        # copied 1:1 instead of being scaled down by the painter
        image = shot.toImage()
        image.setDevicePixelRatio(1.0)
        target = QRect(
            round((geo.left() - virtual.left()) * dpr),
            round((geo.top() - virtual.top()) * dpr),
            round(geo.width() * dpr),
            round(geo.height() * dpr),
        )
        painter.drawImage(target, image)
        grabbed += 1
    painter.end()

    # Not a single screen could be read back.  Returning the canvas here used
    # to hand the overlay a black frame instead of an error.
    if grabbed == 0:
        return None
    return Screenshot(canvas, virtual)


# ----------------------------------------------------------------------
# black frame detection
# ----------------------------------------------------------------------
#: How many samples per axis the cheap pre-check takes.
_BLACK_CHECK_GRID = 16

#: 32bpp formats whose in-memory layout is B,G,R,A on little-endian hosts.
_32BIT_FORMATS = frozenset(
    {
        QImage.Format.Format_ARGB32,
        QImage.Format.Format_ARGB32_Premultiplied,
        QImage.Format.Format_RGB32,
    }
)


def _raw_bytes(img: QImage) -> bytes:
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    return bytes(ptr)


def _every_pixel_black(img: QImage) -> bool:
    """Exhaustive check: are *all* pixels of *img* black?"""
    if img.format() in _32BIT_FORMATS:
        data = _raw_bytes(img)
        # The in-memory layout is B,G,R,A and the alpha byte is always 0xFF,
        # so only the three colour channels are compared.
        return not (any(data[0::4]) or any(data[1::4]) or any(data[2::4]))
    gray = img.convertToFormat(QImage.Format.Format_Grayscale8)
    return not any(_raw_bytes(gray))


def is_uniformly_black(img: QImage) -> bool:
    """True when *img* contains nothing but black pixels.

    An all-black frame almost always means the backend failed rather than
    that the user photographed a black rectangle.

    A cheap grid sample runs first: one non-black pixel anywhere on the grid
    is enough to prove the image is not uniform, which keeps the common case
    at well under a millisecond even on a 4K frame.  Only a black-looking
    image pays for the exhaustive scan.
    """
    if img.isNull() or img.width() <= 0 or img.height() <= 0:
        return False
    width, height = img.width(), img.height()
    for iy in range(_BLACK_CHECK_GRID):
        y = min(height - 1, iy * height // _BLACK_CHECK_GRID)
        for ix in range(_BLACK_CHECK_GRID):
            x = min(width - 1, ix * width // _BLACK_CHECK_GRID)
            colour = img.pixelColor(x, y)
            if colour.red() or colour.green() or colour.blue():
                return False
    return _every_pixel_black(img)


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def grab_desktop(
    include_cursor: bool = False,
    focused_only: bool = False,
    allow_black: bool = False,
) -> Screenshot:
    """Capture the desktop and return it as a single QImage.

    With *focused_only* (the default for the interactive overlay on Wayland) only
    the monitor under the cursor is captured, so the overlay maps 1:1 onto the
    screen the user is looking at.

    Set *allow_black* to accept an entirely black frame; by default a black
    frame is reported as a failed capture instead of being silently shown.
    """
    shot = _grab_desktop(include_cursor, focused_only)
    if not allow_black and is_uniformly_black(shot.image):
        raise CaptureError(_black_capture_hint())
    return shot


def _grab_desktop(include_cursor: bool, focused_only: bool) -> Screenshot:
    """Run the capture backends in order and return the first usable frame."""
    if is_wayland():
        if focused_only:
            output = focused_output_name()
            if output:
                img = _grim_capture(include_cursor, output=output)
                if img is not None and not img.isNull():
                    return Screenshot(img, None)

        img = _grim_capture(include_cursor)
        if img is not None and not img.isNull():
            return Screenshot(img, _guess_virtual_geometry(img))
        img = _portal_capture()
        if img is not None and not img.isNull():
            return Screenshot(img, _guess_virtual_geometry(img))

        # Wayland: the X11 grab is *not* a fallback here.  It does not fail
        # loudly - it returns a black rectangle - so using it produced an
        # unexplained black overlay instead of a useful error message.
        raise CaptureError(_wayland_capture_hint())

    shot = _x11_capture()
    if shot is not None and not shot.image.isNull():
        return shot

    raise CaptureError(_x11_capture_hint())


# ----------------------------------------------------------------------
# error messages
# ----------------------------------------------------------------------
#: Desktop portal package per compositor family.
_PORTAL_PACKAGES = {
    "gnome": "xdg-desktop-portal-gnome",
    "kde": "xdg-desktop-portal-kde",
    "cosmic": "xdg-desktop-portal-cosmic",
}


def _wayland_capture_hint() -> str:
    """Actionable text for a Wayland session where no capture helper answered."""
    comp = compositor_name()
    portal = _PORTAL_PACKAGES.get(comp)

    lines = [
        "Could not capture the screen.",
        "",
        "This is a Wayland session and neither 'grim' nor the desktop portal",
        "answered.  Install a capture helper that matches your desktop:",
        "",
    ]
    if portal:
        lines.append(f"    {portal}   # {comp} (desktop portal)")
    else:
        lines += [
            "    grim                      # sway, Hyprland, river, niri",
            "    xdg-desktop-portal-gnome  # GNOME",
            "    xdg-desktop-portal-kde    # KDE Plasma",
            "    xdg-desktop-portal-cosmic # COSMIC",
        ]
    lines += [
        "",
        "Package names differ per distribution, for example:",
        "    Debian/Ubuntu   sudo apt install grim xdg-desktop-portal-gnome",
        "    Fedora          sudo dnf install grim xdg-desktop-portal-gnome",
        "    Arch            sudo pacman -S grim xdg-desktop-portal-gnome",
    ]
    if portal:
        lines.append(f"    (on {comp}, install {portal} instead of the -gnome one)")
    return "\n".join(lines)


def _x11_capture_hint() -> str:
    """Actionable text for an X11 session whose screen grab failed."""
    return (
        "Could not capture the screen.\n"
        "\n"
        "The X11 screen grab returned nothing.  That usually means the window\n"
        "server has no compositing manager, or the screen contents are not\n"
        "readable by the client.\n"
        "\n"
        "Try a compositing window manager (Mutter, KWin, picom), or log into a\n"
        "Wayland session instead."
    )


def _black_capture_hint() -> str:
    """Actionable text for a capture that came back entirely black."""
    return (
        "The screen grab came back completely black.\n"
        "\n"
        "A black rectangle is not a screenshot, so this is reported as an error\n"
        "instead of being shown as an overlay.\n"
        "\n"
        "On X11 this usually means the window server has no compositing manager:\n"
        "Qt reads the root window, which only carries the visible image when a\n"
        "compositor draws into it.  Try picom/Mutter/KWin, or use a Wayland\n"
        "session.\n"
        "\n"
        "If your screen really is black on purpose, set\n"
        '    "allow_black_capture": true\n'
        "in ~/.config/sekashot/config.json to capture it anyway."
    )


def _guess_virtual_geometry(img: QImage) -> QRect | None:
    """Map a full-desktop image onto logical screen geometry when it matches."""
    app = QGuiApplication.instance()
    if app is None:
        return None
    screens = QGuiApplication.screens()
    if not screens:
        return None
    virtual = screens[0].geometry()
    for screen in screens[1:]:
        virtual = virtual.united(screen.geometry())
    if virtual.width() == img.width() and virtual.height() == img.height():
        return virtual
    return None


def screen_geometry_for_overlay() -> QRect:
    """Geometry (logical px) the full-screen overlay window should cover."""
    app = QGuiApplication.instance()
    screens = QGuiApplication.screens() if app else []
    if not screens:
        return QRect(0, 0, 1920, 1080)
    virtual = screens[0].geometry()
    for screen in screens[1:]:
        virtual = virtual.united(screen.geometry())
    return virtual


def image_size(img: QImage) -> QSize:
    return img.size()

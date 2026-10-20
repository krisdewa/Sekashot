"""Application core: tray icon, hotkey wiring, capture flow and actions."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import datetime
import time
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice, QObject, QTimer
from PyQt6.QtGui import QAction, QGuiApplication, QImage
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

from . import APP_DISPLAY_NAME, APP_NAME, APP_TAGLINE, __version__, icons
from .capture import CaptureError, grab_desktop, is_wayland, session_type
from .config import Config
from .hotkey import install_compositor_keybind, install_global_hotkey, wayland_hotkey_hint
from .overlay import CaptureOverlay
from .settings_dialog import SettingsDialog
from .sound import ClickPlayer


# ----------------------------------------------------------------------
#: How long a desktop notification stays on screen, in milliseconds.
NOTIFY_TIMEOUT_MS = 5000


def _notify_send(title: str, message: str) -> bool:
    """Notify through the `notify-send` CLI; ``False`` when it is unavailable."""
    if shutil.which("notify-send") is None:
        return False
    try:
        subprocess.run(
            [
                "notify-send",
                "--app-name", APP_DISPLAY_NAME,
                "--icon", APP_NAME,
                "--expire-time", str(NOTIFY_TIMEOUT_MS),
                title,
                message,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _notify_dbus(title: str, message: str) -> bool:
    """Post a desktop notification over D-Bus.

    The notification daemon takes ownership of it, so it stays on screen even
    though a standalone ``sekashot --capture`` run quits immediately after the
    action - which is precisely why this route is used instead of the tray.
    """
    try:
        import dbus
    except ImportError:
        return _notify_send(title, message)
    try:
        session = dbus.SessionBus()
        notifications = session.get_object(
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications"
        )
        notifications.Notify(
            APP_DISPLAY_NAME,                     # app_name        (s)
            dbus.UInt32(0),                       # replaces_id     (u)
            APP_NAME,                             # app_icon        (s)
            title,                                # summary         (s)
            message,                              # body            (s)
            dbus.Array([], signature="s"),        # actions         (as)
            dbus.Dictionary({}, signature="sv"),  # hints           (a{sv})
            dbus.Int32(NOTIFY_TIMEOUT_MS),        # expire_timeout  (i)
            dbus_interface="org.freedesktop.Notifications",
        )
    except Exception:  # noqa: BLE001 - any D-Bus failure falls through
        return _notify_send(title, message)
    return True


def _notify(tray, title: str, message: str) -> None:
    """Tell the user what happened, with or without a tray icon.

    A standalone ``sekashot --capture`` run owns no tray icon at all, so
    ``QSystemTrayIcon.showMessage`` was a silent no-op exactly when the user
    most needed the feedback: right after Copy or Save.  Prefer the freedesktop
    notification service, then the tray, then stderr.
    """
    if _notify_dbus(title, message):
        return
    if tray is not None and tray.isVisible():
        tray.showMessage(
            title, message, QSystemTrayIcon.MessageIcon.Information, NOTIFY_TIMEOUT_MS
        )
        return
    print(f"[sekashot] {message}", file=sys.stderr)


def _copy_text_to_clipboard(text: str) -> None:
    QApplication.clipboard().setText(text)


def _copy_image_to_clipboard(image: QImage) -> None:
    QApplication.clipboard().setImage(image)


def _hand_off_clipboard(data: bytes, mime: str) -> bool:
    """Give the payload to ``wl-copy``/``xclip`` so it outlives this process.

    A Wayland (and X11) selection is owned by the process that set it: the moment
    that process exits the clipboard is empty again.  ``sekashot --capture``
    runs standalone and quits right after the action, so a copy made with Qt
    alone would vanish.  ``wl-copy``/``xclip`` fork a helper that keeps serving
    the selection, which is exactly what we need here.

    Returns True when a helper accepted the payload.
    """
    if is_wayland() and shutil.which("wl-copy"):
        command = ["wl-copy", "--type", mime]
    elif shutil.which("xclip"):
        command = ["xclip", "-selection", "clipboard", "-t", mime, "-i"]
    else:
        return False

    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False

    try:
        proc.communicate(data, timeout=10)
    except subprocess.TimeoutExpired:
        # Some builds keep the helper in the foreground while it serves the
        # selection; that is a successful hand-off, so leave it running.
        return True
    return proc.returncode == 0


def _image_png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    data = bytes(buffer.data())
    buffer.close()
    return data


# ----------------------------------------------------------------------
class SekashotApp(QObject):
    """Owns the tray icon, the IPC server and the capture/editor lifecycle."""

    SOCKET_NAME = f"sekashot-{os.getuid()}"

    #: How long to let the compositor actually take the tray icon/menu off the
    #: screen before we grab it.  Long enough to be reliable, short enough that
    #: the user does not feel it (it used to be 250 ms, which was most of the
    #: perceived "the screenshot takes ages to appear" delay).
    TRAY_SETTLE_MS = 80

    #: How often to re-check that the IPC endpoint still exists.  /tmp cleaners
    #: (systemd-tmpfiles) and other instances have both been observed to unlink
    #: the socket file; without this the daemon stays alive but unreachable.
    IPC_WATCHDOG_MS = 30_000

    def __init__(self, app: QApplication, cfg: Config, standalone: bool = False) -> None:
        super().__init__()
        self.app = app
        self.cfg = cfg
        self.standalone = standalone

        self.tray: QSystemTrayIcon | None = None
        self.menu: QMenu | None = None
        self.overlay: CaptureOverlay | None = None
        self.server: QLocalServer | None = None
        self.hotkey_backend = None
        self.sound: ClickPlayer | None = None
        #: True once building the sound player has been attempted (success or not).
        self._sound_ready = False
        self._ipc_timer: QTimer | None = None
        #: inode of the socket file this instance bound, so the watchdog can tell
        #: "our endpoint was deleted" from "someone replaced it with their own".
        self._socket_inode: int | None = None
        self._busy = False
        self._last_image: QImage | None = None
        #: action still being carried out after the overlay closed ("copy" or
        #: "save", or "") - the standalone process must stay alive until it is
        #: done, otherwise dialog/clipboard state is lost on exit.
        self._pending_action = ""
        #: True while we keep the process alive to serve a Qt-owned clipboard.
        self._clipboard_hold = False
        self._hold_timer: QTimer | None = None
        self._hold_deadline = 0.0

    # ------------------------------------------------------------------
    def setup(self) -> bool:
        """Publish the IPC endpoint, tray and hotkey.

        Returns ``False`` when another instance already owns the IPC endpoint -
        the caller must then exit rather than keep this process alive.
        """
        if self.standalone:
            return True

        # Publish the endpoint *first*.  It is what `sekashot --capture` talks
        # to on every Print Screen press, so the tray host and QtMultimedia must
        # not be able to delay (or, if they misbehave, block) it.
        if not self._setup_ipc():
            return False

        self._ipc_timer = QTimer(self)
        self._ipc_timer.setInterval(self.IPC_WATCHDOG_MS)
        self._ipc_timer.timeout.connect(self._watch_ipc)
        self._ipc_timer.start()

        self._setup_tray()
        self._setup_hotkey()
        return True

    # -- sound ---------------------------------------------------------
    def _play_sound(self) -> None:
        """Play the shutter click, building the player lazily on first use.

        QtMultimedia (FFmpeg / PipeWire) is not needed to take a screenshot, so
        it is kept off the startup path entirely, and a failure to initialise it
        disables the sound instead of breaking the capture flow.
        """
        if not self.cfg.play_sound:
            return
        if not self._sound_ready:
            self._sound_ready = True
            try:
                self.sound = ClickPlayer()
            except Exception as exc:  # noqa: BLE001
                self.sound = None
                print(f"[sekashot] sound disabled ({exc})", file=sys.stderr)
        if self.sound is not None:
            self.sound.play()

    # -- tray ----------------------------------------------------------
    def _setup_tray(self) -> None:
        available = QSystemTrayIcon.isSystemTrayAvailable()
        tray = QSystemTrayIcon(icons.app_icon(64), self)
        tray.setToolTip(f"{APP_DISPLAY_NAME} — {APP_TAGLINE}")

        menu = QMenu()
        act_capture = QAction("Take screenshot\tPrint Screen", menu)
        act_capture.triggered.connect(self.request_capture)
        menu.addAction(act_capture)

        act_full = QAction("Full screen → clipboard", menu)
        act_full.triggered.connect(self.capture_to_clipboard)
        menu.addAction(act_full)

        menu.addSeparator()

        self.act_save_last = QAction("Save last capture…", menu)
        self.act_save_last.triggered.connect(self._save_last)
        self.act_save_last.setEnabled(False)
        menu.addAction(self.act_save_last)

        self.act_copy_last = QAction("Copy last capture", menu)
        self.act_copy_last.triggered.connect(self._copy_last)
        self.act_copy_last.setEnabled(False)
        menu.addAction(self.act_copy_last)

        menu.addSeparator()

        act_settings = QAction("Settings…", menu)
        act_settings.triggered.connect(self.open_settings)
        menu.addAction(act_settings)

        act_hotkey = QAction("Hotkey help…", menu)
        act_hotkey.triggered.connect(self._hotkey_help)
        menu.addAction(act_hotkey)

        act_about = QAction("About", menu)
        act_about.triggered.connect(self._about)
        menu.addAction(act_about)

        menu.addSeparator()
        act_quit = QAction("Quit", menu)
        act_quit.triggered.connect(self.quit)
        menu.addAction(act_quit)

        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        tray.show()

        self.tray = tray
        self.menu = menu

        if not available:
            print(
                "[sekashot] No system tray host detected.\n"
                "           The app keeps running in the background; trigger captures with\n"
                "           'sekashot --capture' (bind it to Print Screen).",
                file=sys.stderr,
            )

    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.request_capture()

    # -- ipc -----------------------------------------------------------
    @classmethod
    def _ipc_alive(cls, timeout_ms: int = 300) -> bool:
        """True when another instance is answering on the IPC endpoint."""
        socket = QLocalSocket()
        socket.connectToServer(cls.SOCKET_NAME)
        connected = socket.waitForConnected(timeout_ms)
        socket.abort()
        return connected

    def _setup_ipc(self) -> bool:
        """Publish the IPC endpoint; ``False`` if a live instance owns it.

        ``QLocalServer.removeServer()`` unlinks the socket file *unconditionally*,
        so calling it while another instance is listening silently orphans that
        instance: it keeps running but can never be reached again, and every
        later ``--capture`` falls back to a standalone run that opens an overlay
        and waits - one invisible process per Print Screen press.  Probe the
        endpoint first and only clear a socket nobody answers on.
        """
        if self._ipc_alive():
            return False

        QLocalServer.removeServer(self.SOCKET_NAME)  # stale socket: safe to clear
        server = QLocalServer(self)
        if not server.listen(self.SOCKET_NAME):
            print(
                f"[sekashot] could not listen on '{self.SOCKET_NAME}': {server.errorString()}",
                file=sys.stderr,
            )
            return False
        server.newConnection.connect(self._on_connection)
        self.server = server
        self._socket_inode = self._stat_inode(server.fullServerName())
        return True

    @staticmethod
    def _stat_inode(path: str) -> int | None:
        """The inode behind ``path``, or ``None`` when it is not there."""
        try:
            return os.stat(path).st_ino
        except OSError:
            return None

    def _watch_ipc(self) -> None:
        """Re-publish the endpoint if the socket is gone *or no longer ours*.

        Runs every :data:`IPC_WATCHDOG_MS` so a daemon that lost its socket (to a
        /tmp cleaner, or to another instance unlinking and re-creating it at the
        same path) repairs itself instead of leaving every later Print Screen
        press to spawn a standalone run.  Checking existence alone is not enough:
        a takeover replaces the file, so the inode has to match the one we bound.
        """
        if self.standalone:
            return
        if self.server is not None and (
            self._stat_inode(self.server.fullServerName()) == self._socket_inode
        ):
            return
        # The endpoint is gone, or the file at that path is no longer the socket
        # we bound.  Probe *before* unlinking anything: if a live instance
        # answers, it owns the endpoint, and destroying its socket is precisely
        # the orphaning bug this watchdog exists to repair.
        if self._ipc_alive():
            return
        if self.server is not None:
            self.server.close()
            self.server = None
        self._setup_ipc()

    def _on_connection(self) -> None:
        if self.server is None:
            return
        socket = self.server.nextPendingConnection()
        if socket is None:
            return

        def on_ready() -> None:
            command = bytes(socket.readAll()).decode(errors="replace").strip()
            if command in ("capture", "screenshot", "shot"):
                self.request_capture()
            elif command == "full":
                self.capture_to_clipboard()
            elif command in ("quit", "exit"):
                self.quit()
            socket.disconnectFromServer()

        socket.readyRead.connect(on_ready)
        if socket.bytesAvailable():
            on_ready()

    @classmethod
    def _signal_existing(cls, command: str) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(cls.SOCKET_NAME)
        if not socket.waitForConnected(400):
            return False
        socket.write(command.encode() + b"\n")
        socket.flush()
        socket.waitForBytesWritten(400)
        socket.disconnectFromServer()
        return True

    # -- hotkey --------------------------------------------------------
    def _setup_hotkey(self) -> None:
        backend, message = install_global_hotkey(self.cfg.hotkey, self.request_capture)
        self.hotkey_backend = backend
        print(f"[sekashot] {message}", file=sys.stderr if backend is None else sys.stdout)

    # ------------------------------------------------------------------
    # capture flow
    # ------------------------------------------------------------------
    def request_capture(self) -> None:
        if self._busy or self.overlay is not None:
            return
        if self.menu is not None:
            self.menu.hide()
        if self.tray is not None:
            self.tray.hide()  # keep the tray/dock out of the shot
        # give the compositor a moment to actually remove the menus / tooltips
        QTimer.singleShot(self.TRAY_SETTLE_MS, self._capture_now)

    def _capture_now(self) -> None:
        self._busy = True
        try:
            shot = grab_desktop(
                focused_only=self.cfg.capture_focused_output and is_wayland(),
                allow_black=self.cfg.allow_black_capture,
            )
        except CaptureError as exc:
            self._busy = False
            if self.standalone:
                # A keybind spawned this process and there is no window to hang
                # a modal on: report on stderr and exit instead of lingering as
                # an invisible process (mirrors capture_to_clipboard).  A
                # notification is posted as well, because stderr of a spawned
                # keybind handler is not something the user ever sees.
                print(f"[sekashot] {exc}", file=sys.stderr)
                _notify(None, APP_DISPLAY_NAME, str(exc))
                self._action_complete()
            else:
                self._show_tray_state()
                QMessageBox.critical(None, APP_DISPLAY_NAME, str(exc))
            return
        finally:
            self._show_tray_state()

        self._play_sound()

        overlay = CaptureOverlay(shot.image, self.cfg)
        overlay.copy_requested.connect(self.copy_image)
        overlay.save_requested.connect(self.save_image)
        overlay.finished.connect(self._overlay_finished)
        self.overlay = overlay
        overlay.start()

    def _overlay_finished(self, acted: bool, pending: str = "") -> None:
        self.overlay = None
        self._busy = False
        # persist any tweaks made with the mouse wheel / colour palette
        self.cfg.save()
        # The overlay is already hidden at this point (see CaptureOverlay._emit),
        # so Save can safely open its modal dialog on top of the desktop.
        # Remember whatever is still outstanding so a standalone run does not
        # quit before the dialog has been shown / the clipboard handed off.
        self._pending_action = pending
        self._maybe_finish_standalone()

    def _maybe_finish_standalone(self) -> None:
        """Quit a standalone (``--capture``/``--full``) run once it is truly done."""
        if self.standalone and not self._pending_action and not self._clipboard_hold:
            QTimer.singleShot(0, self.app.quit)

    def _standalone_watchdog(self) -> None:
        """Never let a standalone run survive with nothing on screen.

        If the capture never produced an overlay there is nothing for the user to
        interact with, so the process would sit there forever - and because a
        keybind spawns it, the user would see nothing at all while invisible
        processes piled up.  An on-screen overlay means the user is selecting, so
        that case is left alone.
        """
        if self.overlay is None:
            print(
                "[sekashot] capture did not open an overlay; exiting.",
                file=sys.stderr,
            )
            self.app.quit()

    def _action_complete(self) -> None:
        """Mark the pending action as done and finish a standalone run if idle."""
        self._pending_action = ""
        self._maybe_finish_standalone()

    def _hold_clipboard(self) -> None:
        """Keep this process alive so a Qt-owned clipboard survives a while.

        Only relevant when no ``wl-copy``/``xclip`` helper is available: Qt owns
        the selection itself and the content dies with the process, so a
        standalone run would quit and leave the clipboard empty.
        """
        if not self.standalone:
            return
        self._clipboard_hold = True
        if self._hold_timer is None:
            self._hold_timer = QTimer(self)
            self._hold_timer.setInterval(500)
            self._hold_timer.timeout.connect(self._check_clipboard_hold)
        self._hold_deadline = time.monotonic() + 20.0
        self._hold_timer.start()

    def _check_clipboard_hold(self) -> None:
        owns = QApplication.clipboard().ownsClipboard()
        if (not owns) or time.monotonic() >= self._hold_deadline:
            if self._hold_timer is not None:
                self._hold_timer.stop()
            self._clipboard_hold = False
            self._maybe_finish_standalone()

    def _show_tray_state(self) -> None:
        if self.tray is not None and not self.tray.isVisible():
            self.tray.show()

    def capture_to_clipboard(self) -> None:
        """Classic 'full screen straight to clipboard' shortcut."""
        try:
            shot = grab_desktop(
                focused_only=self.cfg.capture_focused_output and is_wayland(),
                allow_black=self.cfg.allow_black_capture,
            )
        except CaptureError as exc:
            if self.standalone:
                # No window to attach a modal to; report on stderr and exit
                # cleanly instead of hanging the spawned process.  The
                # notification is what the user actually perceives.
                print(f"[sekashot] {exc}", file=sys.stderr)
                _notify(None, APP_DISPLAY_NAME, str(exc))
                self._action_complete()
            else:
                QMessageBox.critical(None, APP_DISPLAY_NAME, str(exc))
            return
        self._play_sound()
        self.copy_image(shot.image)

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _remember(self, image: QImage) -> None:
        self._last_image = image
        if self.tray is not None:
            self.act_save_last.setEnabled(True)
            self.act_copy_last.setEnabled(True)

    def _auto_save(self, image: QImage) -> str | None:
        if not self.cfg.auto_save:
            return None
        folder = Path(self.cfg.auto_save_dir or self.cfg.save_dir)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        path = folder / self._default_name()
        if self._write_image(image, path):
            return str(path)
        return None

    def _default_name(self) -> str:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        ext = "jpg" if self.cfg.save_format.lower() in ("jpg", "jpeg") else "png"
        return f"Screenshot_{stamp}.{ext}"

    def _write_image(self, image: QImage, path: Path) -> bool:
        ext = path.suffix.lower()
        if ext in (".jpg", ".jpeg"):
            return image.save(str(path), "JPEG", self.cfg.jpg_quality)
        return image.save(str(path), "PNG")

    def copy_image(self, image: QImage) -> None:
        self._remember(image)
        if self.cfg.copy_format == "file-url":
            saved = self._auto_save(image)
            if saved is None:
                folder = Path(self.cfg.save_dir)
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / self._default_name()
                if self._write_image(image, target):
                    saved = str(target)
            if saved:
                _copy_text_to_clipboard(str(saved))
                if not _hand_off_clipboard(str(saved).encode("utf-8"), "text/plain;charset=utf-8"):
                    self._hold_clipboard()
                _notify(self.tray, APP_DISPLAY_NAME, f"File path copied to the clipboard.\n{saved}")
                self._action_complete()
                return
        _copy_image_to_clipboard(image)
        # Hand the PNG to wl-copy/xclip so the selection outlives this process;
        # a plain Qt clipboard would be wiped the instant a standalone run quits.
        if not _hand_off_clipboard(_image_png_bytes(image), "image/png"):
            self._hold_clipboard()
        saved = self._auto_save(image)
        _notify(
            self.tray,
            APP_DISPLAY_NAME,
            f"Copied to the clipboard.\nSaved to {saved}" if saved else "Copied to the clipboard.",
        )
        self._action_complete()

    def save_image(self, image: QImage) -> None:
        # The overlay was hidden synchronously, but the compositor needs one more
        # event-loop turn before the full-screen surface is really unmapped.
        # Opening the modal dialog immediately could still tuck it behind the
        # overlay (the reported "Save does nothing"), so defer by a beat.
        QTimer.singleShot(150, lambda: self._do_save(image))

    def _do_save(self, image: QImage) -> None:
        self._remember(image)
        folder = Path(self.cfg.save_dir)
        folder.mkdir(parents=True, exist_ok=True)
        suggested = str(folder / self._default_name())
        filters = "PNG image (*.png);;JPEG image (*.jpg *.jpeg)"
        path, _selected = QFileDialog.getSaveFileName(None, "Save screenshot", suggested, filters)
        if not path:
            self._action_complete()
            return
        target = Path(path)
        if target.suffix == "":
            target = target.with_suffix(".jpg" if self.cfg.save_format.lower() == "jpg" else ".png")
        if self._write_image(image, target):
            _notify(self.tray, APP_DISPLAY_NAME, f"Saved to {target}")
        else:
            # A notification rather than a modal: this can be a standalone run
            # spawned by the keybind, where a parentless dialog may never be
            # seen and would keep the process alive.
            _notify(self.tray, APP_DISPLAY_NAME, f"Could not write {target}")
        self._action_complete()

    # -- menu helpers --------------------------------------------------
    def _save_last(self) -> None:
        if self._last_image is not None:
            self.save_image(self._last_image)

    def _copy_last(self) -> None:
        if self._last_image is not None:
            self.copy_image(self._last_image)

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.cfg, None)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if self.overlay is not None:
                self.overlay.cfg = self.cfg
                self.overlay.update()
            _notify(self.tray, APP_DISPLAY_NAME, "Settings saved.")

    def _hotkey_help(self) -> None:
        text = wayland_hotkey_hint() if is_wayland() else (
            f"Session: X11\nThe application grabs '{self.cfg.hotkey}' itself.\n"
            "You can also run:  sekashot --capture"
        )
        box = QMessageBox()
        box.setWindowTitle("Hotkey")
        box.setText("Global hotkey")
        box.setInformativeText(text)
        box.setIcon(QMessageBox.Icon.Information)
        if is_wayland():
            install = box.addButton("Install it for me", QMessageBox.ButtonRole.ActionRole)
            box.exec()
            if box.clickedButton() is install:
                QMessageBox.information(None, "Hotkey", install_compositor_keybind())
        else:
            box.exec()

    def _about(self) -> None:
        QMessageBox.about(
            None,
            f"About {APP_DISPLAY_NAME}",
            f"<h3>{APP_DISPLAY_NAME} {__version__}</h3>"
            f"<p>{APP_TAGLINE}.</p>"
            "<p>A fast Linux screenshot tool, inspired by Lightshot:<br>"
            "tray + global hotkey, frozen dimmed overlay, live W x H badge,<br>"
            "pen / line / arrow / rectangle / marker / text / blur annotations,<br>"
            "undo, colour picker, copy and save.</p>"
            f"<p>Session: <b>{session_type()}</b><br>Config: <code>{self.cfg.config_path}</code></p>",
        )

    def quit(self) -> None:
        if self.hotkey_backend is not None:
            try:
                self.hotkey_backend.stop()
            except Exception:
                pass
        if self.server is not None:
            self.server.close()
            QLocalServer.removeServer(self.SOCKET_NAME)
        if self.tray is not None:
            self.tray.hide()
        self.cfg.save()
        self.app.quit()


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sekashot",
        description=f"{APP_DISPLAY_NAME} — {APP_TAGLINE}",
    )
    parser.add_argument("-c", "--capture", action="store_true", help="open the screenshot overlay now")
    parser.add_argument("--full", action="store_true", help="grab the whole screen straight to the clipboard")
    parser.add_argument("--settings", action="store_true", help="open the settings window")
    parser.add_argument("--install-hotkey", action="store_true", help="install the compositor keybind (Wayland)")
    parser.add_argument("--force", action="store_true", help="overwrite config files without asking (with --install-hotkey)")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    return parser


def _desktop_file_available(app_id: str) -> bool:
    """True when an installed .desktop file exists for *app_id*.

    Without it Qt would try to register an app ID with the xdg-desktop-portal
    that the portal cannot resolve, which logs a harmless but noisy error.
    """
    bases = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    bases.append(str(Path.home() / ".local" / "share"))
    for base in bases:
        if base and (Path(base) / "applications" / f"{app_id}.desktop").is_file():
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # A keybind-triggered `sekashot --capture` only has to nudge the instance
    # that is already running.  Try that before building a QApplication (and
    # before loading the config) so the whole process exits in tens of
    # milliseconds.  `__main__.py` does the same thing even earlier, without
    # importing PyQt6 at all; this is the safety net for direct callers.
    if args.capture or args.full:
        if SekashotApp._signal_existing("capture" if args.capture else "full"):
            return 0

    app = QApplication([sys.argv[0]])
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    if _desktop_file_available(APP_NAME):
        app.setDesktopFileName(APP_NAME)
    app.setWindowIcon(icons.app_icon())
    app.setQuitOnLastWindowClosed(False)

    if args.version:
        print(f"{APP_DISPLAY_NAME} {__version__}")
        return 0

    if args.install_hotkey:
        if is_wayland():
            print(install_compositor_keybind(force=args.force))
        else:
            print("X11 session: the application grabs the hotkey itself, nothing to install.")
        return 0

    cfg = Config.load()

    if args.settings:
        dialog = SettingsDialog(cfg, None)
        dialog.exec()
        return 0

    standalone = bool(args.capture or args.full)
    runner = SekashotApp(app, cfg, standalone=standalone)
    if not runner.setup():
        print("[sekashot] another instance is already running; exiting.", file=sys.stderr)
        return 0

    if args.capture:
        runner.request_capture()
        # Safety net: a keybind-spawned run must never linger with nothing on
        # screen (an unreachable daemon used to leave one invisible process per
        # press).  Fires only if no overlay ever appeared.
        QTimer.singleShot(10_000, runner._standalone_watchdog)
    elif args.full:
        runner.capture_to_clipboard()
        # Safety net: only fires if the copy somehow never completed. It honours
        # an in-progress clipboard hold, so a valid selection is never truncated.
        QTimer.singleShot(1500, runner._maybe_finish_standalone)
    else:
        print(
            f"[sekashot] running. Session: {session_type()}.",
            file=sys.stderr,
        )
        if is_wayland():
            print(wayland_hotkey_hint(), file=sys.stderr)

    return app.exec()

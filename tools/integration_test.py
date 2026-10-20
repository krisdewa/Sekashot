"""End-to-end integration test, run against the *packaged* payload.

Extract the .deb somewhere, then point PYTHONPATH at it and run this file: it
exercises the real code paths the installed launcher uses - desktop capture (via
grim on Wayland), the overlay, the copy-to-clipboard action, the save writer, the
hotkey installer, and the hot-path Print Screen client.

    dpkg-deb -x sekashot.deb /tmp/root
    PYTHONPATH=/tmp/root/usr/lib/sekashot QT_QPA_PLATFORM=offscreen \
        python3 tools/integration_test.py

Set SEKASHOT_LIVE=1 to run against the real clipboard/desktop instead of the
offscreen platform.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if os.environ.get("SEKASHOT_LIVE") != "1":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PyQt6.QtNetwork import QLocalServer  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, extra: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -> ' + extra) if extra else ''}")
    if not condition:
        FAILURES.append(label)


def main() -> int:
    app = QApplication([sys.argv[0]])
    app.setQuitOnLastWindowClosed(False)

    # Never let the test write into the real ~/.config/sekashot: point the whole
    # run at a throwaway config home (section 7 overrides this for its own use).
    _sandbox_cfg_home = tempfile.mkdtemp(prefix="sekashot-test-cfg-")
    _old_cfg_home = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = _sandbox_cfg_home

    import atexit

    def _restore_cfg_home() -> None:
        if _old_cfg_home is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = _old_cfg_home

    atexit.register(_restore_cfg_home)

    from sekashot import __version__  # noqa: F401
    from sekashot.annotations import RECT, Annotation
    from sekashot.app import SekashotApp
    from sekashot.capture import grab_desktop, session_type
    from sekashot.config import Config
    from sekashot.hotkey import install_compositor_keybind

    print(f"\n1. packaged payload imports (session: {session_type()})")
    check("version is defined", isinstance(__version__, str), __version__)

    # ------------------------------------------------------------------
    print("\n2. desktop capture")
    from sekashot.capture import focused_output_name

    name = focused_output_name()
    check("compositor reports the focused output", bool(name), str(name))
    focused = grab_desktop(focused_only=True)
    full = grab_desktop(focused_only=False)
    fw, fh = focused.image.width(), focused.image.height()
    uw, uh = full.image.width(), full.image.height()
    check("focused capture is non-empty", not focused.image.isNull() and fw > 0, f"{fw}x{fh}")
    check("full virtual desktop capture is non-empty", not full.image.isNull() and uw > 0, f"{uw}x{uh}")
    check("focused capture is one monitor (no wider than the desktop)", fw <= uw, f"focused {fw}x{fh} | desktop {uw}x{uh}")
    shot = full

    # ------------------------------------------------------------------
    print("\n3. the app runs a capture and shows the overlay")
    cfg = Config()
    cfg.play_sound = False
    cfg.marching_ants = False
    runner = SekashotApp(app, cfg, standalone=True)
    runner.setup()
    runner.request_capture()
    QTest.qWait(1500)                      # the request waits 250 ms, then grabs
    check("overlay was created", runner.overlay is not None)
    if runner.overlay is None:
        print("\nFATAL: no overlay, aborting")
        return 1

    overlay = runner.overlay
    check("overlay holds the frozen desktop",
          overlay.shot.width() == shot.image.width() or overlay.shot.width() > 0,
          f"{overlay.shot.width()}x{overlay.shot.height()}")

    # ------------------------------------------------------------------
    print("\n4. select, annotate, then hit the copy button")
    # express the selection in *image* pixels and convert, exactly like a real
    # drag does through the widget<->image transform
    overlay._compute_transform()
    overlay.sel = QRectF(overlay.to_widget(QPointF(0, 0)), overlay.to_widget(QPointF(300, 200)))
    overlay.phase = "ready"
    overlay.tool = RECT
    overlay.annotations.append(Annotation(RECT, [QPointF(20, 20), QPointF(180, 140)], (255, 0, 0, 255), 3))

    delivered: list = []
    badge = overlay.crop_rect()
    check("the size badge rect is exactly 300x200", badge[2:] == (300, 200), f"{badge[2]}x{badge[3]}")
    overlay.copy_requested.connect(lambda img: delivered.append(img))
    # keep the app alive for the remaining checks: in a standalone run the copy
    # would now schedule a quit once the clipboard hand-off has completed.
    runner.standalone = False
    overlay._emit("copy")
    QTest.qWait(200)

    check("copy_requested fired with a 300x200 crop",
          bool(delivered) and delivered[0].width() == 300 and delivered[0].height() == 200,
          f"{delivered[0].width()}x{delivered[0].height()}" if delivered else "nothing")
    check("exported crop matches the badge rect exactly",
          bool(delivered) and (delivered[0].width(), delivered[0].height()) == (badge[2], badge[3]),
          f"{delivered[0].width()}x{delivered[0].height()} vs badge {badge[2]}x{badge[3]}" if delivered else "nothing")
    clip = QApplication.clipboard().image()
    check("clipboard now holds the image", not clip.isNull() and clip.width() == 300,
          f"{clip.width()}x{clip.height()}" if not clip.isNull() else "null")
    check("overlay closed itself after copying", runner.overlay is None)

    # ------------------------------------------------------------------
    print("\n5. save writer (both formats)")
    tmp = Path(tempfile.mkdtemp(prefix="sekashot-test-"))
    image = QImage(120, 80, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.darkCyan)
    cfg.save_format = "png"
    png = tmp / "shot.png"
    check("PNG written", runner._write_image(image, png) and png.stat().st_size > 0)
    cfg.save_format = "jpg"
    jpg = tmp / "shot.jpg"
    check("JPEG written", runner._write_image(image, jpg) and jpg.stat().st_size > 0)

    # ------------------------------------------------------------------
    print("\n6. the Print Screen hot path (must not import PyQt6)")
    # The keybind spawns `sekashot --capture` on every press; its whole job is
    # to hand a command to the running instance.  Sandbox TMPDIR so the stub
    # server owns the socket path instead of any real daemon.
    import sekashot
    import socket
    import threading
    import time as _time

    pkg_root = str(Path(sekashot.__file__).resolve().parent.parent)
    ipc_tmp = Path(tempfile.mkdtemp(prefix="sekashot-ipc-"))
    runtime = ipc_tmp / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    sock_path = ipc_tmp / f"sekashot-{os.getuid()}"

    received: list[str] = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(4)

    def _serve_once() -> None:
        server.settimeout(15)
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            received.append(data.decode(errors="replace").strip())

    thread = threading.Thread(target=_serve_once, daemon=True)
    thread.start()

    env = dict(os.environ)
    env["TMPDIR"] = str(ipc_tmp)
    env["XDG_RUNTIME_DIR"] = str(runtime)
    env["PYTHONPATH"] = pkg_root + (
        os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""
    )

    t0 = _time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-X", "importtime", "-m", "sekashot", "--capture"],
        cwd=pkg_root, env=env, capture_output=True, text=True, timeout=60,
    )
    elapsed_ms = (_time.perf_counter() - t0) * 1000
    thread.join(timeout=5)
    server.close()

    check("the capture client exits cleanly", proc.returncode == 0, proc.stderr[-300:])
    check("the running instance received 'capture'", received == ["capture"], str(received))
    check("the hot path never imports PyQt6", "PyQt6" not in proc.stderr + proc.stdout,
          next((l for l in proc.stderr.splitlines() if "PyQt6" in l), "clean"))
    check("the hot path is fast (<250 ms)", elapsed_ms < 250, f"{elapsed_ms:.0f} ms")

    # ------------------------------------------------------------------
    print("\n6b. a compositor-side close ends a standalone run (no ghost process)")
    # Regression: the overlay sets WA_DeleteOnClose but had no closeEvent(), so a
    # compositor/WM close removed the window while a standalone `--capture`
    # stayed alive forever with nothing on screen - one invisible orphan per
    # press.  Standalone here is inert (setup() is never called), and .app is
    # swapped for a stub so the test itself is never quit.
    from sekashot.app import SekashotApp
    from sekashot.overlay import CaptureOverlay as _CO

    class _FakeApp:
        def __init__(self) -> None:
            self.quit_called = False

        def quit(self) -> None:
            self.quit_called = True

    standalone = SekashotApp(app, Config(), standalone=True)
    fake = _FakeApp()
    standalone.app = fake
    ghost = _CO(QImage(64, 48, QImage.Format.Format_ARGB32_Premultiplied), Config())
    ghost.finished.connect(standalone._overlay_finished)
    standalone.overlay = ghost
    standalone._busy = True

    ghost.close()  # exactly what niri's close-window / a WM shortcut does

    QTest.qWait(80)
    check("the closed overlay is forgotten by the app", standalone.overlay is None)
    check("the standalone run then quits instead of lingering", fake.quit_called is True)

    # ------------------------------------------------------------------
    print("\n6c. a second instance must never orphan the running daemon")
    # Regression: QLocalServer.removeServer() unlinks the socket file even when
    # another instance is listening on it.  Doing that orphaned the live daemon
    # (alive but unreachable) so every Print Screen press fell back to a
    # standalone run - which is how ~40 invisible processes appeared.
    #
    # This section MUST NOT touch the real endpoint.  A test that clobbers the
    # user's running daemon would commit the very bug it is checking for, so it
    # runs on a private socket name of its own.
    real_endpoint = SekashotApp.SOCKET_NAME
    endpoint = f"sekashot-test-{os.getpid()}"
    SekashotApp.SOCKET_NAME = endpoint
    try:
        owner = QLocalServer()
        QLocalServer.removeServer(endpoint)
        check("the daemon publishes its endpoint", owner.listen(endpoint))
        socket_path = Path(owner.fullServerName())
        check("the socket file is on disk", socket_path.exists())
        check(
            "the test endpoint is not the real one",
            socket_path != Path(tempfile.gettempdir()) / real_endpoint,
            f"{socket_path} vs {real_endpoint}",
        )

        second = SekashotApp(app, Config(), standalone=False)
        check("a second instance refuses to take over", second.setup() is False)
        check("it did not clobber the socket file", socket_path.exists())
        check("the running daemon is still reachable", SekashotApp._ipc_alive() is True)
        check("the second instance never published", second.server is None)
        owner.close()

        # the watchdog republishes a socket that something deleted (/tmp cleaners)
        third = SekashotApp(app, Config(), standalone=False)
        check("a fresh instance takes the endpoint when nobody answers", third.setup() is True)
        third_path = Path(third.server.fullServerName())
        os.unlink(str(third_path))                     # simulate a /tmp cleaner
        check("the socket file is gone", not third_path.exists())
        third._watch_ipc()
        QTest.qWait(200)
        check("the watchdog re-published the endpoint", third_path.exists())
        check("...and it answers again", SekashotApp._ipc_alive() is True)
        third.server.close()
        third.server = None
        if third._ipc_timer is not None:
            third._ipc_timer.stop()
        QLocalServer.removeServer(endpoint)

        # The harder case: the file is still there, but nobody is listening on it
        # (the owner died without cleaning up - a crash, a kill -9, or a stale
        # file left behind).  An existence check alone would never notice, so the
        # watchdog compares the inode it bound against the one on disk.
        fourth = SekashotApp(app, Config(), standalone=False)
        check("a fresh instance binds its own socket", fourth.setup() is True)
        fourth_path = Path(fourth.server.fullServerName())
        our_inode = fourth._socket_inode
        check("the bound inode was recorded", our_inode is not None, str(our_inode))

        # a bare AF_UNIX listener leaves the file behind when it goes away, which
        # is exactly the stale-socket shape (Python does not unlink on close)
        os.unlink(str(fourth_path))
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(fourth_path))
        stale.listen(1)
        stale.close()
        check("the path still exists", fourth_path.exists())
        check("but it has a different inode",
              fourth_path.stat().st_ino != our_inode,
              f"{fourth_path.stat().st_ino} vs {our_inode}")
        check("and nobody answers on it", SekashotApp._ipc_alive() is False)

        fourth._watch_ipc()
        QTest.qWait(200)
        check("the watchdog reclaimed the endpoint",
              fourth._socket_inode == fourth_path.stat().st_ino,
              f"{fourth._socket_inode} vs {fourth_path.stat().st_ino}")
        check("...and the endpoint answers again", SekashotApp._ipc_alive() is True)

        fourth.server.close()
        fourth.server = None
        if fourth._ipc_timer is not None:
            fourth._ipc_timer.stop()
        QLocalServer.removeServer(endpoint)
    finally:
        SekashotApp.SOCKET_NAME = real_endpoint

    # ------------------------------------------------------------------
    print("\n7. hotkey installer writes compositor config (sandboxed XDG)")
    sandbox = Path(tempfile.mkdtemp(prefix="sekashot-home-"))
    old_cfg_home = os.environ.get("XDG_CONFIG_HOME")
    old_desktop = os.environ.get("XDG_CURRENT_DESKTOP")
    os.environ["XDG_CONFIG_HOME"] = str(sandbox)
    os.environ["XDG_CURRENT_DESKTOP"] = "niri"
    try:
        (sandbox / "niri").mkdir(parents=True, exist_ok=True)
        (sandbox / "niri" / "config.kdl").write_text("input {\n}\n", encoding="utf-8")
        report = install_compositor_keybind()
        bind = sandbox / "niri" / "sekashot.kdl"
        main_cfg = sandbox / "niri" / "config.kdl"
        check("sekashot.kdl written", bind.is_file())
        check("bind spawns --capture", '--capture' in bind.read_text(encoding="utf-8") if bind.is_file() else False)
        check("main config includes it", 'include "sekashot.kdl"' in main_cfg.read_text(encoding="utf-8"))
        check("backup of the original config kept", (sandbox / "niri" / "config.kdl.sekashot-bak").is_file())
        check("report gives the reload command", "niri msg action" in report, report.splitlines()[-3])

        # running it twice must be idempotent (it must not treat its own output
        # as a pre-existing binding and keep escalating to another key)
        install_compositor_keybind()
        again = bind.read_text(encoding="utf-8")
        check("second run keeps the same key", re.search(r"^\s*Print\s*\{", again, re.M) is not None,
              again.replace("\n", " | ")[:100])

        # ---------------- conflict handling ----------------
        print("\n7b. an existing Print binding must NOT be clobbered")
        sandbox2 = Path(tempfile.mkdtemp(prefix="sekashot-home2-"))
        os.environ["XDG_CONFIG_HOME"] = str(sandbox2)
        (sandbox2 / "niri").mkdir(parents=True, exist_ok=True)
        original = (
            "binds {\n"
            '    Print { spawn "/home/user/.local/bin/dot-screenshot" "region"; }\n'
            "    Ctrl+Print { spawn \"whatever\"; }\n"
            "}\n"
        )
        (sandbox2 / "niri" / "config.kdl").write_text(original, encoding="utf-8")
        report2 = install_compositor_keybind()
        written = (sandbox2 / "niri" / "sekashot.kdl").read_text(encoding="utf-8")
        check("did not re-bind the taken 'Print' key", re.search(r"^\s*Print\s*\{", written, re.M) is None,
              written.replace("\n", " | ")[:120])
        check("used the free 'Mod+Print' instead", "Mod+Print" in written)
        check("report warns about the conflict", "already bound" in report2)
        check(
            "the user's own binding is still in their config",
            "dot-screenshot" in (sandbox2 / "niri" / "config.kdl").read_text(encoding="utf-8"),
        )
    finally:
        os.environ.pop("XDG_CONFIG_HOME", None)
        if old_cfg_home:
            os.environ["XDG_CONFIG_HOME"] = old_cfg_home
        os.environ.pop("XDG_CURRENT_DESKTOP", None)
        if old_desktop:
            os.environ["XDG_CURRENT_DESKTOP"] = old_desktop

    # ------------------------------------------------------------------
    print("\n8. settings dialog constructs and round-trips (sandboxed config home)")
    from sekashot.settings_dialog import SettingsDialog

    sandbox_cfg = Path(tempfile.mkdtemp(prefix="sekashot-cfg-"))
    old_cfg_home_s8 = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = str(sandbox_cfg)
    try:
        cfg3 = Config()
        dlg = SettingsDialog(cfg3, None)
        dlg.pen_width.setValue(7)
        dlg.save_format.setCurrentText("jpg")
        dlg._save()
        from sekashot.config import Config as CFG

        check("dialog saved pen width", CFG.load().pen_width == 7, str(CFG.load().pen_width))
        check("dialog saved format", CFG.load().save_format == "jpg", CFG.load().save_format)
    finally:
        if old_cfg_home_s8 is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = old_cfg_home_s8

    # ------------------------------------------------------------------
    # Regression tests for the reported "Copy and Save do nothing" bug.
    # ------------------------------------------------------------------
    print("\n9. blur / redact tool in the packaged payload")
    from sekashot.annotations import BLUR
    from sekashot.blur import blur_image
    from sekashot.overlay import CaptureOverlay

    # a checkerboard makes the blur measurable: sharp = high contrast, blurred = flat
    checker = QImage(400, 300, QImage.Format.Format_ARGB32_Premultiplied)
    cp = QPainter(checker)
    for y in range(0, 300, 4):
        for x in range(0, 400, 4):
            cp.fillRect(x, y, 4, 4, QColor("white") if ((x // 4) + (y // 4)) % 2 else QColor("black"))
    cp.end()

    blur_overlay = CaptureOverlay(checker, cfg)
    blur_overlay.resize(400, 300)
    blur_overlay._compute_transform()
    blur_overlay.show()
    QTest.qWait(40)
    blur_overlay._compute_transform()
    blur_overlay.sel = QRectF(0, 0, 400, 300)
    blur_overlay.phase = "ready"
    blur_overlay._layout_toolbars()

    v_actions = [a for _r, a, _i, _t in blur_overlay._v_items]
    check("the blur button is on the vertical toolbar", "blur" in v_actions, str(v_actions))

    blur_overlay.tool = BLUR
    QTest.mousePress(blur_overlay, Qt.MouseButton.LeftButton, pos=QPoint(50, 50))
    QTest.mouseMove(blur_overlay, QPoint(200, 150))
    QTest.mouseRelease(blur_overlay, Qt.MouseButton.LeftButton, pos=QPoint(200, 150))
    check("blur annotation recorded", len(blur_overlay.annotations) == 1 and blur_overlay.annotations[0].kind == BLUR,
          str([a.kind for a in blur_overlay.annotations]))

    def _contrast(image, x0, y0, x1, y1):
        vals = [image.pixelColor(x, y).lightness() for x in range(x0, x1, 3) for y in range(y0, y1, 3)]
        return max(vals) - min(vals)

    blurred = blur_overlay.render_result()
    check("the exported crop has the blurred region flattened",
          _contrast(blurred, 60, 60, 190, 140) < 20, str(_contrast(blurred, 60, 60, 190, 140)))
    check("the exported crop keeps the rest sharp",
          _contrast(blurred, 250, 200, 380, 290) > 200, str(_contrast(blurred, 250, 200, 380, 290)))

    solid = QImage(160, 100, QImage.Format.Format_ARGB32_Premultiplied)
    solid.fill(QColor("#3080c0"))
    solid_blur = blur_image(solid, 12)
    probes = [solid_blur.pixelColor(x, y) for x in (0, 159) for y in (0, 99)]
    check("blur does not fade the edges of a solid region",
          all(abs(c.red() - 0x30) <= 2 and abs(c.green() - 0x80) <= 2 and abs(c.blue() - 0xC0) <= 2 for c in probes),
          str(sorted({c.name() for c in probes})))
    blur_overlay.close()

    # ------------------------------------------------------------------
    print("\n10. regression: the full-screen overlay is hidden before the save dialog")
    import sekashot.app as appmod

    runner.standalone = False
    ov_save = CaptureOverlay(shot.image, cfg)
    ov_save.show()
    QTest.qWait(60)
    check("precondition: overlay is on screen", ov_save.isVisible())

    in_emit = [True]
    dialog_visible: list = []
    dialog_during_emit: list = []

    class _FakeFileDialog:
        @staticmethod
        def getSaveFileName(*args, **kwargs):  # noqa: N802
            try:
                visible = ov_save.isVisible()
            except RuntimeError:            # C++ widget already gone => not shown
                visible = False
            dialog_visible.append(visible)
            dialog_during_emit.append(in_emit[0])
            return "", ""

    real_file_dialog = appmod.QFileDialog
    appmod.QFileDialog = _FakeFileDialog
    try:
        runner.overlay = ov_save
        ov_save._compute_transform()
        ov_save.sel = QRectF(ov_save.to_widget(QPointF(0, 0)), ov_save.to_widget(QPointF(200, 150)))
        ov_save.phase = "ready"
        ov_save.save_requested.connect(runner.save_image)
        ov_save._emit("save")
        in_emit[0] = False
        check("the save dialog does not open while the action is still running",
              dialog_during_emit == [], str(dialog_during_emit))
        try:
            still_up = ov_save.isVisible()
        except RuntimeError:
            still_up = False
        check("the overlay is already gone when the action returns", still_up is False)
        QTest.qWait(400)
    finally:
        appmod.QFileDialog = real_file_dialog
        runner.overlay = None

    check("the save dialog opened once the overlay was gone",
          bool(dialog_visible) and dialog_visible[0] is False, str(dialog_visible))

    print("\n11. regression: a clipboard hand-off outlives this process")
    from sekashot.app import _hand_off_clipboard, _image_png_bytes

    payload = _image_png_bytes(image)
    handed = _hand_off_clipboard(payload, "image/png")
    check("a clipboard helper accepted the payload", handed,
          "wl-copy/xclip not available" if not handed else "ok")
    if handed and shutil.which("wl-paste"):
        readback = subprocess.run(
            ["wl-paste", "--type", "image/png", "--no-newline"],
            capture_output=True,
            timeout=15,
        )
        check("a separate process reads back an identical PNG after the setter exited",
              readback.stdout == payload,
              f"{len(readback.stdout)} vs {len(payload)} bytes")

    # ------------------------------------------------------------------
    print("\n12. copy / save notify the user, tray or not")
    # Regression: the old `_notify_tray` was a silent no-op without a visible tray
    # icon - exactly the situation a keybind-spawned `--capture` run is in, so the
    # user was never told that (or where) a copy had been saved.
    import sekashot.app as app_module

    notices: list[tuple[str, str]] = []
    real_notify = app_module._notify
    app_module._notify = lambda tray, title, message: notices.append((title, message))
    try:
        ncfg = Config()
        ncfg.auto_save = True
        ncfg.auto_save_dir = tempfile.mkdtemp(prefix="sekashot-test-autosave-")
        runner = SekashotApp(app, ncfg, standalone=True)
        runner.tray = None                      # what a standalone run really looks like

        sample = QImage(120, 90, QImage.Format.Format_RGB32)
        sample.fill(0xFF3366CC)
        runner.copy_image(sample)

        check("copy notifies even with no tray icon", len(notices) == 1, str(notices))
        if notices:
            title, message = notices[-1]
            check("the notification is attributed to the app", title == "Sekashot", title)
            check("it says the image was copied", message.startswith("Copied"), message)
            check("it names the auto-saved file", "Saved to " in message, message)
        written = sorted(Path(ncfg.auto_save_dir).glob("Screenshot_*"))
        check("the auto-save file exists", len(written) == 1, str(written))
        if written and notices:
            check("the notification quotes that exact path", str(written[0]) in notices[-1][1])

        # Save: answer the file dialog instead of a human
        from PyQt6.QtWidgets import QFileDialog

        real_dialog = QFileDialog.getSaveFileName
        try:
            notices.clear()
            target = Path(ncfg.auto_save_dir) / "chosen-by-test.png"
            QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(target), ""))
            runner._do_save(sample)
            check("save notifies", notices == [("Sekashot", f"Saved to {target}")], str(notices))
            check("the saved file exists", target.exists(), str(target))

            # a write that fails must report too, without leaving a modal behind
            notices.clear()
            unwritable = Path(ncfg.auto_save_dir) / "no-such-dir" / "x.png"
            QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(unwritable), ""))
            runner._do_save(sample)
            check("a failed write is reported",
                  len(notices) == 1 and "Could not write" in notices[-1][1], str(notices))
            check("...and no file was created", not unwritable.exists(), str(unwritable))
        finally:
            QFileDialog.getSaveFileName = real_dialog
    finally:
        app_module._notify = real_notify

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("INTEGRATION TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

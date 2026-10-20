"""Headless functional self-test for the overlay editor.

Drives real Qt mouse/keyboard events through the widget so the selection drag,
tool activation, drawing, undo, inline text and result cropping are all
exercised without a display.

Run with:  QT_QPA_PLATFORM=offscreen python3 tools/selftest.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap, QWheelEvent  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from sekashot.annotations import (  # noqa: E402
    ARROW,
    BLUR,
    LINE,
    MARKER,
    PEN,
    RECT,
    SELECT,
    TEXT,
    Annotation,
)
from sekashot.config import Config  # noqa: E402
from sekashot.overlay import CaptureOverlay  # noqa: E402

W, H = 1920, 1080
FAILURES: list[str] = []


def check(label: str, condition: bool, extra: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{(' -> ' + extra) if extra else ''}")
    if not condition:
        FAILURES.append(label)


def fresh_overlay(cfg: Config) -> CaptureOverlay:
    img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("#203040"))
    overlay = CaptureOverlay(img, cfg)
    overlay.resize(W, H)
    overlay._compute_transform()
    overlay.show()
    QTest.qWait(30)
    overlay._compute_transform()
    return overlay


def button_center(overlay: CaptureOverlay, bar: str, action: str) -> QPoint:
    items = overlay._v_items if bar == "v" else overlay._h_items
    for rect, act, _icon, _tip in items:
        if act == action:
            return QPoint(int(rect.center().x()), int(rect.center().y()))
    raise AssertionError(f"button {action!r} not found in the {bar} toolbar")


def main() -> int:
    app = QApplication([sys.argv[0]])

    # Never touch the real ~/.config/sekashot: section 15 saves the config.
    import tempfile

    sandbox = tempfile.mkdtemp(prefix="sekashot-selftest-cfg-")
    os.environ["XDG_CONFIG_HOME"] = sandbox

    cfg = Config()
    cfg.marching_ants = False
    cfg.show_size_badge = True

    # ------------------------------------------------------------------
    print("\n1. selection drag with live size badge")
    overlay = fresh_overlay(cfg)
    check("no selection initially", overlay.sel is None)
    QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(200, 200))
    QTest.mouseMove(overlay, QPoint(400, 350))
    QTest.mouseMove(overlay, QPoint(600, 500))
    QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(600, 500))
    check("selection = (200,200)+400x300", overlay.sel == QRectF(200, 200, 400, 300), str(overlay.sel))
    check("phase is ready", overlay.phase == "ready")

    # ------------------------------------------------------------------
    print("\n2. toolbars appear and are clickable")
    overlay._layout_toolbars()
    check("vertical toolbar placed", not overlay._v_rect.isEmpty())
    check("horizontal toolbar placed", not overlay._h_rect.isEmpty())
    check("vertical toolbar is right of the selection", overlay._v_rect.left() >= overlay.sel.right())
    check("horizontal toolbar is below the selection", overlay._h_rect.top() >= overlay.sel.bottom())
    check(
        "horizontal toolbar is right-aligned with the selection",
        abs(overlay._h_rect.right() - overlay.sel.right()) <= 1.0,
        f"{overlay._h_rect.right():.1f} vs {overlay.sel.right():.1f}",
    )
    v_actions = [a for _r, a, _i, _t in overlay._v_items]
    h_actions = [a for _r, a, _i, _t in overlay._h_items]
    check(
        "vertical order matches Lightshot",
        v_actions == ["pen", "line", "arrow", "rect", "marker", "text", "blur", "color", "undo"],
        str(v_actions),
    )
    check(
        "horizontal order matches the action set",
        h_actions == ["copy", "save", "close"],
        str(h_actions),
    )

    # ------------------------------------------------------------------
    print("\n3. picking the rectangle tool, then drawing inside the selection")
    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=button_center(overlay, "v", "rect"))
    check("tool switched to rect", overlay.tool == RECT, overlay.tool)
    QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(250, 250))
    QTest.mouseMove(overlay, QPoint(400, 400))
    QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(400, 400))
    check("one annotation recorded", len(overlay.annotations) == 1, str(len(overlay.annotations)))
    check("annotation is a rectangle", overlay.annotations[0].kind == RECT)
    rect_pts = overlay.annotations[0].points
    check("rectangle stored in image space", rect_pts[0] == QPointF(250, 250) and rect_pts[-1] == QPointF(400, 400))

    # ------------------------------------------------------------------
    print("\n4. freehand pen and undo")
    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=button_center(overlay, "v", "pen"))
    check("tool switched to pen", overlay.tool == PEN)
    QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(300, 300))
    for x in range(310, 420, 10):
        QTest.mouseMove(overlay, QPoint(x, 300 + (x % 40)))
    QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(410, 320))
    check("pen stroke recorded", len(overlay.annotations) == 2, str(len(overlay.annotations)))
    check("pen stroke has many points", len(overlay.annotations[-1].points) > 3, str(len(overlay.annotations[-1].points)))
    overlay.undo()
    check("undo removed the pen stroke", len(overlay.annotations) == 1)

    # ------------------------------------------------------------------
    print("\n5. marker, arrow, line")
    for action, kind in (("marker", MARKER), ("arrow", ARROW), ("line", "line")):
        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=button_center(overlay, "v", action))
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(300, 300))
        QTest.mouseMove(overlay, QPoint(360, 340))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(360, 340))
        check(f"{action} recorded", overlay.annotations[-1].kind == kind, overlay.annotations[-1].kind)
    marker = [a for a in overlay.annotations if a.kind == MARKER][0]
    check("marker is translucent", marker.color[3] < 255, str(marker.color[3]))

    # ------------------------------------------------------------------
    print("\n6. inline text tool")
    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=button_center(overlay, "v", "text"))
    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(500, 450))
    check("inline editor opened", overlay.text_edit is not None)
    overlay.text_edit.setText("LIGHT")
    overlay._commit_text()
    check("inline editor closed", overlay.text_edit is None)
    texts = [a for a in overlay.annotations if a.kind == TEXT]
    check("text annotation stored", len(texts) == 1 and texts[0].text == "LIGHT", str([t.text for t in texts]))

    # ------------------------------------------------------------------
    print("\n7. colour switcher")
    overlay._set_color(QColor("#00b8d4"))
    check("colour changed", overlay.color.name() == "#00b8d4")

    # ------------------------------------------------------------------
    print("\n8. crop / render result")
    overlay.tool = SELECT
    overlay.sel = QRectF(200, 200, 400, 300)
    result = overlay.render_result()
    check("crop is 400x300", (result.width(), result.height()) == (400, 300), f"{result.width()}x{result.height()}")
    corner = result.pixelColor(5, 5)
    check("crop shows the frozen pixels", corner.name() == "#203040", corner.name())
    # an annotation outside the crop must not leak in
    overlay.annotations.append(
        __import__("sekashot.annotations", fromlist=["Annotation"]).Annotation(
            RECT, [QPointF(1500, 900), QPointF(1700, 1000)], (255, 0, 0, 255), 4
        )
    )
    result2 = overlay.render_result()
    check("outside annotation does not change the crop size", result2.size() == result.size())

    # ------------------------------------------------------------------
    print("\n9. moving and resizing the selection")
    overlay.annotations.clear()
    QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(300, 300))
    QTest.mouseMove(overlay, QPoint(340, 330))
    QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(340, 330))
    check("selection moved by (40,30)", overlay.sel == QRectF(240, 230, 400, 300), str(overlay.sel))
    overlay._layout_toolbars()
    QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(640, 530))   # bottom-right handle
    QTest.mouseMove(overlay, QPoint(700, 600))
    QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(700, 600))
    check(
        "selection resized from the corner",
        overlay.sel == QRectF(240, 230, 460, 370),
        str(overlay.sel),
    )

    # ------------------------------------------------------------------
    print("\n10. click without dragging selects everything")
    overlay.tool = SELECT
    overlay._layout_toolbars()
    QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(1500, 900))
    QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(1200, 900))
    check("whole screen selected", overlay.sel == QRectF(0, 0, W, H), str(overlay.sel))

    # ------------------------------------------------------------------
    print("\n11. action buttons emit and close")
    overlay._layout_toolbars()
    captured: list = []
    overlay.copy_requested.connect(lambda img: captured.append(("copy", img.size())))
    overlay.save_requested.connect(lambda img: captured.append(("save", img.size())))
    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=button_center(overlay, "h", "save"))
    QTest.qWait(30)
    check("save emitted with the full-screen image", captured and captured[0][0] == "save", str(captured))
    check("save emitted the right size", captured and captured[0][1].width() == W, str(captured))

    # ------------------------------------------------------------------
    print("\n11b. a compositor-side close must still finalise the overlay")
    # Regression: without a closeEvent() override a WM/compositor close
    # (niri's `close-window`, Super+Q, the surface being destroyed) removed the
    # window while a standalone `--capture` run stayed alive forever with
    # nothing on screen - an invisible "ghost" process per press.
    wm_overlay = fresh_overlay(cfg)
    wm_events: list = []
    wm_overlay.finished.connect(lambda acted, pending: wm_events.append((acted, pending)))
    wm_overlay.close()  # exactly what the compositor does
    QTest.qWait(20)
    check("a bare close() emits finished", wm_events == [(False, "")], str(wm_events))
    check("a bare close() marks the overlay as acted", wm_overlay._acted is True)
    wm_overlay.close_overlay()  # late finalisation must be ignored
    QTest.qWait(10)
    check("a late _finish() is ignored (no double emit)", len(wm_events) == 1, str(len(wm_events)))
    # The widget sets WA_DeleteOnClose, so its C++ object is gone by now; only the
    # application must still have been told, which is what the checks above prove.

    # ------------------------------------------------------------------
    print("\n12. keyboard shortcuts")
    overlay2 = fresh_overlay(cfg)
    overlay2.sel = QRectF(100, 100, 300, 200)
    overlay2.phase = "ready"
    overlay2.annotations.append(
        __import__("sekashot.annotations", fromlist=["Annotation"]).Annotation(
            RECT, [QPointF(120, 120), QPointF(200, 200)], (255, 0, 0, 255), 3
        )
    )
    QTest.keyClick(overlay2, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    check("Ctrl+Z undid", len(overlay2.annotations) == 0)
    QTest.keyClick(overlay2, Qt.Key.Key_T)
    check("T selected the text tool", overlay2.tool == TEXT, overlay2.tool)
    QTest.keyClick(overlay2, Qt.Key.Key_V)
    check("V selected the select tool", overlay2.tool == SELECT, overlay2.tool)
    QTest.keyClick(overlay2, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
    check("Shift+Right nudged the selection by 10px", overlay2.sel.left() == 110.0, str(overlay2.sel))

    # ------------------------------------------------------------------
    print("\n13. on-screen annotation clipping (render-level)")
    cfg2 = Config()
    cfg2.marching_ants = False
    clip_overlay = fresh_overlay(cfg2)
    clip_overlay.sel = QRectF(400, 300, 400, 300)
    clip_overlay.phase = "ready"
    from sekashot.annotations import Annotation as _Ann

    clip_overlay.annotations = [
        _Ann(LINE, [QPointF(500, 450), QPointF(1500, 450)], (255, 0, 255, 255), 6)
    ]
    clip_overlay.tool = SELECT
    clip_overlay.update()
    QTest.qWait(40)
    rendered = clip_overlay.grab().toImage()
    inside = rendered.pixelColor(700, 450)
    outside = rendered.pixelColor(1000, 450)
    magenta_inside = inside.red() > 180 and inside.blue() > 180 and inside.green() < 120
    magenta_outside = outside.red() > 180 and outside.blue() > 180 and outside.green() < 120
    check("annotation is drawn inside the selection", magenta_inside, inside.name())
    check("annotation is clipped at the selection edge", not magenta_outside, outside.name())

    # the toolbars must be outside the selection and visible in the render
    v_probe = rendered.pixelColor(int(clip_overlay._v_rect.center().x()), int(clip_overlay._v_rect.center().y()))
    check("vertical toolbar is painted on screen", v_probe.lightness() > 150, v_probe.name())
    check("vertical toolbar sits right of the selection edge", clip_overlay._v_rect.left() >= clip_overlay.sel.right())

    # ------------------------------------------------------------------
    print("\n14. blur / redact tool")
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
    QTest.qWait(30)
    blur_overlay._compute_transform()
    blur_overlay.sel = QRectF(0, 0, 400, 300)
    blur_overlay.phase = "ready"
    blur_overlay._layout_toolbars()

    QTest.mouseClick(blur_overlay, Qt.MouseButton.LeftButton, pos=button_center(blur_overlay, "v", "blur"))
    check("tool switched to blur", blur_overlay.tool == BLUR, blur_overlay.tool)

    QTest.mousePress(blur_overlay, Qt.MouseButton.LeftButton, pos=QPoint(50, 50))
    QTest.mouseMove(blur_overlay, QPoint(200, 150))
    QTest.mouseRelease(blur_overlay, Qt.MouseButton.LeftButton, pos=QPoint(200, 150))
    check("blur annotation recorded", len(blur_overlay.annotations) == 1, str(len(blur_overlay.annotations)))
    blur_ann = blur_overlay.annotations[-1]
    check("blur annotation kind", blur_ann.kind == BLUR, blur_ann.kind)
    check("blur stores a radius in its width field", blur_ann.width == float(cfg.blur_radius), str(blur_ann.width))
    check("blur rect is in image space", blur_overlay._blur_rect(blur_ann) == QRect(50, 50, 150, 100),
          str(blur_overlay._blur_rect(blur_ann)))

    def _contrast(image, x0, y0, x1, y1):
        vals = [image.pixelColor(x, y).lightness() for x in range(x0, x1, 3) for y in range(y0, y1, 3)]
        return max(vals) - min(vals)

    blurred_out = blur_overlay.render_result()
    inside = _contrast(blurred_out, 60, 60, 190, 140)
    outside = _contrast(blurred_out, 250, 200, 380, 290)
    check("blur destroys the checkerboard detail inside the region", inside < 20, str(inside))
    check("pixels outside the blur region stay sharp", outside > 200, str(outside))
    check("blurred region is flattened to a mid tone", 60 < blurred_out.pixelColor(120, 100).lightness() < 200,
          blurred_out.pixelColor(120, 100).name())
    check("blur is cached per region", len(blur_overlay._blur_cache) >= 1, str(len(blur_overlay._blur_cache)))

    # a solid colour must survive a blur unchanged (edge handling is correct)
    solid = QImage(200, 120, QImage.Format.Format_ARGB32_Premultiplied)
    solid.fill(QColor("#3080c0"))
    from sekashot.blur import blur_image

    solid_blur = blur_image(solid, 14)
    probes = [solid_blur.pixelColor(x, y) for x in (0, 1, 100, 198, 199) for y in (0, 1, 60, 118, 119)]
    check("solid colour survives a blur (no edge fade)",
          all(abs(c.red() - 0x30) <= 2 and abs(c.green() - 0x80) <= 2 and abs(c.blue() - 0xC0) <= 2 for c in probes),
          str(sorted({c.name() for c in probes})))

    blur_overlay.undo()
    check("undo removes the blur", len(blur_overlay.annotations) == 0)
    blur_overlay.close()

    # Ctrl+wheel over the canvas changes the blur strength while the tool is active
    cfg3 = Config()
    cfg3.marching_ants = False
    wheel_overlay = fresh_overlay(cfg3)
    wheel_overlay.tool = BLUR
    before = cfg3.blur_radius
    from PyQt6.QtGui import QWheelEvent

    wheel_event = QWheelEvent(
        QPointF(300, 300), QPointF(300, 300), QPoint(0, 0), QPoint(0, 120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier, Qt.ScrollPhase.NoScrollPhase, False,
    )
    wheel_overlay.wheelEvent(wheel_event)
    check("Ctrl+wheel increases the blur radius", cfg3.blur_radius > before, f"{before} -> {cfg3.blur_radius}")
    wheel_overlay.close()

    # ------------------------------------------------------------------
    print("\n15. config round-trip")
    cfg.marker_width = 33
    cfg.copy_format = "file-url"
    cfg.save()
    from sekashot.config import Config as Cfg

    reloaded = Cfg.load()
    check("config persisted", reloaded.marker_width == 33 and reloaded.copy_format == "file-url")

    print("\n16. notification routing falls back sensibly")
    # A standalone `--capture` run has no tray icon, so the old tray-only helper
    # reported nothing at all after Copy/Save.  Notifications must walk down the
    # D-Bus -> notify-send -> tray -> stderr ladder instead of going silent.
    import contextlib
    import io

    import sekashot.app as app_module

    real_dbus = app_module._notify_dbus
    real_which = app_module.shutil.which

    class _Tray:
        def __init__(self, visible: bool) -> None:
            self._visible = visible
            self.calls: list[tuple] = []

        def isVisible(self) -> bool:
            return self._visible

        def showMessage(self, *args) -> None:
            self.calls.append(args)

    try:
        app_module._notify_dbus = lambda title, message: False
        app_module.shutil.which = lambda name: None

        tray = _Tray(visible=True)
        app_module._notify(tray, "Sekashot", "Copied.")
        check("D-Bus down -> the tray is used", len(tray.calls) == 1, str(tray.calls))

        hidden = _Tray(visible=False)
        app_module._notify(hidden, "Sekashot", "Copied.")
        check("a hidden tray is not a notification route", hidden.calls == [])

        check(
            "notify-send is reported missing when it is not installed",
            app_module._notify_send("Sekashot", "Copied.") is False,
        )

        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            app_module._notify(None, "Sekashot", "Copied.")
        check("with nothing left it still reports on stderr", "Copied." in buffer.getvalue())
    finally:
        app_module._notify_dbus = real_dbus
        app_module.shutil.which = real_which

    print("\n17. capture backends never hand out a black frame")
    # Regression test: on Wayland the old code fell through to the X11 grab,
    # which does not fail loudly - it returns a black rectangle.  That black
    # frame was shown as if it were a real screenshot.
    import sekashot.capture as cap

    def _black_image(w: int = 320, h: int = 200) -> QImage:
        img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(QColor(0, 0, 0))
        return img

    def _colour_image(w: int = 320, h: int = 200) -> QImage:
        img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(QColor("#2f7ebf"))
        return img

    real_is_wayland = cap.is_wayland
    real_grim = cap._grim_capture
    real_portal = cap._portal_capture
    real_x11 = cap._x11_capture
    real_screenshot = cap.Screenshot

    try:
        # --- black frame detection -----------------------------------
        check("an all-black image is recognised", cap.is_uniformly_black(_black_image()))
        check(
            "a coloured image is not black",
            not cap.is_uniformly_black(_colour_image()),
        )
        one_bright = _black_image()
        one_bright.setPixelColor(319, 199, QColor(255, 255, 255))
        check(
            "a single lit pixel is enough to clear the black verdict",
            not cap.is_uniformly_black(one_bright),
        )
        check("a null image is not 'uniformly black'", not cap.is_uniformly_black(QImage()))

        # --- Wayland: no helper -> an error, never a black frame ----
        cap.is_wayland = lambda: True
        cap._grim_capture = lambda *a, **k: None
        cap._portal_capture = lambda *a, **k: None
        cap._x11_capture = lambda: cap.Screenshot(_black_image(), None)

        raised = None
        try:
            cap.grab_desktop()
        except cap.CaptureError as exc:
            raised = exc
        check(
            "Wayland without grim/portal raises CaptureError",
            raised is not None,
            type(raised).__name__ if raised else "no error",
        )
        check(
            "the error names grim and the desktop portal",
            raised is not None
            and "grim" in str(raised)
            and "portal" in str(raised),
        )

        # --- a backend that hands back black is still a failure -----
        cap._grim_capture = lambda *a, **k: _black_image()
        raised = None
        try:
            cap.grab_desktop()
        except cap.CaptureError as exc:
            raised = exc
        check(
            "a black frame from grim raises CaptureError",
            raised is not None,
            type(raised).__name__ if raised else "no error",
        )
        check(
            "the black-frame error mentions allow_black_capture",
            raised is not None and "allow_black_capture" in str(raised),
        )

        # --- ... unless the user opted in ----------------------------
        try:
            shot = cap.grab_desktop(allow_black=True)
            opted_in = shot is not None and cap.is_uniformly_black(shot.image)
        except cap.CaptureError:
            opted_in = False
        check("allow_black_capture=True accepts the black frame", opted_in)

        # --- X11: a grab that yields nothing is an error -------------
        cap.is_wayland = lambda: False
        cap._x11_capture = lambda: None
        raised = None
        try:
            cap.grab_desktop()
        except cap.CaptureError as exc:
            raised = exc
        check(
            "an empty X11 grab raises CaptureError",
            raised is not None,
            type(raised).__name__ if raised else "no error",
        )

        # --- X11: a grab that yields black is an error ---------------
        cap._x11_capture = lambda: cap.Screenshot(_black_image(), None)
        raised = None
        try:
            cap.grab_desktop()
        except cap.CaptureError as exc:
            raised = exc
        check(
            "a black X11 grab raises CaptureError",
            raised is not None,
            type(raised).__name__ if raised else "no error",
        )

        # --- X11: a working grab still works -------------------------
        good = _colour_image()
        cap._x11_capture = lambda: cap.Screenshot(good, None)
        try:
            shot = cap.grab_desktop()
            worked = shot is not None and shot.image is good
        except cap.CaptureError:
            worked = False
        check("a healthy X11 grab is returned untouched", worked)
    finally:
        cap.is_wayland = real_is_wayland
        cap._grim_capture = real_grim
        cap._portal_capture = real_portal
        cap._x11_capture = real_x11
        cap.Screenshot = real_screenshot

    print("\n18. no quality loss between capture and export")

    def _diff(a, b) -> tuple[int, int]:
        """(differing pixels, worst per-channel delta) of two equal-sized images."""
        if a.width() != b.width() or a.height() != b.height():
            return (-1, -1)
        a32 = a.convertToFormat(QImage.Format.Format_RGB32)
        b32 = b.convertToFormat(QImage.Format.Format_RGB32)
        n = a32.width() * a32.height()
        sa = a32.constBits().asstring(n * 4)
        sb = b32.constBits().asstring(n * 4)
        bad = 0
        worst = 0
        for i in range(0, len(sa), 4):
            d = abs(sa[i] - sb[i])
            if d:
                bad += 1
                worst = max(worst, d)
        return bad, worst

    # a colourful source image with hard edges in every direction
    src = QImage(800, 600, QImage.Format.Format_ARGB32_Premultiplied)
    src.fill(QColor("#101820"))
    p = QPainter(src)
    for i in range(0, 800, 37):
        p.setPen(QColor((i * 7) % 256, (i * 13) % 256, (i * 29) % 256))
        p.drawLine(i, 0, i, 600)
    p.setPen(QColor("#ff0000"))
    p.drawRect(120, 90, 300, 200)
    p.end()

    # the export must be pixel-identical to the captured region, even when the
    # on-screen overlay is scaled down (which is what a HiDPI display does)
    ov = CaptureOverlay(src, Config())
    ov.resize(src.width() // 2, src.height() // 2)
    x0, y0, w, h = 137, 211, 400, 300
    ov.sel = QRectF(ov.to_widget(QPointF(x0, y0)), ov.to_widget(QPointF(x0 + w, y0 + h)))
    out = ov.render_result()
    check(
        "the export keeps the selection size",
        out.width() == w and out.height() == h,
        f"{out.width()}x{out.height()}",
    )
    bad, worst = _diff(out, src.copy(x0, y0, w, h))
    check(
        "the export is pixel-identical to the capture (no resampling)",
        bad == 0,
        f"{bad} differing px, max delta {worst}",
    )
    check(
        "the export's device pixel ratio is normalised to 1",
        out.devicePixelRatio() == 1.0,
        str(out.devicePixelRatio()),
    )

    # an annotation may only touch the pixels it covers
    ov.annotations = [
        Annotation(
            kind=RECT,
            color=(255, 0, 0, 255),
            width=3,
            points=[QPointF(x0 + 40, y0 + 40), QPointF(x0 + 160, y0 + 120)],
        )
    ]
    ann_out = ov.render_result()
    untouched = _diff(
        ann_out.copy(0, 0, w, 30), src.copy(x0, y0, w, 30)
    )[0]
    check(
        "an annotation leaves the surrounding pixels untouched",
        untouched == 0,
        f"{untouched} differing px",
    )

    # the X11 grab must keep the panel's native resolution
    src_geom = QRect(0, 0, 1920, 1080)
    src_dpr = 2.0
    grabbed = QImage(3840, 2160, QImage.Format.Format_ARGB32_Premultiplied)
    grabbed.fill(QColor("#2f7ebf"))
    grabbed.setDevicePixelRatio(src_dpr)

    class _FakeScreen:
        """A HiDPI screen whose grab comes back at the panel's resolution."""

        def geometry(self):
            return src_geom

        def devicePixelRatio(self):
            return src_dpr

        def grabWindow(self, *_args):
            pm = QPixmap.fromImage(grabbed)
            return pm

    class _FakeScreens(list):
        pass

    real_screens = cap.QGuiApplication.screens
    try:
        cap.QGuiApplication.screens = staticmethod(lambda: _FakeScreens([_FakeScreen()]))
        hi = cap._x11_capture()
        check(
            "the X11 grab keeps the panel's native resolution",
            hi is not None and hi.image.width() == 3840 and hi.image.height() == 2160,
            f"{hi.image.width()}x{hi.image.height()}" if hi else "no capture",
        )
    finally:
        cap.QGuiApplication.screens = real_screens

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

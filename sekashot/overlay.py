"""The frozen-screen overlay: Sekashot's editor.

The overlay paints **everything** inside a single full-screen window (frozen
screenshot, dim mask, selection, both toolbars, size badge and toasts).  That is
deliberate: Wayland clients cannot position extra top-level windows, so keeping
one window guarantees the toolbars always sit next to the selection, exactly
like the original.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetricsF,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
)
from PyQt6.QtWidgets import QApplication, QColorDialog, QLineEdit, QWidget

from . import icons
from .annotations import (
    ARROW,
    BLUR,
    DRAW_TOOLS,
    DRAW_TOOL_IDS,
    LINE,
    MARKER,
    PALETTE,
    PEN,
    RECT,
    SELECT,
    TEXT,
    Annotation,
    draw_annotation,
)
from .blur import blur_region_of

BUTTON = 34
PAD = 3
TOOLBAR_BG = QColor("#f6f6f6")
TOOLBAR_BORDER = QColor("#b3b3b3")
TOOLBAR_HOVER = QColor("#dcdcdc")
TOOLBAR_ACTIVE = QColor("#c3c3c3")


class _InlineEdit(QLineEdit):
    """Single-line text editor used by the Text tool."""

    escaped = pyqtSignal()

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.escaped.emit()
            return
        super().keyPressEvent(event)


class PalettePopup(QWidget):
    """Small colour palette that pops out of the swatch button."""

    chosen = pyqtSignal(QColor)

    def __init__(self, current: QColor) -> None:
        super().__init__(None, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self._buttons: list[tuple[QRectF, QColor]] = []

        cols = 6
        cell = 26
        pad = 8
        rows = math.ceil(len(PALETTE) / cols)
        width = cols * cell + 2 * pad
        height = rows * cell + 2 * pad + 30
        self.resize(width, height)

        for i, value in enumerate(PALETTE):
            r = i // cols
            c = i % cols
            self._buttons.append((QRectF(pad + c * cell, pad + r * cell, cell - 2, cell - 2), QColor(value)))
        self._custom_rect = QRectF(pad, pad + rows * cell + 4, width - 2 * pad, 22)
        self._current = current

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(TOOLBAR_BORDER, 1))
        p.setBrush(TOOLBAR_BG)
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 6, 6)

        pos = self.mapFromGlobal(QCursor.pos())
        for rect, colour in self._buttons:
            p.setPen(QPen(QColor("#8a8a8a"), 1))
            p.setBrush(colour)
            p.drawRoundedRect(rect, 3, 3)
            if rect.contains(QPointF(pos)):
                p.setPen(QPen(QColor("#1f6feb"), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), 4, 4)

        p.setPen(QPen(QColor("#4a4a4a"), 1))
        p.setBrush(QColor("#ffffff"))
        p.drawRoundedRect(self._custom_rect, 4, 4)
        p.drawText(self._custom_rect, int(Qt.AlignmentFlag.AlignCenter), "Custom…")
        p.end()

    def mousePressEvent(self, event):  # noqa: N802
        pos = QPointF(event.position())
        for rect, colour in self._buttons:
            if rect.contains(pos):
                self.chosen.emit(colour)
                self.close()
                return
        if self._custom_rect.contains(pos):
            colour = QColorDialog.getColor(self._current, None, "Annotation colour")
            if colour.isValid():
                self.chosen.emit(colour)
            self.close()


class CaptureOverlay(QWidget):
    """Full-screen frozen screenshot with Sekashot's editor on top."""

    copy_requested = pyqtSignal(object)            # QImage
    save_requested = pyqtSignal(object)            # QImage
    #: (did_the_user_act, pending_action) - the second argument names the action
    #: that is about to run ("copy"/"save"/"") so the application knows a modal
    #: dialog or a clipboard hand-off is still outstanding and must not tear the
    #: process down yet.
    finished = pyqtSignal(bool, str)

    def __init__(self, shot: QImage, cfg, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.shot = shot

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setWindowTitle("Sekashot")
        self.setCursor(Qt.CursorShape.CrossCursor)

        # geometry mapping (filled in by _compute_transform)
        self.scale = 1.0
        self.img_rect = QRectF()

        # editor state
        self.tool = SELECT
        self.sel: QRectF | None = None
        self.phase = "idle"          # idle | selecting | ready
        self.annotations: list[Annotation] = []
        self.current: Annotation | None = None
        self.mode: str | None = None
        self.active_handle: int | None = None
        self._anchor = QPointF()
        self._drag_origin = QPointF()
        self._sel_origin = QRectF()
        self._moved = False
        self._acted = False

        self.color = QColor(cfg.color)
        self._w_pen = float(cfg.pen_width)
        self._w_marker = float(cfg.marker_width)
        self._w_blur = float(cfg.blur_radius)
        self._font_size = float(cfg.font_size)

        # blurred crops of the frozen screenshot, keyed by (rect, radius);
        # dragging repaints on every mouse move, so caching keeps it smooth
        self._blur_cache: dict[tuple[int, int, int, int, int], QImage] = {}

        # toolbars
        self._v_rect = QRectF()
        self._h_rect = QRectF()
        self._v_items: list[tuple[QRectF, str, str, str]] = []
        self._h_items: list[tuple[QRectF, str, str, str]] = []
        self._hover: str | None = None

        # inline text editor
        self.text_edit: _InlineEdit | None = None

        # marching-ants animation
        self._dash = 0.0
        self._ants = QTimer(self)
        self._ants.setInterval(60)
        self._ants.timeout.connect(self._tick_ants)
        if cfg.marching_ants:
            self._ants.start()

        # transient toast
        self._toast_text = ""
        self._toast_error = False
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._clear_toast)

        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        self._screen = screen
        if screen is not None:
            self.setGeometry(screen.geometry())

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        geo = self._screen.geometry() if self._screen else self.geometry()
        self.setGeometry(geo)
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.update()

    def _finish(self, acted: bool, pending: str = "") -> None:
        if self._acted:
            return
        self._acted = True
        self._commit_text()
        self.hide()
        self.finished.emit(acted, pending)
        self.close()

    def close_overlay(self) -> None:
        self._finish(True)

    def closeEvent(self, event):  # noqa: N802
        """Finalise on *any* close, not only the ones we start ourselves.

        Esc, Ctrl+C / Ctrl+S and the toolbar buttons all go through
        :meth:`_finish`, but a compositor-side close (niri's ``close-window``, a
        WM shortcut, the surface being destroyed) would otherwise take the
        window away while the application - for a standalone ``--capture`` run -
        stayed alive forever with nothing on screen.  Report the overlay as
        finished first, then let the close proceed.
        """
        if not self._acted:
            self._acted = True
            self._commit_text()
            self.finished.emit(False, "")
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # coordinate mapping
    # ------------------------------------------------------------------
    def _compute_transform(self) -> None:
        iw, ih = self.shot.width(), self.shot.height()
        w, h = max(1, self.width()), max(1, self.height())
        if iw <= 0 or ih <= 0:
            self.scale = 1.0
            self.img_rect = QRectF(0, 0, w, h)
            return
        s = min(w / iw, h / ih)
        self.scale = s
        self.img_rect = QRectF((w - iw * s) / 2.0, (h - ih * s) / 2.0, iw * s, ih * s)
        # stroke sizes are stored in *screen* px and converted to image px so the
        # on-screen result always matches the configured pen width.
        self._w_pen = max(1.0, self.cfg.pen_width / s)
        self._w_marker = max(2.0, self.cfg.marker_width / s)
        self._w_blur = max(1.0, self.cfg.blur_radius / s)
        self._font_size = max(6.0, self.cfg.font_size / s)

    def to_image(self, point: QPointF) -> QPointF:
        return QPointF(
            (point.x() - self.img_rect.left()) / self.scale,
            (point.y() - self.img_rect.top()) / self.scale,
        )

    def to_widget(self, point: QPointF) -> QPointF:
        return QPointF(
            self.img_rect.left() + point.x() * self.scale,
            self.img_rect.top() + point.y() * self.scale,
        )

    def _clamp_to_image(self, rect: QRectF) -> QRectF:
        r = QRectF(rect).normalized()
        if r.width() > self.img_rect.width():
            r.setWidth(self.img_rect.width())
        if r.height() > self.img_rect.height():
            r.setHeight(self.img_rect.height())
        if r.left() < self.img_rect.left():
            r.moveLeft(self.img_rect.left())
        if r.top() < self.img_rect.top():
            r.moveTop(self.img_rect.top())
        if r.right() > self.img_rect.right():
            r.moveRight(self.img_rect.right())
        if r.bottom() > self.img_rect.bottom():
            r.moveBottom(self.img_rect.bottom())
        return r

    def _clip_rect(self) -> QRectF:
        if self.sel is not None:
            return self.sel.intersected(self.img_rect)
        return self.img_rect

    # ------------------------------------------------------------------
    # painting
    # ------------------------------------------------------------------
    def resizeEvent(self, event):  # noqa: N802
        self._compute_transform()
        super().resizeEvent(event)

    def paintEvent(self, event):  # noqa: N802
        self._compute_transform()
        self._layout_toolbars()

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#000000"))
        p.drawImage(self.img_rect, self.shot)

        # dim mask
        p.fillRect(self.img_rect, QColor(0, 0, 0, self.cfg.dim_alpha))

        # the "hole": redraw the frozen image inside the selection, undimmed
        if self.sel is not None:
            hole = self.sel.intersected(self.img_rect)
            if not hole.isEmpty():
                p.save()
                p.setClipRect(hole)
                p.drawImage(self.img_rect, self.shot)
                p.restore()

        # annotations (clipped to the selection, exactly like Lightshot)
        p.save()
        p.setClipRect(self._clip_rect())
        for a in self.annotations:
            if a.kind == BLUR:
                self._paint_blur(p, a)
            else:
                draw_annotation(p, a)
        if self.current is not None:
            if self.current.kind == BLUR:
                self._paint_blur(p, self.current, outline=True)
            else:
                draw_annotation(p, self.current)
        p.restore()

        if self.sel is not None:
            self._paint_selection(p)
            if self.cfg.show_size_badge:
                self._paint_size_badge(p)

        self._paint_toolbars(p)
        self._paint_toast(p)
        p.end()

    def _paint_selection(self, p: QPainter) -> None:
        r = self.sel
        if r is None:
            return
        inner = QPen(QColor("#ffffff"), 1, Qt.PenStyle.SolidLine)
        outer = QPen(QColor("#111111"), 1, Qt.PenStyle.CustomDashLine)
        outer.setDashPattern([4, 4])
        outer.setDashOffset(self._dash)

        p.save()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(inner)
        p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))
        p.setPen(outer)
        p.drawRect(r.adjusted(-0.5, -0.5, 0.5, 0.5))
        p.restore()

        if self.phase == "ready":
            p.save()
            p.setPen(QPen(QColor("#111111"), 1))
            p.setBrush(QColor("#ffffff"))
            for centre in self._handle_points().values():
                p.drawRect(QRectF(centre.x() - 3.5, centre.y() - 3.5, 7, 7))
            p.restore()

    def _handle_points(self) -> dict[int, QPointF]:
        r = self.sel
        assert r is not None
        cx = r.center().x()
        cy = r.center().y()
        return {
            0: QPointF(r.left(), r.top()),
            1: QPointF(cx, r.top()),
            2: QPointF(r.right(), r.top()),
            3: QPointF(r.right(), cy),
            4: QPointF(r.right(), r.bottom()),
            5: QPointF(cx, r.bottom()),
            6: QPointF(r.left(), r.bottom()),
            7: QPointF(r.left(), cy),
        }

    def _paint_size_badge(self, p: QPainter) -> None:
        if self.sel is None:
            return
        _x, _y, iw, ih = self.crop_rect()
        text = f"{iw}x{ih}"

        font = QFont(self.cfg.font_family)
        font.setPixelSize(12)
        font.setBold(True)
        p.save()
        p.setFont(font)
        fm = QFontMetricsF(font)
        w = fm.horizontalAdvance(text) + 16
        h = fm.height() + 6

        # Lightshot puts the badge above the selection's top-left corner
        x = self.sel.left()
        y = self.sel.top() - h - 4
        if y < 2:
            y = self.sel.top() + 4
        if x + w > self.width() - 2:
            x = self.width() - w - 2
        x = max(2.0, x)

        badge = QRectF(x, y, w, h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(28, 28, 28, 225))
        p.drawRoundedRect(badge, 4, 4)
        p.setPen(QColor("#ffffff"))
        p.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), text)
        p.restore()

    def _paint_toolbars(self, p: QPainter) -> None:
        if self.sel is None:
            return
        for rect, items in ((self._v_rect, self._v_items), (self._h_rect, self._h_items)):
            if rect.isEmpty():
                continue
            p.save()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 45))
            p.drawRoundedRect(rect.translated(1, 2), 6, 6)
            p.setPen(QPen(TOOLBAR_BORDER, 1))
            p.setBrush(TOOLBAR_BG)
            p.drawRoundedRect(rect, 6, 6)
            p.restore()

            # separator before the close button
            if items and items[-1][1] == "close" and len(items) >= 2:
                prev = items[-2][0]
                sep_x = prev.right() + 4.5
                p.save()
                p.setPen(QPen(QColor("#c2c2c2"), 1))
                p.drawLine(QPointF(sep_x, rect.top() + 6), QPointF(sep_x, rect.bottom() - 6))
                p.restore()

            for btn, action, icon_name, tooltip in items:
                if action == self._hover:
                    p.save()
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(TOOLBAR_HOVER)
                    p.drawRoundedRect(btn.adjusted(1, 1, -1, -1), 4, 4)
                    p.restore()
                if action in DRAW_TOOL_IDS and action == self.tool:
                    p.save()
                    p.setPen(QPen(QColor("#9a9a9a"), 1))
                    p.setBrush(TOOLBAR_ACTIVE)
                    p.drawRoundedRect(btn.adjusted(1, 1, -1, -1), 4, 4)
                    p.restore()

                accent = self.color.name() if action == "color" else None
                pm = icons.pixmap(icon_name, size=20, accent=accent)
                target = QPointF(btn.center().x() - 10, btn.center().y() - 10)
                p.drawPixmap(target, pm)

    def _paint_toast(self, p: QPainter) -> None:
        if not self._toast_text:
            return
        font = QFont(self.cfg.font_family)
        font.setPixelSize(13)
        p.save()
        p.setFont(font)
        fm = QFontMetricsF(font)
        lines = self._toast_text.split("\n")
        w = max(fm.horizontalAdvance(line) for line in lines) + 32
        h = fm.height() * len(lines) + 20
        rect = QRectF((self.width() - w) / 2.0, self.height() - h - 48, w, h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(20, 20, 20, 235))
        p.drawRoundedRect(rect, 8, 8)
        p.setPen(QColor("#ff8080") if self._toast_error else QColor("#e8ffe8"))
        p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), self._toast_text)
        p.restore()

    def _tick_ants(self) -> None:
        self._dash = (self._dash + 1) % 8
        if self.sel is not None:
            self.update()

    # ------------------------------------------------------------------
    # toolbar layout / hit testing
    # ------------------------------------------------------------------
    def _layout_toolbars(self) -> None:
        self._v_items = []
        self._h_items = []
        self._v_rect = QRectF()
        self._h_rect = QRectF()
        if self.sel is None or self.sel.width() < 2 or self.sel.height() < 2:
            return

        v_items = [(t, t, tip) for t, t, tip in DRAW_TOOLS]
        v_items.append(("color", "color", "Colour"))
        v_items.append(("undo", "undo", "Undo (Ctrl+Z)"))

        w = BUTTON + 2 * PAD
        h = len(v_items) * BUTTON + 2 * PAD
        x = self.sel.right() + 8
        if x + w > self.width() - 2:
            x = self.sel.left() - 8 - w
        x = min(max(x, 2.0), max(2.0, self.width() - w - 2))
        y = self.sel.top() + (self.sel.height() - h) / 2.0
        y = min(max(y, 2.0), max(2.0, self.height() - h - 2))
        self._v_rect = QRectF(x, y, w, h)
        for i, (action, icon_name, tip) in enumerate(v_items):
            self._v_items.append((QRectF(x + PAD, y + PAD + i * BUTTON, BUTTON, BUTTON), action, icon_name, tip))

        h_items = [
            ("copy", "copy", "Copy to clipboard (Ctrl+C)"),
            ("save", "save", "Save as… (Ctrl+S)"),
            ("close", "close", "Close (Esc)"),
        ]
        sep = 10
        w2 = len(h_items) * BUTTON + sep + 2 * PAD
        h2 = BUTTON + 2 * PAD
        # Right-aligned with the selection: the bar hugs the bottom-right corner,
        # which is where the eye already is after dragging a selection out.
        x2 = self.sel.right() - w2
        if x2 < 2:
            x2 = self.sel.left()
        x2 = min(max(x2, 2.0), max(2.0, self.width() - w2 - 2))
        y2 = self.sel.bottom() + 8
        if y2 + h2 > self.height() - 2:
            y2 = self.sel.top() - 8 - h2
        y2 = min(max(y2, 2.0), max(2.0, self.height() - h2 - 2))
        self._h_rect = QRectF(x2, y2, w2, h2)
        cx = x2 + PAD
        for i, (action, icon_name, tip) in enumerate(h_items):
            if action == "close":
                cx += sep
            self._h_items.append((QRectF(cx, y2 + PAD, BUTTON, BUTTON), action, icon_name, tip))
            cx += BUTTON

    def _toolbar_at(self, pos: QPointF) -> tuple[str, str] | None:
        for rect, action, _icon, tooltip in self._v_items + self._h_items:
            if rect.contains(pos):
                return action, tooltip
        return None

    # ------------------------------------------------------------------
    # mouse
    # ------------------------------------------------------------------
    def _handle_at(self, pos: QPointF) -> int | None:
        if self.sel is None or self.phase != "ready":
            return None
        for index, centre in self._handle_points().items():
            if QRectF(centre.x() - 5, centre.y() - 5, 10, 10).contains(pos):
                return index
        return None

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self._finish(False)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return

        pos = QPointF(event.position())
        self._commit_text()
        self._layout_toolbars()

        hit = self._toolbar_at(pos)
        if hit is not None:
            self._activate_toolbar(hit[0], pos)
            return
        self._hover = None

        handle = self._handle_at(pos)
        if handle is not None:
            self.mode = "resize"
            self.active_handle = handle
            self._anchor = pos
            self._sel_origin = QRectF(self.sel)
            self._moved = False
            return

        if self.sel is not None and self.phase == "ready" and self.sel.contains(pos) and self.tool == SELECT:
            self.mode = "move"
            self._drag_origin = pos
            self._sel_origin = QRectF(self.sel)
            self._moved = False
            return

        if self.tool == TEXT and self.sel is not None and self.sel.contains(pos):
            self._start_text(pos)
            return

        if self.tool == SELECT:
            self.mode = "new"
            self._anchor = self._clamp_point(pos)
            self.sel = QRectF(self._anchor, self._anchor)
            self.phase = "selecting"
            self.annotations.clear()
            self.current = None
            self.update()
            return

        # drawing tool
        if self.sel is None:
            self.sel = QRectF(self.img_rect)
            self.phase = "ready"
        if not self.sel.contains(pos):
            return
        self.mode = "draw"
        self._start_drawing(pos)

    def mouseMoveEvent(self, event):  # noqa: N802
        pos = QPointF(event.position())

        if self.mode == "new":
            self.sel = QRectF(self._anchor, self._clamp_point(pos)).normalized()
            self._moved = True
            self.update()
            return

        if self.mode == "move" and self.sel is not None:
            delta = pos - self._drag_origin
            moved = self._sel_origin.translated(delta)
            moved = self._clamp_to_image(moved)
            self.sel = moved
            self._moved = True
            self.update()
            return

        if self.mode == "resize" and self.sel is not None:
            self.sel = self._resize(self._sel_origin, self.active_handle, self._clamp_point(pos))
            self._moved = True
            self.update()
            return

        if self.mode == "draw" and self.current is not None:
            point = self.to_image(self._clamp_point(pos))
            if self.current.kind in (PEN, MARKER):
                last = self.current.points[-1]
                if math.hypot(point.x() - last.x(), point.y() - last.y()) * self.scale >= 1.6:
                    self.current.points.append(point)
            else:
                if len(self.current.points) == 1:
                    self.current.points.append(point)
                else:
                    self.current.points[-1] = point
            self.update()
            return

        # hover feedback
        self._layout_toolbars()
        hit = self._toolbar_at(pos)
        new_hover = hit[0] if hit else None
        if new_hover != self._hover:
            self._hover = new_hover
            self.update()

        handle = self._handle_at(pos)
        if hit:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        elif handle is not None:
            self.setCursor(
                {
                    0: Qt.CursorShape.SizeFDiagCursor,
                    4: Qt.CursorShape.SizeFDiagCursor,
                    2: Qt.CursorShape.SizeBDiagCursor,
                    6: Qt.CursorShape.SizeBDiagCursor,
                    1: Qt.CursorShape.SizeVerCursor,
                    5: Qt.CursorShape.SizeVerCursor,
                    3: Qt.CursorShape.SizeHorCursor,
                    7: Qt.CursorShape.SizeHorCursor,
                }[handle]
            )
        elif self.sel is not None and self.sel.contains(pos) and self.tool == SELECT:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        mode = self.mode
        self.mode = None
        self.active_handle = None

        if mode == "new":
            if self.sel is not None and (self.sel.width() < 4 or self.sel.height() < 4):
                # a plain click selects the whole screen (Lightshot behaviour)
                self.sel = QRectF(self.img_rect)
            self.phase = "ready"
            self.update()
        elif mode == "draw" and self.current is not None:
            if self.current.kind in (LINE, ARROW, RECT, BLUR) and len(self.current.points) < 2:
                self.current = None
            else:
                self.annotations.append(self.current)
                self.current = None
            self.update()
        elif mode in ("move", "resize"):
            self.update()

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self.sel is not None and self.sel.contains(QPointF(event.position())):
            self._emit("copy")

    def wheelEvent(self, event):  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120.0
            if self.tool == MARKER:
                self.cfg.marker_width = int(max(4, min(60, self.cfg.marker_width + steps * 2)))
            elif self.tool == BLUR:
                self.cfg.blur_radius = int(max(2, min(60, self.cfg.blur_radius + steps * 2)))
            else:
                self.cfg.pen_width = int(max(1, min(20, self.cfg.pen_width + steps)))
            self._blur_cache.clear()
            self._compute_transform()
            self.update()
            event.accept()
            return
        super().wheelEvent(event)

    def _clamp_point(self, point: QPointF) -> QPointF:
        return QPointF(
            min(max(point.x(), self.img_rect.left()), self.img_rect.right()),
            min(max(point.y(), self.img_rect.top()), self.img_rect.bottom()),
        )

    def _resize(self, origin: QRectF, handle: int | None, point: QPointF) -> QRectF:
        r = QRectF(origin)
        if handle in (0, 1, 2):
            r.setTop(point.y())
        if handle in (4, 5, 6):
            r.setBottom(point.y())
        if handle in (0, 6, 7):
            r.setLeft(point.x())
        if handle in (2, 3, 4):
            r.setRight(point.x())
        r = self._clamp_to_image(r)
        if r.width() < 4:
            r.setWidth(4)
        if r.height() < 4:
            r.setHeight(4)
        return r

    def _start_drawing(self, pos: QPointF) -> None:
        point = self.to_image(self._clamp_point(pos))
        if self.tool == MARKER:
            colour = (self.color.red(), self.color.green(), self.color.blue(), self.cfg.marker_alpha)
            width = self._w_marker
        elif self.tool == BLUR:
            colour = (255, 255, 255, 255)   # unused: a blur paints source pixels
            width = self._w_blur
        else:
            colour = (self.color.red(), self.color.green(), self.color.blue(), 255)
            width = self._w_pen
        self.current = Annotation(
            kind=self.tool,
            points=[point],
            color=colour,
            width=width,
            font_family=self.cfg.font_family,
            font_size=int(round(self._font_size)),
        )
        self.update()

    # ------------------------------------------------------------------
    # inline text
    # ------------------------------------------------------------------
    def _start_text(self, pos: QPointF) -> None:
        self._commit_text()
        edit = _InlineEdit(self)
        edit.setFrame(True)
        edit.setStyleSheet(
            "QLineEdit {"
            " background: rgba(255,255,255,0.10);"
            " border: 1px dashed rgba(255,255,255,0.85);"
            f" color: {self.color.name()};"
            " padding: 1px 3px;"
            "}"
        )
        font = QFont(self.cfg.font_family)
        font.setPixelSize(max(11, int(round(self._font_size * self.scale))))
        edit.setFont(font)
        edit.move(int(pos.x()), int(pos.y() - font.pixelSize() * 0.75))
        edit.setMinimumWidth(70)
        edit.resize(90, font.pixelSize() + 10)
        edit.returnPressed.connect(self._commit_text)
        edit.editingFinished.connect(self._commit_text)
        edit.escaped.connect(self._cancel_text)
        edit.textChanged.connect(lambda _t: self._grow_text_edit())
        edit.show()
        edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.text_edit = edit

    def _grow_text_edit(self) -> None:
        edit = self.text_edit
        if edit is None:
            return
        fm = QFontMetricsF(edit.font())
        width = int(fm.horizontalAdvance(edit.text()) + 26)
        edit.resize(max(70, width), edit.height())

    def _commit_text(self) -> None:
        edit = self.text_edit
        if edit is None:
            return
        self.text_edit = None
        text = edit.text()
        position = edit.pos()
        edit.hide()
        edit.deleteLater()
        if not text.strip():
            self.update()
            return
        anchor = self.to_image(QPointF(position.x(), position.y()))
        self.annotations.append(
            Annotation(
                kind=TEXT,
                points=[anchor],
                color=(self.color.red(), self.color.green(), self.color.blue(), 255),
                width=self._w_pen,
                text=text,
                font_family=self.cfg.font_family,
                font_size=int(round(self._font_size)),
            )
        )
        self.update()

    def _cancel_text(self) -> None:
        edit = self.text_edit
        self.text_edit = None
        if edit is not None:
            edit.hide()
            edit.deleteLater()
        self.update()

    # ------------------------------------------------------------------
    # toolbars
    # ------------------------------------------------------------------
    def _activate_toolbar(self, action: str, pos: QPointF) -> None:
        if action in DRAW_TOOL_IDS:
            self.tool = action
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.update()
            return
        if action == "color":
            popup = PalettePopup(self.color)
            popup.chosen.connect(self._set_color)
            # anchor the popup next to the swatch, opening to the left
            global_pos = self.mapToGlobal(QPoint(int(self._v_rect.left() - 8 - popup.width()), int(pos.y() - 20)))
            popup.move(global_pos)
            popup.show()
            self._popup = popup
            return
        if action == "undo":
            self.undo()
            return
        if action == "close":
            self._finish(False)
            return
        if action == "copy":
            self._emit("copy")
            return
        if action == "save":
            self._emit("save")
            return

    def _set_color(self, colour: QColor) -> None:
        self.color = colour
        self.cfg.color = colour.name()
        self.update()

    def undo(self) -> None:
        if self.text_edit is not None:
            self._cancel_text()
            return
        if self.annotations:
            self.annotations.pop()
            self.update()

    # ------------------------------------------------------------------
    # keyboard
    # ------------------------------------------------------------------
    def keyPressEvent(self, event):  # noqa: N802
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)

        if key == Qt.Key.Key_Escape:
            self._finish(False)
            return
        if ctrl and key == Qt.Key.Key_Z:
            self.undo()
            return
        if ctrl and key == Qt.Key.Key_C:
            self._emit("copy")
            return
        if ctrl and key == Qt.Key.Key_S:
            self._emit("save")
            return
        if ctrl and key == Qt.Key.Key_A:
            self.sel = QRectF(self.img_rect)
            self.phase = "ready"
            self.update()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._emit("copy")
            return

        shortcuts = {
            Qt.Key.Key_P: PEN,
            Qt.Key.Key_L: LINE,
            Qt.Key.Key_A: ARROW,
            Qt.Key.Key_R: RECT,
            Qt.Key.Key_M: MARKER,
            Qt.Key.Key_T: TEXT,
            Qt.Key.Key_B: BLUR,
            Qt.Key.Key_V: SELECT,
        }
        if key in shortcuts and not ctrl:
            self.tool = shortcuts[key]
            self.update()
            return

        if self.sel is not None and key in (
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
        ):
            step = 10.0 if mods & Qt.KeyboardModifier.ShiftModifier else 1.0
            dx = (-step if key == Qt.Key.Key_Left else step if key == Qt.Key.Key_Right else 0.0)
            dy = (-step if key == Qt.Key.Key_Up else step if key == Qt.Key.Key_Down else 0.0)
            self.sel = self._clamp_to_image(self.sel.translated(dx, dy))
            self.update()
            return

        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # results / toasts
    # ------------------------------------------------------------------
    def _blur_rect(self, a: Annotation) -> QRect:
        """The image-pixel rectangle a blur annotation covers."""
        if len(a.points) < 2:
            return QRect()
        rect = QRectF(a.points[0], a.points[-1]).normalized().toRect()
        return rect.intersected(QRect(0, 0, self.shot.width(), self.shot.height()))

    def _blurred_crop(self, rect: QRect, radius: int) -> QImage | None:
        """Blurred pixels of the frozen screenshot for *rect*, memoised.

        Keyed on the source rectangle and the radius, so the on-screen preview
        and the exported file share one cached result per region.
        """
        if rect.isEmpty() or rect.width() < 1 or rect.height() < 1:
            return None
        key = (rect.x(), rect.y(), rect.width(), rect.height(), int(radius))
        cached = self._blur_cache.get(key)
        if cached is not None:
            return cached
        blurred = blur_region_of(self.shot, rect, radius)
        if blurred is None:
            return None
        if len(self._blur_cache) >= 48:
            self._blur_cache.clear()
        self._blur_cache[key] = blurred
        return blurred

    def _paint_blur(self, p: QPainter, a: Annotation, outline: bool = False) -> None:
        rect = self._blur_rect(a)
        if rect.isEmpty():
            return
        blurred = self._blurred_crop(rect, int(round(a.width)))
        if blurred is None:
            return
        top_left = self.to_widget(QPointF(rect.left(), rect.top()))
        bottom_right = self.to_widget(QPointF(rect.right() + 1, rect.bottom() + 1))
        target = QRectF(top_left, bottom_right)
        p.drawImage(target, blurred, QRectF(blurred.rect()))
        if outline:
            # a dashed border while the region is still being dragged
            pen = QPen(QColor("#ffffff"), 1, Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([4, 4])
            p.save()
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(pen)
            p.drawRect(target.adjusted(0.5, 0.5, -0.5, -0.5))
            p.restore()

    def crop_rect(self) -> tuple[int, int, int, int]:
        """The exact image-pixel rectangle that :meth:`render_result` exports.

        Both the size badge and the exported image use this, so the `W x H`
        read-out can never disagree with the saved file by a pixel.
        """
        if self.sel is None:
            return 0, 0, self.shot.width(), self.shot.height()

        top_left = self.to_image(self.sel.topLeft())
        bottom_right = self.to_image(self.sel.bottomRight())
        src = QRectF(top_left, bottom_right).normalized().intersected(
            QRectF(0, 0, self.shot.width(), self.shot.height())
        )
        # round (not floor/ceil) so floating-point error in the widget->image
        # transform cannot inflate the crop past what the size badge reports
        x0 = max(0, round(src.left()))
        y0 = max(0, round(src.top()))
        x1 = min(self.shot.width(), round(src.right()))
        y1 = min(self.shot.height(), round(src.bottom()))
        if x1 <= x0 or y1 <= y0:
            # the selection ended up outside the image (can happen after a
            # display change) - fall back to the whole screenshot
            return 0, 0, self.shot.width(), self.shot.height()
        return x0, y0, x1 - x0, y1 - y0

    def render_result(self) -> QImage:
        """Crop the frozen image to the selection and bake the annotations in."""
        x0, y0, w, h = self.crop_rect()

        canvas = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(QColor("#000000"))
        p = QPainter(canvas)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawImage(QRect(0, 0, w, h), self.shot, QRect(x0, y0, w, h))
        p.translate(-x0, -y0)
        for a in self.annotations:
            if a.kind == BLUR:
                rect = self._blur_rect(a)
                if rect.isEmpty():
                    continue
                blurred = self._blurred_crop(rect, int(round(a.width)))
                if blurred is not None:
                    p.drawImage(rect, blurred)
            else:
                draw_annotation(p, a)
        p.end()
        canvas.setDevicePixelRatio(1.0)
        return canvas

    def _emit(self, action: str) -> None:
        image = self.render_result()
        # Hide the full-screen, always-on-top overlay *before* handing the image
        # to the application.  Save opens a modal dialog; if this window were
        # still mapped the dialog would appear underneath it and the user would
        # see nothing happen at all.
        self._finish(True, action)
        if action == "copy":
            self.copy_requested.emit(image)
        elif action == "save":
            self.save_requested.emit(image)

    def show_toast(self, text: str, error: bool = False, timeout_ms: int = 4000) -> None:
        self._toast_text = text
        self._toast_error = error
        self._toast_timer.stop()
        if timeout_ms > 0:
            self._toast_timer.start(timeout_ms)
        self.update()

    def _clear_toast(self) -> None:
        self._toast_text = ""
        self.update()

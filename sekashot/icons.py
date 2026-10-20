"""Vector icons drawn with QPainter.

Lightshot ships its icons as bitmaps; we reproduce the same visual language
(thin, dark-grey outlines on a light rounded button) with pure vector drawing so
the package needs no external asset files and stays crisp on HiDPI screens.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

ICON_COLOR = "#4a4a4a"
ICON_SIZE = 20

_cache: dict[tuple, QIcon] = {}


def _pixmap(name: str, color: str, size: int = ICON_SIZE, accent: str | None = None) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    def X(fx: float) -> float:
        return fx * size

    pen = QPen(QColor(color))
    pen.setWidthF(max(1.4, size * 0.085))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    if name == "pen":
        # diagonal pencil body + tip
        p.drawLine(QPointF(X(0.14), X(0.86)), QPointF(X(0.74), X(0.26)))
        p.drawLine(QPointF(X(0.66), X(0.12)), QPointF(X(0.90), X(0.36)))
        tip = QPainterPath()
        tip.moveTo(X(0.10), X(0.90))
        tip.lineTo(X(0.16), X(0.66))
        tip.lineTo(X(0.34), X(0.84))
        tip.closeSubpath()
        p.setBrush(QBrush(QColor(color)))
        p.drawPath(tip)

    elif name == "line":
        p.drawLine(QPointF(X(0.14), X(0.86)), QPointF(X(0.86), X(0.14)))

    elif name == "arrow":
        p.drawLine(QPointF(X(0.14), X(0.86)), QPointF(X(0.80), X(0.20)))
        head = QPainterPath()
        head.moveTo(X(0.44), X(0.20))
        head.lineTo(X(0.80), X(0.20))
        head.lineTo(X(0.80), X(0.56))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(head)

    elif name == "rect":
        p.drawRoundedRect(QRectF(X(0.14), X(0.20), X(0.72), X(0.60)), X(0.06), X(0.06))

    elif name == "marker":
        thick = QPen(QColor(color))
        thick.setWidthF(max(2.6, size * 0.17))
        thick.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(thick)
        p.drawLine(QPointF(X(0.24), X(0.60)), QPointF(X(0.72), X(0.16)))
        p.setPen(pen)
        p.drawLine(QPointF(X(0.12), X(0.86)), QPointF(X(0.88), X(0.86)))

    elif name == "text":
        p.drawLine(QPointF(X(0.20), X(0.24)), QPointF(X(0.80), X(0.24)))
        p.drawLine(QPointF(X(0.50), X(0.24)), QPointF(X(0.50), X(0.82)))

    elif name == "blur":
        # a pixelate / redact mark: an outlined square with a checkerboard of
        # cells, clearly distinct from the plain empty "rectangle" icon
        p.drawRoundedRect(QRectF(X(0.12), X(0.12), X(0.76), X(0.76)), X(0.07), X(0.07))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(color)))
        cell = 0.76 / 3
        for gx in range(3):
            for gy in range(3):
                if (gx + gy) % 2 == 0:
                    p.drawRect(QRectF(X(0.12 + gx * cell), X(0.12 + gy * cell), X(cell), X(cell)))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(pen)

    elif name == "color":
        swatch = QColor(accent or color)
        p.setPen(QPen(swatch.darker(160), max(1.0, size * 0.06)))
        p.setBrush(QBrush(swatch))
        p.drawRoundedRect(QRectF(X(0.14), X(0.14), X(0.72), X(0.72)), X(0.10), X(0.10))
        # folded white corner, exactly like the Lightshot swatch
        corner = QPainterPath()
        corner.moveTo(X(0.62), X(0.86))
        corner.lineTo(X(0.86), X(0.62))
        corner.lineTo(X(0.86), X(0.86))
        corner.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawPath(corner)

    elif name == "undo":
        p.drawArc(QRectF(X(0.18), X(0.22), X(0.64), X(0.60)), 35 * 16, 290 * 16)
        head = QPainterPath()
        head.moveTo(X(0.09), X(0.34))
        head.lineTo(X(0.31), X(0.19))
        head.lineTo(X(0.27), X(0.48))
        head.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(color)))
        p.drawPath(head)

    # ---- horizontal action bar ---------------------------------------
    elif name == "copy":
        p.drawRoundedRect(QRectF(X(0.12), X(0.12), X(0.52), X(0.60)), X(0.08), X(0.08))
        p.drawRoundedRect(QRectF(X(0.36), X(0.30), X(0.52), X(0.58)), X(0.08), X(0.08))

    elif name == "save":
        p.drawRoundedRect(QRectF(X(0.14), X(0.14), X(0.72), X(0.72)), X(0.08), X(0.08))
        p.drawRect(QRectF(X(0.32), X(0.14), X(0.36), X(0.26)))
        p.drawRect(QRectF(X(0.28), X(0.56), X(0.44), X(0.30)))

    elif name == "close":
        thick = QPen(QColor(color))
        thick.setWidthF(max(1.8, size * 0.11))
        thick.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(thick)
        p.drawLine(QPointF(X(0.22), X(0.22)), QPointF(X(0.78), X(0.78)))
        p.drawLine(QPointF(X(0.78), X(0.22)), QPointF(X(0.22), X(0.78)))

    elif name == "tray":
        p.setPen(QPen(QColor(color), max(1.2, size * 0.07)))
        p.drawRoundedRect(QRectF(X(0.14), X(0.22), X(0.72), X(0.56)), X(0.10), X(0.10))
        p.setBrush(QBrush(QColor(color)))
        p.drawEllipse(QPointF(X(0.50), X(0.50)), X(0.10), X(0.10))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(X(0.50), X(0.06)), QPointF(X(0.50), X(0.16)))

    p.end()
    return pm


def icon(name: str, color: str = ICON_COLOR, size: int = ICON_SIZE, accent: str | None = None) -> QIcon:
    key = (name, color, size, accent)
    if key not in _cache:
        _cache[key] = QIcon(_pixmap(name, color, size, accent))
    return _cache[key]


def pixmap(name: str, color: str = ICON_COLOR, size: int = ICON_SIZE, accent: str | None = None) -> QPixmap:
    return _pixmap(name, color, size, accent)


def app_icon(size: int = 256) -> QIcon:
    """The application/tray/window icon: a Lightshot-ish camera + crop frame."""
    key = ("__app__", size)
    if key in _cache:
        return _cache[key]

    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = size

    grad = QColor("#3aa0ff")
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.22, s * 0.22)

    # camera body
    p.setBrush(QBrush(QColor("#ffffff")))
    body = QRectF(s * 0.16, s * 0.30, s * 0.68, s * 0.46)
    p.drawRoundedRect(body, s * 0.09, s * 0.09)
    p.drawRoundedRect(QRectF(s * 0.36, s * 0.20, s * 0.28, s * 0.14), s * 0.04, s * 0.04)

    # lens
    p.setBrush(QBrush(QColor("#2f7fd6")))
    p.drawEllipse(QRectF(s * 0.38, s * 0.38, s * 0.24, s * 0.24))
    p.setBrush(QBrush(QColor("#ffffff")))
    p.drawEllipse(QRectF(s * 0.45, s * 0.45, s * 0.10, s * 0.10))

    # crop corner marks
    pen = QPen(QColor("#ffffff"), max(2.0, s * 0.03))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    for cx, cy, dx, dy in ((0.09, 0.09, 1, 1), (0.91, 0.09, -1, 1), (0.09, 0.91, 1, -1), (0.91, 0.91, -1, -1)):
        p.drawLine(QPointF(s * cx, s * cy), QPointF(s * (cx + 0.09 * dx), s * cy))
        p.drawLine(QPointF(s * cx, s * cy), QPointF(s * cx, s * (cy + 0.09 * dy)))

    p.end()
    res = QIcon(pm)
    _cache[key] = res
    return res


def app_icon_png(path: str, size: int = 256) -> None:
    """Write the app icon to a PNG file (used by the .deb build)."""
    app_icon(size).pixmap(size, size).save(path, "PNG")

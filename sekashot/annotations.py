"""Annotation model + rendering.

Annotations are stored in **image pixel space** (never widget space) so that
exporting a crop is a matter of translating the painter and replaying the list.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF

#: tool identifiers used across the UI
SELECT = "select"
PEN = "pen"
LINE = "line"
ARROW = "arrow"
RECT = "rect"
MARKER = "marker"
TEXT = "text"
BLUR = "blur"

#: vertical toolbar order (exactly as in Lightshot)
DRAW_TOOLS: list[tuple[str, str, str]] = [
    (PEN, "pen", "Pen (P)"),
    (LINE, "line", "Line (L)"),
    (ARROW, "arrow", "Arrow (A)"),
    (RECT, "rect", "Rectangle (R)"),
    (MARKER, "marker", "Marker (M)"),
    (TEXT, "text", "Text (T)"),
    (BLUR, "blur", "Blur / redact (B)"),
]
DRAW_TOOL_IDS = [t[0] for t in DRAW_TOOLS]

#: palette offered by the colour swatch
PALETTE = [
    "#ff0000", "#ff8c00", "#ffd400", "#00c853", "#00b8d4",
    "#2962ff", "#7c4dff", "#c2185b", "#000000", "#6d6d6d", "#ffffff",
]


@dataclass
class Annotation:
    kind: str
    points: list[QPointF]
    color: tuple[int, int, int, int]
    width: float
    text: str = ""
    font_family: str = "Sans"
    font_size: int = 18
    bold: bool = False
    filled: bool = False

    # -- serialisation (nice for tests / future undo files) -------------
    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "points": [(p.x(), p.y()) for p in self.points],
            "color": list(self.color),
            "width": self.width,
            "text": self.text,
            "font_family": self.font_family,
            "font_size": self.font_size,
            "bold": self.bold,
            "filled": self.filled,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Annotation":
        return cls(
            kind=data["kind"],
            points=[QPointF(x, y) for x, y in data["points"]],
            color=tuple(data["color"]),  # type: ignore[arg-type]
            width=data["width"],
            text=data.get("text", ""),
            font_family=data.get("font_family", "Sans"),
            font_size=data.get("font_size", 18),
            bold=data.get("bold", False),
            filled=data.get("filled", False),
        )


def _pen_for(a: Annotation) -> QPen:
    pen = QPen(QColor(*a.color))
    pen.setWidthF(max(1.0, a.width))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _arrow_head(painter: QPainter, start: QPointF, end: QPointF, width: float, color: QColor) -> None:
    line = QLineF(start, end)
    length = line.length()
    if length < 1e-3:
        return
    head_len = min(max(width * 4.2, 10.0), length * 0.55)
    head_wid = head_len * 0.52
    angle = math.radians(line.angle())  # Qt angles are CCW, y-axis inverted
    tip = end
    # base of the arrow head, measured back along the shaft
    bx = tip.x() - math.cos(angle) * head_len
    by = tip.y() + math.sin(angle) * head_len
    left = QPointF(bx - math.sin(angle) * head_wid, by - math.cos(angle) * head_wid)
    right = QPointF(bx + math.sin(angle) * head_wid, by + math.cos(angle) * head_wid)

    head = QPolygonF([tip, left, right])
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawPolygon(head)


def draw_annotation(painter: QPainter, a: Annotation) -> None:
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    if a.kind == PEN:
        painter.setPen(_pen_for(a))
        if len(a.points) == 1:
            painter.setBrush(QColor(*a.color))
            painter.drawEllipse(a.points[0], a.width / 2, a.width / 2)
        else:
            for i in range(1, len(a.points)):
                painter.drawLine(a.points[i - 1], a.points[i])
        if len(a.points) > 2:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolyline(QPolygonF(a.points))

    elif a.kind == LINE:
        if len(a.points) >= 2:
            painter.setPen(_pen_for(a))
            painter.drawLine(a.points[0], a.points[-1])

    elif a.kind == ARROW:
        if len(a.points) >= 2:
            pen = _pen_for(a)
            painter.setPen(pen)
            start, end = a.points[0], a.points[-1]
            painter.drawLine(start, end)
            _arrow_head(painter, start, end, a.width, QColor(*a.color))

    elif a.kind == RECT:
        if len(a.points) >= 2:
            rect = QRectF(a.points[0], a.points[-1]).normalized()
            pen = _pen_for(a)
            painter.setPen(pen)
            if getattr(a, "filled", False):
                painter.setBrush(QColor(a.color[0], a.color[1], a.color[2], 40))
            else:
                painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)

    elif a.kind == MARKER:
        # translucent, flat-capped, thick highlighter stroke
        pen = QPen(QColor(*a.color))
        pen.setWidthF(max(2.0, a.width))
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        pts = a.points
        if len(pts) == 1:
            painter.drawEllipse(pts[0], a.width / 2, a.width / 2)
        else:
            for i in range(1, len(pts)):
                painter.drawLine(pts[i - 1], pts[i])

    elif a.kind == TEXT:
        if a.points and a.text:
            font = QFont(a.font_family)
            font.setPixelSize(max(6, int(a.font_size)))
            font.setBold(a.bold)
            painter.setFont(font)
            flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            anchor = a.points[0]
            # a generous rect lets long strings render without eliding
            box = QRectF(anchor.x(), anchor.y(), 100000.0, 100000.0)
            # a subtle halo keeps text readable on busy backgrounds
            painter.setPen(QColor(0, 0, 0, 90))
            halo = max(1.0, a.width * 0.6)
            for dx, dy in ((-halo, 0), (halo, 0), (0, -halo), (0, halo)):
                painter.drawText(box.translated(dx, dy), flags, a.text)
            painter.setPen(QColor(*a.color))
            painter.drawText(box, flags, a.text)

    elif a.kind == BLUR:
        # A blur needs the source pixels, which this painter does not carry.
        # The overlay renders it explicitly (see CaptureOverlay._paint_blur and
        # CaptureOverlay.render_result) so nothing is drawn here on purpose.
        pass

    painter.restore()


def draw_annotations(painter: QPainter, annotations: list[Annotation]) -> None:
    for a in annotations:
        draw_annotation(painter, a)

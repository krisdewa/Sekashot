"""Offscreen preview: render the overlay (and a crop) to PNG for visual checks.

Run with:  QT_QPA_PLATFORM=offscreen python3 tools/preview.py [outdir]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from sekashot.annotations import ARROW, BLUR, LINE, MARKER, PEN, RECT, TEXT, Annotation  # noqa: E402
from sekashot.config import Config  # noqa: E402
from sekashot.overlay import CaptureOverlay  # noqa: E402

W, H = 1920, 1080


def fake_desktop() -> QImage:
    img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(img)
    grad = QLinearGradient(0, 0, W, H)
    grad.setColorAt(0.0, QColor("#1b2a4a"))
    grad.setColorAt(0.5, QColor("#2e6f6a"))
    grad.setColorAt(1.0, QColor("#7a4a2b"))
    p.fillRect(img.rect(), grad)
    p.setPen(Qt.PenStyle.NoPen)
    for cx, cy, r, col in (
        (300, 250, 150, "#e0533f"),
        (700, 800, 220, "#4fae6a"),
        (1250, 380, 180, "#e8b83c"),
        (1600, 860, 240, "#5470c6"),
    ):
        p.setBrush(QColor(col))
        p.drawEllipse(QPointF(cx, cy), r, r)
    p.setPen(QColor("#ffffff"))
    f = QFont("Sans")
    f.setPixelSize(72)
    f.setBold(True)
    p.setFont(f)
    p.drawText(120, 1020, "mock desktop 1920x1080")
    p.end()
    return img


def build() -> CaptureOverlay:
    cfg = Config()
    cfg.save_dir = "/tmp"
    overlay = CaptureOverlay(fake_desktop(), cfg)
    overlay.resize(W, H)
    overlay._compute_transform()

    overlay.sel = QRectF(430, 300, 780, 500)
    overlay.phase = "ready"
    overlay.tool = RECT

    # one of every annotation kind, so every drawing path is exercised
    overlay.annotations = [
        Annotation(RECT, [QPointF(560, 470), QPointF(1080, 700)], (255, 140, 0, 255), 5),
        Annotation(MARKER, [QPointF(520, 400), QPointF(900, 400), QPointF(1080, 430)], (255, 212, 0, 110), 26),
        Annotation(PEN, [QPointF(600, 760), QPointF(680, 700), QPointF(760, 770), QPointF(840, 700)],
                   (0, 200, 120, 255), 5),
        Annotation(LINE, [QPointF(1140, 470), QPointF(900, 330)], (120, 190, 255, 255), 5),
        Annotation(ARROW, [QPointF(480, 760), QPointF(1000, 640)], (255, 60, 60, 255), 6),
        Annotation(TEXT, [QPointF(620, 560)], (255, 255, 255, 255), 4, text="Hello Sekashot", font_size=34),
        # a redacted region: blurs the frozen desktop pixels underneath, so the
        # word underneath becomes unreadable (the classic blur use case)
        Annotation(BLUR, [QPointF(870, 545), QPointF(1090, 610)], (255, 255, 255, 255), 12),
        # deliberately runs past the right edge to prove annotation clipping
        Annotation(LINE, [QPointF(1100, 760), QPointF(1500, 760)], (255, 0, 255, 255), 6),
    ]
    return overlay


def main() -> int:
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/opencode")
    outdir.mkdir(parents=True, exist_ok=True)

    app = QApplication([sys.argv[0]])
    overlay = build()

    # during-drag state (no toolbars, badge only)
    overlay.phase = "selecting"
    overlay.grab().save(str(outdir / "overlay-selecting.png"), "PNG")

    overlay.phase = "ready"
    overlay.update()
    overlay.grab().save(str(outdir / "overlay-ready.png"), "PNG")

    result = overlay.render_result()
    result.save(str(outdir / "overlay-crop.png"), "PNG")

    # idle state: dimmed desktop, no selection yet
    overlay.sel = None
    overlay.phase = "idle"
    overlay.update()
    overlay.grab().save(str(outdir / "overlay-idle.png"), "PNG")

    print("screen size :", app.primaryScreen().size())
    print("crop size   :", result.width(), "x", result.height())
    print("written to  :", outdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

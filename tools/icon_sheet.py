"""Render every vector icon (and the app icon) into one contact sheet for review.

Run with:  QT_QPA_PLATFORM=offscreen python3 tools/icon_sheet.py [out.png]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QRectF, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QFont, QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from sekashot import icons  # noqa: E402

VERTICAL = ["pen", "line", "arrow", "rect", "marker", "text", "blur", "color", "undo"]
HORIZONTAL = ["copy", "save", "close"]

SCALE = 4
CELL = 20 * SCALE
LABEL = 22


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/opencode/icon-sheet.png")
    out.parent.mkdir(parents=True, exist_ok=True)

    app = QApplication([sys.argv[0]])
    names = VERTICAL + HORIZONTAL

    cols = len(names) + 1
    width = cols * (CELL + 10) + 20
    height = CELL + LABEL + 40

    sheet = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    sheet.fill(QColor("#f6f6f6"))
    p = QPainter(sheet)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    font = QFont("Sans")
    font.setPixelSize(13)
    p.setFont(font)

    x = 10
    for name in names:
        accent = "#ff0000" if name == "color" else None
        pm = icons.pixmap(name, size=CELL, accent=accent)
        p.drawPixmap(int(x), 14, pm)
        p.setPen(QColor("#333333"))
        p.drawText(QRectF(x, 14 + CELL, CELL, LABEL), int(Qt.AlignmentFlag.AlignCenter), name)
        # reference-size rendering, as it appears in the toolbar
        small = icons.pixmap(name, size=20, accent=accent)
        p.drawPixmap(int(x + 20), 14 + CELL + LABEL, small)
        x += CELL + 10

    # app icon on the right
    app_pm = icons.app_icon(CELL).pixmap(CELL, CELL)
    p.drawPixmap(int(x), 14, app_pm)
    p.setPen(QColor("#333333"))
    p.drawText(QRectF(x, 14 + CELL, CELL, LABEL), int(Qt.AlignmentFlag.AlignCenter), "app")
    p.end()

    print("sheet size:", sheet.width(), "x", sheet.height())
    sheet.save(str(out), "PNG")
    print("written:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

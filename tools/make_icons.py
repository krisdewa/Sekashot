"""Write the hicolor icon theme PNGs used by the .deb.

Run with:  QT_QPA_PLATFORM=offscreen python3 tools/make_icons.py <output-root>
The output root is normally ``packaging/icons/hicolor``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from sekashot.icons import app_icon  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256, 512)


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "packaging" / "icons"
    app = QApplication([sys.argv[0]])  # noqa: F841 - needed for QPixmap/QIcon

    written = []
    for size in SIZES:
        folder = root / "hicolor" / f"{size}x{size}" / "apps"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "sekashot.png"
        image: QImage = app_icon(size).pixmap(size, size).toImage()
        image.save(str(target), "PNG")
        written.append(target)

    # a plain PNG next to the package for quick previews / README use
    preview = root / "sekashot.png"
    app_icon(256).pixmap(256, 256).save(str(preview), "PNG")
    written.append(preview)

    for path in written:
        print(f"{path}  ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

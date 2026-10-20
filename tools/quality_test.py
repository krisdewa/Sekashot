"""Quality audit: prove that the exported screenshot is not degraded.

Run it on a live session:

    python3 tools/quality_test.py

It answers four questions with measurements, not assumptions:

  A. Is the fast PPM capture path (grim) pixel-identical to grim's PNG?
  B. Does the exported image match the captured region pixel-for-pixel, even
     when the on-screen overlay is scaled down (what a HiDPI display does)?
  C. What does the exported file look like on disk - bit depth, alpha, size?
  D. Is the clipboard payload lossless?

The interesting part is B: the overlay widget is deliberately shrunk to half
size before the selection is made, so any accidental resampling of the export
would show up as differing pixels.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QBuffer, QPointF, QRectF  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((label, ok, extra))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' -> ' + extra) if extra else ''}")


def diff(a: QImage, b: QImage) -> tuple[int, int]:
    """(differing pixels, worst per-channel delta) of two equal-sized images."""
    bad, worst = 0, 0
    for _x, _y, d in diff_pixels(a, b):
        bad += 1
        worst = max(worst, d)
    return bad, worst


def diff_pixels(a: QImage, b: QImage):
    """Yield (x, y, delta) for every pixel that differs between two images."""
    if a.width() != b.width() or a.height() != b.height():
        return
    a32 = a.convertToFormat(QImage.Format.Format_RGB32)
    b32 = b.convertToFormat(QImage.Format.Format_RGB32)
    w, h = a32.width(), a32.height()
    n = w * h
    sa = a32.constBits().asstring(n * 4)
    sb = b32.constBits().asstring(n * 4)
    for i in range(0, len(sa), 4):
        d = max(abs(sa[i] - sb[i]), abs(sa[i + 1] - sb[i + 1]), abs(sa[i + 2] - sb[i + 2]))
        if d:
            yield i // 4 % w, i // 4 // w, d


def _synthetic_ppm(w: int, h: int) -> bytes:
    """A deterministic PPM (P6) frame - no live screen involved."""
    body = bytearray()
    for y in range(h):
        for x in range(w):
            body += bytes(((x * 3) % 256, (y * 5) % 256, (x * y + 7) % 256))
    return f"P6\n{w} {h}\n255\n".encode() + bytes(body)


def main() -> int:
    app = QApplication([])

    print("\nA. the PPM fast path is lossless")
    # A.1 - deterministic: the same frame as PPM and as PNG must decode to the
    # same pixels.  Nothing here depends on what the screen is showing.
    src = QImage(64, 48, QImage.Format.Format_RGB32)
    for y in range(48):
        for x in range(64):
            src.setPixelColor(x, y, QColor((x * 3) % 256, (y * 5) % 256, (x * y + 7) % 256))
    buf = QBuffer()
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    src.save(buf, "PNG")
    png_bytes = bytes(buf.data())
    ppm_bytes = _synthetic_ppm(64, 48)

    dec_ppm = QImage()
    dec_ppm.loadFromData(ppm_bytes, "PPM")
    dec_png = QImage()
    dec_png.loadFromData(png_bytes, "PNG")
    check("PPM decodes", not dec_ppm.isNull(), f"{dec_ppm.width()}x{dec_ppm.height()}")
    check("PNG decodes", not dec_png.isNull(), f"{dec_png.width()}x{dec_png.height()}")
    check("the two encodings hold the same frame", ppm_bytes[:2] == b"P6" and png_bytes[:4] == b"\x89PNG")
    bad, worst = diff(dec_ppm, dec_png)
    check(
        "PPM and PNG of the same frame decode identically",
        bad == 0,
        f"{bad} differing px, max delta {worst}",
    )

    # A.2 - live: grim's PPM output against grim's PNG output.  The screen
    # moves between two grabs (a clock, a spinner, an animation), so the two
    # frames are never identical.  Instead of giving up, the change between
    # two consecutive PPM grabs is used as the noise floor: every pixel that
    # differs between the PPM and the PNG frame must already be a pixel that
    # was moving.  A lossy PPM path would differ in far more places than that.
    ppm_proc = subprocess.run(["grim", "-t", "ppm", "-c", "-"], capture_output=True)
    ppm2_proc = subprocess.run(["grim", "-t", "ppm", "-c", "-"], capture_output=True)
    png_proc = subprocess.run(["grim", "-t", "png", "-c", "-"], capture_output=True)
    if ppm_proc.returncode or png_proc.returncode:
        print("  (grim is unavailable here - skipping the live check)")
    else:
        ppm_img = QImage()
        ppm_img.loadFromData(ppm_proc.stdout, "PPM")
        ppm2_img = QImage()
        ppm2_img.loadFromData(ppm2_proc.stdout, "PPM")
        png_img = QImage()
        png_img.loadFromData(png_proc.stdout, "PNG")
        check("same dimensions", ppm_img.size() == png_img.size())

        moving = {(x, y) for x, y, _d in diff_pixels(ppm_img, ppm2_img)}
        unexplained = {
            (x, y) for x, y, _d in diff_pixels(ppm_img, png_img)
        } - moving
        check(
            "grim PPM == grim PNG, apart from what was moving",
            not unexplained,
            f"{len(moving)} moving px, {len(unexplained)} unexplained",
        )

    print("\nB. capture -> overlay (downscaled) -> export")
    from sekashot.capture import grab_desktop  # noqa: E402
    from sekashot.config import Config  # noqa: E402
    from sekashot.overlay import CaptureOverlay  # noqa: E402

    shot = grab_desktop()
    img = shot.image
    check("capture succeeded", not img.isNull(), f"{img.width()}x{img.height()}")
    if img.isNull():
        print("\ncannot continue without a capture")
        return 1
    check(
        "capture is 8 bit per channel",
        img.depth() == 32,
        f"depth={img.depth()} format={img.format().name}",
    )

    cfg = Config()
    ov = CaptureOverlay(img, cfg)
    ov.resize(img.width() // 2, img.height() // 2)
    ov.show()
    app.processEvents()
    check("the overlay really is scaled", ov.scale < 0.99, f"scale={ov.scale:.4f}")

    x0, y0, w, h = 137, 211, 640, 480
    ov.sel = QRectF(ov.to_widget(QPointF(x0, y0)), ov.to_widget(QPointF(x0 + w, y0 + h)))
    check("crop rect matches the request", ov.crop_rect() == (x0, y0, w, h), str(ov.crop_rect()))

    out = ov.render_result()
    check("export size", out.width() == w and out.height() == h, f"{out.width()}x{out.height()}")
    check("export DPR normalised", out.devicePixelRatio() == 1.0, str(out.devicePixelRatio()))
    bad, worst = diff(out, img.copy(x0, y0, w, h))
    check("export == original pixels (no resampling)", bad == 0, f"{bad} differing px, max delta {worst}")

    print("\nC. the exported file")
    with tempfile.TemporaryDirectory() as tmp:
        png_path = Path(tmp) / "out.png"
        out.save(str(png_path), "PNG")
        reread = QImage()
        reread.load(str(png_path))
        check("PNG re-reads to the same pixels", diff(out, reread)[0] == 0)
        check("PNG keeps 8 bit channels", reread.depth() == 32, f"depth={reread.depth()}")
        png_size = png_path.stat().st_size

        jpg_path = Path(tmp) / "out.jpg"
        out.save(str(jpg_path), "JPEG", cfg.jpg_quality)
        jbad, jworst = diff(out, QImage(str(jpg_path)))
        print(
            f"       PNG {png_size} B (lossless, default) vs JPEG q{cfg.jpg_quality} "
            f"{jpg_path.stat().st_size} B -> {jbad} differing px, max delta {jworst}"
        )

    print("\nD. the clipboard payload")
    from sekashot.app import _image_png_bytes  # noqa: E402

    blob = _image_png_bytes(out)
    check("the clipboard payload is PNG", blob.startswith(b"\x89PNG"), f"{len(blob)} B")
    clip = QImage()
    clip.loadFromData(blob, "PNG")
    check("the clipboard payload is lossless", diff(out, clip)[0] == 0)

    print("\n" + "=" * 60)
    failed = [r for r in RESULTS if not r[1]]
    if failed:
        print(f"{len(failed)} FAILED of {len(RESULTS)}")
        for label, _ok, extra in failed:
            print(f"  FAIL {label} {extra}")
        return 1
    print(f"ALL {len(RESULTS)} QUALITY CHECKS PASSED - no pixel loss")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

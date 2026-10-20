"""Gaussian blur used by the blur / redaction annotation.

The blur is always computed from the **frozen screenshot** pixels, never from
what is already on the canvas.  That matters: the on-screen preview and the
exported file therefore run the exact same function over the exact same source
pixels, so what you see blurred is what gets saved - a blurred region can never
leak the sharp original underneath.
"""

from __future__ import annotations

from PyQt6.QtCore import QRect, QRectF, Qt
from PyQt6.QtGui import QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene

#: QGraphicsBlurEffect's kernel gets slow for very large crops; past this many
#: pixels on the longest side we blur a down-scaled copy and scale it back up.
#: The visual result is equivalent (blur is scale-invariant) and it keeps the
#: live preview during a drag responsive on a full-screen selection.
_MAX_WORK_SIZE = 1200


def _pad_clamped(image: QImage, pad: int) -> QImage:
    """Grow *image* by *pad* px on every side, repeating the edge pixels.

    Without this the blur would average in transparent pixels from beyond the
    crop and fade the edges of the region.
    """
    w, h = image.width(), image.height()
    out = QImage(w + 2 * pad, h + 2 * pad, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.drawImage(pad, pad, image)
    # the four edges, stretched from a single row/column of source pixels
    p.drawImage(QRect(pad, 0, w, pad), image, QRect(0, 0, w, 1))
    p.drawImage(QRect(pad, pad + h, w, pad), image, QRect(0, h - 1, w, 1))
    p.drawImage(QRect(0, pad, pad, h), image, QRect(0, 0, 1, h))
    p.drawImage(QRect(pad + w, pad, pad, h), image, QRect(w - 1, 0, 1, h))
    # the four corners, filled from the single corner pixel
    for cx, cy, sx, sy in (
        (0, 0, 0, 0),
        (pad + w, 0, w - 1, 0),
        (0, pad + h, 0, h - 1),
        (pad + w, pad + h, w - 1, h - 1),
    ):
        p.drawImage(QRect(cx, cy, pad, pad), image, QRect(sx, sy, 1, 1))
    p.end()
    return out


def blur_image(region: QImage, radius: float) -> QImage:
    """Return *region* with a Gaussian blur of *radius* image pixels applied."""
    radius = max(1, int(round(radius)))
    if region.isNull() or region.width() < 1 or region.height() < 1:
        return region

    work = region
    work_radius = radius
    longest = max(region.width(), region.height())
    if longest > _MAX_WORK_SIZE:
        factor = longest / float(_MAX_WORK_SIZE)
        work = region.scaled(
            max(1, int(round(region.width() / factor))),
            max(1, int(round(region.height() / factor))),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        work_radius = max(1.0, radius / factor)

    work_radius = max(1, int(round(work_radius)))
    pad = work_radius * 3 + 2
    padded = _pad_clamped(work, pad)

    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(QPixmap.fromImage(padded))
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(work_radius)
    effect.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
    item.setGraphicsEffect(effect)
    scene.addItem(item)

    out = QImage(padded.size(), QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    scene.render(p, QRectF(out.rect()), QRectF(padded.rect()))
    p.end()

    blurred = out.copy(pad, pad, work.width(), work.height())
    if work is not region:
        blurred = blurred.scaled(
            region.size(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return blurred


def blur_region_of(source: QImage, rect: QRect, radius: float) -> QImage | None:
    """Blur ``source`` cropped to *rect* (source coordinates).

    Returns ``None`` when the rectangle does not overlap the source at all.
    """
    rect = rect.intersected(QRect(0, 0, source.width(), source.height()))
    if rect.width() < 1 or rect.height() < 1:
        return None
    return blur_image(source.copy(rect), radius)

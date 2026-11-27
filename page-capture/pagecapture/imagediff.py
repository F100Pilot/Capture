"""Deteção de mudança visual entre screenshots (Pillow)."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageChops

THUMB_WIDTH = 400
PIXEL_DELTA = 32
_LUT = [255 if v > PIXEL_DELTA else 0 for v in range(256)]


def thumbnail(png: bytes) -> Image.Image:
    """PNG -> miniatura em tons de cinzento com largura fixa (bilinear)."""
    with Image.open(BytesIO(png)) as img:
        gray = img.convert("L")
    height = max(1, round(gray.height * THUMB_WIDTH / gray.width))
    return gray.resize((THUMB_WIDTH, height), Image.Resampling.BILINEAR)


def changed_fraction(a: Image.Image, b: Image.Image) -> float:
    """Fração de píxeis com diferença > PIXEL_DELTA níveis. Tamanhos diferentes -> 1.0."""
    if a.size != b.size:
        return 1.0
    mask = ImageChops.difference(a, b).point(_LUT)
    return mask.histogram()[255] / (a.width * a.height)

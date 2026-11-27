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


def mask_bands(img: Image.Image, bands: list[tuple[float, float]]) -> Image.Image:
    """Cópia com faixas horizontais (frações da altura, 0..1) pintadas a preto.

    Serve para ignorar zonas como a barra de navegação ao comparar o conteúdo.
    """
    if not bands:
        return img
    out = img.copy()
    for top, bottom in bands:
        y0 = max(0, int(top * img.height))
        y1 = min(img.height, int(bottom * img.height) + 1)
        if y1 > y0:
            out.paste(0, (0, y0, img.width, y1))
    return out

from io import BytesIO

from PIL import Image, ImageDraw

from pagecapture.imagediff import changed_fraction, thumbnail


def png(text: str, size=(1366, 768)) -> bytes:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, size[0], 60], fill=(30, 30, 70))
    d.text((40, 120), text, fill="black", font_size=40)
    d.text((40, 200), text + " - detalhe", fill="black", font_size=28)
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_identical_is_zero():
    a = thumbnail(png("Procedimento A"))
    assert changed_fraction(a, thumbnail(png("Procedimento A"))) == 0.0


def test_text_change_same_template_detected():
    a, b = thumbnail(png("Procedimento A")), thumbnail(png("Confirmar valores"))
    assert changed_fraction(a, b) >= 0.003


def test_different_sizes_is_one():
    a, b = thumbnail(png("x")), thumbnail(png("x", size=(1366, 2000)))
    assert changed_fraction(a, b) == 1.0

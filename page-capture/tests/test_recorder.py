import csv
from io import BytesIO

from PIL import Image, ImageDraw

from pagecapture.imagediff import thumbnail
from pagecapture.runner import Recorder


def png(text: str) -> bytes:
    img = Image.new("RGB", (1366, 768), "white")
    ImageDraw.Draw(img).text((40, 120), text, fill="black", font_size=60)
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_repeated_page_is_never_written(tmp_path):
    a, b = png("Página A"), png("Página B")
    with Recorder(tmp_path) as rec:
        assert rec.save(a, thumbnail(a), "u", "start", "", None) == 1
        assert rec.save(b, thumbnail(b), "u", "click", "x", 0.01) == 2
        assert rec.save(a, thumbnail(a), "u", "click", "x", 0.01) is None  # igual à 1
        assert rec.count == 2
        run_dir = rec.dir
    assert sorted(p.name for p in run_dir.glob("*.png")) == ["0001.png", "0002.png"]
    with open(run_dir / "index.csv", encoding="utf-8") as fh:
        assert [r["n"] for r in csv.DictReader(fh)] == ["1", "2"]

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


def block_png(i: int) -> bytes:
    """Página com i blocos: cada estado acrescenta informação visível."""
    img = Image.new("RGB", (1366, 768), "white")
    draw = ImageDraw.Draw(img)
    for j in range(i):
        draw.rectangle([50 + j * 200, 100, 200 + j * 200, 300], fill="black")
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_keep_last_only_keeps_final_state_per_slide(tmp_path):
    shots = {k: block_png(i) for i, k in enumerate(("1a", "1b", "1c", "2a", "2b"), start=1)}
    with Recorder(tmp_path, keep_last_only=True) as rec:
        rec.save(shots["1a"], thumbnail(shots["1a"]), "u", "start", "", None)
        rec.save(shots["1b"], thumbnail(shots["1b"]), "u", "update", "", 0.01)
        rec.save(shots["1c"], thumbnail(shots["1c"]), "u", "update", "", 0.01)
        # durante o slide os intermédios ficam guardados (nada se perde se falhar a meio)
        assert sorted(p.name for p in rec.dir.glob("*.png")) == ["0001-2.png", "0001-3.png",
                                                                 "0001.png"]
        rec.save(shots["2a"], thumbnail(shots["2a"]), "u", "click", "button SEGUINTE", 0.3)
        rec.save(shots["2b"], thumbnail(shots["2b"]), "u", "update", "", 0.01)
        run_dir = rec.dir
    assert sorted(p.name for p in run_dir.glob("*.png")) == ["0001.png", "0002.png"]
    assert (run_dir / "0001.png").read_bytes() == shots["1c"]  # último estado do slide 1
    assert (run_dir / "0002.png").read_bytes() == shots["2b"]
    with open(run_dir / "index.csv", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [(r["n"], r["file"], r["trigger"], r["control"]) for r in rows] == [
        ("1", "0001.png", "start", ""), ("2", "0002.png", "click", "button SEGUINTE")]

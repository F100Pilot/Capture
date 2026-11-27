import csv
import functools
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from pagecapture.runner import Recorder, Runner, RunOptions

FIXTURES = Path(__file__).parent / "fixtures" / "course"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    handler = functools.partial(QuietHandler, directory=str(FIXTURES))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/index.html"
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.e2e
def test_course_in_iframe(server, tmp_path):
    # PC_BROWSER_EXECUTABLE: opcional, para ambientes onde o Chromium não veio de
    # "python -m playwright install chromium".
    exe = os.environ.get("PC_BROWSER_EXECUTABLE") or None
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(tmp_path / "profile"), headless=True, executable_path=exe,
            viewport={"width": 1366, "height": 768},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(server)
            opts = RunOptions(max_pages=20, change_timeout=5, manual_timeout=2)
            with Recorder(tmp_path / "out") as rec:
                Runner(context, rec, opts).run()
                run_dir = rec.dir
            clicks = page.frame_locator("#content").locator("body").evaluate(
                "() => window.clickLog")
        finally:
            context.close()

    pngs = sorted(run_dir.glob("*.png"))
    with open(run_dir / "index.csv", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [p.name for p in pngs] == [f"{i:04d}.png" for i in range(1, 7)]
    assert [r["trigger"] for r in rows] == ["start"] + ["click"] * 5
    assert all(r["duplicate_of"] == "" for r in rows)
    assert [c["slide"] for c in clicks] == [1, 2, 3, 4, 5]
    labels = " ".join(c["label"] for c in clicks).lower()
    assert "previous" not in labels and "voltar" not in labels and "left" not in labels

import csv
import os

import pytest
from playwright.sync_api import sync_playwright

from pagecapture.runner import Recorder, Runner, RunOptions


@pytest.mark.e2e
def test_course_in_iframe(course_server, tmp_path):
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
            page.goto(course_server)
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

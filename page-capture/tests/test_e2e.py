import csv
import os

import pytest
from playwright.sync_api import sync_playwright

from pagecapture.runner import MAX_CONSECUTIVE_DUPLICATES, Recorder, Runner, RunOptions


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
    assert [c["slide"] for c in clicks] == [1, 2, 3, 4, 5]
    labels = " ".join(c["label"] for c in clicks).lower()
    assert "previous" not in labels and "voltar" not in labels and "left" not in labels


@pytest.mark.e2e
def test_restricted_navigation_is_retried(course_server, tmp_path):
    exe = os.environ.get("PC_BROWSER_EXECUTABLE") or None
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(tmp_path / "profile"), headless=True, executable_path=exe,
            viewport={"width": 1366, "height": 768},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(course_server.replace("index.html", "restricted.html"))
            opts = RunOptions(max_pages=5, change_timeout=5, manual_timeout=3)
            with Recorder(tmp_path / "out") as rec:
                Runner(context, rec, opts).run()
                run_dir = rec.dir
            events = page.evaluate("() => window.events")
        finally:
            context.close()

    with open(run_dir / "index.csv", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["trigger"] for r in rows] == ["start", "click"]  # o aviso nunca é gravado
    assert "blocked" in events and "ok" in events and events[-1] == "next"


@pytest.mark.e2e
def test_repeated_pages_not_saved_and_cycle_ends(course_server, tmp_path):
    exe = os.environ.get("PC_BROWSER_EXECUTABLE") or None
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(tmp_path / "profile"), headless=True, executable_path=exe,
            viewport={"width": 1366, "height": 768},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(course_server.replace("index.html", "loop.html"))
            opts = RunOptions(max_pages=50, change_timeout=5, manual_timeout=3)
            with Recorder(tmp_path / "out") as rec:
                Runner(context, rec, opts).run()
                run_dir = rec.dir
            clicks = page.evaluate("() => window.clicks")
        finally:
            context.close()

    assert sorted(p.name for p in run_dir.glob("*.png")) == ["0001.png", "0002.png"]
    assert clicks == 1 + MAX_CONSECUTIVE_DUPLICATES  # termina após 5 repetidas seguidas

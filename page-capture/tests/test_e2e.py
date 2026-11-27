import csv
import logging
import os
import time

import pytest
from PIL import Image
from playwright.sync_api import sync_playwright

from pagecapture.runner import (
    MAX_CONSECUTIVE_DUPLICATES,
    Recorder,
    Runner,
    RunOptions,
    close_gracefully,
)


@pytest.mark.e2e
def test_course_in_iframe(course_server, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="pagecapture")
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
    assert "Captura CDP falhou" not in caplog.text  # capturas sem «clip» (não redimensiona)


@pytest.mark.e2e
def test_restricted_navigation_is_retried(course_server, tmp_path, caplog):
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
    # em cada nova tentativa inspeciona o slide e indica o que há para clicar
    assert 'zonas clicáveis: div "Saber mais" #hotspot' in caplog.text


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


@pytest.mark.e2e
def test_full_page_does_not_resize_while_polling(course_server, tmp_path):
    exe = os.environ.get("PC_BROWSER_EXECUTABLE") or None
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(tmp_path / "profile"), headless=True, executable_path=exe,
            viewport={"width": 1366, "height": 768},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(course_server.replace("index.html", "tall.html"))
            opts = RunOptions(max_pages=5, change_timeout=5, manual_timeout=2, full_page=True)
            with Recorder(tmp_path / "out") as rec:
                Runner(context, rec, opts).run()
                run_dir = rec.dir
            resizes = page.evaluate("() => window.resizes")
        finally:
            context.close()

    pngs = sorted(run_dir.glob("*.png"))
    assert len(pngs) == 2
    assert all(Image.open(p).height > 768 for p in pngs)  # gravadas em página inteira
    # só as 2 capturas gravadas redimensionam; o polling (dezenas de screenshots) não
    assert resizes <= 3 * len(pngs), resizes


@pytest.mark.e2e
def test_waits_for_disabled_next_and_captures_final_state(course_server, tmp_path, caplog):
    exe = os.environ.get("PC_BROWSER_EXECUTABLE") or None
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(tmp_path / "profile"), headless=True, executable_path=exe,
            viewport={"width": 1366, "height": 768},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(course_server.replace("index.html", "timeline.html"))
            opts = RunOptions(max_pages=5, change_timeout=5, manual_timeout=2)
            with Recorder(tmp_path / "out") as rec:
                Runner(context, rec, opts).run()
                run_dir = rec.dir
            events = page.evaluate("() => window.events")
        finally:
            context.close()

    with open(run_dir / "index.csv", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    # gravada ao aparecer (start) e a cada informação nova que apareceu (update)
    assert [r["trigger"] for r in rows] == ["start", "update", "update", "click"]
    assert events == ["enabled", "next"]  # nunca clicou com o SEGUINTE desativado
    red, green = (250, 230, 90), (20, 160, 60)  # «red» = 1.º bloco (amarelo claro)

    def px(n, xy):
        return Image.open(run_dir / f"{n:04d}.png").convert("RGB").getpixel(xy)

    assert px(1, (250, 400)) != red and px(1, (850, 400)) != green
    assert px(2, (250, 400)) == red and px(2, (850, 400)) != green
    assert px(3, (250, 400)) == red and px(3, (850, 400)) == green
    assert "parece pedir uma interação" not in caplog.text  # só narração: sem alerta


@pytest.mark.e2e
def test_interactive_slide_alerts_user_and_continues(course_server, tmp_path, caplog):
    exe = os.environ.get("PC_BROWSER_EXECUTABLE") or None
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(tmp_path / "profile"), headless=True, executable_path=exe,
            viewport={"width": 1366, "height": 768},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(course_server.replace("index.html", "interactive.html"))
            opts = RunOptions(max_pages=5, change_timeout=5, manual_timeout=2)
            with Recorder(tmp_path / "out") as rec:
                Runner(context, rec, opts).run()
                run_dir = rec.dir
            events = page.evaluate("() => window.events")
        finally:
            context.close()

    with open(run_dir / "index.csv", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["trigger"] for r in rows] == ["start", "update", "click"]
    assert events == ["enabled", "next"]
    # zona clicável (Storyline: classe cursor-hover, sem cursor «mão» sem o rato em cima)
    # detetada logo (antes dos 60 s de ready_alert) e descrita no alerta
    assert "parece pedir uma interação" in caplog.text and 'div "b)" #piece' in caplog.text
    assert ".component_base.std.outline" not in caplog.text.split("parece pedir")[1]  # leitor


@pytest.mark.e2e
def test_keep_last_only_on_progressive_slide(course_server, tmp_path):
    exe = os.environ.get("PC_BROWSER_EXECUTABLE") or None
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(tmp_path / "profile"), headless=True, executable_path=exe,
            viewport={"width": 1366, "height": 768},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(course_server.replace("index.html", "timeline.html"))
            opts = RunOptions(max_pages=5, change_timeout=5, manual_timeout=2)
            with Recorder(tmp_path / "out", keep_last_only=True) as rec:
                Runner(context, rec, opts).run()
                run_dir = rec.dir
        finally:
            context.close()

    assert sorted(p.name for p in run_dir.glob("*.png")) == ["0001.png", "0002.png"]
    first = Image.open(run_dir / "0001.png").convert("RGB")
    # o slide 1 ficou só com o estado final: os dois blocos que foram aparecendo
    assert first.getpixel((250, 400)) == (250, 230, 90)
    assert first.getpixel((850, 400)) == (20, 160, 60)


@pytest.mark.e2e
def test_close_gracefully_lets_lesson_report_exit(course_server, tmp_path, beacons):
    exe = os.environ.get("PC_BROWSER_EXECUTABLE") or None
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(tmp_path / "profile"), headless=True, executable_path=exe,
            viewport={"width": 1366, "height": 768},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(course_server.replace("index.html", "exit.html"))
        page.click("h1")  # beforeunload só pede confirmação depois de interação do utilizador
        close_gracefully(context)
    for _ in range(20):  # o aviso chega ao servidor de forma assíncrona
        if "/lms/exit" in beacons:
            break
        time.sleep(0.1)
    assert "/lms/exit" in beacons  # a lição conseguiu avisar o LMS da saída


@pytest.mark.e2e
def test_browser_is_private_nothing_persists(course_server):
    from pagecapture.runner import launch_context

    exe = os.environ.get("PC_BROWSER_EXECUTABLE") or None
    url = course_server.replace("index.html", "loop.html")
    with sync_playwright() as pw:
        ctx = launch_context(pw, viewport={"width": 800, "height": 600}, headless=True,
                             sandbox=False, executable_path=exe)
        page = ctx.new_page()
        page.goto(url)
        page.evaluate("() => { document.cookie = 'sessao=1; max-age=31536000'; "
                      "localStorage.setItem('x', '1'); }")
        assert "sessao=1" in page.evaluate("() => document.cookie")
        close_gracefully(ctx)

        ctx = launch_context(pw, viewport={"width": 800, "height": 600}, headless=True,
                             sandbox=False, executable_path=exe)
        page = ctx.new_page()
        page.goto(url)
        assert page.evaluate("() => document.cookie") == ""  # nem cookies persistentes
        assert page.evaluate("() => localStorage.getItem('x')") is None
        close_gracefully(ctx)

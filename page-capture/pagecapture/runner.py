"""Loop de captura no browser e gravação (PNG + index.csv)."""

from __future__ import annotations

import csv
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
from playwright.sync_api import BrowserContext, Frame, Page, Playwright
from playwright.sync_api import Error as PlaywrightError

from .imagediff import changed_fraction, thumbnail
from .scoring import rank

log = logging.getLogger("pagecapture")

POLL_INTERVAL = 0.4
SETTLE_DIFF = 0.001
SETTLE_MAX = 6.0
DUPLICATE_DIFF = 0.001
TOP_CANDIDATES = 3
CLICK_TIMEOUT_MS = 5000
SCREENSHOT_TIMEOUT_MS = 10000
MAX_MISSED_CLICKS = 2
DIAGNOSTIC_TOP = 10
BROWSER_HINT = ("Experimente: python -m playwright install chromium "
                "(ou --channel chrome / --channel msedge; em último caso --no-sandbox)")

COLLECT_JS = r"""
() => {
  const SEL = 'button,a,[role=button],[role=link],input[type=button],input[type=submit],' +
    'input[type=image],[onclick],[tabindex],[aria-label],[title],[class*=next i],[id*=next i],' +
    '[class*=arrow i],[class*=seguinte i],[class*=avancar i]';
  document.querySelectorAll('[data-pc-cand]').forEach(e => e.removeAttribute('data-pc-cand'));
  const vw = window.innerWidth, vh = window.innerHeight;
  const out = [];
  for (const el of document.querySelectorAll(SEL)) {
    if (out.length >= 400) break;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    if (r.right <= 0 || r.bottom <= 0 || r.left >= vw || r.top >= vh) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' ||
        parseFloat(st.opacity) === 0 || st.pointerEvents === 'none') continue;
    if (el.checkVisibility && !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}))
      continue;
    const idx = out.length;
    el.setAttribute('data-pc-cand', String(idx));
    const rawCls = el.getAttribute('class') || '';
    const img = el.querySelector('img[alt]');
    const text = (el.tagName === 'INPUT' ? el.value : el.innerText) || '';
    out.push({
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      text: text.trim().slice(0, 200),
      aria: el.getAttribute('aria-label') || '',
      title: el.getAttribute('title') || '',
      alt: el.getAttribute('alt') || (img ? img.getAttribute('alt') : '') || '',
      id: el.getAttribute('id') || '',
      cls: rawCls,
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true' ||
        /disabled/i.test(rawCls),
      x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height, vw, vh,
    });
  }
  return out;
}
"""

CSV_FIELDS = [
    "n", "file", "timestamp_utc", "url", "trigger", "control", "changed_fraction", "duplicate_of",
]


class BrowserClosedError(RuntimeError):
    """Todas as janelas do browser foram fechadas."""


class StopRequested(Exception):
    """Paragem pedida pelo utilizador (interface web)."""


def launch_context(pw: Playwright, profile: Path, *, viewport: dict[str, int],
                   headless: bool = False, channel: str | None = None, sandbox: bool = True,
                   executable_path: str | None = None) -> BrowserContext:
    """Browser com perfil persistente (a sessão de login fica no perfil local)."""
    return pw.chromium.launch_persistent_context(
        str(profile), headless=headless, channel=channel, viewport=viewport,
        chromium_sandbox=sandbox, executable_path=executable_path,
    )


@dataclass(frozen=True)
class RunOptions:
    next_selector: str | None = None
    max_pages: int = 1000
    change_threshold: float = 0.003
    change_timeout: float = 15.0
    manual_timeout: float = 300.0
    full_page: bool = False


def _new_run_dir(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    for i in range(1, 1000):
        path = base / (stamp if i == 1 else f"{stamp}_{i}")
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"Não foi possível criar uma pasta nova em {base}")


class Recorder:
    """Grava NNNN.png sequenciais e index.csv (flush linha a linha). Nunca sobrescreve."""

    def __init__(self, base_dir: Path):
        self.dir = _new_run_dir(Path(base_dir))
        self._file = open(self.dir / "index.csv", "x", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(CSV_FIELDS)
        self._file.flush()
        self._thumbs: list[Image.Image] = []

    @property
    def count(self) -> int:
        return len(self._thumbs)

    def _find_duplicate(self, thumb: Image.Image) -> int | None:
        for i in range(len(self._thumbs) - 1, -1, -1):
            if changed_fraction(self._thumbs[i], thumb) < DUPLICATE_DIFF:
                return i + 1
        return None

    def save(self, png: bytes, thumb: Image.Image, url: str, trigger: str, control: str,
             fraction: float | None) -> int:
        n = self.count + 1
        name = f"{n:04d}.png"
        dup = self._find_duplicate(thumb)
        with open(self.dir / name, "xb") as fh:
            fh.write(png)
        self._writer.writerow([
            n, name, datetime.now(timezone.utc).isoformat(timespec="seconds"), url, trigger,
            control, "" if fraction is None else f"{fraction:.4f}", dup or "",
        ])
        self._file.flush()
        self._thumbs.append(thumb)
        frac_txt = "" if fraction is None else f" (mudança {fraction:.2%})"
        log.info("Página %d gravada [%s]%s", n, trigger, frac_txt)
        if dup:
            log.warning("A página %d parece igual à página %d.", n, dup)
        return n

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> Recorder:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Runner:
    def __init__(self, context: BrowserContext, recorder: Recorder, opts: RunOptions,
                 stop_event: threading.Event | None = None):
        self.context = context
        self.recorder = recorder
        self.opts = opts
        self._stop = stop_event

    def _check_stop(self) -> None:
        if self._stop is not None and self._stop.is_set():
            raise StopRequested

    # --- browser -----------------------------------------------------------------------------
    def current_page(self) -> Page:
        """Janela mais recente ainda aberta (segue popups do LMS)."""
        for page in reversed(self.context.pages):
            if not page.is_closed():
                return page
        raise BrowserClosedError("Não há janelas abertas.")

    def _screenshot(self) -> tuple[bytes, Image.Image] | None:
        for attempt in (1, 2):
            self._check_stop()
            page = self.current_page()
            try:
                png = page.screenshot(full_page=self.opts.full_page, timeout=SCREENSHOT_TIMEOUT_MS)
                return png, thumbnail(png)
            except PlaywrightError as e:
                log.debug("Screenshot falhou (tentativa %d): %s", attempt, e)
                time.sleep(POLL_INTERVAL)
        return None

    def _visible_frames(self, page: Page) -> list[Frame | None]:
        frames: list[Frame | None] = []
        for frame in page.frames:
            if frame is not page.main_frame:
                try:
                    if frame.is_detached() or not frame.frame_element().is_visible():
                        log.debug("Frame invisível ignorado: %s", frame.url)
                        frames.append(None)
                        continue
                except PlaywrightError as e:
                    log.debug("Frame ignorado: %s", e)
                    frames.append(None)
                    continue
            frames.append(frame)
        return frames

    def _click_selector(self, page: Page, selector: str) -> str | None:
        for frame in page.frames:
            try:
                loc = frame.locator(selector)
                if loc.count() == 0:
                    continue
                loc.first.click(timeout=CLICK_TIMEOUT_MS)
                return f"seletor {selector}"
            except PlaywrightError as e:
                log.debug("Seletor %r falhou no frame %s: %s", selector, frame.url, e)
        return None

    def click_next(self) -> str | None:
        """Clica o controlo de avançar. Devolve a descrição do elemento ou None."""
        page = self.current_page()
        if self.opts.next_selector:
            return self._click_selector(page, self.opts.next_selector)
        frames = self._visible_frames(page)
        features: list[list[dict]] = []
        for frame in frames:
            if frame is None:
                features.append([])
                continue
            try:
                features.append(frame.evaluate(COLLECT_JS))
            except PlaywrightError as e:
                log.debug("Recolha falhou no frame %s: %s", frame.url, e)
                features.append([])
        candidates = rank(features)
        if not candidates and log.isEnabledFor(logging.DEBUG):
            self._log_diagnostics(frames, features)
        for cand in candidates[:TOP_CANDIDATES]:
            log.debug("Candidato %.1f: %s", cand.score, cand.description)
        for cand in candidates[:TOP_CANDIDATES]:
            frame = frames[cand.frame_index]
            try:
                frame.locator(f'[data-pc-cand="{cand.idx}"]').click(timeout=CLICK_TIMEOUT_MS)
                log.info("Clique: %s", cand.description)
                return cand.description
            except PlaywrightError as e:
                log.debug("Clique falhou em %s: %s", cand.description, e)
        return None

    def _log_diagnostics(self, frames: list[Frame | None], features: list[list[dict]]) -> None:
        """Com -v: o que a heurística viu quando não encontrou nenhum candidato."""
        for i, (frame, feats) in enumerate(zip(frames, features, strict=True)):
            url = frame.url if frame is not None else "(ignorado)"
            log.debug("Frame %d: %d elementos — %s", i, len(feats), url[:150])
        best = rank(features, min_score=float("-inf"))[:DIAGNOSTIC_TOP]
        if not best:
            log.debug("Nenhum elemento pontuável (todos excluídos ou nenhum recolhido).")
        for cand in best:
            log.debug("  abaixo do mínimo: %.1f frame %d %s",
                      cand.score, cand.frame_index, cand.description)

    # --- deteção de mudança ------------------------------------------------------------------
    def _settle(self, png: bytes, thumb: Image.Image) -> tuple[bytes, Image.Image]:
        deadline = time.monotonic() + SETTLE_MAX
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            shot = self._screenshot()
            if shot is None:
                continue
            stable = changed_fraction(thumb, shot[1]) < SETTLE_DIFF
            png, thumb = shot
            if stable:
                break
        return png, thumb

    def wait_for_change(self, ref: Image.Image, timeout: float
                        ) -> tuple[bytes, Image.Image, float] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            shot = self._screenshot()
            if shot and changed_fraction(ref, shot[1]) >= self.opts.change_threshold:
                png, thumb = self._settle(*shot)
                frac = changed_fraction(ref, thumb)
                if frac >= self.opts.change_threshold:
                    return png, thumb, frac
            time.sleep(POLL_INTERVAL)
        return None

    # --- loop principal ----------------------------------------------------------------------
    def run(self) -> None:
        try:
            self._run()
        except StopRequested:
            log.info("Paragem pedida; a terminar.")

    def _run(self) -> None:
        shot = self._screenshot()
        if shot is None:
            raise RuntimeError("Não foi possível capturar a página inicial.")
        png, ref = self._settle(*shot)
        self.recorder.save(png, ref, self.current_page().url, "start", "", None)

        missed = 0
        while self.recorder.count < self.opts.max_pages:
            self._check_stop()
            control = self.click_next() if missed < MAX_MISSED_CLICKS else None
            if control is not None:
                result = self.wait_for_change(ref, self.opts.change_timeout)
                if result is None:
                    missed += 1
                    log.warning("O clique não mudou a página (%d/%d).", missed, MAX_MISSED_CLICKS)
                    continue
                trigger = "click"
            else:
                reason = ("cliques sem efeito" if missed >= MAX_MISSED_CLICKS
                          else "controlo de avançar não encontrado")
                log.warning("Modo assistido (%s): avance manualmente no browser "
                            "(espero até %.0f s).", reason, self.opts.manual_timeout)
                result = self.wait_for_change(ref, self.opts.manual_timeout)
                if result is None:
                    log.info("Sem mudança durante %.0f s; a terminar.", self.opts.manual_timeout)
                    return
                trigger, control = "manual", ""
            missed = 0
            png, ref, frac = result
            self.recorder.save(png, ref, self.current_page().url, trigger, control, frac)
        log.info("Atingido o limite de %d páginas.", self.opts.max_pages)

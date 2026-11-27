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

from .imagediff import changed_fraction, mask_bands, thumbnail
from .scoring import MIN_SCORE, describe, is_blocked_next, rank, score

log = logging.getLogger("pagecapture")

POLL_INTERVAL = 0.4
SETTLE_DIFF = 0.001
SETTLE_MAX = 6.0
DUPLICATE_DIFF = 0.001
TOP_CANDIDATES = 3
CLICK_TIMEOUT_MS = 5000
SCREENSHOT_TIMEOUT_MS = 10000
MAX_MISSED_CLICKS = 2
MAX_CONSECUTIVE_DUPLICATES = 5
READY_POLL = 1.0
READY_TIMEOUT = 600.0  # espera máxima por um SEGUINTE desativado antes do modo assistido
HOTSPOT_CHECK_EVERY = 5  # verificações de READY_POLL entre pesquisas de zonas clicáveis
READY_PROGRESS_EVERY = 15  # registo periódico enquanto espera pelo SEGUINTE
ASSIST_INTERACT = "interact"  # SEGUINTE desativado: interagir com o slide (sem carregar SEGUINTE)
ASSIST_ADVANCE = "advance"  # sem SEGUINTE utilizável: avançar manualmente
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

# Avisos de "navegação limitada" (ex. Articulate Storyline): o slide ainda não terminou.
BLOCKER_PATTERN = (
    r"navega[cç][aã]o\s+(?:[eé]\s+)?(?:limitada|restrita)"
    r"|navigation\s+(?:is\s+)?(?:restricted|limited)"
    r"|garanta\s+que\s+(?:leu|viu)|you\s+must\s+(?:complete|view)"
)
RESTRICTED_RETRY = 5.0

BLOCKER_JS = r"""
(src) => {
  const re = new RegExp(src, 'i');
  const okRe = /^\s*(ok|fechar|close)\s*$/i;
  const vis = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    return !el.checkVisibility ||
      el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
  };
  document.querySelectorAll('[data-pc-dismiss]').forEach(e => e.removeAttribute('data-pc-dismiss'));
  for (const el of document.querySelectorAll('body *')) {
    if (el.childElementCount > 3) continue;
    const t = el.textContent || '';
    if (t.length > 300 || !re.test(t) || !vis(el)) continue;
    let box = el.parentElement;
    for (let i = 0; i < 6 && box; i++, box = box.parentElement) {
      const btn = Array.from(box.querySelectorAll('button,[role=button],a,div,span'))
        .find(b => b.childElementCount <= 2 && okRe.test(b.textContent || '') && vis(b));
      if (btn) {
        btn.setAttribute('data-pc-dismiss', '1');
        return {text: t.replace(/\s+/g, ' ').trim().slice(0, 120), button: true};
      }
    }
    return {text: t.replace(/\s+/g, ' ').trim().slice(0, 120), button: false};
  }
  return null;
}
"""

# Zonas clicáveis do slide (cursor «mão»), para saber se o slide espera uma interação.
HOTSPOT_JS = r"""
() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    if (out.length >= 50) break;
    const st = getComputedStyle(el);
    if (st.cursor !== 'pointer') continue;
    const parent = el.parentElement;
    if (parent && getComputedStyle(parent).cursor === 'pointer') continue;  // só o topo
    const r = el.getBoundingClientRect();
    if (r.width < 10 || r.height < 10) continue;
    if (r.right <= 0 || r.bottom <= 0 || r.left >= vw || r.top >= vh) continue;
    if (el.checkVisibility && !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}))
      continue;
    const rawCls = el.getAttribute('class') || '';
    const text = (el.innerText || el.textContent || '').trim();
    out.push({
      tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
      text: text.slice(0, 200), aria: el.getAttribute('aria-label') || '',
      title: el.getAttribute('title') || '', alt: el.getAttribute('alt') || '',
      id: el.getAttribute('id') || '', cls: typeof rawCls === 'string' ? rawCls : '',
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height, vw, vh,
    });
  }
  return out;
}
"""

CSV_FIELDS = [
    "n", "file", "timestamp_utc", "url", "trigger", "control", "changed_fraction",
]


class BrowserClosedError(RuntimeError):
    """Todas as janelas do browser foram fechadas."""


class StopRequested(Exception):
    """Paragem pedida pelo utilizador (interface web)."""


def launch_context(pw: Playwright, profile: Path, *, viewport: dict[str, int] | None,
                   headless: bool = False, channel: str | None = None, sandbox: bool = True,
                   executable_path: str | None = None) -> BrowserContext:
    """Browser com perfil persistente (a sessão de login fica no perfil local).

    viewport=None: sem emulação de tamanho; a página usa o tamanho real da janela.
    """
    return pw.chromium.launch_persistent_context(
        str(profile), headless=headless, channel=channel, viewport=viewport,
        no_viewport=viewport is None, chromium_sandbox=sandbox, executable_path=executable_path,
    )


@dataclass(frozen=True)
class RunOptions:
    next_selector: str | None = None
    max_pages: int = 1000
    change_threshold: float = 0.003
    change_timeout: float = 15.0
    manual_timeout: float = 300.0
    full_page: bool = False
    ready_alert: float = 60.0  # alerta se o SEGUINTE continuar desativado (sem zonas clicáveis)


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
             fraction: float | None) -> int | None:
        """Grava a página. Repetida (quase igual a uma já gravada) -> não grava e devolve None."""
        dup = self._find_duplicate(thumb)
        if dup:
            log.warning("Página repetida (igual à página %d): não gravada.", dup)
            return None
        n = self.count + 1
        name = f"{n:04d}.png"
        with open(self.dir / name, "xb") as fh:
            fh.write(png)
        self._writer.writerow([
            n, name, datetime.now(timezone.utc).isoformat(timespec="seconds"), url, trigger,
            control, "" if fraction is None else f"{fraction:.4f}",
        ])
        self._file.flush()
        self._thumbs.append(thumb)
        frac_txt = "" if fraction is None else f" (mudança {fraction:.2%})"
        log.info("Página %d gravada [%s]%s", n, trigger, frac_txt)
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
        self.assist: str | None = None  # ASSIST_* enquanto se espera pelo utilizador
        self.last_settle_stable = False
        self._nav_bands: list[tuple[float, float]] = []  # faixas da barra de navegação (0..1)

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
                # nunca full_page aqui: redimensiona a página a cada captura (Storyline reajusta)
                png = page.screenshot(timeout=SCREENSHOT_TIMEOUT_MS)
                return png, thumbnail(png)
            except PlaywrightError as e:
                log.debug("Screenshot falhou (tentativa %d): %s", attempt, e)
                time.sleep(POLL_INTERVAL)
        return None

    def _final_png(self, png: bytes) -> bytes:
        """PNG a gravar: com --full-page, uma única captura da página inteira; senão o mesmo."""
        if not self.opts.full_page:
            return png
        try:
            return self.current_page().screenshot(full_page=True, timeout=SCREENSHOT_TIMEOUT_MS)
        except PlaywrightError as e:
            log.warning("Captura da página inteira falhou; gravo a área visível: %s",
                        str(e).splitlines()[0])
            return png

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
        frames, features = self._collect(page)
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

    def _collect(self, page: Page) -> tuple[list[Frame | None], list[list[dict]]]:
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
        return frames, features

    def next_is_blocked(self) -> bool:
        """True se o controlo de avançar existe mas está desativado (e nenhum outro serve)."""
        page = self.current_page()
        if self.opts.next_selector:
            for frame in page.frames:
                try:
                    loc = frame.locator(self.opts.next_selector)
                    if loc.count():
                        return loc.first.is_disabled()
                except PlaywrightError as e:
                    log.debug("Seletor %r falhou no frame %s: %s",
                              self.opts.next_selector, frame.url, e)
            return False
        frames, features = self._collect(page)
        self._update_nav_bands(page, frames, features)
        if rank(features):
            return False
        return any(is_blocked_next(f) for feats in features for f in feats)

    def _update_nav_bands(self, page: Page, frames: list[Frame | None],
                          features: list[list[dict]]) -> None:
        """Faixa horizontal de cada controlo de avançar (ativo ou não), em frações da janela."""
        bands: list[tuple[float, float]] = []
        try:
            height = page.evaluate("() => window.innerHeight") or 0
        except PlaywrightError:
            return
        for fi, feats in enumerate(features):
            frame = frames[fi]
            for idx, f in enumerate(feats):
                if frame is None or not (is_blocked_next(f) or (score(f) or 0) >= MIN_SCORE):
                    continue
                try:
                    box = frame.locator(f'[data-pc-cand="{idx}"]').bounding_box(timeout=1000)
                except PlaywrightError:
                    continue
                if box and height:
                    pad = box["height"] * 0.5
                    bands.append(((box["y"] - pad) / height,
                                  (box["y"] + box["height"] + pad) / height))
        self._nav_bands = bands

    def _content_diff(self, a: Image.Image, b: Image.Image) -> float:
        """Diferença ignorando a barra de navegação (ex. o SEGUINTE a ficar ativo)."""
        return changed_fraction(mask_bands(a, self._nav_bands), mask_bands(b, self._nav_bands))

    def find_hotspots(self) -> list[str]:
        """Zonas clicáveis do slide (cursor «mão») que não são controlos de navegação."""
        found: list[str] = []
        for frame in self._visible_frames(self.current_page()):
            if frame is None:
                continue
            try:
                feats = frame.evaluate(HOTSPOT_JS)
                controls = frame.evaluate(COLLECT_JS)
            except PlaywrightError as e:
                log.debug("Pesquisa de zonas clicáveis falhou no frame %s: %s", frame.url, e)
                continue
            # barra de navegação do leitor: a faixa horizontal do controlo de avançar
            nexts = [c for c in controls if is_blocked_next(c) or (score(c) or 0) >= MIN_SCORE]
            for f in feats:
                # exclui navegação: termos negativos/positivos (score None ou > 0) e SEGUINTE
                if score(f) != 0 or is_blocked_next(f):
                    continue
                if any(abs(f["y"] - n["y"]) <= max(n["h"], f["h"]) for n in nexts):
                    log.debug("Zona clicável ignorada (barra de navegação): %s", describe(f))
                    continue
                found.append(describe(f))
        log.debug("Zonas clicáveis no slide: %s", "; ".join(found) if found else "nenhuma")
        return found

    def wait_until_ready(self, ref: Image.Image, timeout: float = READY_TIMEOUT
                         ) -> Image.Image:
        """Espera que um SEGUINTE desativado fique ativo, gravando a informação nova que for
        aparecendo no slide. Devolve a referência atualizada.

        Alerta o utilizador logo que o slide tenha zonas clicáveis (provável interação pedida),
        ou ao fim de opts.ready_alert segundos (ex. narração longa).
        """
        start = time.monotonic()
        checks = 0
        try:
            while time.monotonic() - start < timeout and self.next_is_blocked():
                if checks == 0:
                    log.info("SEGUINTE desativado (conteúdo a decorrer ou interação pedida): "
                             "à espera que fique ativo.")
                if self.assist is None and checks % HOTSPOT_CHECK_EVERY == 0:
                    hotspots = self.find_hotspots()
                    if hotspots:
                        self.assist = ASSIST_INTERACT
                        log.warning("O slide parece pedir uma interação (zonas clicáveis: %s). "
                                    "Faça-a no browser; não carregue em SEGUINTE.",
                                    "; ".join(hotspots[:3]))
                if self.assist is None and time.monotonic() - start > self.opts.ready_alert:
                    self.assist = ASSIST_INTERACT
                    log.warning("SEGUINTE desativado há %.0f s: se o slide pedir uma interação, "
                                "faça-a no browser.", self.opts.ready_alert)
                if checks and checks % READY_PROGRESS_EVERY == 0:
                    log.info("Ainda à espera que o SEGUINTE fique ativo (%.0f s).",
                             time.monotonic() - start)
                checks += 1
                ref = self._watch(ref, READY_POLL)
        finally:
            self.assist = None
        if checks:  # o SEGUINTE acabou de ativar: o slide pode ter mudado no último instante
            ref = self.capture_update(ref)
        return ref

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

    def dismiss_blocker(self) -> str | None:
        """Fecha um aviso de navegação limitada, se visível. Devolve o texto do aviso ou None."""
        page = self.current_page()
        for frame in page.frames:
            try:
                found = frame.evaluate(BLOCKER_JS, BLOCKER_PATTERN)
            except PlaywrightError as e:
                log.debug("Verificação de aviso falhou no frame %s: %s", frame.url, e)
                continue
            if not found:
                continue
            try:
                if found["button"]:
                    frame.locator('[data-pc-dismiss="1"]').first.click(timeout=CLICK_TIMEOUT_MS)
                else:
                    page.keyboard.press("Escape")
            except PlaywrightError as e:
                log.warning("Não foi possível fechar o aviso: %s", str(e).splitlines()[0])
            return found["text"]
        return None

    def _pause(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._check_stop()
            time.sleep(0.2)

    # --- deteção de mudança ------------------------------------------------------------------
    def _settle(self, png: bytes, thumb: Image.Image) -> tuple[bytes, Image.Image]:
        """Espera que o ecrã estabilize. self.last_settle_stable diz se estabilizou."""
        deadline = time.monotonic() + SETTLE_MAX
        self.last_settle_stable = False
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            shot = self._screenshot()
            if shot is None:
                continue
            stable = changed_fraction(thumb, shot[1]) < SETTLE_DIFF
            png, thumb = shot
            if stable:
                self.last_settle_stable = True
                break
        return png, thumb

    def capture_update(self, ref: Image.Image) -> Image.Image:
        """Grava informação nova que apareceu no slide (trigger «update»). Devolve a referência.

        Só grava depois de o ecrã estabilizar: animações contínuas não enchem a pasta.
        """
        shot = self._screenshot()
        if shot is None or self._content_diff(ref, shot[1]) < self.opts.change_threshold:
            return ref
        png, thumb = self._settle(*shot)
        if not self.last_settle_stable:
            log.debug("Ecrã ainda em animação; a mudança será gravada quando estabilizar.")
            return ref
        frac = self._content_diff(ref, thumb)
        if frac < self.opts.change_threshold:
            return ref
        if self.recorder.count < self.opts.max_pages:
            self.recorder.save(self._final_png(png), thumb, self.current_page().url, "update",
                               "", frac)
        return thumb

    def _watch(self, ref: Image.Image, seconds: float) -> Image.Image:
        """Pausa que continua a gravar a informação nova que for aparecendo."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._pause(min(READY_POLL, max(0.0, deadline - time.monotonic())))
            ref = self.capture_update(ref)
        return ref

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
        self.recorder.save(self._final_png(png), ref, self.current_page().url, "start", "", None)
        ref = self.wait_until_ready(ref)

        missed = 0
        duplicates = 0
        blocked_since: float | None = None
        while self.recorder.count < self.opts.max_pages:
            self._check_stop()
            if self.dismiss_blocker():  # aviso que ficou aberto
                self._pause(1.0)
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
                self.assist = ASSIST_ADVANCE
                try:
                    result = self.wait_for_change(ref, self.opts.manual_timeout)
                finally:
                    self.assist = None
                if result is None:
                    log.info("Sem mudança durante %.0f s; a terminar.", self.opts.manual_timeout)
                    return
                trigger, control = "manual", ""

            blocker = self.dismiss_blocker()
            if blocker:  # a "mudança" foi o aviso; não gravar e tentar mais tarde
                now = time.monotonic()
                blocked_since = blocked_since or now
                if now - blocked_since > self.opts.manual_timeout:
                    log.warning("Navegação limitada há mais de %.0f s; passo a modo assistido.",
                                self.opts.manual_timeout)
                    missed = MAX_MISSED_CLICKS
                else:
                    log.info("Navegação limitada («%s»): aviso fechado; nova tentativa em %.0f s.",
                             blocker[:60], RESTRICTED_RETRY)
                hotspots = self.find_hotspots()  # o slide pede uma interação?
                if hotspots:
                    log.warning("O slide parece pedir uma interação (zonas clicáveis: %s). "
                                "Faça-a no browser; não carregue em SEGUINTE.",
                                "; ".join(hotspots[:3]))
                    self.assist = ASSIST_INTERACT
                try:
                    ref = self._watch(ref, RESTRICTED_RETRY)
                finally:
                    self.assist = None
                continue
            missed = 0
            blocked_since = None
            png, ref, frac = result
            saved = self.recorder.save(self._final_png(png), ref, self.current_page().url,
                                       trigger, control, frac)
            ref = self.wait_until_ready(ref)  # grava ao aparecer e cada informação nova
            if saved:
                duplicates = 0
                continue
            duplicates += 1
            if duplicates >= MAX_CONSECUTIVE_DUPLICATES:
                log.warning("%d páginas repetidas seguidas: o conteúdo parece estar em ciclo; "
                            "a terminar.", duplicates)
                return
        log.info("Atingido o limite de %d páginas.", self.opts.max_pages)

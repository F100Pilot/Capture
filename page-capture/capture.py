"""CLI: abre o conteúdo no browser, captura cada página nova e avança automaticamente."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from pagecapture import __version__
from pagecapture.runner import BrowserClosedError, Recorder, Runner, RunOptions

EXIT_OK, EXIT_ERROR, EXIT_BROWSER = 0, 1, 2
GOTO_TIMEOUT_MS = 60000
log = logging.getLogger("pagecapture")


def url_arg(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return value
    if parsed.scheme == "file" and parsed.path:
        return value
    raise argparse.ArgumentTypeError("URL inválido: use http://, https:// ou file://")


def viewport_arg(value: str) -> dict[str, int]:
    m = re.fullmatch(r"(\d{2,5})[xX](\d{2,5})", value.strip())
    if not m:
        raise argparse.ArgumentTypeError("viewport inválido: use LARGURAxALTURA, ex. 1366x768")
    width, height = int(m[1]), int(m[2])
    if not (200 <= width <= 10000 and 200 <= height <= 10000):
        raise argparse.ArgumentTypeError("viewport fora dos limites (200-10000 px)")
    return {"width": width, "height": height}


def int_min1(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"inteiro inválido: {value}") from None
    if n < 1:
        raise argparse.ArgumentTypeError("tem de ser >= 1")
    return n


def positive_float(value: str) -> float:
    try:
        x = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"número inválido: {value}") from None
    if not x > 0:
        raise argparse.ArgumentTypeError("tem de ser > 0")
    return x


def fraction_arg(value: str) -> float:
    x = positive_float(value)
    if x >= 1:
        raise argparse.ArgumentTypeError("tem de estar entre 0 e 1 (ex. 0.003)")
    return x


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="capture.py",
        description="Captura conteúdo web/e-learning página a página e avança automaticamente.",
    )
    p.add_argument("url", type=url_arg, help="URL inicial (http, https ou file)")
    p.add_argument("--out", type=Path, default=Path("capturas"), help="pasta base das capturas")
    p.add_argument("--profile", type=Path, default=Path(".browser-profile"),
                   help="perfil local do browser (mantém a sessão; não versionar)")
    p.add_argument("--channel", choices=["chrome", "msedge"],
                   help="usar o Chrome/Edge instalado em vez do Chromium do Playwright")
    p.add_argument("--viewport", type=viewport_arg, default=viewport_arg("1366x768"),
                   help="tamanho da janela, ex. 1366x768")
    p.add_argument("--next-selector", help="seletor CSS do botão avançar (substitui a heurística)")
    p.add_argument("--max-pages", type=int_min1, default=1000)
    p.add_argument("--change-threshold", type=fraction_arg, default=0.003,
                   help="fração mínima de píxeis alterados para contar como página nova")
    p.add_argument("--change-timeout", type=positive_float, default=15.0,
                   help="segundos à espera de mudança após um clique")
    p.add_argument("--manual-timeout", type=positive_float, default=300.0,
                   help="segundos à espera de avanço manual antes de terminar")
    p.add_argument("--full-page", action="store_true", help="screenshot da página inteira")
    p.add_argument("--headless", action="store_true", help="sem janela (impede login manual)")
    p.add_argument("--no-prompt", action="store_true", help="não esperar ENTER antes de capturar")
    p.add_argument("-v", "--verbose", action="store_true", help="registo detalhado")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _session(context, args: argparse.Namespace) -> int:
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.goto(args.url, wait_until="load", timeout=GOTO_TIMEOUT_MS)
    except PlaywrightError as e:
        if args.no_prompt:
            log.error("Não foi possível abrir %s: %s", args.url, str(e).splitlines()[0])
            return EXIT_ERROR
        log.warning("Não foi possível abrir %s; pode navegar manualmente.", args.url)
    if not args.no_prompt:
        input("\nFaça login e abra a primeira página a capturar. Depois carregue ENTER aqui... ")

    opts = RunOptions(
        next_selector=args.next_selector, max_pages=args.max_pages,
        change_threshold=args.change_threshold, change_timeout=args.change_timeout,
        manual_timeout=args.manual_timeout, full_page=args.full_page,
    )
    with Recorder(args.out) as recorder:
        log.info("A gravar em %s (Ctrl+C para parar)", recorder.dir)
        try:
            Runner(context, recorder, opts).run()
        except BrowserClosedError:
            log.warning("O browser foi fechado; a terminar.")
        finally:
            log.info("Gravadas %d páginas em %s", recorder.count, recorder.dir)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
    )
    try:
        with sync_playwright() as pw:
            try:
                context = pw.chromium.launch_persistent_context(
                    str(args.profile), headless=args.headless, channel=args.channel,
                    viewport=args.viewport,
                )
            except PlaywrightError as e:
                log.error("O browser não abriu: %s", str(e).splitlines()[0])
                log.error("Experimente: python -m playwright install chromium "
                          "(ou use --channel chrome / --channel msedge)")
                return EXIT_BROWSER
            try:
                return _session(context, args)
            except PlaywrightError as e:
                log.error("Erro do browser: %s", str(e).splitlines()[0])
                return EXIT_ERROR
            except RuntimeError as e:
                log.error("%s", e)
                return EXIT_ERROR
            finally:
                try:
                    context.close()
                except Exception as e:  # o browser pode já ter morrido (Ctrl+C)
                    log.debug("Erro ao fechar o browser: %s", e)
    except KeyboardInterrupt:
        # o Ctrl+C também mata o driver do Playwright; silencia o ruído do asyncio ao sair
        logging.getLogger("asyncio").setLevel(logging.CRITICAL)
        log.warning("Interrompido (Ctrl+C). As capturas já gravadas foram mantidas.")
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

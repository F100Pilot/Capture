"""Interface web local (127.0.0.1) para lançar, acompanhar e rever capturas.

Sem dependências novas: http.server da biblioteca standard. O Playwright corre numa thread própria
(a API síncrona exige que todas as chamadas sejam feitas na mesma thread).
"""

from __future__ import annotations

import csv
import json
import logging
import re
import secrets
import threading
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .runner import (
    BROWSER_HINT,
    BrowserClosedError,
    Recorder,
    Runner,
    RunOptions,
    close_gracefully,
    delete_captures,
    launch_context,
    wait_for_new_page,
)
from .validators import fraction_arg, int_min1, positive_float, url_arg, viewport_arg

log = logging.getLogger("pagecapture")

GOTO_TIMEOUT_MS = 60000
MAX_REOPEN = 3  # reaberturas do URL inicial se o site fechar a janela antes de começar
LOG_LINES = 1000
MAX_BODY = 64 * 1024
UI_FILE = Path(__file__).with_name("ui.html")
RUN_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}(?:_\d+)?$")
FILE_RE = re.compile(r"^(?:\d{4}(?:-\d{1,4})?\.png|index\.csv)$")
CHANNELS = {"": None, "msedge": "msedge", "chrome": "chrome"}
BUSY_STATES = {"opening", "login", "capturing", "review", "stopping"}


class BusyError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionParams:
    url: str
    channel: str | None
    viewport: dict[str, int]
    sandbox: bool
    verbose: bool
    keep_last: bool
    opts: RunOptions


def parse_params(data: dict) -> SessionParams:
    """Valida o pedido JSON da interface. Erros -> ValueError com mensagem para o utilizador."""
    if not isinstance(data, dict):
        raise ValueError("pedido inválido")

    def field(name: str, default: str = "") -> str:
        value = data.get(name, default)
        return "" if value is None else str(value).strip()

    try:
        channel_key = field("channel", "msedge")
        if channel_key not in CHANNELS:
            raise ValueError("browser inválido")
        selector = field("next_selector") or None
        opts = RunOptions(
            next_selector=selector,
            max_pages=int_min1(field("max_pages", "1000")),
            change_threshold=fraction_arg(field("change_threshold", "0.003")),
            change_timeout=positive_float(field("change_timeout", "15")),
            manual_timeout=positive_float(field("manual_timeout", "300")),
            full_page=False,  # redimensiona a página a cada gravação; só na CLI (--full-page)
            ready_alert=positive_float(field("ready_alert", "60")),
        )
        return SessionParams(
            url=url_arg(field("url")),
            channel=CHANNELS[channel_key],
            viewport=viewport_arg(field("viewport", "1366x768")),
            sandbox=not bool(data.get("no_sandbox")),
            verbose=bool(data.get("verbose")),
            keep_last=False,  # nada é apagado automaticamente; o utilizador escolhe na galeria
            opts=opts,
        )
    except ValueError:
        raise
    except Exception as e:  # argparse.ArgumentTypeError dos validadores
        raise ValueError(str(e)) from None


class LogBuffer(logging.Handler):
    """Guarda as últimas linhas do logger 'pagecapture' para a interface."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        self._lines: deque[tuple[int, str, str]] = deque(maxlen=LOG_LINES)
        self._seq = 0
        self._lines_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:
            self.handleError(record)
            return
        with self._lines_lock:
            self._seq += 1
            self._lines.append((self._seq, record.levelname, text))

    def since(self, seq: int) -> tuple[list[dict], int]:
        with self._lines_lock:
            lines = [{"seq": s, "level": lv, "text": t} for s, lv, t in self._lines if s > seq]
            return lines, self._seq


class Controller:
    """Uma sessão de captura de cada vez, numa thread dedicada ao Playwright."""

    def __init__(self, out: Path, *, headless: bool = False,
                 executable_path: str | None = None):
        self.out = Path(out)
        self.headless = headless
        self.executable_path = executable_path
        self.logs = LogBuffer()
        log.addHandler(self.logs)
        self._lock = threading.Lock()
        self._state = "idle"
        self._message = "Pronto."
        self._run_dir: Path | None = None
        self._recorder: Recorder | None = None
        self._runner: Runner | None = None
        self._begin = threading.Event()
        self._stop = threading.Event()
        self._close = threading.Event()  # fechar o browser depois da captura
        self._thread: threading.Thread | None = None

    # --- estado ------------------------------------------------------------------------------
    def _set(self, state: str, message: str) -> None:
        with self._lock:
            self._state, self._message = state, message

    def status(self, since: int = 0) -> dict:
        lines, last = self.logs.since(since)
        with self._lock:
            rec = self._recorder
            runner = self._runner
            return {
                "state": self._state,
                "message": self._message,
                "run": self._run_dir.name if self._run_dir else None,
                "count": rec.count if rec else 0,
                "version": rec.version if rec else 0,
                "assist": runner.assist if runner and self._state == "capturing" else None,
                "logs": lines,
                "last_seq": last,
                "runs": self.list_runs(),
            }

    def list_runs(self) -> list[str]:
        if not self.out.is_dir():
            return []
        return sorted((p.name for p in self.out.iterdir() if p.is_dir() and RUN_RE.match(p.name)),
                      reverse=True)

    def run_rows(self, run: str) -> list[dict]:
        if not RUN_RE.match(run):
            raise FileNotFoundError(run)
        path = self.out / run / "index.csv"
        with open(path, encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def file_path(self, run: str, name: str) -> Path:
        if not (RUN_RE.match(run) and FILE_RE.match(name)):
            raise FileNotFoundError(name)
        path = self.out / run / name
        if not path.is_file():
            raise FileNotFoundError(name)
        return path

    def delete(self, run: str, files: object) -> int:
        """Move capturas escolhidas para a subpasta «apagadas» (só capturas já terminadas)."""
        if not RUN_RE.match(run) or not (self.out / run / "index.csv").is_file():
            raise FileNotFoundError(run)
        if (not isinstance(files, list) or not files or len(files) > 5000
                or not all(isinstance(f, str) and FILE_RE.match(f) and f.endswith(".png")
                           for f in files)):
            raise ValueError("lista de ficheiros inválida")
        with self._lock:
            if (self._state in ("opening", "login", "capturing", "stopping")
                    and self._run_dir is not None and self._run_dir.name == run):
                raise BusyError("Esta captura ainda está a decorrer; apague depois de terminar.")
            return delete_captures(self.out / run, files)

    # --- ações -------------------------------------------------------------------------------
    def start(self, params: SessionParams) -> None:
        with self._lock:
            if self._state in BUSY_STATES:
                raise BusyError("Já existe uma sessão em curso.")
            self._state, self._message = "opening", "A abrir o browser…"
            self._run_dir = None
            self._recorder = None
            self._runner = None
            self._begin.clear()
            self._stop.clear()
            self._close.clear()
            log.setLevel(logging.DEBUG if params.verbose else logging.INFO)
            self._thread = threading.Thread(target=self._work, args=(params,),
                                            name="pagecapture-browser", daemon=True)
            self._thread.start()

    def begin(self) -> None:
        with self._lock:
            if self._state != "login":
                raise BusyError("O browser ainda não está pronto para começar.")
        self._begin.set()

    def stop(self) -> None:
        """Parar a captura; depois da captura («review»), fechar o browser."""
        with self._lock:
            if self._state not in BUSY_STATES:
                return
            closing = self._state == "review"
            self._state, self._message = "stopping", ("A fechar o browser…" if closing
                                                       else "A parar…")
        (self._close if closing else self._stop).set()

    def shutdown(self, timeout: float = 20) -> None:
        self._stop.set()
        self._close.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout)
        log.removeHandler(self.logs)

    # --- thread do browser -------------------------------------------------------------------
    def _work(self, p: SessionParams) -> None:
        try:
            with sync_playwright() as pw:
                try:
                    context = launch_context(
                        pw, viewport=p.viewport, headless=self.headless,
                        channel=p.channel, sandbox=p.sandbox,
                        executable_path=self.executable_path,
                    )
                except PlaywrightError as e:
                    log.error("O browser não abriu: %s", str(e).splitlines()[0])
                    log.error("%s", BROWSER_HINT)
                    self._set("error", "O browser não abriu. Ver o registo.")
                    return
                try:
                    self._session(context, p)
                finally:
                    close_gracefully(context)
                    if self._state == "stopping":
                        self._set("done", "Browser fechado.")
        except Exception as e:  # nunca deixar a thread morrer em silêncio
            log.exception("Erro inesperado")
            self._set("error", f"Erro: {str(e).splitlines()[0] if str(e) else type(e).__name__}")

    def _session(self, context, p: SessionParams) -> None:
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(p.url, wait_until="load", timeout=GOTO_TIMEOUT_MS)
        except PlaywrightError as e:
            log.warning("Não foi possível abrir o URL (%s); navegue manualmente.",
                        str(e).splitlines()[0])
        self._set("login", "Faça login no browser que abriu, abra a primeira página a capturar "
                           "e carregue em «Começar captura».")
        log.info("Browser aberto. À espera de «Começar captura».")
        reopened = 0
        while not self._begin.is_set():
            if self._stop.is_set():
                log.info("Cancelado antes de começar.")
                self._set("done", "Cancelado.")
                return
            page = wait_for_new_page(context)
            if page is None:
                # no modo privado o site pode fechar a janela inicial (ex. após o login)
                reopened += 1
                if reopened > MAX_REOPEN:
                    log.warning("O browser foi fechado antes de começar.")
                    self._set("done", "O browser foi fechado.")
                    return
                log.warning("A janela fechou-se (habitual após o login em modo privado). "
                            "Abro de novo o URL inicial; a sessão mantém-se.")
                page = context.new_page()
                try:
                    page.goto(p.url, wait_until="load", timeout=GOTO_TIMEOUT_MS)
                except PlaywrightError as e:
                    log.warning("Não foi possível abrir o URL (%s); navegue manualmente.",
                                str(e).splitlines()[0])
                continue
            self._pump(page)

        self._set("capturing", "A capturar… Não feche o browser.")
        with Recorder(self.out, keep_last_only=p.keep_last) as rec:
            with self._lock:
                self._recorder, self._run_dir = rec, rec.dir
            log.info("A gravar em %s", rec.dir)
            try:
                runner = Runner(context, rec, p.opts, stop_event=self._stop)
                with self._lock:
                    self._runner = runner
                runner.run()
            except BrowserClosedError:
                log.warning("O browser foi fechado; a terminar.")
            except (PlaywrightError, RuntimeError) as e:
                log.error("Erro durante a captura: %s", str(e).splitlines()[0])
                self._set("error", f"Erro durante a captura. Gravadas {rec.count} páginas.")
                return
            finally:
                log.info("Gravadas %d páginas em %s", rec.count, rec.dir)
        self._review(context, f"Captura terminada: {rec.count} páginas em {rec.dir.name}.")

    @staticmethod
    def _pump(page) -> None:
        """Espera curta que também processa os eventos do browser (popups, fecho)."""
        try:
            page.wait_for_timeout(300)
        except PlaywrightError as e:
            log.debug("Espera interrompida: %s", e)

    def _review(self, context, summary: str) -> None:
        """Deixa o browser aberto para o utilizador sair da lição (o LMS regista a conclusão)."""
        self._set("review", f"{summary} Para o LMS registar a conclusão, saia da lição pelo "
                            "botão do conteúdo ou do LMS (ex. «Sair») e depois carregue em "
                            "«Fechar browser».")
        log.info("Captura terminada. Saia da lição no browser e depois carregue em "
                 "«Fechar browser».")
        while not self._close.is_set():
            page = wait_for_new_page(context)
            if page is None:
                self._set("done", f"{summary} Browser fechado.")
                return
            self._pump(page)


class Handler(BaseHTTPRequestHandler):
    server: UIServer
    server_version = "page-capture"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:
        pass  # pedidos da própria interface (polling) não entram no registo

    # --- respostas ---------------------------------------------------------------------------
    def _headers(self, status: int, ctype: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _host_ok(self) -> bool:
        # defesa contra DNS rebinding: só aceita os nomes locais desta porta
        return self.headers.get("Host", "") in self.server.allowed_hosts

    # --- GET ---------------------------------------------------------------------------------
    def do_GET(self) -> None:
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "host não permitido")
        path, _, query = self.path.partition("?")
        ctrl = self.server.controller
        if path == "/":
            body = self.server.page
            self._headers(HTTPStatus.OK, "text/html; charset=utf-8", len(body))
            self.wfile.write(body)
        elif path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
        elif path == "/api/status":
            m = re.fullmatch(r"since=(\d{1,12})", query)
            self._json(HTTPStatus.OK, ctrl.status(int(m[1]) if m else 0))
        elif m := re.fullmatch(r"/api/runs/([^/]+)", path):
            try:
                self._json(HTTPStatus.OK, {"run": m[1], "rows": ctrl.run_rows(m[1])})
            except FileNotFoundError:
                self._error(HTTPStatus.NOT_FOUND, "captura não encontrada")
        elif m := re.fullmatch(r"/files/([^/]+)/([^/]+)", path):
            try:
                file = ctrl.file_path(m[1], m[2])
            except FileNotFoundError:
                return self._error(HTTPStatus.NOT_FOUND, "ficheiro não encontrado")
            data = file.read_bytes()
            ctype = "image/png" if file.suffix == ".png" else "text/csv; charset=utf-8"
            self._headers(HTTPStatus.OK, ctype, len(data))
            self.wfile.write(data)
        else:
            self._error(HTTPStatus.NOT_FOUND, "não encontrado")

    # --- POST --------------------------------------------------------------------------------
    def do_POST(self) -> None:
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "host não permitido")
        # o cabeçalho próprio obriga a preflight CORS (que não é aceite) -> bloqueia CSRF
        if not secrets.compare_digest(self.headers.get("X-PC-Token", ""), self.server.token):
            return self._error(HTTPStatus.FORBIDDEN, "token inválido")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            return self._error(HTTPStatus.BAD_REQUEST, "pedido demasiado grande")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return self._error(HTTPStatus.BAD_REQUEST, "JSON inválido")

        ctrl = self.server.controller
        if m := re.fullmatch(r"/api/runs/([^/]+)/delete", self.path):
            try:
                deleted = ctrl.delete(m[1], data.get("files") if isinstance(data, dict) else None)
            except FileNotFoundError:
                return self._error(HTTPStatus.NOT_FOUND, "captura não encontrada")
            except ValueError as e:
                return self._error(HTTPStatus.BAD_REQUEST, str(e))
            except BusyError as e:
                return self._error(HTTPStatus.CONFLICT, str(e))
            return self._json(HTTPStatus.OK, {"deleted": deleted})
        try:
            if self.path == "/api/start":
                ctrl.start(parse_params(data))
            elif self.path == "/api/begin":
                ctrl.begin()
            elif self.path == "/api/stop":
                ctrl.stop()
            else:
                return self._error(HTTPStatus.NOT_FOUND, "não encontrado")
        except ValueError as e:
            return self._error(HTTPStatus.BAD_REQUEST, str(e))
        except BusyError as e:
            return self._error(HTTPStatus.CONFLICT, str(e))
        self._json(HTTPStatus.OK, ctrl.status(since=10**12))


class UIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, controller: Controller, host: str = "127.0.0.1", port: int = 8765):
        super().__init__((host, port), Handler)
        self.controller = controller
        self.token = secrets.token_urlsafe(32)
        bound = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{bound}", f"localhost:{bound}"}
        self.url = f"http://127.0.0.1:{bound}/"
        html = UI_FILE.read_text(encoding="utf-8")
        self.page = html.replace("{{TOKEN}}", self.token).encode("utf-8")

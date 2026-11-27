"""Interface web local: abre http://127.0.0.1:8765/ no browser predefinido."""

from __future__ import annotations

import argparse
import logging
import sys
import webbrowser
from pathlib import Path

from pagecapture import __version__
from pagecapture.webui import Controller, UIServer

log = logging.getLogger("pagecapture")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="capture_ui.py",
                                description="Interface web local do page-capture.")
    p.add_argument("--port", type=int, default=8765, help="porta local (default 8765)")
    p.add_argument("--out", type=Path, default=Path("capturas"), help="pasta base das capturas")
    p.add_argument("--no-open", action="store_true", help="não abrir o browser automaticamente")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)
    if not 0 < args.port < 65536:
        p.error("porta inválida")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    controller = Controller(args.out)
    try:
        server = UIServer(controller, "127.0.0.1", args.port)
    except OSError as e:
        log.error("Não foi possível usar a porta %d (%s). Já está aberto? Experimente --port.",
                  args.port, e)
        return 1
    log.info("Interface em %s  (Ctrl+C aqui para sair)", server.url)
    if not args.no_open:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.getLogger("asyncio").setLevel(logging.CRITICAL)
        log.info("A sair… as capturas já gravadas foram mantidas.")
    finally:
        controller.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

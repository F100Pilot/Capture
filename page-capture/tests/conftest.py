import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "course"


class QuietHandler(SimpleHTTPRequestHandler):
    beacons: list[str] = []  # POSTs recebidos (ex. navigator.sendBeacon ao sair da lição)

    def log_message(self, *args):
        pass

    def do_POST(self):
        QuietHandler.beacons.append(self.path)
        self.send_response(204)
        self.end_headers()


@pytest.fixture
def course_server():
    handler = functools.partial(QuietHandler, directory=str(FIXTURES))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/index.html"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def beacons():
    """POSTs recebidos pelo course_server (mesma instância do handler)."""
    QuietHandler.beacons.clear()
    return QuietHandler.beacons

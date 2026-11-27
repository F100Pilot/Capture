import http.client
import json
import os
import threading
import time

import pytest

from pagecapture.webui import Controller, UIServer, parse_params


@pytest.fixture
def ui(tmp_path):
    exe = os.environ.get("PC_BROWSER_EXECUTABLE") or None
    ctrl = Controller(tmp_path / "out", tmp_path / "profile", headless=True, executable_path=exe)
    srv = UIServer(ctrl, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    ctrl.shutdown()
    srv.server_close()


def request(srv, method, path, body=None, token=True, host=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if token:
        headers["X-PC-Token"] = srv.token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    res = conn.getresponse()
    payload = res.read()
    conn.close()
    return res.status, payload


def test_parse_params_validates():
    p = parse_params({"url": "https://lms.example/x", "channel": "msedge", "max_pages": "5"})
    assert p.channel == "msedge" and p.opts.max_pages == 5 and p.sandbox
    for bad in ({"url": "ftp://x"}, {"url": "https://x", "channel": "firefox"},
                {"url": "https://x", "change_threshold": "2"}):
        with pytest.raises(ValueError):
            parse_params(bad)


def test_security_checks(ui):
    status, body = request(ui, "GET", "/")
    assert status == 200 and ui.token.encode() in body
    assert request(ui, "GET", "/", host="evil.example:80")[0] == 403
    assert request(ui, "POST", "/api/stop", body={}, token=False)[0] == 403
    assert request(ui, "GET", "/files/..%2F..%2Fsecret/0001.png")[0] == 404
    assert request(ui, "GET", "/files/2026-01-01_000000/../index.csv")[0] == 404
    assert request(ui, "POST", "/api/start", body={"url": "javascript:alert(1)"})[0] == 400


@pytest.mark.e2e
def test_full_flow(ui, course_server):
    def state():
        return json.loads(request(ui, "GET", "/api/status")[1])

    seen_assist = []

    def wait_for(states, timeout=60):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            s = state()
            seen_assist.append(s["assist"])
            if s["state"] in states:
                return s
            time.sleep(0.3)
        raise AssertionError(f"estado final {state()['state']}, esperado {states}")

    status, _ = request(ui, "POST", "/api/start", body={
        "url": course_server, "channel": "", "manual_timeout": "3", "change_timeout": "5",
        "no_sandbox": True})
    assert status == 200
    wait_for({"login"})
    assert request(ui, "POST", "/api/start", body={"url": course_server})[0] == 409
    assert request(ui, "POST", "/api/begin", body={})[0] == 200
    s = wait_for({"review", "error"})  # captura terminada; browser aberto para sair da lição
    assert s["state"] == "review", s["message"]
    assert "saia da lição" in s["message"]
    assert request(ui, "POST", "/api/stop", body={})[0] == 200  # «Fechar browser»
    assert wait_for({"done"})["state"] == "done"
    assert s["count"] == 6 and s["runs"] == [s["run"]]
    assert any(seen_assist) and not s["assist"]  # último slide sem «avançar» -> modo assistido

    rows = json.loads(request(ui, "GET", f"/api/runs/{s['run']}")[1])["rows"]
    assert [r["trigger"] for r in rows] == ["start"] + ["click"] * 5
    status, png = request(ui, "GET", f"/files/{s['run']}/0001.png")
    assert status == 200 and png.startswith(b"\x89PNG")


@pytest.mark.e2e
def test_stop_before_begin(ui, course_server):
    assert request(ui, "POST", "/api/start", body={"url": course_server, "channel": "",
                                                    "no_sandbox": True})[0] == 200
    end = time.monotonic() + 60
    while json.loads(request(ui, "GET", "/api/status")[1])["state"] != "login":
        assert time.monotonic() < end
        time.sleep(0.3)
    assert request(ui, "POST", "/api/stop", body={})[0] == 200
    while (s := json.loads(request(ui, "GET", "/api/status")[1]))["state"] == "stopping":
        assert time.monotonic() < end
        time.sleep(0.3)
    assert s["state"] == "done" and s["runs"] == []

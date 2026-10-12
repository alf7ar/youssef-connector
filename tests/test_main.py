"""App glue: connect code, /connector/ping check, status page + /connect, and the full fake e2e (no devices, no network)."""
import base64
import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from connector import main as cm  # noqa: E402


def code(u, t):
    return base64.urlsafe_b64encode(json.dumps({"u": u, "t": t}).encode()).decode().rstrip("=")


@pytest.fixture
def ping_hub():
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("X-Connector-Token")))
            ok = self.headers.get("X-Connector-Token") == "good"
            body = b'{"ok":true}' if ok else b"{}"
            self.send_response(200 if ok else 401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()


def test_parse_connect_code():
    assert cm.parse_connect_code(code("http://127.0.0.1:1/", "tok")) == ("http://127.0.0.1:1", "tok")
    assert cm.parse_connect_code("  " + code("https://hub.invalid", "t") + "\n") == ("https://hub.invalid", "t")
    for bad in ("", "!!!", code("ftp://x", "t"), code("https://hub.invalid", ""), base64.urlsafe_b64encode(b"[]").decode()):
        with pytest.raises(ValueError):
            cm.parse_connect_code(bad)


def test_url_schemes():
    assert cm.ws_base("https://h") == "wss://h" and cm.ws_base("http://127.0.0.1:1") == "ws://127.0.0.1:1"
    assert cm.http_base("wss://h") == "https://h"


def test_check_code(ping_hub):
    url, seen = ping_hub
    assert cm.check_code(url, "good")[0] is True
    ok, msg = cm.check_code(url, "bad")
    assert ok is False and "Wrong" in msg
    assert seen[0] == ("/connector/ping", "good")
    assert cm.check_code("http://127.0.0.1:1", "good", timeout=1)[0] is False


def test_status_page_connect_flow(tmp_path, ping_hub):
    url, _ = ping_hub
    app = cm.App(tmp_path, "9.9.9", lambda *a: None)
    port = app.serve(0)
    base = f"http://127.0.0.1:{port}"
    st = json.loads(urllib.request.urlopen(base + "/status").read())
    assert st == {**st, "configured": False, "connected": False, "version": "9.9.9"}
    assert b"Youssef Connector" in urllib.request.urlopen(base + "/").read()

    def post(c, ctype="application/json", host=None):
        req = urllib.request.Request(base + "/connect", data=json.dumps({"code": c}).encode(),
                                     headers={"Content-Type": ctype, **({"Host": host} if host else {})})
        try:
            return json.loads(urllib.request.urlopen(req).read())
        except urllib.error.HTTPError as e:
            return {"http": e.code}
    assert post(code(url, "wrong"))["ok"] is False and not (tmp_path / "config.json").exists()
    assert post(code(url, "good"), ctype="text/plain") == {"http": 403}
    assert post(code(url, "good"), host="evil.example") == {"http": 403}
    assert post(code(url, "good"))["ok"] is True
    assert cm.read_config(tmp_path) == {"url": url, "token": "good"} and app.wake.is_set()
    app.server.shutdown()


def test_e2e_fake_adapters(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("YC_HOME", str(tmp_path))
    out = tmp_path / "e2e.txt"
    assert cm.main(["--e2e", "--selftest-out", str(out)]) == 0
    assert "E2E OK ended=agent_end" in out.read_text()


def test_selftest_runs_clean(tmp_path):
    """In its own process: on Windows --selftest imports the real winrt/COM modules, which other tests stub in sys.modules."""
    import subprocess
    import importlib.util
    if importlib.util.find_spec("onnxruntime") is None:   # skip, but do not import it here: an onnxruntime import in pytest can clash with winrt's DLLs on Windows (PORT_NOTES.md)
        pytest.skip("onnxruntime not installed (--selftest needs it)")
    out = tmp_path / "selftest.txt"
    root = Path(__file__).resolve().parents[1]
    p = subprocess.run([sys.executable, "-m", "connector.main", "--selftest", "--selftest-out", str(out)], cwd=root, timeout=90,
                       env={**os.environ, "YC_HOME": str(tmp_path)}, capture_output=True, text=True)
    text = out.read_text()
    assert p.returncode == 0, (p.returncode, text, p.stderr[-1500:])
    for step in ("imports", "crash-log", "com-init", "vad-model", "fake-audio-loop"):
        assert f"PASS {step}" in text, text
    assert "SELFTEST OK in" in text

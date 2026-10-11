"""update.py against a local http.server: newer -> verified -> installer launched; tampered sha -> rejected."""
import hashlib
import json
import sys
import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import update  # noqa: E402


def test_versions():
    assert update.is_newer("0.1.1", "0.1.0") and update.is_newer("v1.0.0", "0.9.9")
    assert not update.is_newer("0.1.0", "0.1.0") and not update.is_newer("0.0.9", "0.1.0")


@pytest.fixture
def site(tmp_path):
    d = tmp_path / "site"
    d.mkdir()

    class H(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), partial(H, directory=str(d)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def publish(ver, sha_override=None, manifest_sha=None):
        exe = d / f"YoussefConnector-Setup-{ver}.exe"
        exe.write_bytes(b"fake installer " + ver.encode())
        real = hashlib.sha256(exe.read_bytes()).hexdigest()
        (d / f"{exe.name}.sha256").write_text(f"{sha_override or real}  {exe.name}\n")
        m = {"version": ver, "url": f"{base}/{exe.name}"}
        if manifest_sha:
            m["sha256"] = manifest_sha
        (d / "latest.json").write_text(json.dumps(m))
        return f"{base}/latest.json"
    yield publish
    srv.shutdown()


def run(url, tmp_path, local="0.1.0"):
    launched = []
    r = update.try_update(local, tmp_path / "home", lambda *a: None, manifest_url=url, launch=launched.append,
                          tmpdir=tmp_path / "dl")
    return r, launched


def test_update_ok(site, tmp_path):
    r, launched = run(site("0.2.0"), tmp_path)
    assert r is True and launched[0].name == "YoussefConnector-Setup-0.2.0.exe"
    assert update.update_attempted_recently("0.2.0", tmp_path / "home")
    assert run(site("0.2.0"), tmp_path) == (False, [])      # loop guard: same target version within the hour


def test_not_newer(site, tmp_path):
    assert run(site("0.1.0"), tmp_path) == (False, [])


def test_tampered_sha_rejected(site, tmp_path):
    r, launched = run(site("0.2.0", sha_override="0" * 64), tmp_path)
    assert (r, launched) == (False, [])
    assert not (tmp_path / "dl" / "YoussefConnector-Setup-0.2.0.exe").exists()
    assert not update.update_attempted_recently("0.2.0", tmp_path / "home")


def test_manifest_sha_disagrees_rejected(site, tmp_path):
    assert run(site("0.2.0", manifest_sha="f" * 64), tmp_path) == (False, [])


def test_network_failure_is_quiet(tmp_path):
    assert run("http://127.0.0.1:1/latest.json", tmp_path) == (False, [])


def test_https_only(tmp_path):
    assert run("http://example.invalid/latest.json", tmp_path) == (False, [])

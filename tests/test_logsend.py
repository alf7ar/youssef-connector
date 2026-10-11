"""Send logs: redaction + tails (pure), and the real App flow: POST /sendlogs -> {"type": "logs"} on the control socket."""
import asyncio
import base64
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from connector import logsend  # noqa: E402
from connector import main as cm  # noqa: E402

TOKEN = "Zk3_synthetic-token-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


def test_redact_known_and_generic():
    cfg = {"url": "https://hub.invalid", "token": TOKEN}
    code = base64.urlsafe_b64encode(json.dumps({"u": cfg["url"], "t": TOKEN}, separators=(",", ":")).encode()).decode().rstrip("=")
    text = (f"2026-01-01 connect with {TOKEN}\nX-Connector-Token: abc123\n{{'token': 'short1'}}\nAuthorization: Bearer abcdefgh12345678\n"
            f"code {code}\nrandom opaque {'Q' * 40}\nfine line: phone ready ok\n")
    out = logsend.redact(text, [cfg["token"]])
    for leak in (TOKEN, "abc123", "short1", "abcdefgh12345678", code, "Q" * 40):
        assert leak not in out, leak
    assert "fine line: phone ready ok" in out


def test_collect_tails_both_files(tmp_path):
    (tmp_path / "connector.log").write_text("OLD\n" * 200_000 + f"last line token={TOKEN}\n", encoding="utf-8")
    (tmp_path / "crash.log").write_text("=== run ===\nFatal Python error\n", encoding="utf-8")
    text = logsend.collect(tmp_path, {"token": TOKEN, "url": "https://hub.invalid"})
    assert "===== connector.log" in text and "===== crash.log" in text and "Fatal Python error" in text
    assert "last line" in text and TOKEN not in text
    assert len(text.encode()) < 2 * logsend.TAIL + 1000          # each file is capped at the tail size
    assert "(missing or empty)" in logsend.collect(tmp_path / "nope")


def test_send_logs_over_control_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("YC_FAKE", "1")
    (tmp_path / "connector.log").write_text(f"hello log\nheader X-Connector-Token: {TOKEN}\n", encoding="utf-8")

    async def go():
        mh = cm._mock_hub_module()
        hub = mh.MockHub(token=TOKEN)
        await hub.start()
        app = cm.App(tmp_path, "9.9.9", lambda *a: None)
        port = app.serve(0)
        app.cfg = {"url": hub.url, "token": TOKEN}
        runner = asyncio.create_task(app.run(False))

        def post():
            req = urllib.request.Request(f"http://127.0.0.1:{port}/sendlogs", data=b"{}", headers={"Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=20).read())
        try:
            r0 = await asyncio.to_thread(post)              # not connected yet (or racing): either way a clean answer
            await hub.wait_msg("hello", timeout=20)
            for _ in range(50):
                r = await asyncio.to_thread(post)
                if r["ok"]:
                    break
                await asyncio.sleep(0.1)
            assert r["ok"] is True, (r0, r)
            m = await hub.wait_msg("logs", timeout=10)
            assert "hello log" in m["text"] and TOKEN not in m["text"]
            req = urllib.request.Request(f"http://127.0.0.1:{port}/sendlogs", data=b"{}", headers={"Content-Type": "text/plain"})
            try:
                urllib.request.urlopen(req)
                assert False, "text/plain must be refused"
            except urllib.error.HTTPError as e:
                assert e.code == 403
        finally:
            app.stop_event.set()
            await asyncio.wait_for(runner, 10)
            app.server.shutdown()
            await hub.stop()
    asyncio.run(go())


def test_send_logs_offline(tmp_path):
    app = cm.App(tmp_path, "9.9.9", lambda *a: None)
    ok, msg = app.send_logs()
    assert ok is False and "Not connected" in msg


def test_status_has_arabic_reason_and_warnings(tmp_path):
    class P:
        def ready(self):
            return {"ok": False, "reason": "bluetooth_off", "warnings": []}

    class C:
        _ws = object()
        _call = None
    app = cm.App(tmp_path, "9.9.9", lambda *a: None)
    app.phone, app.conn = P(), C()
    r = app.status()["ready"]
    assert "البلوتوث" in r["reason_ar"] and r["reason_en"]
    assert b"\xd8\xa5\xd8\xb1\xd8\xb3\xd8\xa7\xd9\x84" in cm.PAGE.encode()    # the page has the Arabic send-logs label


def test_ready_failed_fallback_has_arabic(tmp_path):
    from connector import core
    from connector.fakes import FakeAudio, FakeGate, FakePhone

    class Boom(FakePhone):
        def ready(self):
            raise RuntimeError("x")

    async def go():
        mh = cm._mock_hub_module()
        hub = mh.MockHub(token="t")
        await hub.start()
        clock = core.Clock(speed=20.0)
        conn = core.Connector(hub.url, "t", Boom("answer", clock=clock), FakeAudio(clock=clock), FakeGate(True), clock=clock)
        task = asyncio.create_task(conn.run())
        try:
            hb = await hub.wait_msg("hb", timeout=10)
        finally:
            conn.stop()
            task.cancel()
            await hub.stop()
        return hb["ready"]
    r = asyncio.run(go())
    assert r["ok"] is False and r["reason"].startswith("ready() failed") and "الجاهزية" in r["reason_ar"] and r["reason_en"]

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


def test_send_logs_refused_during_call(tmp_path):
    class Call:
        id = "c1"
        reason = None       # an active call: reason is set only once it has ended

    class C:
        _ws = object()
        _call = Call()
    app = cm.App(tmp_path, "9.9.9", lambda *a: None)
    app.conn = C()
    app.loop = None         # would otherwise fail as "Not connected": the call check must come first
    (tmp_path / "connector.log").write_text("hello\n", encoding="utf-8")
    ok, msg = app.send_logs()
    assert ok is False and "call" in msg and "مكالمة" in msg
    C._call.reason = "agent_end"    # call ended: the same request is no longer refused by the call check
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


def test_crash_pending_reads_only_the_previous_run_section(tmp_path):
    hdr = "\n=== run 2026-10-12 06:00:00 pid 1 ===\n"
    p = tmp_path / "crash.log"
    assert logsend.crash_pending(tmp_path) is False                       # no file: first run ever
    p.write_text(hdr, encoding="utf-8")
    assert logsend.crash_pending(tmp_path) is False                       # header only: clean previous run
    p.write_text(hdr + hdr, encoding="utf-8")
    assert logsend.crash_pending(tmp_path) is False                       # two clean runs in a row
    p.write_text(hdr + "Fatal Python error: Segmentation fault\n" + hdr, encoding="utf-8")
    assert logsend.crash_pending(tmp_path) is False                       # the crashed run was already reported at its next start
    p.write_text(hdr + "Fatal Python error: Segmentation fault\nFile x.py line 1\n", encoding="utf-8")
    assert logsend.crash_pending(tmp_path) is True                        # the last run wrote a stack: report it now
    p.write_text("no header at all, just junk\n", encoding="utf-8")
    assert logsend.crash_pending(tmp_path) is True                        # unknown content counts as a crash


def test_crash_logs_sent_once_on_connect_after_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("YC_FAKE", "1")
    (tmp_path / "connector.log").write_text(f"earlier line token={TOKEN}\n", encoding="utf-8")
    (tmp_path / "crash.log").write_text("\n=== run x ===\nFatal Python error: Segmentation fault\n", encoding="utf-8")
    assert logsend.crash_pending(tmp_path) is True

    async def go():
        mh = cm._mock_hub_module()
        hub = mh.MockHub(token=TOKEN)
        await hub.start()
        app = cm.App(tmp_path, "9.9.9", lambda *a: None)
        app.crash_pending = True
        app.cfg = {"url": hub.url, "token": TOKEN}
        runner = asyncio.create_task(app.run(False))
        try:
            m = await hub.wait_msg("logs", timeout=20)                  # no POST /sendlogs: it goes up by itself
            assert "Fatal Python error" in m["text"] and "earlier line" in m["text"] and TOKEN not in m["text"]
            for _ in range(50):
                if app.crash_pending is False:
                    break
                await asyncio.sleep(0.1)
            assert app.crash_pending is False
        finally:
            app.stop_event.set()
            await asyncio.wait_for(runner, 10)
            await hub.stop()
    asyncio.run(go())


def _core_rig(hub, collect_logs):
    from connector import core
    from connector.fakes import FakeAudio, FakeGate, FakePhone
    clock = core.Clock(speed=20.0)
    conn = core.Connector(hub.url, TOKEN, FakePhone("answer", clock=clock), FakeAudio(clock=clock), FakeGate(True), clock=clock,
                          collect_logs=collect_logs)
    return conn, asyncio.create_task(conn.run())


def test_hub_send_logs_request_uploads_redacted_logs(tmp_path):
    (tmp_path / "connector.log").write_text(f"hub asked for logs\nheader X-Connector-Token: {TOKEN}\n", encoding="utf-8")

    async def go():
        mh = cm._mock_hub_module()
        hub = mh.MockHub(token=TOKEN)
        await hub.start()
        conn, task = _core_rig(hub, lambda: logsend.collect(tmp_path, {"token": TOKEN, "url": hub.url}))
        try:
            await hub.wait_msg("hello", timeout=10)
            await hub._send(type="send_logs")
            m = await hub.wait_msg("logs", timeout=10)
            assert "hub asked for logs" in m["text"] and TOKEN not in m["text"]
        finally:
            conn.stop()
            task.cancel()
            await hub.stop()
    asyncio.run(go())


def test_hub_send_logs_request_during_call_replies_busy(tmp_path):
    from connector import core

    async def go():
        mh = cm._mock_hub_module()
        hub = mh.MockHub(token=TOKEN)
        await hub.start()
        uploads = []
        conn, task = _core_rig(hub, lambda: uploads.append(1) or "LOGS")
        try:
            await hub.wait_msg("hello", timeout=10)
            conn._call = core._Call("c1", asyncio.get_running_loop())     # a live call: reason stays None until it ends
            await hub._send(type="send_logs")
            await hub.wait_msg("logs_busy", timeout=10)
            assert uploads == [] and not any(t == "logs" for t, _, _ in hub.msgs)
            conn._call.finish("agent_end")                                 # call over: the same request now uploads
            await hub._send(type="send_logs")
            m = await hub.wait_msg("logs", timeout=10)
            assert m["text"] == "LOGS" and uploads == [1]
        finally:
            conn.stop()
            task.cancel()
            await hub.stop()
    asyncio.run(go())


def test_send_logs_request_ignored_without_collector():
    async def go():
        mh = cm._mock_hub_module()
        hub = mh.MockHub(token=TOKEN)
        await hub.start()
        conn, task = _core_rig(hub, None)
        try:
            await hub.wait_msg("hello", timeout=10)
            await hub._send(type="send_logs")
            await asyncio.sleep(0.5)
            assert not any(t in ("logs", "logs_busy") for t, _, _ in hub.msgs)
        finally:
            conn.stop()
            task.cancel()
            await hub.stop()
    asyncio.run(go())

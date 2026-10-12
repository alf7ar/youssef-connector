"""Connector core vs the protocol mock hub (tests/mock_hub.py) with fake phone/audio/gate, on a sped-up clock.
Run: python -m pytest -q tests/test_core.py"""
import asyncio
import contextlib
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from connector import core as cc  # noqa: E402
from connector.fakes import FakeAudio, FakeGate, FakePhone  # noqa: E402
from mock_hub import MockHub  # noqa: E402

SPEED = 20.0


def tone(seconds, rate=24000, amp=8000, hz=300):
    k = np.arange(int(seconds * rate))
    return (amp * np.sin(2 * np.pi * hz * k / rate)).astype(np.int16).tobytes()


def rms(pcm):
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


class RecClock(cc.Clock):
    def __init__(self, speed=SPEED):
        super().__init__(speed)
        self.sleeps = []

    async def sleep(self, s):
        self.sleeps.append(s)
        await super().sleep(s)


@contextlib.asynccontextmanager
async def rig(scenario="answer", gate=None, hub=None, token="tok", conn_token=None, clock=None, audio=None, phone=None,
              **phone_kw):
    clock = clock or RecClock()
    hub = hub or MockHub(token=token)
    await hub.start()
    phone = phone or FakePhone(scenario, clock=clock, **phone_kw)
    audio = audio or FakeAudio(clock=clock)
    gate = gate or FakeGate(True)
    conn = cc.Connector(hub.url, conn_token or token, phone, audio, gate, clock=clock, host="testhost")
    task = asyncio.create_task(conn.run())
    r = type("R", (), {})()
    r.hub, r.phone, r.audio, r.gate, r.conn, r.clock, r.task = hub, phone, audio, gate, conn, clock, task
    try:
        yield r
    finally:
        conn.stop()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        await hub.stop()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


async def place(r, phone="01012345678", cid="c1"):
    await r.hub.wait_msg("hello")
    return await r.hub.dial(phone, cid)


# ---- scenarios ------------------------------------------------------------------------------------------------------
def test_answer_then_agent_end():
    async def go():
        async def script(call):
            await call.send_audio(tone(1.0), pace_s=0.001)
            await call.send(type="status", state="speaking")
            await call.send(type="transcript", who="agent", delta="hi")
            await asyncio.sleep(0.3)
            await call.end("agent_end")
        hub = MockHub(on_call=script)
        async with rig("answer", hub=hub) as r:
            await place(r)
            ended = await hub.wait_msg("ended", "c1")
            assert ended["reason"] == "agent_end" and ended["talk_s"] > 0
            types = [t for t, _, _ in hub.msgs]
            assert types[:2] == ["hello", "hb"]
            for a, b in (("dial_ack", "ringing"), ("ringing", "answered"), ("answered", "ended")):
                assert types.index(a) < types.index(b)
            assert hub.of_type("hello")[0]["host"] == "testhost" and hub.of_type("hello")[0]["audio_route"] == "hf_direct"
            assert hub.of_type("dial_ack")[0]["ok"] is True
            assert r.phone.dialed == ["01012345678"] and r.phone.hangups == 1
            call = hub.calls["c1"]
            assert call.audio and all(len(f) == 1920 for f in call.audio)
            # 1 s of 24 kHz tone in -> ~1 s of 16 kHz out (32000 B)
            fed = bytes(r.audio.players[0].fed)
            assert abs(len(fed) - 32000) < 1500
            assert r.audio.players[0].stopped and r.audio.sources[0]._stop.is_set()
    run(go())


def test_preroll_sent_first():
    async def go():
        async def script(call):
            await asyncio.sleep(0.5)
            await call.end()
        hub = MockHub(on_call=script)
        async with rig("answer", hub=hub, gate=FakeGate(True, consume=40)) as r:
            await place(r)
            await hub.wait_msg("ended", "c1")
            # gate consumed 40 frames (0.8 s) incl. 0.3 s silence: first out frames carry the pre-roll, >= 0.8 s of audio
            total = sum(len(f) for f in hub.calls["c1"].audio)
            assert total >= int(0.8 * 24000 * 2) - 1920 * 2
    run(go())


def test_no_answer_rings_forever_90s():
    async def go():
        async with rig("no_answer") as r:
            await place(r)
            ended = await r.hub.wait_msg("ended", "c1")
            assert ended["reason"] == "no_answer" and ended["talk_s"] == 0
            assert r.hub.of_type("ringing") and not r.hub.of_type("answered")
            assert r.phone.hangups == 1 and "c1" not in r.hub.calls
    run(go())


def test_no_answer_gate_false():
    async def go():
        async with rig("answer", gate=FakeGate(False)) as r:
            await place(r)
            ended = await r.hub.wait_msg("ended", "c1")
            assert ended["reason"] == "no_answer" and r.phone.hangups == 1 and not r.hub.of_type("answered")
    run(go())


def test_busy():
    async def go():
        async with rig("busy") as r:
            await place(r)
            ended = await r.hub.wait_msg("ended", "c1")
            assert ended["reason"] == "busy" and r.phone.hangups == 0 and "c1" not in r.hub.calls
    run(go())


def test_dial_failed():
    async def go():
        async with rig("answer", dial_ok=False) as r:
            await place(r)
            ack = await r.hub.wait_msg("dial_ack", "c1")
            ended = await r.hub.wait_msg("ended", "c1")
            assert ack["ok"] is False and ack["error"] and ended["reason"] == "dial_failed"
    run(go())


def test_hangup_mid_callee_hangup_closes_call_socket():
    async def go():
        async with rig("hangup_mid", after_s=2.0) as r:
            await place(r)
            ended = await r.hub.wait_msg("ended", "c1")
            assert ended["reason"] == "callee_hangup" and ended["talk_s"] > 0
            call = await r.hub.wait_call("c1")
            await asyncio.wait_for(call.closed.wait(), 5)
            assert r.phone.hangups == 0
    run(go())


def test_drop_call_ended():
    async def go():
        async with rig("drop", after_s=2.0) as r:
            await place(r)
            assert (await r.hub.wait_msg("ended", "c1"))["reason"] == "call_ended"
    run(go())


def test_end_call_scenario_hangs_up_phone():
    async def go():
        async def script(call):
            await asyncio.sleep(0.2)
            await call.end()
        async with rig("end_call", hub=MockHub(on_call=script)) as r:
            await place(r)
            assert (await r.hub.wait_msg("ended", "c1"))["reason"] == "agent_end"
            assert r.phone.hangups == 1
    run(go())


def test_hangup_command():
    async def go():
        async with rig("answer") as r:
            await place(r)
            await r.hub.wait_msg("answered", "c1")
            await r.hub.wait_call("c1")
            await r.hub.hangup("c1")
            assert (await r.hub.wait_msg("ended", "c1"))["reason"] == "hangup_cmd"
            assert r.phone.hangups == 1
    run(go())


def test_hangup_command_while_ringing():
    async def go():
        async with rig("no_answer") as r:
            await place(r)
            await r.hub.wait_msg("ringing", "c1")
            await r.hub.hangup("c1")
            assert (await r.hub.wait_msg("ended", "c1"))["reason"] == "hangup_cmd"
    run(go())


def test_call_socket_dropped_mid_call_audio_error():
    async def go():
        async def script(call):
            await asyncio.sleep(0.2)
            await call.drop()
        async with rig("answer", hub=MockHub(on_call=script)) as r:
            await place(r)
            assert (await r.hub.wait_msg("ended", "c1"))["reason"] == "audio_error"
            assert r.phone.hangups == 1
    run(go())


def test_audio_source_dies_audio_error():
    async def go():
        async with rig("answer") as r:
            await place(r)
            await r.hub.wait_call("c1")
            r.audio.sources[0].kill()
            assert (await r.hub.wait_msg("ended", "c1"))["reason"] == "audio_error"
    run(go())


def test_second_dial_while_busy_rejected():
    async def go():
        async with rig("answer") as r:
            await place(r)
            await r.hub.wait_msg("answered", "c1")
            await r.hub.dial("+2010", "c2")
            ack = await r.hub.wait_msg("dial_ack", "c2")
            assert ack["ok"] is False and r.phone.dialed == ["01012345678"]
    run(go())


# ---- ordering / audio gating ----------------------------------------------------------------------------------------
def test_no_call_socket_or_audio_before_answered_ack():
    async def go():
        hub = MockHub(auto_ack=False)
        async with rig("answer", hub=hub) as r:
            await place(r)
            await hub.wait_msg("answered", "c1")
            await asyncio.sleep(0.2)                       # real time (< ACK_TIMEOUT/SPEED = 0.5 s): connector must still be waiting
            assert hub.early_call_attempts == 0 and "c1" not in hub.calls
            assert not any(k in ("call_open", "audio", "call_rejected") for k, _ in hub.log)
            await hub.ack("c1")
            call = await hub.wait_call("c1")
            await asyncio.sleep(0.3)
            assert call.audio
            kinds = [k for k, _ in hub.log]
            assert kinds.index("ctl:answered") < kinds.index("ack_sent") < kinds.index("call_open") < kinds.index("audio")
            assert hub.early_call_attempts == 0
    run(go())


def test_never_audio_before_answered_when_gate_never_true():
    async def go():
        async with rig("answer", gate=FakeGate(False)) as r:
            await place(r)
            await r.hub.wait_msg("ended", "c1")
            assert not any(k in ("call_open", "audio") for k, _ in r.hub.log)
    run(go())


def test_ack_timeout_audio_error():
    async def go():
        hub = MockHub(auto_ack=False)
        async with rig("answer", hub=hub) as r:
            await place(r)
            ended = await hub.wait_msg("ended", "c1", timeout=20)
            assert ended["reason"] == "audio_error" and "c1" not in hub.calls and r.phone.hangups == 1
    run(go())


# ---- flush / end / drain --------------------------------------------------------------------------------------------
def test_flush_resets_player_and_resampler():
    async def go():
        async def script(call):
            await call.send_audio(tone(0.5), pace_s=0.001)
            await asyncio.sleep(0.1)
            await call.flush()
            await asyncio.sleep(0.1)
            await call.send_audio(tone(0.5), pace_s=0.001)
            await asyncio.sleep(0.2)
            await call.end()
        async with rig("answer", hub=MockHub(on_call=script)) as r:
            await place(r)
            assert (await r.hub.wait_msg("ended", "c1"))["reason"] == "agent_end"
            assert r.audio.players[0].flushes == 1
            assert len(r.audio.players[0].fed) > 20000
    run(go())


def test_end_waits_for_drain():
    async def go():
        clock = RecClock()
        hub = MockHub()

        async def on_call(call):
            await call.send_audio(tone(5.0))   # ~5 s of audio queued in the player at once
            call.t_end = clock.now()
            await call.end()
        hub.on_call = on_call
        async with rig("answer", hub=hub, clock=clock) as r:
            await place(r)
            ended = await hub.wait_msg("ended", "c1")
            t_ended = clock.now()
            call = hub.calls["c1"]
            waited = t_ended - call.t_end
            assert ended["reason"] == "agent_end" and 2.0 < waited < cc.DRAIN_MAX - 0.5
    run(go())


def test_end_drain_timeout_8s():
    async def go():
        clock = RecClock()
        hub = MockHub()

        async def on_call(call):
            await asyncio.sleep(0.05)
            call.t_end = clock.now()
            await call.end()
        hub.on_call = on_call
        async with rig("answer", hub=hub, clock=clock, audio=FakeAudio(clock=clock, stuck_player=True)) as r:
            await place(r)
            ended = await hub.wait_msg("ended", "c1")
            waited = clock.now() - hub.calls["c1"].t_end
            assert ended["reason"] == "agent_end" and cc.DRAIN_MAX - 0.5 <= waited < cc.DRAIN_MAX + 3
            assert r.phone.hangups == 1
    run(go())


# ---- resampling -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("a,b,seconds", [(16000, 24000, 1.0), (24000, 16000, 1.0)])
def test_resample_length_and_rms(a, b, seconds):
    src = tone(seconds, rate=a, hz=440)
    rs = cc.Resampler(a, b)
    out = b"".join(rs.process(src[i:i + 641]) for i in range(0, len(src), 641))   # odd-sized chunks
    want = int(seconds * b) * 2
    assert abs(len(out) - want) <= 0.03 * want
    assert abs(rms(out) - rms(src)) <= 0.06 * rms(src)


def test_resampler_reset_and_empty():
    rs = cc.Resampler(24000, 16000)
    assert rs.process(b"") == b"" and rs.process(b"\x01") == b""
    rs.reset()
    assert len(rs.process(tone(0.5))) > 0


# ---- control client -------------------------------------------------------------------------------------------------
def test_reconnect_backoff_sequence():
    async def go():
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                           # nothing listens here
        clock = RecClock(speed=1000)
        conn = cc.Connector(f"ws://127.0.0.1:{port}", "tok", FakePhone(clock=clock), FakeAudio(clock=clock), FakeGate(),
                            clock=clock)
        orig = clock.sleep

        async def sleep(s):
            await orig(s)
            if len(clock.sleeps) >= 8:
                conn.stop()
        clock.sleep = sleep
        await conn.run()
        assert clock.sleeps[:7] == [1, 2, 4, 8, 16, 30, 30]
    run(go())


def test_backoff_resets_after_welcome():
    async def go():
        clock = RecClock(speed=1000)
        hub = MockHub()
        async with rig("answer", hub=hub, clock=clock) as r:
            await hub.wait_msg("hello")
            await hub.control.close(1011)
            await asyncio.sleep(0.3)
            await hub.wait_msg("hello")
            assert [s for s in clock.sleeps if s in cc.BACKOFF] == [1]   # one reconnect, backoff restarted at 1 s
    run(go())


def test_hb_cadence_and_payload():
    async def go():
        clock = RecClock(speed=100)
        async with rig("answer", clock=clock) as r:
            await r.hub.wait_msg("hello")
            for _ in range(200):
                if len(r.hub.of_type("hb")) >= 4:
                    break
                await asyncio.sleep(0.02)
            hbs = r.hub.of_type("hb")
            assert len(hbs) >= 4
            assert set(hbs[0]["ready"]) >= {"ok", "reason", "bt_radio", "iphone_connected", "hfp_audio_device",
                                            "phone_link_running", "session_locked", "call_active", "hf_link"}
            assert clock.sleeps.count(10.0) >= 3 and set(clock.sleeps) <= {10.0, 0.25}
            times = [t for ty, _, t in r.hub.msgs if ty == "hb"]
            gaps = [(b - a) * clock.speed for a, b in zip(times, times[1:])]
            assert all(7 < g < 14 for g in gaps), gaps
    run(go())


def test_auth_header_sent_on_both_sockets():
    async def go():
        async with rig("answer", token="s3cret") as r:
            await place(r)
            await r.hub.wait_msg("answered", "c1")
            await r.hub.wait_call("c1")
            assert r.hub.headers_seen[:2] == ["s3cret", "s3cret"] and set(r.hub.headers_seen) == {"s3cret"}
    run(go())


def test_bad_token_rejected_4401_retries_every_5_min_not_tight():
    async def go():
        hub = MockHub(token="good")
        async with rig("answer", hub=hub, conn_token="old", clock=RecClock(speed=2000)) as r:
            for _ in range(200):
                if len(hub.headers_seen) >= 3:
                    break
                await asyncio.sleep(0.02)
            assert len(hub.headers_seen) >= 3 and set(hub.headers_seen) == {"old"}
            assert not hub.msgs and r.conn.stop_reason is None and r.conn.token_rejected
            assert cc.TOKEN_RETRY == 300.0 and set(r.clock.sleeps) == {cc.TOKEN_RETRY}   # never the 1..30 s backoff
    run(go())


def test_welcome_min_version_stops_connector():
    async def go():
        hub = MockHub(min_version="9.0.0")
        async with rig("answer", hub=hub) as r:
            await asyncio.wait_for(r.task, 5)
            assert r.conn.stop_reason == "outdated"
    run(go())


def test_replaced_once_waits_60s_and_reconnects():
    async def go():
        hub = MockHub()
        async with rig("answer", hub=hub, clock=RecClock(speed=200)) as r1:
            await hub.wait_msg("hello")
            clock2 = RecClock()
            c2 = cc.Connector(hub.url, "tok", FakePhone(clock=clock2), FakeAudio(clock=clock2), FakeGate(), clock=clock2)
            t2 = asyncio.create_task(c2.run())
            await asyncio.sleep(0.2)
            c2.stop()
            t2.cancel()
            with contextlib.suppress(BaseException):
                await t2
            for _ in range(100):
                if cc.REPLACED_WAIT in r1.clock.sleeps and len([1 for t, *_ in hub.msgs if t == "hello"]) >= 2:
                    break
                await asyncio.sleep(0.05)
            assert cc.REPLACED_WAIT == 60.0 and cc.REPLACED_WAIT in r1.clock.sleeps
            assert r1.conn.stop_reason is None and not r1.task.done()
    run(go())


def test_replaced_three_times_in_10_min_stops():
    async def go():
        hub = MockHub()
        async with rig("answer", hub=hub, clock=RecClock(speed=200)) as r1:
            await hub.wait_msg("hello")
            clock2 = RecClock(speed=200)
            c2 = cc.Connector(hub.url, "tok", FakePhone(clock=clock2), FakeAudio(clock=clock2), FakeGate(), clock=clock2)
            t2 = asyncio.create_task(c2.run())      # two connectors that keep replacing each other
            await asyncio.wait_for(asyncio.wait({r1.task, t2}, return_when=asyncio.FIRST_COMPLETED), 20)
            stopped = [c for c in (r1.conn, c2) if c.stop_reason == "replaced"]
            assert stopped, (r1.conn.stop_reason, c2.stop_reason)
            assert len(stopped[0]._replaced_at) == cc.REPLACED_MAX == 3
            c2.stop()
            t2.cancel()
            with contextlib.suppress(BaseException):
                await t2
    run(go())


def test_control_loss_mid_call_hangs_up():
    async def go():
        async with rig("answer") as r:
            await place(r)
            await r.hub.wait_call("c1")
            await r.hub.control.close(1011)
            for _ in range(200):
                if r.phone.hangups:
                    break
                await asyncio.sleep(0.02)
            assert r.phone.hangups == 1
    run(go())


def test_blackholed_network_mid_call_hangs_up_within_ping_timeout(monkeypatch):
    """A network that silently stops answering (no close, no reset) must still end the call locally: the hub gives up on a
    silent connector after 30 s, so the connector's own keepalive has to fire first or the phone line stays up."""
    monkeypatch.setattr(cc, "WS_PING_INTERVAL", 0.3, raising=False)
    monkeypatch.setattr(cc, "WS_PING_TIMEOUT", 0.3, raising=False)
    monkeypatch.setattr(cc, "WS_CLOSE_TIMEOUT", 0.3, raising=False)

    async def go():
        async with rig("answer") as r:
            await place(r)
            call = await r.hub.wait_call("c1")
            r.hub.control.transport.pause_reading()   # the hub side stops reading: pings go unanswered
            call.ws.transport.pause_reading()
            for _ in range(300):
                if r.phone.hangups:
                    break
                await asyncio.sleep(0.02)
            assert r.phone.hangups == 1
            await asyncio.sleep(0.3)
            assert r.audio.sources and all(s._stop.is_set() for s in r.audio.sources)
    run(go())


# ---- server-pushed config (welcome["config"]) ----------------------------------------------------------------------
CFG = {"heal": {"radio_cycle": False, "adapter_reset": False, "max_adapter_resets_per_hour": 2,
                "max_radio_toggles_per_hour": 4}, "ns": "off", "in_gain": 1.5}


async def _started(hub):
    await hub.start()
    return hub


def _conn(hub, clock, **kw):
    return cc.Connector(hub.url, "tok", FakePhone(clock=clock), FakeAudio(clock=clock), FakeGate(True), clock=clock,
                        host="testhost", **kw)


async def _until_true(pred, timeout=10.0):
    t0 = asyncio.get_running_loop().time()
    while not pred():
        assert asyncio.get_running_loop().time() - t0 < timeout
        await asyncio.sleep(0.02)


@contextlib.asynccontextmanager
async def _running(conn, hub):
    task = asyncio.create_task(conn.run())
    try:
        yield
    finally:
        conn.stop()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        await hub.stop()


def test_welcome_config_kept_persisted_and_callbacks(tmp_path):
    async def go():
        import json
        hub = await _started(MockHub(config=CFG))
        conn = _conn(hub, RecClock(), home=tmp_path)
        got = []
        conn.on_config(got.append)
        async with _running(conn, hub):
            await _until_true(lambda: got)
        assert got[0] == CFG and conn.remote_config == CFG
        assert json.loads((tmp_path / "remote_config.json").read_text()) == CFG
        got[0]["ns"] = "x"                                   # callbacks get a copy
        assert conn.remote_config["ns"] == "off"
    run(go())


def test_config_loaded_at_start_replayed_and_server_wins(tmp_path):
    async def go():
        import json
        (tmp_path / "remote_config.json").write_text(json.dumps({"ns": "rnnoise"}))
        hub = await _started(MockHub(config=CFG))
        conn = _conn(hub, RecClock(), home=tmp_path)
        assert conn.remote_config == {"ns": "rnnoise"}      # available before any welcome
        got = []
        conn.on_config(got.append)
        assert got == [{"ns": "rnnoise"}]                    # replayed on registration
        async with _running(conn, hub):
            await _until_true(lambda: len(got) == 2)
        assert got[1] == CFG and json.loads((tmp_path / "remote_config.json").read_text()) == CFG
    run(go())


def test_welcome_without_config_changes_nothing(tmp_path):
    async def go():
        hub = await _started(MockHub())
        conn = _conn(hub, RecClock(), home=tmp_path)
        got = []
        conn.on_config(got.append)
        async with _running(conn, hub):
            await hub.wait_msg("hello")
            await _until_true(lambda: conn._welcomed)
        assert got == [] and conn.remote_config == {} and not (tmp_path / "remote_config.json").exists()
    run(go())


def test_bad_config_and_bad_callback_are_harmless(tmp_path):
    async def go():
        hub = await _started(MockHub(config=["not", "a", "dict"]))
        conn = _conn(hub, RecClock(), home=tmp_path)
        async with _running(conn, hub):
            await _until_true(lambda: conn._welcomed)
        assert conn.remote_config == {}
        hub2 = await _started(MockHub(config=CFG))
        conn2 = _conn(hub2, RecClock())                      # no home: nothing persisted
        ok = []
        conn2.on_config(lambda c: 1 / 0)
        conn2.on_config(ok.append)
        async with _running(conn2, hub2):
            await _until_true(lambda: ok)
    run(go())


def test_corrupt_remote_config_file_ignored(tmp_path):
    (tmp_path / "remote_config.json").write_text("{nope")
    assert cc.Connector("ws://127.0.0.1:1", "tok", FakePhone(), FakeAudio(), FakeGate(), home=tmp_path).remote_config == {}


# ---- DNS resilience --------------------------------------------------------------------------------------------------
def _fake_dns(monkeypatch, resolves: bool):
    """Replace core.connect: a call without host= fails like an unresolvable name (or, resolves=True, is pinned to 127.0.0.1)."""
    import socket
    calls = []
    real = cc.connect

    def fake(url, **kw):
        calls.append((url, dict(kw)))
        if "host" not in kw:
            if not resolves:
                raise socket.gaierror(-2, "Name or service not known")
            kw["host"] = "127.0.0.1"
        return real(url, **kw)
    monkeypatch.setattr(cc, "connect", fake)
    return calls


def test_dns_failure_uses_last_good_ip_keeps_hostname(monkeypatch, tmp_path):
    async def go():
        reports = []
        monkeypatch.setattr(cc, "_heal_report", lambda failure, **d: reports.append((failure, d)))
        calls = _fake_dns(monkeypatch, resolves=False)
        hub = MockHub()
        await hub.start()
        clock = RecClock()
        conn = cc.Connector(f"ws://hub.test:{hub.port}", "tok", FakePhone(clock=clock), FakeAudio(clock=clock), FakeGate(True),
                            clock=clock, host="testhost", home=tmp_path)
        conn._dns_cache["hub.test"] = "127.0.0.1"
        task = asyncio.create_task(conn.run())
        try:
            await _until_true(lambda: conn._welcomed)
            assert calls[0][1].get("host") is None and calls[1][1]["host"] == "127.0.0.1"
            assert calls[1][0].startswith("ws://hub.test:")          # URL (SNI / Host / cert name) keeps the hostname
            assert hub.hosts_seen[0] == f"hub.test:{hub.port}"
            assert reports and reports[0][0] == "net_dns" and reports[0][1]["cached_ip"] is True
        finally:
            conn.stop()
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            await hub.stop()
    run(go())


def test_dns_ip_remembered_after_good_connect_and_persisted(monkeypatch, tmp_path):
    async def go():
        import json
        _fake_dns(monkeypatch, resolves=True)
        hub = MockHub()
        await hub.start()
        clock = RecClock()
        conn = cc.Connector(f"ws://hub.test:{hub.port}", "tok", FakePhone(clock=clock), FakeAudio(clock=clock), FakeGate(True),
                            clock=clock, host="testhost", home=tmp_path)
        task = asyncio.create_task(conn.run())
        try:
            await _until_true(lambda: conn._welcomed)
            assert conn._dns_cache == {"hub.test": "127.0.0.1"}
            assert json.loads((tmp_path / "dns_cache.json").read_text()) == {"hub.test": "127.0.0.1"}
        finally:
            conn.stop()
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            await hub.stop()
        again = cc.Connector("ws://hub.test:1", "tok", FakePhone(clock=clock), FakeAudio(clock=clock), FakeGate(), home=tmp_path)
        assert again._dns_cache == {"hub.test": "127.0.0.1"}        # survives a restart
    run(go())


def test_ip_literal_url_is_not_cached(monkeypatch):
    async def go():
        hub = MockHub()
        async with rig("answer", hub=hub) as r:
            await hub.wait_msg("hello")
            assert r.conn._dns_cache == {}
    run(go())


def test_dns_failure_without_cache_backs_off_with_jitter(monkeypatch):
    async def go():
        reports = []
        monkeypatch.setattr(cc, "_heal_report", lambda failure, **d: reports.append((failure, d)))
        _fake_dns(monkeypatch, resolves=False)
        clock = RecClock(speed=1000)
        conn = cc.Connector("ws://hub.test:1", "tok", FakePhone(clock=clock), FakeAudio(clock=clock), FakeGate(), clock=clock)
        orig = clock.sleep

        async def sleep(s):
            await orig(s)
            if len(clock.sleeps) >= 6:
                conn.stop()
        clock.sleep = sleep
        await conn.run()
        lo, hi = cc.DNS_JITTER
        for got, base in zip(clock.sleeps, cc.BACKOFF):
            assert base * lo <= got <= base * hi
        assert clock.sleeps[4] != clock.sleeps[5]                    # both are capped at 30 s, jitter keeps them apart
        assert len(reports) >= 6 and all(f == "net_dns" and d["cached_ip"] is False for f, d in reports)
    run(go())


def test_non_dns_oserror_does_not_use_cache_and_keeps_plain_backoff():
    async def go():
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        clock = RecClock(speed=1000)
        conn = cc.Connector(f"ws://127.0.0.1:{port}", "tok", FakePhone(clock=clock), FakeAudio(clock=clock), FakeGate(), clock=clock)
        conn._dns_cache["127.0.0.1"] = "not-an-ip"
        orig = clock.sleep

        async def sleep(s):
            await orig(s)
            if len(clock.sleeps) >= 3:
                conn.stop()
        clock.sleep = sleep
        await conn.run()
        assert clock.sleeps[:3] == [1, 2, 4]
    run(go())


def test_heal_report_is_safe_without_heal_module():
    cc._heal_report("net_dns", host="x")


def test_dns_fallback_for_call_socket(monkeypatch):
    async def go():
        calls = _fake_dns(monkeypatch, resolves=True)        # control socket resolves (IP gets cached)...
        hub = MockHub()
        await hub.start()
        clock = RecClock()
        conn = cc.Connector(f"ws://hub.test:{hub.port}", "tok", FakePhone("answer", clock=clock), FakeAudio(clock=clock),
                            FakeGate(True), clock=clock, host="testhost")
        task = asyncio.create_task(conn.run())
        try:
            await _until_true(lambda: conn._welcomed)
            import socket

            def dead(url, **kw):                              # ...then DNS dies before the call socket opens
                calls.append((url, dict(kw)))
                if "host" not in kw:
                    raise socket.gaierror(-3, "Temporary failure in name resolution")
                return orig(url, **kw)
            orig = cc.connect
            monkeypatch.setattr(cc, "connect", dead)
            await hub.dial("01012345678", "c1")
            await hub.wait_call("c1")
            assert any("/call/c1/ws" in u and kw.get("host") == "127.0.0.1" for u, kw in calls)
        finally:
            conn.stop()
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            await hub.stop()
    run(go())

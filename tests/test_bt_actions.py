"""connector/bt_actions.py: the four Bluetooth heal actions with every Windows call stubbed, plus the readiness
bluetooth_adapter_missing reason."""
import asyncio
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import bt_actions as B  # noqa: E402
from connector import readiness  # noqa: E402

MAC = "02:00:00:00:00:01"


class Phone:
    """Stand-in for connector.phone."""
    flags = {"call_active": False}
    radio = {"present": True, "on": True, "error": None}
    calls = []

    @classmethod
    def call_flags(cls):
        return dict(cls.flags)

    @classmethod
    async def bt_radio(cls, turn_on=False):
        cls.calls.append(turn_on)
        return dict(cls.radio)

    @staticmethod
    async def find_iphone_mac():
        return MAC


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    P = type("P", (Phone,), {"flags": {"call_active": False}, "radio": dict(Phone.radio), "calls": []})
    monkeypatch.setattr(B, "_phone", lambda: P)
    monkeypatch.setattr(readiness, "_mac", [MAC])
    monkeypatch.setattr(readiness, "_hf_thread", [None])
    monkeypatch.setattr(B, "_job", {"thread": None, "result": None, "t0": 0.0, "done": None})
    return P


# ---------------------------------------------------------------- radio_on
def test_radio_on_ok_and_requests_turn_on(stubs):
    stubs.radio = {"present": True, "on": True, "error": None, "access": "ALLOWED"}
    r = B.radio_on()
    assert r["ok"] is True and r["on"] is True and r["access"] == "ALLOWED" and stubs.calls == [True]


@pytest.mark.parametrize("radio,reason", [
    ({"present": False, "on": False, "error": None}, "no_adapter"),
    ({"present": True, "on": False, "error": None, "access": "DENIED_BY_SYSTEM"}, "access_denied_by_system"),
    ({"present": True, "on": False, "error": None, "access": "ALLOWED"}, "still_off"),
    ({"present": None, "on": None, "error": "RuntimeError: boom"}, "radio_error"),
])
def test_radio_on_failures(stubs, radio, reason):
    stubs.radio = radio
    r = B.radio_on()
    assert r["ok"] is False and r["reason"] == reason


def test_radio_on_refuses_during_call_or_unknown_state(stubs):
    for flag in (True, None):
        stubs.flags = {"call_active": flag}
        r = B.radio_on()
        assert r["ok"] is False and r["reason"] == "in_call"
    assert stubs.calls == []


def test_radio_on_exception_is_contained(stubs):
    async def boom(turn_on=False):
        raise OSError("x")
    stubs.bt_radio = staticmethod(boom)
    assert B.radio_on()["reason"] == "radio_error"


# ---------------------------------------------------------------- radio_cycle
class FakeRadio:
    def __init__(self, on=True, honor=True):
        self.on, self.honor, self.log = on, honor, []


def wire_radio(monkeypatch, radio, access="ALLOWED", set_status=("ALLOWED", "ALLOWED"), have=True):
    seq = list(set_status)
    sleeps = []

    async def radios():
        return [radio] if have else []

    async def req():
        return access

    async def setr(r, on):
        s = seq.pop(0)
        r.log.append(("on" if on else "off", s))
        if s == "ALLOWED" and r.honor:
            r.on = on
        return s

    async def asleep(secs):
        sleeps.append(secs)

    monkeypatch.setattr(B, "_bt_radios", radios)
    monkeypatch.setattr(B, "_request_access", req)
    monkeypatch.setattr(B, "_radio_is_on", lambda r: r.on)
    monkeypatch.setattr(B, "_set_radio", setr)
    monkeypatch.setattr(B, "_asleep", asleep)
    return sleeps


def test_radio_cycle_off_wait_on_verify(monkeypatch):
    r = FakeRadio()
    sleeps = wire_radio(monkeypatch, r)
    out = B.radio_cycle()
    assert out["ok"] is True and out["on"] is True and out["steps"] == ["off:ALLOWED", "on:ALLOWED"]
    assert r.log == [("off", "ALLOWED"), ("on", "ALLOWED")] and sleeps[0] == 2.0


def test_radio_cycle_not_back_on(monkeypatch):
    r = FakeRadio(on=False, honor=False)
    wire_radio(monkeypatch, r)
    out = B.radio_cycle()
    assert out["ok"] is False and out["reason"] == "still_off"


def test_radio_cycle_access_denied_never_touches_radio(monkeypatch):
    r = FakeRadio()
    wire_radio(monkeypatch, r, access="DENIED_BY_USER")
    out = B.radio_cycle()
    assert out["ok"] is False and out["reason"] == "access_denied_by_user" and r.log == []


def test_radio_cycle_off_denied_stops_without_on(monkeypatch):
    r = FakeRadio()
    wire_radio(monkeypatch, r, set_status=("DENIED_BY_SYSTEM",))
    out = B.radio_cycle()
    assert out["ok"] is False and out["reason"] == "access_denied_by_system" and r.on is True


def test_radio_cycle_no_adapter_and_call_guard(monkeypatch, stubs):
    wire_radio(monkeypatch, FakeRadio(), have=False)
    assert B.radio_cycle()["reason"] == "no_adapter"
    stubs.flags = {"call_active": True}
    assert B.radio_cycle()["reason"] == "in_call"


# ---------------------------------------------------------------- hf_refresh
def test_interpret_hf_codes():
    good = {"ok": True, "enable_rc": 0, "disable_rc": 0, "after": {"capture": 8, "render": 8}}
    assert B.interpret_hf(good)["ok"] is True
    r = B.interpret_hf({"ok": False, "disable_rc": 5, "enable_rc": 5})
    assert r["ok"] is False and r["reason"] == "needs_admin"
    assert B.interpret_hf({"ok": False, "error": "access_denied", "disable_rc": 5})["reason"] == "needs_admin"
    assert B.interpret_hf({"ok": False, "disable_rc": 87, "enable_rc": 0})["reason"] == "service_state_failed"
    assert B.interpret_hf({"ok": False, "disable_rc": 0, "enable_rc": 0})["reason"] == "endpoints_not_back"
    assert B.interpret_hf({"ok": False, "error": "no iPhone MAC"})["reason"] == "no_phone"


def test_hf_refresh_success_passes_mac(monkeypatch):
    seen = []
    monkeypatch.setattr(B, "_refresh_hf", lambda mac: seen.append(mac) or {"ok": True, "enable_rc": 0, "disable_rc": 0})
    r = B.hf_refresh()
    assert r["ok"] is True and seen == [MAC]


def test_hf_refresh_access_denied_is_needs_admin(monkeypatch):
    monkeypatch.setattr(B, "_refresh_hf", lambda mac: {"ok": False, "disable_rc": 5, "error": "access_denied"})
    r = B.hf_refresh()
    assert r["ok"] is False and r["reason"] == "needs_admin"


def test_hf_refresh_timeout_keeps_worker_and_second_call_is_in_progress(monkeypatch):
    gate = threading.Event()
    started = []

    def slow(mac):
        started.append(1)
        gate.wait(10)
        return {"ok": True}
    monkeypatch.setattr(B, "_refresh_hf", slow)
    r = B.hf_refresh(timeout=0.2)
    assert r["ok"] is False and r["reason"] == "timeout" and r["pending"] is True
    assert B.hf_refresh_state()["running"] is True
    r2 = B.hf_refresh(timeout=0.2)
    assert r2["reason"] == "in_progress" and started == [1]
    gate.set()
    B._job["thread"].join(5)
    st = B.hf_refresh_state()
    assert st["running"] is False and st["result"]["ok"] is True


def test_start_hf_refresh_is_nonblocking(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(B, "_refresh_hf", lambda mac: (gate.wait(10), {"ok": True})[1])
    st = B.start_hf_refresh(MAC)
    assert st == {"started": True, "running": True, "age": 0.0}
    assert B.start_hf_refresh(MAC)["started"] is False
    gate.set()
    B._job["thread"].join(5)


def test_hf_refresh_refuses_during_call_and_when_readiness_refresh_runs(monkeypatch, stubs):
    monkeypatch.setattr(B, "_refresh_hf", lambda mac: pytest.fail("must not run"))
    stubs.flags = {"call_active": True}
    assert B.hf_refresh()["reason"] == "in_call"
    stubs.flags = {"call_active": False}
    gate = threading.Event()
    t = threading.Thread(target=gate.wait, args=(5,), daemon=True)
    t.start()
    monkeypatch.setattr(readiness, "_hf_thread", [t])
    assert B.hf_refresh()["reason"] == "in_progress"
    gate.set()
    t.join(5)


def test_hf_refresh_no_phone(monkeypatch, stubs):
    monkeypatch.setattr(readiness, "_mac", [""])

    async def none():
        return ""
    stubs.find_iphone_mac = staticmethod(none)
    monkeypatch.setattr(B, "_refresh_hf", lambda mac: pytest.fail("must not run"))
    assert B.hf_refresh()["reason"] == "no_phone"


def test_hf_refresh_worker_exception_contained(monkeypatch):
    def boom(mac):
        raise RuntimeError("x")
    monkeypatch.setattr(B, "_refresh_hf", boom)
    r = B.hf_refresh()
    assert r["ok"] is False and r["reason"] == "refresh_error"


# ---------------------------------------------------------------- phone_connect
class FakeDev:
    def __init__(self, paired=True, connected=False, connect_after=None):
        self.paired, self.connected, self.connect_after, self.closed, self.probed = paired, connected, connect_after, False, 0


def wire_dev(monkeypatch, dev, probe=None):
    clock = [0.0]
    ticks = []

    class T:   # fake clock for bt_actions.time
        @staticmethod
        def monotonic():
            return clock[0]

    async def opener(mac):
        return dev

    async def probe_fn(d):
        d.probed += 1
        if probe is not None:
            raise probe
        return {"error": "SUCCESS", "services": 1}

    async def asleep(secs):
        clock[0] += secs
        ticks.append(secs)
        if dev is not None and dev.connect_after is not None and clock[0] >= dev.connect_after:
            dev.connected = True

    monkeypatch.setattr(B, "time", T)
    monkeypatch.setattr(B, "_open_device", opener)
    monkeypatch.setattr(B, "_device_paired", lambda d: d.paired)
    monkeypatch.setattr(B, "_device_connected", lambda d: d.connected)
    monkeypatch.setattr(B, "_device_close", lambda d: setattr(d, "closed", True))
    monkeypatch.setattr(B, "_rfcomm_probe", probe_fn)
    monkeypatch.setattr(B, "_asleep", asleep)
    return clock


def test_phone_connect_already_connected_skips_probe(monkeypatch):
    d = FakeDev(connected=True)
    wire_dev(monkeypatch, d)
    r = B.phone_connect()
    assert r["ok"] is True and r["already_connected"] is True and d.probed == 0 and d.closed


def test_phone_connect_becomes_connected_after_probe(monkeypatch):
    d = FakeDev(connect_after=4.0)
    wire_dev(monkeypatch, d)
    r = B.phone_connect()
    assert r["ok"] is True and d.probed == 1 and r["rfcomm"]["services"] == 1 and r["connect_secs"] == 4.0 and d.closed


def test_phone_connect_times_out_after_15s(monkeypatch):
    d = FakeDev()
    clock = wire_dev(monkeypatch, d)
    r = B.phone_connect()
    assert r["ok"] is False and r["reason"] == "not_connected" and clock[0] == 15.0 and d.closed


def test_phone_connect_probe_error_still_waits(monkeypatch):
    d = FakeDev(connect_after=2.0)
    wire_dev(monkeypatch, d, probe=OSError("rfcomm"))
    r = B.phone_connect()
    assert r["ok"] is True and "OSError" in r["rfcomm_error"]


def test_phone_connect_not_paired_and_missing_device(monkeypatch):
    d = FakeDev(paired=False)
    wire_dev(monkeypatch, d)
    assert B.phone_connect()["reason"] == "not_paired" and d.closed
    wire_dev(monkeypatch, None)
    assert B.phone_connect()["reason"] == "not_paired"


def test_phone_connect_no_mac_call_guard_and_exception(monkeypatch, stubs):
    monkeypatch.setattr(readiness, "_mac", ["not-a-mac"])
    assert B.phone_connect()["reason"] == "no_phone"
    monkeypatch.setattr(readiness, "_mac", [MAC])

    async def boom(mac):
        raise OSError("x")
    monkeypatch.setattr(B, "_open_device", boom)
    assert B.phone_connect()["reason"] == "connect_error"
    stubs.flags = {"call_active": None}
    assert B.phone_connect()["reason"] == "in_call"


# ---------------------------------------------------------------- register
def test_register_adds_four_actions_with_tiers():
    seen = {}

    class H:
        @staticmethod
        def register_action(name, fn, tier="safe"):
            seen[name] = (fn, tier)
    B.register(H)
    assert {k: v[1] for k, v in seen.items()} == {"radio_on": "safe", "hf_refresh": "safe", "phone_connect": "safe",
                                                  "radio_cycle": "heavy"}
    assert seen["radio_on"][0] is B.radio_on and seen["radio_cycle"][0] is B.radio_cycle


# ---------------------------------------------------------------- readiness: adapter missing vs bluetooth off
class RP:
    radio = {"present": True, "on": True}

    @classmethod
    async def bt_radio(cls):
        return dict(cls.radio)

    @staticmethod
    def call_flags():
        return {"call_active": False}

    @staticmethod
    def hf_link():
        return "unplugged"


def ready_env(monkeypatch, radio):
    P = type("RP2", (RP,), {"radio": radio})
    monkeypatch.setattr(readiness, "phone", P)
    monkeypatch.setattr(readiness, "_phone_link_running", lambda: True)
    monkeypatch.setattr(readiness, "battery_saver_on", lambda: False)
    monkeypatch.setattr(readiness, "session_locked", lambda: False)
    monkeypatch.setattr(readiness, "_prev_tick", [None])
    monkeypatch.setattr(readiness, "_hf_due", [None])


def test_readiness_adapter_missing_vs_off(monkeypatch):
    ready_env(monkeypatch, {"present": False, "on": False})
    info = {}
    assert readiness.auto_ready(info, MAC, lambda *a, **k: None) == (False, "bluetooth_adapter_missing")
    assert info["bt_radio"] is False
    ready_env(monkeypatch, {"present": True, "on": False})
    assert readiness.auto_ready({}, MAC, lambda *a, **k: None) == (False, "bluetooth_off")


def test_adapter_missing_texts_arabic_and_english():
    import re
    ar, en = readiness.reason_texts("bluetooth_adapter_missing")
    assert re.search(r"[؀-ۿ]", ar) and re.search(r"[A-Za-z]", en)
    d = readiness.decorate({"ok": False, "reason": "bluetooth_adapter_missing"})
    assert d["reason_ar"] == ar and d["reason_en"] == en

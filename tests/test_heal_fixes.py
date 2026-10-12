"""R-c4 review fixes on connector.heal / bt_actions / main / win_adapters: heal busy gate, policy cap clamp, fixed tiers,
no in-call callback under the lock, hf_refresh hourly cap, hf thread shared with readiness, radio always back on."""
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import bt_actions as B  # noqa: E402
from connector import heal, readiness  # noqa: E402
from test_bt_actions import FakeRadio, wire_radio  # noqa: E402
from test_bt_actions import stubs  # noqa: E402,F401  (fixture)
from test_heal import Clock, clk  # noqa: E402,F401


# ---------------------------------------------------------------- H1 (1) busy is set before the last in-call check
def test_busy_is_set_before_second_in_call_check(clk):
    seen = []
    calls = [0]

    def in_call():
        calls[0] += 1
        if calls[0] == 2:   # the check after picking
            seen.append(heal.is_busy())
        return False
    heal.set_in_call(in_call)
    heal.register_action("radio_on", lambda: {"ok": True})
    heal.report("bluetooth_off")
    assert heal.tick()["action"] == "radio_on"
    assert seen == [True] and heal.is_busy() is False


def test_busy_cleared_when_second_check_aborts(clk):
    calls = [0]

    def in_call():
        calls[0] += 1
        return calls[0] >= 2
    heal.set_in_call(in_call)
    ran = []
    heal.register_action("radio_on", lambda: ran.append(1) or {"ok": True})
    heal.report("bluetooth_off")
    assert heal.tick() is None and not ran and heal.is_busy() is False


def test_ready_wrapper_says_heal_busy_while_heal_runs(clk):
    from connector import main as cm
    ready = cm.heal_ready(lambda: {"ok": True, "reason": "", "warnings": []}, None)
    assert ready()["ok"] is True
    seen = []
    heal.register_action("radio_on", lambda: seen.append(ready()) or {"ok": True})
    heal.report("bluetooth_off")
    heal.tick()
    r = seen[0]
    assert r["ok"] is False and r["reason"] == "heal_busy" and r["reason_en"] and r["reason_ar"] and "heal" in r
    assert ready()["ok"] is True


def test_winphone_dial_refused_while_heal_busy(monkeypatch):
    from connector import win_adapters
    Ph = type("Ph", (), {"hf_link": staticmethod(lambda: pytest.fail("must not look at the phone")),
                         "call_flags": staticmethod(lambda: pytest.fail("must not look at the phone"))})
    monkeypatch.setattr(win_adapters, "phone", Ph)
    monkeypatch.setattr(win_adapters, "heal", type("H", (), {"is_busy": staticmethod(lambda: True)}))
    w = win_adapters.WinPhone.__new__(win_adapters.WinPhone)
    w._log = lambda *a, **k: None
    assert w.dial("01012345678") is False and "repair" in w.last_error


# ---------------------------------------------------------------- H1 (2) caps never raise
@pytest.mark.parametrize("v", [float("inf"), 1e308, 10 ** 30, "inf", float("-inf"), float("nan"), None, "x", [], 7.9])
def test_policy_cap_values_never_raise(clk, v):
    heal.set_policy({"max_adapter_resets_per_hour": v, "max_radio_toggles_per_hour": v, "max_hf_refresh_per_hour": v})
    p = heal.status()["policy"]
    for k in heal.CAP_KEYS:
        assert isinstance(p[k], int) and 0 <= p[k] <= heal.MAX_CAP
    heal.register_action("hf_refresh", lambda: {"ok": True})
    heal.report("hf_endpoints")
    heal.tick()   # _cap_left with the clamped value


def test_infinite_cap_clamps_to_max(clk):
    heal.set_policy({"max_radio_toggles_per_hour": float("inf")})
    assert heal.status()["policy"]["max_radio_toggles_per_hour"] == heal.MAX_CAP


# ---------------------------------------------------------------- H1 (3) tiers from a fixed table
def test_contract_tiers_are_fixed_not_from_caller(clk):
    heal.register_action("radio_cycle", lambda: {"ok": True}, tier="safe")
    heal.register_action("adapter_reset", lambda: {"ok": True}, tier="safe")
    heal.register_action("hf_refresh", lambda: {"ok": True}, tier="heavy")
    assert heal._actions["radio_cycle"][1] == "heavy" and heal._actions["adapter_reset"][1] == "heavy"
    assert heal._actions["hf_refresh"][1] == "safe"
    assert heal._skip_reason("radio_cycle", 0.0) == "heavy_off" and heal._skip_reason("adapter_reset", 0.0) == "heavy_off"
    heal.register_action("custom_x", lambda: {"ok": True}, tier="heavy")
    assert heal._actions["custom_x"][1] == "heavy"


# ---------------------------------------------------------------- H1 (4) no callback under the lock
def test_status_calls_in_call_callback_outside_lock(clk):
    got = []

    def in_call():
        got.append(heal._lock.acquire(blocking=False))   # would deadlock-block if status() held it
        if got[-1]:
            heal._lock.release()
        return False
    heal.set_in_call(in_call)
    assert heal.status()["in_call"] is False
    assert got == [True]


# ---------------------------------------------------------------- H1 (5) hf_refresh hourly cap
def _hf_runs(clk, monkeypatch, span_s, policy=None):
    """hf_refresh runs for a failure that keeps being reported and whose exhausted ladder restarts every 100 s."""
    monkeypatch.setattr(heal, "LADDER_RESTART_S", 100.0)
    if policy:
        heal.set_policy(policy)
    n = [0]
    heal.register_action("hf_refresh", lambda: n.__setitem__(0, n[0] + 1) or {"ok": True})
    end = clk.t + span_s
    while clk.t < end:
        heal.report("hf_endpoints")
        heal.tick()
        clk.adv(heal.COOLDOWN["hf_refresh"] + 20)
    return n[0]


def test_hf_refresh_default_cap_4_per_hour(clk, monkeypatch):
    heal.set_policy(None)
    assert heal.status()["policy"]["max_hf_refresh_per_hour"] == 4
    assert _hf_runs(clk, monkeypatch, 1500) == 4


def test_hf_refresh_cap_from_policy_and_window_expires(clk, monkeypatch):
    assert _hf_runs(clk, monkeypatch, 1500, {"max_hf_refresh_per_hour": 2}) == 2
    assert not heal._cap_left("hf_refresh", clk.t)
    clk.adv(3700)
    assert heal._cap_left("hf_refresh", clk.t)   # the hourly window moved on


# ---------------------------------------------------------------- H2 (a) requirements
def test_requirements_has_rfcomm_wheel():
    req = (Path(__file__).resolve().parents[1] / "packaging" / "requirements.txt").read_text().splitlines()
    names = [ln.strip() for ln in req if ln.strip() and not ln.startswith("#")]
    assert "winrt-Windows.Devices.Bluetooth.Rfcomm" in names and "winrt-Windows.Devices.Bluetooth" in names


# ---------------------------------------------------------------- H2 (b) readiness shares the hf thread
def test_start_hf_refresh_registers_thread_with_readiness(monkeypatch, stubs):
    gate = threading.Event()
    monkeypatch.setattr(B, "_refresh_hf", lambda mac: (gate.wait(10), {"ok": True})[1])
    assert B.start_hf_refresh("02:00:00:00:00:01")["started"] is True
    t = readiness._hf_thread[0]
    assert t is B._job["thread"] and t.is_alive()
    monkeypatch.setattr(readiness, "_hf_due", [None])
    monkeypatch.setattr(readiness, "_last_hf_refresh", [-1e9])
    monkeypatch.setattr(readiness, "_hf_check", lambda *a: pytest.fail("readiness must not start a second toggle"))
    assert readiness._maybe_refresh("02:00:00:00:00:01", lambda *a, **k: None) is True   # hf_refreshing, nothing started
    gate.set()
    t.join(5)


# ---------------------------------------------------------------- H2 (c) radio_cycle always turns the radio back on
def test_radio_cycle_off_unspecified_but_radio_off_is_turned_on(monkeypatch):
    r = FakeRadio(on=True, honor=False)
    wire_radio(monkeypatch, r)
    # the off lands and the radio is off, but its status is UNSPECIFIED, not ALLOWED
    seq = ["UNSPECIFIED", "ALLOWED"]

    async def setr(rad, on):
        s = seq.pop(0)
        rad.log.append(("on" if on else "off", s))
        if not on:
            rad.on = False
        elif s == "ALLOWED":
            rad.on = True
        return s
    monkeypatch.setattr(B, "_set_radio", setr)
    out = B.radio_cycle()
    assert r.on is True and r.log == [("off", "UNSPECIFIED"), ("on", "ALLOWED")] and out["ok"] is True


def test_radio_cycle_exception_between_off_and_on_still_turns_on(monkeypatch):
    r = FakeRadio()
    wire_radio(monkeypatch, r)

    async def boom(secs):
        raise RuntimeError("boom")
    monkeypatch.setattr(B, "_asleep", boom)
    out = B.radio_cycle()
    assert r.on is True and ("on", "ALLOWED") in r.log
    assert out["ok"] is False and out["reason"] == "radio_error"


# ---------------------------------------------------------------- start_heal registers bt_actions (guarded)
def test_start_heal_registers_bt_actions_and_survives_failure(clk, monkeypatch):
    from connector import main as cm
    app = type("A", (), {"stop_event": threading.Event(), "in_call": lambda self: False})()
    app.stop_event.set()
    t = cm.start_heal(app, lambda *a: None)
    t.join(5)
    assert {"radio_on", "hf_refresh", "phone_connect", "radio_cycle"} <= set(heal.status()["registered"])
    assert heal._actions["radio_cycle"][1] == "heavy"
    logs = []
    monkeypatch.setattr(B, "register", lambda h=None: (_ for _ in ()).throw(RuntimeError("no winrt")))
    t = cm.start_heal(app, lambda *a: logs.append(a))
    t.join(5)
    assert logs and logs[0][0] == "heal_register_failed"

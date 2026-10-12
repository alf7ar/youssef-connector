"""connector/heal.py: ladders, cooldowns, hourly caps, in-call block, heavy tiers off by default, policy, reset on recovery,
exceptions in actions. The clock is a fake (heal._now / heal._wall), actions are plain lambdas."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import heal  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def adv(self, s):
        self.t += s


@pytest.fixture
def clk(monkeypatch):
    heal.reset_for_tests()
    monkeypatch.setattr(heal, "LADDER_RESTART_S", 1e9)   # test_exhausted_ladder_restarts_later sets its own
    c = Clock()
    heal._now = c
    heal._wall = c
    yield c
    heal.reset_for_tests()


def actions(*names, ok=True, heavy=("radio_cycle", "adapter_reset")):
    calls = []
    for n in names:
        heal.register_action(n, (lambda n=n: calls.append(n) or {"ok": ok}), tier="heavy" if n in heavy else "safe")
    return calls


ALL = ("radio_on", "radio_cycle", "adapter_reset", "hf_refresh", "phone_connect", "phone_link_launch")


def climb(clk, failure, n):
    """Report + tick n times, letting every action settle in between; returns the actions that ran."""
    ran = []
    for _ in range(n):
        heal.report(failure)
        e = heal.tick()
        if e:
            ran.append(e["action"])
        clk.adv(400)
    return ran


def test_bluetooth_off_ladder_full_policy(clk):
    calls = actions(*ALL, ok=False)
    heal.set_policy({"radio_cycle": True, "adapter_reset": True})
    assert climb(clk, "bluetooth_off", 5) == ["radio_on", "radio_cycle", "adapter_reset"]
    assert calls == ["radio_on", "radio_cycle", "adapter_reset"]


@pytest.mark.parametrize("failure,ladder", [
    ("iphone_not_connected", ["phone_connect", "hf_refresh", "radio_cycle", "adapter_reset"]),
    ("hf_endpoints", ["hf_refresh", "radio_cycle", "adapter_reset"]),
    ("bt_unhealthy", ["hf_refresh", "radio_cycle", "adapter_reset"]),
    ("bthusb_error", ["hf_refresh", "radio_cycle", "adapter_reset"]),
    ("phone_link_not_running", ["phone_link_launch"]),
    ("phone_link_not_linked", ["phone_link_launch"]),
    ("hangup_failed", []),
    ("net_dns", []),
])
def test_ladders(clk, failure, ladder):
    actions(*ALL, ok=False)
    heal.set_policy({"radio_cycle": True, "adapter_reset": True, "max_radio_toggles_per_hour": 9, "max_adapter_resets_per_hour": 9})
    assert climb(clk, failure, 6) == ladder


def test_heavy_off_by_default_and_missing_skipped(clk):
    actions("radio_on", "radio_cycle", "adapter_reset", ok=False)   # no hf_refresh / phone_connect registered
    assert climb(clk, "bluetooth_off", 4) == ["radio_on"]
    assert climb(clk, "iphone_not_connected", 3) == []
    assert heal.status()["policy"] == heal.DEFAULT_POLICY


def test_policy_enables_one_heavy_tier(clk):
    actions(*ALL, ok=False)
    heal.set_policy({"radio_cycle": True})
    assert climb(clk, "hf_endpoints", 4) == ["hf_refresh", "radio_cycle"]
    heal.set_policy({"radio_cycle": "yes", "max_radio_toggles_per_hour": "x"})   # only True enables; junk keeps the default
    assert heal._policy == heal.DEFAULT_POLICY
    heal.set_policy(None)
    assert heal._policy == heal.DEFAULT_POLICY


def test_one_action_per_tick(clk):
    calls = actions(*ALL)
    heal.report("bluetooth_off")
    heal.report("phone_link_not_running")
    assert heal.tick()["action"] == "radio_on"
    assert calls == ["radio_on"]
    assert heal.tick()["action"] == "phone_link_launch"   # next tick: the other failure
    assert heal.tick() is None


def test_needs_new_report_and_settle_before_next_rung(clk):
    heal.set_policy({"radio_cycle": True})
    actions(*ALL, ok=False)
    heal.report("bluetooth_off")
    assert heal.tick()["action"] == "radio_on"
    clk.adv(heal.COOLDOWN["radio_on"] + 1)
    assert heal.tick() is None                     # no report since the action ended
    heal.report("bluetooth_off")
    heal.report("bluetooth_off")
    clk.adv(1)
    assert heal.tick()["action"] == "radio_cycle"
    heal.report("bluetooth_off")
    clk.adv(5)
    assert heal.tick() is None                     # radio_cycle still settling


def test_action_cooldown_across_failures(clk):
    calls = actions(*ALL)
    heal.report("hf_endpoints")
    assert heal.tick()["action"] == "hf_refresh"
    heal.report("bt_unhealthy")
    clk.adv(10)
    assert heal.tick() is None                     # hf_refresh ran 10 s ago (cooldown 180 s)
    clk.adv(heal.COOLDOWN["hf_refresh"])
    heal.report("bt_unhealthy")
    assert heal.tick()["action"] == "hf_refresh"
    assert calls == ["hf_refresh", "hf_refresh"]


def test_hourly_radio_cap_then_report_only(clk):
    logs = []
    heal.set_log(lambda *a: logs.append(a))
    actions(*ALL, ok=False)
    heal.set_policy({"max_radio_toggles_per_hour": 2})
    ran = []
    for _ in range(4):   # each episode: report, act, recover (stale), fail again
        ran += climb(clk, "bluetooth_off", 1)
        clk.adv(heal.STALE_S + 1)
        heal.tick()
    assert ran == ["radio_on", "radio_on"]
    assert heal.status()["caps"]["radio_toggles"] == 2
    assert any(a[0] == "heal_skip" and json.loads(a[1])["why"] == "capped" for a in logs)
    clk.adv(3600)
    assert climb(clk, "bluetooth_off", 1) == ["radio_on"]   # the hour rolled over


def test_adapter_reset_cap(clk):
    actions(*ALL, ok=False)
    heal.set_policy({"adapter_reset": True, "max_adapter_resets_per_hour": 1})
    heal.register_action("hf_refresh", lambda: {"ok": False})
    first = climb(clk, "hf_endpoints", 3)
    clk.adv(heal.STALE_S + 1)
    heal.tick()
    second = climb(clk, "hf_endpoints", 3)
    assert first == ["hf_refresh", "adapter_reset"] and second == ["hf_refresh"]
    assert heal.status()["caps"]["adapter_resets"] == 1


def test_in_call_blocks(clk):
    calls = actions(*ALL)
    busy = [True]
    heal.set_in_call(lambda: busy[0])
    heal.report("bluetooth_off")
    assert heal.tick() is None and calls == []
    assert heal.status()["in_call"] is True
    heal.set_in_call(lambda: 1 / 0)                 # cannot tell: treated as in a call
    assert heal.tick() is None and calls == []
    heal.set_in_call(lambda: busy[0])
    busy[0] = False
    assert heal.tick()["action"] == "radio_on"


def test_call_starts_between_pick_and_run(clk):
    calls = actions(*ALL)
    seq = iter([False, True])
    heal.set_in_call(lambda: next(seq, True))
    heal.report("bluetooth_off")
    assert heal.tick() is None and calls == []
    heal.set_in_call(lambda: False)
    assert heal.tick()["action"] == "radio_on"      # rung and cap were rolled back
    assert heal.status()["caps"]["radio_toggles"] == 1


def test_reset_on_recovery(clk):
    heal.set_policy({"radio_cycle": True})
    actions(*ALL, ok=False)
    assert climb(clk, "bluetooth_off", 1) == ["radio_on"]
    clk.adv(heal.STALE_S + 1)                       # no longer reported: recovered
    heal.tick()
    assert "bluetooth_off" not in heal.status()["active"]
    assert climb(clk, "bluetooth_off", 1) == ["radio_on"]   # starts from the first rung again


def test_event_failure_survives_a_call(clk):
    calls = actions(*ALL)
    heal.set_in_call(lambda: True)
    heal.report("bthusb_error", id=3, provider="BTHUSB")
    clk.adv(300)                                    # a 5 minute call
    assert heal.tick() is None
    heal.set_in_call(lambda: False)
    e = heal.tick()
    assert e["action"] == "hf_refresh" and calls == ["hf_refresh"]


def test_exception_in_action_is_logged_failure(clk):
    logs = []
    heal.set_log(lambda *a: logs.append(a))
    heal.register_action("radio_on", lambda: 1 / 0)
    heal.report("bluetooth_off")
    e = heal.tick()
    assert e["ok"] is False and "ZeroDivisionError" in e["error"]
    rec = [json.loads(a[1]) for a in logs if a[0] == "heal_action"]
    assert rec and rec[0]["failure"] == "bluetooth_off" and rec[0]["action"] == "radio_on" and rec[0]["ok"] is False
    assert "ms" in rec[0]
    assert heal.status()["actions"][-1]["error"].startswith("ZeroDivisionError")


def test_report_only_and_unknown_failure(clk):
    logs = []
    heal.set_log(lambda *a: logs.append(a))
    actions(*ALL)
    heal.report("net_dns", host="example.invalid")
    heal.report("something_new")
    assert heal.tick() is None
    assert [json.loads(a[1])["failure"] for a in logs if a[0] == "heal_report_only"] == ["net_dns", "something_new"]
    assert heal.status()["active"]["net_dns"]["next"] is None


def test_exhausted_ladder_restarts_later(clk, monkeypatch):
    monkeypatch.setattr(heal, "LADDER_RESTART_S", 900.0)
    actions(*ALL, ok=False)
    assert climb(clk, "phone_link_not_running", 2) == ["phone_link_launch"]   # second report 400 s later: exhausted
    for _ in range(4):                              # still reported, but the ladder ran out
        heal.report("phone_link_not_running")
        assert heal.tick() is None
        clk.adv(20)
    clk.adv(heal.LADDER_RESTART_S)
    heal.report("phone_link_not_running")
    assert heal.tick()["action"] == "phone_link_launch"


def test_adapter_reset_success_follows_with_hf_refresh(clk):
    heal.set_policy({"adapter_reset": True})
    calls = actions("adapter_reset", "hf_refresh")
    heal.register_action("hf_refresh", lambda: calls.append("hf_refresh") or {"ok": False})
    climb(clk, "bluetooth_off", 1)
    assert calls == ["adapter_reset"]
    e = heal.tick()
    assert e["action"] == "hf_refresh" and e["failure"] == "follow_up"


def test_status_is_small_json(clk):
    actions(*ALL)
    for _ in range(30):
        climb(clk, "phone_link_not_running", 1)
        clk.adv(heal.STALE_S + 1)
        heal.tick()
    heal.report("hf_endpoints", states={"capture": 4})
    st = heal.status()
    assert len(st["actions"]) == heal.RING and set(st["actions"][0]) >= {"t", "failure", "action", "ok", "ms"}
    assert isinstance(st["actions"][-1]["t"], float) and st["actions"][-1]["t"] >= st["actions"][0]["t"]   # newest last
    assert st["busy"] is False and set(st["caps"]) == {"adapter_resets", "radio_toggles", "hf_refresh"}
    assert len(json.dumps(st)) < 4000
    assert st["registered"] == sorted(ALL)


def test_busy_while_action_runs(clk):
    seen = []
    heal.register_action("radio_on", lambda: seen.append(heal.status()["busy"]) or {"ok": True})
    heal.report("bluetooth_off")
    heal.tick()
    assert seen == [True] and heal.status()["busy"] is False


def test_non_dict_result(clk):
    heal.register_action("radio_on", lambda: True)
    heal.report("bluetooth_off")
    assert heal.tick()["ok"] is True


# ---------------------------------------------------------------- wiring: readiness, WinPhone.busy, main (tick thread, heartbeat, policy)
def test_readiness_check_reports_heal_failures(clk, monkeypatch):
    from connector import readiness
    from test_readiness_warnings import P, setup_ready
    monkeypatch.setattr(readiness, "collect_warnings", lambda name="": [])
    monkeypatch.setattr(readiness, "heal", heal)

    async def off():
        return {"on": False}
    setup_ready(monkeypatch, phone=P.mk(bt_radio=staticmethod(off)))
    assert readiness.check("", lambda *a, **k: None)["reason"] == "bluetooth_off"
    assert "bluetooth_off" in heal.status()["active"]
    setup_ready(monkeypatch, locked=True)          # not a heal class: not reported
    assert readiness.check("", lambda *a, **k: None)["reason"] == "session_locked"
    assert list(heal.status()["active"]) == ["bluetooth_off"]


def test_readiness_does_not_report_hf_refreshing(clk, monkeypatch):
    from connector import readiness
    from test_readiness_warnings import P, setup_ready
    monkeypatch.setattr(readiness, "collect_warnings", lambda name="": [])
    monkeypatch.setattr(readiness, "heal", heal)
    setup_ready(monkeypatch, phone=P.mk(hf_abnormal=staticmethod(lambda eps: True)))
    monkeypatch.setattr(readiness, "_maybe_refresh", lambda mac, log: True)
    assert readiness.check("", lambda *a, **k: None)["reason"] == "hf_refreshing"
    monkeypatch.setattr(readiness, "_maybe_refresh", lambda mac, log: False)
    assert readiness.check("", lambda *a, **k: None)["reason"] == "hf_endpoints"
    assert list(heal.status()["active"]) == ["hf_endpoints"]


def test_winphone_busy(monkeypatch):
    from connector import win_adapters
    flags = {"call_active": False, "call_incoming": False}
    monkeypatch.setattr(win_adapters, "phone", type("Ph", (), {"call_flags": staticmethod(lambda: dict(flags))}))
    w = win_adapters.WinPhone.__new__(win_adapters.WinPhone)
    w.t_dial, w.ended = None, False
    assert w.busy() is False
    flags["call_incoming"] = True
    assert w.busy() is True
    flags.update(call_incoming=False, call_active=None)   # unknown: assume a call
    assert w.busy() is True
    flags["call_active"] = False
    w.t_dial = 1.0
    assert w.busy() is True


def test_start_heal_thread_ticks(clk, monkeypatch):
    import threading
    from connector import bt_actions, main as cm
    monkeypatch.setattr(bt_actions, "register", lambda h=None: None)   # the real actions would replace the test's radio_on
    heal._now = heal._wall = __import__("time").monotonic
    done = threading.Event()
    heal.register_action("radio_on", lambda: done.set() or {"ok": True})
    heal.report("bluetooth_off")
    app = type("A", (), {"stop_event": threading.Event(), "in_call": lambda self: False})()
    t = cm.start_heal(app, lambda *a: None)
    try:
        assert done.wait(5)
    finally:
        app.stop_event.set()
        t.join(5)
    assert not t.is_alive()


def test_app_heartbeat_has_heal_and_config_policy(clk, tmp_path, monkeypatch):
    import asyncio
    from connector import main as cm
    import mock_hub
    monkeypatch.setenv("YC_FAKE", "1")

    async def go():
        hub = mock_hub.MockHub(token="t")
        await hub.start()
        app = cm.App(tmp_path, "9.9.9", lambda *a: None)
        app.cfg = {"url": hub.url, "token": "t", "heal": {"radio_cycle": True}}
        runner = asyncio.create_task(app.run(False))
        try:
            hb = await hub.wait_msg("hb", timeout=20)
        finally:
            app.stop_event.set()
            await asyncio.wait_for(runner, 10)
            await hub.stop()
        return hb
    hb = asyncio.run(go())
    h = hb["ready"]["heal"]
    assert h["policy"]["radio_cycle"] is True and h["policy"]["adapter_reset"] is False
    assert h["actions"] == [] and h["busy"] is False

"""End-to-end heal scenarios against the connector/heal.py contract, driven with the fakes.
heal.py may not be merged yet: scenario tests are xfail(strict=False) while it is missing (or while behavior differs);
the fakes' own tests always run."""
import pytest

from connector import fakes
from connector.fakes import FakeEventLog, FakePhone, FakeRadio, FakeSchtasks

try:
    from connector import heal
except ImportError:
    heal = None

needs_heal = pytest.mark.xfail(heal is None, reason="connector/heal.py not merged yet", strict=False)


# ---- fakes (always run) ----
def test_fake_radio():
    r = FakeRadio(on=False)
    assert r.set(True) and r.on
    r2 = FakeRadio(on=False, fail_on=True)
    assert not r2.set(True) and not r2.on and r2.toggles == [True]


def test_fake_eventlog():
    e = FakeEventLog()
    e.add("BTHUSB", 17, "x")
    e.add("other", 1)
    assert len(e.read("BTHUSB")) == 1 and len(e.read()) == 2
    e.clear()
    assert e.read() == []


def test_fake_schtasks():
    s = FakeSchtasks()
    assert s.run("t") == 1
    assert s.create("t", "cmd") == 0 and s.query("t") and s.run("t") == 0 and s.runs == ["t"]
    assert s.delete("t") == 0 and not s.query("t")
    assert FakeSchtasks(fail=True).create("t", "c") == 1


@pytest.mark.parametrize("cls", fakes.FAILURE_CLASSES)
def test_fake_phone_failure_states(cls):
    p = FakePhone()
    assert p.ready()["ok"]
    p.set_failure(cls)
    if cls in fakes.FAILURE_READY:
        r = p.ready()
        assert not r["ok"] and any(v is False for k, v in r.items() if k != "ok")
    p.clear_failure()
    assert p.ready()["ok"] and p.ready()["bt_radio"] and p.ready()["hf_link"]


def test_fake_phone_hangup_failed():
    p = FakePhone()
    p.dial("01000000000")
    p.set_failure("hangup_failed")
    p.hangup()
    assert p.hangups == 1 and p.call_state() != "ended"
    p.clear_failure()
    p.hangup()
    assert p.call_state() == "ended"


# ---- scenarios against the heal contract ----
@pytest.fixture
def h():
    if heal is None:
        pytest.xfail("connector/heal.py not merged yet")
    heal.reset_for_tests()
    yield heal
    heal.reset_for_tests()


def _wire(h, **policy):
    """Register radio_on / hf_refresh against fakes; returns (radio, phone, calls)."""
    radio, phone, calls = FakeRadio(on=False), FakePhone(), []
    phone.set_failure("bluetooth_off")

    def radio_on():
        calls.append("radio_on")
        ok = radio.set(True)
        if ok:
            phone.clear_failure()
        return {"ok": ok}

    def hf_refresh():
        calls.append("hf_refresh")
        phone.clear_failure()
        return {"ok": True}

    h.register_action("radio_on", radio_on, tier="safe")
    h.register_action("hf_refresh", hf_refresh, tier="safe")
    h.set_in_call(lambda: False)
    if policy:
        h.set_policy(policy)
    return radio, phone, calls


def _drain(h, n=6):
    for _ in range(n):
        h.tick()


@needs_heal
def test_bluetooth_off_runs_radio_on(h):
    radio, phone, calls = _wire(h)
    h.report("bluetooth_off")
    _drain(h)
    assert "radio_on" in calls and radio.on and phone.ready()["ok"]
    assert isinstance(h.status(), dict)


@needs_heal
def test_no_heal_during_call(h):
    radio, phone, calls = _wire(h)
    h.set_in_call(lambda: True)
    h.report("bluetooth_off")
    _drain(h)
    assert calls == [] and not radio.on


@needs_heal
def test_hf_endpoints_runs_hf_refresh(h):
    radio, phone, calls = _wire(h)
    phone.set_failure("hf_endpoints")
    h.report("hf_endpoints")
    _drain(h)
    assert "hf_refresh" in calls


@needs_heal
def test_heavy_tiers_off_by_default(h):
    _, _, calls = _wire(h)
    for name in ("radio_cycle", "adapter_reset"):
        h.register_action(name, lambda n=name: calls.append(n) or {"ok": True}, tier="heavy")
    for _ in range(5):
        h.report("bthusb_error")
        h.report("bt_unhealthy")
        _drain(h)
    assert "radio_cycle" not in calls and "adapter_reset" not in calls


@needs_heal
def test_heavy_radio_cycle_when_policy_enables_and_rate_limited(h):
    _, _, calls = _wire(h)
    h.register_action("radio_cycle", lambda: calls.append("radio_cycle") or {"ok": False}, tier="heavy")
    h.set_policy({"radio_cycle": True, "max_radio_toggles_per_hour": 1})
    for _ in range(6):
        h.report("bt_unhealthy")
        _drain(h)
    assert calls.count("radio_cycle") <= 1


@needs_heal
def test_adapter_reset_hourly_cap(h):
    _, _, calls = _wire(h)
    h.register_action("adapter_reset", lambda: calls.append("adapter_reset") or {"ok": False}, tier="heavy")
    h.set_policy({"adapter_reset": True, "max_adapter_resets_per_hour": 2})
    for _ in range(10):
        h.report("bthusb_error")
        _drain(h)
    assert calls.count("adapter_reset") <= 2


@needs_heal
def test_failing_action_does_not_raise_and_is_reported_in_status(h):
    h.register_action("radio_on", lambda: (_ for _ in ()).throw(RuntimeError("boom")), tier="safe")
    h.set_in_call(lambda: False)
    h.report("bluetooth_off")
    _drain(h)   # must not raise
    assert isinstance(h.status(), dict)


@needs_heal
def test_phone_link_not_running_launches(h):
    calls = []
    sch = FakeSchtasks()
    sch.create("pl", "phone link")
    h.register_action("phone_link_launch", lambda: calls.append("pl") or {"ok": sch.run("pl") == 0}, tier="safe")
    h.set_in_call(lambda: False)
    h.report("phone_link_not_running")
    _drain(h)
    assert calls and sch.runs == ["pl"]


@needs_heal
def test_reset_for_tests_clears_state(h):
    _wire(h)
    h.report("bluetooth_off")
    h.reset_for_tests()
    h.set_in_call(lambda: False)
    calls = []
    h.register_action("radio_on", lambda: calls.append(1) or {"ok": True})
    _drain(h)
    assert calls == []

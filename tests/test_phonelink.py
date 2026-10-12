"""Phone Link launch (heal action phone_link_launch), plcall failure classification, and the one dial retry.
subprocess, the Phone Link process check and the phone module are mocked; heal is a stub (owner H1 not merged yet)."""
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import phonelink, readiness, win_adapters  # noqa: E402


class FakeHeal:
    def __init__(self):
        self.actions = {}
        self.reports = []

    def register_action(self, name, fn, tier="safe"):
        self.actions[name] = (fn, tier)

    def report(self, failure, **detail):
        self.reports.append((failure, detail))


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    """Replaces the time module as seen by phonelink only (asyncio.run on Windows also calls the global time.sleep)."""
    c = FakeClock()
    monkeypatch.setattr(phonelink, "time", types.SimpleNamespace(monotonic=c.monotonic, sleep=c.sleep))
    return c


@pytest.fixture
def heal(monkeypatch):
    h = FakeHeal()
    monkeypatch.setattr(phonelink, "heal", h)
    return h


# ---------------------------------------------------------------- launch
def test_launch_does_nothing_when_phone_link_runs(monkeypatch):
    popen = []
    monkeypatch.setattr(readiness, "_tasklist_has", lambda image: True)
    monkeypatch.setattr(phonelink.subprocess, "Popen", lambda *a, **k: popen.append(a))
    assert phonelink.launch() == {"ok": True, "launched": False, "running": True}
    assert popen == []


def test_launch_starts_explorer_app_and_waits_for_process(monkeypatch, clock):
    calls = []
    seen = iter([False, False, True])   # not running at the check, then not yet, then running
    monkeypatch.setattr(readiness, "_tasklist_has", lambda image: (calls.append(image), next(seen))[1])
    popen_args = {}

    def fake_popen(args, **kw):
        popen_args["args"], popen_args["kw"] = args, kw
    monkeypatch.setattr(phonelink.subprocess, "Popen", fake_popen)
    r = phonelink.launch()
    assert r["ok"] is True and r["launched"] is True and r["running"] is True
    assert popen_args["args"] == ["explorer.exe", r"shell:AppsFolder\Microsoft.YourPhone_8wekyb3d8bbwe!App"]
    assert popen_args["kw"]["creationflags"] == phonelink.CREATE_NO_WINDOW
    assert set(calls) == {"PhoneExperienceHost.exe"}


def test_launch_times_out_after_20_seconds(monkeypatch, clock):
    monkeypatch.setattr(readiness, "_tasklist_has", lambda image: False)
    monkeypatch.setattr(phonelink.subprocess, "Popen", lambda *a, **k: None)
    r = phonelink.launch()
    assert r["ok"] is False and r["launched"] is True and r["running"] is False
    assert "20 s" in r["error"]
    assert 20.0 <= clock.t <= 21.0


def test_launch_unknown_process_state_is_not_not_running(monkeypatch, clock):
    """tasklist failing (None) must not pop Phone Link up: no launch, ok False."""
    popen = []
    monkeypatch.setattr(readiness, "_tasklist_has", lambda image: None)
    monkeypatch.setattr(phonelink.subprocess, "Popen", lambda *a, **k: popen.append(a))
    r = phonelink.launch()
    assert r["ok"] is False and r["launched"] is False and r["running"] is None
    assert popen == [] and clock.t == 0.0


def test_launch_is_registered_with_heal_as_safe_tier(monkeypatch):
    """Import-time registration: a stub heal that is present before the module is (re)loaded gets the action."""
    h = FakeHeal()
    monkeypatch.setitem(sys.modules, "connector.heal", types.SimpleNamespace(
        register_action=h.register_action, report=h.report))
    import importlib
    reloaded = importlib.reload(phonelink)
    try:
        fn, tier = h.actions["phone_link_launch"]
        assert fn is reloaded.launch and tier == "safe"
    finally:
        monkeypatch.undo()
        importlib.reload(phonelink)


# ---------------------------------------------------------------- classification
@pytest.mark.parametrize("err, reason", [
    ("Phone Link is not running", "phone_link_not_running"),
    ("Phone Link dialer did not show the number", "phone_link_not_linked"),
    ("Phone Link did not start the call", "phone_link_not_linked"),
    ("no Calls UI", "phone_link_not_linked"),
    ("bad number", None),
    ("plcall timeout", None),
    ("", None),
    (None, None),
])
def test_reason_for_plcall_errors(err, reason):
    assert phonelink.reason_for(err) == reason


@pytest.mark.parametrize("err, ok", [
    ("Phone Link is not running", True),
    ("Phone Link dialer did not show the number", True),
    ("no Calls UI", True),
    ("Phone Link did not start the call", False),   # after the Call press: the phone may already ring
    ("plcall timeout", False),
    ("bad number", False),
    (None, False),
])
def test_retryable_only_before_the_call_button_press(err, ok):
    assert phonelink.retryable(err) is ok


def test_report_goes_to_heal_when_present(heal):
    phonelink.report("phone_link_not_linked", error="x")
    assert heal.reports == [("phone_link_not_linked", {"error": "x"})]


def test_report_is_noop_without_heal(monkeypatch):
    monkeypatch.setattr(phonelink, "heal", None)
    phonelink.report("phone_link_not_running")   # must not raise


def test_reason_codes_have_texts():
    for code in ("phone_link_not_running", "phone_link_not_linked"):
        ar, en = readiness.reason_texts(code)
        assert ar and en and "Phone Link" in en


# ---------------------------------------------------------------- WinPhone.dial retry
NOT_SHOWN = {"ok": False, "error": "Phone Link dialer did not show the number"}
NOT_RUNNING = {"ok": False, "error": "Phone Link is not running"}
NOT_STARTED = {"ok": False, "error": "Phone Link did not start the call"}
OK = {"ok": True, "method": "invoke", "button": "Call"}


class World:
    """Fake clock + scripted phone line shared by the fake phone module and win_adapters' time."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []
        self.events = []        # (time, fn): run when the clock passes the time
        self.call_active = False
        self.call_incoming = False
        self.hf = "absent"
        self.launches = []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.advance(s)

    def advance(self, s):
        self.t += s
        for ev in [e for e in self.events if e[0] <= self.t]:
            self.events.remove(ev)
            ev[1]()

    def at(self, t, fn):
        self.events.append((t, fn))


class FakePhone:
    """Stands in for connector.phone: scripted plcall results (each takes durations[i] s), a scripted line."""

    def __init__(self, world, results, durations):
        self.world, self.results, self.durations = world, list(results), list(durations)
        self.calls = []

    async def phone_link(self, action, number=""):
        self.calls.append((action, number))
        self.world.advance(self.durations.pop(0) if self.durations else 0.5)
        return self.results.pop(0)

    def hf_link(self):
        return self.world.hf

    def call_flags(self):
        return {"call_active": self.world.call_active, "call_incoming": self.world.call_incoming}


@pytest.fixture
def dialer(monkeypatch):
    world = World()
    monkeypatch.setattr(win_adapters, "time", types.SimpleNamespace(monotonic=world.monotonic, sleep=world.sleep))
    monkeypatch.setattr(phonelink, "launch", lambda: (world.launches.append(world.t), {"ok": True, "launched": True})[1])

    def make(results, durations=()):
        fake = FakePhone(world, results, durations)
        monkeypatch.setattr(win_adapters, "phone", fake)
        logs = []
        w = win_adapters.WinPhone("hf_direct", "", lambda ev, s="": logs.append((ev, s)))
        return w, fake, logs
    return make, world


def test_dial_retries_once_after_3s_on_pre_press_failure(dialer, heal):
    make, world = dialer
    w, fake, logs = make([NOT_SHOWN, OK])
    assert w.dial("01012345678") is True
    assert len(fake.calls) == 2 and fake.calls[0] == fake.calls[1] == ("dial", "01012345678")
    assert sum(world.sleeps) == pytest.approx(phonelink.RETRY_S)
    assert world.launches == []         # the launch is only for "not running"
    assert any(ev == "dial_retry" for ev, _ in logs)
    assert heal.reports == []
    assert w.t_dial is not None


def test_dial_failing_twice_reports_not_linked(dialer, heal):
    make, world = dialer
    w, fake, _ = make([dict(NOT_SHOWN), dict(NOT_SHOWN)])
    assert w.dial("01012345678") is False
    assert len(fake.calls) == 2
    assert w.last_error.startswith("phone_link_not_linked: ")
    assert heal.reports and heal.reports[0][0] == "phone_link_not_linked"
    assert w.t_dial is None


def test_dial_not_running_launches_phone_link_before_the_retry(dialer, heal):
    make, world = dialer
    w, fake, _ = make([NOT_RUNNING, OK], [0.5, 0.5])
    assert w.dial("01012345678") is True
    assert len(fake.calls) == 2 and world.launches == [0.5]


def test_dial_not_running_twice_is_reported_as_phone_link_not_running(dialer, heal):
    make, world = dialer
    w, fake, _ = make([dict(NOT_RUNNING), dict(NOT_RUNNING)])
    assert w.dial("01012345678") is False
    assert heal.reports[0][0] == "phone_link_not_running"


def test_dial_not_running_without_a_launch_does_not_retry(dialer, heal, monkeypatch):
    make, world = dialer
    monkeypatch.setattr(phonelink, "launch", lambda: {"ok": False, "launched": False, "error": "state unknown"})
    w, fake, logs = make([NOT_RUNNING, OK])
    assert w.dial("01012345678") is False
    assert len(fake.calls) == 1 and world.sleeps == []
    assert ("dial_no_retry" in [ev for ev, _ in logs]) and '"launch_failed"' in dict(logs)["dial_no_retry"]
    assert heal.reports[0][0] == "phone_link_not_running"


def test_dial_does_not_retry_after_the_call_button_was_pressed(dialer, heal):
    """press-then-fail: 'did not start the call' means the Call button was pressed and the phone may be ringing."""
    make, world = dialer
    w, fake, _ = make([NOT_STARTED, OK], [2.0])
    assert w.dial("01012345678") is False
    assert len(fake.calls) == 1 and world.sleeps == []
    assert heal.reports[0][0] == "phone_link_not_linked"


def test_dial_does_not_retry_a_slow_failure(dialer, heal):
    """a failure 5+ s after the request is past the point where the hub would still take a ring (ack_timeout 15 s)."""
    make, world = dialer
    w, fake, logs = make([NOT_SHOWN, OK], [12.0])
    assert w.dial("01012345678") is False
    assert len(fake.calls) == 1 and world.sleeps == [] and world.launches == []
    assert '"slow_failure"' in dict(logs)["dial_no_retry"]


def test_dial_just_under_5s_still_retries(dialer, heal):
    make, world = dialer
    w, fake, _ = make([NOT_SHOWN, OK], [4.9])
    assert w.dial("01012345678") is True and len(fake.calls) == 2


def test_dial_retry_must_start_before_the_deadline(dialer, heal, monkeypatch):
    """a slow Phone Link launch pushes the retry past the deadline: no retry."""
    make, world = dialer
    monkeypatch.setattr(phonelink, "launch", lambda: (world.advance(8.0), {"ok": True, "launched": True})[1])
    w, fake, logs = make([NOT_RUNNING, OK], [0.5])
    assert w.dial("01012345678") is False
    assert len(fake.calls) == 1 and '"too_late"' in dict(logs)["dial_no_retry"]


def test_dial_call_appearing_during_the_wait_is_success_not_a_second_dial(dialer, heal):
    make, world = dialer
    w, fake, logs = make([NOT_SHOWN, OK], [0.5])
    world.at(1.5, lambda: setattr(world, "call_active", True))
    assert w.dial("01012345678") is True
    assert len(fake.calls) == 1          # no second plcall
    assert w.t_dial is not None
    assert any(ev == "dial_late_success" for ev, _ in logs)
    assert heal.reports == []


def test_dial_hf_link_coming_up_during_the_wait_is_success(dialer, heal):
    make, world = dialer
    w, fake, _ = make([NOT_SHOWN, OK], [0.5])
    world.at(2.0, lambda: setattr(world, "hf", "active"))
    assert w.dial("01012345678") is True and len(fake.calls) == 1


def test_dial_unknown_call_state_never_retries(dialer, heal):
    make, world = dialer
    world.call_active = None
    w, fake, logs = make([NOT_SHOWN, OK])
    assert w.dial("01012345678") is False
    assert len(fake.calls) == 1 and world.sleeps == []
    assert '"line_not_clear"' in dict(logs)["dial_no_retry"]


def test_dial_call_state_turning_unknown_during_the_wait_never_retries(dialer, heal):
    make, world = dialer
    w, fake, logs = make([NOT_SHOWN, OK], [0.5])
    world.at(2.0, lambda: setattr(world, "call_active", None))
    assert w.dial("01012345678") is False
    assert len(fake.calls) == 1
    assert '"unknown"' in dict(logs)["dial_no_retry"]


def test_dial_incoming_call_during_the_wait_never_retries(dialer, heal):
    make, world = dialer
    w, fake, logs = make([NOT_SHOWN, OK], [0.5])
    world.at(1.0, lambda: setattr(world, "call_incoming", True))
    world.at(2.0, lambda: setattr(world, "call_incoming", False))   # it stopped again before the wait ended
    assert w.dial("01012345678") is False
    assert len(fake.calls) == 1 and '"incoming"' in dict(logs)["dial_no_retry"]


def test_dial_hf_link_already_active_never_retries(dialer, heal):
    make, world = dialer
    w, fake, _ = make([NOT_SHOWN, OK], [0.5])
    world.at(0.2, lambda: setattr(world, "hf", "active"))   # up by the time the first attempt returns
    assert w.dial("01012345678") is False
    assert len(fake.calls) == 1 and world.sleeps == []


def test_dial_does_not_retry_on_bad_number(dialer, heal):
    make, world = dialer
    w, fake, _ = make([{"ok": False, "error": "bad number"}])
    assert w.dial("01012345678") is False
    assert len(fake.calls) == 1 and world.sleeps == []
    assert heal.reports == []


def test_dial_does_not_retry_on_timeout(dialer, heal):
    make, world = dialer
    w, fake, _ = make([{"ok": False, "error": "plcall timeout"}])
    assert w.dial("01012345678") is False
    assert len(fake.calls) == 1 and world.sleeps == []


def test_dial_does_not_retry_when_a_call_is_already_up(dialer, heal):
    make, world = dialer
    w, fake, _ = make([NOT_SHOWN], [0.5])
    world.at(0.2, lambda: setattr(world, "call_active", True))
    assert w.dial("01012345678") is False
    assert len(fake.calls) == 1 and world.sleeps == []

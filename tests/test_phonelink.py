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


def test_launch_treats_unknown_process_state_as_not_running(monkeypatch, clock):
    monkeypatch.setattr(readiness, "_tasklist_has", lambda image: None)
    monkeypatch.setattr(phonelink.subprocess, "Popen", lambda *a, **k: None)
    assert phonelink.launch()["ok"] is False


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
class FakePhone:
    """Stands in for connector.phone: scripted plcall results, idle line."""

    def __init__(self, results, call_active=False, active_after_first=False):
        self.results = list(results)
        self.calls = []
        self.call_active = call_active
        self.active_after_first = active_after_first

    async def phone_link(self, action, number=""):
        self.calls.append((action, number))
        if self.active_after_first:   # Windows sees a call after the first attempt
            self.call_active = True
        return self.results.pop(0)

    def hf_link(self):
        return "absent"

    def call_flags(self):
        return {"call_active": self.call_active, "call_incoming": False}


@pytest.fixture
def dialer(monkeypatch):
    sleeps = []
    monkeypatch.setattr(win_adapters, "time", types.SimpleNamespace(monotonic=time.monotonic,
                                                                   sleep=lambda s: sleeps.append(s)))

    def make(results, call_active=False, active_after_first=False):
        fake = FakePhone(results, call_active, active_after_first)
        monkeypatch.setattr(win_adapters, "phone", fake)
        logs = []
        w = win_adapters.WinPhone("hf_direct", "", lambda ev, s="": logs.append((ev, s)))
        return w, fake, logs
    return make, sleeps


def test_dial_retries_once_after_3s_on_phone_link_ui_failure(dialer, heal):
    make, sleeps = dialer
    w, fake, logs = make([{"ok": False, "error": "Phone Link dialer did not show the number"},
                          {"ok": True, "method": "invoke", "button": "Call"}])
    assert w.dial("01012345678") is True
    assert len(fake.calls) == 2 and fake.calls[0] == fake.calls[1] == ("dial", "01012345678")
    assert sleeps == [3.0]
    assert any(ev == "dial_retry" for ev, _ in logs)
    assert heal.reports == []           # the retry succeeded: nothing to report
    assert w.t_dial is not None


def test_dial_failing_twice_reports_not_linked(dialer, heal):
    make, sleeps = dialer
    err = {"ok": False, "error": "Phone Link dialer did not show the number"}
    w, fake, _ = make([dict(err), dict(err)])
    assert w.dial("01012345678") is False
    assert len(fake.calls) == 2 and sleeps == [3.0]
    assert w.last_error.startswith("phone_link_not_linked: ")
    assert heal.reports and heal.reports[0][0] == "phone_link_not_linked"
    assert w.t_dial is None


def test_dial_not_running_is_reported_as_phone_link_not_running(dialer, heal):
    make, _ = dialer
    w, fake, _ = make([{"ok": False, "error": "Phone Link is not running"},
                       {"ok": False, "error": "Phone Link is not running"}])
    assert w.dial("01012345678") is False
    assert heal.reports[0][0] == "phone_link_not_running"


def test_dial_does_not_retry_on_bad_number(dialer, heal):
    make, sleeps = dialer
    w, fake, _ = make([{"ok": False, "error": "bad number"}])
    assert w.dial("01012345678") is False
    assert len(fake.calls) == 1 and sleeps == []
    assert heal.reports == []


def test_dial_does_not_retry_on_timeout(dialer, heal):
    make, sleeps = dialer
    w, fake, _ = make([{"ok": False, "error": "plcall timeout"}])
    assert w.dial("01012345678") is False
    assert len(fake.calls) == 1 and sleeps == []


def test_dial_does_not_retry_when_a_call_is_already_up(dialer, heal):
    make, sleeps = dialer
    w, fake, _ = make([{"ok": False, "error": "Phone Link dialer did not show the number"}], active_after_first=True)
    assert w.dial("01012345678") is False
    assert len(fake.calls) == 1 and sleeps == []

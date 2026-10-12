"""connector/callhealth.py: post-call Bluetooth health (bt_recovering), the hang-up fallback, and phone._com_thread's MTA keeper.
The Windows phone module, the heal engine and micguard are replaced by fakes."""
import sys
import threading
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import callhealth, win_adapters  # noqa: E402


class FakeHeal:
    def __init__(self):
        self.reports, self.ring = [], []

    def report(self, failure, **detail):
        self.reports.append((failure, detail))

    def status(self):
        return {"enabled": True, "ok": True, "recent": list(self.ring)}   # top-level ok must not count as a success

    def succeed(self, action="hf_refresh", n=0):
        self.ring.append({"failure": "bt_unhealthy", "action": action, "ok": True, "ms": 10 + n})


class FakePhone:
    """The `phone` module: a call stays listed for `live_for` polls after end_calls (None = forever)."""

    def __init__(self, end_ok=True, end_raises=False, live_for=0, link_ok=True, link_ends=True):
        self.end_ok, self.end_raises, self.live_for = end_ok, end_raises, live_for
        self.link_ok, self.link_ends = link_ok, link_ends
        self.live = True
        self.polls_left = None
        self.link_calls = []
        self.end_called = 0
        self.call_active = True

    def end_calls(self):
        self.end_called += 1
        if self.end_raises:
            raise OSError("PhoneEnd failed")
        if self.end_ok:
            self.polls_left = self.live_for
            return {"ok": True, "ended": [{"id": 1, "state": "talking", "hr": "0x0"}], "left": []}
        return {"ok": False, "ended": [{"id": 1, "state": "talking", "hr": "0x80004005"}], "left": [{"id": 1, "state": "talking"}]}

    def phone_calls(self):
        if self.polls_left is not None:
            if self.live_for is not None and self.polls_left <= 0:
                self.live = False
            self.polls_left = None if self.live_for is None else self.polls_left - 1
        return [{"id": 1, "state": "talking" if self.live else "idle"}]

    async def phone_link(self, action, number=""):
        self.link_calls.append(action)
        if self.link_ends:
            self.live = False
            self.polls_left = None
            self.live_for = 0
        return {"ok": self.link_ok} if self.link_ok else {"ok": False, "error": "no end-call button in Phone Link"}

    def call_flags(self):
        return {"call_active": self.live}


@pytest.fixture
def env(monkeypatch, tmp_path):
    h = FakeHeal()
    callhealth.reset_for_tests()
    monkeypatch.setattr(callhealth, "heal", h)
    monkeypatch.setattr(callhealth, "LIVE_WAIT", 0.3)
    monkeypatch.setattr(callhealth, "POLL_S", 0.01)
    mg = types.SimpleNamespace(restores=[], mutes=[])
    mg.restore = lambda log, a, b, why: mg.restores.append(why) or {}
    mg.mute = lambda ids, log: mg.mutes.append(ids)
    monkeypatch.setattr(win_adapters, "micguard", mg)
    monkeypatch.setattr(win_adapters, "HOME", tmp_path)
    rd = types.SimpleNamespace(check=lambda mac, log: win_adapters.readiness.decorate({"ok": True, "reason": None}),
                               decorate=win_adapters.readiness.decorate)
    monkeypatch.setattr(win_adapters, "readiness", rd)
    yield types.SimpleNamespace(heal=h, mg=mg)
    callhealth.reset_for_tests()


def make_phone(monkeypatch, fp, **kw):
    monkeypatch.setattr(win_adapters, "phone", fp)
    return win_adapters.WinPhone("hf_direct", "02:00:00:00:00:01", lambda *a, **k: None)


# ---------------------------------------------------------------- recovery window
def test_window_closes_after_120s(env):
    assert callhealth.mark_unhealthy("audio_error", now=1000.0)
    assert env.heal.reports == [("bt_unhealthy", {"why": "audio_error"})]
    assert callhealth.recovering(now=1119.0) is True
    assert callhealth.recovering(now=1120.0) is False
    assert callhealth.recovering(now=1121.0) is False   # stays closed


def test_window_closes_on_first_successful_heal_action(env):
    env.heal.succeed("radio_on")                          # an older success must not count
    callhealth.mark_unhealthy("audio_error", now=0.0)
    assert callhealth.recovering(now=5.0) is True
    env.heal.ring.append({"failure": "bt_unhealthy", "action": "hf_refresh", "ok": False})   # a failed attempt does not count
    assert callhealth.recovering(now=6.0) is True
    env.heal.succeed("hf_refresh")
    assert callhealth.recovering(now=7.0) is False


def test_rolled_ring_still_detects_new_success(env):
    for i in range(3):
        env.heal.succeed(n=i)
    callhealth.mark_unhealthy("audio_error", now=0.0)
    env.heal.ring.pop(0)                                  # bounded ring: oldest entry rolls out as the new one comes in
    env.heal.succeed(n=99)
    assert callhealth.recovering(now=1.0) is False


def test_no_heal_engine_means_no_window(monkeypatch, env):
    monkeypatch.setattr(callhealth, "heal", None)
    assert callhealth.mark_unhealthy("audio_error") is False
    assert callhealth.recovering() is False


def test_report_error_means_no_window(env):
    env.heal.report = lambda *a, **k: 1 / 0
    logs = []
    assert callhealth.mark_unhealthy("audio_error", lambda *a, **k: logs.append(a)) is False
    assert callhealth.recovering() is False and logs


def test_apply_ready(env):
    callhealth.mark_unhealthy("audio_error", now=0.0)
    r = callhealth.apply_ready({"ok": True, "reason": None}, now=1.0)
    assert r["ok"] is False and r["reason"] == "bt_recovering" and r["reason_ar"] and r["reason_en"]
    r = callhealth.apply_ready({"ok": False, "reason": "bluetooth_off"}, now=1.0)
    assert r["reason"] == "bluetooth_off"                 # a more specific reason wins
    r = callhealth.apply_ready({"ok": True, "reason": None}, now=500.0)
    assert r["ok"] is True


# ---------------------------------------------------------------- WinPhone wiring
def test_no_call_audio_ends_with_audio_error_and_blocks_next_dial(monkeypatch, env):
    ph = make_phone(monkeypatch, types.SimpleNamespace(
        hf_endpoints=lambda: {}, phone_calls=lambda: [{"id": 1, "state": "talking"}], call_flags=lambda: {"call_active": True},
        audio_to_laptop=lambda: {"ok": True}))
    ph.t_dial = 100.0
    assert ph._before_audio(101.0) == "ringing"           # talking_since = 101
    assert env.heal.reports == []
    assert ph._before_audio(101.0 + win_adapters.NO_AUDIO_S + 1) == "ended"
    ph._flush_end()
    assert ph.end_hint() == "audio_error"
    assert [r[0] for r in env.heal.reports] == ["bt_unhealthy"]
    r = ph.ready()
    assert r["ok"] is False and r["reason"] == "bt_recovering" and r["reason_en"]
    env.heal.succeed()
    assert ph.ready()["ok"] is True                       # one heal action succeeded: the next dial may go


def test_audio_setup_failure_is_not_a_bluetooth_failure(monkeypatch, env):
    """_activate only runs once both Hands-Free endpoints are up: Bluetooth is fine, so no bt_unhealthy / bt_recovering,
    and last_error stays visible."""
    ph = make_phone(monkeypatch, types.SimpleNamespace(endpoints=lambda: []))
    ph.t_dial = 1.0
    monkeypatch.setattr(win_adapters.micguard, "mute", lambda *a: 1 / 0)
    assert ph._activate({"capture": {"id": "c", "format": "x"}, "render": {"id": "r", "format": "x"}}, 2.0) == "ended"
    ph._flush_end()
    assert env.heal.reports == [] and ph.hint == "audio_error" and ph.end_hint() == "audio_error"
    assert "ZeroDivisionError" in ph.last_error
    assert ph.ready()["ok"] is True and callhealth.recovering() is False


def test_missing_cables_is_not_a_bluetooth_failure(monkeypatch, env):
    ph = make_phone(monkeypatch, types.SimpleNamespace(cable_endpoints=lambda: []))
    ph.route, ph.t_dial = "cable", 1.0
    monkeypatch.setattr(win_adapters.win_audio, "find_cable_devices", lambda: (None, None, {}))
    assert ph._activate({"capture": {"id": "c"}, "render": {"id": "r"}}, 2.0) == "ended"
    ph._flush_end()
    assert env.heal.reports == [] and "cables missing" in ph.last_error


def test_hf_seen_resets_on_new_call(monkeypatch, env):
    ph = make_phone(monkeypatch, types.SimpleNamespace())
    ph.hf_seen = True
    ph._reset()
    assert ph.hf_seen is False and ph.pending_end is None


def test_restore_runs_before_health_report(monkeypatch, env):
    ph = make_phone(monkeypatch, types.SimpleNamespace())
    ph.t_dial, ph.stuck_logged = 1.0, True
    order = []
    ph._restore = lambda: order.append("restore")
    env.heal.report = lambda *a, **k: order.append("report")
    ph._end(None)
    ph._flush_end()
    assert order == ["restore", "report"]


def test_only_bt_unhealthy_success_closes_window(env):
    callhealth.mark_unhealthy("audio_error", now=0.0)
    env.heal.ring.append({"failure": "bluetooth_off", "action": "radio_on", "ok": True})
    assert callhealth.recovering(now=1.0) is True
    env.heal.succeed("hf_refresh")
    assert callhealth.recovering(now=2.0) is False


class LockyHeal:
    """Like the real heal engine: status() holds its lock while calling the in_call callback, report() takes the same lock."""

    def __init__(self, in_call):
        self.lock, self.in_call = threading.Lock(), in_call

    def report(self, failure, **detail):
        with self.lock:
            pass

    def status(self):
        with self.lock:
            self.in_call()
        return {}


def test_no_deadlock_with_lock_taking_in_call(monkeypatch, env):
    ph = make_phone(monkeypatch, types.SimpleNamespace(
        hf_endpoints=lambda: {}, phone_calls=lambda: [{"id": 1, "state": "talking"}], call_flags=lambda: {"call_active": True},
        audio_to_laptop=lambda: {"ok": True}))

    def in_call():
        with ph.lock:   # e.g. WinPhone.busy() grown a lock
            pass
    monkeypatch.setattr(callhealth, "heal", LockyHeal(in_call))
    ph._before_audio = lambda now: ph._end(None)
    stop, done = threading.Event(), []

    def heartbeat():   # heal.status() + ready() -> recovering(), as the heartbeat thread does
        while not stop.is_set():
            callhealth.heal.status()
            callhealth.recovering()

    def calls():
        for _ in range(300):
            with ph.lock:
                ph._reset()
                ph.t_dial = 100.0
                ph.stuck_logged = True
            assert ph.call_state() == "ended"   # _end under the phone lock, the health report outside it
            callhealth.recovering()
        done.append(1)
    hb = threading.Thread(target=heartbeat, daemon=True)
    cl = threading.Thread(target=calls, daemon=True)
    hb.start()
    cl.start()
    cl.join(10)
    stop.set()
    hb.join(5)
    assert done == [1] and not hb.is_alive(), "deadlock between the heartbeat and the call thread"


def test_hf_link_stuck_then_callee_gone_reports(monkeypatch, env):
    ph = make_phone(monkeypatch, types.SimpleNamespace())
    ph.t_dial, ph.stuck_logged = 1.0, True
    assert ph._end(None) == "ended"
    ph._flush_end()
    assert [r[0] for r in env.heal.reports] == ["bt_unhealthy"] and env.heal.reports[0][1]["why"] == "hf_link_stuck"


def test_stuck_but_audio_came_up_is_healthy(monkeypatch, env):
    ph = make_phone(monkeypatch, types.SimpleNamespace())
    ph.t_dial, ph.stuck_logged, ph.active = 1.0, True, True
    ph._end("callee_hangup")
    ph._flush_end()
    assert env.heal.reports == [] and ph.ready()["ok"] is True


@pytest.mark.parametrize("hint", ["callee_hangup", "call_ended", None])
def test_normal_call_end_is_healthy(monkeypatch, env, hint):
    ph = make_phone(monkeypatch, types.SimpleNamespace())
    ph.t_dial = 1.0
    ph._end(hint)
    ph._flush_end()
    assert env.heal.reports == [] and callhealth.recovering() is False


# ---------------------------------------------------------------- hang-up fallback
def test_hangup_clean_path_no_fallback(monkeypatch, env):
    fp = FakePhone(live_for=0)
    ph = make_phone(monkeypatch, fp)
    ph.t_dial = 1.0
    ph.hangup()
    assert fp.end_called == 1 and fp.link_calls == [] and env.heal.reports == []
    assert ph.ended and env.mg.restores == ["call_end"]


def test_hangup_end_calls_raises_uses_phone_link(monkeypatch, env):
    fp = FakePhone(end_raises=True)
    ph = make_phone(monkeypatch, fp)
    ph.t_dial = 1.0
    ph.hangup()
    assert fp.link_calls == ["hangup"]
    assert env.heal.reports[0][0] == "hangup_failed" and env.heal.reports[0][1]["recovered"] is True
    assert env.mg.restores == ["call_end"]


def test_hangup_call_still_live_after_wait_uses_phone_link(monkeypatch, env):
    fp = FakePhone(end_ok=True, live_for=None)            # PhoneEnd says ok but the call stays listed
    ph = make_phone(monkeypatch, fp)
    ph.t_dial = 1.0
    ph.hangup()
    assert fp.link_calls == ["hangup"] and [r[0] for r in env.heal.reports] == ["hangup_failed"]


def test_hangup_slow_end_within_wait_needs_no_fallback(monkeypatch, env):
    fp = FakePhone(end_ok=True, live_for=5)               # gone after 5 polls (0.05 s), well inside LIVE_WAIT
    ph = make_phone(monkeypatch, fp)
    ph.t_dial = 1.0
    ph.hangup()
    assert fp.link_calls == [] and env.heal.reports == []


def test_hangup_everything_fails_still_restores(monkeypatch, env):
    fp = FakePhone(end_ok=False, link_ok=False, link_ends=False)
    ph = make_phone(monkeypatch, fp)
    ph.t_dial, ph.switched = 1.0, True
    monkeypatch.setattr(win_adapters, "restore_defaults", lambda: True)
    ph.hangup()
    assert fp.link_calls == ["hangup"]
    assert env.heal.reports[0][0] == "hangup_failed" and env.heal.reports[0][1]["recovered"] is False
    assert ph.ended and env.mg.restores == ["call_end"] and ph.switched is False


def test_hangup_unreadable_state_is_not_recovered(monkeypatch, env):
    def boom():
        raise OSError("no state")
    fp = FakePhone(end_ok=False, link_ok=False)
    fp.phone_calls, fp.call_flags = boom, boom
    r = callhealth.hangup(fp, sleep=lambda s: None)
    assert r["live_after"] is True and env.heal.reports[-1][1]["recovered"] is False


def test_hangup_without_call_is_quiet(monkeypatch, env):
    class Idle(FakePhone):
        def end_calls(self):
            return {"ok": False, "ended": [], "left": []}   # what phone.end_calls returns when no call is listed

        def phone_calls(self):
            return [{"id": 1, "state": "idle"}]

        def call_flags(self):
            return {"call_active": False}
    fp = Idle()
    ph = make_phone(monkeypatch, fp)
    ph.hangup()
    assert fp.link_calls == [] and env.heal.reports == [] and ph.ended is False
    assert env.mg.restores == ["call_end"]


def test_hangup_without_phone_link_reports(env):
    class NoLink:
        live = True

        def end_calls(self):
            raise OSError("PhoneEnd failed")

        def phone_calls(self):
            return [{"id": 1, "state": "talking"}]
    r = callhealth.hangup(NoLink(), lambda *a, **k: None)
    assert r["live_after"] is True and r["fallback"]["ok"] is False and env.heal.reports[0][1]["recovered"] is False


def test_windows_says_call_active_while_phone_service_lists_none(env):
    class Gap(FakePhone):
        def phone_calls(self):
            return [{"id": 1, "state": "idle"}]
    fp = Gap(end_ok=True)                                 # call_active stays True: Phone Link button is tried
    r = callhealth.hangup(fp, lambda *a, **k: None)
    assert fp.link_calls == ["hangup"] and r["live_after"] is False   # plcall hangup "worked": FakePhone clears live


# ---------------------------------------------------------------- phone._com_thread MTA keeper
def load_phone(monkeypatch):
    names = ("winrt", "winrt.windows", "winrt.windows.applicationmodel", "winrt.windows.applicationmodel.calls",
             "winrt.windows.devices", "winrt.windows.devices.bluetooth")
    mods = {n: types.ModuleType(n) for n in names}
    mods["winrt.windows.applicationmodel"].calls = mods["winrt.windows.applicationmodel.calls"]
    mods["winrt.windows.devices.bluetooth"].BluetoothConnectionStatus = object
    mods["winrt.windows.devices.bluetooth"].BluetoothDevice = object
    com = types.ModuleType("comtypes")
    com.inits = []
    com.CoInitializeEx = lambda f: com.inits.append(threading.current_thread().name)
    mods["comtypes"] = com
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)
    monkeypatch.delitem(sys.modules, "connector.phone", raising=False)
    import importlib
    return importlib.import_module("connector.phone"), com


def test_com_thread_starts_one_mta_keeper(monkeypatch):
    ph, com = load_phone(monkeypatch)
    assert ph._com_thread(lambda c: 41 + 1) == 42
    assert ph._com_thread(lambda c: "again") == "again"
    keeper = ph._mta_keeper[0]
    assert len(ph._mta_keeper) == 1 and keeper.is_alive() and keeper.daemon and keeper.name == "mta-keeper"
    assert com.inits.count("mta-keeper") == 1 and com.inits.count("phone-com") == 2   # keeper joined before the first COM thread
    with pytest.raises(ValueError):
        ph._com_thread(lambda c: (_ for _ in ()).throw(ValueError("boom")))

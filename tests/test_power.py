"""Keep-awake (connector/power.py): execution-state flags, battery rule, clear on exit, and the App wiring. Windows API mocked.
Run: python -m pytest -q tests/test_power.py"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import power  # noqa: E402


def test_want_awake_rules():
    ac, low, ok, unk = (lambda: (False, 80)), (lambda: (True, 19)), (lambda: (True, 20)), (lambda: (None, None))
    assert power.want_awake(True, ac) and power.want_awake(True, ok) and power.want_awake(True, unk)
    assert not power.want_awake(True, low)                       # battery below 20%
    assert not power.want_awake(False, ac)                       # not connected / not ready
    assert power.want_awake(True, lambda: (False, 5))            # plugged in at 5% is fine


def test_flags_never_include_display():
    assert power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED == 0x80000001
    assert not (power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED) & 0x2     # ES_DISPLAY_REQUIRED


def _wait(cond, t=3.0):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_assert_follow_state_and_clear_on_exit_same_thread():
    calls, state = [], {"active": False, "bat": (False, 90)}

    def fake(flags):
        calls.append((flags, threading.current_thread().name))
        return 0

    ka = power.KeepAwake(lambda: state["active"], set_state=fake, battery=lambda: state["bat"], poll=0.01).start()
    try:
        time.sleep(0.1)
        assert calls == []                                       # not connected+ready: nothing asserted
        state["active"] = True
        assert _wait(lambda: len(calls) == 1) and calls[0][0] == 0x80000001
        state["bat"] = (True, 10)                                # unplugged and low: released
        assert _wait(lambda: len(calls) == 2) and calls[1][0] == power.ES_CONTINUOUS
        state["bat"] = (False, 10)
        assert _wait(lambda: len(calls) == 3) and calls[2][0] == 0x80000001
    finally:
        ka.stop()
    assert calls[-1][0] == power.ES_CONTINUOUS and not ka.held   # cleared on exit
    assert {n for _, n in calls} == {"keep-awake"}               # set and cleared on the one dedicated thread


def test_failure_is_logged_not_raised_and_retried():
    logs, n = [], {"i": 0}

    def flaky(flags):
        n["i"] += 1
        if n["i"] == 1:
            raise OSError("boom")
        return 0

    ka = power.KeepAwake(lambda: True, log=lambda *a: logs.append(a), set_state=flaky, battery=lambda: (False, 100), poll=0.01).start()
    try:
        assert _wait(lambda: ka.held)
    finally:
        ka.stop()
    assert any("failed" in str(x[0]) for x in logs)


def test_is_active_exception_releases():
    calls = []
    ka = power.KeepAwake(lambda: 1 / 0, set_state=lambda f: calls.append(f) or 0, battery=lambda: (False, 100), poll=0.01).start()
    time.sleep(0.1)
    ka.stop()
    assert calls == []


def test_app_keep_awake_active_needs_connected_and_ready(tmp_path):
    from connector import main
    app = main.App(tmp_path, "0.0.0", log=lambda *a: None)
    assert app.keep_awake_active() is False
    app.conn = type("C", (), {"_ws": None, "last_ready": {"ok": True}})()
    assert app.keep_awake_active() is False
    app.conn._ws = object()
    assert app.keep_awake_active() is True
    app.conn.last_ready = {"ok": False}
    assert app.keep_awake_active() is False
    app.conn.last_ready = None
    assert app.keep_awake_active() is False


def test_status_token_rejected(tmp_path):
    from connector import main
    app = main.App(tmp_path, "0.0.0", log=lambda *a: None)
    app.conn = type("C", (), {"_ws": None, "last_ready": None, "_call": None, "token_rejected": True})()
    st = app.status()
    assert st["state"] == "token_rejected" and "/" in st["message"] and not st["connected"]

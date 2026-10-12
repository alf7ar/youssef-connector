import json
import subprocess
import sys
import types

import pytest

from connector import repair


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


@pytest.fixture
def calls(monkeypatch):
    seen = []
    state = {"query": cp(0), "run": cp(0), "on_run": None}

    def fake(*args):
        seen.append(args)
        if args[0] == "/query":
            return state["query"]
        if state["on_run"]:
            state["on_run"]()
        return state["run"]

    monkeypatch.setattr(repair, "_schtasks", fake)
    return seen, state


def test_not_installed(calls, tmp_path):
    seen, state = calls
    state["query"] = cp(1, err="ERROR: The system cannot find the file specified.")
    r = repair.adapter_reset(path=tmp_path / "last.json")
    assert r == {"ok": False, "error": "not_installed"}
    assert [a[0] for a in seen] == ["/query"]          # never ran


def test_success_new_file(calls, tmp_path):
    seen, state = calls
    p = tmp_path / "last.json"
    p.write_text(json.dumps({"time": "T1", "steps": ["old"], "rc": 0}))
    clk = FakeClock()
    n = {"k": 0}
    orig_sleep = clk.sleep

    def sleep(s):
        n["k"] += 1
        if n["k"] == 2:        # the task finishes during polling
            p.write_text(json.dumps({"time": "T2", "steps": ["pnputil", "bthserv"], "rc": 0}))
        orig_sleep(s)

    r = repair.adapter_reset(path=p, sleep=sleep, clock=clk.now)
    assert r["ok"] is True and r["steps"] == ["pnputil", "bthserv"] and r["rc"] == 0
    assert seen[1] == ("/run", "/tn", "YoussefRepair")
    assert n["k"] == 2     # the stale last.json was not accepted


def test_success_no_previous_file(calls, tmp_path):
    seen, state = calls
    p = tmp_path / "last.json"
    state["on_run"] = lambda: p.write_text(json.dumps({"time": "T1", "steps": [], "rc": 0}))
    clk = FakeClock()
    r = repair.adapter_reset(path=p, sleep=clk.sleep, clock=clk.now)
    assert r["ok"] is True


def test_repair_reports_nonzero_rc(calls, tmp_path):
    seen, state = calls
    p = tmp_path / "last.json"
    state["on_run"] = lambda: p.write_text(json.dumps({"time": "T1", "steps": ["x"], "rc": 5}))
    clk = FakeClock()
    r = repair.adapter_reset(path=p, sleep=clk.sleep, clock=clk.now)
    assert r["ok"] is False and r["error"] == "repair_failed" and r["rc"] == 5


def test_half_written_file_keeps_polling(calls, tmp_path):
    seen, state = calls
    p = tmp_path / "last.json"
    state["on_run"] = lambda: p.write_text("{")
    clk = FakeClock()
    n = {"k": 0}

    def sleep(s):
        n["k"] += 1
        if n["k"] == 1:
            p.write_text(json.dumps({"time": "T1", "steps": [], "rc": 0}))
        clk.sleep(s)

    assert repair.adapter_reset(path=p, sleep=sleep, clock=clk.now)["ok"] is True


def test_timeout_when_file_never_changes(calls, tmp_path):
    seen, state = calls
    p = tmp_path / "last.json"
    p.write_text(json.dumps({"time": "T1", "steps": [], "rc": 0}))
    clk = FakeClock()
    r = repair.adapter_reset(path=p, sleep=clk.sleep, clock=clk.now)
    assert r["ok"] is False and r["error"] == "timeout" and r["seconds"] >= 90


def test_run_failed(calls, tmp_path):
    seen, state = calls
    state["run"] = cp(1, err="ERROR: Access is denied.")
    r = repair.adapter_reset(path=tmp_path / "last.json")
    assert r["ok"] is False and r["error"] == "run_failed" and "denied" in r["detail"]


def test_schtasks_missing_and_timeout(monkeypatch, tmp_path):
    def boom(*a):
        raise FileNotFoundError()
    monkeypatch.setattr(repair, "_schtasks", boom)
    assert repair.adapter_reset(path=tmp_path / "l.json")["error"] == "schtasks_missing"

    def slow(*a):
        raise subprocess.TimeoutExpired("schtasks", 20)
    monkeypatch.setattr(repair, "_schtasks", slow)
    assert repair.adapter_reset(path=tmp_path / "l.json")["error"] == "schtasks_timeout"


def test_schtasks_uses_no_window(monkeypatch):
    got = {}

    def fake_run(argv, **kw):
        got.update(argv=argv, kw=kw)
        return cp(0)
    monkeypatch.setattr(repair.subprocess, "run", fake_run)
    repair._schtasks("/query", "/tn", "YoussefRepair")
    assert got["argv"] == ["schtasks", "/query", "/tn", "YoussefRepair"]
    assert got["kw"]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_last_json_path_uses_programdata(monkeypatch):
    monkeypatch.setenv("PROGRAMDATA", "X")
    assert repair.last_json_path().parts[-3:] == ("YoussefConnector", "repair", "last.json")


def test_register_with_heal(monkeypatch):
    reg = []
    fake = types.ModuleType("connector.heal")
    fake.register_action = lambda name, fn, tier="safe": reg.append((name, fn, tier))
    monkeypatch.setitem(sys.modules, "connector.heal", fake)
    monkeypatch.setattr(sys.modules["connector"], "heal", fake, raising=False)
    assert repair.register() is True
    assert reg == [("adapter_reset", repair.adapter_reset, "heavy")]


def test_register_without_heal(monkeypatch):
    monkeypatch.setitem(sys.modules, "connector.heal", None)    # forces ImportError
    assert repair.register() is False

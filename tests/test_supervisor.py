"""--supervise: relaunch with backoff, stop on update / replaced / clean exit, ask guard.ok_to_restart() before each relaunch."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from connector import guard, main as cm, supervisor  # noqa: E402

STOP = cm.SUPERVISOR_STOP
V = "0.1.2"


class Fake:
    """Fake child + clock + waiter: `codes` = [(exit code, seconds the child ran), ...]; the clock only moves when the child runs."""

    def __init__(self, codes):
        self.codes, self.now, self.waits, self.cmds = list(codes), 0.0, [], []

    def run(self, cmd):
        self.cmds.append(cmd)
        code, ran = self.codes.pop(0)
        self.now += ran
        return code

    def wait(self, s):
        self.waits.append(s)

    def go(self, home, ok=lambda: (True, "ok"), argv=("--supervise",)):
        logs = []
        rc = supervisor.supervise(home, V, list(argv), STOP, log=logs.append, run=self.run, ok_to_restart=ok,
                                  wait=self.wait, clock=lambda: self.now)
        return rc, logs


def test_backoff_10_20_40_80_120_cap(tmp_path):
    f = Fake([(1, 1)] * 7 + [(cm.EXIT_UPDATE, 1)])
    rc, logs = f.go(tmp_path)
    assert rc == 0 and f.waits == [10, 20, 40, 80, 120, 120, 120]
    assert len(f.cmds) == 8 and "update" in logs[-1]


def test_backoff_resets_after_a_stable_run(tmp_path):
    f = Fake([(1, 1), (1, 1), (-9, 500), (1, 1), (0, 1)])
    f.go(tmp_path)
    assert f.waits == [10, 20, 10, 20]


@pytest.mark.parametrize("code", [0, cm.EXIT_UPDATE, cm.EXIT_REPLACED])
def test_stop_codes_do_not_relaunch(tmp_path, code):
    f = Fake([(code, 5)])
    rc, logs = f.go(tmp_path)
    assert rc == 0 and f.waits == [] and len(f.cmds) == 1 and "not relaunching" in logs[-1]


@pytest.mark.parametrize("code", [1, 3, -1, -9, 3221225477])
def test_crash_codes_relaunch(tmp_path, code):
    f = Fake([(code, 2), (0, 2)])
    f.go(tmp_path)
    assert len(f.cmds) == 2 and f.waits == [10]


def test_guard_refusal_stops_after_the_wait(tmp_path):
    f = Fake([(1, 1), (1, 1)])
    rc, logs = f.go(tmp_path, ok=lambda: (False, "update_installing"))
    assert rc == 0 and len(f.cmds) == 1 and f.waits == [10] and "update_installing" in logs[-1]


def test_child_command_drops_the_flag_and_keeps_others(monkeypatch):
    monkeypatch.setattr(guard, "frozen", lambda: True)
    assert supervisor.child_command(["--supervise", "--no-update"]) == [sys.executable, "--no-update"]
    monkeypatch.setattr(guard, "frozen", lambda: False)
    assert supervisor.child_command(["--supervise"]) == [sys.executable, "-m", "connector.main"]


def test_real_child_process_gets_the_supervised_env(tmp_path):
    script = tmp_path / "child.py"
    script.write_text("import os, sys\nsys.exit(11 if os.environ.get('YC_SUPERVISED') == '1' else 5)\n")
    assert supervisor.run_child([sys.executable, str(script)]) == cm.EXIT_REPLACED


def test_run_child_unstartable_returns_minus_one():
    assert supervisor.run_child(["/nonexistent/definitely-not-here"]) == -1


def test_main_refuses_second_supervisor_and_running_app(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(supervisor, "supervise", lambda *a, **k: called.append(1) or 0)
    monkeypatch.setattr(supervisor, "single_instance", lambda: False)
    assert supervisor.main(["--supervise"], tmp_path, V, lambda *a: None) == 0 and not called
    monkeypatch.setattr(supervisor, "single_instance", lambda: True)
    monkeypatch.setattr(guard, "instance_running", lambda: True)
    assert supervisor.main(["--supervise"], tmp_path, V, lambda *a: None) == 0 and not called
    monkeypatch.setattr(guard, "instance_running", lambda: False)
    assert supervisor.main(["--supervise"], tmp_path, V, lambda *a: None) == 0 and called == [1]


def test_main_dispatches_supervise_before_the_app_mutex(tmp_path, monkeypatch):
    monkeypatch.setenv("YC_HOME", str(tmp_path))
    seen = {}
    monkeypatch.setattr(supervisor, "main", lambda argv, home, ver, log, stop: seen.update(argv=argv, home=home, stop=stop) or 0)
    monkeypatch.setattr(cm, "single_instance", lambda: pytest.fail("the app mutex must not be taken by the supervisor"))
    assert cm.main(["--supervise", "--no-update"]) == 0
    assert seen["argv"] == ["--supervise", "--no-update"] and seen["home"] == tmp_path and seen["stop"] is cm.SUPERVISOR_STOP


def test_exit_codes_only_distinct_under_supervision(monkeypatch):
    monkeypatch.delenv("YC_SUPERVISED", raising=False)
    assert cm.exit_code(cm.EXIT_UPDATE) == 0 and cm.exit_code(cm.EXIT_REPLACED) == 0
    monkeypatch.setenv("YC_SUPERVISED", "1")
    assert cm.exit_code(cm.EXIT_UPDATE) == 10 and cm.exit_code(cm.EXIT_REPLACED) == 11
    assert set(STOP) == {0, 10, 11}


def test_main_returns_update_code_when_the_installer_launched(tmp_path, monkeypatch):
    monkeypatch.setenv("YC_HOME", str(tmp_path))
    monkeypatch.setenv("YC_SUPERVISED", "1")
    monkeypatch.setattr(cm, "single_instance", lambda: True)
    monkeypatch.setattr(cm.App, "serve", lambda self, port: 0)
    monkeypatch.setattr(cm.App, "try_update", lambda self: True)
    assert cm.main([]) == cm.EXIT_UPDATE
    monkeypatch.delenv("YC_SUPERVISED")
    assert cm.main([]) == 0


# ------------------------------------------------------------------ guard.ok_to_restart
def mk_backup(home, ver="0.1.1"):
    (home / "previous").mkdir(parents=True)
    (home / "previous" / "backup.json").write_text(json.dumps({"version": ver}))


def state(home, fd, ran=5, clean=False, now=1000.0):
    guard._write(home / "guard.json", {"version": V, "start": now, "alive": now + ran, "clean": clean, "fd": fd, "boot": 0})


def ok(home, **kw):
    return guard.ok_to_restart(home, V, now=kw.pop("now", 2000.0), boot=kw.pop("boot", 0), can_rollback=kw.pop("can_rollback", True))


def test_ok_when_nothing_is_wrong(tmp_path):
    assert ok(tmp_path) == (True, "ok")
    state(tmp_path, fd=0)
    assert ok(tmp_path) == (True, "ok")          # one fast death so far


def test_refuses_while_an_update_installs(tmp_path):
    guard._write(tmp_path / "update_state.json", {"version": "0.2.0", "ts": 1950.0})
    assert ok(tmp_path) == (False, "update_installing")
    guard._write(tmp_path / "update_state.json", {"version": "0.2.0", "ts": 1000.0})
    assert ok(tmp_path) == (True, "ok")
    (tmp_path / "update_state.json").write_text("{bad")
    assert ok(tmp_path) == (True, "ok")


def test_refuses_while_a_rollback_restore_is_staged(tmp_path):
    p = tmp_path / guard.PENDING_ROLLED
    p.write_text("{}")
    now = time.time()
    assert guard.ok_to_restart(tmp_path, V, now=now, boot=0, can_rollback=True) == (False, "rollback_pending")
    os.utime(p, (now - 1000, now - 1000))        # a failed restore leaves the file behind: stale after RESTORE_GRACE_S
    assert guard.ok_to_restart(tmp_path, V, now=now, boot=0, can_rollback=True) == (True, "ok")


def test_rollback_due_means_the_next_start_rolls_back(tmp_path):
    mk_backup(tmp_path)
    state(tmp_path, fd=1)                        # next start counts the 2nd fast death
    assert ok(tmp_path) == (True, "rollback_due")


def test_crash_loop_without_a_backup_gives_up(tmp_path):
    state(tmp_path, fd=guard.SUPERVISED_MAX_DEATHS - 1)
    assert ok(tmp_path) == (False, "crash_loop")
    state(tmp_path, fd=guard.SUPERVISED_MAX_DEATHS - 2)
    assert ok(tmp_path) == (True, "ok")
    mk_backup(tmp_path)                          # with a backup the rollback happens long before the cap
    state(tmp_path, fd=guard.SUPERVISED_MAX_DEATHS - 1)
    assert ok(tmp_path) == (True, "rollback_due")
    assert ok(tmp_path, can_rollback=False) == (False, "crash_loop")     # source run: nothing can roll back


def test_machine_restart_or_clean_exit_is_not_a_death(tmp_path):
    state(tmp_path, fd=9, clean=True)
    assert ok(tmp_path) == (True, "ok")
    guard._write(tmp_path / "guard.json", {"version": V, "start": 1000.0, "alive": 1005.0, "clean": False, "fd": 9, "boot": 100.0})
    assert ok(tmp_path, boot=100000.0) == (True, "ok")

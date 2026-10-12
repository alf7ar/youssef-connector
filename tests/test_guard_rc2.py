"""0.1.2-rc2 crash-loop guard fixes (Linux fakes): import-broken builds count their starts, a quick restart is still a death,
a restart / Windows shutdown is not, the restore helper checks robocopy, min_version understands release candidates."""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from connector import core, guard  # noqa: E402


@pytest.fixture(autouse=True)
def no_heartbeat_thread(monkeypatch):
    monkeypatch.setattr(guard, "start_heartbeat", lambda *a, **k: None)


def make_backup(home, ver="0.1.1"):
    (home / "previous").mkdir(parents=True)
    (home / "previous" / "backup.json").write_text(json.dumps({"version": ver}))


def die(home, after):
    st = guard._read(home / "guard.json")
    guard._write(home / "guard.json", {**st, "alive": st["start"] + after})


def test_begin_counts_starts_so_an_import_broken_build_rolls_back(tmp_path):
    home = tmp_path / "home"
    make_backup(home)
    guard._write(home / "guard.json", {"version": "0.1.1", "start": 0, "alive": 100, "clean": True, "fd": 0})   # v1 left clean
    spawned = []
    seen = []
    for i in range(1, 5):      # entry.py: begin(), then `from connector.main import main` raises: process exits, nothing else runs
        now = 10_000.0 + i * 300
        seen.append(guard.begin(home, "0.1.2", running=False, can_rollback=True, now=now, spawn=lambda c, e: spawned.append((c, e))))
        if seen[-1] == "rollback":
            break
        die(home, 1)
    assert seen == ["ok", "ok", "rollback"], seen         # the 3rd start sees two fast deaths in a row
    assert len(spawned) == 1 and isinstance(spawned[0][0], str) and "restore.cmd" in spawned[0][0]
    assert spawned[0][1]["YC_BACKUP"] == str(home / "previous")
    assert guard.pinned_bad(home) == "0.1.2"


def test_quick_restart_is_counted(tmp_path):
    home = tmp_path / "home"
    make_backup(home)
    out = []
    for i in range(3):         # restarted 20 s after each start (inside the old 30 s heartbeat blind spot), each lived 5 s
        out.append(guard.begin(home, "0.1.2", running=False, can_rollback=True, now=1000.0 + i * 20, spawn=lambda c, e: None))
        die(home, 5)
    assert out == ["ok", "ok", "rollback"]


def test_other_instance_running_records_nothing_and_never_rolls_back(tmp_path):
    home = tmp_path / "home"
    make_backup(home)
    for i in range(5):
        guard.begin(home, "0.1.2", running=False, can_rollback=False, now=1000.0 + i)
        die(home, 1)
    before = guard._read(home / "guard.json")
    assert guard.begin(home, "0.1.2", running=True, can_rollback=True, now=2000.0) == "other"
    assert guard._read(home / "guard.json") == before


def test_death_before_a_restart_of_the_machine_is_not_counted(tmp_path):
    home = tmp_path / "home"
    make_backup(home)
    for i in range(3):         # three starts, each killed by a reboot: every boot time is different
        boot = 500.0 + i * 3000
        assert guard.pending_deaths(guard._read(home / "guard.json"), "0.1.2", 1000.0 + i * 100, boot) == 0
        guard.record_start(home, "0.1.2", 1000.0 + i * 100, boot)
        die(home, 30)
    assert not guard.rollback_due(home, "0.1.2", 2000.0, boot=500.0 + 3 * 3000)
    # same boot, small clock drift: still a crash loop
    guard.record_start(home, "0.1.2", 3000.0, boot=9000.0)
    die(home, 10)
    guard.record_start(home, "0.1.2", 3100.0, boot=9010.0)
    die(home, 10)
    assert guard.rollback_due(home, "0.1.2", 3200.0, boot=9005.0)


def test_heartbeat_marks_the_run_clean_when_windows_is_shutting_down(tmp_path, monkeypatch):
    monkeypatch.undo()          # the real start_heartbeat
    monkeypatch.setattr(guard, "POLL_S", 0.01)
    home = tmp_path / "home"
    guard.record_start(home, "0.1.2", time.time(), boot=1.0)
    flag = {"down": False}
    stop = guard.start_heartbeat(home, is_shutting_down=lambda: flag["down"])
    time.sleep(0.1)
    assert not guard._read(home / "guard.json").get("clean")       # .get: on Windows a read can hit os.replace and see {}
    flag["down"] = True
    for _ in range(100):
        if guard._read(home / "guard.json").get("clean"):
            break
        time.sleep(0.02)
    stop.set()
    time.sleep(0.1)
    assert guard._read(home / "guard.json").get("clean") is True
    assert guard.pending_deaths(guard._read(home / "guard.json"), "0.1.2", time.time() + 5) == 0


def test_heartbeat_refreshes_alive(tmp_path, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(guard, "POLL_S", 0.01)
    monkeypatch.setattr(guard, "BEAT_S", 0.05)
    home = tmp_path / "home"
    guard.record_start(home, "0.1.2", 1.0, boot=1.0)
    stop = guard.start_heartbeat(home, is_shutting_down=lambda: False)
    time.sleep(0.3)
    stop.set()
    st = guard._read(home / "guard.json")
    assert st["alive"] > 1000 and st["clean"] is False


def test_restore_script_mirrors_and_checks_robocopy():
    sc = guard.RESTORE_SCRIPT
    assert "%YC_MODE%" in sc and "/PURGE" not in sc
    assert sc.index("robocopy") < sc.index("if %RC% GEQ 8 goto fail") < sc.index("move /y") < sc.index('start ""') < sc.index(":fail")
    assert sc.index("exit /b 0") < sc.index(":fail")                 # a failed restore does not start the bad build again
    assert "\r\n" in sc and "\n" not in sc.replace("\r\n", "")
    sc.encode("ascii")                                               # no path (accented / Arabic user names) inside the batch file


def test_restore_env_modes_and_unsafe_paths(tmp_path):
    home, app = tmp_path / "home", tmp_path / "app"
    e = guard.restore_env(home, home / "previous", app)
    assert e["YC_MODE"] == "/MIR" and e["YC_BACKUP"] == str(home / "previous") and e["YC_TARGET"] == str(app)
    assert e["YC_EXE"] == "YoussefConnector.exe" and e["YC_HOME"] == str(home)
    assert guard.restore_env(home, app / "previous", app)["YC_MODE"] == "/E"       # never purge the folder the backup lives in
    with pytest.raises(ValueError):
        guard.restore_env(home, tmp_path / 'a"b', app)


def test_restore_command_is_one_string_without_backslash_quotes(tmp_path):
    home = tmp_path / "home dir (x86)"
    cmd = guard.restore_command(home)
    assert isinstance(cmd, str) and '\\"' not in cmd                 # a str reaches Popen verbatim (no list2cmdline)
    assert cmd == f'cmd /d /e:on /v:off /s /c ""{home / "restore.cmd"}""'
    assert (home / "restore.cmd").read_bytes().decode("ascii").startswith("@echo off")


def test_entry_py_records_the_start_before_main_is_imported(tmp_path):
    """Run packaging/entry.py against a fake `connector` whose main cannot be imported (3 times): guard.json must exist and grow."""
    pkg = tmp_path / "x" / "connector"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "guard.py").write_text((ROOT / "connector" / "guard.py").read_text(encoding="utf-8"), encoding="utf-8")
    (pkg / "main.py").write_text("raise ImportError('broken build')\n")
    (tmp_path / "x" / "VERSION").write_text("0.9.9\n")
    home = tmp_path / "home"
    code = f"import sys,runpy; sys.argv=['entry.py']; runpy.run_path({str(ROOT / 'packaging' / 'entry.py')!r}, run_name='__main__')"
    for i in range(3):
        r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path / "x", capture_output=True, text=True,
                           env={"PYTHONPATH": str(tmp_path / "x"), "YC_HOME": str(home), "PATH": ""})
        assert r.returncode == 1 and "broken build" in r.stderr
    st = guard._read(home / "guard.json")
    assert st["version"] == "0.9.9" and st["clean"] is False and st["fd"] == 2     # 3rd start saw two deaths


def test_entry_py_no_longer_leaves_start_recording_to_main():
    assert "record_start" not in (ROOT / "connector" / "main.py").read_text(encoding="utf-8")
    assert "guard.begin" in (ROOT / "packaging" / "entry.py").read_text(encoding="utf-8")


def test_min_version_understands_release_candidates():
    assert core._vtuple("0.1.2-rc1") > core._vtuple("0.1.1")
    assert core._vtuple("0.1.2-rc1") < core._vtuple("0.1.2")
    assert core._vtuple("0.1.2-rc2") > core._vtuple("0.1.2-rc1")
    assert core._vtuple("0.1.2-rc1") >= core._vtuple("0.0.0") and core._vtuple("0.1.1") >= core._vtuple("0")


@pytest.mark.parametrize("version,min_version,outdated", [
    ("0.1.2-rc1", "0.1.1", False),      # the hub's MIN_CONNECTOR_VERSION 0.1.1 must not stop an rc connector (it parsed as 0.1.0)
    ("0.1.2-rc1", "0.1.2", True),       # an rc is older than its release
    ("0.1.2-rc2", "0.1.2-rc10", True),  # rc numbers compare as numbers
    ("0.1.2-rc10", "0.1.2-rc2", False),
    ("0.1.0", "0.1", False), ("0.1", "0.1.0", False), ("0.1.1", "0.1.1", False),   # plain versions unchanged
    ("0.1.1", "0.1.2", True), ("1.0.0", "0.9.9", False), ("0.1.10", "0.1.9", False), ("0.1.2", "0.1.2-rc1", False),
])
def test_welcome_min_version_is_rc_aware(version, min_version, outdated):
    conn = core.Connector("ws://x", "tok", None, None, None, version=version)
    asyncio.run(conn._on_control({"type": "welcome", "min_version": min_version}))
    assert (conn.stop_reason == "outdated") is outdated


def test_restore_robocopy_is_bounded_and_flags_spawn_hidden():
    assert "/R:10 /W:3" in guard.RESTORE_SCRIPT
    import inspect
    assert "0x08000000" in inspect.getsource(guard._spawn)          # CREATE_NO_WINDOW


def test_heartbeat_and_mark_clean_hammer_guard_json(tmp_path):
    import threading
    home = tmp_path
    guard.record_start(home, "0.1.2", 1000.0)
    errs, stop = [], threading.Event()

    def beats():
        while not stop.is_set():
            guard._beat(home)

    ts = [threading.Thread(target=beats) for _ in range(3)]
    for t in ts:
        t.start()
    try:
        for _ in range(2000):
            guard.mark_clean(home)
            st = guard._read(home / "guard.json")
            if not st.get("clean"):
                errs.append(st)
    finally:
        stop.set()
        for t in ts:
            t.join()
    assert not errs and guard._read(home / "guard.json")["clean"] is True

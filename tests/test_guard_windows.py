"""Runs the REAL rollback restore helper (restore.cmd via `cmd /d /s /c ""..""`, robocopy, start) on two temp folders. Windows only
(the windows-build.yml pytest step); Linux skips it and covers the same logic with the batch interpreter in test_update_safety.py.
Folder names have spaces, parentheses and a non-ASCII letter on purpose: that is what broke list2cmdline quoting."""
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from connector import guard  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "nt", reason="needs cmd.exe and robocopy")


def wait_for(pred, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.5)
    return False


def setup(tmp_path):
    base = tmp_path / "Program Files (x86) é"
    home, target, backup = base / "home dir", base / "app dir", base / "home dir" / "previous"
    marker = base / "started.txt"
    for d in (backup, target):
        d.mkdir(parents=True)
    (backup / "backup.json").write_text(json.dumps({"version": "0.1.1"}))
    (backup / "data.txt").write_text("old", encoding="utf-8")
    (backup / "start_old.cmd").write_bytes(f'@chcp 65001 >nul\r\n@echo started> "{marker}"\r\n'.encode("utf-8"))    # the harmless "old exe"
    (target / "data.txt").write_text("NEW", encoding="utf-8")
    (target / "added_by_bad_build.txt").write_text("x", encoding="utf-8")
    guard._write(home / "guard.json", {"version": "0.1.2", "start": 1, "alive": 5, "clean": False, "fd": 1})
    assert guard.rollback(home, "0.1.2", target=target, spawn=lambda c: None, log=lambda *a: None)    # stages the state files
    return home, target, backup, marker


def test_restore_helper_restores_old_files_and_starts_the_old_exe(tmp_path):
    home, target, backup, marker = setup(tmp_path)
    assert not (home / "rolled_back.json").exists()
    cmd = guard.restore_command(home, backup, target, exe="start_old.cmd")
    assert isinstance(cmd, str) and '\\"' not in cmd
    guard._spawn(cmd)
    assert wait_for(marker.exists), "the start line did not run the old exe"
    assert (target / "data.txt").read_text(encoding="utf-8") == "old"
    assert not (target / "added_by_bad_build.txt").exists()                 # /MIR removed what the bad build added
    assert (target / "start_old.cmd").exists()
    assert guard._read(home / "rolled_back.json")["to"] == "0.1.1"
    st = guard._read(home / "guard.json")
    assert st["version"] == "0.1.1" and st["clean"] is True and st["fd"] == 0
    assert not (home / guard.PENDING_GUARD).exists() and not (home / guard.PENDING_ROLLED).exists()


def test_failed_restore_leaves_state_untouched_and_logs(tmp_path):
    home, target, backup, marker = setup(tmp_path)
    before = guard._read(home / "guard.json")
    shutil.rmtree(backup)                                                    # robocopy: source missing -> exit 16
    guard._spawn(guard.restore_command(home, backup, target, exe="start_old.cmd"))
    log = home / "connector.log"
    assert wait_for(lambda: log.exists() and "update_rollback_failed" in log.read_text(errors="replace"))
    assert not marker.exists()
    assert (target / "data.txt").read_text(encoding="utf-8") == "NEW"
    assert guard._read(home / "guard.json") == before and before["fd"] == 1
    assert not (home / "rolled_back.json").exists()

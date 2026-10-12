"""Channels, no-update-during-call, crash-loop rollback (fakes only: local http.server, fake spawn/launch, tmp dirs)."""
import hashlib
import json
import os
import re
import sys
import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import guard, update  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def site(tmp_path):
    d = tmp_path / "site"
    d.mkdir()

    class H(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), partial(H, directory=str(d)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def publish(ver, manifest="latest.json"):
        exe = d / f"YoussefConnector-Setup-{ver}.exe"
        exe.write_bytes(b"fake installer " + ver.encode())
        sha = hashlib.sha256(exe.read_bytes()).hexdigest()
        (d / f"{exe.name}.sha256").write_text(f"{sha}  {exe.name}\n")
        (d / manifest).write_text(json.dumps({"version": ver, "url": f"{base}/{exe.name}", "sha256": sha}))
    publish.base = base
    yield publish
    srv.shutdown()


def run(site, tmp_path, local="0.1.0", **kw):
    launched = []
    r = update.try_update(local, tmp_path / "home", lambda *a: None, manifest_url=site.base + "/latest.json",
                          beta_url=site.base + "/latest-beta.json", launch=launched.append, tmpdir=tmp_path / "dl", **kw)
    return r, [p.name for p in launched]


def test_version_key_prerelease_order():
    k = update.version_key
    assert k("0.1.2-rc1") < k("0.1.2-rc2") < k("0.1.2") < k("0.1.3-rc1")
    assert update.is_newer("0.1.2-rc1", "0.1.1") and not update.is_newer("0.1.2-rc1", "0.1.2")


def test_stable_channel_ignores_beta(site, tmp_path):
    site("0.1.1")
    site("0.2.0-rc1", "latest-beta.json")
    assert run(site, tmp_path) == (True, ["YoussefConnector-Setup-0.1.1.exe"])


def test_beta_channel_takes_newer_prerelease(site, tmp_path):
    site("0.1.1")
    site("0.2.0-rc1", "latest-beta.json")
    assert run(site, tmp_path, channel="beta") == (True, ["YoussefConnector-Setup-0.2.0-rc1.exe"])


def test_beta_channel_prefers_stable_release_over_its_rc(site, tmp_path):
    site("0.2.0")
    site("0.2.0-rc1", "latest-beta.json")
    assert run(site, tmp_path, channel="beta") == (True, ["YoussefConnector-Setup-0.2.0.exe"])


def test_beta_channel_without_beta_manifest_falls_back(site, tmp_path):
    site("0.1.1")
    assert run(site, tmp_path, channel="beta") == (True, ["YoussefConnector-Setup-0.1.1.exe"])


def test_no_update_during_call(site, tmp_path):
    site("0.2.0")
    assert run(site, tmp_path, busy=lambda: True) == (False, [])
    assert not (tmp_path / "dl").exists()                # nothing even downloaded
    assert not update.update_attempted_recently("0.2.0", tmp_path / "home")
    assert run(site, tmp_path, busy=lambda: False)[0] is True


def test_call_starting_mid_download_postpones(site, tmp_path):
    site("0.2.0")
    seq = iter([False, True])
    assert run(site, tmp_path, busy=lambda: next(seq)) == (False, [])
    assert not update.update_attempted_recently("0.2.0", tmp_path / "home")


def test_workflow_prerelease_never_writes_latest_json():
    wf = (ROOT / ".github/workflows/windows-build.yml").read_text(encoding="utf-8")
    assert "latest-beta.json" in wf and "beta-channel" in wf and "--prerelease" in wf
    pre = wf[wf.index("if ($env:IS_PRERELEASE -eq 'true') {\n            # pre-release"):wf.index("} else {\n            $files")]
    assert "latest.json" not in re.sub(r"latest-beta\.json", "", pre)
    assert "YoussefConnector-Setup.exe" not in pre


# ---------------------------------------------------------------- crash-loop guard
def make_backup(home, ver="0.1.1"):
    (home / "previous").mkdir(parents=True)
    (home / "previous" / "YoussefConnector.exe").write_bytes(b"old")
    (home / "previous" / "backup.json").write_text(json.dumps({"version": ver}))


def test_backup_current_copies_app_dir(tmp_path):
    app = tmp_path / "app"
    (app / "_internal").mkdir(parents=True)
    (app / "YoussefConnector.exe").write_bytes(b"exe")
    (app / "_internal" / "x.dll").write_bytes(b"dll")
    home = tmp_path / "home"
    assert guard.backup_current(home, "0.1.1", src=app)
    assert (home / "previous" / "_internal" / "x.dll").read_bytes() == b"dll"
    assert guard.previous_version(home) == "0.1.1"
    assert guard.backup_current(home, "0.1.2", src=app) and guard.previous_version(home) == "0.1.2"   # replaces


def test_two_fast_deaths_roll_back(tmp_path):
    home = tmp_path / "home"
    make_backup(home)
    t = 1000.0
    for _ in range(2):        # start, die after 10 s (heartbeat stops), start again 60 s later
        assert not guard.rollback_due(home, "0.1.2", t)
        guard.record_start(home, "0.1.2", t)
        guard._write(home / "guard.json", {**guard._read(home / "guard.json"), "alive": t + 10})
        t += 60
    assert guard.rollback_due(home, "0.1.2", t)
    spawned, logs = [], []
    assert guard.rollback(home, "0.1.2", target=tmp_path / "app", spawn=lambda c, e: spawned.append((c, e)), log=logs.append)
    cmd, env = spawned[0]
    assert isinstance(cmd, str) and '\\"' not in cmd
    assert env["YC_BACKUP"] == str(home / "previous") and env["YC_TARGET"] == str(tmp_path / "app")
    assert not (home / "rolled_back.json").exists()       # written by restore.cmd after robocopy, not before
    assert logs and "update_rolled_back" in logs[0]
    assert guard.pinned_bad(home) == "0.1.2"
    run_restore_cmd(home / "restore.cmd", spawned[0][1], robocopy_rc=1)  # robocopy "files copied" = success
    assert json.loads((home / "rolled_back.json").read_text())["to"] == "0.1.1"
    assert not guard.rollback_due(home, "0.1.1", t)       # restored exe starts from a clean slate


def run_restore_cmd(script, env, robocopy_rc):
    """Minimal batch interpreter for the lines restore.cmd uses (Linux has no cmd.exe): robocopy exits with `robocopy_rc`."""
    lines = [l for l in script.read_bytes().decode("utf-8").split("\r\n") if l]
    done, i, errorlevel = [], 0, 0
    while i < len(lines):
        l = lines[i]
        i += 1
        for k, v in env.items():
            l = l.replace(f"%{k}%", v)
        if l.startswith("robocopy "):
            errorlevel = robocopy_rc
        elif l == "if errorlevel 8 goto fail" and errorlevel >= 8:
            i = lines.index(":fail")
        elif l.startswith("move /y "):
            src, dst = [x for x in l.split('"') if x.strip() and x != "move /y " and not x.startswith(" >")][:2]
            os.replace(src.replace("\\", "/"), dst.replace("\\", "/"))
        elif l.startswith("start "):
            done.append("start")
        elif l.startswith("echo "):
            done.append("log:" + ("failed" if "update_rollback_failed" in l else "restored"))
        elif l.startswith("exit"):
            break
    return done


def test_failed_restore_keeps_death_count_and_logs_no_rollback(tmp_path):
    home = tmp_path / "home"
    make_backup(home)
    t = 1000.0
    for _ in range(2):
        guard.record_start(home, "0.1.2", t)
        guard._write(home / "guard.json", {**guard._read(home / "guard.json"), "alive": t + 10})
        t += 60
    spawned = []
    guard.rollback(home, "0.1.2", target=tmp_path / "app", spawn=lambda c, e: spawned.append((c, e)), log=lambda *a: None)
    env = spawned[0][1]
    before = guard._read(home / "guard.json")
    assert run_restore_cmd(home / "restore.cmd", env, robocopy_rc=8) == ["log:failed"]       # no start, only the failure line
    assert guard._read(home / "guard.json") == before and before["fd"] == 1 and before["version"] == "0.1.2"
    assert not (home / "rolled_back.json").exists() and guard.rolled_back_info(home) == {}
    assert guard.rollback_due(home, "0.1.2", t)           # the next start tries the restore again
    assert run_restore_cmd(home / "restore.cmd", env, robocopy_rc=16) == ["log:failed"]


def test_one_fast_death_or_long_run_or_clean_exit_does_not_roll_back(tmp_path):
    home = tmp_path / "home"
    make_backup(home)
    guard.record_start(home, "0.1.2", 1000.0)
    guard._write(home / "guard.json", {**guard._read(home / "guard.json"), "alive": 1010.0})
    assert not guard.rollback_due(home, "0.1.2", 1100.0)            # 1 fast death so far
    guard.record_start(home, "0.1.2", 1100.0)
    guard._write(home / "guard.json", {**guard._read(home / "guard.json"), "alive": 1100.0 + 300})   # lived 300 s
    assert not guard.rollback_due(home, "0.1.2", 2000.0)
    guard.record_start(home, "0.1.2", 2000.0)
    guard.mark_clean(home)                                          # clean quit resets
    assert guard.pending_deaths(guard._read(home / "guard.json"), "0.1.2", 2005.0) == 0


def test_running_instance_is_not_a_death(tmp_path):
    home = tmp_path / "home"
    make_backup(home)
    guard.record_start(home, "0.1.2", 1000.0)
    before = guard._read(home / "guard.json")
    assert guard.begin(home, "0.1.2", running=True, can_rollback=True, now=1005.0) == "other"   # mutex held: nothing recorded
    assert guard._read(home / "guard.json") == before


def test_no_rollback_without_a_different_backup(tmp_path):
    home = tmp_path / "home"
    guard._write(home / "guard.json", {"version": "0.1.2", "start": 1, "alive": 5, "clean": False, "fd": 1})
    assert not guard.rollback_due(home, "0.1.2", 1000.0)            # no backup
    make_backup(home, "0.1.2")
    assert not guard.rollback_due(home, "0.1.2", 1000.0)            # backup is the same version


def test_pin_blocks_bad_version_but_not_newer(site, tmp_path):
    home = tmp_path / "home"
    guard._write(home / "pin.json", {"pinned": "0.1.1", "bad": "0.2.0"})
    site("0.2.0")
    assert run(site, tmp_path, local="0.1.1") == (False, [])
    site("0.2.1")
    assert run(site, tmp_path, local="0.1.1")[0] is True


def test_ready_dict_reports_rollback(tmp_path, monkeypatch):
    from connector import main as cm
    monkeypatch.setenv("YC_FAKE", "1")
    home = tmp_path / "home"
    guard._write(home / "rolled_back.json", {"from": "0.1.2", "to": "0.1.1"})
    app = cm.App(home, "0.1.1", lambda *a: None)
    app.cfg = {"url": "http://127.0.0.1:1", "token": "t"}
    import asyncio

    async def go():
        t = asyncio.create_task(app.run(False))
        for _ in range(40):
            await asyncio.sleep(0.05)
            if app.phone is not None:
                break
        r = app.phone.ready()
        app.stop_event.set()
        await t
        return r
    r = asyncio.run(go())
    assert r["update_rolled_back"] == {"from": "0.1.2", "to": "0.1.1"}

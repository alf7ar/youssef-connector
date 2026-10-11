"""Crash-loop guard for auto-updates (stdlib only: it must import even when the new build is broken).

Before an update installs, the running app dir is copied to <home>/previous/ (backup_current). Every real start records
<home>/guard.json {version, start, alive, clean, fd}; a heartbeat thread refreshes `alive`, a clean exit sets `clean`.
A start that finds the last run unclean and shorter than FAST_DEATH_S counts as a fast death; two consecutive fast deaths
of the same version restore the backup over the app dir (detached helper, after this process exits), pin the old version
(<home>/pin.json: the bad version and anything older is never offered again) and leave <home>/rolled_back.json, which the
restored app reports as `update_rolled_back` in its ready dict and log."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

FAST_DEATH_S = 120
LIVE_S = 30          # a heartbeat younger than this means another instance is still running (not a death)
BEAT_S = 5
LOOP_DEATHS = 2


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(p: Path, d: dict):
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def app_dir() -> Path:
    return Path(sys.executable).parent


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def pending_deaths(st: dict, version: str, now: float) -> int:
    """Consecutive fast deaths of `version` as seen at a start at time `now` (0 = healthy / unknown / another instance alive)."""
    if not st or st.get("version") != version:
        return 0
    if now - float(st.get("alive", 0)) < LIVE_S and not st.get("clean"):
        return 0
    if st.get("clean"):
        return 0
    n = int(st.get("fd", 0))
    if float(st.get("alive", 0)) - float(st.get("start", 0)) < FAST_DEATH_S:
        return n + 1
    return 0


def previous_version(home: Path) -> str:
    return str(_read(home / "previous" / "backup.json").get("version") or "")


def backup_current(home: Path, version: str, src: Path = None, log=print) -> bool:
    """Copy the running app dir to <home>/previous/ (replacing an older backup). Never raises."""
    try:
        src = src or app_dir()
        dst = home / "previous"
        tmp = home / "previous.new"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp)
        shutil.rmtree(dst, ignore_errors=True)
        os.replace(tmp, dst)
        _write(dst / "backup.json", {"version": version, "ts": time.time()})
        return True
    except Exception as e:  # noqa: BLE001
        log(f"guard: backup failed (update continues without rollback): {type(e).__name__}: {e}")
        return False


def restore_command(backup: Path, target: Path, exe: str = "YoussefConnector.exe") -> list:
    bad = '"' + "\r\n"
    if any(c in str(p) for p in (backup, target) for c in bad):
        raise ValueError("unsafe path")
    return ["cmd", "/c", f'ping -n 4 127.0.0.1 >nul & robocopy "{backup}" "{target}" /E /NFL /NDL /NJH /NJS /NP >nul'
                          f' & start "" "{target}\\{exe}"']


def _spawn(cmd: list):
    flags = (0x00000008 | 0x00000200) if os.name == "nt" else 0
    subprocess.Popen(cmd, creationflags=flags, close_fds=True)


def rollback_due(home: Path, version: str, now: float = None) -> bool:
    now = time.time() if now is None else now
    return (pending_deaths(_read(home / "guard.json"), version, now) >= LOOP_DEATHS
            and bool(previous_version(home)) and previous_version(home) != version
            and (home / "previous").is_dir())


def _filelog(home: Path):
    def log(*a):
        try:
            with open(home / "connector.log", "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a) + "\n")
        except OSError:
            pass
    return log


def rollback(home: Path, version: str, target: Path = None, spawn=_spawn, log=None) -> bool:
    """Restore the backup over the app dir and pin it. True = the caller must exit now (the helper restarts the old exe)."""
    log = log or _filelog(home)
    try:
        target = target or app_dir()
        prev = previous_version(home)
        log(f"update_rolled_back: {version} died within {FAST_DEATH_S}s on {LOOP_DEATHS} starts; restoring {prev}")
        _write(home / "pin.json", {"pinned": prev, "bad": version, "ts": time.time()})
        _write(home / "rolled_back.json", {"from": version, "to": prev, "ts": time.time()})
        _write(home / "guard.json", {"version": prev, "start": 0, "alive": 0, "clean": True, "fd": 0})
        spawn(restore_command(home / "previous", target))
        return True
    except Exception as e:  # noqa: BLE001
        log(f"guard: rollback failed: {type(e).__name__}: {e}")
        return False


def rolled_back_info(home: Path) -> dict:
    return _read(home / "rolled_back.json")


def pinned_bad(home: Path) -> str:
    """Version that must not be (re)installed, or ''."""
    return str(_read(home / "pin.json").get("bad") or "")


def record_start(home: Path, version: str, now: float = None):
    now = time.time() if now is None else now
    fd = pending_deaths(_read(home / "guard.json"), version, now)
    _write(home / "guard.json", {"version": version, "start": now, "alive": now, "clean": False, "fd": fd})


def _beat(home: Path):
    st = _read(home / "guard.json")
    if st and not st.get("clean"):
        st["alive"] = time.time()
        _write(home / "guard.json", st)


def start_heartbeat(home: Path, stop: threading.Event = None) -> threading.Event:
    stop = stop or threading.Event()

    def run():
        while not stop.wait(BEAT_S):
            _beat(home)
    threading.Thread(target=run, daemon=True).start()
    return stop


def mark_clean(home: Path):
    st = _read(home / "guard.json")
    if st:
        st["clean"] = True
        st["alive"] = time.time()
        _write(home / "guard.json", st)

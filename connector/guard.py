"""Crash-loop guard for auto-updates (stdlib only: it must import even when the new build is broken).

Before an update installs, the running app dir is copied to <home>/previous/ (backup_current). Every real start records
<home>/guard.json {version, start, alive, clean, fd, boot}; a heartbeat thread refreshes `alive`, a clean exit sets `clean`.
begin() runs from packaging/entry.py BEFORE `connector.main` is imported, so a build that cannot even import still counts its starts.
A start that finds the last run unclean and shorter than FAST_DEATH_S counts as a fast death, unless the machine was restarted
since (boot time changed) or the heartbeat thread saw Windows shutting down (it marks the run clean). Another running instance is
detected with the named mutex (not with the heartbeat age), so a quick restart is counted. Two consecutive fast deaths
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
BEAT_S = 5           # guard.json `alive` refresh period
POLL_S = 1           # how often the heartbeat thread looks at "Windows is shutting down"
LOOP_DEATHS = 2
BOOT_TOL_S = 120     # boot times further apart than this mean the machine restarted in between
MUTEX = "Global\\YoussefConnector"   # the single-instance mutex (main.single_instance uses this name)


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


def instance_running() -> bool:
    """True when another instance holds the single-instance mutex (Windows; elsewhere False)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenMutexW.restype = ctypes.c_void_p
        h = k32.OpenMutexW(0x00100000, False, MUTEX)      # SYNCHRONIZE
        if h:
            k32.CloseHandle(ctypes.c_void_p(h))
            return True
        return k32.GetLastError() == 5                    # ERROR_ACCESS_DENIED: it exists
    except Exception:  # noqa: BLE001
        return False


def boot_time() -> float:
    """Wall-clock time of the last Windows boot (None elsewhere). Fast Startup keeps the tick count across a shutdown, so this
    only tells restarts apart; shutting_down() covers the rest."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.GetTickCount64.restype = ctypes.c_ulonglong
        return time.time() - k32.GetTickCount64() / 1000.0
    except Exception:  # noqa: BLE001
        return None


def shutting_down() -> bool:
    """True while Windows is shutting down / logging the session off (SM_SHUTTINGDOWN)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetSystemMetrics(0x2000))
    except Exception:  # noqa: BLE001
        return False


def pending_deaths(st: dict, version: str, now: float, boot: float = None) -> int:
    """Consecutive fast deaths of `version` as seen at a start at time `now` (0 = healthy / unknown / clean exit / the machine
    restarted since the last run). The caller has checked that no other instance is running."""
    if not st or st.get("version") != version or st.get("clean"):
        return 0
    if boot and st.get("boot") and abs(float(boot) - float(st["boot"])) > BOOT_TOL_S:
        return 0       # killed by a restart / shutdown, not a crash
    if float(st.get("alive", 0)) - float(st.get("start", 0)) < FAST_DEATH_S:
        return int(st.get("fd", 0)) + 1
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
    """Helper that waits for this process to exit, mirrors the backup over the app dir (/MIR: files the bad version added are
    removed too; /E when the backup lives inside the target, so it is never purged) and starts the old exe only when
    robocopy succeeded (its exit codes 0-7 are success, 8+ failure)."""
    bad = '"' + "\r\n"
    if any(c in str(p) for p in (backup, target) for c in bad):
        raise ValueError("unsafe path")
    mode = "/E" if Path(backup).is_relative_to(Path(target)) else "/MIR"
    return ["cmd", "/c", f'ping -n 4 127.0.0.1 >nul & robocopy "{backup}" "{target}" {mode} /NFL /NDL /NJH /NJS /NP >nul'
                          f' & if errorlevel 8 (exit /b 1) else (start "" "{target}\\{exe}")']


def _spawn(cmd: list):
    flags = (0x00000008 | 0x00000200) if os.name == "nt" else 0
    subprocess.Popen(cmd, creationflags=flags, close_fds=True)


def rollback_due(home: Path, version: str, now: float = None, boot: float = None) -> bool:
    now = time.time() if now is None else now
    boot = boot_time() if boot is None else boot
    return (pending_deaths(_read(home / "guard.json"), version, now, boot) >= LOOP_DEATHS
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


def record_start(home: Path, version: str, now: float = None, boot: float = None):
    now = time.time() if now is None else now
    boot = boot_time() if boot is None else boot
    fd = pending_deaths(_read(home / "guard.json"), version, now, boot)
    _write(home / "guard.json", {"version": version, "start": now, "alive": now, "clean": False, "fd": fd, "boot": boot or 0})


def _beat(home: Path):
    st = _read(home / "guard.json")
    if st and not st.get("clean"):
        st["alive"] = time.time()
        _write(home / "guard.json", st)


def start_heartbeat(home: Path, stop: threading.Event = None, is_shutting_down=None) -> threading.Event:
    """Refresh `alive` every BEAT_S; when Windows starts shutting down, mark the run clean (the kill that follows is no crash)."""
    stop = stop or threading.Event()
    down = is_shutting_down or shutting_down

    def run():
        n = 0
        while not stop.wait(POLL_S):
            if down():
                mark_clean(home)
                continue
            n += 1
            if n * POLL_S >= BEAT_S:
                n = 0
                _beat(home)
    threading.Thread(target=run, daemon=True).start()
    return stop


def begin(home: Path, version: str, running: bool = None, can_rollback: bool = None, now: float = None, spawn=_spawn) -> str:
    """The first thing a start does (packaging/entry.py, before `connector.main` is imported). Returns
    "other"    another instance is running: nothing is recorded (main() exits at once on the mutex),
    "rollback" two fast deaths in a row: the restore helper was started, the caller must exit now,
    "ok"       this start is recorded and the heartbeat runs."""
    if instance_running() if running is None else running:
        return "other"
    now = time.time() if now is None else now
    if (frozen() if can_rollback is None else can_rollback) and rollback_due(home, version, now) \
            and rollback(home, version, spawn=spawn):
        return "rollback"
    record_start(home, version, now)
    start_heartbeat(home)
    return "ok"


def mark_clean(home: Path):
    st = _read(home / "guard.json")
    if st:
        st["clean"] = True
        st["alive"] = time.time()
        _write(home / "guard.json", st)

"""`--supervise`: keep the app running. Starts the app (this same exe, no flag) as a child and relaunches it when it exits
unexpectedly (crash, kill, fatal error), waiting 10, 20, 40, 80, 120 s (cap) between relaunches; the wait restarts at 10 s after a
child that ran at least STABLE_S. It stops, without relaunching, when the child exits for an update, because another connector
replaced it, or cleanly (exit codes in main.py; the child only uses the distinct codes when YC_SUPERVISED=1).

guard.py keeps the crash-loop death count and the rollback: before every relaunch the supervisor asks guard.ok_to_restart()
(never during an update install or a staged rollback restore, and not forever in a crash loop that cannot be rolled back). The
relaunched child's own guard.begin() records the start and performs the rollback after two fast deaths. The supervisor itself
records nothing in guard.json (packaging/entry.py skips the guard for --supervise) and holds a different mutex than the app,
so the child still passes the app's single-instance check. Stdlib only, no heavy imports: it must stay alive when the app is broken.
It never holds files longer than needed: it exits the moment the child does for update / rollback, so the installer and
restore.cmd can replace the exe."""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import guard

MUTEX = "Global\\YoussefConnectorSupervisor"
MIN_BACKOFF_S = 10
MAX_BACKOFF_S = 120
STABLE_S = guard.FAST_DEATH_S      # a child that lived this long was healthy: the next relaunch waits MIN_BACKOFF_S again
ENV_FLAG = "YC_SUPERVISED"


def single_instance() -> bool:
    """True if we are the only supervisor (Windows named mutex; other OS: always True)."""
    if os.name != "nt":
        return True
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    h = k32.CreateMutexW(None, False, MUTEX)
    if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        k32.CloseHandle(ctypes.c_void_p(h))
        return False
    single_instance.handle = h
    return True


def child_command(argv: list) -> list:
    """The app's own command line without --supervise: the frozen exe itself, or `python -m connector.main` from source."""
    rest = [a for a in argv if a != "--supervise"]
    if guard.frozen():
        return [sys.executable, *rest]
    return [sys.executable, "-m", "connector.main", *rest]


def run_child(cmd: list) -> int:
    """Start the app and wait for it. Returns its exit code (-1 when it could not be started)."""
    flags = 0x08000000 if os.name == "nt" else 0       # CREATE_NO_WINDOW
    try:
        return subprocess.Popen(cmd, creationflags=flags, env={**os.environ, ENV_FLAG: "1"}).wait()
    except OSError:
        return -1


def supervise(home: Path, version: str, argv: list, stop_codes: dict, log=print, run=None, ok_to_restart=None,
              wait=None, clock=time.monotonic, stop: threading.Event = None) -> int:
    """The relaunch loop. `run(cmd)->exit code`, `ok_to_restart()->(bool, reason)`, `wait(seconds)` are injectable for tests.
    stop_codes = {exit code: why}: the child exits that mean "do not relaunch". Returns the supervisor's own exit code (0)."""
    stop = stop or threading.Event()
    run = run or run_child
    wait = wait or stop.wait
    ok_to_restart = ok_to_restart or (lambda: guard.ok_to_restart(home, version))
    cmd = child_command(argv)
    backoff = MIN_BACKOFF_S
    while not stop.is_set():
        t0 = clock()
        code = run(cmd)
        ran = clock() - t0
        if code in stop_codes:
            log(f"supervisor: app exited with {code} ({stop_codes[code]}) after {ran:.0f}s; not relaunching")
            return 0
        if ran >= STABLE_S:
            backoff = MIN_BACKOFF_S
        log(f"supervisor: app exited unexpectedly (code {code}) after {ran:.0f}s; relaunching in {backoff}s")
        wait(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_S)
        ok, why = ok_to_restart()
        if not ok:
            log(f"supervisor: guard refuses a relaunch ({why}); stopping")
            return 0
        if why != "ok":
            log(f"supervisor: guard says {why}")
    return 0


def main(argv: list, home: Path, version: str, log=print, stop_codes: dict = None) -> int:
    """Entry for `--supervise`. Exits 0 at once when another supervisor or an unsupervised app instance is already running."""
    if not single_instance():
        return 0
    if guard.instance_running():
        log("supervisor: the app is already running (unsupervised); not starting a second copy")
        return 0
    log(f"supervisor: started for {version}")
    try:
        return supervise(home, version, argv, stop_codes or {0: "clean exit"}, log=log)
    except KeyboardInterrupt:
        return 0

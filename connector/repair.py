"""User-side trigger for the elevated repair task (installer registers the SYSTEM scheduled task "YoussefRepair").

The task script restarts the Bluetooth adapter + services and writes %ProgramFiles%\\YoussefConnector\\repair\\last.json
(time, steps, rc; the folder is admin-only writable, Users may read). This module only starts the task (schtasks /run,
no elevation needed: the task grants Users run rights) and waits for a NEW last.json. Registered with connector.heal as the heavy action "adapter_reset".
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from pathlib import Path

TASK = "YoussefRepair"
POLL_TIMEOUT = 90.0
POLL_EVERY = 2.0
SCHTASKS_TIMEOUT = 20


def last_json_path() -> Path:
    # 64-bit Program Files (the installer runs in 64-bit mode); ProgramW6432 also covers a 32-bit interpreter
    base = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles") or r"C:\Program Files"
    return Path(base) / "YoussefConnector" / "repair" / "last.json"


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, timeout=SCHTASKS_TIMEOUT,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _snapshot(path: Path) -> tuple:
    """(mtime, raw time field) of last.json, (None, None) when missing/unreadable."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return (None, None)
    try:
        t = json.loads(path.read_text(encoding="utf-8-sig")).get("time")
    except Exception:  # noqa: BLE001
        t = None
    return (mtime, t)


def _read_new(path: Path, before: tuple) -> dict | None:
    """Parsed last.json if it is newer than `before` (by mtime or by its time field) and valid JSON, else None."""
    mtime, t = _snapshot(path)
    if mtime is None:
        return None
    if before[0] is not None and mtime <= before[0] and t == before[1]:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return None            # half-written: poll again
    return data if isinstance(data, dict) else None


def adapter_reset(*, path: Path | None = None, timeout: float = POLL_TIMEOUT, poll_every: float = POLL_EVERY,
                  sleep=time.sleep, clock=time.monotonic) -> dict:
    """Run the elevated repair task and wait for it to finish. -> dict(ok, error?, steps?, rc?, seconds?)."""
    path = path or last_json_path()
    try:
        q = _schtasks("/query", "/tn", TASK)
    except FileNotFoundError:
        return {"ok": False, "error": "schtasks_missing"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "schtasks_timeout"}
    if q.returncode != 0:
        return {"ok": False, "error": "not_installed"}

    before = _snapshot(path)
    t0 = clock()
    try:
        r = _schtasks("/run", "/tn", TASK)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "schtasks_timeout"}
    except OSError as e:
        return {"ok": False, "error": f"schtasks_failed: {e}"}
    if r.returncode != 0:
        return {"ok": False, "error": "run_failed", "rc": r.returncode,
                "detail": ((r.stderr or "") + (r.stdout or "")).strip()[-200:]}

    while True:
        data = _read_new(path, before)
        if data is not None:
            rc = data.get("rc")
            ok = rc in (None, 0)
            out = {"ok": ok, "steps": data.get("steps"), "rc": rc, "seconds": round(clock() - t0, 1)}
            if not ok:   # rc 2: no Bluetooth adapter present (Get-PnpDevice found none): nothing to restart, not a failure of the task
                out["error"] = "no_adapter" if rc == 2 else "repair_failed"
            return out
        if clock() - t0 >= timeout:
            return {"ok": False, "error": "timeout", "seconds": round(clock() - t0, 1)}
        sleep(poll_every)


def register() -> bool:
    """Register adapter_reset with connector.heal (heavy tier). False when heal is not available."""
    try:   # import_module: sys.modules wins over the package attribute (tests swap the module there)
        heal = importlib.import_module(f"{__package__ or 'connector'}.heal")
    except ImportError:
        return False
    heal.register_action("adapter_reset", adapter_reset, tier="heavy")
    return True

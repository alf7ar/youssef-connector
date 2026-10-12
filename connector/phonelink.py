"""Phone Link as a dial dependency: start it when it is not running (heal action phone_link_launch), and classify the
plcall.ps1 failures of a dial so WinPhone.dial can retry a Phone Link UI failure once and report the reason
(phone_link_not_running / phone_link_not_linked) to heal.

Windows-only pieces (explorer, tasklist) are plain subprocess calls; tests mock subprocess and the process check."""
import importlib
import subprocess
import time

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
APP_ID = r"shell:AppsFolder\Microsoft.YourPhone_8wekyb3d8bbwe!App"
PROCESS = "PhoneExperienceHost.exe"
LAUNCH_WAIT = 20.0      # s to wait for the Phone Link process after launching it
LAUNCH_POLL = 1.0       # s between two process checks
RETRY_S = 3.0           # s before the one retry of a dial that failed on a Phone Link UI failure

REASON_NOT_RUNNING = "phone_link_not_running"
REASON_NOT_LINKED = "phone_link_not_linked"
# plcall.ps1 error texts that mean the Phone Link UI did not place the call (the number itself was fine)
_NOT_LINKED_MARKERS = ("dialer did not show the number", "did not start the call", "no Calls UI")
_NOT_RUNNING_MARKER = "Phone Link is not running"


def _sibling(name):
    """Sibling module (package or flat import); None when it cannot be imported."""
    try:
        return importlib.import_module("." + name, __package__) if __package__ else importlib.import_module(name)
    except ImportError:
        return None


heal = _sibling("heal")          # owner H1's module; None until it is merged
readiness = _sibling("readiness")


def running() -> bool:
    """True when the Phone Link process runs (False also when that cannot be told)."""
    return readiness._tasklist_has(PROCESS) is True


def launch() -> dict:
    """Start Phone Link (no window) when it is not running, then wait up to LAUNCH_WAIT s for its process.
    Returns {ok, launched, running[, error]}. Blocking; heal runs it in its own thread."""
    if running():
        return {"ok": True, "launched": False, "running": True}
    subprocess.Popen(["explorer.exe", APP_ID], creationflags=CREATE_NO_WINDOW)
    t0 = time.monotonic()
    while time.monotonic() - t0 < LAUNCH_WAIT:
        time.sleep(LAUNCH_POLL)
        if running():
            return {"ok": True, "launched": True, "running": True, "secs": round(time.monotonic() - t0, 1)}
    return {"ok": False, "launched": True, "running": False, "error": f"{PROCESS} did not start in {LAUNCH_WAIT:.0f} s"}


def reason_for(error) -> str | None:
    """Reason code of a plcall.ps1 dial failure text: phone_link_not_running, phone_link_not_linked, or None for any other
    failure (bad number, a timeout, ...). Only the first two are Phone Link UI failures that a dial retries."""
    e = str(error or "")
    if _NOT_RUNNING_MARKER in e:
        return REASON_NOT_RUNNING
    if any(m in e for m in _NOT_LINKED_MARKERS):
        return REASON_NOT_LINKED
    return None


def report(reason: str, **detail) -> None:
    """Tell heal about a Phone Link failure (no-op until heal is merged)."""
    if heal is not None:
        heal.report(reason, **detail)


if heal is not None:
    heal.register_action("phone_link_launch", launch, tier="safe")

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
RETRY_MAX_ELAPSED_S = 5.0    # a failure later than this after the dial request is never retried (hub ack_timeout is 15 s)
RETRY_START_DEADLINE_S = 8.0   # the retry must start before this long after the dial request (after launch + wait)

REASON_NOT_RUNNING = "phone_link_not_running"
REASON_NOT_LINKED = "phone_link_not_linked"
# plcall.ps1 error texts that mean the Phone Link UI did not place the call (the number itself was fine)
_NOT_LINKED_MARKERS = ("dialer did not show the number", "did not start the call", "no Calls UI")
# the failures that happen BEFORE the Call button is pressed: only these may be retried. "did not start the call" comes after
# the press (the phone may already be ringing), so retrying it could ring the customer twice.
_PRE_PRESS_MARKERS = ("dialer did not show the number", "no Calls UI")
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
    An unknown process state (tasklist failed) is not "not running": nothing is launched then.
    Returns {ok, launched, running[, error]}. Blocking; heal runs it in its own thread."""
    state = readiness._tasklist_has(PROCESS)
    if state is True:
        return {"ok": True, "launched": False, "running": True}
    if state is None:
        return {"ok": False, "launched": False, "running": None, "error": "Phone Link process state unknown"}
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


def retryable(error) -> bool:
    """True when a dial failure happened before the Call button was pressed (Phone Link not running, or its dialer/Calls UI
    not found), so a second attempt cannot ring the callee twice. Any failure after the press is never retried."""
    e = str(error or "")
    return _NOT_RUNNING_MARKER in e or any(m in e for m in _PRE_PRESS_MARKERS)


def report(reason: str, **detail) -> None:
    """Tell heal about a Phone Link failure (no-op until heal is merged)."""
    if heal is not None:
        heal.report(reason, **detail)


if heal is not None:
    heal.register_action("phone_link_launch", launch, tier="safe")

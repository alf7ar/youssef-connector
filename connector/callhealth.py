"""Post-call Bluetooth health and the hang-up fallback.

1. A call that ends because its audio never came up (audio_error, hf_link_stuck logged, NO_AUDIO_S hit) leaves the iPhone
   Hands-Free link in a state where the NEXT dial is doomed. on_call_end() reports "bt_unhealthy" to the heal engine and starts
   a recovery window: ready() answers {ok: False, reason: "bt_recovering"} (apply_ready) until one heal action succeeded or
   RECOVER_S passed. Without a heal engine nothing could fix the link, so no window is opened.
2. hangup(): phone.end_calls(); when it fails, or the call is still live LIVE_WAIT s later, the Phone Link end-call button
   (plcall.ps1 hangup) is tried and "hangup_failed" is reported to the heal engine (report-only there).

Pure logic over the `phone` module passed in (nothing Windows-specific is imported here)."""
import asyncio
import importlib
import json
import threading
import time


def _sibling(name):
    """Sibling module (package or flat import); None when it cannot be imported."""
    try:
        return importlib.import_module("." + name, __package__) if __package__ else importlib.import_module(name)
    except ImportError:
        return None


heal = _sibling("heal")      # owner H1; None until merged (tests assign a stub)

RECOVER_S = 120.0            # s ready() says bt_recovering at most
LIVE_WAIT = 3.0              # s a call may stay live after PhoneEnd before the fallback runs
POLL_S = 0.25
REASON = "bt_recovering"
TEXT_AR = "بجهّز البلوتوث بعد مكالمة فشل صوتها. استنى شوية وجرّب تاني."
TEXT_EN = "Recovering Bluetooth after a call whose audio failed. Wait a moment and try again."

_lock = threading.Lock()
_st = {"since": None, "baseline": frozenset(), "why": ""}


def reset_for_tests():
    with _lock:
        _st.update(since=None, baseline=frozenset(), why="")


def _ok_attempts(node, out=None, top=True) -> frozenset:
    """Signatures of every nested {action, ok: True, ...} record in a heal.status() dict (its shape is the heal engine's
    business; only 'a dict with an "action" and ok True' is relied on)."""
    out = set() if out is None else out
    if isinstance(node, dict):
        if not top and node.get("ok") is True and "action" in node:
            out.add(json.dumps(node, sort_keys=True, default=str))
        for v in node.values():
            _ok_attempts(v, out, False)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _ok_attempts(v, out, False)
    return frozenset(out)


def _heal_ok_set() -> frozenset:
    try:
        return _ok_attempts(heal.status())
    except Exception:  # noqa: BLE001
        return frozenset()


def mark_unhealthy(why: str, log=None, now: float = None) -> bool:
    """Report bt_unhealthy to the heal engine and open the recovery window. False when there is no heal engine."""
    if heal is None:
        return False
    now = time.monotonic() if now is None else now
    try:
        heal.report("bt_unhealthy", why=why)
    except Exception as e:  # noqa: BLE001
        if log:
            log("bt_unhealthy_report_failed", error=f"{type(e).__name__}: {e}"[:150])
        return False
    with _lock:
        _st.update(since=now, baseline=_heal_ok_set(), why=why)
    if log:
        log("bt_recovering", why=why, window_s=RECOVER_S)
    return True


def recovering(now: float = None) -> bool:
    """True from mark_unhealthy() until a heal action succeeded after it or RECOVER_S passed (then the window closes)."""
    now = time.monotonic() if now is None else now
    with _lock:
        since = _st["since"]
        if since is None:
            return False
        if now - since >= RECOVER_S or (_heal_ok_set() - _st["baseline"]):
            _st["since"] = None
            return False
        return True


def apply_ready(ready: dict, now: float = None) -> dict:
    """While recovering, a ready dict that says ok becomes {ok: False, reason: "bt_recovering"} (a more specific reason of the
    readiness check, and a running call, are left as they are). Modified in place and returned."""
    if ready.get("ok") and recovering(now):
        ready["ok"], ready["reason"] = False, REASON
        ready["reason_ar"], ready["reason_en"] = TEXT_AR, TEXT_EN
    return ready


def on_call_end(hint, stuck: bool, audio_was_up: bool, log=None) -> bool:
    """Called once when a call ends. Unhealthy: the call ended with audio_error, or hf_link_stuck was logged and the call
    audio never came up. True when the recovery window was opened."""
    if hint == "audio_error":
        return mark_unhealthy("audio_error", log)
    if stuck and not audio_was_up:
        return mark_unhealthy("hf_link_stuck", log)
    return False


# ---------------------------------------------------------------- hang-up
def _live(ph):
    """True when the Phone Service lists a live call OR Windows says a call is active (the two disagree for a moment at times);
    None when neither can be read."""
    known = False
    try:
        known = True
        if any(c["state"] not in ("idle", "ambiguous") for c in ph.phone_calls()):
            return True
    except Exception:  # noqa: BLE001
        known = False
    try:
        a = ph.call_flags().get("call_active")
    except Exception:  # noqa: BLE001
        a = None
    if a is True:
        return True
    return False if (known or a is False) else None


def _still_live(ph, wait: float, sleep, clock) -> bool:
    """True when the call is still live after polling for up to `wait` s (returns as soon as it is gone or unknown)."""
    end = clock() + wait
    while _live(ph):
        if clock() >= end:
            return True
        sleep(POLL_S)
    return False


def hangup(ph, log=None, sleep=time.sleep, clock=time.monotonic) -> dict:
    """End the call: ph.end_calls(), else (failed, or still live LIVE_WAIT s later) the Phone Link end-call button. Returns
    the end_calls result plus {"fallback": plcall result, "live_after": bool} when the fallback ran. Never raises."""
    try:
        r = dict(ph.end_calls())
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
    nothing_to_end = not r.get("ended") and not r.get("left") and "error" not in r   # no live call was listed: not a failure
    if (r.get("ok") or nothing_to_end) and not _still_live(ph, LIVE_WAIT, sleep, clock):
        return r
    fb = {"ok": False, "error": "no Phone Link hang-up path"}
    if hasattr(ph, "phone_link"):
        try:
            fb = asyncio.run(ph.phone_link("hangup"))
        except Exception as e:  # noqa: BLE001
            fb = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
    r["fallback"] = fb
    r["live_after"] = _still_live(ph, LIVE_WAIT, sleep, clock) if fb.get("ok") else bool(_live(ph))
    if heal is not None:
        try:
            heal.report("hangup_failed", recovered=not r["live_after"], error=str(r.get("error") or fb.get("error") or "")[:150])
        except Exception as e:  # noqa: BLE001
            if log:
                log("hangup_report_failed", error=f"{type(e).__name__}: {e}"[:150])
    return r

"""Self-healing engine (module-level singleton). Other modules register repair actions and report failures; a background
thread in main.py calls tick() every 2 s, which runs at most one due action and never while a call is up.

  register_action(name, fn, tier="safe"|"heavy")   fn() -> {"ok": bool, ...}
  report(failure, **detail)                        a failure class seen now (readiness reason, event log, post-call health)
  status() -> dict                                 goes into the heartbeat ready["heal"]
  set_policy(dict)                                 server-pushed: heavy tiers on/off and hourly caps
  set_in_call(fn_bool)                             tick() does nothing while fn() is True (or raises)
  set_log(fn)                                      the app logger, log(event, json_text) style
  tick()                                           at most one action; returns its ring entry or None
  reset_for_tests()

Each failure has a ladder of actions tried in order. A rung is skipped when its action is not registered, is heavy and the
policy does not enable it, or its hourly cap is used up. After a rung runs, the next one waits for the action's settle time
and for a new report() of the failure after the action ended. A failure not reported for STALE_S seconds is dropped, so its
ladder starts again from the first rung next time. An exhausted ladder is report-only until the failure goes stale or
LADDER_RESTART_S passed since its last action.
Assumption: both radio_on and radio_cycle count as radio toggles for max_radio_toggles_per_hour."""
import collections
import json
import threading
import time

LADDERS = {
    "bluetooth_off": ("radio_on", "radio_cycle", "adapter_reset"),
    "iphone_not_connected": ("phone_connect", "hf_refresh", "radio_cycle", "adapter_reset"),
    "hf_endpoints": ("hf_refresh", "radio_cycle", "adapter_reset"),
    "bt_unhealthy": ("hf_refresh", "radio_cycle", "adapter_reset"),
    "bthusb_error": ("hf_refresh", "radio_cycle", "adapter_reset"),
    "phone_link_not_running": ("phone_link_launch",),
    "phone_link_not_linked": ("phone_link_launch",),
    "no_phone_paired": (),
    "hangup_failed": (),
    "net_dns": (),
}
FAILURES = tuple(LADDERS)
# s an action blocks re-runs of itself (any failure) and, after it ran for a failure, before that failure's next rung
COOLDOWN = {"radio_on": 20.0, "phone_connect": 30.0, "phone_link_launch": 30.0, "hf_refresh": 180.0, "radio_cycle": 120.0,
            "adapter_reset": 300.0}
DEFAULT_COOLDOWN = 60.0
STALE_S = 45.0           # readiness reasons arrive with every heartbeat (10 s): 3 missed beats + slack = recovered
EVENT_STALE_S = 600.0    # one-shot events (event log, post-call health) stay pending across a call
LADDER_RESTART_S = 900.0  # s after the last action (or first report) before an exhausted ladder starts again from rung 0
EVENT_FAILURES = ("bt_unhealthy", "bthusb_error", "hangup_failed", "net_dns")
CAPS = {"adapter_reset": "max_adapter_resets_per_hour", "radio_on": "max_radio_toggles_per_hour",
        "radio_cycle": "max_radio_toggles_per_hour"}
DEFAULT_POLICY = {"radio_cycle": False, "adapter_reset": False, "max_adapter_resets_per_hour": 2, "max_radio_toggles_per_hour": 4}
RING = 20
STATUS_RECENT = 10

_now = time.monotonic
_wall = time.time
_lock = threading.Lock()
_tick_lock = threading.Lock()


def _noop(*a):
    pass


def reset_for_tests():
    global _now, _wall, _log, _in_call, _policy, _follow_up
    with _lock:
        _now, _wall = time.monotonic, time.time
        _log, _in_call = _noop, (lambda: False)
        _policy = dict(DEFAULT_POLICY)
        _actions.clear()
        _failures.clear()
        _ring.clear()
        _last_run.clear()
        _cap_hits.clear()
        _follow_up = None


_actions = {}            # name -> (fn, tier)
_failures = {}           # failure -> {rung, first, last_seen, last_end, wait_until, detail, count, noted}
_ring = collections.deque(maxlen=RING)
_last_run = {}           # action -> monotonic end of its last run
_cap_hits = collections.defaultdict(collections.deque)   # policy cap key -> monotonic attempt times (last hour)
_log = _noop
_in_call = lambda: False   # noqa: E731
_policy = dict(DEFAULT_POLICY)
_follow_up = None        # action to run on the next tick (after a successful adapter_reset: hf_refresh)
FOLLOW_UPS = {"adapter_reset": "hf_refresh"}


def _emit(ev, **kw):
    try:
        _log(ev, json.dumps(kw, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        pass


def set_log(fn):
    global _log
    _log = fn or _noop


def set_in_call(fn):
    global _in_call
    _in_call = fn or (lambda: False)


def set_policy(policy):
    """Merge the known keys of a server-pushed policy over the defaults (None / {} = defaults: heavy tiers off)."""
    global _policy
    new = dict(DEFAULT_POLICY)
    for k, v in (policy or {}).items() if isinstance(policy, dict) else ():
        if k in ("radio_cycle", "adapter_reset"):
            new[k] = v is True
        elif k in ("max_adapter_resets_per_hour", "max_radio_toggles_per_hour"):
            try:
                new[k] = max(0, int(v))
            except (TypeError, ValueError):
                pass
    with _lock:
        changed = new != _policy
        _policy = new
    if changed:
        _emit("heal_policy", **new)


def register_action(name, fn, tier="safe"):
    with _lock:
        _actions[str(name)] = (fn, "heavy" if tier == "heavy" else "safe")


def _clean(detail: dict) -> dict:
    return {str(k)[:40]: (v if isinstance(v, (bool, int, float)) or v is None else str(v)[:120]) for k, v in detail.items()}


def report(failure, **detail):
    failure = str(failure)
    now = _now()
    with _lock:
        f = _failures.get(failure)
        if f is None:
            f = _failures[failure] = {"rung": 0, "first": now, "last_seen": now, "last_end": None, "wait_until": 0.0,
                                      "detail": {}, "count": 0, "noted": set()}
        f["last_seen"] = now
        f["count"] += 1
        if detail:
            f["detail"] = _clean(detail)


def _in_call_now() -> bool:
    try:
        return bool(_in_call())
    except Exception:  # noqa: BLE001
        return True    # cannot tell: assume a call is up


def _cap_left(action, now) -> bool:
    key = CAPS.get(action)
    if key is None:
        return True
    hits = _cap_hits[key]
    while hits and now - hits[0] >= 3600.0:
        hits.popleft()
    return len(hits) < int(_policy.get(key, 0))


def _skip_reason(action, now):
    """Why a rung cannot run at all now (None = it can): not registered, heavy tier off, hourly cap used."""
    a = _actions.get(action)
    if a is None:
        return "missing"
    if a[1] == "heavy" and _policy.get(action) is not True:
        return "heavy_off"
    if not _cap_left(action, now):
        return "capped"
    return None


def _note(name, f, key, **kw):
    if key not in f["noted"]:
        f["noted"].add(key)
        _emit(name, **kw)


def _pick(now):
    """(failure, action) to run now, or (None, follow-up action), or (None, None). Called under _lock."""
    global _follow_up
    for name in list(_failures):
        f = _failures[name]
        if now - f["last_seen"] > (EVENT_STALE_S if name in EVENT_FAILURES else STALE_S):
            del _failures[name]
            if f["last_end"] is not None:
                _emit("heal_reset", failure=name, rungs_tried=f["rung"])
    if _follow_up:
        act, _follow_up = _follow_up, None
        if _skip_reason(act, now) is None:
            return None, act
    for name, f in sorted(_failures.items(), key=lambda kv: kv[1]["first"]):
        if now < f["wait_until"] or (f["last_end"] is not None and f["last_seen"] <= f["last_end"]):
            continue   # the last action is settling, or no report since it ended
        ladder = LADDERS.get(name, ())
        if ladder and f["rung"] >= len(ladder) and now - (f["last_end"] or f["first"]) >= LADDER_RESTART_S:
            f["rung"], f["noted"] = 0, set()   # still failing long after the ladder ran out: try again (cooldowns/caps apply)
            _emit("heal_restart", failure=name)
        while f["rung"] < len(ladder):
            act = ladder[f["rung"]]
            why = _skip_reason(act, now)
            if why is None:
                break
            _note("heal_skip", f, (act, why), failure=name, action=act, why=why)
            f["rung"] += 1
        if f["rung"] >= len(ladder):
            _note("heal_report_only", f, "report_only", failure=name, ladder=list(ladder), detail=f["detail"])
            continue
        act = ladder[f["rung"]]
        if now - _last_run.get(act, -1e18) < COOLDOWN.get(act, DEFAULT_COOLDOWN):
            continue   # the action ran recently (for another failure): wait
        return name, act
    return None, None


def tick():
    """Run at most one due action (blocking; call from a background thread). None when nothing ran."""
    global _follow_up
    if not _tick_lock.acquire(blocking=False):
        return None
    try:
        if _in_call_now():
            return None
        with _lock:
            now = _now()
            failure, act = _pick(now)
            if act is None:
                return None
            fn = _actions[act][0]
            key = CAPS.get(act)
            if key:
                _cap_hits[key].append(now)
            if failure:
                f = _failures[failure]
                f["rung"] += 1
                detail = dict(f["detail"])
            else:
                detail = {"follow_up": True}
        if _in_call_now():   # a call may have started while we picked
            with _lock:
                if key and _cap_hits[key]:
                    _cap_hits[key].pop()
                if failure and failure in _failures:
                    _failures[failure]["rung"] -= 1
            return None
        t0 = _now()
        err = None
        try:
            r = fn()
            ok = bool(r.get("ok")) if isinstance(r, dict) else bool(r)
            if isinstance(r, dict) and not ok and r.get("reason"):
                err = str(r.get("reason"))[:120]
        except Exception as e:  # noqa: BLE001
            ok, err = False, f"{type(e).__name__}: {e}"[:160]
        end = _now()
        entry = {"t": round(_wall(), 1), "failure": failure or "follow_up", "action": act, "ok": ok, "ms": int((end - t0) * 1000)}
        if err:
            entry["error"] = err
        with _lock:
            _ring.append(entry)
            _last_run[act] = end
            if failure and failure in _failures:
                f = _failures[failure]
                f["last_end"] = end
                f["wait_until"] = end + COOLDOWN.get(act, DEFAULT_COOLDOWN)
            if ok and act in FOLLOW_UPS:
                _follow_up = FOLLOW_UPS[act]
        _emit("heal_action", **entry, **({"detail": detail} if detail else {}))
        return entry
    finally:
        _tick_lock.release()


def status() -> dict:
    """Small JSON-safe summary for the heartbeat: policy, in_call, active failures, last actions, hourly cap use."""
    now = _now()
    with _lock:
        active = {}
        for name, f in _failures.items():
            ladder = LADDERS.get(name, ())
            active[name] = {"next": ladder[f["rung"]] if f["rung"] < len(ladder) else None, "tried": f["rung"],
                            "count": f["count"], "age_s": int(now - f["first"])}
        caps = {}
        for key in ("max_adapter_resets_per_hour", "max_radio_toggles_per_hour"):
            caps[key[4:-9]] = sum(1 for t in _cap_hits.get(key, ()) if now - t < 3600.0)
        return {"policy": dict(_policy), "in_call": _in_call_now(), "actions": sorted(_actions), "active": active,
                "recent": list(_ring)[-STATUS_RECENT:], "used_1h": caps}


reset_for_tests()

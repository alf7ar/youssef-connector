"""Readiness of the laptop for the next call (what the connector reports in every heartbeat): can Windows place a call
through Phone Link right now. Ported from the private dialer's native/calls.py (auto_ready and its helpers), without any
LLM/TTS key checks and without a call-history reader. Functions marked "verbatim" in PORT_NOTES.md are byte-identical
copies; auto_ready and the Hands-Free refresh are adapted (synchronous, the refresh runs in a daemon thread so a ~2 minute
refresh never stalls the heartbeat).

check() is what PhoneAdapter.ready() returns: {ok, reason, bt_radio, iphone_connected, hfp_audio_device,
phone_link_running, session_locked, call_active, hf_link, hf_state}. Blocking; call it from a worker thread."""
import asyncio
import importlib
import threading
import time


def _sibling(name):
    """Import a sibling module whether this file is used as part of the `connector` package or flat; None when it cannot be
    imported (phone needs the Windows-only winrt packages; tests inject a stub with `readiness.phone = ...`)."""
    try:
        return importlib.import_module("." + name, __package__) if __package__ else importlib.import_module(name)
    except ImportError:
        return None


phone = _sibling("phone")

RESUME_GAP = 20.0   # s between two 0.5 s watch ticks (wall or monotonic clock) that means the laptop slept
HF_SETTLE = 30.0    # s after app start / resume before the idle Hands-Free check (let Bluetooth reconnect first)

HF_REFRESH_EVERY = 900.0   # s between automatic Hands-Free refreshes (each takes ~2 min and needs an idle line)
_last_hf_refresh = [0.0]
RESCAN_EVERY = 30.0   # s between two iPhone re-scans while the cached phone is not connected
_rescan_t = [-1e9]


def resumed(prev: tuple | None, now: tuple, gap: float = RESUME_GAP) -> bool:
    """(wall, monotonic) of two consecutive watch ticks: True when either clock jumped by more than `gap` s (the laptop
    slept: Windows corrects the wall clock on resume, and one of the two clocks skips the sleep)."""
    return prev is not None and (abs(now[0] - prev[0]) > gap or now[1] - prev[1] > gap)


def _phone_link_running() -> bool:
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq PhoneExperienceHost.exe", "/NH"], capture_output=True,
                             text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return "PhoneExperienceHost" in out
    except Exception:  # noqa: BLE001
        return False


def _tasklist_has(image: str):
    """True/False: is a process with this image name running (tasklist); None when that cannot be told (not Windows)."""
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"], capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return image.lower() in out.lower()
    except Exception:  # noqa: BLE001
        return None


def session_locked():
    """True when the Windows lock screen is up (LogonUI.exe runs only then); Phone Link's UI automation needs it unlocked."""
    return _tasklist_has("LogonUI.exe")


def battery_saver_on():
    """True when Windows battery saver is on (it throttles background apps / Bluetooth); None if not detectable."""
    try:
        import ctypes

        class PowerStatus(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte), ("BatteryLifePercent", ctypes.c_ubyte),
                        ("SystemStatusFlag", ctypes.c_ubyte), ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
        st = PowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return None
        return bool(st.SystemStatusFlag & 1)
    except Exception:  # noqa: BLE001
        return None


# ---- adapted from calls.py (see PORT_NOTES.md): state of the idle Hands-Free check, now driven by the heartbeat
_STARTED = time.monotonic()
_hf_due = [(_STARTED + HF_SETTLE, "app_start")]   # (due, why) of the next idle Hands-Free check; None when done
_prev_tick = [None]                               # (wall, monotonic) at the previous check, for sleep/resume detection
_hf_thread = [None]
_mac = [""]                                       # iPhone MAC found once (no settings file in the public connector)


def _print_log(ev, **kw):
    print(ev, kw, flush=True)


def _hf_check(why: str, mac: str, log):
    """Calls.Bridge._hf_check, blocking (up to ~2 min) and run in a daemon thread: if an idle Hands-Free endpoint is missing
    or "notpresent", refresh the iPhone's Hands-Free service. Never during a call."""
    try:
        hf = phone.hf_endpoints()
        st = {k: e.get("state") for k, e in hf.items()}
        if not phone.hf_abnormal(hf):
            log("hf_check", why=why, result="endpoints normal", states=st)
            return
        mac = mac or _mac[0] or asyncio.run(phone.find_iphone_mac())
        _mac[0] = mac or _mac[0]
        if phone.call_flags().get("call_active") is not False:
            log("hf_check", why=why, result="a call is up: no refresh", states=st)
            return
        log("hf_check", why=why, result="refreshing the iPhone Hands-Free service", states=st)
        log("hf_refresh", why=why, **phone.refresh_hf_service(mac))
    except Exception as e:  # noqa: BLE001
        log("hf_check", why=why, error=f"{type(e).__name__}: {e}"[:200])


def _maybe_refresh(mac: str, log) -> bool:
    """Abnormal Hands-Free endpoints: start (at most one) background refresh when one is due. True while one is running.
    Due = the app-start/resume check once its settle time has passed, else at most every HF_REFRESH_EVERY seconds."""
    now = time.monotonic()
    t = _hf_thread[0]
    if t is not None and t.is_alive():
        return True
    why = None
    due = _hf_due[0]
    if due and now >= due[0]:
        why, _hf_due[0] = due[1], None
    elif not due and now - _last_hf_refresh[0] > HF_REFRESH_EVERY:
        why = "auto_dial"
    if why:
        _last_hf_refresh[0] = now
        _hf_thread[0] = threading.Thread(target=_hf_check, args=(why, mac, log), daemon=True, name="hf-refresh")
        _hf_thread[0].start()
        return True
    return False


def auto_ready(info: dict, mac: str = "", log=_print_log) -> tuple:
    """(ok, reason) whether the laptop can place the next call now (calls.auto_ready without the OpenAI key check and
    without the Bridge). Only an explicit negative blocks, except the call state (None = unknown, could be a live call:
    wait). Order: cheap local checks first. Fills `info` with every value it looked at."""
    flags = phone.call_flags()
    info["call_active"] = flags.get("call_active")
    if flags.get("call_active") is not False or flags.get("call_incoming"):
        return False, "call_active" if flags.get("call_active") else ("call_incoming" if flags.get("call_incoming") else "call_state_unknown")
    link = phone.hf_link()
    info["hf_state"], info["hf_link"] = link, link == "active"
    if link == "active":
        return False, "hf_link_active"
    info["session_locked"] = session_locked()
    if info["session_locked"] is True:
        return False, "session_locked"
    if battery_saver_on() is True:
        return False, "battery_saver"
    info["phone_link_running"] = _phone_link_running()
    if info["phone_link_running"] is False:
        return False, "phone_link_not_running"
    radio = asyncio.run(phone.bt_radio())
    info["bt_radio"] = radio.get("on")
    if radio.get("on") is False:
        return False, "bluetooth_off"
    cfg_mac = mac
    mac = mac or _mac[0] or asyncio.run(phone.find_iphone_mac())
    _mac[0] = mac or _mac[0]
    info["iphone_connected"] = asyncio.run(phone.iphone(mac)).get("connected")
    if info["iphone_connected"] is False and not cfg_mac and time.monotonic() - _rescan_t[0] >= RESCAN_EVERY:
        # the cached phone is not connected: another paired iPhone may be (at most one scan per RESCAN_EVERY s)
        _rescan_t[0] = time.monotonic()
        new = asyncio.run(phone.find_iphone_mac())
        if new and new != mac:
            mac = _mac[0] = new
            info["iphone_connected"] = asyncio.run(phone.iphone(mac)).get("connected")
    if info["iphone_connected"] is False:
        return False, "iphone_not_connected"
    eps = phone.hf_endpoints()
    info["hfp_audio_device"] = len(eps) == 2
    if phone.hf_abnormal(eps):
        if _maybe_refresh(mac, log):   # the ~2 min refresh runs in the background; not ready until the endpoints are back
            return False, "hf_refreshing"
        return False, "hf_endpoints"
    if _hf_due[0] and time.monotonic() >= _hf_due[0][0]:
        _hf_due[0] = None
    return True, ""


def check(mac: str = "", log=_print_log) -> dict:
    """PhoneAdapter.ready(): {ok, reason, bt_radio, iphone_connected, hfp_audio_device, phone_link_running, session_locked,
    call_active, hf_link (bool), hf_state (MMDevices state name)}; values not probed (early exit) are None. Never raises."""
    info = {k: None for k in ("bt_radio", "iphone_connected", "hfp_audio_device", "phone_link_running", "session_locked",
                              "call_active", "hf_link", "hf_state")}
    if phone is None:
        return {"ok": False, "reason": "phone_module_unavailable", **info}
    try:
        tick = (time.time(), time.monotonic())
        if resumed(_prev_tick[0], tick):   # the laptop slept: let Bluetooth reconnect, then look at the Hands-Free endpoints
            _hf_due[0] = (tick[1] + HF_SETTLE, "resume")
            log("resume_detected", hf_check_in_s=HF_SETTLE)
        _prev_tick[0] = tick
        ok, reason = auto_ready(info, mac, log)
    except Exception as e:  # noqa: BLE001
        ok, reason = False, f"ready_check_failed: {type(e).__name__}"
    return {"ok": ok, "reason": reason, **info}

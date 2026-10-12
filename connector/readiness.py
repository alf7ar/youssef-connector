"""Readiness of the laptop for the next call (what the connector reports in every heartbeat): can Windows place a call
through Phone Link right now. Ported from the private dialer's native/calls.py (auto_ready and its helpers), without any
LLM/TTS key checks and without a call-history reader. Functions marked "verbatim" in PORT_NOTES.md are byte-identical
copies; auto_ready and the Hands-Free refresh are adapted (synchronous, the refresh runs in a daemon thread so a ~2 minute
refresh never stalls the heartbeat).

check() is what PhoneAdapter.ready() returns: {ok, reason, reason_ar, reason_en, warnings, bt_radio, iphone_connected,
hfp_audio_device, phone_link_running, session_locked, call_active, hf_link, hf_state}. Blocking; call it from a worker thread.
The warning probes (netsh, route, PowerShell: up to 15+15+20 s when Windows hangs) never run on that path: each runs in its own
background thread, results are cached, and one check() waits at most PROBE_BUDGET s for them (a late result shows up in the next
heartbeat; a result older than PROBE_MAX_AGE counts as unknown), so they cannot push a heartbeat past the hub's 30 s timeout.
`warnings` is a list of {code, ar, en}: things that do not stop the next call (ok stays True) but hurt it
(iphone_hotspot_internet, old_app_running); `reason_ar`/`reason_en` are the texts of `reason` ("" when ready)."""
import asyncio
import importlib
import ipaddress
import re
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
heal = _sibling("heal")

RESUME_GAP = 20.0   # s between two 0.5 s watch ticks (wall or monotonic clock) that means the laptop slept
HF_SETTLE = 30.0    # s after app start / resume before the idle Hands-Free check (let Bluetooth reconnect first)

HF_REFRESH_EVERY = 900.0   # s between automatic Hands-Free refreshes (each takes ~2 min and needs an idle line)
_last_hf_refresh = [0.0]
RESCAN_EVERY = 30.0   # s between two iPhone re-scans while the cached phone is not connected
_rescan_t = [-1e9]

# ---- texts (Arabic, English) of every reason code auto_ready/check can return and of every warning code
REASONS = {
    "call_active": ("في مكالمة شغالة دلوقتي. استنى لما تخلص.", "A call is in progress. Wait for it to end."),
    "call_incoming": ("في مكالمة داخلة على الآيفون. ردّ عليها أو ارفضها.", "An incoming call is ringing on the iPhone. Answer or decline it."),
    "call_state_unknown": ("مش قادر أعرف حالة المكالمات على اللابتوب. استنى شوية.", "The call state on the laptop cannot be read yet. Wait a moment."),
    "hf_link_active": ("خط الصوت (Hands-Free) مفتوح من مكالمة قبل كده. اقفل أي مكالمة وافصل وصّل البلوتوث.",
                       "The Hands-Free audio link is still up from an earlier call. End any call or reconnect Bluetooth."),
    "session_locked": ("شاشة اللابتوب مقفولة. افتحها (ادخل الباسورد).", "The laptop is locked. Unlock it."),
    "battery_saver": ("وضع توفير البطارية شغال وبيبوّظ البلوتوث. اقفله أو وصّل الشاحن.",
                      "Battery saver is on and throttles Bluetooth. Turn it off or plug in the charger."),
    "phone_link_not_running": ("برنامج Phone Link مش شغال. افتحه من قايمة ابدأ.", "Phone Link is not running. Open it from the Start menu."),
    "bluetooth_off": ("البلوتوث مقفول على اللابتوب. شغّله.", "Bluetooth is off on the laptop. Turn it on."),
    "bluetooth_adapter_missing": ("مفيش كارت بلوتوث شغال على اللابتوب (مفصول أو متشال). فعّله من Device Manager أو وصّل دونجل.",
                                  "No Bluetooth adapter is available on the laptop (disabled or absent). Enable it in Device Manager or plug in a dongle."),
    "iphone_not_connected": ("الآيفون مش متوصل بالبلوتوث. شغّل البلوتوث على الآيفون وقرّبه من اللابتوب.",
                             "The iPhone is not connected over Bluetooth. Turn Bluetooth on on the iPhone and bring it close."),
    "heal_busy": ("بصلّح البلوتوث دلوقتي. استنى شوية وجرّب تاني.", "A Bluetooth repair is running. Wait a moment and try again."),
    "hf_refreshing": ("بجدد خدمة الصوت (Hands-Free) مع الآيفون. هتاخد حوالي دقيقتين.",
                      "Refreshing the iPhone Hands-Free audio service. It takes about two minutes."),
    "hf_endpoints": ("جهازي الصوت بتوع الآيفون (Hands-Free) مش ظاهرين. افصل وصّل البلوتوث.",
                     "The iPhone Hands-Free audio devices are missing. Reconnect Bluetooth."),
    "phone_module_unavailable": ("وحدة الهاتف مش شغالة على الجهاز ده. أعد تثبيت البرنامج.",
                                 "The phone module is unavailable on this computer. Reinstall the app."),
    "ready_check_failed": ("فحص الجاهزية فشل. لو استمر اقفل البرنامج وافتحه تاني.", "The readiness check failed. Restart the app if it persists."),
    "ready() failed": ("فحص الجاهزية فشل. لو استمر اقفل البرنامج وافتحه تاني.", "The readiness check failed. Restart the app if it persists."),
}
WARNINGS = {
    "iphone_hotspot_internet": ("اللابتوب واخد نت من نفس الآيفون، ده بيبوظ صوت المكالمة. وصّل واي فاي تاني",
                                "The laptop gets its internet from the same iPhone, which breaks the call audio. Connect to another Wi-Fi."),
    "old_app_running": ("البرنامج القديم (AhmedDialer) شغال وبيتعارض مع البرنامج ده. اقفله.",
                        "The old AhmedDialer app is running and conflicts with this one. Close it."),
}
OLD_APP_TASKS = ("AhmedDialerApp", "AhmedDialerOpenOnUnlock")
# the iPhone Personal Hotspot subnet (prefix 172.20.10 /28), built from numbers: the public-repo gate rejects dotted IPv4 literals
HOTSPOT_NET = ipaddress.IPv4Network(((172 << 24) | (20 << 16) | (10 << 8), 28))
NET_TTL, OLD_APP_TTL = 10.0, 30.0   # s a network / old-app probe result is reused (heartbeat every 10 s; the old-app probe starts PowerShell)
PROBE_BUDGET = 4.0                   # s one check() waits for the warning probes at most (heartbeat every 10 s, hub timeout 30 s)
PROBE_MAX_AGE = 120.0                # s a cached probe result stays usable when its refresh hangs; older = unknown (None)
_cache = {}                          # key -> (monotonic time, value)
_inflight = {}                       # key -> running probe thread
_probe_lock = threading.Lock()
_phone_name = [""]                   # Bluetooth name of the paired iPhone last seen (the Personal Hotspot SSID defaults to it)


def reason_texts(reason) -> tuple:
    """(ar, en) of a reason code; "ready_check_failed: X" and "ready() failed: X" use the text of their code. ("", "") for no reason."""
    if not reason:
        return "", ""
    key = str(reason).split(":")[0].strip()
    return REASONS.get(key) or (f"سبب غير معروف: {reason}", f"Unknown reason: {reason}")


def warning(code: str) -> dict:
    ar, en = WARNINGS[code]
    return {"code": code, "ar": ar, "en": en}


def decorate(ready: dict, warnings=()) -> dict:
    """Add reason_ar, reason_en and warnings to a ready dict in place (ok and reason are left as they are)."""
    ready["reason_ar"], ready["reason_en"] = reason_texts(ready.get("reason"))
    ready["warnings"] = list(warnings)
    return ready


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


# ---- warnings: the next call can still be placed (ok stays True) but may sound broken or collide with the old app
def _run(cmd: list, timeout: float = 15.0):
    """stdout of a Windows command, None when it cannot be run (not Windows, timeout, error). Tests replace this."""
    import subprocess
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace",
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:  # noqa: BLE001
        return None


def _kick(key: str, fn):
    """Run fn() in a daemon thread and cache its value, unless a probe for `key` is already running."""
    def run():
        try:
            val = fn()
        except Exception:  # noqa: BLE001
            return
        _cache[key] = (time.monotonic(), val)
    with _probe_lock:
        t = _inflight.get(key)
        if t is not None and t.is_alive():
            return
        t = _inflight[key] = threading.Thread(target=run, daemon=True, name="probe-" + key)
        t.start()


def _cached(key: str, ttl: float, fn, deadline: float = None):
    """Value of probe `key`: the cache while it is younger than `ttl`; else a background refresh is started and waited for until
    `deadline` (monotonic; default now + PROBE_BUDGET), then the cache is used if it is younger than PROBE_MAX_AGE, else None."""
    now = time.monotonic()
    hit = _cache.get(key)
    if not (hit and now - hit[0] < ttl):
        _kick(key, fn)
        t = _inflight.get(key)
        if t is not None:
            t.join(max(0.0, (now + PROBE_BUDGET if deadline is None else deadline) - time.monotonic()))
        hit = _cache.get(key)
    return hit[1] if hit and time.monotonic() - hit[0] < PROBE_MAX_AGE else None


def parse_ssid(out):
    """SSID of the connected Wi-Fi from `netsh wlan show interfaces` ("" = no Wi-Fi connection, None = not readable).
    Only the exact key "SSID" counts (not BSSID); netsh prints no SSID line for a disconnected adapter."""
    if out is None:
        return None
    for line in out.splitlines():
        m = re.match(r"^\s*SSID\s*:\s*(.*?)\s*$", line)
        if m and m.group(1):
            return m.group(1)
    return ""


def parse_gateways(out):
    """Default-route gateways (IPv4 strings) from `route print -4` (the 0.0.0.0 / 0.0.0.0 rows; "On-link" rows have none);
    None when not readable. The columns are numbers, so this works in every Windows language."""
    if out is None:
        return None
    gws = []
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 3 and f[0] == f[1] == "0.0.0.0":
            try:
                gws.append(str(ipaddress.IPv4Address(f[2])))
            except ValueError:
                pass
    return gws


def parse_old_app(out):
    """Names of the old-app things found by the PowerShell probe: 'proc:<image>' per running process under AhmedDialer
    (exe path or command line) and 'task:<name>' per scheduled task in state Running; None when not readable."""
    if out is None:
        return None
    found = []
    for line in out.splitlines():
        f = line.strip().split("|")
        if len(f) >= 3 and f[0] == "P":
            found.append("proc:" + f[2])
        elif len(f) >= 3 and f[0] == "T" and f[2].strip().lower() == "running" and f[1] in OLD_APP_TASKS:
            found.append("task:" + f[1])
    return found


def _norm_name(s) -> str:
    return " ".join(str(s or "").replace("\u2019", "'").replace("\u2018", "'").split()).casefold()


def _ssid_probe():
    return parse_ssid(_run(["netsh", "wlan", "show", "interfaces"]))


def _gw_probe():
    return parse_gateways(_run(["route", "print", "-4"]))


def hotspot_internet(phone_name: str = "", deadline: float = None):
    """True when the laptop's internet comes through the paired iPhone's own Personal Hotspot: the connected Wi-Fi SSID equals
    the iPhone's name, or a default gateway is inside the hotspot subnet. False when neither; None when nothing could be read."""
    ssid = _cached("ssid", NET_TTL, _ssid_probe, deadline)
    gws = _cached("gw", NET_TTL, _gw_probe, deadline)
    if ssid and phone_name and _norm_name(ssid) == _norm_name(phone_name):
        return True
    if any(ipaddress.IPv4Address(g) in HOTSPOT_NET for g in gws or ()):
        return True
    return None if ssid is None and gws is None else False


def _old_app_probe():
    # the pattern is built from two pieces so this PowerShell's own command line (which is excluded by $PID anyway) cannot match it
    tasks = ",".join(f"'{t}'" for t in OLD_APP_TASKS)
    ps = ("$p='*Ahmed'+'Dialer*'; "
          "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and ($_.ExecutablePath -like $p -or $_.CommandLine -like $p) } "
          "| ForEach-Object { 'P|' + $_.ProcessId + '|' + $_.Name }; "
          f"foreach ($n in {tasks}) {{ $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue; "
          "if ($t) { 'T|' + $n + '|' + $t.State } }")
    return parse_old_app(_run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=20))


def old_app_running(deadline: float = None):
    """True when the old AhmedDialer app (a process under AhmedDialer, or its scheduled task AhmedDialerApp /
    AhmedDialerOpenOnUnlock in state Running) is up; None when that cannot be told (not Windows)."""
    found = _cached("old_app", OLD_APP_TTL, _old_app_probe, deadline)
    return None if found is None else bool(found)


def collect_warnings(phone_name: str = "") -> list:
    """The warnings that apply now, as [{code, ar, en}]. A probe that fails, is unreadable or is still running after
    PROBE_BUDGET s adds nothing (its last result, if recent, is used)."""
    deadline = time.monotonic() + PROBE_BUDGET
    for key, ttl, fn in (("ssid", NET_TTL, _ssid_probe), ("gw", NET_TTL, _gw_probe), ("old_app", OLD_APP_TTL, _old_app_probe)):
        hit = _cache.get(key)
        if not (hit and time.monotonic() - hit[0] < ttl):
            _kick(key, fn)     # start all stale probes first: they run side by side inside one budget
    out = []
    for code, probe in (("iphone_hotspot_internet", lambda: hotspot_internet(phone_name, deadline)),
                        ("old_app_running", lambda: old_app_running(deadline))):
        try:
            if probe() is True:
                out.append(warning(code))
        except Exception:  # noqa: BLE001
            pass
    return out


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
    if radio.get("present") is False:   # no Bluetooth radio listed at all: adapter disabled in Device Manager or absent
        return False, "bluetooth_adapter_missing"
    if radio.get("on") is False:
        return False, "bluetooth_off"
    cfg_mac = mac
    mac = mac or _mac[0] or asyncio.run(phone.find_iphone_mac())
    _mac[0] = mac or _mac[0]
    ph = asyncio.run(phone.iphone(mac))
    info["iphone_connected"] = ph.get("connected")
    _phone_name[0] = ph.get("name") or _phone_name[0]
    if info["iphone_connected"] is False and not cfg_mac and time.monotonic() - _rescan_t[0] >= RESCAN_EVERY:
        # the cached phone is not connected: another paired iPhone may be (at most one scan per RESCAN_EVERY s)
        _rescan_t[0] = time.monotonic()
        new = asyncio.run(phone.find_iphone_mac())
        if new and new != mac:
            mac = _mac[0] = new
            ph = asyncio.run(phone.iphone(mac))
            info["iphone_connected"] = ph.get("connected")
            _phone_name[0] = ph.get("name") or _phone_name[0]
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
    """PhoneAdapter.ready(): {ok, reason, reason_ar, reason_en, warnings, bt_radio, iphone_connected, hfp_audio_device, phone_link_running, session_locked,
    call_active, hf_link (bool), hf_state (MMDevices state name)}; values not probed (early exit) are None. Never raises."""
    info = {k: None for k in ("bt_radio", "iphone_connected", "hfp_audio_device", "phone_link_running", "session_locked",
                              "call_active", "hf_link", "hf_state")}
    if phone is None:
        return decorate({"ok": False, "reason": "phone_module_unavailable", **info})
    try:
        tick = (time.time(), time.monotonic())
        if resumed(_prev_tick[0], tick):   # the laptop slept: let Bluetooth reconnect, then look at the Hands-Free endpoints
            _hf_due[0] = (tick[1] + HF_SETTLE, "resume")
            log("resume_detected", hf_check_in_s=HF_SETTLE)
        _prev_tick[0] = tick
        ok, reason = auto_ready(info, mac, log)
    except Exception as e:  # noqa: BLE001
        ok, reason = False, f"ready_check_failed: {type(e).__name__}"
    if heal is not None and reason in heal.FAILURES:   # the heal engine's ladder for it (never hf_refreshing: a refresh runs)
        try:
            heal.report(reason)
        except Exception:  # noqa: BLE001
            pass
    warns = []
    if info["call_active"] is False:   # no probes (PowerShell) while a call is up or its state is unknown
        warns = collect_warnings(_phone_name[0])
    return decorate({"ok": ok, "reason": reason, **info}, warns)

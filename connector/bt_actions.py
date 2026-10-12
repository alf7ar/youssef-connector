"""Bluetooth self-healing actions for connector.heal: radio_on, radio_cycle (heavy), hf_refresh, phone_connect.

Each action is fn() -> dict(ok: bool, reason: str, ...) and never raises. Every Windows (winrt / bthprops) call sits in a thin
`_`-function so tests stub it. Actions run on the heal background thread (no running asyncio loop), never during a call:
each one re-checks the call state itself and refuses (reason "in_call") when a call is up or its state is unknown.
register() adds them to the heal singleton (heal.register_action)."""
import asyncio
import re
import threading
import time

HF_TIMEOUT = 150.0         # s the hf_refresh action waits for the (about 2 min) service refresh before reporting "timeout"
CONNECT_TIMEOUT = 15.0     # s phone_connect waits for the link to come up
CYCLE_OFF_SECS = 2.0       # s the radio stays off in radio_cycle
HFAG_SHORT_ID = 0x111F     # HandsfreeAG: opening this service makes Windows connect the phone's Hands-Free link
ERROR_ACCESS_DENIED = 5
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


# ---- thin Windows layer (stubbed in tests) -------------------------------------------------------------------------------
def _phone():
    from connector import phone
    return phone


def _sleep(secs: float) -> None:
    time.sleep(secs)


async def _asleep(secs: float) -> None:
    await asyncio.sleep(secs)


def _in_call():
    """True / False / None (unknown) from the Windows call state."""
    return _phone().call_flags().get("call_active")


def _mac() -> str:
    """The paired iPhone's MAC: the one readiness already found, else a scan ("" if none)."""
    from connector import readiness
    mac = readiness._mac[0]
    if not mac:
        mac = asyncio.run(_phone().find_iphone_mac())
        readiness._mac[0] = mac or readiness._mac[0]
    return mac


async def _bt_radios() -> list:
    from winrt.windows.devices.radios import Radio, RadioKind
    return [r for r in await Radio.get_radios_async() if r.kind == RadioKind.BLUETOOTH]


async def _request_access() -> str:
    from winrt.windows.devices.radios import Radio
    return _phone()._enum_name(await Radio.request_access_async())


def _radio_is_on(radio) -> bool:
    from winrt.windows.devices.radios import RadioState
    return radio.state == RadioState.ON


async def _set_radio(radio, on: bool) -> str:
    """Radio.set_state_async -> RadioAccessStatus name ("ALLOWED", "DENIED_BY_USER", "DENIED_BY_SYSTEM", "UNSPECIFIED")."""
    from winrt.windows.devices.radios import RadioState
    return _phone()._enum_name(await radio.set_state_async(RadioState.ON if on else RadioState.OFF))


async def _open_device(mac: str):
    from winrt.windows.devices.bluetooth import BluetoothDevice
    return await BluetoothDevice.from_bluetooth_address_async(_phone().mac_int(mac))


def _device_paired(dev) -> bool:
    return bool(dev.device_information.pairing.is_paired)


def _device_connected(dev) -> bool:
    from winrt.windows.devices.bluetooth import BluetoothConnectionStatus
    return dev.connection_status == BluetoothConnectionStatus.CONNECTED


def _device_close(dev) -> None:
    try:
        dev.close()
    except Exception:  # noqa: BLE001
        pass


async def _rfcomm_probe(dev) -> dict:
    """Uncached RFCOMM service discovery for HandsfreeAG (0x111F): Windows has to open the ACL link to answer it."""
    from winrt.windows.devices.bluetooth import BluetoothCacheMode
    from winrt.windows.devices.bluetooth.rfcomm import RfcommServiceId
    res = await dev.get_rfcomm_services_for_id_with_cache_mode_async(RfcommServiceId.from_short_id(HFAG_SHORT_ID),
                                                                      BluetoothCacheMode.UNCACHED)
    return {"error": _phone()._enum_name(res.error), "services": len(res.services)}


def _refresh_hf(mac: str) -> dict:
    """phone.refresh_hf_service (blocking, about 2 min)."""
    return _phone().refresh_hf_service(mac)


def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:200]


def _call_guard():
    """None when no call is active, else the refusal dict."""
    try:
        active = _in_call()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "in_call", "error": _err(e)}
    if active is not False:
        return {"ok": False, "reason": "in_call", "call_active": active}
    return None


# ---- radio_on ----------------------------------------------------------------------------------------------------------
def radio_on() -> dict:
    """Switch the Bluetooth radio on (phone.bt_radio(turn_on=True)). reason: no_adapter | access_<status> | radio_error | still_off."""
    if (g := _call_guard()) is not None:
        return g
    try:
        r = asyncio.run(_phone().bt_radio(turn_on=True))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "radio_error", "error": _err(e)}
    out = {"present": r.get("present"), "on": r.get("on"), "access": r.get("access"), "set": r.get("set"), "error": r.get("error")}
    if r.get("error"):
        return {"ok": False, "reason": "radio_error", **out}
    if r.get("present") is False:
        return {"ok": False, "reason": "no_adapter", **out}
    if r.get("on"):
        return {"ok": True, "reason": "", **out}
    bad = next((v for v in (r.get("set"), r.get("access")) if v and v != "ALLOWED"), None)
    return {"ok": False, "reason": f"access_{bad.lower()}" if bad else "still_off", **out}


# ---- radio_cycle (heavy) -----------------------------------------------------------------------------------------------
async def _cycle() -> dict:
    out = {"ok": False, "reason": "", "steps": []}
    radios = await _bt_radios()
    if not radios:
        out["reason"] = "no_adapter"
        return out
    r = radios[0]
    access = await _request_access()
    out["access"] = access
    if access != "ALLOWED":
        out["reason"] = f"access_{access.lower()}"
        return out
    was_on = _radio_is_on(r)
    out["was_on"] = was_on
    on = None
    off = None
    try:
        off = await _set_radio(r, False)
        out["steps"].append(f"off:{off}")
        if off == "ALLOWED":
            await _asleep(CYCLE_OFF_SECS)
    finally:   # the radio must never stay off: switch it back on after an off that worked or that left it off (UNSPECIFIED can still work)
        if off in (None, "ALLOWED") or not _radio_is_on(r):
            on = await _set_radio(r, True)
            out["steps"].append(f"on:{on}")
    if on is None:   # the off request was refused and the radio is still on: nothing changed
        out["reason"] = f"access_{off.lower()}"
        return out
    await _asleep(1.5)
    out["on"] = _radio_is_on(r)
    if on != "ALLOWED":
        out["reason"] = f"access_{on.lower()}"
    elif not out["on"]:
        out["reason"] = "still_off"
    else:
        out["ok"] = True
    return out


def radio_cycle() -> dict:
    """Bluetooth radio off, 2 s, on (Windows.Devices.Radios), then verify the state is ON again. Heavy: the iPhone link drops."""
    if (g := _call_guard()) is not None:
        return g
    try:
        return asyncio.run(_cycle())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "radio_error", "error": _err(e)}


# ---- hf_refresh (non-blocking worker + bounded wait) ---------------------------------------------------------------------
_lock = threading.Lock()
_job = {"thread": None, "result": None, "t0": 0.0, "done": None}   # the one hf refresh worker


def interpret_hf(res: dict) -> dict:
    """phone.refresh_hf_service result -> action result. BluetoothSetServiceState return codes: 0 ok, 5 ERROR_ACCESS_DENIED
    (needs an elevated process: reason needs_admin), anything else is a failed toggle."""
    res = res or {}
    drc, erc = res.get("disable_rc"), res.get("enable_rc")
    out = {k: res[k] for k in ("before", "after", "disable_rc", "enable_rc", "disable_secs", "back_secs", "bt_connected",
                               "error") if k in res}
    if ERROR_ACCESS_DENIED in (drc, erc) or res.get("error") == "access_denied":
        return {"ok": False, "reason": "needs_admin", **out}
    if res.get("ok"):
        return {"ok": True, "reason": "", **out}
    if res.get("error"):
        return {"ok": False, "reason": "no_phone" if "MAC" in str(res["error"]) else "refresh_error", **out}
    if drc not in (None, 0) or erc not in (None, 0):
        return {"ok": False, "reason": "service_state_failed", **out}
    return {"ok": False, "reason": "endpoints_not_back", **out}


def _worker(mac: str, done: threading.Event) -> None:
    try:
        res = interpret_hf(_refresh_hf(mac))
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "reason": "refresh_error", "error": _err(e)}
    _job["result"] = res
    done.set()


def start_hf_refresh(mac: str = "") -> dict:
    """Non-blocking: start the Hands-Free service refresh in a daemon thread unless one is already running.
    {started, running, age}; poll hf_refresh_state()."""
    with _lock:
        t = _job["thread"]
        if t is not None and t.is_alive():
            return {"started": False, "running": True, "age": round(time.monotonic() - _job["t0"], 1)}
        done = threading.Event()
        _job.update(thread=None, result=None, t0=time.monotonic(), done=done)
        t = threading.Thread(target=_worker, args=(mac, done), daemon=True, name="bt-hf-refresh")
        _job["thread"] = t
        try:   # readiness must not start a second toggle while this one runs: it answers hf_refreshing instead
            from connector import readiness
            readiness._hf_thread[0] = t
        except Exception:  # noqa: BLE001
            pass
        t.start()
        return {"started": True, "running": True, "age": 0.0}


def hf_refresh_state() -> dict:
    t = _job["thread"]
    running = t is not None and t.is_alive()
    return {"running": running, "result": _job["result"], "age": round(time.monotonic() - _job["t0"], 1) if _job["t0"] else None}


def hf_refresh(timeout: float = HF_TIMEOUT) -> dict:
    """heal action: refresh the iPhone Hands-Free service. Waits at most `timeout` s (default 150) for the worker; past that it
    reports reason "timeout" and leaves the worker running (a second call reports "in_progress" instead of starting another).
    Access denied -> ok False, reason "needs_admin"."""
    if (g := _call_guard()) is not None:
        return g
    from connector import readiness
    ht = getattr(readiness, "_hf_thread", [None])[0]
    if ht is not None and ht.is_alive():
        return {"ok": False, "reason": "in_progress", "by": "readiness"}
    try:
        mac = _mac()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "no_phone", "error": _err(e)}
    if not mac:
        return {"ok": False, "reason": "no_phone"}
    st = start_hf_refresh(mac)
    if not st["started"]:
        return {"ok": False, "reason": "in_progress", "age": st["age"]}
    _job["done"].wait(timeout)
    if _job["result"] is None:
        return {"ok": False, "reason": "timeout", "pending": True, "waited": timeout}
    return _job["result"]


# ---- phone_connect -----------------------------------------------------------------------------------------------------
async def _connect(mac: str, timeout: float) -> dict:
    dev = await _open_device(mac)
    if dev is None:
        return {"ok": False, "reason": "not_paired"}
    try:
        if not _device_paired(dev):
            return {"ok": False, "reason": "not_paired"}
        if _device_connected(dev):
            return {"ok": True, "reason": "", "already_connected": True}
        out = {"ok": False, "reason": "not_connected"}
        try:
            out["rfcomm"] = await _rfcomm_probe(dev)
        except Exception as e:  # noqa: BLE001
            out["rfcomm_error"] = _err(e)
        t0 = time.monotonic()
        while True:
            if _device_connected(dev):
                out.update(ok=True, reason="", connect_secs=round(time.monotonic() - t0, 1))
                return out
            if time.monotonic() - t0 >= timeout:
                return out
            await _asleep(1.0)
    finally:
        _device_close(dev)


def phone_connect(timeout: float = CONNECT_TIMEOUT) -> dict:
    """Best-effort connect of the paired iPhone: an uncached RFCOMM lookup of HandsfreeAG makes Windows open the link.
    ok when connection_status is CONNECTED within `timeout` s (default 15)."""
    if (g := _call_guard()) is not None:
        return g
    try:
        mac = _mac()
        if not mac or not _MAC_RE.match(mac):
            return {"ok": False, "reason": "no_phone"}
        return asyncio.run(_connect(mac, timeout))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "connect_error", "error": _err(e)}


# ---- registration ------------------------------------------------------------------------------------------------------
ACTIONS = (("radio_on", radio_on, "safe"), ("hf_refresh", hf_refresh, "safe"), ("phone_connect", phone_connect, "safe"),
           ("radio_cycle", radio_cycle, "heavy"))


def register(heal=None) -> None:
    """Register the four actions with connector.heal (or a stand-in with the same register_action)."""
    if heal is None:
        from connector import heal
    for name, fn, tier in ACTIONS:
        heal.register_action(name, fn, tier=tier)

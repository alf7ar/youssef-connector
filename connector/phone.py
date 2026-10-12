"""Windows side of the iPhone link (pywinrt, unpackaged): Bluetooth radio, iPhone pairing/connection, the Hands-Free
audio endpoints, and the system call state (Windows.ApplicationModel.Calls.PhoneCallManager).

Dialing/hang-up through PhoneLineTransportDevice needs package identity with the restricted capability
phoneLineTransportManagement (RequestAccessAsync -> DeniedBySystem, RegisterApp/Connect -> E_ACCESSDENIED from plain
Python). Calls go through Phone Link instead (plcall.ps1: tel: launch + its Call button over UI Automation); this
module also reads the audio endpoints from the MMDevices registry and switches the default devices (IPolicyConfig)."""
import asyncio
import json
import re
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import winrt.windows.applicationmodel.calls as C
from winrt.windows.devices.bluetooth import BluetoothConnectionStatus, BluetoothDevice

try:
    from winrt.windows.devices.radios import Radio, RadioKind, RadioState
except Exception:  # noqa: BLE001
    Radio = None

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


def mac_int(mac: str) -> int:
    return int(mac.replace(":", ""), 16)


async def bt_radio(turn_on: bool = False) -> dict:
    """{'present', 'on', 'error'}; turn_on=True switches an off Bluetooth radio on."""
    if Radio is None:
        return {"present": None, "on": None, "error": "winrt Radios package missing"}
    try:
        radios = await Radio.get_radios_async()
        bt = [r for r in radios if r.kind == RadioKind.BLUETOOTH]
        if not bt:
            return {"present": False, "on": False, "error": None}
        r = bt[0]
        if turn_on and r.state != RadioState.ON:
            await Radio.request_access_async()
            await r.set_state_async(RadioState.ON)
            await asyncio.sleep(1.5)
        return {"present": True, "on": r.state == RadioState.ON, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"present": None, "on": None, "error": f"{type(e).__name__}: {e}"[:200]}


_find_err = [None]


async def find_iphone_mac() -> str:
    """MAC of a paired classic-Bluetooth device whose name contains "iPhone" ("" if none). Several iPhones may be paired:
    the one that is CONNECTED now wins, else the first found. A failure is logged once (not swallowed)."""
    from winrt.windows.devices.enumeration import DeviceInformation
    first = ""
    try:
        sel = BluetoothDevice.get_device_selector_from_pairing_state(True)
        # pywinrt 3.x names the selector overload find_all_async_aqs_filter; older versions overload find_all_async
        find = getattr(DeviceInformation, "find_all_async_aqs_filter", None) or DeviceInformation.find_all_async
        for di in await find(sel):
            if "iphone" in (di.name or "").lower():
                d = await BluetoothDevice.from_id_async(di.id)
                if d is not None:
                    a = f"{d.bluetooth_address:012X}"
                    connected = d.connection_status == BluetoothConnectionStatus.CONNECTED
                    d.close()
                    mac = ":".join(a[i:i + 2] for i in range(0, 12, 2))
                    if connected:
                        return mac
                    first = first or mac
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"[:200]
        if _find_err[0] != msg:
            _find_err[0] = msg
            print("find_iphone_mac failed:", msg, flush=True)
    return first


async def paired_iphones() -> list:
    """Every paired classic-Bluetooth device whose name contains "iPhone": [{'mac', 'name', 'connected'}] (status page list)."""
    from winrt.windows.devices.enumeration import DeviceInformation
    out = []
    sel = BluetoothDevice.get_device_selector_from_pairing_state(True)
    find = getattr(DeviceInformation, "find_all_async_aqs_filter", None) or DeviceInformation.find_all_async
    for di in await find(sel):
        if "iphone" in (di.name or "").lower():
            d = await BluetoothDevice.from_id_async(di.id)
            if d is None:
                continue
            a = f"{d.bluetooth_address:012X}"
            connected = d.connection_status == BluetoothConnectionStatus.CONNECTED
            name = d.name or di.name or ""
            d.close()
            out.append({"mac": ":".join(a[i:i + 2] for i in range(0, 12, 2)), "name": name, "connected": connected})
    return out


async def iphone(mac: str) -> dict:
    """{'mac', 'name', 'paired', 'connected'} for the phone at `mac`."""
    out = {"mac": mac, "name": None, "paired": False, "connected": False}
    if not mac or not MAC_RE.match(mac):
        return out
    try:
        d = await BluetoothDevice.from_bluetooth_address_async(mac_int(mac))
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"[:200]
        return out
    if d is None:
        return out
    out["name"] = d.name
    out["paired"] = bool(d.device_information.pairing.is_paired)
    out["connected"] = d.connection_status == BluetoothConnectionStatus.CONNECTED
    d.close()
    return out


MM_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"
MM_STATE = {1: "active", 2: "disabled", 4: "notpresent", 8: "unplugged"}


def endpoints() -> list:
    """Every audio endpoint in the MMDevices registry: {flow: "render"|"capture", id: MMDevice ID, name, device, state}."""
    import winreg
    out = []
    for flow, prefix in (("Render", "{0.0.0.00000000}"), ("Capture", "{0.0.1.00000000}")):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{MM_KEY}\\{flow}")
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                guid = winreg.EnumKey(root, i)
                try:
                    with winreg.OpenKey(root, guid + "\\Properties") as pk:
                        dev = str(winreg.QueryValueEx(pk, "{b3f8fa53-0004-438e-9003-51a46e139bfc},6")[0])
                        name = str(winreg.QueryValueEx(pk, "{a45c254e-df1c-4efd-8020-67d146a850e0},2")[0])
                    with winreg.OpenKey(root, guid) as ek:
                        st = int(winreg.QueryValueEx(ek, "DeviceState")[0])
                except OSError:
                    continue
                fmt = None
                try:  # PKEY_AudioEngine_DeviceFormat: PROPVARIANT-wrapped WAVEFORMATEX(TENSIBLE) -> "16000Hz/16bit/1ch"
                    with winreg.OpenKey(root, guid + "\\Properties") as pk:
                        b = bytes(winreg.QueryValueEx(pk, "{f19f064d-082c-4e27-bc73-6882a1bb8e4c},0")[0])
                    _tag, ch, sr, _avg, _al, bits = struct.unpack("<HHIIHH", b[8:24])
                    fmt = f"{sr}Hz/{bits}bit/{ch}ch"
                except Exception:  # noqa: BLE001
                    pass
                out.append({"flow": flow.lower(), "id": f"{prefix}.{guid}", "name": name, "device": dev, "state": st,
                            "format": fmt})
    return out


def hf_endpoints() -> dict:
    """iPhone Hands-Free endpoints {"capture": {...}, "render": {...}} (state 1 = hands-free link up)."""
    out = {}
    for e in endpoints():
        d = e["device"].lower()
        if "iphone" in d and "hands-free" in d and (e["flow"] not in out or e["state"] < out[e["flow"]]["state"]):
            out[e["flow"]] = e
    return out


def hf_link() -> str:
    """State of the iPhone Hands-Free audio endpoints from the MMDevices registry: "active" (hands-free link up: a call's
    audio is on the laptop), "unplugged" (paired, no hands-free link), "disabled", "notpresent" or "absent"."""
    states = [e["state"] for e in hf_endpoints().values()]
    if not states:
        return "absent"
    if all(st == 1 for st in states):
        return "active"
    return MM_STATE.get(max(states), str(max(states)))


def cable_endpoints() -> dict:
    """Active virtual-cable endpoints for the "cable" route: render "CABLE Input" (the call's speaker) and capture
    "Hi-Fi Cable Output" (the call's microphone)."""
    out = {}
    for e in endpoints():
        if e["state"] != 1:
            continue
        if e["flow"] == "render" and e["name"] == "CABLE Input" and "VB-Audio Virtual Cable" in e["device"]:
            out["render"] = e["id"]
        if e["flow"] == "capture" and e["name"] == "Hi-Fi Cable Output":
            out["capture"] = e["id"]
    return out


def _com_thread(fn):
    """Run fn on a fresh MTA thread (comtypes initialises COM on import/use; keep it off the asyncio/winrt threads)."""
    box = {}

    def run():
        try:
            sys.coinit_flags = 0  # COINIT_MULTITHREADED, read by comtypes at import
            import comtypes
            comtypes.CoInitializeEx(0)
            box["r"] = fn(comtypes)
        except Exception as e:  # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(15)
    if "e" in box:
        raise box["e"]
    return box.get("r")


def get_default_endpoints() -> dict:
    """{"render": [console, multimedia, communications], "capture": [...]} MMDevice IDs."""
    def fn(comtypes):
        from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
        from pycaw.constants import CLSID_MMDeviceEnumerator
        en = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
        out = {}
        for flow, name in ((0, "render"), (1, "capture")):
            ids = []
            for role in (0, 1, 2):
                try:
                    ids.append(en.GetDefaultAudioEndpoint(flow, role).GetId())
                except Exception:  # noqa: BLE001
                    ids.append(None)
            out[name] = ids
        return out
    return _com_thread(fn)


def set_default_endpoint(endpoint_id: str, roles=(0, 1)) -> None:
    """Make endpoint_id the default for the given roles (0 console, 1 multimedia, 2 communications), current user."""
    def fn(comtypes):
        from ctypes import c_int, c_wchar_p
        from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

        class IPolicyConfig(IUnknown):
            _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
            _methods_ = [COMMETHOD([], HRESULT, f"Unused{i}") for i in range(10)] + [
                COMMETHOD([], HRESULT, "SetDefaultEndpoint", (["in"], c_wchar_p, "id"), (["in"], c_int, "role"))]
        pc = comtypes.CoCreateInstance(GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}"), IPolicyConfig,
                                       comtypes.CLSCTX_ALL)
        for r in roles:
            pc.SetDefaultEndpoint(endpoint_id, r)
    _com_thread(fn)


PLCALL = Path(__file__).with_name("plcall.ps1")


async def phone_link(action: str, number: str = "") -> dict:
    """plcall.ps1 dial|hangup|dump: Phone Link over UI Automation (must run in the owner's desktop session)."""
    args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PLCALL), "-Action", action]
    if number:
        args += ["-Number", number]
    p = await asyncio.create_subprocess_exec(*args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        out, err = await asyncio.wait_for(p.communicate(), 40)
    except asyncio.TimeoutError:
        p.kill()
        return {"ok": False, "error": "plcall timeout"}
    txt = out.decode("utf-8", "replace").strip()
    try:
        return json.loads(txt.splitlines()[-1])
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": (txt + " " + err.decode("utf-8", "replace"))[-300:]}


# ---- Phone Service call control (PhoneOm.dll, the in-box client of PhoneSvc; undocumented, reverse-engineered on this
# laptop 2026-10-10, Win10 19045). The public WinRT PhoneCall.End needs CallsPhoneContract v6+ (Windows 11) and Phone Link
# 1.25072 on Windows 10 has no in-call window, so this is the only working end-call path. Verified live on 888 calls:
# PhoneEnd ended the call in 0.3 s; PhoneSetBluetoothHfpCallAudioTransfer(line, 1) moved the call audio to the laptop in
# 2 s (0 = back to the iPhone). RPC interface 8be456ec-9244-4d10-88e8-1ddf1baa9ade:
#   PhoneEnd(LONG *callId)                                   (proc 1)
#   PhoneSetBluetoothHfpCallAudioTransfer(GUID *line, LONG toHandsFree)   (proc 28)
#   PhoneGetState(PH_CALL_INFO **calls, UINT *count, BYTE *state) -> array of 0x19C4-byte records:
#       +0xBC8 state (0 ambiguous, 1 incoming, 2 calling, 3 talking, 4 hold, 5 idle, 6 transfer), +0xBE4 call id
_POM = None
_POM_LOCK = threading.Lock()
CALL_STATES = {0: "ambiguous", 1: "incoming", 2: "calling", 3: "talking", 4: "hold", 5: "idle", 6: "transfer"}
_CALL_REC = 0x19C4


def _pom():
    global _POM
    with _POM_LOCK:
        if _POM is None:
            import ctypes
            om = ctypes.WinDLL("PhoneOm.dll")
            hr = om.PhoneAPIInitialize() & 0xFFFFFFFF
            if hr:
                raise OSError(f"PhoneAPIInitialize hr={hr:#010x}")
            _POM = om
        return _POM


def phone_calls() -> list:
    """Calls Windows' Phone Service knows (the iPhone line over Bluetooth): [{"id", "state"}] (idle ones included)."""
    import ctypes
    om = _pom()
    p, n, extra = ctypes.c_void_p(), ctypes.c_uint(), (ctypes.c_ubyte * 256)()
    hr = om.PhoneGetState(ctypes.byref(p), ctypes.byref(n), extra) & 0xFFFFFFFF
    if hr:
        raise OSError(f"PhoneGetState hr={hr:#010x}")
    out = []
    try:
        for i in range(n.value):
            b = ctypes.string_at(p.value + i * _CALL_REC, _CALL_REC)
            st = int.from_bytes(b[0xBC8:0xBCC], "little")
            out.append({"id": int.from_bytes(b[0xBE4:0xBE8], "little"), "state": CALL_STATES.get(st, str(st))})
    finally:
        if p.value:
            om.PhoneFreeCallInfo(p)
    return out


def end_calls() -> dict:
    """End every live call on the line (talking, calling/ringing out, incoming, held). Blocking; run in a thread."""
    import ctypes
    om = _pom()
    ended = []
    for c in phone_calls():
        if c["state"] in ("idle", "ambiguous"):
            continue
        cid = ctypes.c_long(c["id"])
        hr = om.PhoneEnd(ctypes.byref(cid)) & 0xFFFFFFFF
        ended.append({"id": c["id"], "state": c["state"], "hr": f"{hr:#x}"})
    left = []
    for _ in range(12):  # confirm (normally < 0.5 s)
        left = [c for c in phone_calls() if c["state"] not in ("idle", "ambiguous")]
        if not left:
            break
        time.sleep(0.25)
    return {"ok": bool(ended) and not left, "ended": ended, "left": left}


def audio_to_laptop(on: bool = True) -> dict:
    """Move the current call's audio to the laptop's Hands-Free link (on) or back to the iPhone (off)."""
    import ctypes
    om = _pom()
    line = (ctypes.c_ubyte * 16)()
    hr = om.PhoneGetDefaultOutgoingLine(line) & 0xFFFFFFFF
    if hr:
        return {"ok": False, "error": f"PhoneGetDefaultOutgoingLine hr={hr:#010x}"}
    hr = om.PhoneSetBluetoothHfpCallAudioTransfer(line, 1 if on else 0) & 0xFFFFFFFF
    return {"ok": hr == 0, "hr": f"{hr:#x}"}


def call_flags() -> dict:
    """System call state as Windows sees it (Bluetooth phone line included)."""
    try:
        return {"call_active": bool(C.PhoneCallManager.is_call_active),
                "call_incoming": bool(C.PhoneCallManager.is_call_incoming)}
    except Exception as e:  # noqa: BLE001
        return {"call_active": None, "call_incoming": None, "error": f"{type(e).__name__}: {e}"[:200]}


# ---- iPhone Hands-Free service refresh (bthprops BluetoothSetServiceState on the iPhone's HandsfreeAG 0x111F service).
# Measured 2026-10-10 12:19 (idle, no call): disable blocked 22.8 s and removed both HF endpoints (state 4), enable took
# 3 s, and the endpoints came back as normal idle (8, "unplugged") about 90 s later. Never run it during a call.
HFAG_UUID = "0000111f-0000-1000-8000-00805f9b34fb"


def hf_abnormal(hf: dict) -> bool:
    """True when the idle Hands-Free endpoints are not in a usable state: an endpoint missing or "notpresent" (4)."""
    return any(k not in hf or hf[k].get("state") == 4 for k in ("capture", "render"))


def refresh_hf_service(mac: str, wait: float = 120.0) -> dict:
    """Toggle the iPhone's Hands-Free service off and on, then wait (<= wait s) for both HF endpoints to be back in a
    normal state (1 active or 8 unplugged). Blocking: run in a thread, and only when no call is active."""
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class SYSTEMTIME(ctypes.Structure):
        _fields_ = [(n, wintypes.WORD) for n in ("y", "mo", "dow", "d", "h", "mi", "s", "ms")]

    class BDI(ctypes.Structure):  # BLUETOOTH_DEVICE_INFO
        _fields_ = [("dwSize", wintypes.DWORD), ("Address", ctypes.c_ulonglong), ("ulClassofDevice", wintypes.ULONG),
                    ("fConnected", wintypes.BOOL), ("fRemembered", wintypes.BOOL), ("fAuthenticated", wintypes.BOOL),
                    ("stLastSeen", SYSTEMTIME), ("stLastUsed", SYSTEMTIME), ("szName", wintypes.WCHAR * 248)]

    def states():
        return {k: e.get("state") for k, e in hf_endpoints().items()}

    out = {"ok": False, "before": states()}
    if not mac or not MAC_RE.match(mac):
        out["error"] = "no iPhone MAC"
        return out
    b = uuid.UUID(HFAG_UUID).bytes
    g = GUID(int.from_bytes(b[0:4], "big"), int.from_bytes(b[4:6], "big"), int.from_bytes(b[6:8], "big"))
    g.Data4[:] = list(b[8:16])
    bt = ctypes.WinDLL("bthprops.cpl")
    info = BDI()
    info.dwSize = ctypes.sizeof(BDI)
    info.Address = mac_int(mac)
    rc = bt.BluetoothGetDeviceInfo(None, ctypes.byref(info))
    out["bt_connected"] = bool(info.fConnected)
    if rc:
        out["error"] = f"BluetoothGetDeviceInfo rc={rc}"
        return out
    t0 = time.monotonic()
    out["disable_rc"] = bt.BluetoothSetServiceState(None, ctypes.byref(info), ctypes.byref(g), 0)
    out["disable_secs"] = round(time.monotonic() - t0, 1)
    time.sleep(1)
    out["after_disable"] = states()
    t1 = time.monotonic()
    out["enable_rc"] = bt.BluetoothSetServiceState(None, ctypes.byref(info), ctypes.byref(g), 1)
    while time.monotonic() - t1 < wait:
        s = states()
        if all(s.get(k) in (1, 8) for k in ("capture", "render")):
            break
        time.sleep(1)
    out["after"] = states()
    out["back_secs"] = round(time.monotonic() - t1, 1)
    out["ok"] = out["enable_rc"] == 0 and all(out["after"].get(k) in (1, 8) for k in ("capture", "render"))
    return out

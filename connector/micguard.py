"""Keep the room out of the call: while a call session is active, the laptop's own default microphone (the physical default
capture endpoint for the console and communications roles, e.g. Realtek "Microphone Array") is muted; its previous mute
state is restored when the session ends. The iPhone Hands-Free capture and the virtual cables are never touched.
Crash safety: the endpoints muted here are written to mic-mute.json BEFORE muting, and restore_leftover() (app start)
unmutes whatever a crashed session left behind. COM runs on a fresh MTA thread with sys.coinit_flags = 0 set before
comtypes is imported (same approach as phone._com_thread)."""
import json
import os
import sys
import threading
from pathlib import Path

HOME = Path(os.environ.get("YOUSSEF_HOME") or os.environ.get("YC_HOME") or (Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "YoussefConnector"))
STATE = HOME / "mic-mute.json"
ROLES = (0, 2)   # eConsole, eCommunications


_mta_keeper = []


def _keep_mta_alive():
    """One daemon thread that joins the MTA and never leaves, so the MTA (and every comtypes object created in it) outlives
    the short-lived COM threads. Without it, a COM wrapper left for the garbage collector was Released after its apartment
    was gone: access violation in _ctypes (live test 2026-10-11, GC ran inside the soundcard import on the audio thread)."""
    if _mta_keeper:
        return
    ready = threading.Event()

    def run():
        try:
            sys.coinit_flags = 0
            import comtypes
            comtypes.CoInitializeEx(0)
        except Exception:  # noqa: BLE001  (no comtypes: tests / non-Windows)
            return
        finally:
            ready.set()
        threading.Event().wait()
    t = threading.Thread(target=run, daemon=True, name="mta-keeper")
    _mta_keeper.append(t)
    t.start()
    ready.wait(10)


def _com_thread(fn):
    box = {}
    _keep_mta_alive()

    def run():
        try:
            sys.coinit_flags = 0  # COINIT_MULTITHREADED, read by comtypes at import
            import comtypes
            comtypes.CoInitializeEx(0)
            box["r"] = fn(comtypes)
        except Exception as e:  # noqa: BLE001
            box["e"] = e
        finally:
            import gc
            gc.collect()   # release this thread's COM wrappers here, while it is still in the apartment
    t = threading.Thread(target=run, daemon=True, name="micguard-com")
    t.start()
    t.join(15)
    if "e" in box:
        raise box["e"]
    return box.get("r")


class WinMics:
    """The real backend (Windows Core Audio through pycaw/comtypes)."""

    def _ev(self, comtypes, en, endpoint_id):
        from ctypes import POINTER, cast
        from pycaw.pycaw import IAudioEndpointVolume
        dev = en.GetDevice(endpoint_id)
        iface = dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
        return cast(iface, POINTER(IAudioEndpointVolume))

    def _en(self, comtypes):
        from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
        from pycaw.constants import CLSID_MMDeviceEnumerator
        return comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)

    def default_capture(self) -> list:
        def fn(comtypes):
            en, ids = self._en(comtypes), []
            for role in ROLES:
                try:
                    i = en.GetDefaultAudioEndpoint(1, role).GetId()
                except Exception:  # noqa: BLE001  (no capture device)
                    continue
                if i not in ids:
                    ids.append(i)
            return ids
        return _com_thread(fn)

    def get_mute(self, endpoint_id: str) -> bool:
        return _com_thread(lambda c: bool(self._ev(c, self._en(c), endpoint_id).GetMute()))

    def set_mute(self, endpoint_id: str, mute: bool) -> None:
        _com_thread(lambda c: self._ev(c, self._en(c), endpoint_id).SetMute(1 if mute else 0, None))


def _short(i: str) -> str:
    return str(i)[-14:]


def mute(exclude=(), log=None, mics=None, state: Path = None) -> dict:
    """Mute the default capture endpoint(s) not in `exclude` (lowercase IDs: Hands-Free capture, cables). Only endpoints
    that were unmuted are muted (and recorded for restore). Never raises; logs mic_mute."""
    mics, state, log = mics or WinMics(), state or STATE, log or (lambda e, **kw: None)
    ex = {str(x).lower() for x in exclude}
    info = {"muted": [], "already_muted": [], "skipped": []}
    try:
        targets = []
        for i in mics.default_capture():
            if str(i).lower() in ex:
                info["skipped"].append(_short(i))
            elif mics.get_mute(i):
                info["already_muted"].append(_short(i))
            else:
                targets.append(i)
        if targets:   # recorded before muting: a crash in between still gets restored at the next start
            saved = _read(state)
            state.write_text(json.dumps(sorted(set(saved) | set(targets))), encoding="utf-8")
        for i in targets:
            mics.set_mute(i, True)
            info["muted"].append(_short(i))
        info["after_mute"] = [mics.get_mute(i) for i in targets]
    except Exception as e:  # noqa: BLE001
        info["error"] = f"{type(e).__name__}: {e}"[:150]
    log("mic_mute", **info)
    return info


def _read(state: Path) -> list:
    try:
        return [str(x) for x in json.loads(state.read_text(encoding="utf-8"))]
    except Exception:  # noqa: BLE001
        return []


def restore(log=None, mics=None, state: Path = None, why: str = "call_end") -> dict:
    """Unmute the endpoints mute() muted (from the state file), then delete it. Never raises; logs mic_restore."""
    mics, state, log = mics or WinMics(), state or STATE, log or (lambda e, **kw: None)
    ids = _read(state)
    info = {"why": why, "restored": [], "failed": []}
    if not ids and not state.exists():
        return info
    for i in ids:
        try:
            mics.set_mute(i, False)
            info["restored"].append(_short(i))
        except Exception as e:  # noqa: BLE001  (endpoint gone: it comes back unmuted by default; keep trying next time)
            info["failed"].append(f"{_short(i)} {type(e).__name__}"[:60])
    try:
        if info["failed"]:
            state.write_text(json.dumps([i for i in ids if _short(i) not in info["restored"]]), encoding="utf-8")
        else:
            state.unlink()
    except OSError:
        pass
    log("mic_restore", **info)
    return info

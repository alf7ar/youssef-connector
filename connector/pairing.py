"""Gated auto-pair: pairs the iPhone from the Windows side when nobody has paired one yet, or when "add phone" was pressed.

The gate is open when (a) no phone is paired, or (b) request_add_phone() was called within the last ADD_WINDOW_S seconds
(status page POST /add-phone; a hub "add_phone" command can call the same method). Never during a call.
With the gate open we scan UNPAIRED Bluetooth devices and pair only when EXACTLY one candidate (name contains "iphone" or
Bluetooth class Phone) is in range, so a neighbour's phone is never picked from a list. Windows-side prompts (confirm / PIN
match) are accepted automatically; the person still has to press Pair on the iPhone. A paired phone drives heal.report(
"iphone_not_connected") so the heal ladder connects it and refreshes Hands-Free.

winrt is imported lazily (only in the thin Windows glue at the bottom), so this module imports and tests on any platform.
No Bluetooth address or device id is ever logged."""
import asyncio
import hashlib
import importlib
import json
import os
import threading
import time
from pathlib import Path

ADD_WINDOW_S = 120.0       # "add phone" keeps the gate open this long
PAIR_TIMEOUT_S = 60.0      # pair_async gives up after this
NO_PHONE_EVERY_S = 60.0    # gate (a): s between phone-paired checks / scans with nothing found
ADD_SCAN_EVERY_S = 5.0     # gate (b): s between scans while waiting for the phone to appear
AFTER_ATTEMPT_S = 120.0    # s of quiet after a pairing attempt that failed on the no-phone gate (b: ADD_SCAN_EVERY_S)
# gate (a) backoff, per device name (stranger safety): at most once an hour, 3 a day, never again after 2 declines/time-outs.
# The add-phone window (a person asking for it) bypasses all of it. Persisted in HOME so a restart does not reset it.
BACKOFF_HOUR_S = 3600.0
BACKOFF_DAY_S = 86400.0
BACKOFF_PER_DAY = 3
BACKOFF_MAX_DECLINES = 2
BACKOFF_FILE = "pairing_backoff.json"
MSG_PAIR_AR = "اضغط Pair على الآيفون"
MSG_PAIR_EN = "Press Pair on the iPhone"
PAIRED_STATUSES = (0, 3)   # DevicePairingResultStatus.PAIRED, ALREADY_PAIRED
_active = [0]              # pair attempts running right now (all Pairers): busy() for the readiness / heal gates


def _sibling(name):
    """Sibling module whether this file is used inside the `connector` package or flat; None when it cannot be imported."""
    try:
        return importlib.import_module("." + name, __package__) if __package__ else importlib.import_module(name)
    except ImportError:
        return None


# Resolved lazily (first use): importing phone/heal pulls in winrt, which must load AFTER onnxruntime (PORT_NOTES.md), and
# main.py imports this module at start-up. Tests set these to a stub or None.
_UNSET = object()
heal = _UNSET    # owner H1; None when it is not merged
phone = _UNSET   # needs the Windows-only winrt packages


def _get(name):
    g = globals()
    if g[name] is _UNSET:
        g[name] = _sibling(name)
    return g[name]


def busy() -> bool:
    """True while a pair attempt (the unpaired-device inquiry or pair_async) runs: Bluetooth is busy, nothing else should
    reset the radio or refresh Hands-Free meanwhile."""
    return _active[0] > 0


def pick_candidates(devices) -> list:
    """Unpaired devices that look like a phone. devices: iterable of (name, is_phone_class). A phone by name ("iphone") or by
    Bluetooth major class Phone."""
    return [(n, c) for n, c in devices if "iphone" in (n or "").lower() or c]


async def pair_device(di, timeout: float = PAIR_TIMEOUT_S, kinds=None) -> dict:
    """Pair the DeviceInformation `di` with custom pairing, accepting every Windows-side prompt. Returns
    {"ok", "status", "error"}; never raises. Times out after `timeout` s."""
    prompts = []
    custom = None
    token = None
    try:
        custom = di.pairing.custom

        def on_request(_sender, args):
            prompts.append(getattr(getattr(args, "pairing_kind", None), "name", None) or str(getattr(args, "pairing_kind", "")))
            args.accept()
        token = custom.add_pairing_requested(on_request)

        async def run():
            return await custom.pair_async(kinds)
        res = await asyncio.wait_for(run(), timeout)
        st = res.status
        return {"ok": int(st) in PAIRED_STATUSES, "status": getattr(st, "name", None) or str(st), "prompts": len(prompts)}
    except asyncio.TimeoutError:
        return {"ok": False, "status": "timeout", "prompts": len(prompts)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": "error", "error": f"{type(e).__name__}: {e}"[:200], "prompts": len(prompts)}
    finally:
        if custom is not None and token is not None:
            try:
                custom.remove_pairing_requested(token)
            except Exception:  # noqa: BLE001
                pass


class Pairer:
    """One per app. tick() is cheap and safe to call every 2 s from a daemon thread; it scans/pairs only when due and the gate is open.

    Injection points (tests): has_phone() -> bool, scan() -> async list of candidates (opaque objects with .name),
    pair(candidate) -> async {"ok", "status"}, in_call() -> bool (set `in_call` later to widen it, e.g. with the system call
    state), clock(). `home` persists the gate-(a) backoff (None = in memory only); `wall` is its wall clock."""

    def __init__(self, log=None, *, has_phone=None, scan=None, pair=None, in_call=None, clock=time.monotonic, on_paired=None,
                 home=None, wall=time.time):
        self.log = log or (lambda *a: None)
        self.has_phone = has_phone or _default_has_phone
        self.scan = scan or _default_scan
        self.pair = pair or _default_pair
        self.in_call = in_call or (lambda: False)
        self.clock = clock
        self.on_paired = on_paired or _report_paired
        self._lock = threading.Lock()
        self._add_until = 0.0
        self._next_at = 0.0
        self._pairing = False
        self._busy = False
        self._attempting = False
        self.home, self.wall = (Path(home) if home else None), wall
        self._backoff = self._load_backoff()
        self._last = None      # {"result", "name", "t"} of the last attempt
        self._thread = None
        self._stop = threading.Event()

    def _log(self, ev, **kw):
        try:
            self.log(ev, json.dumps(kw, ensure_ascii=False, default=str) if kw else "")
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- gate
    def request_add_phone(self) -> float:
        """Open the gate for ADD_WINDOW_S seconds ("add phone" pressed). Returns the window length."""
        with self._lock:
            self._add_until = self.clock() + ADD_WINDOW_S
            self._next_at = 0.0
        self._log("pairing_add_phone", window_s=ADD_WINDOW_S)
        return ADD_WINDOW_S

    def add_window_open(self) -> bool:
        return self.clock() < self._add_until

    def busy(self) -> bool:
        """True while a pair attempt (scan + pair_async) of this Pairer runs."""
        return self._attempting

    # ---------------------------------------------------------------- stranger backoff (gate (a) only)
    @staticmethod
    def _key(name) -> str:
        return hashlib.sha1((name or "?").strip().lower().encode("utf-8", "replace")).hexdigest()[:12]

    def _load_backoff(self) -> dict:
        if self.home is None:
            return {}
        try:
            d = json.loads((self.home / BACKOFF_FILE).read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _save_backoff(self):
        if self.home is None:
            return
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            p = self.home / BACKOFF_FILE
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(self._backoff), encoding="utf-8")
            os.replace(tmp, p)
        except Exception as e:  # noqa: BLE001
            self._log("pairing_backoff_save_failed", error=f"{type(e).__name__}: {e}"[:150])

    def _backoff_blocks(self, name) -> str:
        """"" when gate (a) may prompt this device now, else why not: "declined" / "hour" / "day"."""
        e = self._backoff.get(self._key(name))
        if not e:
            return ""
        now = self.wall()
        if e.get("declines", 0) >= BACKOFF_MAX_DECLINES:
            return "declined"
        times = [t for t in e.get("times", []) if now - t < BACKOFF_DAY_S]
        if times and now - max(times) < BACKOFF_HOUR_S:
            return "hour"
        if len(times) >= BACKOFF_PER_DAY:
            return "day"
        return ""

    def _backoff_record(self, name, outcome: str):
        """outcome: "start" (an attempt begins: counts toward the hour/day limits), "declined" (failed/timed out), "paired"."""
        k = self._key(name)
        if outcome == "paired":
            self._backoff.pop(k, None)
        else:
            e = self._backoff.setdefault(k, {"times": [], "declines": 0})
            now = self.wall()
            if outcome == "start":
                e["times"] = [t for t in e.get("times", []) if now - t < BACKOFF_DAY_S] + [now]
            else:
                e["declines"] = e.get("declines", 0) + 1
        self._save_backoff()

    def status(self) -> dict:
        d = {"active": self._pairing, "add_phone": self.add_window_open() and not self._pairing,
             "last": self._last}
        if self._pairing:
            d["message_ar"], d["message_en"] = MSG_PAIR_AR, MSG_PAIR_EN
        return d

    # ---------------------------------------------------------------- one attempt
    async def attempt(self, why: str) -> str:
        """Scan unpaired devices and pair iff exactly one candidate. Returns the result tag (also logged)."""
        self._attempting = True
        _active[0] += 1
        try:
            return await self._attempt(why)
        finally:
            self._attempting = False
            _active[0] = max(0, _active[0] - 1)

    async def _attempt(self, why: str) -> str:
        try:
            cands = list(await self.scan())
        except Exception as e:  # noqa: BLE001
            return self._done(why, "scan_failed", None, error=f"{type(e).__name__}: {e}"[:150])
        if not cands:
            return self._done(why, "no_candidate", None)
        if len(cands) > 1:
            return self._done(why, "ambiguous", None, candidates=len(cands))
        c = cands[0]
        name = getattr(c, "name", None)
        if self.in_call():   # a call can start during the inquiry: the check at tick() time is not enough
            return self._done(why, "in_call", name)
        gated = why != "add_phone"
        if gated:
            blocked = self._backoff_blocks(name)
            if blocked:
                return self._done(why, "backoff", name, reason=blocked)
            self._backoff_record(name, "start")
        self._pairing = True
        self._log("pairing_start", why=why, name=name)
        try:
            r = await self.pair(c)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "status": "error", "error": f"{type(e).__name__}: {e}"[:150]}
        finally:
            self._pairing = False
        if r.get("ok"):
            with self._lock:
                self._add_until = 0.0
            self._backoff_record(name, "paired")
            self._done(why, "paired", name, status=r.get("status"))
            try:
                self.on_paired()
            except Exception as e:  # noqa: BLE001
                self._log("pairing_report_failed", error=f"{type(e).__name__}: {e}"[:150])
            return "paired"
        if gated and r.get("status") != "error":   # declined / timed out / rejected (not our own exception)
            self._backoff_record(name, "declined")
        return self._done(why, "failed", name, status=r.get("status"), error=r.get("error"))

    def _done(self, why, result, name, **kw) -> str:
        self._last = {"result": result, "name": name, "t": time.time()}
        self._log("pairing_attempt", why=why, result=result, name=name, **{k: v for k, v in kw.items() if v is not None})
        return result

    # ---------------------------------------------------------------- driver
    def tick(self) -> str | None:
        """Run one due step. Returns the attempt result tag, "has_phone", or None (nothing due / gate closed / in a call)."""
        now = self.clock()
        if self._busy or now < self._next_at or self.in_call():
            return None
        self._busy = True
        try:
            adding = self.add_window_open()
            if not adding:
                try:
                    have = bool(self.has_phone())
                except Exception as e:  # noqa: BLE001
                    self._log("pairing_check_failed", error=f"{type(e).__name__}: {e}"[:150])
                    have = True    # unknown: do not pair
                if have:
                    self._next_at = self.clock() + NO_PHONE_EVERY_S
                    return "has_phone"
            res = asyncio.run(self.attempt("add_phone" if adding else "no_phone"))
            adding = self.add_window_open()    # re-read: a press during the attempt must not be pushed back to AFTER_ATTEMPT_S
            gap = ADD_SCAN_EVERY_S if adding else (NO_PHONE_EVERY_S if res == "no_candidate" else AFTER_ATTEMPT_S)   # backoff/in_call: the long gap
            self._next_at = self.clock() + gap
            return res
        finally:
            self._busy = False

    def start(self, every: float = 2.0):
        if self._thread is not None:
            return

        def loop():
            while not self._stop.wait(every):
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001
                    self._log("pairing_tick_failed", error=f"{type(e).__name__}: {e}"[:150])
        self._thread = threading.Thread(target=loop, daemon=True, name="pairing")
        self._thread.start()

    def stop(self):
        self._stop.set()


# ------------------------------------------------------------------ thin Windows glue (winrt; not unit tested)
class _Cand:
    def __init__(self, di, name):
        self.di, self.name = di, name


def _report_paired():
    h = _get("heal")
    if h is not None:
        h.report("iphone_not_connected", source="pairing")


def _default_has_phone() -> bool:
    ph = _get("phone")
    if ph is None:
        raise RuntimeError("phone module unavailable")
    mac = asyncio.run(ph.find_iphone_mac())
    if not mac and ph.scan_info.get("ok") is not True:
        raise RuntimeError("paired-device scan failed")   # unknown, not "no phone": tick() keeps the gate closed
    return bool(mac)


async def _default_scan() -> list:
    from winrt.windows.devices.bluetooth import BluetoothDevice, BluetoothMajorClass
    from winrt.windows.devices.enumeration import DeviceInformation
    sel = BluetoothDevice.get_device_selector_from_pairing_state(False)
    find = getattr(DeviceInformation, "find_all_async_aqs_filter", None) or DeviceInformation.find_all_async
    out = []
    for di in await find(sel):
        is_phone = False
        try:
            d = await BluetoothDevice.from_id_async(di.id)
            if d is not None:
                is_phone = d.class_of_device.major_class == BluetoothMajorClass.PHONE
                d.close()
        except Exception:  # noqa: BLE001
            pass
        if pick_candidates([(di.name, is_phone)]):
            out.append(_Cand(di, di.name))
    return out


async def _default_pair(cand) -> dict:
    from winrt.windows.devices.enumeration import DevicePairingKinds
    kinds = DevicePairingKinds.CONFIRM_ONLY | DevicePairingKinds.DISPLAY_PIN | DevicePairingKinds.CONFIRM_PIN_MATCH
    return await pair_device(cand.di, PAIR_TIMEOUT_S, kinds)

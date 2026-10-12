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
import importlib
import json
import threading
import time

ADD_WINDOW_S = 120.0       # "add phone" keeps the gate open this long
PAIR_TIMEOUT_S = 60.0      # pair_async gives up after this
NO_PHONE_EVERY_S = 60.0    # gate (a): s between phone-paired checks / scans with nothing found
ADD_SCAN_EVERY_S = 5.0     # gate (b): s between scans while waiting for the phone to appear
AFTER_ATTEMPT_S = 120.0    # s of quiet after a pairing attempt that failed on the no-phone gate (b: ADD_SCAN_EVERY_S)
MSG_PAIR_AR = "اضغط Pair على الآيفون"
MSG_PAIR_EN = "Press Pair on the iPhone"
PAIRED_STATUSES = (0, 3)   # DevicePairingResultStatus.PAIRED, ALREADY_PAIRED


def _sibling(name):
    """Sibling module whether this file is used inside the `connector` package or flat; None when it cannot be imported."""
    try:
        return importlib.import_module("." + name, __package__) if __package__ else importlib.import_module(name)
    except ImportError:
        return None


heal = _sibling("heal")     # owner H1; None until it is merged (tests inject a stub)
phone = _sibling("phone")   # needs the Windows-only winrt packages


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
    pair(candidate) -> async {"ok", "status"}, in_call() -> bool, clock()."""

    def __init__(self, log=None, *, has_phone=None, scan=None, pair=None, in_call=None, clock=time.monotonic, on_paired=None):
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

    def status(self) -> dict:
        d = {"active": self._pairing, "add_phone": self.add_window_open() and not self._pairing,
             "last": self._last}
        if self._pairing:
            d["message_ar"], d["message_en"] = MSG_PAIR_AR, MSG_PAIR_EN
        return d

    # ---------------------------------------------------------------- one attempt
    async def attempt(self, why: str) -> str:
        """Scan unpaired devices and pair iff exactly one candidate. Returns the result tag (also logged)."""
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
            self._done(why, "paired", name, status=r.get("status"))
            try:
                self.on_paired()
            except Exception as e:  # noqa: BLE001
                self._log("pairing_report_failed", error=f"{type(e).__name__}: {e}"[:150])
            return "paired"
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
            gap = ADD_SCAN_EVERY_S if adding else (NO_PHONE_EVERY_S if res == "no_candidate" else AFTER_ATTEMPT_S)
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
    if heal is not None:
        heal.report("iphone_not_connected", source="pairing")


def _default_has_phone() -> bool:
    if phone is None:
        raise RuntimeError("phone module unavailable")
    return bool(asyncio.run(phone.find_iphone_mac()))


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
                is_phone = d.bluetooth_class_of_device.major_class == BluetoothMajorClass.PHONE
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

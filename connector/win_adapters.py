"""Windows adapters for the connector core: PhoneAdapter (Phone Link dial, Phone Service call state / hang-up, Hands-Free
link), AudioAdapter (iPhone Hands-Free endpoints "hf_direct", or the virtual cables "cable") and AnswerGate (Silero VAD).

Thin glue over the modules ported from the private dialer (phone, plcall.ps1, micguard, win_audio, vad, readiness); it follows
that dialer's native/calls.py _flow (wait for the Hands-Free link, move the call audio to the laptop, mute the room mic,
cable defaults), _bridge (source/player, hang-up and call-end detection) and watch. The functions copied verbatim from
calls.py sit in the block below. Importable on any platform (phone needs winrt, so `phone` is None off Windows and
WinPhone() then refuses to start); tests may assign a stub to `win_adapters.phone`.

make_adapters(route, mac, gain, loudness, log) -> (WinPhone, WinAudio, SileroGate) is the entry point for main.py."""
import asyncio
import importlib
import json
import re
import threading
import time

import numpy as np

try:
    from .core import AnswerGate, AudioAdapter, PhoneAdapter
except ImportError:   # imported flat (connector/ on sys.path)
    from core import AnswerGate, AudioAdapter, PhoneAdapter


def _sibling(name):
    """Sibling module (package or flat import); None when it cannot be imported."""
    try:
        return importlib.import_module("." + name, __package__) if __package__ else importlib.import_module(name)
    except ImportError:
        return None


phone = _sibling("phone")           # needs the Windows-only winrt packages
micguard = _sibling("micguard")
win_audio = _sibling("win_audio")
vad = _sibling("vad")
readiness = _sibling("readiness")

HOME = micguard.HOME                # %LOCALAPPDATA%\YoussefConnector (YOUSSEF_HOME overrides)
DEFAULTS_BACKUP = HOME / "default-devices.json"   # normal default devices while the cable route has them switched
GONE_S = 1.2        # s without any live call (before the call audio is on the laptop) that mean the call is over (3 x 0.4 s)
OFF_S = 1.5         # s of "call ended" / "Hands-Free link dropped" that mean the call is over (3 x 0.5 s)
NO_AUDIO_S = 30.0   # s the Phone Service says "talking" without the Hands-Free link coming up: give up (audio_error)

NUM_RE = re.compile(r"^\+?[0-9]{3,15}$")


def _not_room_mics() -> list:
    """Capture endpoints that carry the call itself (iPhone Hands-Free, virtual cables): never muted by micguard."""
    try:
        return [e["id"] for e in phone.endpoints() if e["flow"] == "capture" and
                ("hands-free" in e["device"].lower() or "cable" in (e["name"] + " " + e["device"]).lower())]
    except Exception:  # noqa: BLE001
        return []


def _switch_to_cables(cab: dict):
    """Cable route: save the normal default devices (once; survives a crash), then point console+multimedia defaults
    (communications already points there) at the cables so Phone Link's call audio goes through them."""
    if not DEFAULTS_BACKUP.exists():
        DEFAULTS_BACKUP.write_text(json.dumps(phone.get_default_endpoints()), encoding="utf-8")
    phone.set_default_endpoint(cab["render"], (0, 1, 2))
    phone.set_default_endpoint(cab["capture"], (0, 1, 2))


def restore_defaults() -> bool:
    """Put back the normal console+multimedia default devices saved by _switch_to_cables (also run at app start)."""
    try:
        saved = json.loads(DEFAULTS_BACKUP.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    try:
        for flow in ("render", "capture"):
            ids = saved.get(flow) or []
            for role in (0, 1):
                if role < len(ids) and ids[role]:
                    phone.set_default_endpoint(ids[role], (role,))
        DEFAULTS_BACKUP.unlink()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"restore_defaults: {type(e).__name__}: {e}", flush=True)
        return False


# ---------------------------------------------------------------- phone
class WinPhone(PhoneAdapter):
    """call_state() is polled every 0.25 s by the core, in a worker thread. It returns:
      idle     no call dialed / call not seen by the Phone Service yet
      ringing  the Phone Service lists a live call but the Hands-Free link (the call audio on the laptop) is not up yet;
               during it the call audio is moved to the laptop once the call is "talking" (calls._flow)
      active   both iPhone Hands-Free endpoints (or, route cable, the Hands-Free link plus the virtual cables) are up:
               devices chosen, the room microphone muted, cable defaults switched
      ended    the call is over; end_hint() tells why once the callee has answered (SileroGate calls mark_answered)"""

    def __init__(self, route: str = "hf_direct", mac: str = "", log=None):
        if phone is None:
            raise RuntimeError("the Windows phone modules are unavailable (winrt not installed / not Windows)")
        self.route = "cable" if route == "cable" else "hf_direct"
        self.mac = mac
        self.log = log or (lambda *a: None)
        self.lock = threading.RLock()
        self.last_error = ""
        HOME.mkdir(parents=True, exist_ok=True)
        self._reset()

    def _log(self, ev, **kw):
        try:
            self.log(ev, json.dumps(kw, ensure_ascii=False, default=str) if kw else "")
        except Exception:  # noqa: BLE001
            pass

    def _reset(self):
        self.t_dial = None      # monotonic time the dial succeeded; None = no call
        self.seen = False       # the Phone Service has listed a live call
        self.gone_since = None  # since when no live call is listed
        self.gap_logged = False
        self.talking_since = None
        self.transfers = self.ok_transfers = 0
        self.last_transfer = 0.0
        self.stuck_logged = False
        self.active = False     # call audio is on the laptop and the devices are chosen
        self.was_active = False
        self.off_since = None   # since when Windows says the call is not active
        self.hf_gone_since = None
        self.answered = False
        self.hint = None
        self.ended = False
        self.devs = None        # (capture, render): MMDevice IDs (hf_direct) or PortAudio indices (cable)
        self.switched = False   # cable route: default devices were switched

    # ---- PhoneAdapter
    def ready(self) -> dict:
        if self.t_dial is not None and not self.ended:
            return {"ok": False, "reason": "call_active", "call_active": True}
        return readiness.check(self.mac, self._log)

    def dial(self, number: str) -> bool:
        num = re.sub(r"[\s\-().]", "", str(number))
        if not NUM_RE.match(num):
            self.last_error = "bad number"
            return False
        with self.lock:
            self._reset()
        if phone.hf_link() == "active" or phone.call_flags().get("call_active"):
            self.last_error = "a call is already up on the laptop: end it first"
            self._log("dial_refused", why=self.last_error)
            return False
        r = asyncio.run(phone.phone_link("dial", num))   # plcall.ps1 returns once Windows sees the call
        self._log("dial", number_tail=num[-4:], ok=r.get("ok"), error=r.get("error"), method=r.get("method"))
        if not r.get("ok"):
            self.last_error = str(r.get("error") or "Phone Link dial failed")
            return False
        with self.lock:
            self._reset()
            self.t_dial = time.monotonic()
        self.last_error = ""
        return True

    def call_state(self) -> str:
        with self.lock:
            if self.t_dial is None:
                return "idle"
            if self.ended:
                return "ended"
            now = time.monotonic()
            return self._in_call(now) if self.active else self._before_audio(now)

    def hangup(self) -> None:
        try:
            r = phone.end_calls()
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
        self._log("phone_hangup", **r)
        with self.lock:
            self.ended = self.ended or self.t_dial is not None
            self._restore()

    def end_hint(self):
        if self.hint == "audio_error":
            return "audio_error"
        return self.hint if self.answered else None   # before the answer the core tells busy / no_answer by the ring time

    # ---- helpers for the audio adapters
    def mark_answered(self):
        self.answered = True

    def cleanup(self):
        """Idempotent end-of-call cleanup (room microphone unmuted, cable defaults restored); called at every call end."""
        with self.lock:
            self._restore()

    def startup_cleanup(self):
        """At app start: unmute a microphone a crashed call left muted and restore switched default devices."""
        r = micguard.restore(self._log, None, None, "app_start")
        if r.get("restored") or r.get("failed"):
            self._log("leftover_mic_mute_restored", **r)
        if restore_defaults():
            self._log("leftover_defaults_restored")

    # ---- call flow (calls._flow / watch)
    def _end(self, hint):
        self.ended, self.hint = True, hint
        self._restore()
        return "ended"

    def _restore(self):
        try:
            micguard.restore(self._log, None, None, "call_end")
        except Exception as e:  # noqa: BLE001
            self._log("mic_restore_failed", error=f"{type(e).__name__}: {e}"[:150])
        if self.switched:
            self.switched = False
            self._log("defaults_restored", ok=restore_defaults())

    def _before_audio(self, now: float) -> str:
        hf = phone.hf_endpoints()
        if all(hf.get(k, {}).get("state") == 1 for k in ("capture", "render")):
            return self._activate(hf, now)
        try:
            states = [c["state"] for c in phone.phone_calls()]
        except Exception:  # noqa: BLE001
            states = []
        live = [x for x in states if x not in ("idle", "ambiguous")]
        self.seen = self.seen or bool(live)
        if (self.seen or now - self.t_dial > 5) and not live:
            self.gone_since = self.gone_since if self.gone_since is not None else now
            if now - self.gone_since >= GONE_S:
                # the Phone Service briefly lists no live call while a ringing call is answered (calls.py, 2026-10-10):
                # it only ended if Windows says so too (call_active None = unknown: keep waiting)
                flags = phone.call_flags()
                if flags.get("call_active") is not True:
                    return self._end(None)   # ended (or never came up) before its audio reached the laptop
                if not self.gap_logged:
                    self.gap_logged = True
                    self._log("phone_state_gap", states=states, **flags)
        else:
            self.gone_since = None
        if self.talking_since is None and ("talking" in live or "hold" in live):
            self.talking_since = now
        # answered but its audio stayed on the iPhone: ask the Phone Service to move it to the laptop (~2 s).
        # Only once the call is talking: a transfer while it is still dialing went wrong in a live test.
        if self.talking_since and self.transfers < 3 and now - self.talking_since > 2.5 + 5 * self.transfers:
            try:
                r = phone.audio_to_laptop()
            except Exception as e:  # noqa: BLE001
                r = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
            self.transfers += 1
            self.ok_transfers += bool(r.get("ok"))
            self.last_transfer = now
            self._log("audio_to_laptop", attempt=self.transfers, **r)
        if self.ok_transfers >= 2 and not self.stuck_logged and now - self.last_transfer > 4:
            self.stuck_logged = True   # the Phone Service accepted the transfers but the Hands-Free link never opened
            self._log("hf_link_stuck", ok_transfers=self.ok_transfers, live=live,
                      hf={k: e.get("state") for k, e in hf.items()}, **phone.call_flags())
        if self.talking_since and now - self.talking_since > NO_AUDIO_S:
            self.last_error = "no call audio on the laptop (Hands-Free link not active)"
            self._log("no_call_audio", transfers=self.transfers)
            return self._end("audio_error")
        return "ringing" if live else "idle"

    def _activate(self, hf: dict, now: float) -> str:
        try:
            if self.route == "cable":
                ii, oi, info = win_audio.find_cable_devices()
                cab = phone.cable_endpoints()
                if ii is None or oi is None or len(cab) < 2:
                    raise RuntimeError("virtual cables missing (VB-CABLE / Hi-Fi Cable): run the setup again")
                self.devs = (ii, oi)
            else:
                info = {"in": f"id {hf['capture']['id']} {hf['capture'].get('format')}",
                        "out": f"id {hf['render']['id']} {hf['render'].get('format')}"}
                self.devs = (hf["capture"]["id"], hf["render"]["id"])
            self._log("audio_devices", route=self.route, **info)
            micguard.mute(_not_room_mics(), self._log)   # the callee hears only Youssef, never the room
            if self.route == "cable":
                _switch_to_cables(cab)
                self.switched = True
                self._log("defaults_switched", to=cab)
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"[:300]
            self._log("audio_setup_failed", error=self.last_error)
            return self._end("audio_error")
        self.active, self.was_active = True, False
        return "active"

    def _in_call(self, now: float) -> str:
        active = phone.call_flags().get("call_active")
        self.was_active = self.was_active or bool(active)
        if self.was_active and active is False:
            self.off_since = self.off_since if self.off_since is not None else now
            if now - self.off_since >= OFF_S:
                return self._end("callee_hangup")   # Windows saw the call end
        else:
            self.off_since = None
        if phone.hf_link() != "active":
            self.hf_gone_since = self.hf_gone_since if self.hf_gone_since is not None else now
            if now - self.hf_gone_since >= OFF_S:
                return self._end("call_ended")      # Hands-Free link dropped: the call is over
        else:
            self.hf_gone_since = None
        return "active"


# ---------------------------------------------------------------- audio
class _Source:
    """Core source interface over IdSource / SdSource. alive() is also False when the player's stream died."""

    def __init__(self, inner, owner):
        self.inner, self.owner, self.closed = inner, owner, False

    def start(self, cb):
        self.inner.start(cb)

    def alive(self) -> bool:
        pl = self.owner.player_obj
        return bool(self.inner.alive()) and (pl is None or bool(pl.alive()))

    def stop(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.inner.stop()
        finally:
            self.owner._closed()


class _Player:
    """Core player interface over IdPlayer / SdPlayer; applies the dialer's Loudness chain (the former eleven.py behaviour:
    process() per chunk, clear() on flush) because the hub sends the agent's audio unprocessed."""

    def __init__(self, inner, loud, owner):
        self.inner, self.loud, self.owner, self.closed = inner, loud, owner, False

    def start(self):
        self.inner.start()

    def feed(self, pcm: bytes):
        pcm = pcm[:len(pcm) // 2 * 2]
        if self.loud is not None and pcm:
            pcm = self.loud.process(np.frombuffer(pcm, dtype=np.int16)).tobytes()
        self.inner.feed(pcm)

    def flush(self) -> int:
        dropped = self.inner.flush()
        if self.loud is not None:
            self.loud.clear()   # the audio still held in the loudness delay belongs to the interrupted response
        return dropped

    def pending(self) -> float:
        return self.inner.pending()

    def alive(self) -> bool:
        return bool(self.inner.alive())

    def stop(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.inner.stop()
        finally:
            self.owner.player_obj = None
            self.owner._closed()


class WinAudio(AudioAdapter):
    """source()/player() are called by the core once the call is active, so the devices WinPhone chose are known."""

    def __init__(self, phone_adapter: WinPhone, gain: float = 1.0, loudness: bool = True, log=None):
        self.phone, self.gain, self.loudness = phone_adapter, float(gain), loudness
        self.log = log or phone_adapter._log
        self.player_obj = None
        self._live = 0

    def _devs(self):
        if not self.phone.devs:
            raise RuntimeError("call audio devices are not chosen yet")
        return self.phone.devs

    def _opened(self):
        self._live += 1
        win_audio.AUDIO_BUSY.set()   # PortAudio must not be re-initialised while a call's streams are open

    def _closed(self):
        self._live = max(0, self._live - 1)
        if self._live == 0:
            win_audio.AUDIO_BUSY.clear()
            self.phone.cleanup()

    def source(self):
        cap = self._devs()[0]
        inner = (win_audio.SdSource(cap, gain=self.gain) if self.phone.route == "cable"
                 else win_audio.IdSource(cap, gain=self.gain))
        self._opened()
        return _Source(inner, self)

    def player(self):
        ren = self._devs()[1]
        inner = win_audio.SdPlayer(ren) if self.phone.route == "cable" else win_audio.IdPlayer(ren)
        if hasattr(inner, "endpoint_id"):
            inner.on_event = self.log   # IdPlayer logs the endpoint volume it sets (endpoint_volume event)
        self._opened()
        self.player_obj = _Player(inner, win_audio.Loudness() if self.loudness else None, self)
        return self.player_obj


# ---------------------------------------------------------------- answer gate
class SileroGate(AnswerGate):
    """The callee has answered when Silero hears speech (confidence >= 0.7 for 0.3 s): ringback is tonal, not speech."""

    def __init__(self, phone_adapter: WinPhone = None):
        self.phone = phone_adapter

    async def wait_answer(self, frames_iter, timeout_s: float) -> bool:
        ok = await vad.wait_for_speech(frames_iter, timeout_s)
        if ok and self.phone is not None:
            self.phone.mark_answered()
        return ok


def make_adapters(route: str = "hf_direct", mac: str = "", gain: float = 1.0, loudness: bool = True, log=None):
    """(WinPhone, WinAudio, SileroGate) for core.Connector(base_url, token, phone, audio, gate, audio_route=route).
    `log(*args)` is the core's log callable. Call phone.startup_cleanup() once at app start."""
    ph = WinPhone(route, mac, log)
    return ph, WinAudio(ph, gain, loudness), SileroGate(ph)


def build(cfg: dict = None, log=None):
    """-> (phone, audio, gate, audio_route), what main.build_adapters expects. Optional keys of `cfg` (default: HOME/config.json):
    audio_route "hf_direct"|"cable", phone_mac, in_gain (callee microphone gain), loudness (False = no output loudness chain).
    Restores a microphone mute / cable defaults a crashed call left behind. `log(*args)` defaults to a flushed print."""
    if log is None:
        def log(*a):
            print(*a, flush=True)
    if cfg is None:
        try:
            cfg = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cfg = {}
    ph, au, gate = make_adapters(cfg.get("audio_route") or "hf_direct", cfg.get("phone_mac") or "",
                                 cfg.get("in_gain", 1.0), cfg.get("loudness", True) is not False, log)
    ph.startup_cleanup()
    return ph, au, gate, ph.route

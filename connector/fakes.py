"""Fakes for the connector core (tests, and the public repo's CI): FakePhone, FakeAudio, FakeGate, FakeRadio, FakeEventLog,
FakeSchtasks. Stdlib + numpy only."""
import threading
from typing import Optional

import numpy as np

try:
    from .core import AnswerGate, AudioAdapter, Clock, PhoneAdapter
except ImportError:   # imported flat (dialer/ on sys.path)
    from core import AnswerGate, AudioAdapter, Clock, PhoneAdapter

SCENARIOS = ("answer", "no_answer", "busy", "hangup_mid", "drop", "end_call")

# Failure classes (heal contract) -> the ready() keys that go False when that failure is active.
FAILURE_READY = {
    "bluetooth_off": {"bt_radio": False, "iphone_connected": False, "hfp_audio_device": False},
    "iphone_not_connected": {"iphone_connected": False, "hfp_audio_device": False},
    "no_phone_paired": {"iphone_connected": False, "hfp_audio_device": False, "paired": False},
    "hf_endpoints": {"hfp_audio_device": False},
    "phone_link_not_running": {"phone_link_running": False},
    "phone_link_not_linked": {"phone_link_linked": False},
    "bt_unhealthy": {"hf_link": False},
}
FAILURE_CLASSES = ("bluetooth_off", "iphone_not_connected", "no_phone_paired", "hf_endpoints", "phone_link_not_running",
                   "phone_link_not_linked", "bt_unhealthy", "bthusb_error", "hangup_failed", "net_dns")


class FakePhone(PhoneAdapter):
    """Scripted phone. Times are on `clock`.
    answer/end_call: ring `ring_s`, then active until hangup(). no_answer: rings forever. busy: rings 1 s then ended.
    hangup_mid: active, callee hangs up `after_s` after going active. drop: active, then the line drops (hint call_ended)."""
    def __init__(self, scenario: str = "answer", clock: Optional[Clock] = None, ring_s: float = 1.0, after_s: float = 3.0,
                 dial_ok: bool = True):
        assert scenario in SCENARIOS, scenario
        self.scenario, self.clock = scenario, clock or Clock()
        self.ring_s, self.after_s, self.dial_ok = ring_s, after_s, dial_ok
        self.dialed = []
        self.hangups = 0
        self.hangup_fails = False
        self._t_dial = None
        self._hung = False
        self.ready_dict = {"ok": True, "reason": "", "bt_radio": True, "iphone_connected": True, "hfp_audio_device": True,
                           "phone_link_running": True, "session_locked": False, "call_active": False, "hf_link": True}

    def ready(self) -> dict:
        return dict(self.ready_dict)

    def set_failure(self, name: str) -> None:
        """Put the fake into the state a heal failure class describes (ready() reflects it). 'hangup_failed' makes hangup()
        not end the call; classes with no ready() signal (bthusb_error, net_dns) leave ready() untouched."""
        assert name in FAILURE_CLASSES, name
        self.ready_dict.update(FAILURE_READY.get(name, {}))
        self.ready_dict["ok"] = not FAILURE_READY.get(name)
        if name == "hangup_failed":
            self.hangup_fails = True

    def clear_failure(self) -> None:
        self.ready_dict.update({"ok": True, "reason": "", "bt_radio": True, "iphone_connected": True, "hfp_audio_device": True,
                                "phone_link_running": True, "hf_link": True})
        self.ready_dict.pop("paired", None)
        self.ready_dict.pop("phone_link_linked", None)
        self.hangup_fails = False

    def dial(self, phone: str) -> bool:
        self.dialed.append(phone)
        if not self.dial_ok:
            return False
        self._t_dial, self._hung = self.clock.now(), False
        return True

    def hangup(self) -> None:
        self.hangups += 1
        if not self.hangup_fails:
            self._hung = True

    def call_state(self) -> str:
        if self._t_dial is None:
            return "idle"
        if self._hung:
            return "ended"
        dt = self.clock.now() - self._t_dial
        s = self.scenario
        if s == "no_answer":
            return "ringing"
        if s == "busy":
            return "ringing" if dt < self.ring_s else "ended"
        if dt < self.ring_s:
            return "ringing"
        if s == "hangup_mid" and dt >= self.ring_s + self.after_s:
            return "ended"
        if s == "drop" and dt >= self.ring_s + self.after_s:
            return "ended"
        return "active"

    def end_hint(self) -> Optional[str]:
        return "call_ended" if self.scenario == "drop" and not self._hung else None


class FakeSource:
    """Scripted callee: silence for `silence_s`, then a 220 Hz tone; 20 ms frames paced on the clock from a thread."""
    def __init__(self, clock: Clock, silence_s: float = 0.3, amp: int = 6000):
        self.clock, self.silence_s, self.amp = clock, silence_s, amp
        self.frames_sent = 0
        self._stop = threading.Event()
        self._thread = None
        self._alive = True

    def start(self, cb):
        self.started_at = self.clock.now()
        self._thread = threading.Thread(target=self._run, args=(cb,), daemon=True)
        self._thread.start()

    def _run(self, cb):
        n = 0
        while not self._stop.is_set() and self._alive:
            t = n * 0.02
            if t < self.silence_s:
                frame = np.zeros(320, dtype=np.int16)
            else:
                k = np.arange(320) + n * 320
                frame = (self.amp * np.sin(2 * np.pi * 220 * k / 16000)).astype(np.int16)
            cb(frame.tobytes())
            self.frames_sent += 1
            n += 1
            self.clock.sleep_sync(0.02)

    def stop(self):
        self._stop.set()

    def alive(self) -> bool:
        return self._alive

    def kill(self):
        self._alive = False


class FakePlayer:
    """Records fed PCM; pending() simulates real-time playback on the clock (stuck=True never drains)."""
    def __init__(self, clock: Clock, stuck: bool = False, preload_s: float = 0.0):
        self.clock, self.stuck, self.preload_s = clock, stuck, preload_s
        self.fed = bytearray()
        self.flushes = 0
        self.started = self.stopped = False
        self._until = 0.0

    def start(self):
        self.started = True
        self._until = self.clock.now() + self.preload_s

    def feed(self, pcm: bytes):
        self.fed += pcm
        self._until = max(self._until, self.clock.now()) + len(pcm) / 32000.0

    def flush(self):
        self.flushes += 1
        self._until = self.clock.now()

    def pending(self) -> float:
        if self.stuck:
            return 99.0
        return max(0.0, self._until - self.clock.now())

    def stop(self):
        self.stopped = True


class FakeAudio(AudioAdapter):
    def __init__(self, clock: Optional[Clock] = None, silence_s: float = 0.3, stuck_player: bool = False,
                 player_preload_s: float = 0.0):
        self.clock = clock or Clock()
        self.silence_s, self.stuck_player, self.player_preload_s = silence_s, stuck_player, player_preload_s
        self.sources, self.players = [], []

    def source(self):
        s = FakeSource(self.clock, self.silence_s)
        self.sources.append(s)
        return s

    def player(self):
        p = FakePlayer(self.clock, self.stuck_player, self.player_preload_s)
        self.players.append(p)
        return p


class FakeGate(AnswerGate):
    """Consumes `consume` frames (default 25 = 0.5 s) then returns `result`."""
    def __init__(self, result: bool = True, consume: int = 25):
        self.result, self.consume = result, consume
        self.frames_seen = 0

    async def wait_answer(self, frames_iter, timeout_s: float) -> bool:
        async for _ in frames_iter:
            self.frames_seen += 1
            if self.frames_seen >= self.consume:
                break
        return self.result


class FakeRadio:
    """Bluetooth radio. set(on) records toggles; `fail_on` makes the next set(True) report failure (stuck radio)."""
    def __init__(self, on: bool = True, fail_on: bool = False):
        self.on, self.fail_on = on, fail_on
        self.toggles = []

    def set(self, on: bool) -> bool:
        self.toggles.append(bool(on))
        if on and self.fail_on:
            return False
        self.on = bool(on)
        return True


class FakeEventLog:
    """Windows event log. add() queues entries; read(source=None) returns them (newest last)."""
    def __init__(self):
        self.entries = []

    def add(self, source: str, event_id: int, message: str = "", level: str = "error") -> None:
        self.entries.append({"source": source, "id": event_id, "message": message, "level": level})

    def read(self, source: Optional[str] = None) -> list:
        return [dict(e) for e in self.entries if source is None or e["source"] == source]

    def clear(self) -> None:
        self.entries.clear()


class FakeSchtasks:
    """schtasks.exe stand-in: create/delete/run/query by task name; `fail` makes every call return rc 1."""
    def __init__(self, fail: bool = False):
        self.tasks, self.runs, self.calls, self.fail = {}, [], [], fail

    def create(self, name: str, cmd: str, **kw) -> int:
        self.calls.append(("create", name))
        if self.fail:
            return 1
        self.tasks[name] = {"cmd": cmd, **kw}
        return 0

    def delete(self, name: str) -> int:
        self.calls.append(("delete", name))
        if self.fail or name not in self.tasks:
            return 1
        del self.tasks[name]
        return 0

    def run(self, name: str) -> int:
        self.calls.append(("run", name))
        if self.fail or name not in self.tasks:
            return 1
        self.runs.append(name)
        return 0

    def query(self, name: str) -> bool:
        self.calls.append(("query", name))
        return (not self.fail) and name in self.tasks

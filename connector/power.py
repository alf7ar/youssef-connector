"""Keep Windows awake while the connector is connected and ready, so sleep does not drop the Bluetooth / Phone Link line.

SetThreadExecutionState is per thread: ES_CONTINUOUS holds until that same thread clears it or exits, so one dedicated
daemon thread asserts and clears it. ES_DISPLAY_REQUIRED is NOT used (the screen may turn off). On battery below
LOW_BATTERY_PCT the request is dropped so a laptop is not drained flat. Non-Windows: no-op.
"""
import threading
from typing import Callable, Optional

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
LOW_BATTERY_PCT = 20
POLL_S = 10.0


def _win_set_state(flags: int) -> int:
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.SetThreadExecutionState.restype = ctypes.c_uint
    k32.SetThreadExecutionState.argtypes = [ctypes.c_uint]
    return int(k32.SetThreadExecutionState(flags))


def battery_status():
    """(on_battery, percent) from GetSystemPowerStatus; (None, None) when unknown or not Windows."""
    try:
        import ctypes

        class PowerStatus(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte), ("BatteryLifePercent", ctypes.c_ubyte),
                        ("SystemStatusFlag", ctypes.c_ubyte), ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
        st = PowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return None, None
        on_battery = {0: True, 1: False}.get(st.ACLineStatus)
        pct = st.BatteryLifePercent if st.BatteryLifePercent <= 100 else None
        return on_battery, pct
    except Exception:  # noqa: BLE001
        return None, None


def want_awake(active: bool, battery=battery_status) -> bool:
    """active (connected and ready) and not (on battery below LOW_BATTERY_PCT). Unknown battery data keeps it awake."""
    if not active:
        return False
    on_battery, pct = battery()
    return not (on_battery is True and pct is not None and pct < LOW_BATTERY_PCT)


class KeepAwake:
    def __init__(self, is_active: Callable[[], bool], log=None, set_state: Optional[Callable[[int], int]] = None,
                 battery: Callable = battery_status, poll: float = POLL_S):
        self.is_active, self.battery, self.poll = is_active, battery, poll
        self.log = log or (lambda *a: None)
        self._set = set_state or _win_set_state
        self.held = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _apply(self, want: bool):
        """Runs on the keep-awake thread only."""
        if want == self.held:
            return
        try:
            self._set(ES_CONTINUOUS | ES_SYSTEM_REQUIRED if want else ES_CONTINUOUS)
            self.held = want
            self.log("keep-awake", "on" if want else "off")
        except Exception as e:  # noqa: BLE001
            self.log("keep-awake failed:", repr(e))

    def _loop(self):
        while not self._stop.is_set():
            try:
                want = want_awake(bool(self.is_active()), self.battery)
            except Exception:  # noqa: BLE001
                want = False
            self._apply(want)
            self._stop.wait(self.poll)
        self._apply(False)          # cleared on the thread that set it

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="keep-awake", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

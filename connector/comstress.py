"""COM/GC stress (Windows only): reproduces the 0.1.1 crash pattern. comtypes wrappers created on short-lived MTA threads were
dropped into reference cycles, so the garbage collector Released them later on another thread, after their apartment was gone
(access violation in _ctypes). micguard._com_thread + _keep_mta_alive must survive it.

  python -m connector.comstress [--no-keeper] [--n 500]     JSON result on the last line; exit 0 = survived

--no-keeper patches micguard._keep_mta_alive to a no-op (the old pattern). It is not deterministic whether the old pattern
crashes; the CI test only records what happened. Also used by `--selftest` (small N, keeper on)."""
import gc
import json
import sys
import threading
import time

from . import micguard

SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"   # always registered, needs no audio device


class Holder:
    """Owns a COM pointer inside a reference cycle: only the cyclic GC frees it, on whichever thread runs gc.collect()."""
    finalised_on = []

    def __init__(self, ptr):
        self.ptr, self.me = ptr, self

    def __del__(self):
        Holder.finalised_on.append(threading.current_thread().name)


def _create(comtypes):
    """A COM object that exists on every Windows runner: the endpoint enumerator, else a shell link."""
    try:
        from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
        from pycaw.constants import CLSID_MMDeviceEnumerator
        return comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
    except Exception:  # noqa: BLE001
        from comtypes import GUID, IUnknown
        return comtypes.CoCreateInstance(GUID(SHELL_LINK), IUnknown, comtypes.CLSCTX_INPROC_SERVER)


def _collect_elsewhere(held):
    """Drop the references and run the GC on a plain thread that never initialised COM."""
    held.clear()
    t = threading.Thread(target=gc.collect, name="gc-thread")
    t.start()
    t.join(30)
    return not t.is_alive()


def run(n=500, keeper=True, batch=50) -> dict:
    if not keeper:
        micguard._keep_mta_alive = lambda: None
    held, created = [], 0
    t0 = time.time()
    for i in range(n):
        held.append(micguard._com_thread(lambda c: Holder(_create(c))))
        created += 1
        if len(held) >= batch:
            assert _collect_elsewhere(held), "gc thread hung"
    assert _collect_elsewhere(held), "gc thread hung"
    gc.collect()
    elsewhere = sum(1 for name in Holder.finalised_on if name == "gc-thread")
    return {"keeper": keeper, "n": n, "created": created, "finalised": len(Holder.finalised_on), "finalised_on_gc_thread": elsewhere,
            "mta_keeper_alive": bool(micguard._mta_keeper and micguard._mta_keeper[0].is_alive()), "secs": round(time.time() - t0, 2)}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    n = int(argv[argv.index("--n") + 1]) if "--n" in argv else 500
    try:
        import faulthandler
        faulthandler.enable(all_threads=True)
    except Exception:  # noqa: BLE001
        pass
    print(json.dumps(run(n, keeper="--no-keeper" not in argv)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Network state of the laptop for the call path: captive portal, system proxy, reachability of the hub.

Probes run in a background thread every NET_EVERY seconds, only while no call is up (busy() is False) and only when a hub URL is
configured. Each probe has a short timeout; a probe that cannot tell returns None (unknown), never a guess. The heartbeat reads the
cached state through snapshot() (never blocks). The readiness warnings captive_portal / proxy_blocking come from warning_codes().

- captive portal: GET http://www.msftconnecttest.com/connecttest.txt must answer "Microsoft Connect Test"; any other body (a login
  page after a redirect) means a captive portal. No answer at all = unknown (no internet is not a captive portal).
- proxy: WinHTTP proxy (`netsh winhttp show proxy`) or a proxy from the environment / WinINet registry (urllib getproxies()).
- server: HTTPS HEAD to the configured hub base URL; any HTTP answer counts as reachable.
proxy_blocking = a proxy is set AND the hub is not reachable through it.
"""
import subprocess
import threading
import time
import urllib.error
import urllib.request

CAPTIVE_URL = "http://www.msftconnecttest.com/connecttest.txt"
CAPTIVE_BODY = "Microsoft Connect Test"
NET_EVERY = 60.0      # s between two probe rounds (idle only)
FIRST_DELAY = 5.0     # s after start before the first round
PROBE_TIMEOUT = 5.0   # s per HTTP probe

_lock = threading.Lock()
_state = {"captive_portal": None, "proxy": None, "server": None, "checked_at": None}
_started = [False]


def _http_get(url: str, timeout: float):
    """(status, body text) of a GET; raises OSError (URLError/timeout included) when there is no answer."""
    req = urllib.request.Request(url, headers={"User-Agent": "youssef-connector-netcheck"})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - fixed http URL above
        return r.status, r.read(4096).decode("utf-8", errors="replace")


def _http_head(url: str, timeout: float) -> bool:
    """True when the server answers at all (any status); False when there is no answer."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "youssef-connector-netcheck"})
    try:
        with urllib.request.urlopen(req, timeout=timeout):   # noqa: S310 - hub URL from the paired connect code
            return True
    except urllib.error.HTTPError:
        return True
    except (OSError, ValueError):
        return False


def _run(cmd: list, timeout: float = 10.0):
    """stdout of a command, None when it cannot run. Tests replace this."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace",
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:  # noqa: BLE001
        return None


def probe_captive(get=_http_get):
    """True = captive portal, False = open internet, None = no answer."""
    try:
        _status, body = get(CAPTIVE_URL, PROBE_TIMEOUT)
    except Exception:  # noqa: BLE001
        return None
    return CAPTIVE_BODY not in (body or "")


def parse_winhttp(out):
    """True when `netsh winhttp show proxy` names a proxy server, False for direct access, None when not readable."""
    if out is None:
        return None
    if "Direct access" in out:
        return False
    if "Proxy Server" in out:
        return True
    return None


def probe_proxy(run=_run, env_proxies=urllib.request.getproxies):
    """True when a proxy is set (WinHTTP, environment or WinINet), False when none is, None when unknown."""
    wh = parse_winhttp(run(["netsh", "winhttp", "show", "proxy"]))
    try:
        env = any(k != "no" for k in env_proxies())
    except Exception:  # noqa: BLE001
        env = False
    if wh or env:
        return True
    return False if wh is False else None


def probe_server(base: str, head=_http_head):
    """True when the hub answers, False when not. base is the http(s) base URL (empty = nothing to probe, None)."""
    if not base:
        return None
    return bool(head(base, PROBE_TIMEOUT))


def run_round(base: str, busy=lambda: False, captive=probe_captive, proxy=probe_proxy, server=probe_server) -> dict:
    """One probe round: updates and returns the state. A round stops early (keeps the old values) when a call starts."""
    res = {}
    for key, fn in (("captive_portal", captive), ("proxy", proxy), ("server", lambda: server(base))):
        if busy():
            return snapshot()
        try:
            res[key] = fn()
        except Exception:  # noqa: BLE001
            res[key] = None
    res["checked_at"] = time.time()
    with _lock:
        _state.update(res)
    return snapshot()


def snapshot() -> dict:
    """The cached state: {captive_portal, proxy, server} (bool or None) and checked_at (wall-clock seconds or None)."""
    with _lock:
        return dict(_state)


def warning_codes(st: dict = None) -> list:
    """Readiness warning codes that apply to the cached state: captive_portal, proxy_blocking."""
    st = snapshot() if st is None else st
    out = []
    if st.get("captive_portal") is True:
        out.append("captive_portal")
    if st.get("proxy") is True and st.get("server") is False:
        out.append("proxy_blocking")
    return out


def start(get_base, busy=lambda: False, every: float = NET_EVERY, first: float = FIRST_DELAY):
    """Start the background probe thread once. get_base() -> http base URL ("" when not configured yet)."""
    if _started[0]:
        return None
    _started[0] = True
    wait = threading.Event().wait

    def loop():
        wait(first)
        while True:
            if not busy():
                base = get_base() or ""
                if base:
                    try:
                        run_round(base, busy=busy)
                    except Exception:  # noqa: BLE001
                        pass
            wait(every)

    t = threading.Thread(target=loop, daemon=True, name="netcheck")
    t.start()
    return t


def reset_for_tests():
    with _lock:
        _state.update({"captive_portal": None, "proxy": None, "server": None, "checked_at": None})
    _started[0] = False

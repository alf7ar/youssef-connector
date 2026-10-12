"""Windows event-log watcher: spots Bluetooth driver trouble and crashes of the connector / Phone Link while the app runs.

Every POLL_S seconds (daemon thread, one short `wevtutil` process per log, no admin needed, no console window):
  * System log, records newer than the last seen record id: BTHUSB events 3/8/17/18/22, any warning or error from BTHPORT,
    BthEnum and Microsoft-Windows-Bluetooth-*. Each new one is logged and reported to the self-healing module as
    heal.report("bthusb_error", id=..., provider=...) (at most one report per provider/event id per poll).
  * Application log: Application Error / Windows Error Reporting events whose faulting app is YoussefConnector.exe or
    PhoneExperienceHost.exe. Logged and kept in status()["events"] (the heartbeat shows them under ready["heal"]["events"]).
The first poll only records the newest System record id (old history is not replayed); the Application log is looked at
EXTRA_LOOKBACK_S back once, because a crash usually comes just before the app was started again.
Events carry only provider, ids, level, time and the faulting app name: no message text, paths or user names."""
import collections
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET

POLL_S = 20.0
MAX_EVENTS = 20          # kept for status()
QUERY_COUNT = 50         # newest records fetched per query
STARTUP_LOOKBACK_S = 600
BTHUSB_IDS = frozenset({3, 8, 17, 18, 22})
BT_PROVIDERS = frozenset({"bthport", "bthenum"})
BT_PREFIX = "microsoft-windows-bluetooth-"
APP_PROVIDERS = frozenset({"application error", "windows error reporting"})
WATCHED_APPS = ("youssefconnector.exe", "phoneexperiencehost.exe")
_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"


def _xml_events(text):
    """wevtutil /f:xml prints bare <Event> elements one after the other; wrap them in a root. Returns the Event elements."""
    text = re.sub(r"<\?xml[^>]*\?>", "", text or "")
    try:
        root = ET.fromstring("<Events>" + text + "</Events>")
    except ET.ParseError:
        return []
    return [e for e in root if e.tag in (_NS + "Event", "Event")]


def _find(el, path):
    r = el.find(path.replace("{ns}", _NS))
    return r if r is not None else el.find(path.replace("{ns}", ""))


def parse_event(el):
    """One <Event> element -> dict(record_id, provider, event_id, level, time, data=[strings]) or None."""
    s = _find(el, "{ns}System")
    if s is None:
        return None
    prov, rid, eid, lvl, tc = (_find(s, "{ns}" + t) for t in ("Provider", "EventRecordID", "EventID", "Level", "TimeCreated"))
    try:
        record_id = int(rid.text)
        event_id = int((eid.text or "").strip())
    except (AttributeError, TypeError, ValueError):
        return None
    try:
        level = int(lvl.text)
    except (AttributeError, TypeError, ValueError):
        level = 4
    data = []
    ed = _find(el, "{ns}EventData")
    if ed is not None:
        data = [(d.text or "") for d in ed]
    return {"record_id": record_id, "provider": (prov.get("Name") if prov is not None else "") or "", "event_id": event_id,
            "level": level, "time": (tc.get("SystemTime") if tc is not None else "") or "", "data": data}


def parse(text):
    """wevtutil XML text -> list of parsed event dicts (malformed records are skipped)."""
    return [e for e in map(parse_event, _xml_events(text)) if e]


def is_bt_event(ev):
    """True for a Bluetooth event worth acting on."""
    p = ev["provider"].lower()
    if p == "bthusb":
        return ev["event_id"] in BTHUSB_IDS
    if p in BT_PROVIDERS or p.startswith(BT_PREFIX):
        return ev["level"] <= 3          # critical, error, warning
    return False


def app_hit(ev):
    """Faulting app name (lower case, from WATCHED_APPS) for an Application Error / WER event, else None."""
    if ev["provider"].lower() not in APP_PROVIDERS:
        return None
    blob = " ".join(ev["data"]).lower()
    for a in WATCHED_APPS:
        if a in blob:
            return a
    return None


def _wevtutil(log_name, xpath, count=QUERY_COUNT):
    """Run wevtutil (newest first); returns stdout, or None when it cannot run (not Windows, timeout, error)."""
    try:
        r = subprocess.run(["wevtutil", "qe", log_name, "/q:" + xpath, "/f:xml", f"/c:{count}", "/rd:true"],
                           capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def system_query(after_id):
    return f"*[System[EventRecordID>{int(after_id)} and (Level=1 or Level=2 or Level=3 or Provider[@Name='BTHUSB'])]]"


def app_query(after_id=None, lookback_s=STARTUP_LOOKBACK_S):
    prov = "(Provider[@Name='Application Error'] or Provider[@Name='Windows Error Reporting'])"
    cond = f"EventRecordID>{int(after_id)}" if after_id is not None else f"TimeCreated[timediff(@SystemTime)<={int(lookback_s) * 1000}]"
    return f"*[System[{prov} and {cond}]]"


class Watcher:
    def __init__(self, log=None, run=None, report=None, poll_s=POLL_S, clock=time.time):
        """run(log_name, xpath, count) -> xml text or None (default: wevtutil); report(failure, **detail) (default: heal.report,
        looked up at call time; a missing heal module is fine)."""
        self.log, self.run, self._report, self.poll_s, self.clock = log or (lambda *a: None), run or _wevtutil, report, poll_s, clock
        self.events = collections.deque(maxlen=MAX_EVENTS)
        self.sys_last = None          # newest System record id seen (None = baseline not taken yet)
        self.app_last = None
        self.polls = 0
        self.stop_event = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def _heal_report(self, failure, **detail):
        fn = self._report
        if fn is None:
            try:
                from . import heal
                fn = heal.report
            except Exception:  # noqa: BLE001
                return
        try:
            fn(failure, **detail)
        except Exception as e:  # noqa: BLE001
            self.log(f"eventwatch: heal.report failed: {type(e).__name__}")

    def _add(self, **item):
        with self._lock:
            self.events.append(item)

    def poll_system(self):
        if self.sys_last is None:                       # baseline: remember the newest record, replay nothing
            out = self.run("System", "*", 1)
            evs = parse(out) if out else []
            if evs:
                self.sys_last = max(e["record_id"] for e in evs)
            elif out is not None and not out.strip():   # a log with no records at all
                self.sys_last = 0
            return []
        out = self.run("System", system_query(self.sys_last), QUERY_COUNT)
        if out is None:
            return []
        evs = sorted((e for e in parse(out) if e["record_id"] > self.sys_last), key=lambda e: e["record_id"])
        if evs:
            self.sys_last = evs[-1]["record_id"]
        new, seen = [e for e in evs if is_bt_event(e)], set()
        for e in new:
            self._add(log="System", provider=e["provider"], event_id=e["event_id"], record_id=e["record_id"],
                      level=e["level"], time=e["time"])
            self.log(f"eventwatch: bluetooth event provider={e['provider']} event={e['event_id']} record={e['record_id']} level={e['level']}")
            k = (e["provider"], e["event_id"])
            if k not in seen:
                seen.add(k)
                self._heal_report("bthusb_error", id=e["event_id"], provider=e["provider"], record_id=e["record_id"])
        return new

    def poll_app(self):
        first = self.app_last is None
        out = self.run("Application", app_query(None if first else self.app_last), QUERY_COUNT)
        if out is None:
            return []
        evs = sorted((e for e in parse(out) if first or e["record_id"] > self.app_last), key=lambda e: e["record_id"])
        if evs:
            self.app_last = evs[-1]["record_id"]
        if first:                                       # from now on only newer records (the lookback above was one-off)
            newest = parse(self.run("Application", "*", 1) or "")
            self.app_last = max([self.app_last or 0] + [e["record_id"] for e in newest])
        hits = []
        for e in evs:
            a = app_hit(e)
            if a:
                hits.append(e)
                self._add(log="Application", provider=e["provider"], event_id=e["event_id"], record_id=e["record_id"],
                          level=e["level"], time=e["time"], app=a, startup=first)
                self.log(f"eventwatch: app crash event app={a} provider={e['provider']} event={e['event_id']} record={e['record_id']}")
        return hits

    def poll(self):
        self.polls += 1
        r = []
        for fn in (self.poll_system, self.poll_app):
            try:
                r += fn()
            except Exception as e:  # noqa: BLE001
                self.log(f"eventwatch: {fn.__name__} failed: {type(e).__name__}: {e}")
        return r

    def status(self):
        with self._lock:
            return {"events": list(self.events)}

    def _loop(self):
        while not self.stop_event.is_set():
            self.poll()
            self.stop_event.wait(self.poll_s)

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self.stop_event.clear()
            self._thread = threading.Thread(target=self._loop, name="eventwatch", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self.stop_event.set()


_default = None


def start(log=None):
    """Start (once) the process-wide watcher. Does nothing off Windows."""
    global _default
    import os
    if os.name != "nt":
        return None
    if _default is None:
        _default = Watcher(log=log)
    return _default.start()


def stop():
    if _default is not None:
        _default.stop()


def status():
    return _default.status() if _default is not None else {"events": []}

"""connector/eventwatch.py: wevtutil XML parsing and the poll logic on canned event-log output (no Windows, no processes)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import eventwatch  # noqa: E402

NS = "http://schemas.microsoft.com/win/2004/08/events/event"


def ev(rid, provider, eid, level=2, data=(), t="2026-10-12T06:00:00.000000000Z"):
    d = "".join(f"<Data>{x}</Data>" for x in data)
    return (f"<Event xmlns='{NS}'><System><Provider Name='{provider}' Guid='{{00000000-0000-0000-0000-000000000000}}'/>"
            f"<EventID Qualifiers='49152'>{eid}</EventID><Level>{level}</Level><TimeCreated SystemTime='{t}'/>"
            f"<EventRecordID>{rid}</EventRecordID><Channel>System</Channel><Computer>TEST-PC</Computer></System>"
            f"<EventData>{d}</EventData></Event>")


class FakeLog:
    """Stands in for wevtutil: holds records per log, returns the newest `count` records (newest first, like /rd:true)."""
    def __init__(self):
        self.recs = {"System": [], "Application": []}     # (rid, xml)
        self.calls = []
        self.fail = False

    def add(self, log, rid, xml):
        self.recs[log].append((rid, xml))

    def __call__(self, log, xpath, count):
        self.calls.append((log, xpath, count))
        if self.fail:
            return None
        rows = sorted(self.recs[log], reverse=True)
        return "".join(x for _, x in rows[:count])


def watcher(fl, reports):
    return eventwatch.Watcher(log=lambda *a: reports.append(("log",) + a),
                              run=fl, report=lambda f, **d: reports.append((f, d)))


def test_parse_canned_xml_and_wrapper_without_root():
    xml = '<?xml version="1.0"?>' + ev(10, "BTHUSB", 3, data=["x"]) + ev(11, "BTHPORT", 17, level=3)
    evs = eventwatch.parse(xml)
    assert [(e["record_id"], e["provider"], e["event_id"], e["level"]) for e in evs] == [(10, "BTHUSB", 3, 2), (11, "BTHPORT", 17, 3)]
    assert evs[0]["time"].startswith("2026-10-12") and evs[0]["data"] == ["x"]


def test_parse_garbage_and_empty():
    assert eventwatch.parse("") == [] and eventwatch.parse(None) == []
    assert eventwatch.parse("<Event><oops") == []
    assert eventwatch.parse("<Event xmlns='%s'><System/></Event>" % NS) == []        # no record id


def test_is_bt_event_rules():
    mk = lambda p, i, l: {"provider": p, "event_id": i, "level": l}  # noqa: E731
    assert eventwatch.is_bt_event(mk("BTHUSB", 22, 4))                 # listed id, even at information level
    assert not eventwatch.is_bt_event(mk("BTHUSB", 5, 2))              # BTHUSB but not one of 3/8/17/18/22
    assert eventwatch.is_bt_event(mk("BTHPORT", 1, 2)) and eventwatch.is_bt_event(mk("BthEnum", 1, 1))
    assert not eventwatch.is_bt_event(mk("BTHPORT", 1, 3))             # warning from BTHPORT: routine reconnect noise, ignore
    assert not eventwatch.is_bt_event(mk("BthEnum", 1, 3))
    assert not eventwatch.is_bt_event(mk("Microsoft-Windows-Bluetooth-MTPEnum", 9, 3))
    assert not eventwatch.is_bt_event(mk("BTHPORT", 1, 4))             # information level from BTHPORT: ignore
    assert eventwatch.is_bt_event(mk("Microsoft-Windows-Bluetooth-MTPEnum", 9, 2))
    assert not eventwatch.is_bt_event(mk("Disk", 7, 2))


def test_first_poll_is_baseline_then_only_new_bt_events_reported():
    fl, out = FakeLog(), []
    fl.add("System", 100, ev(100, "BTHUSB", 3))                         # history: must not be replayed
    w = watcher(fl, out)
    assert w.poll() == [] and w.sys_last == 100 and not [o for o in out if o[0] == "bthusb_error"]
    fl.add("System", 101, ev(101, "Disk", 7))                           # unrelated error
    fl.add("System", 102, ev(102, "BTHUSB", 18))
    fl.add("System", 103, ev(103, "BthEnum", 5, level=2))
    new = w.poll()
    assert [e["record_id"] for e in new] == [102, 103] and w.sys_last == 103
    rep = [o for o in out if o[0] == "bthusb_error"]
    assert rep == [("bthusb_error", {"id": 18, "provider": "BTHUSB", "record_id": 102}),
                   ("bthusb_error", {"id": 5, "provider": "BthEnum", "record_id": 103})]
    assert any(o[0] == "log" and "BTHUSB" in o[1] for o in out)
    assert w.poll() == []                                               # nothing new: no duplicate report
    assert len([o for o in out if o[0] == "bthusb_error"]) == 2


def test_duplicate_events_in_one_poll_report_once_but_all_listed():
    fl, out = FakeLog(), []
    w = watcher(fl, out)
    w.poll()
    for r in (1, 2, 3):
        fl.add("System", r, ev(r, "BTHUSB", 8))
    w.poll()
    assert len([o for o in out if o[0] == "bthusb_error"]) == 1
    assert len([e for e in w.status()["events"] if e["log"] == "System"]) == 3


def test_wevtutil_failure_is_harmless_and_baseline_retried():
    fl, out = FakeLog(), []
    fl.fail = True
    w = watcher(fl, out)
    assert w.poll() == [] and w.sys_last is None and w.app_last is None
    fl.fail = False
    fl.add("System", 5, ev(5, "BTHUSB", 3))
    w.poll()
    assert w.sys_last == 5 and not [o for o in out if o[0] == "bthusb_error"]


def test_failure_and_baseline_are_logged_once():
    fl, out = FakeLog(), []
    fl.fail = True
    w = watcher(fl, out)
    w.poll()
    w.poll()
    fails = [o for o in out if o[0] == "log" and "query failed" in o[1]]
    assert len(fails) == 2 and {"System", "Application"} == {("System" if "System" in f[1] else "Application") for f in fails}
    fl.fail = False
    fl.add("System", 9, ev(9, "BTHUSB", 3))
    w.poll()
    assert any(o[0] == "log" and "watching System log from record 9" in o[1] for o in out)


def test_application_crash_of_watched_apps_only():
    fl, out = FakeLog(), []
    w = watcher(fl, out)
    w.poll()
    fl.add("Application", 20, ev(20, "Application Error", 1000, data=["YoussefConnector.exe", "KERNELBASE.dll"]))
    fl.add("Application", 21, ev(21, "Application Error", 1000, data=["notepad.exe", "1.0"]))
    fl.add("Application", 22, ev(22, "Windows Error Reporting", 1001, level=4, data=["AppCrash", "PhoneExperienceHost.exe"]))
    hits = w.poll()
    assert [h["record_id"] for h in hits] == [20, 22]
    evs = [e for e in w.status()["events"] if e["log"] == "Application"]
    assert [(e["app"], e["event_id"]) for e in evs] == [("youssefconnector.exe", 1000), ("phoneexperiencehost.exe", 1001)]
    assert not [o for o in out if o[0] == "bthusb_error"]               # crashes are logged, not bluetooth reports
    assert any(o[0] == "log" and "youssefconnector.exe" in o[1] for o in out)
    assert w.poll() == []


def test_application_startup_lookback_marks_startup_and_sets_baseline():
    fl, out = FakeLog(), []
    fl.add("Application", 7, ev(7, "Application Error", 1000, data=["YoussefConnector.exe"]))
    w = watcher(fl, out)
    w.poll()
    assert w.app_last == 7
    e = [x for x in w.status()["events"] if x["log"] == "Application"]
    assert len(e) == 1 and e[0]["startup"] is True
    assert "timediff(@SystemTime)<=600000" in [c for c in fl.calls if c[0] == "Application"][0][1]
    w.poll()
    assert len(w.status()["events"]) == 1                               # not replayed


def test_status_has_no_message_text_and_is_bounded():
    fl, out = FakeLog(), []
    w = watcher(fl, out)
    w.poll()
    for r in range(1, 41):
        fl.add("System", r, ev(r, "BTHUSB", 3, data=["C:\\private\\path"]))
    w.poll()
    st = w.status()["events"]
    assert len(st) == eventwatch.MAX_EVENTS and "private" not in repr(st)


def test_reporter_exception_does_not_break_poll():
    fl = FakeLog()
    logs = []
    w = eventwatch.Watcher(log=lambda *a: logs.append(a), run=fl, report=lambda f, **d: 1 / 0)
    w.poll()
    fl.add("System", 2, ev(2, "BTHUSB", 3))
    assert len(w.poll()) == 1 and w.sys_last == 2


def test_missing_heal_module_is_fine():
    fl = FakeLog()
    w = eventwatch.Watcher(run=fl)                                      # default reporter: heal.py may not exist
    w.poll()
    fl.add("System", 2, ev(2, "BTHUSB", 3))
    assert len(w.poll()) == 1


def test_queries_are_well_formed_and_bounded():
    q = eventwatch.app_query()
    assert "timediff(@SystemTime)<=600000" in q and "'Application Error'" in q and "'Windows Error Reporting'" in q
    fl = FakeLog()
    w = watcher(fl, [])
    for _ in range(3):
        w.poll()
    # no record-id filter (a full-log scan every poll): after the baseline every query is "*" or the one-off startup lookback
    assert all(c[1] == "*" or "timediff" in c[1] for c in fl.calls)
    assert all(c[2] <= eventwatch.QUERY_COUNT for c in fl.calls)
    assert any(c[2] == eventwatch.QUERY_COUNT and c[1] == "*" for c in fl.calls if c[0] == "System")


def test_module_start_is_noop_off_windows_and_status_default(monkeypatch):
    monkeypatch.setattr("os.name", "posix")
    assert eventwatch.start() is None
    assert eventwatch.status() == {"events": []}


def test_thread_loop_polls_and_stops():
    fl, out = FakeLog(), []
    w = eventwatch.Watcher(log=lambda *a: None, run=fl, report=lambda f, **d: out.append(f), poll_s=0.01)
    w.start()
    import time
    t = time.time()
    while w.polls < 3 and time.time() - t < 5:
        time.sleep(0.01)
    w.stop()
    assert w.polls >= 3


def test_bad_record_in_batch_does_not_blind_the_watcher():
    fl, out = FakeLog(), []
    w = watcher(fl, out)
    w.poll()
    fl.add("System", 1, ev(1, "BTHUSB", 3))
    fl.add("System", 2, ev(2, "Disk", 7, data=["bad &#x0; char"]))              # illegal XML character reference
    fl.add("System", 3, ev(3, "Disk", 7, data=["raw \x01 ctrl"]))                 # literal control character
    fl.add("System", 4, "<Event xmlns='%s'><System><oops></Event>" % NS)        # not XML at all
    fl.add("System", 5, ev(5, "BTHUSB", 18))
    new = w.poll()
    assert [e["record_id"] for e in new] == [1, 5] and w.sys_last == 5          # sys_last still advances past the bad records
    assert [o[1]["record_id"] for o in out if o[0] == "bthusb_error"] == [1, 5]
    assert any(o[0] == "log" and "unparseable" in o[1] for o in out)            # and it says so
    assert w.poll() == []


def test_parse_per_record_with_bad_records_between():
    xml = ev(1, "BTHUSB", 3) + ev(2, "Disk", 7, data=["&#x0;"]) + "<Event><System><oops></Event>" + ev(4, "BTHUSB", 8)
    assert [e["record_id"] for e in eventwatch.parse(xml)] == [1, 2, 4]          # illegal char refs are dropped, record 3 is skipped


def test_bad_record_in_baseline_batch_does_not_freeze_baseline():
    fl, out = FakeLog(), []
    fl.add("System", 100, ev(100, "BTHUSB", 3))
    fl.add("System", 101, ev(101, "Disk", 7, data=["&#x0;"]))
    w = watcher(fl, out)
    w.poll()
    assert w.sys_last is not None and w.sys_last >= 100
    fl.add("System", 102, "<Event><System><oops></Event>")                       # newest record unreadable: baseline retried, not 0
    w2 = watcher(fl, out)
    w2.sys_last = None
    w2.poll_system()
    assert w2.sys_last is None
    fl.add("System", 103, ev(103, "BTHUSB", 3))
    w2.poll_system()
    assert w2.sys_last == 103


def test_failed_newest_record_query_keeps_app_baseline_unset_and_does_not_replay():
    class Flaky(FakeLog):
        def __init__(self):
            super().__init__()
            self.fail_newest = True

        def __call__(self, log, xpath, count):
            if log == "Application" and xpath == "*" and count == 1 and self.fail_newest:
                self.calls.append((log, xpath, count))
                return None
            return super().__call__(log, xpath, count)
    fl, out = Flaky(), []
    for r in range(1, 31):                                                       # old history in the Application log
        fl.add("Application", r, ev(r, "Application Error", 1000, data=["YoussefConnector.exe"]))
    w = watcher(fl, out)
    w.poll()
    assert w.app_last is None                                                    # NOT 0
    first = [e for e in w.status()["events"] if e["log"] == "Application"]
    w.poll()
    assert w.app_last is None
    assert len([e for e in w.status()["events"] if e["log"] == "Application"]) == len(first)   # the retry does not duplicate them
    fl.fail_newest = False
    w.poll()
    assert w.app_last == 30
    assert len([e for e in w.status()["events"] if e["log"] == "Application"]) == len(first)
    assert all(e["startup"] for e in w.status()["events"] if e["log"] == "Application")
    w.poll()                                                                     # old records are never replayed as live crashes
    assert not [e for e in w.status()["events"] if e["log"] == "Application" and not e["startup"]]


def test_warning_level_bluetooth_events_are_ignored_but_errors_reported():
    fl, out = FakeLog(), []
    w = watcher(fl, out)
    w.poll()
    fl.add("System", 1, ev(1, "BTHPORT", 10, level=3))
    fl.add("System", 2, ev(2, "Microsoft-Windows-Bluetooth-MTPEnum", 9, level=3))
    fl.add("System", 3, ev(3, "BthEnum", 4, level=2))
    new = w.poll()
    assert [e["record_id"] for e in new] == [3] and w.sys_last == 3
    assert [o[1]["provider"] for o in out if o[0] == "bthusb_error"] == ["BthEnum"]


def test_system_gap_is_logged():
    fl, out = FakeLog(), []
    w = watcher(fl, out)
    w.poll()
    for r in range(1, eventwatch.QUERY_COUNT + 20):
        fl.add("System", r, ev(r, "Disk", 7, level=4))
    w.poll()
    assert any(o[0] == "log" and "gap" in o[1] for o in out)

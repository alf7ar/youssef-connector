"""main.with_events: the event-log events land in ready["heal"]["events"] whatever the other ready() wrappers did."""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import main  # noqa: E402

EVS = [{"log": "System", "provider": "BTHUSB", "event_id": 3, "record_id": 7}]


class StubHeal:
    @staticmethod
    def status():
        return {"actions": [{"name": "hf_refresh"}], "busy": False, "policy": {}}


def phone_ready():
    return {"ok": True, "hf": "active"}


def test_events_merge_into_heal_status_when_heal_wrapper_is_inside():
    heal_wrapped = lambda: {**phone_ready(), "heal": StubHeal.status()}  # noqa: E731  (what c-H1 does to phone.ready)
    r = main.with_events(heal_wrapped, lambda: EVS)()
    assert r["heal"]["events"] == EVS and r["heal"]["actions"] == [{"name": "hf_refresh"}] and r["ok"] is True


def test_without_heal_and_without_events():
    assert main.with_events(phone_ready, lambda: EVS)()["heal"] == {"events": EVS}
    assert main.with_events(phone_ready, lambda: [])() == phone_ready()
    assert main.with_events(lambda: {"ok": 1, "heal": StubHeal.status()}, lambda: [])()["heal"] == StubHeal.status()


def test_never_raises_and_non_dict_passthrough():
    def boom():
        raise RuntimeError("x")
    assert main.with_events(phone_ready, boom)() == phone_ready()
    assert main.with_events(lambda: None, lambda: EVS)() is None


def test_heal_value_not_a_dict_is_replaced_not_crashed():
    r = main.with_events(lambda: {"heal": "weird"}, lambda: EVS)()
    assert r["heal"] == {"events": EVS}


def test_events_wrapper_is_the_last_ready_wrapper_in_app_run():
    src = inspect.getsource(main.App.run)
    wrappers = [i for i in range(len(src)) if src.startswith("phone.ready =", i)]
    assert wrappers and src.startswith("phone.ready = with_events(", wrappers[-1])

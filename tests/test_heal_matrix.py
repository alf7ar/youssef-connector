"""connector/heal.py policy matrix: every failure class x heavy tiers (off/on) x in-call (no/yes) x hourly caps (free/reached)
-> the action tick() runs next (or None). The expected value is computed here from the ladder rules, independently of the
engine's _pick/_skip_reason code. Clock is fake (heal._now / heal._wall)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import heal  # noqa: E402

HEAVY = ("radio_cycle", "adapter_reset")
CAP_KEY = {"radio_on": "max_radio_toggles_per_hour", "radio_cycle": "max_radio_toggles_per_hour",
           "adapter_reset": "max_adapter_resets_per_hour"}
ACTIONS = ("radio_on", "radio_cycle", "adapter_reset", "hf_refresh", "phone_connect", "phone_link_launch")
FAILURES = tuple(heal.LADDERS)


def expected_next(failure, heavy_on, in_call, capped):
    """Ladder rules: in a call nothing runs; otherwise the first rung that is not heavy-while-off and not cap-blocked."""
    if in_call:
        return None
    for act in heal.LADDERS[failure]:
        if act in HEAVY and not heavy_on:
            continue
        if act in CAP_KEY and capped:
            continue
        return act
    return None


@pytest.fixture
def clk(monkeypatch):
    heal.reset_for_tests()
    monkeypatch.setattr(heal, "LADDER_RESTART_S", 1e9)

    class Clock:
        t = 1000.0

        def __call__(self):
            return self.t

    c = Clock()
    heal._now = c
    heal._wall = c
    yield c
    heal.reset_for_tests()


def register_all(calls):
    for n in ACTIONS:
        heal.register_action(n, (lambda n=n: calls.append(n) or {"ok": True}), tier="heavy" if n in HEAVY else "safe")


@pytest.mark.parametrize("in_call", [False, True], ids=["no_call", "in_call"])
@pytest.mark.parametrize("capped", [False, True], ids=["cap_free", "cap_reached"])
@pytest.mark.parametrize("heavy_on", [False, True], ids=["heavy_off", "heavy_on"])
@pytest.mark.parametrize("failure", FAILURES)
def test_policy_matrix_next_action(clk, failure, heavy_on, in_call, capped):
    calls = []
    register_all(calls)
    policy = {"radio_cycle": heavy_on, "adapter_reset": heavy_on}
    heal.set_policy(policy)
    if capped:
        for key in set(CAP_KEY.values()):
            heal._cap_hits[key].extend([clk.t] * int(heal._policy[key]))
    heal.set_in_call(lambda: in_call)

    heal.report(failure)
    entry = heal.tick()

    want = expected_next(failure, heavy_on, in_call, capped)
    if want is None:
        assert entry is None
        assert calls == []
    else:
        assert entry is not None and entry["action"] == want
        assert entry["failure"] == failure and entry["ok"] is True
        assert calls == [want]


@pytest.mark.parametrize("failure", FAILURES)
def test_default_policy_matrix_is_heavy_off(clk, failure):
    """No policy pushed (missing key = today's behavior): heavy rungs never run, even with caps free."""
    calls = []
    register_all(calls)
    heal.set_policy(None)
    for _ in range(len(heal.LADDERS[failure]) + 1):
        heal.report(failure)
        heal.tick()
        clk.t += 400
    assert "radio_cycle" not in calls and "adapter_reset" not in calls

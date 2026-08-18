"""The wake-rate circuit breaker driven offline with a fake clock.

The breaker is the router's runaway-loop backstop (basecradle-router#110): a
synthetic burst of wakes trips it (dispatch stops), normal rates never trip, and
the window self-heals after the cooldown. The clock is injected so the tests are
deterministic and never sleep. No network, model, or live agent.
Test cast: Nova Digital (``nova``, AI) is the agent woken.
"""

import logging

from basecradle_router import breaker as breaker_mod
from basecradle_router.breaker import (
    AGENT_SCOPE,
    STREAM_SCOPE,
    BreakerConfig,
    BreakerState,
    WakeRateBreaker,
)

NOVA = "nova"  # the agent's harness_key (its OS-user slug)
TIMELINE = "0192aaaa-bbbb-7ccc-8ddd-eeeeffff0000"
OTHER_TIMELINE = "0192cccc-dddd-7eee-8fff-000011112222"


class _Clock:
    """A hand-cranked monotonic clock: starts at 0, advances only when told."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _breaker(clock: _Clock, **overrides) -> WakeRateBreaker:
    cfg = BreakerConfig(**{"max_wakes": 5, "window": 60.0, "cooldown": 60.0, **overrides})
    return WakeRateBreaker(cfg, clock=clock)


# --- config validation -----------------------------------------------------


def test_config_rejects_nonsense_thresholds() -> None:
    import pytest

    for bad in (
        {"max_wakes": 0},
        {"window": 0},
        {"window": -1},
        {"cooldown": -1},
        {"stream_max_wakes": -1},
    ):
        with pytest.raises(ValueError):
            BreakerConfig(**bad)


# --- the agent scope: normal load never trips ------------------------------


def test_steady_normal_rate_never_trips() -> None:
    clock = _Clock()
    breaker = _breaker(clock)
    # One wake every 30s, well under 5/60s — runs forever without tripping.
    for _ in range(50):
        outcome = breaker.admit(NOVA)
        assert outcome.admitted
        clock.advance(30.0)


def test_a_burst_at_the_cap_still_admits() -> None:
    # Exactly the threshold within the window is allowed; only *over* trips.
    clock = _Clock()
    breaker = _breaker(clock, max_wakes=5)
    for _ in range(5):
        assert breaker.admit(NOVA).admitted


# --- the agent scope: a runaway burst trips --------------------------------


def test_a_runaway_burst_trips_and_halts_dispatch() -> None:
    clock = _Clock()
    breaker = _breaker(clock, max_wakes=5)
    for _ in range(5):
        assert breaker.admit(NOVA).admitted  # the 1st..5th fit the window
    tripped = breaker.admit(NOVA)  # the 6th goes over → trip
    assert tripped.state is BreakerState.TRIPPED
    assert tripped.scope == AGENT_SCOPE
    assert tripped.key == NOVA
    assert not tripped.admitted
    # Further wakes during cooldown are refused as OPEN, without re-tripping.
    again = breaker.admit(NOVA)
    assert again.state is BreakerState.OPEN
    assert not again.admitted


def test_a_different_agent_is_unaffected_by_anothers_trip() -> None:
    clock = _Clock()
    breaker = _breaker(clock, max_wakes=5)
    for _ in range(6):
        breaker.admit(NOVA)  # trips nova
    assert not breaker.admit(NOVA).admitted
    # A different agent has its own window — it is not collateral damage.
    assert breaker.admit("jt").admitted


# --- auto-cooldown reset ---------------------------------------------------


def test_cooldown_elapses_and_dispatch_resumes() -> None:
    clock = _Clock()
    breaker = _breaker(clock, max_wakes=5, cooldown=60.0)
    for _ in range(6):
        breaker.admit(NOVA)  # trip
    assert not breaker.admit(NOVA).admitted  # still cooling down at t=0

    clock.advance(59.0)
    assert not breaker.admit(NOVA).admitted  # cooldown not yet elapsed

    clock.advance(2.0)  # t=61 > trip(0)+cooldown(60)
    healed = breaker.admit(NOVA)
    assert healed.admitted  # window cleared, wakes resume


def test_old_wakes_age_out_of_the_window() -> None:
    # Wakes spread across more than the window never accumulate to the cap.
    clock = _Clock()
    breaker = _breaker(clock, max_wakes=5, window=60.0)
    for _ in range(4):
        assert breaker.admit(NOVA).admitted
        clock.advance(20.0)  # 4 wakes over 60s, but never 6 within any 60s window
    for _ in range(4):
        assert breaker.admit(NOVA).admitted
        clock.advance(20.0)


# --- the per-(agent, stream) scope -----------------------------------------


def test_one_looping_stream_trips_under_the_agent_cap() -> None:
    # The agent cap is generous, but a single timeline spinning trips the tighter
    # per-stream cap before the agent total is reached.
    clock = _Clock()
    breaker = _breaker(clock, max_wakes=100, stream_max_wakes=5)
    for _ in range(5):
        assert breaker.admit(NOVA, TIMELINE).admitted
    tripped = breaker.admit(NOVA, TIMELINE)
    assert tripped.state is BreakerState.TRIPPED
    assert tripped.scope == STREAM_SCOPE
    assert tripped.key == TIMELINE


def test_one_tripped_stream_does_not_block_another_stream() -> None:
    clock = _Clock()
    breaker = _breaker(clock, max_wakes=100, stream_max_wakes=5)
    for _ in range(6):
        breaker.admit(NOVA, TIMELINE)  # trips the first timeline
    assert not breaker.admit(NOVA, TIMELINE).admitted
    # A different timeline for the same agent has its own per-stream window.
    assert breaker.admit(NOVA, OTHER_TIMELINE).admitted


def test_stream_scope_disabled_when_stream_max_is_zero() -> None:
    clock = _Clock()
    breaker = _breaker(clock, max_wakes=100, stream_max_wakes=0)
    # With the per-stream scope off, only the (generous) agent cap applies.
    for _ in range(50):
        assert breaker.admit(NOVA, TIMELINE).admitted


def test_agent_scope_trips_even_across_many_streams() -> None:
    # No single stream loops, but the agent's *total* across streams runs away.
    clock = _Clock()
    breaker = _breaker(clock, max_wakes=5, stream_max_wakes=100)
    for i in range(5):
        assert breaker.admit(NOVA, f"timeline-{i}").admitted
    tripped = breaker.admit(NOVA, "timeline-overflow")
    assert tripped.state is BreakerState.TRIPPED
    assert tripped.scope == AGENT_SCOPE


# --- the trip is loud, never silent ----------------------------------------


def test_trip_escalates_with_a_loud_error_log(caplog) -> None:
    clock = _Clock()
    breaker = _breaker(clock, max_wakes=2)
    with caplog.at_level(logging.INFO, logger="basecradle_router.breaker"):
        for _ in range(3):
            breaker.admit(NOVA)  # third trips
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "event=breaker_tripped" in errors[0].getMessage()
    assert f"agent={NOVA}" in errors[0].getMessage()


def test_the_trip_line_keeps_every_field_the_prose_sentence_carried(caplog) -> None:
    # #228 normalised the breaker's prose into the router's kv grammar. The SHAPE moved;
    # the DATA did not — a trip must still say which scope, which key, which agent, how
    # many wakes over what threshold, and how long the halt lasts, because that is the
    # whole content of the runaway-loop escalation an operator reads at 3am.
    clock = _Clock()
    breaker = _breaker(clock, max_wakes=2, window=60.0, cooldown=90.0)
    with caplog.at_level(logging.INFO, logger="basecradle_router.breaker"):
        for _ in range(3):
            breaker.admit(NOVA, "issue-7")
    line = next(r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)
    for field in (
        "event=breaker_tripped",
        f"agent={NOVA}",
        f"scope={AGENT_SCOPE}",
        f"key={NOVA}",
        "count=3",
        "threshold=2",
        "window=60s",
        "cooldown=90s",
    ):
        assert field in line, f"the trip line dropped {field!r}: {line}"


def test_a_refusal_mid_cooldown_is_spelled_as_a_refused_wake(caplog) -> None:
    # One vocabulary for one fact: the wake-lock guard already refuses wakes with
    # `event=wake_refused reason=<why>`, so the breaker's cooling-down refusal says the
    # same thing the same way — one query finds every wake a gate stopped, and `reason=`
    # says which gate. The old line led with a prose "wake refused: circuit breaker OPEN"
    # that no such query could reach.
    clock = _Clock()
    breaker = _breaker(clock, max_wakes=1, cooldown=60.0)
    for _ in range(2):
        breaker.admit(NOVA)  # second trips
    with caplog.at_level(logging.INFO, logger="basecradle_router.breaker"):
        assert breaker.admit(NOVA).state is BreakerState.OPEN
    line = next(r.getMessage() for r in caplog.records if "event=wake_refused" in r.getMessage())
    assert f"reason={breaker_mod.BREAKER_OPEN}" in line
    assert f"agent={NOVA}" in line
    assert f"scope={AGENT_SCOPE}" in line


# --- the per-stream window dict stays bounded ------------------------------


def test_stale_stream_windows_are_garbage_collected(monkeypatch) -> None:
    # A daemon that runs for ages must not accumulate one window per distinct
    # timeline forever: a stale, untripped window is reclaimed by the periodic GC.
    monkeypatch.setattr(breaker_mod, "_GC_INTERVAL", 4)
    clock = _Clock()
    breaker = _breaker(clock, max_wakes=100, stream_max_wakes=100, window=60.0)

    # Touch three distinct timelines, then let them all age out of the window.
    for stream in ("t-1", "t-2", "t-3"):
        breaker.admit(NOVA, stream)
    clock.advance(120.0)  # every recorded wake is now older than the window

    # Drive admits on a *fresh* stream until the GC interval fires; the three
    # aged-out windows (and the aged agent window) are swept, leaving only live ones.
    for _ in range(4):
        breaker.admit(NOVA, "t-live")

    keys = set(breaker._windows)
    assert (STREAM_SCOPE, NOVA, "t-1") not in keys
    assert (STREAM_SCOPE, NOVA, "t-2") not in keys
    assert (STREAM_SCOPE, NOVA, "t-3") not in keys
    assert (STREAM_SCOPE, NOVA, "t-live") in keys  # still active → kept


def test_gc_never_drops_a_tripped_window(monkeypatch) -> None:
    # A tripped window holds the live cooldown clock — the GC must never reclaim it.
    monkeypatch.setattr(breaker_mod, "_GC_INTERVAL", 2)
    clock = _Clock()
    breaker = _breaker(clock, max_wakes=2, cooldown=300.0)
    for _ in range(3):
        breaker.admit(NOVA)  # trip nova's agent window (cooldown=300s)
    clock.advance(120.0)  # well past the 60s window, but inside the cooldown
    for _ in range(2):
        breaker.admit("other")  # drive admits to fire the GC
    # nova is still cooling down: its window survives and still refuses.
    assert breaker.admit(NOVA).state is BreakerState.OPEN


def test_reset_is_logged(caplog) -> None:
    clock = _Clock()
    breaker = _breaker(clock, max_wakes=2, cooldown=10.0)
    for _ in range(3):
        breaker.admit(NOVA)  # trip
    clock.advance(11.0)
    with caplog.at_level(logging.INFO, logger="basecradle_router.breaker"):
        assert breaker.admit(NOVA).admitted
    resets = [r for r in caplog.records if "event=breaker_reset" in r.getMessage()]
    assert len(resets) == 1
    assert f"agent={NOVA}" in resets[0].getMessage()
    assert f"scope={AGENT_SCOPE}" in resets[0].getMessage()

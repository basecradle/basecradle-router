"""The wake scheduler: per-agent-fair, bounded, one wake in flight per agent.

These pin the properties basecradle-router#182 turned out to need — the Fleet
Transport incident where one busy agent's deep wake backlog starved every other
agent's wake (including the NOC's transport probe) because the old default-executor
hand-off both parked pool threads on a blocking agent lock and had no cross-agent
fairness. The load-bearing test is
``test_a_busy_agents_backlog_does_not_delay_an_idle_agents_wake`` — the incident's
exact shape, and the definition of done: a deep backlog cannot delay an idle agent's
wake beyond its own queue depth.

Everything here drives the scheduler directly (it is framework-free — pure threading),
with a fake ``run`` callable standing in for ``pipeline.execute``. No model, agent, or
network is touched; gating is by :class:`threading.Event` so the tests are
deterministic rather than timing-dependent.

Cast: fabricated builder agents keyed by the repo they captain; their delivery ids are
well-formed UUIDv7s (CLAUDE.md), and each wake carries a human-readable label in its
``wake_arg`` so an ordering assertion reads plainly.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import pytest

from basecradle_router.models import Agent, Event, EventKind, Recipient
from basecradle_router.pipeline import PipelineResult
from basecradle_router.scheduler import WakeScheduler


def _agent(slug: str) -> Agent:
    """A builder agent whose harness_key (its OS-user slug) is ``slug``."""
    return Agent(
        key=f"basecradle/{slug}",
        os_user=slug,
        clone_path=f"/home/{slug}/clone",
        bot_slug=f"{slug}-ai",
    )


def _event(slug: str, label: str, n: int) -> Event:
    """An event for ``slug``'s agent, tagged ``label`` (recorded in ordering tests)."""
    return Event(
        source="github",
        kind=EventKind.HANDOFF,
        recipient=Recipient(by="repo", value=f"basecradle/{slug}"),
        wake_arg=label,
        # A well-formed UUIDv7 — version nibble 7, variant nibble 9 — unique per n.
        delivery_id=f"0192f3a4-5b6c-7d8e-9f01-{n:012x}",
    )


def _eventually(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Poll ``predicate`` until true or ``timeout`` — for the few non-event checks."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# --- construction ----------------------------------------------------------


def test_lanes_must_be_at_least_one() -> None:
    with pytest.raises(ValueError, match="lanes must be >= 1"):
        WakeScheduler(lambda *_a: None, lanes=0)


def test_lanes_is_exposed_for_the_startup_banner() -> None:
    sched = WakeScheduler(lambda *_a: None, lanes=5)
    try:
        assert sched.lanes == 5
    finally:
        sched.shutdown()


def test_a_submitted_wake_runs_with_its_agent_and_event() -> None:
    seen: list[tuple[str, str]] = []

    def run(agent: Agent, event: Event, _result: PipelineResult) -> None:
        seen.append((agent.harness_key, event.wake_arg))

    sched = WakeScheduler(run, lanes=2)
    try:
        sched.submit(_agent("one"), _event("one", "go", 1), PipelineResult())
        assert sched.wait_idle(timeout=5)
        assert seen == [("one", "go")]
    finally:
        sched.shutdown()


# --- the single-in-flight-per-agent guarantee ------------------------------


def test_at_most_one_wake_per_agent_even_with_spare_lanes() -> None:
    first_running = threading.Event()
    release_first = threading.Event()
    second_running = threading.Event()

    def run(_agent_: Agent, event: Event, _result: PipelineResult) -> None:
        if event.wake_arg == "first":
            first_running.set()
            assert release_first.wait(5)
        else:
            second_running.set()

    sched = WakeScheduler(run, lanes=4)  # ample lanes; only the per-agent rule can hold #2
    try:
        agent = _agent("solo")
        sched.submit(agent, _event("solo", "first", 1), PipelineResult())
        assert first_running.wait(5)
        sched.submit(agent, _event("solo", "second", 2), PipelineResult())
        # Three lanes are free, yet the second wake for the SAME agent must not start
        # while the first holds it — scheduling, not a blocked thread, serialises it.
        assert not second_running.wait(0.2)
        release_first.set()
        assert second_running.wait(5)
        assert sched.wait_idle(timeout=5)
    finally:
        sched.shutdown()


def test_wakes_for_one_agent_run_in_submission_order() -> None:
    order: list[str] = []
    lock = threading.Lock()

    def run(_agent_: Agent, event: Event, _result: PipelineResult) -> None:
        with lock:
            order.append(event.wake_arg)

    sched = WakeScheduler(run, lanes=4)
    try:
        agent = _agent("seq")
        labels = [f"j{n}" for n in range(6)]
        for n, label in enumerate(labels):
            sched.submit(agent, _event("seq", label, n), PipelineResult())
        assert sched.wait_idle(timeout=5)
        # Single in-flight per agent + a FIFO queue = the agent's one ordered stream.
        assert order == labels
    finally:
        sched.shutdown()


# --- the incident, and the definition of done ------------------------------


def test_a_busy_agents_backlog_does_not_delay_an_idle_agents_wake() -> None:
    # The Fleet Transport incident's exact shape (basecradle-router#182), and the DoD:
    # a busy agent with a deep wake backlog cannot delay an idle agent's wake beyond
    # its own queue depth. Here the idle agent's queue depth is 1, so it must run at
    # once on the free lane while the busy agent's 8-deep backlog is still undrained.
    release_busy = threading.Event()
    busy_lock = threading.Lock()
    busy_started: list[str] = []
    idle_ran = threading.Event()

    def run(agent: Agent, event: Event, _result: PipelineResult) -> None:
        if agent.harness_key == "busy":
            with busy_lock:
                busy_started.append(event.wake_arg)
            assert release_busy.wait(5)
        else:
            idle_ran.set()

    sched = WakeScheduler(run, lanes=2)
    try:
        busy = _agent("busy")
        for n in range(8):  # a deep backlog for one agent — far past the lane count
            sched.submit(busy, _event("busy", f"busy-{n}", 100 + n), PipelineResult())
        # The idle agent's single wake, submitted AFTER the whole backlog is queued.
        sched.submit(_agent("idle"), _event("idle", "idle-0", 200), PipelineResult())

        # It runs promptly on the second lane, without the busy backlog draining at all.
        assert idle_ran.wait(5), "idle agent's wake starved behind the busy backlog"
        # And the busy agent held exactly ONE lane throughout: its 8-deep backlog never
        # occupied more than one lane, so it could never have clogged the pool.
        with busy_lock:
            assert busy_started == ["busy-0"]

        release_busy.set()
        assert sched.wait_idle(timeout=10)
        with busy_lock:
            assert len(busy_started) == 8  # nothing was dropped — the backlog all ran
    finally:
        sched.shutdown()


def test_round_robin_fairness_a_hot_agent_cannot_jump_a_waiting_peer() -> None:
    order: list[str] = []
    lock = threading.Lock()
    a1_running = threading.Event()
    release_a1 = threading.Event()

    def run(_agent_: Agent, event: Event, _result: PipelineResult) -> None:
        with lock:
            order.append(event.wake_arg)
        if event.wake_arg == "A1":
            a1_running.set()
            assert release_a1.wait(5)

    sched = WakeScheduler(run, lanes=1)  # one lane, so ordering is fully observable
    try:
        agent_a, agent_b = _agent("aaa"), _agent("bbb")
        sched.submit(agent_a, _event("aaa", "A1", 1), PipelineResult())
        assert a1_running.wait(5)  # A1 holds the one lane
        sched.submit(agent_a, _event("aaa", "A2", 2), PipelineResult())  # A's 2nd, queued
        sched.submit(agent_b, _event("bbb", "B1", 3), PipelineResult())  # B's 1st, queued after
        release_a1.set()
        assert sched.wait_idle(timeout=5)
        # A2 was submitted before B1, but A had just run — so on A1's completion A goes
        # to the BACK of the ready queue and B1 runs first. A hot agent refilling its
        # own queue cannot starve a waiting peer.
        assert order == ["A1", "B1", "A2"]
    finally:
        sched.shutdown()


def test_total_concurrency_never_exceeds_lanes() -> None:
    lanes = 3
    lock = threading.Lock()
    live = 0
    peak = 0
    lanes_full = threading.Event()
    release = threading.Event()

    def run(_agent_: Agent, _event_: Event, _result: PipelineResult) -> None:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
            if live >= lanes:
                lanes_full.set()
        assert release.wait(5)
        with lock:
            live -= 1

    sched = WakeScheduler(run, lanes=lanes)
    try:
        for n in range(lanes + 3):  # more distinct agents than lanes, all wanting one
            sched.submit(_agent(f"ag{n}"), _event(f"ag{n}", f"w{n}", n), PipelineResult())
        assert lanes_full.wait(5)  # the lanes filled
        time.sleep(0.15)  # give any (incorrect) over-scheduling a chance to show
        with lock:
            assert peak == lanes  # never a lane more, even with extra agents queued
        release.set()
        assert sched.wait_idle(timeout=10)
    finally:
        sched.shutdown()


# --- resilience and lifecycle ----------------------------------------------


def test_a_defect_in_the_run_fn_frees_the_lane_and_keeps_draining(caplog) -> None:
    ran: list[str] = []
    lock = threading.Lock()

    def run(_agent_: Agent, event: Event, _result: PipelineResult) -> None:
        with lock:
            ran.append(event.wake_arg)
        if event.wake_arg == "boom":
            raise RuntimeError("defect in execute")

    sched = WakeScheduler(run, lanes=2)
    try:
        with caplog.at_level(logging.ERROR, logger="basecradle_router.scheduler"):
            agent = _agent("crashy")
            sched.submit(agent, _event("crashy", "boom", 1), PipelineResult())
            sched.submit(agent, _event("crashy", "after", 2), PipelineResult())
            assert sched.wait_idle(timeout=5)
        # The raising wake did not wedge the agent: the lane was freed and the next
        # same-agent wake still ran. (pipeline.execute never raises; this guards a defect.)
        assert ran == ["boom", "after"]
        assert any("wake run raised" in r.getMessage() for r in caplog.records)
    finally:
        sched.shutdown()


def test_saturation_is_logged_on_the_transition(caplog) -> None:
    release = threading.Event()
    reached = threading.Event()

    def run(_agent_: Agent, _event_: Event, _result: PipelineResult) -> None:
        reached.set()
        assert release.wait(5)

    sched = WakeScheduler(run, lanes=1)
    try:
        with caplog.at_level(logging.INFO, logger="basecradle_router.scheduler"):
            sched.submit(_agent("h1"), _event("h1", "a", 1), PipelineResult())
            assert reached.wait(5)  # h1 holds the only lane
            # A second agent now has pending work but no lane → saturated.
            sched.submit(_agent("h2"), _event("h2", "b", 2), PipelineResult())
            release.set()
            assert sched.wait_idle(timeout=5)
        msgs = [r.getMessage() for r in caplog.records]
        assert sum("saturated: all" in m for m in msgs) == 1  # edge-triggered, logged once
        assert any("no longer saturated" in m for m in msgs)
    finally:
        sched.shutdown()


def test_wait_idle_returns_immediately_when_nothing_is_queued() -> None:
    sched = WakeScheduler(lambda *_a: None, lanes=2)
    try:
        assert sched.wait_idle(timeout=0.1) is True
    finally:
        sched.shutdown()


def test_wait_idle_times_out_while_a_wake_is_still_running() -> None:
    release = threading.Event()
    running = threading.Event()

    def run(_agent_: Agent, _event_: Event, _result: PipelineResult) -> None:
        running.set()
        assert release.wait(5)

    sched = WakeScheduler(run, lanes=1)
    try:
        sched.submit(_agent("slow"), _event("slow", "x", 1), PipelineResult())
        assert running.wait(5)
        assert sched.wait_idle(timeout=0.1) is False  # not idle — a wake is in flight
        release.set()
        assert sched.wait_idle(timeout=5) is True
    finally:
        sched.shutdown()

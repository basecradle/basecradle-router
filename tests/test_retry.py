"""A temporarily failed wake is retried at about 1, 5 and 30 minutes; a success ends it (#527).

On 2026-11-02 two wakes died on a vendor's 429 and the reply never came: the router's three
attempts fell inside ten seconds, and the outage outlasted them. These pin the job queue's
rule that replaced them, through the real pipeline with the wake mocked at its boundary and
the timers fired by hand, so nothing waits:

- only exit 75 (``EX_TEMPFAIL``) earns the schedule, leaving the quick retry at once; every
  other exit keeps its three quick tries and its ``outcome=failed``;
- each delivery has its own schedule, its attempts at about 1, 5 and 30 minutes, the last
  one's failure the delivery's failure;
- a success cancels the retries left: the retry's own, by scheduling nothing more, and a
  newer delivery's, by covering the waiting retry so it collapses at dispatch;
- the wake is told which attempt it is; a stop ends the schedule; the wake lock makes a
  retry wait, never drops it; and a schedule survives a restart.

Cast: Nova Digital (``nova``), a harness agent, woken for John Doe's (``john``) messages on
her timeline. Every id is a fabricated, well-formed UUIDv7.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

from basecradle_router.config import Config
from basecradle_router.evidence import EvidenceStore
from basecradle_router.models import Agent, Event, EventKind, IssueRef, Recipient, WakeKind
from basecradle_router.pipeline import Outcome, Pipeline, PipelineResult, Stage
from basecradle_router.retry import DEFAULT_DELAYS, RetrySchedule
from basecradle_router.routes import RouteRegistry
from basecradle_router.wake import (
    ATTEMPT_ENV,
    ATTEMPTS_ENV,
    HomeServerWaker,
    SubprocessWaker,
    WakeAttempt,
    WakeError,
    WakeInvocation,
    WakeResult,
    WakeStopped,
    WakeTempFailed,
    WakeTry,
)
from basecradle_router.wakelock import WakeLockDecision, WakeLockState

#: The first of three quick tries (#533).
FIRST_TRY = WakeTry(1, 3)

NOVA = Agent(
    key="nova",
    os_user="nova",
    clone_path="/home/nova/harness",
    wake_kind=WakeKind.HARNESS,
    recipient_uuid="01a161c5-9ff1-76f8-a04c-c476e36c30b7",
    wake_bin="/home/nova/venv/bin/basecradle-harness-wake",
)
TIMELINE = "0192aaaa-bbbb-7ccc-8ddd-eeeeffff0001"
START = datetime(2026, 11, 2, 3, 0, 0, tzinfo=timezone.utc)

_ids = itertools.count(1)


def _event(*, at: datetime = START, source: str = "basecradle") -> Event:
    return Event(
        source=source,
        kind=EventKind.PLATFORM_EVENT,
        recipient=Recipient(by="recipient_uuid", value=NOVA.recipient_uuid or ""),
        wake_arg=TIMELINE,
        delivery_id=f"0192f3a4-5b6c-7d8e-9f01-{next(_ids):012x}",
        occurred_at=at,
        event_type="message.created",
    )


def _tempfail() -> WakeTempFailed:
    return WakeTempFailed("wake of nova exited 75: rate limited", exit_code=75)


class _Clock:
    """The wall clock the pipeline and the schedule share; a test moves it."""

    def __init__(self) -> None:
        self.at = START

    def __call__(self) -> datetime:
        return self.at


class _Timers:
    """The schedule's :data:`~basecradle_router.retry.Arm`: records each arming, fires by hand."""

    def __init__(self) -> None:
        self.armed: list[tuple[float, Callable[[], None], list[bool]]] = []

    def __call__(self, delay: float, fire: Callable[[], None]) -> Callable[[], None]:
        cancelled = [False]
        self.armed.append((delay, fire, cancelled))
        return lambda: cancelled.__setitem__(0, True)

    @property
    def live(self) -> list[float]:
        return [delay for delay, _, cancelled in self.armed if not cancelled[0]]

    def fire_next(self) -> None:
        for _delay, fire, cancelled in self.armed:
            if not cancelled[0]:
                cancelled[0] = True
                fire()
                return
        raise AssertionError("no retry is armed")


class _Waker:
    """Answers each wake from ``script`` in order (an exception is raised); records attempts."""

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, WakeAttempt | None]] = []

    def wake(
        self, agent: Agent, event: Event, attempt: WakeAttempt | None = None, *, quick_try: WakeTry
    ) -> WakeResult:
        self.calls.append((event.delivery_id, attempt))
        answer = self.script.pop(0) if self.script else WakeResult(0)
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, WakeResult)
        return answer


class _Lock:
    """A wake-lock guard that is held while ``held`` is set."""

    def __init__(self) -> None:
        self.held = False

    def check(self, agent: str, *, source: str | None = None) -> WakeLockDecision:
        if self.held:
            return WakeLockDecision(WakeLockState.HELD, detail="wake_lock_held: wake_hold")
        return WakeLockDecision(WakeLockState.ABSENT)


class _Rig:
    """The pipeline, its schedule and its queue, with every boundary in the test's hand."""

    def __init__(self, waker: _Waker, *, path: str | None = None) -> None:
        self.clock = _Clock()
        self.timers = _Timers()
        self.waker = waker
        self.lock = _Lock()
        self.results: list[PipelineResult] = []
        self.retries = self.schedule(path)
        self.pipeline = Pipeline(
            registry=RouteRegistry(),
            config=Config(
                agents=MappingProxyType({NOVA.key: NOVA}),
                enabled_routes=frozenset({"basecradle"}),
                webhook_secrets=MappingProxyType({}),
            ),
            waker=waker,  # type: ignore[arg-type]
            wake_lock=self.lock,  # type: ignore[arg-type]
            retries=self.retries,
            evidence=EvidenceStore(None),
            sleep=lambda _s: None,
            now=self.clock,
        )

    def schedule(self, path: str | None) -> RetrySchedule:
        schedule = RetrySchedule(
            path, resolve=lambda _e: NOVA, arm=self.timers, now=lambda: self.clock.at
        )
        schedule.start(self.dispatch)
        return schedule

    def dispatch(self, agent: Agent, event: Event) -> None:
        """What the scheduler does with a submit: run the slow half, here and now."""
        result = PipelineResult()
        self.results.append(result)
        self.pipeline.execute(agent, event, result)

    def deliver(self, event: Event) -> PipelineResult:
        self.dispatch(NOVA, event)
        return self.results[-1]

    def after(self, seconds: float) -> PipelineResult:
        """Let ``seconds`` pass, then fire the next armed retry."""
        self.clock.at += timedelta(seconds=seconds)
        before = len(self.results)
        self.timers.fire_next()
        assert len(self.results) == before + 1, "the retry was never dispatched"
        return self.results[-1]


def _wake_stage(result: PipelineResult) -> tuple[Stage, Outcome]:
    return result.stages[-1]


def _lines(caplog: pytest.LogCaptureFixture, head: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if head in r.getMessage()]


# --- the schedule ---------------------------------------------------------------------------


def test_a_temporary_failure_is_retried_at_about_1_5_and_30_minutes_then_given_up() -> None:
    rig = _Rig(_Waker(_tempfail(), _tempfail(), _tempfail(), _tempfail()))
    first = rig.deliver(_event())
    assert _wake_stage(first) == (Stage.WAKE, Outcome.DEFERRED)
    assert rig.timers.live == [60.0]

    second = rig.after(60)
    assert _wake_stage(second) == (Stage.WAKE, Outcome.DEFERRED)
    assert rig.timers.live == [300.0]
    third = rig.after(300)
    assert _wake_stage(third) == (Stage.WAKE, Outcome.DEFERRED)
    assert rig.timers.live == [1800.0]

    last = rig.after(1800)
    assert _wake_stage(last) == (Stage.WAKE, Outcome.FAILED)
    assert "attempts=4" in last.records[-1].detail
    assert rig.timers.live == []
    assert rig.retries.waiting() == []
    assert [attempt for _, attempt in rig.waker.calls] == [
        WakeAttempt(1, 4),
        WakeAttempt(2, 4),
        WakeAttempt(3, 4),
        WakeAttempt(4, 4),
    ]
    assert DEFAULT_DELAYS == (60.0, 300.0, 1800.0)


def test_a_temporary_failure_leaves_the_quick_retry_at_once() -> None:
    # Three tries in ten seconds is exactly what an outage outlasts: exit 75 is tried once.
    rig = _Rig(_Waker(_tempfail()))
    rig.deliver(_event())
    assert len(rig.waker.calls) == 1


def test_any_other_failure_keeps_its_three_quick_tries_and_its_failure() -> None:
    crash = WakeError("wake of nova exited 1: boom", exit_code=1)
    rig = _Rig(_Waker(crash, crash, crash))
    result = rig.deliver(_event())
    assert _wake_stage(result) == (Stage.WAKE, Outcome.FAILED)
    assert len(rig.waker.calls) == 3
    assert rig.retries.waiting() == [] and rig.timers.live == []


def test_the_retry_that_succeeds_ends_its_schedule_and_marks_its_delivery_done() -> None:
    rig = _Rig(_Waker(_tempfail(), WakeResult(0)))
    event = _event()
    rig.deliver(event)
    retried = rig.after(60)
    assert _wake_stage(retried) == (Stage.WAKE, Outcome.OK)
    assert rig.retries.waiting() == [] and rig.timers.live == []
    # Done means done: the same delivery again is a duplicate, never a second reply.
    again = rig.deliver(event)
    assert _wake_stage(again) == (Stage.DEDUP, Outcome.IGNORED)
    assert len(rig.waker.calls) == 2


def test_a_newer_deliverys_success_cancels_the_retries_left(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The waiting retry's work is done once a later wake on its timeline succeeds: the session
    # started after its event, so it read it. It is cancelled then, never run.
    caplog.set_level(logging.INFO, logger="basecradle_router")
    rig = _Rig(_Waker(_tempfail(), WakeResult(0)))
    waiting = _event(at=START)
    rig.deliver(waiting)
    rig.clock.at = START + timedelta(seconds=30)
    newer = _event(at=START + timedelta(seconds=20))
    assert _wake_stage(rig.deliver(newer)) == (Stage.WAKE, Outcome.OK)
    assert rig.retries.waiting() == [] and rig.timers.live == []
    assert len(rig.waker.calls) == 2
    (line,) = [m for m in _lines(caplog, "decision=coalesced") if waiting.delivery_id in m]
    assert f"into={newer.delivery_id}" in line


def test_a_success_cancels_only_what_it_read() -> None:
    # Another timeline's retry, and one whose event came after the session started, stand.
    rig = _Rig(_Waker(_tempfail(), _tempfail(), WakeResult(0)))
    other = Event(
        source="basecradle",
        kind=EventKind.PLATFORM_EVENT,
        recipient=Recipient(by="recipient_uuid", value=NOVA.recipient_uuid or ""),
        wake_arg="0192aaaa-bbbb-7ccc-8ddd-eeeeffff0002",
        delivery_id="0192f3a4-5b6c-7d8e-9f01-0000000000bb",
        occurred_at=START,
    )
    later = _event(at=START + timedelta(seconds=40))
    rig.deliver(other)
    rig.deliver(later)
    rig.clock.at = START + timedelta(seconds=30)
    rig.deliver(_event(at=START + timedelta(seconds=10)))
    assert sorted(d for _, d, _, _ in rig.retries.waiting()) == sorted(
        [other.delivery_id, later.delivery_id]
    )


def test_a_cancelled_retry_stays_cancelled_across_a_restart(tmp_path: Path) -> None:
    path = str(tmp_path / "wake-retries.json")
    rig = _Rig(_Waker(_tempfail(), WakeResult(0)), path=path)
    rig.deliver(_event(at=START))
    rig.clock.at = START + timedelta(seconds=30)
    rig.deliver(_event(at=START + timedelta(seconds=20)))
    rig.retries.stop()
    assert rig.schedule(path).waiting() == []


def test_a_duplicate_of_a_waiting_delivery_collapses_into_its_schedule() -> None:
    # Two Apps, one GUID: the duplicate never wakes into the same outage, and never restarts
    # the waiting delivery's schedule.
    rig = _Rig(_Waker(_tempfail(), WakeResult(0)))
    event = _event()
    rig.deliver(event)
    duplicate = rig.deliver(event)
    assert duplicate.stages[-1] == (Stage.DEDUP, Outcome.IGNORED)
    assert "reason=retry_pending" in duplicate.records[-1].detail
    assert len(rig.waker.calls) == 1
    assert [(n, at) for _, _, n, at in rig.retries.waiting()] == [(2, START + timedelta(minutes=1))]
    assert _wake_stage(rig.after(60)) == (Stage.WAKE, Outcome.OK)
    assert rig.waker.calls[-1][1] == WakeAttempt(2, 4)


def test_a_retry_that_could_not_be_submitted_is_armed_again_at_the_next_start() -> None:
    timers = _Timers()
    schedule = RetrySchedule(arm=timers, now=lambda: START)

    def broken(_agent: Agent, _event: Event) -> None:
        raise RuntimeError("cannot schedule new futures after shutdown")

    schedule.start(broken)
    event = _event()
    schedule.schedule(NOVA, event, WakeAttempt(1, 4))
    timers.fire_next()
    assert schedule.attempt(NOVA.harness_key, event) == WakeAttempt(1, 4)  # not due
    fired: list[Event] = []
    schedule.start(lambda _a, e: fired.append(e))
    timers.fire_next()
    assert fired == [event]


def test_each_delivery_has_its_own_schedule() -> None:
    rig = _Rig(_Waker(_tempfail(), _tempfail()))
    a, b = _event(), _event()
    rig.deliver(a)
    rig.deliver(b)
    assert [(d, n) for _, d, n, _ in rig.retries.waiting()] == [
        (a.delivery_id, 2),
        (b.delivery_id, 2),
    ]
    assert rig.timers.live == [60.0, 60.0]


def test_a_stopped_retry_ends_the_schedule() -> None:
    stop = WakeStopped("stopped by SIGTERM", signal="TERM", exit_code=-15)
    rig = _Rig(_Waker(_tempfail(), stop))
    rig.deliver(_event())
    stopped = rig.after(60)
    assert _wake_stage(stopped) == (Stage.WAKE, Outcome.STOPPED)
    assert rig.retries.waiting() == [] and rig.timers.live == []


def test_a_retry_crashing_ends_the_schedule_with_todays_failure() -> None:
    crash = WakeError("wake of nova exited 1: boom", exit_code=1)
    rig = _Rig(_Waker(_tempfail(), crash, crash, crash))
    rig.deliver(_event())
    failed = rig.after(60)
    assert _wake_stage(failed) == (Stage.WAKE, Outcome.FAILED)
    assert rig.retries.waiting() == [] and rig.timers.live == []


def test_under_the_wake_lock_a_retry_waits_and_is_never_dropped() -> None:
    rig = _Rig(_Waker(_tempfail(), WakeResult(0)))
    rig.deliver(_event())
    rig.lock.held = True
    held = rig.after(60)
    assert _wake_stage(held) == (Stage.WAKE_LOCK, Outcome.IGNORED)
    assert len(rig.waker.calls) == 1
    assert rig.timers.live == [60.0]  # attempt 2 waits its own step's wait again
    assert [n for _, _, n, _ in rig.retries.waiting()] == [2]

    rig.lock.held = False
    ran = rig.after(60)
    assert _wake_stage(ran) == (Stage.WAKE, Outcome.OK)
    assert rig.waker.calls[-1][1] == WakeAttempt(2, 4)


def test_a_later_attempt_waits_its_own_steps_wait_under_the_lock() -> None:
    rig = _Rig(_Waker(_tempfail(), _tempfail()))
    rig.deliver(_event())
    rig.after(60)
    rig.lock.held = True
    rig.after(300)
    assert rig.timers.live == [300.0]


def test_a_first_wake_under_the_lock_is_refused_as_it_always_was() -> None:
    rig = _Rig(_Waker())
    rig.lock.held = True
    result = rig.deliver(_event())
    assert _wake_stage(result) == (Stage.WAKE_LOCK, Outcome.IGNORED)
    assert rig.retries.waiting() == [] and rig.timers.live == []


def test_a_probe_is_never_retried_and_its_exit_75_is_still_a_failure() -> None:
    rig = _Rig(_Waker(_tempfail()))
    probe = Event(
        source="probe",
        kind=EventKind.SYNTHETIC_PROBE,
        recipient=Recipient(by="harness_key", value="nova"),
        wake_arg="BCNOC1 nonce " + "a" * 64,
        delivery_id="0192f3a4-5b6c-7d8e-9f01-0000000000ff",
    )
    result = rig.deliver(probe)
    assert _wake_stage(result) == (Stage.WAKE, Outcome.FAILED)
    assert rig.waker.calls == [(probe.delivery_id, None)]
    assert rig.retries.waiting() == []


# --- the lines ------------------------------------------------------------------------------


def test_each_scheduled_retry_is_one_line_naming_when(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="basecradle_router")
    rig = _Rig(_Waker(_tempfail()))
    event = _event()
    rig.deliver(event)
    (line,) = _lines(caplog, "event=wake_retry")
    assert f"attempt=1/4 delivery={event.delivery_id} agent=nova source=basecradle" in line
    assert "try=" not in line
    assert "delay=60.0s retry_at=2026-11-02T03:01:00Z" in line
    (end,) = _lines(caplog, "event=wake_end")
    assert "outcome=deferred" in end and "retry_at=2026-11-02T03:01:00Z" in end
    (start,) = _lines(caplog, "event=wake_start")
    assert start.endswith("attempt=1/4")


def test_the_retrys_bookend_names_its_attempt(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="basecradle_router")
    rig = _Rig(_Waker(_tempfail(), WakeResult(0)))
    rig.deliver(_event())
    rig.after(60)
    starts = _lines(caplog, "event=wake_start")
    assert [s.rsplit(" ", 1)[-1] for s in starts] == ["attempt=1/4", "attempt=2/4"]


# --- surviving a restart --------------------------------------------------------------------


def test_a_waiting_retry_survives_a_restart(tmp_path: Path) -> None:
    path = str(tmp_path / "wake-retries.json")
    rig = _Rig(_Waker(_tempfail(), WakeResult(0)), path=path)
    event = _event()
    rig.deliver(event)
    assert os.stat(path).st_mode & 0o777 == 0o600
    rig.retries.stop()

    # A new daemon: the same file, a fresh schedule, the wait already half over.
    rig.clock.at += timedelta(seconds=45)
    restarted = rig.schedule(path)
    assert restarted.waiting() == [(NOVA.key, event.delivery_id, 2, START + timedelta(minutes=1))]
    assert rig.timers.live == [15.0]


def test_a_restored_retry_already_due_fires_at_once(tmp_path: Path) -> None:
    path = str(tmp_path / "wake-retries.json")
    rig = _Rig(_Waker(_tempfail()), path=path)
    rig.deliver(_event())
    rig.retries.stop()
    rig.clock.at += timedelta(hours=1)
    rig.schedule(path)
    assert rig.timers.live == [0.0]


def test_a_restored_retry_runs_as_the_attempt_it_was(tmp_path: Path) -> None:
    path = str(tmp_path / "wake-retries.json")
    rig = _Rig(_Waker(_tempfail(), WakeResult(0)), path=path)
    rig.deliver(_event())
    rig.retries.stop()
    rig.retries = rig.schedule(path)
    object.__setattr__(rig.pipeline, "retries", rig.retries)
    ran = rig.after(60)
    assert _wake_stage(ran) == (Stage.WAKE, Outcome.OK)
    assert rig.waker.calls[-1][1] == WakeAttempt(2, 4)
    assert json.loads(Path(path).read_text())["retries"] == []


def test_the_file_round_trips_a_github_handoff(tmp_path: Path) -> None:
    path = str(tmp_path / "wake-retries.json")
    handoff = Event(
        source="github",
        kind=EventKind.HANDOFF,
        recipient=Recipient(by="repo", value="basecradle/basecradle-python"),
        wake_arg="Cross-repo handoff: work https://github.com/basecradle/basecradle-python/issues/42",
        delivery_id="0192f3a4-5b6c-7d8e-9f01-00000000aaaa",
        origin=IssueRef(
            repo="basecradle/basecradle-python",
            number=42,
            url="https://github.com/basecradle/basecradle-python/issues/42",
            title="Mirror the Wire Shape",
        ),
        occurred_at=START,
        event_type="issues",
    )
    first = RetrySchedule(path, arm=_Timers(), now=lambda: START)
    first.schedule(NOVA, handoff, WakeAttempt(1, 4))
    restored: list[Event] = []
    RetrySchedule(path, resolve=lambda e: restored.append(e) or NOVA, now=lambda: START)
    assert restored == [handoff]


def test_an_entry_whose_agent_is_gone_is_dropped_with_one_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = str(tmp_path / "wake-retries.json")
    RetrySchedule(path, arm=_Timers(), now=lambda: START).schedule(
        NOVA, _event(), WakeAttempt(1, 4)
    )
    restored = RetrySchedule(path, resolve=lambda _e: None, now=lambda: START)
    assert restored.waiting() == []
    assert len(_lines(caplog, "event=wake_retry_unrestored")) == 1
    assert "reason=unresolved" in _lines(caplog, "event=wake_retry_unrestored")[0]
    assert json.loads(Path(path).read_text())["retries"] == []


def test_an_unreadable_file_restores_nothing_and_never_raises(tmp_path: Path) -> None:
    path = tmp_path / "wake-retries.json"
    path.write_text("{not json")
    assert RetrySchedule(str(path), resolve=lambda _e: NOVA).waiting() == []
    path.write_text(json.dumps({"version": 99, "retries": []}))
    assert RetrySchedule(str(path), resolve=lambda _e: NOVA).waiting() == []


def test_construction_sweeps_only_its_own_orphaned_temps(tmp_path: Path) -> None:
    own = tmp_path / ".wake-retries-abc123.tmp"
    foreign = tmp_path / ".evidence-abc123.tmp"
    beside = tmp_path / "notes.tmp"
    for f in (own, foreign, beside):
        f.write_text("x")
    RetrySchedule(str(tmp_path / "wake-retries.json"))
    assert not own.exists()
    assert foreign.exists() and beside.exists()


def test_an_unwritable_file_degrades_to_memory_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = str(tmp_path / "missing-dir" / "wake-retries.json")
    schedule = RetrySchedule(path, arm=_Timers(), now=lambda: START)
    schedule.schedule(NOVA, _event(), WakeAttempt(1, 4))
    schedule.schedule(NOVA, _event(), WakeAttempt(1, 4))
    assert len(schedule.waiting()) == 2
    assert len(_lines(caplog, "event=wake_retries_write_failed")) == 1


def test_a_stopped_schedule_fires_nothing() -> None:
    timers = _Timers()
    fired: list[Event] = []
    schedule = RetrySchedule(arm=timers, now=lambda: START)
    schedule.start(lambda _a, e: fired.append(e))
    schedule.schedule(NOVA, _event(), WakeAttempt(1, 4))
    ((_, fire, _),) = timers.armed
    schedule.stop()
    fire()
    assert fired == []


# --- the attempt reaches the wake ----------------------------------------------------------


def test_the_attempt_rides_to_the_wake_runner_as_one_flag() -> None:
    seen: list[WakeInvocation] = []
    waker = HomeServerWaker(runner=lambda i: seen.append(i) or WakeResult(0))
    waker.wake(NOVA, _event(), WakeAttempt(2, 4), quick_try=FIRST_TRY)
    argv = seen[0].argv
    assert argv[argv.index("--attempt") + 1] == "2/4"
    assert argv.index("--attempt") < argv.index("--")


def test_the_attempt_reaches_a_subprocess_wakes_environment() -> None:
    seen: list[WakeInvocation] = []
    waker = SubprocessWaker(runner=lambda i: seen.append(i) or WakeResult(0))
    waker.wake(NOVA, _event(), WakeAttempt(3, 4), quick_try=FIRST_TRY)
    assert seen[0].env[ATTEMPT_ENV] == "3" and seen[0].env[ATTEMPTS_ENV] == "4"


def test_without_an_attempt_the_wake_carries_neither() -> None:
    seen: list[WakeInvocation] = []
    HomeServerWaker(runner=lambda i: seen.append(i) or WakeResult(0)).wake(
        NOVA, _event(), quick_try=FIRST_TRY
    )
    assert "--attempt" not in seen[0].argv


def test_exit_75_is_a_temporary_failure_and_still_a_wake_error() -> None:
    waker = SubprocessWaker(runner=lambda _i: WakeResult(75, stderr="rate limited"))
    with pytest.raises(WakeTempFailed) as raised:
        waker.wake(NOVA, _event(), quick_try=FIRST_TRY)
    assert isinstance(raised.value, WakeError)
    assert str(raised.value) == "wake of nova exited 75: rate limited"
    with pytest.raises(WakeError) as other:
        SubprocessWaker(runner=lambda _i: WakeResult(1, stderr="boom")).wake(
            NOVA, _event(), quick_try=FIRST_TRY
        )
    assert not isinstance(other.value, WakeTempFailed)


def test_an_attempt_past_its_last_is_refused() -> None:
    with pytest.raises(ValueError):
        WakeAttempt(5, 4)
    assert WakeAttempt(4, 4).last and not WakeAttempt(3, 4).last


# --- the daemon's wiring --------------------------------------------------------------------


def test_create_app_persists_the_schedule_where_the_env_says(tmp_path: Path) -> None:
    from basecradle_router.app import create_app

    registry = tmp_path / "agents.json"
    registry.write_text(
        json.dumps(
            {
                "nova": {
                    "os_user": "nova",
                    "clone_path": NOVA.clone_path,
                    "kind": "harness",
                    "recipient_uuid": NOVA.recipient_uuid,
                    "wake_bin": NOVA.wake_bin,
                }
            }
        )
    )
    path = tmp_path / "wake-retries.json"
    # A retry a previous daemon left: restored against the registry this one runs.
    RetrySchedule(str(path), arm=_Timers(), now=lambda: START).schedule(
        NOVA, _event(), WakeAttempt(1, 4)
    )
    server = create_app(
        {
            "BASECRADLE_ROUTER_AGENTS": str(registry),
            "BASECRADLE_ROUTER_ENABLED_ROUTES": "basecradle",
            "BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET": "bc_isk_" + "1" * 32,
            "BASECRADLE_ROUTER_EVIDENCE_FILE": "none",
            "BASECRADLE_ROUTER_RETRY_FILE": str(path),
        },
        waker=_Waker(),  # type: ignore[arg-type]
    )
    assert server.pipeline.retries.path == str(path)
    assert [agent for agent, *_ in server.pipeline.retries.waiting()] == ["nova"]


def test_a_retry_for_a_route_since_disabled_is_not_restored(tmp_path: Path) -> None:
    from basecradle_router.app import create_app

    registry = tmp_path / "agents.json"
    registry.write_text(
        json.dumps(
            {
                "basecradle/basecradle-python": {
                    "os_user": "nova",
                    "clone_path": "/home/nova/basecradle-python",
                    "bot_slug": "basecradle-python-ai",
                },
                "nova-harness": {
                    "os_user": "nova-harness",
                    "clone_path": NOVA.clone_path,
                    "kind": "harness",
                    "recipient_uuid": NOVA.recipient_uuid,
                    "wake_bin": NOVA.wake_bin,
                },
            }
        )
    )
    path = tmp_path / "wake-retries.json"
    harness = Agent(
        key="nova-harness",
        os_user="nova-harness",
        clone_path=NOVA.clone_path,
        wake_kind=WakeKind.HARNESS,
        recipient_uuid=NOVA.recipient_uuid,
        wake_bin=NOVA.wake_bin,
    )
    RetrySchedule(str(path), arm=_Timers(), now=lambda: START).schedule(
        harness, _event(), WakeAttempt(1, 4)
    )
    server = create_app(
        {
            "BASECRADLE_ROUTER_AGENTS": str(registry),
            "BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET": "fake-github-webhook-secret-" + "0" * 32,
            "BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS": "john",
            "BASECRADLE_ROUTER_EVIDENCE_FILE": "none",
            "BASECRADLE_ROUTER_RETRY_FILE": str(path),
        },
        waker=_Waker(),  # type: ignore[arg-type]
    )
    assert "basecradle" not in server.pipeline.registry
    assert server.pipeline.retries.waiting() == []


def test_a_retry_whose_time_came_goes_onto_its_agents_queue() -> None:
    import asyncio

    from basecradle_router.server import WebhookServer

    waker = _Waker(_tempfail(), WakeResult(0))
    timers = _Timers()
    pipeline = Pipeline(
        registry=RouteRegistry(),
        config=Config(
            agents=MappingProxyType({NOVA.key: NOVA}),
            enabled_routes=frozenset({"basecradle"}),
            webhook_secrets=MappingProxyType({}),
        ),
        waker=waker,  # type: ignore[arg-type]
        retries=RetrySchedule(arm=timers),
        sleep=lambda _s: None,
    )
    server = WebhookServer(pipeline, lanes=1)
    pipeline.retries.start(server._submit_retry)
    server._spawn_wake((NOVA, _event()), PipelineResult())
    asyncio.run(server.drain())
    timers.fire_next()
    asyncio.run(server.drain())
    assert [attempt for _, attempt in waker.calls] == [WakeAttempt(1, 4), WakeAttempt(2, 4)]
    assert pipeline.retries.waiting() == []

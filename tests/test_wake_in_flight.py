"""The in-flight marker: what is running, readable from outside the journal (#456).

Every other field in the evidence document moves only when a wake *ends*, so an ageing
``last_ok_at`` read from outside could not tell a long wake on a slow model from a
wedged lane (`@ernie`, 2026-11-11: a ten-minute wake read as stalled for half an hour).
The ``wake_started_*`` group says what is running. Its worth rests on one property more
than any other: **it is cleared on every way out of a wake**, because a marker a dead
wake left behind turns *slow* into *running forever*. So each exit path gets its own
test here — success, an exhausted failure, a harness watchdog's kill, an operator's
stop, an unexpected error, and a retry that re-stamps it — and each reads the marker
**from the file, during the wake**, the way a process that is not the daemon reads it.
The daemon-restart path is the store's own (``test_evidence.py``). No network, model, or
live agent. Test cast: Nova Digital (``nova``, AI).
"""

from datetime import datetime, timedelta, timezone
from types import MappingProxyType

from basecradle_router.config import Config
from basecradle_router.evidence import EvidenceStore, read_evidence
from basecradle_router.models import Agent, Event, EventKind, IssueRef, Recipient
from basecradle_router.pipeline import Outcome, Pipeline, PipelineResult, Stage
from basecradle_router.routes import RouteRegistry
from basecradle_router.routes.github import GithubRoute
from basecradle_router.secret import Secret
from basecradle_router.wake import WakeError, WakeResult, WakeStopped
from basecradle_router.wakelock import WakeLockGuard

NOVA = Agent(
    key="basecradle/basecradle-python",
    os_user="nova",
    clone_path="/home/nova/basecradle-python",
    bot_slug="basecradle-python-ai",
)
SECRET = "fake-github-webhook-secret-" + "0" * 32  # correctly-shaped fake
DELIVERY = "0192f3a4-5b6c-7d8e-9f01-00000000000a"
PROBE_DELIVERY = "dd5443c7af3a475abb31af8a1b07e4f7"
ISSUE_URL = "https://github.com/basecradle/basecradle-python/issues/42"
LAUNCHED = datetime(2026, 11, 11, 14, 0, 0, tzinfo=timezone.utc)

#: The group, as the store spells it — every field one clear must reset together.
GROUP = (
    "wake_started_at",
    "wake_started_delivery",
    "wake_started_route",
    "wake_started_synthetic",
)


def _group(wake) -> tuple:
    return tuple(getattr(wake, name) for name in GROUP)


CLEARED = (None, None, None, None)


class _Instants:
    """The pipeline's wall clock: one fixed minute later per reading."""

    def __init__(self) -> None:
        self.readings = 0

    def __call__(self) -> datetime:
        moment = LAUNCHED + timedelta(minutes=self.readings)
        self.readings += 1
        return moment


class _ReadingWaker:
    """A wake that reads the evidence *file* while it runs, then ends as ``outcomes`` say.

    Each entry of ``outcomes`` is one attempt: ``None`` succeeds, an exception is raised.
    What the file said during each attempt is kept in ``seen`` — read with
    :func:`read_evidence`, the out-of-process read side, never the daemon's memory.
    """

    def __init__(self, path: str, *outcomes: BaseException | None) -> None:
        self.path = path
        self.outcomes = list(outcomes) or [None]
        self.seen: list[tuple] = []

    def wake(self, agent: Agent, event: Event) -> WakeResult:
        self.seen.append(_group(read_evidence(self.path).agent_wakes[agent.harness_key]))
        outcome = self.outcomes[len(self.seen) - 1]
        if outcome is not None:
            raise outcome
        return WakeResult(exit_code=0, stdout="woke")


def _store(tmp_path) -> tuple[EvidenceStore, str]:
    path = str(tmp_path / "evidence.json")
    return EvidenceStore(path), path


def _pipeline(tmp_path, evidence: EvidenceStore, waker: _ReadingWaker) -> Pipeline:
    registry = RouteRegistry()
    registry.register(GithubRoute(["drawkkwast"]))
    return Pipeline(
        registry=registry,
        config=Config(
            agents=MappingProxyType({NOVA.key: NOVA}),
            enabled_routes=frozenset({"github"}),
            webhook_secrets=MappingProxyType({"github": Secret(SECRET)}),
        ),
        waker=waker,
        evidence=evidence,
        wake_lock=WakeLockGuard(lock_dir=str(tmp_path / "wake-locks")),
        sleep=lambda _d: None,
        now=_Instants(),
    )


def _event(occurred_at: datetime | None = None) -> Event:
    return Event(
        source="github",
        kind=EventKind.HANDOFF,
        recipient=Recipient(by="repo", value=NOVA.key),
        wake_arg=f"Cross-repo handoff: work {ISSUE_URL}",
        delivery_id=DELIVERY,
        origin=IssueRef(repo=NOVA.key, number=42, url=ISSUE_URL, title="Do the thing"),
        occurred_at=occurred_at,
    )


def _probe_event() -> Event:
    return Event(
        source="probe",
        kind=EventKind.SYNTHETIC_PROBE,
        recipient=Recipient(by="harness_key", value=NOVA.harness_key),
        wake_arg="BCNOC1 " + "0" * 32 + " " + "a" * 64,
        delivery_id=PROBE_DELIVERY,
    )


def _run(tmp_path, *outcomes, event: Event | None = None):
    evidence, path = _store(tmp_path)
    waker = _ReadingWaker(path, *outcomes)
    result = PipelineResult()
    _pipeline(tmp_path, evidence, waker).execute(NOVA, event or _event(), result)
    return waker, result, read_evidence(path).agent_wakes[NOVA.harness_key]


RUNNING = ("2026-11-11T14:00:00+00:00", DELIVERY, "github", False)


def test_a_successful_wake_is_marked_while_it_runs_and_cleared_with_its_ok(tmp_path) -> None:
    waker, result, after = _run(tmp_path)

    assert waker.seen == [RUNNING]
    assert result.stages[-1] == (Stage.WAKE, Outcome.OK)
    assert after.ok == 1
    assert _group(after) == CLEARED


def test_an_exhausted_wake_is_cleared_with_its_failure(tmp_path) -> None:
    blip = WakeError("claude exited 1", exit_code=1)
    waker, result, after = _run(tmp_path, blip, blip, blip)

    assert len(waker.seen) == 3  # marked throughout every attempt
    assert result.stages[-1] == (Stage.WAKE, Outcome.FAILED)
    assert (after.ok, after.failed) == (0, 1)
    assert _group(after) == CLEARED


def test_a_watchdog_kill_inside_the_wake_is_a_failure_and_is_cleared(tmp_path) -> None:
    # The router sets no wake deadline (#389); a wake that runs out of time is ended by
    # the harness's own watchdog inside it, which surfaces as runuser's 128+N — a crash
    # (wake.STOP_SIGNALS), so it takes the failure path and must clear on it.
    killed = WakeError("claude exited 143", exit_code=143)
    _waker, result, after = _run(tmp_path, killed, killed, killed)

    assert result.stages[-1] == (Stage.WAKE, Outcome.FAILED)
    assert _group(after) == CLEARED


def test_a_retry_re_stamps_the_marker_with_its_own_launch(tmp_path) -> None:
    blip = WakeError("transient blip", exit_code=2)
    waker, result, after = _run(tmp_path, blip, None)

    first, second = waker.seen
    assert first[0] == "2026-11-11T14:00:00+00:00"
    assert second[0] == "2026-11-11T14:01:00+00:00"  # the attempt now running, not the first
    assert second[1:] == RUNNING[1:]
    assert result.stages[-1] == (Stage.WAKE, Outcome.OK)
    assert _group(after) == CLEARED


def test_a_stopped_wake_records_no_outcome_and_is_still_cleared(tmp_path) -> None:
    stop = WakeStopped("wake of nova stopped by SIGTERM", signal="TERM", exit_code=-15)
    waker, result, after = _run(tmp_path, stop)

    assert waker.seen == [RUNNING]
    assert result.stages[-1] == (Stage.WAKE, Outcome.STOPPED)
    # A stop is no outcome (#386) — and still no longer running.
    assert (after.ok, after.failed, after.refused, after.deduped) == (0, 0, 0, 0)
    assert _group(after) == CLEARED


def test_an_unexpected_error_on_the_way_out_is_still_cleared(tmp_path) -> None:
    waker, result, after = _run(tmp_path, RuntimeError("a waker bug"))

    assert waker.seen == [RUNNING]
    assert result.stages[-1] == (Stage.WAKE, Outcome.FAILED)  # execute's last resort
    assert _group(after) == CLEARED


def test_a_probe_in_flight_is_marked_synthetic(tmp_path) -> None:
    waker, _result, after = _run(tmp_path, event=_probe_event())

    assert waker.seen == [("2026-11-11T14:00:00+00:00", PROBE_DELIVERY, "probe", True)]
    assert _group(after) == CLEARED


def test_the_marker_is_drawn_at_the_instant_coverage_is(tmp_path) -> None:
    # One instant, two consumers: the coalesce gate's "this session began reading at"
    # and the document's "this wake launched at" must never disagree.
    evidence, path = _store(tmp_path)
    waker = _ReadingWaker(path)
    pipeline = _pipeline(tmp_path, evidence, waker)
    earlier = LAUNCHED - timedelta(minutes=5)
    pipeline.execute(NOVA, _event(occurred_at=earlier), PipelineResult())

    covered = pipeline.coverage.covering(NOVA.harness_key, _event(occurred_at=earlier))
    assert covered is not None
    assert covered.started_at.isoformat() == waker.seen[0][0] == RUNNING[0]

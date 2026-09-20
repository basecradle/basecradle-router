"""One handoff, one wake — coalescing and the dispatch-time recheck (basecradle-router#272).

basecradle/basecradle#548 measured it: one handoff produced **five** full agent sessions
in 23 minutes, because every accepted delivery became its own queued wake. These pin the
fix end to end through the real github route and the real pipeline — every collaborator
at the boundary mocked, nothing on the network — with payloads shaped the way GitHub
sends them:

- deliveries that queued behind a wake collapse into **at most one** follow-up;
- a queued delivery whose issue closed (or lost ``handoff``, or gained ``Do Not Work``)
  while it waited launches nothing;
- the ``opened`` and ``labeled`` of one create wake the agent **once**, for both ways a
  create is labeled, in either arrival order;
- the platform route's semantics are untouched.

The queue is driven the way :class:`~basecradle_router.scheduler.WakeScheduler` drives it
for one agent: every delivery is *accepted* the moment it arrives, and the pending ones
are *executed* in arrival order once the wake ahead of them has run. Test cast: John Doe
(``john``) hands off; Nova Digital's builder (``nova``) is woken. All data is fabricated.
"""

import hashlib
import hmac
import itertools
import json
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from basecradle_router.coalesce import WakeCoverage
from basecradle_router.config import Config
from basecradle_router.evidence import EvidenceStore
from basecradle_router.models import Agent, Event, EventKind, IssueRef, Recipient, WakeKind
from basecradle_router.pipeline import Outcome, Pipeline, PipelineResult, Stage
from basecradle_router.routes import BasecradleRoute, InboundRequest, RouteRegistry
from basecradle_router.routes.github import GithubRoute
from basecradle_router.wake import WakeError, WakeResult

SECRET = "whsec_" + "0" * 32
BASECRADLE_SECRET = "bc_isk_" + "1" * 32
JOHN = "john"  # John Doe, a trusted human org member — files the handoff, comments
AGENT_BOT = "basecradle-python-ai[bot]"  # Nova Digital's builder bot — closes the issue
REPO = "basecradle/basecradle-python"
ISSUE = f"https://github.com/{REPO}/issues/42"
NOVA = Agent(
    key=REPO,
    os_user="nova",
    clone_path="/home/nova/basecradle-python",
    bot_slug="basecradle-python-ai",
)
JT = Agent(
    key="jt",
    os_user="jt",
    clone_path="/home/jt/harness",
    wake_kind=WakeKind.HARNESS,
    recipient_uuid="019e916c-7f45-700e-afc0-f45557b237b7",
    wake_bin="/home/jt/venv/bin/basecradle-harness-wake",
)

_ids = itertools.count(1)


def _uuid7() -> str:
    """A fresh, well-formed UUIDv7 — one per delivery, as ``X-GitHub-Delivery`` is."""
    return f"0192f3a4-5b6c-7d8e-9f01-{next(_ids):012x}"


def _at(clock: str) -> str:
    """A GitHub timestamp on the incident's day: whole seconds, ``Z``-suffixed."""
    return f"2026-09-19T{clock}Z"


def _moment(clock: str) -> datetime:
    """The router's own clock reading at ``clock`` (``HH:MM:SS.ffffff``), UTC."""
    return datetime.fromisoformat(f"2026-09-19T{clock}+00:00")


# --- doubles ----------------------------------------------------------------


class _Waker:
    """Records every wake; fails the first ``fail`` calls, as a broken wake path would."""

    def __init__(self, fail: int = 0) -> None:
        self.fail = fail
        self.calls: list[Event] = []

    def wake(self, agent: Agent, event: Event) -> WakeResult:
        self.calls.append(event)
        if len(self.calls) <= self.fail:
            raise WakeError("wake path down", exit_code=1)
        return WakeResult(exit_code=0)

    @property
    def deliveries(self) -> list[str]:
        return [event.delivery_id for event in self.calls]


class _WallClock:
    """The pipeline's ``now``: a test sets the instant each wake launches at."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def _pipeline(
    *, waker: _Waker | None = None, now: _WallClock | None = None, evidence=None, attempts=3
) -> tuple[Pipeline, _Waker, _WallClock]:
    waker = waker or _Waker()
    now = now or _WallClock(_moment("03:14:08.415000"))
    registry = RouteRegistry()
    registry.register(GithubRoute(frozenset({JOHN})))
    registry.register(BasecradleRoute())
    config = Config(
        agents=MappingProxyType({NOVA.key: NOVA, JT.key: JT}),
        enabled_routes=frozenset({"github", "basecradle"}),
        webhook_secrets=MappingProxyType({"github": SECRET, "basecradle": BASECRADLE_SECRET}),
    )
    pipeline = Pipeline(
        registry=registry,
        config=config,
        waker=waker,
        evidence=evidence if evidence is not None else EvidenceStore(None),
        wake_attempts=attempts,
        sleep=lambda _seconds: None,
        now=now,
    )
    return pipeline, waker, now


# --- GitHub-shaped payloads ---------------------------------------------------
#
# The fields GitHub sends on the issue object that this path reads — `state`, `labels`,
# `created_at`, `updated_at` — alongside the ones it has always read, in the shapes it
# sends them (label objects, `Z` timestamps, a `closed_at` beside a closed state).


def _issue(
    *,
    state: str = "open",
    labels: tuple[str, ...] = ("handoff",),
    created: str = "03:14:07",
    updated: str = "03:14:07",
) -> dict:
    return {
        "url": f"https://api.github.com/repos/{REPO}/issues/42",
        "html_url": ISSUE,
        "number": 42,
        "title": "Mirror the wire-shape change",
        "user": {"login": JOHN, "type": "User"},
        "labels": [
            {"id": 7000 + index, "name": name, "color": "0e8a16", "default": False}
            for index, name in enumerate(labels)
        ],
        "state": state,
        "comments": 0,
        "created_at": _at(created),
        "updated_at": _at(updated),
        "closed_at": _at(updated) if state == "closed" else None,
        "body": "Mirror the wire-shape change in the SDK.",
    }


def _signed(event: str, payload: dict, delivery: str | None = None) -> InboundRequest:
    body = json.dumps(payload).encode("utf-8")
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return InboundRequest(
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery or _uuid7(),
            "X-Hub-Signature-256": f"sha256={digest}",
        },
        body=body,
    )


def _issues(action: str, issue: dict, *, sender: str = JOHN, label: str | None = None):
    payload = {
        "action": action,
        "issue": issue,
        "repository": {"full_name": REPO, "name": "basecradle-python"},
        "sender": {"login": sender, "type": "Bot" if sender.endswith("[bot]") else "User"},
    }
    if label is not None:
        payload["label"] = {"id": 7000, "name": label, "color": "0e8a16", "default": False}
    return _signed("issues", payload)


def _comment(issue: dict, *, at: str, sender: str = JOHN):
    payload = {
        "action": "created",
        "issue": issue,
        "comment": {
            "id": next(_ids),
            "user": {"login": sender, "type": "User"},
            "created_at": _at(at),
            "updated_at": _at(at),
            "body": "One more thing for the handoff.",
        },
        "repository": {"full_name": REPO, "name": "basecradle-python"},
        "sender": {"login": sender, "type": "User"},
    }
    return _signed("issue_comment", payload)


# --- driving the queue --------------------------------------------------------


class _Queue:
    """One agent's FIFO, driven as the scheduler drives it: accept on arrival, run in order."""

    def __init__(self, pipeline: Pipeline) -> None:
        self.pipeline = pipeline
        self.pending: list[tuple[Agent, Event, PipelineResult]] = []
        self.results: dict[str, PipelineResult] = {}

    def arrive(self, request: InboundRequest) -> str | None:
        """Accept a delivery the moment it arrives; queue it if it asks for a wake."""
        accepted = self.pipeline.accept("github", request)
        if accepted.pending is None:
            return None
        agent, event = accepted.pending
        self.pending.append((agent, event, accepted.result))
        self.results[event.delivery_id] = accepted.result
        return event.delivery_id

    def run_next(self) -> PipelineResult:
        agent, event, result = self.pending.pop(0)
        self.pipeline.execute(agent, event, result)
        return result

    def drain(self) -> None:
        while self.pending:
            self.run_next()

    def fate(self, delivery: str) -> tuple[Stage, Outcome]:
        return self.results[delivery].stages[-1]


WOKE = (Stage.WAKE, Outcome.OK)
COALESCED = (Stage.COALESCE, Outcome.IGNORED)
DROPPED = (Stage.RECHECK, Outcome.IGNORED)


# --- the incident: basecradle-ruby#149's five deliveries ---------------------
#
# The journal of 2026-09-19 (basecradle#548), replayed with fabricated ids: a labeled
# create (`opened` + `labeled`), a capital comment while the agent works, the agent
# closing the issue, a reopen comment, the capital removing and re-applying `handoff`,
# and the agent closing it again — every one of them arriving while the first wake runs.


def _the_five_deliveries(queue: _Queue, *, final_close: bool) -> dict[str, str]:
    ids = {}
    ids["opened"] = queue.arrive(_issues("opened", _issue()))
    queue.run_next()  # wake A launches at 03:14:08.415 and runs through the rest
    ids["labeled"] = queue.arrive(_issues("labeled", _issue(), label="handoff"))
    ids["comment"] = queue.arrive(_comment(_issue(updated="03:22:03"), at="03:22:03"))
    queue.arrive(_issues("closed", _issue(state="closed", updated="03:24:36"), sender=AGENT_BOT))
    reopened = _issue(updated="03:26:05")
    ids["reopen_comment"] = queue.arrive(_comment(reopened, at="03:26:05"))
    queue.arrive(_issues("reopened", reopened))
    queue.arrive(_issues("unlabeled", _issue(labels=(), updated="03:26:07"), label="handoff"))
    ids["relabeled"] = queue.arrive(_issues("labeled", _issue(updated="03:26:08"), label="handoff"))
    if final_close:
        closed = _issue(state="closed", updated="03:31:03")
        queue.arrive(_issues("closed", closed, sender=AGENT_BOT))
    return ids


def test_the_five_deliveries_cost_one_wake_when_the_issue_ends_closed() -> None:
    pipeline, waker, _ = _pipeline()
    queue = _Queue(pipeline)
    ids = _the_five_deliveries(queue, final_close=True)

    # Five deliveries asked for a wake; the router accepted every one of them.
    assert all(ids.values())
    queue.drain()

    assert waker.deliveries == [ids["opened"]]  # one session, not five
    # The `labeled` of the same create happened before wake A started: A read it.
    assert queue.fate(ids["labeled"]) == COALESCED
    # Everything after A started is for an issue that is now closed: nothing launches.
    for name in ("comment", "reopen_comment", "relabeled"):
        assert queue.fate(ids[name]) == DROPPED, name


def test_the_five_deliveries_cost_one_follow_up_when_the_issue_stays_open() -> None:
    # A handoff stamped `CLOSER: capital` stays open after the agent reports. Deliveries
    # during the first session then earn exactly ONE follow-up — the running session
    # may have missed them — and everything queued behind that follow-up is read by it.
    pipeline, waker, now = _pipeline()
    queue = _Queue(pipeline)
    ids = _the_five_deliveries(queue, final_close=False)

    now.at = _moment("03:40:00.000000")  # A finished; the follow-up launches now
    queue.drain()

    assert waker.deliveries == [ids["opened"], ids["comment"]]
    assert queue.fate(ids["labeled"]) == COALESCED
    assert queue.fate(ids["comment"]) == WOKE
    assert queue.fate(ids["reopen_comment"]) == COALESCED
    assert queue.fate(ids["relabeled"]) == COALESCED


# --- ask 3: one create, one wake — both ways a create is labeled ---------------


@pytest.mark.parametrize("labeled_first", [False, True], ids=["opened-first", "labeled-first"])
def test_a_labeled_create_wakes_once_in_either_arrival_order(labeled_first: bool) -> None:
    # `gh issue create --label handoff`: GitHub sends `opened` (the label already in the
    # payload) and `labeled`, a fraction of a second apart and in no promised order.
    pipeline, waker, _ = _pipeline()
    queue = _Queue(pipeline)
    opened = _issues("opened", _issue())
    labeled = _issues("labeled", _issue(), label="handoff")
    first, second = (labeled, opened) if labeled_first else (opened, labeled)

    woke = queue.arrive(first)
    collapsed = queue.arrive(second)
    queue.drain()

    assert waker.deliveries == [woke]
    assert queue.fate(woke) == WOKE
    assert queue.fate(collapsed) == COALESCED


def test_an_unlabeled_create_then_label_wakes_once_when_opened_carries_the_label() -> None:
    # basecradle-ruby#151's shape: created unlabeled at 03:42:03, `handoff` added ~1 s
    # later as a separate call. The journal shows BOTH deliveries woke, and the router
    # reads nothing from GitHub — so the `opened` payload itself carried `handoff`,
    # rendered after the label landed (both delivery GUIDs are minted at 03:42:04.1-.5,
    # after the 03:42:04 label). Its `created_at` is still the creation, though, and the
    # `labeled` one's update is the label — both before the wake that reads them.
    pipeline, waker, now = _pipeline(now=_WallClock(_moment("03:42:05.156000")))
    queue = _Queue(pipeline)
    late_render = _issue(created="03:42:03", updated="03:42:04")

    woke = queue.arrive(_issues("opened", late_render))
    collapsed = queue.arrive(_issues("labeled", late_render, label="handoff"))
    queue.drain()

    assert waker.deliveries == [woke]
    assert queue.fate(collapsed) == COALESCED


def test_an_unlabeled_create_then_label_wakes_once_when_opened_does_not() -> None:
    # The same shape rendered promptly: `opened` carries no label, so it is no handoff
    # at all and the `labeled` alone wakes — one wake either way.
    pipeline, waker, _ = _pipeline(now=_WallClock(_moment("03:42:05.156000")))
    queue = _Queue(pipeline)

    assert queue.arrive(_issues("opened", _issue(labels=(), created="03:42:03"))) is None
    woke = queue.arrive(
        _issues("labeled", _issue(created="03:42:03", updated="03:42:04"), label="handoff")
    )
    queue.drain()

    assert waker.deliveries == [woke]


def test_two_apps_delivering_every_event_twice_still_cost_one_wake() -> None:
    # The live journal shows every delivery arriving twice under one GUID (two fleet
    # Apps on the repo, #133). The delivery dedup keeps collapsing the copy of the wake
    # that ran; the coalesce collapses both copies of the `labeled` it read.
    pipeline, waker, _ = _pipeline()
    queue = _Queue(pipeline)
    opened_id, labeled_id = _uuid7(), _uuid7()
    opened = _signed("issues", json.loads(_issues("opened", _issue()).body), opened_id)
    labeled = json.loads(_issues("labeled", _issue(), label="handoff").body)
    for request in (
        opened,
        _signed("issues", json.loads(opened.body), opened_id),
        _signed("issues", labeled, labeled_id),
        _signed("issues", labeled, labeled_id),
    ):
        queue.arrive(request)
    fates = []
    while queue.pending:
        fates.append(queue.run_next().stages[-1])

    assert waker.deliveries == [opened_id]
    assert fates == [WOKE, (Stage.DEDUP, Outcome.IGNORED), COALESCED, COALESCED]


# --- the recheck: a session's life is its issue's life -------------------------


def test_a_wake_queued_behind_another_is_dropped_when_its_issue_closes_first() -> None:
    pipeline, waker, _ = _pipeline()
    queue = _Queue(pipeline)
    queued = queue.arrive(_comment(_issue(updated="03:20:00"), at="03:20:00"))
    queue.arrive(_issues("closed", _issue(state="closed", updated="03:21:00"), sender=AGENT_BOT))
    queue.drain()

    assert waker.calls == []
    assert queue.fate(queued) == DROPPED


def test_a_label_applied_to_an_already_closed_issue_launches_nothing() -> None:
    # The fifth wake of the incident: `handoff` re-applied, the payload itself saying
    # closed. Nothing queued ahead of it — the recheck runs at dispatch regardless.
    pipeline, waker, _ = _pipeline()
    queue = _Queue(pipeline)
    closed = _issue(state="closed", updated="03:36:19")
    delivery = queue.arrive(_issues("labeled", closed, label="handoff"))
    queue.drain()

    assert waker.calls == []
    assert queue.fate(delivery) == DROPPED


def test_a_reopened_issue_wakes_again() -> None:
    pipeline, waker, _ = _pipeline(now=_WallClock(_moment("03:40:00.000000")))
    queue = _Queue(pipeline)
    queue.arrive(_issues("closed", _issue(state="closed", updated="03:24:36"), sender=AGENT_BOT))
    queue.arrive(_issues("reopened", _issue(updated="03:26:05")))
    delivery = queue.arrive(_comment(_issue(updated="03:26:06"), at="03:26:06"))
    queue.drain()

    assert waker.deliveries == [delivery]


@pytest.mark.parametrize(
    ("labels", "reason"),
    [((), "handoff_removed"), (("handoff", "Do Not Work"), "do_not_work")],
    ids=["handoff-removed", "do-not-work"],
)
def test_a_queued_wake_honours_a_label_change_made_while_it_waited(labels, reason, caplog):
    pipeline, waker, _ = _pipeline()
    queue = _Queue(pipeline)
    delivery = queue.arrive(_comment(_issue(updated="03:20:00"), at="03:20:00"))
    action = "unlabeled" if not labels else "labeled"
    label = "handoff" if not labels else "Do Not Work"
    queue.arrive(_issues(action, _issue(labels=labels, updated="03:21:00"), label=label))
    with caplog.at_level("INFO", logger="basecradle_router"):
        queue.drain()

    assert waker.calls == []
    assert queue.fate(delivery) == DROPPED
    assert f"reason={reason}" in queue.results[delivery].records[-1].detail


def test_an_older_report_arriving_late_never_rolls_the_issue_back() -> None:
    # GitHub promises no delivery order. A `closed` report older than the `reopened`
    # one already seen must not re-close the issue in the ledger.
    pipeline, waker, _ = _pipeline(now=_WallClock(_moment("03:40:00.000000")))
    queue = _Queue(pipeline)
    queue.arrive(_issues("reopened", _issue(updated="03:26:05")))
    queue.arrive(_issues("closed", _issue(state="closed", updated="03:24:36"), sender=AGENT_BOT))
    delivery = queue.arrive(_comment(_issue(updated="03:26:06"), at="03:26:06"))
    queue.drain()

    assert waker.deliveries == [delivery]


# --- coverage is earned by a success, and only a success ------------------------


def test_a_failed_wake_covers_nothing() -> None:
    # The dedup's resilience, kept: a delivery behind a wake that FAILED keeps its own
    # chance to wake the agent — the collapse is never allowed to lose the work.
    pipeline, waker, _ = _pipeline(waker=_Waker(fail=3))
    queue = _Queue(pipeline)
    first = queue.arrive(_issues("opened", _issue()))
    second = queue.arrive(_issues("labeled", _issue(), label="handoff"))
    queue.drain()

    assert queue.fate(first) == (Stage.WAKE, Outcome.FAILED)
    assert queue.fate(second) == WOKE
    assert waker.deliveries[-1] == second


def test_an_event_after_the_wake_started_is_not_covered_by_it() -> None:
    pipeline, waker, now = _pipeline()
    queue = _Queue(pipeline)
    queue.arrive(_issues("opened", _issue()))
    queue.run_next()  # launched at 03:14:08.415
    late = queue.arrive(_comment(_issue(updated="03:14:09"), at="03:14:09"))
    now.at = _moment("03:20:00.000000")
    queue.drain()

    assert queue.fate(late) == WOKE
    assert len(waker.calls) == 2


def test_an_event_without_a_time_is_never_coalesced() -> None:
    # The opt-in: an event a source did not stamp keeps one-delivery-one-wake.
    pipeline, waker, _ = _pipeline()
    queue = _Queue(pipeline)
    timeless = _issue()
    del timeless["created_at"], timeless["updated_at"]
    for _ in range(2):
        queue.arrive(_issues("opened", timeless))
    queue.drain()

    assert len(waker.calls) == 2


# --- the accounting: every delivery's fate reads off its delivery id -----------


def _decisions(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "event=delivery_decision" in r.getMessage()]


def test_a_coalesced_delivery_gets_its_own_decision_line_naming_the_wake(caplog) -> None:
    pipeline, _, _ = _pipeline()
    queue = _Queue(pipeline)
    with caplog.at_level("INFO", logger="basecradle_router"):
        woke = queue.arrive(_issues("opened", _issue()))
        collapsed = queue.arrive(_issues("labeled", _issue(), label="handoff"))
        queue.drain()

    mine = [line for line in _decisions(caplog) if f"delivery={collapsed}" in line]
    assert mine == [
        f"event=delivery_decision source=github event_type=issues decision=woke "
        f"recipient={REPO} delivery={collapsed}",
        f"event=delivery_decision source=github event_type=issues decision=coalesced "
        f"recipient={REPO} delivery={collapsed} into={woke}",
    ]
    stage = queue.results[collapsed].records[-1].detail
    assert f"into={woke}" in stage
    assert "occurred=2026-09-19T03:14:07Z" in stage
    assert "wake_started=2026-09-19T03:14:08.415000Z" in stage


def test_a_dropped_delivery_gets_its_own_decision_line_with_the_reason(caplog) -> None:
    pipeline, _, _ = _pipeline()
    queue = _Queue(pipeline)
    with caplog.at_level("INFO", logger="basecradle_router"):
        delivery = queue.arrive(_comment(_issue(updated="03:20:00"), at="03:20:00"))
        closed = _issue(state="closed", updated="03:21:00")
        queue.arrive(_issues("closed", closed, sender=AGENT_BOT))
        queue.drain()

    assert [line for line in _decisions(caplog) if f"delivery={delivery}" in line][-1] == (
        f"event=delivery_decision source=github event_type=issue_comment decision=dropped "
        f"recipient={REPO} delivery={delivery} reason=issue_closed"
    )


def test_a_coalesce_is_evidence_of_a_success_and_a_drop_is_no_evidence(tmp_path) -> None:
    # A coalesce is reachable only through a successful wake, exactly like a dedup, so
    # it is counted as one (#218) — never as a refusal. A drop says nothing about the
    # edge at all: the work ended, and the agent was never gated.
    evidence = EvidenceStore(str(tmp_path / "evidence.json"))
    pipeline, _, _ = _pipeline(evidence=evidence)
    queue = _Queue(pipeline)
    queue.arrive(_issues("opened", _issue()))
    queue.run_next()  # the one wake — before the issue closes
    queue.arrive(_issues("labeled", _issue(), label="handoff"))
    queue.arrive(_comment(_issue(updated="03:20:00"), at="03:20:00"))
    queue.arrive(_issues("closed", _issue(state="closed", updated="03:21:00"), sender=AGENT_BOT))
    queue.drain()

    wake = evidence.snapshot().agent_wakes["nova"]
    assert (wake.ok, wake.deduped, wake.refused, wake.failed) == (1, 1, 0, 0)


# --- ask 5: the platform route is untouched ------------------------------------


def _platform_event(delivery: str) -> Event:
    """A basecradle timeline delivery as its route normalizes one: no event time."""
    return Event(
        source="basecradle",
        kind=EventKind.PLATFORM_EVENT,
        recipient=Recipient(by="recipient_uuid", value=JT.recipient_uuid),
        wake_arg="0192aaaa-bbbb-7ccc-8ddd-eeeeffff0000",
        delivery_id=delivery,
    )


def test_platform_wakes_are_not_coalesced_or_rechecked() -> None:
    # The shared queue is the same, but neither new gate opens for a route that stamps
    # no event time and answers no recheck: two messages on one timeline, two wakes.
    pipeline, waker, _ = _pipeline()
    for delivery in ("evt_0192f3a4000000000001", "evt_0192f3a4000000000002"):
        result = PipelineResult()
        pipeline.execute(JT, _platform_event(delivery), result)
        assert result.stages[-1] == WOKE
    assert len(waker.calls) == 2


# --- WakeCoverage, the unit ------------------------------------------------------


def _event(occurred: datetime | None, delivery: str = "0192f3a4-5b6c-7d8e-9f01-00000000c0de"):
    return Event(
        source="github",
        kind=EventKind.HANDOFF,
        recipient=Recipient(by="repo", value=REPO),
        wake_arg=f"Cross-repo handoff: work {ISSUE}",
        delivery_id=delivery,
        occurred_at=occurred,
    )


def test_coverage_is_strict_on_the_start_instant() -> None:
    start = _moment("03:14:08.000000")
    coverage = WakeCoverage()
    coverage.record(_event(_moment("03:14:07.000000")), start)

    assert coverage.covering(_event(start - timedelta(microseconds=1))) is not None
    assert coverage.covering(_event(start)) is None  # not strictly before: not covered


def test_coverage_only_moves_forward() -> None:
    coverage = WakeCoverage()
    coverage.record(_event(_moment("03:00:00.000000"), "later"), _moment("03:30:00.000000"))
    coverage.record(_event(_moment("03:00:00.000000"), "earlier"), _moment("03:10:00.000000"))

    covering = coverage.covering(_event(_moment("03:20:00.000000")))
    assert covering is not None and covering.delivery == "later"


def test_coverage_ignores_events_without_a_time_both_ways() -> None:
    coverage = WakeCoverage()
    coverage.record(_event(None), _moment("03:30:00.000000"))
    assert coverage.covering(_event(_moment("03:00:00.000000"))) is None

    coverage.record(_event(_moment("03:00:00.000000")), _moment("03:30:00.000000"))
    assert coverage.covering(_event(None)) is None


def test_coverage_is_bounded_and_evicts_the_least_recent_stream() -> None:
    coverage = WakeCoverage(capacity=2)

    def on(issue: int, occurred: datetime) -> Event:
        url = f"https://github.com/{REPO}/issues/{issue}"
        return Event(
            source="github",
            kind=EventKind.HANDOFF,
            recipient=Recipient(by="repo", value=REPO),
            wake_arg=f"Cross-repo handoff: work {url}",
            delivery_id=f"0192f3a4-5b6c-7d8e-9f01-{issue:012x}",
            origin=IssueRef(repo=REPO, number=issue, url=url, title="Mirror the wire-shape change"),
            occurred_at=occurred,
        )

    early, start = _moment("03:00:00.000000"), _moment("03:10:00.000000")
    for issue in (1, 2, 3):
        coverage.record(on(issue, early), start)

    assert coverage.covering(on(1, early)) is None  # evicted
    assert coverage.covering(on(3, early)) is not None


def test_coverage_rejects_a_non_positive_capacity() -> None:
    with pytest.raises(ValueError, match="capacity"):
        WakeCoverage(capacity=0)


def test_an_event_time_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _event(datetime(2026, 9, 19, 3, 14, 7))
    assert _event(datetime(2026, 9, 19, 3, 14, 7, tzinfo=timezone.utc)).occurred_at is not None

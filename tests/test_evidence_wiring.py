"""Evidence is recorded from the *live* paths, not only by direct calls.

The store itself is covered in ``test_evidence.py``; what is pinned here is that
the daemon actually feeds it — a ledger wired to nothing is precisely the
green-while-absent shape the instrument exists to close
(basecradle/basecradle#460). Three seams: the pipeline's accept half (delivery
sink), its slow half (wake proof), and the scheduler (queue depth). Plus the two
fail-directions that matter: an unauthenticated caller cannot grow the document,
and an observer that breaks cannot break a wake. No network, model, or live agent.
Test cast: Nova Digital (``nova``, AI).
"""

import hashlib
import hmac
import json
from types import MappingProxyType

from basecradle_router.breaker import BreakerConfig, WakeRateBreaker
from basecradle_router.config import Config
from basecradle_router.dedup import DeliveryDeduper
from basecradle_router.evidence import EvidenceStore
from basecradle_router.models import Agent, Event, EventKind, IssueRef, Recipient
from basecradle_router.pipeline import Pipeline, PipelineResult
from basecradle_router.routes import InboundRequest, RouteRegistry
from basecradle_router.routes.github import GithubRoute
from basecradle_router.scheduler import WakeScheduler
from basecradle_router.wake import WakeError, WakeResult
from basecradle_router.wakelock import WakeLockGuard

NOVA = Agent(
    key="basecradle/basecradle-python",
    os_user="nova",
    clone_path="/home/nova/basecradle-python",
    bot_slug="basecradle-python-ai",
)
SECRET = "whsec_" + "0" * 32  # correctly-shaped fake
DELIVERY = "0192f3a4-5b6c-7d8e-9f01-00000000000a"
ISSUE_URL = "https://github.com/basecradle/basecradle-python/issues/42"


class _StubWaker:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def wake(self, agent: Agent, event: Event) -> WakeResult:
        if self.fail:
            raise WakeError("claude exited 1", exit_code=1)
        return WakeResult(exit_code=0, stdout="woke")


def _config() -> Config:
    return Config(
        agents=MappingProxyType({NOVA.key: NOVA}),
        enabled_routes=frozenset({"github"}),
        webhook_secrets=MappingProxyType({"github": SECRET}),
    )


def _pipeline(evidence: EvidenceStore, *, waker=None, **kwargs) -> Pipeline:
    registry = RouteRegistry()
    registry.register(GithubRoute(["drawkkwast"]))
    return Pipeline(
        registry=registry,
        config=_config(),
        waker=waker or _StubWaker(),
        evidence=evidence,
        sleep=lambda _d: None,
        **kwargs,
    )


def _event(delivery: str = DELIVERY) -> Event:
    return Event(
        source="github",
        kind=EventKind.HANDOFF,
        recipient=Recipient(by="repo", value=NOVA.key),
        wake_arg=f"Cross-repo handoff: work {ISSUE_URL}",
        delivery_id=delivery,
        origin=IssueRef(repo=NOVA.key, number=42, url=ISSUE_URL, title="Do the thing"),
    )


def _handoff_request(*, secret: str = SECRET, delivery: str = DELIVERY) -> InboundRequest:
    body = json.dumps(
        {
            "action": "labeled",
            "label": {"name": "handoff"},
            "sender": {"login": "drawkkwast"},
            "repository": {"full_name": NOVA.key},
            "issue": {
                "number": 42,
                "html_url": ISSUE_URL,
                "title": "Do the thing",
                "labels": [{"name": "handoff"}],
            },
        }
    ).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return InboundRequest(
        headers={
            "X-Hub-Signature-256": f"sha256={digest}",
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": delivery,
        },
        body=body,
    )


# --- the delivery sink: recorded from the real verify boundary ---------------


def test_a_verified_delivery_records_the_sinks_proof() -> None:
    evidence = EvidenceStore(None)

    _pipeline(evidence).handle("github", _handoff_request())

    sink = evidence.snapshot().delivery_sinks["github"]
    assert (sink.accepted, sink.rejected, sink.woke) == (1, 0, 1)


def test_a_bad_signature_records_a_rejection_not_an_accept() -> None:
    evidence = EvidenceStore(None)

    _pipeline(evidence).handle("github", _handoff_request(secret="whsec_" + "9" * 32))

    sink = evidence.snapshot().delivery_sinks["github"]
    assert (sink.accepted, sink.rejected) == (0, 1)
    assert "does not match" in sink.last_reject_reason


def test_an_unknown_source_cannot_grow_the_evidence_document() -> None:
    # The webhook path is unauthenticated: recording a sink row for an arbitrary
    # /webhooks/<anything> would let anyone grow the document one bogus source at a
    # time. Only a *known* route is ever counted.
    evidence = EvidenceStore(None)

    _pipeline(evidence).handle("not-a-real-source", _handoff_request())

    assert evidence.snapshot().delivery_sinks == {}


def test_a_verified_but_unactionable_delivery_counts_as_ignored() -> None:
    evidence = EvidenceStore(None)
    body = json.dumps({"action": "closed", "sender": {"login": "drawkkwast"}}).encode("utf-8")
    digest = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()

    _pipeline(evidence).handle(
        "github",
        InboundRequest(
            headers={
                "X-Hub-Signature-256": f"sha256={digest}",
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": DELIVERY,
            },
            body=body,
        ),
    )

    sink = evidence.snapshot().delivery_sinks["github"]
    # Verification still passed, so the integration is proven armed — that is the
    # instance-5 distinction, and it must not depend on the payload being actionable.
    assert (sink.accepted, sink.ignored, sink.woke) == (1, 1, 0)


# --- the wake edge: recorded from the real slow half ------------------------


def test_a_successful_wake_records_the_edges_evidence() -> None:
    evidence = EvidenceStore(None)

    _pipeline(evidence).execute(NOVA, _event(), PipelineResult())

    wake = evidence.snapshot().agent_wakes["nova"]
    assert wake.ok == 1
    assert wake.last_ok_delivery == DELIVERY


def test_an_exhausted_wake_records_a_failure_not_a_success() -> None:
    evidence = EvidenceStore(None)

    _pipeline(evidence, waker=_StubWaker(fail=True)).execute(NOVA, _event(), PipelineResult())

    wake = evidence.snapshot().agent_wakes["nova"]
    assert (wake.ok, wake.failed) == (0, 1)
    assert wake.last_ok_at is None  # still never-proven


def test_a_held_wake_lock_records_a_refusal(tmp_path) -> None:
    (tmp_path / "nova.lock").write_text(
        '{"agent": "nova", "reason": "converge", "expires_at": "2999-01-01T00:00:00+00:00"}',
        encoding="utf-8",
    )
    evidence = EvidenceStore(None)

    _pipeline(evidence, wake_lock=WakeLockGuard(lock_dir=str(tmp_path))).execute(
        NOVA, _event(), PipelineResult()
    )

    wake = evidence.snapshot().agent_wakes["nova"]
    assert (wake.ok, wake.failed, wake.refused) == (0, 0, 1)
    assert wake.last_refused_reason.startswith("wake_lock_held")


def test_a_tripped_breaker_records_a_refusal() -> None:
    evidence = EvidenceStore(None)
    breaker = WakeRateBreaker(BreakerConfig(max_wakes=1, window=60.0, cooldown=60.0))
    pipeline = _pipeline(evidence, breaker=breaker)

    pipeline.execute(NOVA, _event("delivery-1"), PipelineResult())
    pipeline.execute(NOVA, _event("delivery-2"), PipelineResult())

    wake = evidence.snapshot().agent_wakes["nova"]
    assert (wake.ok, wake.refused) == (1, 1)


def test_a_duplicate_delivery_records_a_refusal_not_a_second_wake() -> None:
    evidence = EvidenceStore(None)
    pipeline = _pipeline(evidence, deduper=DeliveryDeduper(600.0))

    pipeline.execute(NOVA, _event(), PipelineResult())
    pipeline.execute(NOVA, _event(), PipelineResult())  # same delivery id

    wake = evidence.snapshot().agent_wakes["nova"]
    assert (wake.ok, wake.refused) == (1, 1)
    assert wake.last_refused_reason == "duplicate_delivery"


# --- the scheduler's queue depth -------------------------------------------


def test_the_scheduler_reports_queue_depth_to_its_observer() -> None:
    seen: list[tuple[str, int]] = []
    scheduler = WakeScheduler(
        lambda agent, event, result: None, lanes=1, on_queue_change=lambda k, n: seen.append((k, n))
    )

    scheduler.submit(NOVA, _event(), PipelineResult())
    scheduler.wait_idle(timeout=5)
    scheduler.shutdown()

    assert seen[0] == ("nova", 1)  # enqueued/in flight
    assert seen[-1] == ("nova", 0)  # drained


def test_a_broken_queue_observer_cannot_break_a_wake(caplog) -> None:
    # Observability must never be able to wedge the thing it observes: the observer
    # runs outside the scheduler's condition lock and its defects are swallowed.
    ran: list[str] = []

    def explode(key: str, pending: int) -> None:
        raise RuntimeError("observer is broken")

    scheduler = WakeScheduler(
        lambda agent, event, result: ran.append(agent.harness_key),
        lanes=1,
        on_queue_change=explode,
    )

    with caplog.at_level("ERROR", logger="basecradle_router.scheduler"):
        scheduler.submit(NOVA, _event(), PipelineResult())
        assert scheduler.wait_idle(timeout=5)
    scheduler.shutdown()

    assert ran == ["nova"]
    assert "queue observer raised" in caplog.text

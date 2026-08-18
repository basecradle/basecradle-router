"""The core pipeline driven offline end-to-end, every collaborator mocked.

A fabricated, correctly-signed handoff flows through verify → normalize →
resolve → lock → wake; the ordered stage outcomes are asserted. The pipeline
ends at the wake — the router never merges (the agent enables GitHub native
auto-merge on its own PR; see issue #38). No network, model, or live agent.
Test cast: John Doe (human) hands off; Nova Digital (``nova``, AI) is woken.
"""

import itertools
import json
import re
import shlex
from types import MappingProxyType

from basecradle_router.breaker import BreakerConfig, WakeRateBreaker
from basecradle_router.config import Config
from basecradle_router.dedup import DeliveryDeduper
from basecradle_router.evidence import EvidenceStore
from basecradle_router.logfmt import BLUE, GREEN, RED, RESET, YELLOW
from basecradle_router.models import Agent, Event, EventKind, Recipient, WakeKind
from basecradle_router.pipeline import Outcome, Pipeline, PipelineResult, Stage
from basecradle_router.routes import BasecradleRoute, InboundRequest, ProbeRoute, RouteRegistry
from basecradle_router.routes.github import GithubRoute
from basecradle_router.wake import WakeError, WakeResult
from basecradle_router.wakelock import WakeLockGuard, WakeLockState

SECRET = "whsec_" + "0" * 32
BASECRADLE_SECRET = "whsec_" + "1" * 32
HANDOFF_SENDER = "john"  # John Doe, a trusted human org member, files the handoff
TRUSTED_ACTORS = frozenset({HANDOFF_SENDER})
UNTRUSTED_SENDER = "drive-by-stranger"
NOVA = Agent(
    key="basecradle/basecradle-python",
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
ISSUE_URL = "https://github.com/basecradle/basecradle-python/issues/42"
TIMELINE_UUID = "0192aaaa-bbbb-7ccc-8ddd-eeeeffff0000"
# Well-formed UUIDv7 delivery ids for the dedup tests (an X-GitHub-Delivery value
# is a UUID — CLAUDE.md: "UUIDs are real well-formed UUIDv7"). Two distinct ones
# plus a shared one to stand in for "the same event delivered twice".
DELIVERY_A = "0192f3a4-5b6c-7d8e-9f01-00000000000a"
DELIVERY_B = "0192f3a4-5b6c-7d8e-9f01-00000000000b"
DELIVERY_DUP = "0192f3a4-5b6c-7d8e-9f01-0000000d00f1"


# --- doubles ---------------------------------------------------------------


class _StubWaker:
    def __init__(self, result: WakeResult | None = None, fail_times: int = 0) -> None:
        self.result = result or WakeResult(exit_code=0, stdout="woke")
        self.fail_times = fail_times
        self.calls: list[tuple[Agent, Event]] = []

    def wake(self, agent: Agent, event: Event) -> WakeResult:
        self.calls.append((agent, event))
        if len(self.calls) <= self.fail_times:
            raise WakeError("transient blip", exit_code=2)
        return self.result


def _config(agents: dict[str, Agent] | None = None) -> Config:
    return Config(
        agents=MappingProxyType({NOVA.key: NOVA} if agents is None else agents),
        enabled_routes=frozenset({"github"}),
        webhook_secrets=MappingProxyType({"github": SECRET}),
    )


def _registry() -> RouteRegistry:
    registry = RouteRegistry()
    registry.register(GithubRoute(TRUSTED_ACTORS))
    return registry


def _pipeline(
    *,
    waker: _StubWaker | None = None,
    config: Config | None = None,
    locks=None,
    breaker: WakeRateBreaker | None = None,
    deduper: DeliveryDeduper | None = None,
    wake_lock: WakeLockGuard | None = None,
    evidence: EvidenceStore | None = None,
    clock=None,
) -> tuple[Pipeline, _StubWaker]:
    waker = waker or _StubWaker()
    kwargs = dict(
        registry=_registry(),
        config=config or _config(),
        waker=waker,
        sleep=lambda _d: None,  # deterministic: never really sleep
    )
    if locks is not None:
        kwargs["locks"] = locks
    if breaker is not None:
        kwargs["breaker"] = breaker
    if deduper is not None:
        kwargs["deduper"] = deduper
    if wake_lock is not None:
        kwargs["wake_lock"] = wake_lock
    if evidence is not None:
        kwargs["evidence"] = evidence
    if clock is not None:
        kwargs["clock"] = clock
    return Pipeline(**kwargs), waker


class _FakeClock:
    """A monotonic clock that advances a fixed step per reading.

    The pipeline reads it twice per wake attempt (before and after), so a ``step``
    of N makes every attempt take exactly N "seconds" — a logged duration a test can
    assert on exactly, with nothing real timed.
    """

    def __init__(self, step: float = 23.1) -> None:
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        reading = self.now
        self.now += self.step
        return reading


class _StubWakeLock:
    """A wake-lock guard stub keyed by slug, so a test pins a held/stale lock."""

    def __init__(self, decisions: dict[str, WakeLockState] | None = None) -> None:
        self.decisions = decisions or {}
        self.checked: list[str] = []

    def check(self, slug: str):
        from basecradle_router.wakelock import WakeLockDecision

        self.checked.append(slug)
        state = self.decisions.get(slug, WakeLockState.ABSENT)
        return WakeLockDecision(state, state.value)


# Each call gets a distinct delivery id by default — a distinct GitHub delivery
# is precisely a distinct ``X-GitHub-Delivery`` GUID, so this models reality and
# keeps the new delivery-dedup (#133) from collapsing tests that fire many
# *separate* events. A test exercising dedup passes an explicit shared ``delivery``.
_delivery_seq = itertools.count(1)


def _github_request(
    *,
    action: str = "opened",
    labels: tuple[str, ...] = ("handoff",),
    repo: str = "basecradle/basecradle-python",
    event: str = "issues",
    sign: bool = True,
    delivery: str | None = None,
    sender: str = HANDOFF_SENDER,
) -> InboundRequest:
    if delivery is None:
        delivery = f"0192f3a4-5b6c-7d8e-9f01-{next(_delivery_seq):012x}"
    payload = {
        "action": action,
        "issue": {
            "number": 42,
            "title": "Mirror the wire-shape change",
            "html_url": f"https://github.com/{repo}/issues/42",
            "labels": [{"name": name} for name in labels],
        },
        "repository": {"full_name": repo},
        "sender": {"login": sender, "type": "User"},
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
    if sign:
        import hashlib
        import hmac

        digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={digest}"
    return InboundRequest(headers=headers, body=body)


# --- the happy path --------------------------------------------------------


def test_signed_handoff_wakes_the_right_agent_with_the_right_trigger() -> None:
    pipeline, waker = _pipeline()
    result = pipeline.handle("github", _github_request())

    assert result.stages == [
        (Stage.ROUTE, Outcome.OK),
        (Stage.VERIFY, Outcome.OK),
        (Stage.NORMALIZE, Outcome.OK),
        (Stage.RESOLVE, Outcome.OK),
        (Stage.LOCK, Outcome.OK),
        (Stage.WAKE, Outcome.OK),
    ]
    assert len(waker.calls) == 1
    woken_agent, woken_event = waker.calls[0]
    assert woken_agent is NOVA
    # The wake carries the handoff-recognition marker (the route's quarantine
    # envelope follows it — asserted in detail in test_github_route).
    assert woken_event.wake_arg.startswith(f"Cross-repo handoff: work {ISSUE_URL}\n")
    assert result.agent is NOVA


# --- the basecradle route: a platform event wakes a harness persona --------


def _basecradle_pipeline(waker: _StubWaker | None = None) -> tuple[Pipeline, _StubWaker]:
    waker = waker or _StubWaker()
    registry = RouteRegistry()
    registry.register(BasecradleRoute())
    config = Config(
        agents=MappingProxyType({JT.key: JT}),
        enabled_routes=frozenset({"basecradle"}),
        webhook_secrets=MappingProxyType({"basecradle": BASECRADLE_SECRET}),
        recipient_index=MappingProxyType({JT.recipient_uuid: JT}),
    )
    return (
        Pipeline(registry=registry, config=config, waker=waker, sleep=lambda _d: None),
        waker,
    )


def _basecradle_request(
    *,
    event: str = "message.created",
    recipient_uuid: str = JT.recipient_uuid,
    timeline_uuid: str = TIMELINE_UUID,
    sign: bool = True,
    delivery: str = "0192f3a4-5b6c-7d8e-9f01-23456789abcd",
) -> InboundRequest:
    payload = {
        "event": event,
        "event_id": delivery,
        "occurred_at": "2026-06-09T00:00:00Z",
        "actor_uuid": None,
        "recipient_uuid": recipient_uuid,
        "timeline_uuid": timeline_uuid,
        "resource": {"type": "message", "uuid": timeline_uuid, "url": "https://x"},
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"X-BaseCradle-Event": event, "X-BaseCradle-Delivery": delivery}
    if sign:
        import hashlib
        import hmac

        digest = hmac.new(BASECRADLE_SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers["X-BaseCradle-Signature"] = f"sha256={digest}"
    return InboundRequest(headers=headers, body=body)


def test_signed_platform_event_wakes_the_harness_persona() -> None:
    pipeline, waker = _basecradle_pipeline()
    result = pipeline.handle("basecradle", _basecradle_request())

    assert result.stages == [
        (Stage.ROUTE, Outcome.OK),
        (Stage.VERIFY, Outcome.OK),
        (Stage.NORMALIZE, Outcome.OK),
        (Stage.RESOLVE, Outcome.OK),
        (Stage.LOCK, Outcome.OK),
        (Stage.WAKE, Outcome.OK),
    ]
    assert len(waker.calls) == 1
    woken_agent, woken_event = waker.calls[0]
    assert woken_agent is JT  # resolved by recipient_uuid, not a repo
    assert woken_event.wake_arg == TIMELINE_UUID  # the timeline the harness processes


def test_basecradle_bad_signature_is_rejected_before_normalize() -> None:
    pipeline, waker = _basecradle_pipeline()
    result = pipeline.handle("basecradle", _basecradle_request(sign=False))
    assert result.stages[-1] == (Stage.VERIFY, Outcome.REJECTED)
    assert waker.calls == []


def test_basecradle_unknown_recipient_fails_at_resolve_no_wake() -> None:
    pipeline, waker = _basecradle_pipeline()
    result = pipeline.handle(
        "basecradle",
        _basecradle_request(recipient_uuid="019e0000-0000-7000-8000-000000000000"),
    )
    assert result.terminal is Outcome.FAILED
    assert result.stages[-1][0] is Stage.RESOLVE
    assert waker.calls == []


def test_basecradle_non_message_event_is_ignored() -> None:
    pipeline, waker = _basecradle_pipeline()
    result = pipeline.handle("basecradle", _basecradle_request(event="reaction.created"))
    assert result.stages[-1] == (Stage.NORMALIZE, Outcome.IGNORED)
    assert waker.calls == []


# --- ignore / reject / fail ------------------------------------------------


def test_non_handoff_event_is_ignored_and_short_circuits() -> None:
    pipeline, waker = _pipeline()
    result = pipeline.handle("github", _github_request(labels=("bug",)))

    assert result.stages == [
        (Stage.ROUTE, Outcome.OK),
        (Stage.VERIFY, Outcome.OK),
        (Stage.NORMALIZE, Outcome.IGNORED),
    ]
    assert waker.calls == []


def test_bad_signature_is_rejected_before_normalize() -> None:
    pipeline, waker = _pipeline()
    result = pipeline.handle("github", _github_request(sign=False))

    assert result.stages == [
        (Stage.ROUTE, Outcome.OK),
        (Stage.VERIFY, Outcome.REJECTED),
    ]
    assert waker.calls == []


def test_missing_secret_fails_at_verify_without_crashing() -> None:
    # An enabled route with no configured secret is our misconfiguration: the
    # verify stage fails (logged) rather than the daemon crashing.
    config = Config(
        agents=MappingProxyType({NOVA.key: NOVA}),
        enabled_routes=frozenset({"github"}),
        webhook_secrets=MappingProxyType({}),  # no github secret
    )
    pipeline, waker = _pipeline(config=config)
    result = pipeline.handle("github", _github_request())
    assert result.terminal is Outcome.FAILED
    assert result.stages[-1][0] is Stage.VERIFY
    assert waker.calls == []


def test_unknown_source_is_rejected() -> None:
    pipeline, _ = _pipeline()
    result = pipeline.handle("gitlab", _github_request())
    assert result.stages == [(Stage.ROUTE, Outcome.REJECTED)]


def test_unregistered_repo_fails_at_resolve() -> None:
    pipeline, waker = _pipeline(config=_config(agents={}))
    result = pipeline.handle("github", _github_request())
    assert result.terminal is Outcome.FAILED
    assert result.stages[-1][0] is Stage.RESOLVE
    assert waker.calls == []


def test_malformed_payload_is_rejected_at_normalize() -> None:
    bad = _github_request()
    broken = InboundRequest(headers=dict(bad.headers), body=b"{not json")
    # Re-sign the broken body so it passes verify and fails at normalize.
    import hashlib
    import hmac

    digest = hmac.new(SECRET.encode(), broken.body, hashlib.sha256).hexdigest()
    headers = dict(broken.headers)
    headers["X-Hub-Signature-256"] = f"sha256={digest}"
    pipeline, _ = _pipeline()
    result = pipeline.handle("github", InboundRequest(headers=headers, body=broken.body))
    assert (Stage.NORMALIZE, Outcome.REJECTED) in result.stages


def test_handoff_from_untrusted_sender_is_rejected_at_normalize() -> None:
    # A correctly-signed handoff from an actor not on the fleet allow-list is
    # rejected at normalize — no wake. Defense-in-depth behind GitHub's perms.
    pipeline, waker = _pipeline()
    result = pipeline.handle("github", _github_request(sender=UNTRUSTED_SENDER))

    assert result.stages == [
        (Stage.ROUTE, Outcome.OK),
        (Stage.VERIFY, Outcome.OK),
        (Stage.NORMALIZE, Outcome.REJECTED),
    ]
    assert waker.calls == []


# --- wake retry ------------------------------------------------------------


def test_wake_retries_a_transient_failure_then_succeeds() -> None:
    waker = _StubWaker(fail_times=2)  # fails twice, succeeds on the third
    pipeline, _ = _pipeline(waker=waker)
    result = pipeline.handle("github", _github_request())
    assert (Stage.WAKE, Outcome.OK) in result.stages
    assert len(waker.calls) == 3


def test_wake_gives_up_after_the_bound_and_records_failure() -> None:
    waker = _StubWaker(fail_times=99)  # never succeeds
    pipeline, _ = _pipeline(waker=waker)
    result = pipeline.handle("github", _github_request())
    assert result.terminal is Outcome.FAILED
    assert result.stages[-1][0] is Stage.WAKE
    assert len(waker.calls) == 3  # the default bound


# --- the wake-rate circuit breaker gates dispatch at the chokepoint --------


def test_a_wake_burst_trips_the_breaker_and_halts_dispatch() -> None:
    # A synthetic runaway: a low cap (2 wakes/window) so a third delivery trips
    # the breaker at the chokepoint — the wake is refused, recorded, not dispatched.
    breaker = WakeRateBreaker(BreakerConfig(max_wakes=2, window=60.0, cooldown=60.0))
    pipeline, waker = _pipeline(breaker=breaker)

    for _ in range(2):
        result = pipeline.handle("github", _github_request())
        assert result.stages[-1] == (Stage.WAKE, Outcome.OK)
    assert len(waker.calls) == 2

    # The third dispatch trips: locked, then refused by the breaker — no wake.
    tripped = pipeline.handle("github", _github_request())
    assert tripped.stages == [
        (Stage.ROUTE, Outcome.OK),
        (Stage.VERIFY, Outcome.OK),
        (Stage.NORMALIZE, Outcome.OK),
        (Stage.RESOLVE, Outcome.OK),
        (Stage.LOCK, Outcome.OK),
        (Stage.BREAKER, Outcome.IGNORED),
    ]
    assert len(waker.calls) == 2  # dispatch stopped — the waker was not called again


def test_breaker_cooldown_restores_dispatch_through_the_pipeline() -> None:
    clock = [0.0]
    breaker = WakeRateBreaker(
        BreakerConfig(max_wakes=2, window=60.0, cooldown=30.0), clock=lambda: clock[0]
    )
    pipeline, waker = _pipeline(breaker=breaker)

    for _ in range(2):
        pipeline.handle("github", _github_request())
    assert pipeline.handle("github", _github_request()).stages[-1] == (
        Stage.BREAKER,
        Outcome.IGNORED,
    )  # tripped
    assert len(waker.calls) == 2

    clock[0] = 31.0  # past the cooldown
    healed = pipeline.handle("github", _github_request())
    assert healed.stages[-1] == (Stage.WAKE, Outcome.OK)  # dispatch resumed
    assert len(waker.calls) == 3


# --- issue_comment re-wake flows through the same pipeline (#129) -----------


def test_signed_comment_rewakes_the_agent_through_the_pipeline() -> None:
    # A reply on a handoff issue re-wakes its agent end-to-end, the same path as
    # the opening handoff — pointing the agent back at the issue to re-read it.
    pipeline, waker = _pipeline()
    result = pipeline.handle("github", _github_request(event="issue_comment", action="created"))

    assert result.stages[-1] == (Stage.WAKE, Outcome.OK)
    assert len(waker.calls) == 1
    woken_agent, woken_event = waker.calls[0]
    assert woken_agent is NOVA
    assert woken_event.wake_arg.startswith(f"Cross-repo handoff: work {ISSUE_URL}\n")


def test_a_comment_storm_on_one_issue_trips_the_per_issue_breaker() -> None:
    # The breaker's per-(agent, issue) scope (stream_key == issue url) caps a
    # comment storm on a single handoff issue even while the agent's overall rate
    # stays well under its cap — a reply loop is gated, not amplified (#129).
    breaker = WakeRateBreaker(
        BreakerConfig(max_wakes=20, window=60.0, cooldown=60.0, stream_max_wakes=2)
    )
    pipeline, waker = _pipeline(breaker=breaker)

    def _comment():
        return pipeline.handle("github", _github_request(event="issue_comment", action="created"))

    for _ in range(2):
        assert _comment().stages[-1] == (Stage.WAKE, Outcome.OK)
    # The third comment on the same issue trips the per-stream cap — refused, no wake.
    assert _comment().stages[-1] == (Stage.BREAKER, Outcome.IGNORED)
    assert len(waker.calls) == 2


# --- delivery dedup collapses a duplicate webhook delivery (#133) -----------


def test_duplicate_delivery_is_collapsed_into_one_wake() -> None:
    # One logical event delivered twice (same X-GitHub-Delivery GUID — e.g. two
    # fleet Apps on the repo) wakes the agent ONCE: the second is a visible DEDUP
    # ignore, not a second session.
    pipeline, waker = _pipeline()
    first = pipeline.handle("github", _github_request(delivery=DELIVERY_DUP))
    assert first.stages[-1] == (Stage.WAKE, Outcome.OK)

    second = pipeline.handle("github", _github_request(delivery=DELIVERY_DUP))
    assert second.stages == [
        (Stage.ROUTE, Outcome.OK),
        (Stage.VERIFY, Outcome.OK),
        (Stage.NORMALIZE, Outcome.OK),
        (Stage.RESOLVE, Outcome.OK),
        (Stage.LOCK, Outcome.OK),
        (Stage.DEDUP, Outcome.IGNORED),
    ]
    assert len(waker.calls) == 1  # exactly one wake for the duplicated event


def test_distinct_deliveries_each_wake() -> None:
    # Two genuinely different events (distinct GUIDs) both wake — dedup never
    # over-collapses, the dangerous direction.
    pipeline, waker = _pipeline()
    assert pipeline.handle("github", _github_request(delivery=DELIVERY_A)).stages[-1] == (
        Stage.WAKE,
        Outcome.OK,
    )
    assert pipeline.handle("github", _github_request(delivery=DELIVERY_B)).stages[-1] == (
        Stage.WAKE,
        Outcome.OK,
    )
    assert len(waker.calls) == 2


def test_dedup_does_not_suppress_a_duplicate_when_the_first_wake_failed() -> None:
    # Mark-after-success: a duplicate is collapsed only if the original actually
    # woke. A failed original leaves the duplicate free to retry the work.
    waker = _StubWaker(fail_times=99)  # the first delivery's wake never succeeds
    pipeline, _ = _pipeline(waker=waker)
    first = pipeline.handle("github", _github_request(delivery=DELIVERY_DUP))
    assert first.terminal is Outcome.FAILED

    waker.fail_times = 0  # the box recovers; the duplicate now succeeds
    second = pipeline.handle("github", _github_request(delivery=DELIVERY_DUP))
    assert second.stages[-1] == (Stage.WAKE, Outcome.OK)  # not suppressed


def test_dedup_entry_expires_after_ttl_and_allows_a_rewake() -> None:
    # The recently-woke window is bounded: past the TTL, the same delivery wakes
    # afresh (a long-delayed redelivery is a fresh signal, not a duplicate).
    clock = [0.0]
    deduper = DeliveryDeduper(ttl=30.0, clock=lambda: clock[0])
    pipeline, waker = _pipeline(deduper=deduper)

    assert pipeline.handle("github", _github_request(delivery=DELIVERY_DUP)).stages[-1] == (
        Stage.WAKE,
        Outcome.OK,
    )
    assert pipeline.handle("github", _github_request(delivery=DELIVERY_DUP)).stages[-1] == (
        Stage.DEDUP,
        Outcome.IGNORED,
    )  # within the TTL: collapsed
    clock[0] = 31.0  # past the TTL
    assert pipeline.handle("github", _github_request(delivery=DELIVERY_DUP)).stages[-1] == (
        Stage.WAKE,
        Outcome.OK,
    )  # window cleared: wakes again
    assert len(waker.calls) == 2


def test_dedup_disabled_by_zero_ttl_lets_every_delivery_wake() -> None:
    # TTL 0 disables dedup entirely — the escape hatch; every delivery wakes.
    pipeline, waker = _pipeline(deduper=DeliveryDeduper(ttl=0.0))
    for _ in range(3):
        assert pipeline.handle("github", _github_request(delivery=DELIVERY_DUP)).stages[-1] == (
            Stage.WAKE,
            Outcome.OK,
        )
    assert len(waker.calls) == 3


# --- the NOC wake-lock interlock gates dispatch (basecradle-router#120) -----


def test_held_wake_lock_refuses_the_wake() -> None:
    # The NOC is converging this agent: the lock is held, so the wake is refused
    # at the WAKE_LOCK stage — locked, then dropped, and the waker is never called.
    wake_lock = _StubWakeLock({"nova": WakeLockState.HELD})
    pipeline, waker = _pipeline(wake_lock=wake_lock)

    result = pipeline.handle("github", _github_request())
    assert result.stages == [
        (Stage.ROUTE, Outcome.OK),
        (Stage.VERIFY, Outcome.OK),
        (Stage.NORMALIZE, Outcome.OK),
        (Stage.RESOLVE, Outcome.OK),
        (Stage.LOCK, Outcome.OK),
        (Stage.WAKE_LOCK, Outcome.IGNORED),
    ]
    assert waker.calls == []  # no wake landed on the converging agent
    assert wake_lock.checked == ["nova"]  # checked by the agent's slug (harness_key)


def test_unparseable_wake_lock_also_refuses() -> None:
    # Present-but-malformed honours "Present = locked": refuse, same as held.
    wake_lock = _StubWakeLock({"nova": WakeLockState.UNPARSEABLE})
    pipeline, waker = _pipeline(wake_lock=wake_lock)
    result = pipeline.handle("github", _github_request())
    assert result.stages[-1] == (Stage.WAKE_LOCK, Outcome.IGNORED)
    assert waker.calls == []


def test_stale_wake_lock_wakes_normally() -> None:
    # A stale (expired) lock does not gate: dispatch proceeds through to the wake.
    wake_lock = _StubWakeLock({"nova": WakeLockState.STALE})
    pipeline, waker = _pipeline(wake_lock=wake_lock)
    result = pipeline.handle("github", _github_request())
    assert result.stages[-1] == (Stage.WAKE, Outcome.OK)
    assert len(waker.calls) == 1


def test_absent_wake_lock_wakes_normally() -> None:
    # No lock present: the common case — wake, with no WAKE_LOCK record in the trace.
    pipeline, waker = _pipeline(wake_lock=_StubWakeLock())
    result = pipeline.handle("github", _github_request())
    assert result.stages[-1] == (Stage.WAKE, Outcome.OK)
    assert Stage.WAKE_LOCK not in [s for s, _ in result.stages]


def test_held_wake_lock_does_not_consume_breaker_budget() -> None:
    # The wake-lock is checked BEFORE the breaker, so a refused wake never records
    # against the breaker window: after the lock clears, full budget remains.
    breaker = WakeRateBreaker(BreakerConfig(max_wakes=2, window=60.0, cooldown=60.0))
    wake_lock = _StubWakeLock({"nova": WakeLockState.HELD})
    pipeline, waker = _pipeline(breaker=breaker, wake_lock=wake_lock)

    # Three deliveries arrive while the lock is held — all refused, none counted.
    for _ in range(3):
        assert pipeline.handle("github", _github_request()).stages[-1] == (
            Stage.WAKE_LOCK,
            Outcome.IGNORED,
        )
    assert waker.calls == []

    # The converge finishes (lock clears); two wakes still fit under the cap.
    wake_lock.decisions.clear()
    for _ in range(2):
        assert pipeline.handle("github", _github_request()).stages[-1] == (Stage.WAKE, Outcome.OK)
    assert len(waker.calls) == 2


# --- the lock prevents concurrent double-wakes -----------------------------


def test_same_agent_is_locked_during_a_wake() -> None:
    import threading

    from basecradle_router.concurrency import AgentLocks

    locks = AgentLocks()
    in_wake = threading.Event()
    release = threading.Event()

    class _BlockingWaker:
        def wake(self, agent: Agent, event: Event) -> WakeResult:
            in_wake.set()
            release.wait(timeout=5)
            return WakeResult(exit_code=0)

    pipeline = Pipeline(
        registry=_registry(),
        config=_config(),
        waker=_BlockingWaker(),
        locks=locks,
        sleep=lambda _d: None,
    )
    worker = threading.Thread(target=lambda: pipeline.handle("github", _github_request()))
    worker.start()
    assert in_wake.wait(timeout=5)  # the wake is running, holding the agent lock

    # The agent is locked: a second wake cannot start concurrently.
    assert locks.acquire(NOVA.harness_key, blocking=False) is False

    release.set()
    worker.join(timeout=5)
    assert locks.acquire(NOVA.harness_key, blocking=False) is True  # released after the wake
    locks.release(NOVA.harness_key)


# --- observability: key=value stage lines, delivery correlation, duration (#170) ---
#
# The audit that produced #170 found the log stream machine-shippable but
# human-opaque: the wake completion line (`stage=wake outcome=ok exit 0`) named
# neither the agent nor the delivery, so two concurrent wakes interleaved
# ambiguously in Live Tail; the wake's wall-clock was never recorded; and the retry
# backoff swallowed transient failures until final exhaustion, so a flapping agent
# read as healthy. These pin the fix.


#: Any ANSI SGR sequence — the palette's escapes, stripped when a test wants to read a
#: line the way a colour-blind consumer (a grep, a ClickHouse `extract`) reads it.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _stage_line(caplog, stage: Stage) -> str:
    """The pipeline's log line for ``stage`` — the message as it reaches journald."""
    return next(
        r.getMessage()
        for r in caplog.records
        if r.name == "basecradle_router.pipeline" and f"stage={stage.value} " in r.getMessage()
    )


def test_the_wake_line_identifies_the_agent_the_delivery_the_exit_and_the_duration(caplog) -> None:
    # The headline of #170: the wake completion line must fully identify its wake.
    pipeline, _ = _pipeline(clock=_FakeClock(step=23.1))
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        pipeline.handle("github", _github_request(delivery=DELIVERY_A))

    line = _stage_line(caplog, Stage.WAKE)
    assert "outcome=ok" in line
    assert f"agent={NOVA.harness_key}" in line  # the OS-user slug: joins to the wake's own journal
    assert f"delivery={DELIVERY_A}" in line
    assert "exit=0" in line
    assert "duration=23.1s" in line


def test_every_stage_line_from_normalize_onward_carries_the_delivery_id(caplog) -> None:
    # `delivery=<id>` must select ONE delivery's whole trip out of a Live Tail of
    # interleaved concurrent deliveries. Route and verify run before the route has
    # read the source's delivery header, so they are the deliberate exception.
    pipeline, _ = _pipeline()
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        pipeline.handle("github", _github_request(delivery=DELIVERY_A))

    correlated = (Stage.NORMALIZE, Stage.RESOLVE, Stage.LOCK, Stage.WAKE)
    for stage in correlated:
        assert f"delivery={DELIVERY_A}" in _stage_line(caplog, stage), f"{stage} lost the delivery"


def test_stage_lines_are_key_value_with_no_bare_trailing_detail(caplog) -> None:
    # The defect: `stage=wake outcome=ok exit 0` — a bare, positional trailing value
    # that no log query could address. EVERY token is now a named key. Both a clean
    # wake and a failing one are driven, because the failing path is the one that logs
    # a free-text error, and a quoted error must stay ONE field rather than decaying
    # into bare tokens — which is exactly what shlex.split (not str.split) checks.
    for waker in (_StubWaker(), _StubWaker(fail_times=99)):
        pipeline, _ = _pipeline(waker=waker)
        with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
            pipeline.handle("github", _github_request())

    lines = [r.getMessage() for r in caplog.records if r.name == "basecradle_router.pipeline"]
    assert any("outcome=failed" in line for line in lines)  # the free-text error path ran
    for line in lines:
        for token in shlex.split(line):
            assert "=" in token, f"bare positional token {token!r} in: {line}"


def test_a_transient_wake_failure_is_logged_as_it_happens_not_only_at_exhaustion(caplog) -> None:
    # The silent-backoff fix: attempts 1 and 2 fail, attempt 3 succeeds. The wake is
    # a success — but a flapping agent must NOT read as healthy, so each retried
    # failure is a WARNING naming the attempt, the agent, the delivery, and the error.
    pipeline, waker = _pipeline(waker=_StubWaker(fail_times=2), clock=_FakeClock(step=5.0))
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        result = pipeline.handle("github", _github_request(delivery=DELIVERY_A))

    assert result.terminal is Outcome.OK  # it did eventually wake
    assert len(waker.calls) == 3

    retries = [r for r in caplog.records if "event=wake_retry" in r.getMessage()]
    assert len(retries) == 2  # the two failures a retry FOLLOWED; the 3rd succeeded
    assert all(r.levelname == "WARNING" for r in retries)
    assert "attempt=1/3" in retries[0].getMessage()
    assert "attempt=2/3" in retries[1].getMessage()
    for message in (r.getMessage() for r in retries):
        assert f"agent={NOVA.harness_key}" in message
        assert f"delivery={DELIVERY_A}" in message
        assert "transient blip" in message
        assert "duration=5.0s" in message  # each failed attempt's own wall-clock


def test_an_exhausted_wake_records_the_agent_delivery_attempts_and_error(caplog) -> None:
    pipeline, _ = _pipeline(waker=_StubWaker(fail_times=99), clock=_FakeClock(step=2.0))
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        result = pipeline.handle("github", _github_request(delivery=DELIVERY_B))

    assert result.terminal is Outcome.FAILED
    line = _stage_line(caplog, Stage.WAKE)
    assert "outcome=failed" in line
    assert f"agent={NOVA.harness_key}" in line
    assert f"delivery={DELIVERY_B}" in line
    assert "attempts=3" in line
    assert "duration=2.0s" in line
    assert "transient blip" in line


def test_the_dedup_ignore_line_names_the_agent_and_the_delivery(caplog) -> None:
    # A collapsed duplicate must say WHICH delivery it collapsed, or the ignore is
    # visible in name only.
    pipeline, _ = _pipeline(deduper=DeliveryDeduper(ttl=600.0))
    pipeline.handle("github", _github_request(delivery=DELIVERY_DUP))
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        result = pipeline.handle("github", _github_request(delivery=DELIVERY_DUP))

    assert (Stage.DEDUP, Outcome.IGNORED) in result.stages
    line = _stage_line(caplog, Stage.DEDUP)
    assert f"agent={NOVA.harness_key}" in line
    assert f"delivery={DELIVERY_DUP}" in line
    assert "reason=duplicate_delivery" in line


def test_the_breaker_ignore_line_names_the_agent_and_the_delivery(caplog) -> None:
    # Same for a breaker refusal: the wake it refused is identified, not just counted.
    breaker = WakeRateBreaker(BreakerConfig(max_wakes=1, window=60.0, cooldown=60.0))
    pipeline, _ = _pipeline(breaker=breaker)
    pipeline.handle("github", _github_request())  # fills the window
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        result = pipeline.handle("github", _github_request(delivery=DELIVERY_B))

    assert (Stage.BREAKER, Outcome.IGNORED) in result.stages
    line = _stage_line(caplog, Stage.BREAKER)
    assert f"agent={NOVA.harness_key}" in line
    assert f"delivery={DELIVERY_B}" in line


# --- the wake-origin label: `source=<route>` on every wake line (#222) -------
#
# The router's probe traverses the real path on purpose, so a probe wake lands on the
# same `stage=wake` line a real handoff does — and that line is what a log-metric
# extractor lifts a per-wake metric from. Extraction lifts only LOW-CARDINALITY keys,
# so the probe's one previous marker (a `probe-` prefix typed into the high-cardinality
# `delivery=` id) was dropped on the floor and every chart built on the metric silently
# mixed the fleet's own test traffic with its real work. `source=` is the router's own
# existing vocabulary — the fast half and the routes layer have always logged it —
# carried through to the half that had dropped it. These pin that no pipeline line can
# lose it, whatever the wake's outcome.

# The wake half is the half `_who` feeds, and is what the NOC's extraction guard names
# as the surface that must newly carry `source=`. It is derived by SUBTRACTION on
# purpose: a Stage added later falls into it by default and fails the coverage assertion
# below until it is exercised, rather than slipping through untested.
_ACCEPT_STAGES = (Stage.ROUTE, Stage.VERIFY, Stage.NORMALIZE, Stage.RESOLVE)
_WAKE_STAGES = tuple(stage for stage in Stage if stage not in _ACCEPT_STAGES)

PROBE_DELIVERY = "dd5443c7af3a475abb31af8a1b07e4f7"


def _probe_event(delivery: str = PROBE_DELIVERY) -> Event:
    """A synthetic probe event, shaped as the probe route normalizes one.

    Built here rather than driven through the probe route because the key is the
    *core's*: `_who` reads `Event.source`, so it must hold for any source at all, not
    just the ones that happen to exist today.
    """
    return Event(
        source="probe",
        kind=EventKind.SYNTHETIC_PROBE,
        recipient=Recipient(by="harness_key", value=NOVA.harness_key),
        wake_arg="BCNOC1 " + "0" * 32 + " " + "a" * 64,
        delivery_id=delivery,
    )


def _pipeline_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "basecradle_router.pipeline"]


def _uncoloured(line: str) -> str:
    """``line`` with every ANSI escape stripped — what a colour-blind consumer reads."""
    return _ANSI.sub("", line)


def _leading_token(line: str) -> str:
    """The line's first ``key=value`` token, whether or not the palette painted it."""
    return _uncoloured(line).split(" ", 1)[0]


def _wake_lines(caplog) -> list[str]:
    """Every pipeline line that is *about a wake* — the slow half, plus the retry line."""
    return [
        message
        for message in _pipeline_lines(caplog)
        if "event=wake_retry" in message
        or any(f"stage={stage.value} " in message for stage in _WAKE_STAGES)
    ]


def test_a_wake_line_names_the_source_the_delivery_arrived_on(caplog) -> None:
    # The headline of #222, on the one line the per-wake duration metric is lifted from.
    pipeline, _ = _pipeline()

    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        pipeline.handle("github", _github_request(delivery=DELIVERY_A))
    real = _stage_line(caplog, Stage.WAKE)
    assert "outcome=ok" in real
    assert "source=github" in real
    # `delivery=` identifies, and only identifies: it stays the high-cardinality join
    # key, and the kind marker is a field of its own — never a prefix hidden inside it.
    assert f"delivery={DELIVERY_A}" in real

    caplog.clear()
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        pipeline.execute(NOVA, _probe_event(), PipelineResult())
    probe = _stage_line(caplog, Stage.WAKE)
    assert "outcome=ok" in probe
    assert "source=probe" in probe
    assert f"delivery={PROBE_DELIVERY}" in probe


def test_no_pipeline_line_says_synthetic_at_all(caplog) -> None:
    # #222 retired the vocabulary outright ("in an AI fleet, everything is synthetic —
    # it answers no question"), so the retirement is pinned rather than merely done: a
    # line that carried BOTH keys would leave two answers to one question in the journal
    # and let a consumer quietly keep reading the retired one.
    pipeline, _ = _pipeline()
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        pipeline.handle("github", _github_request(delivery=DELIVERY_A))
        pipeline.execute(NOVA, _probe_event(), PipelineResult())

    for line in _pipeline_lines(caplog):
        assert "synthetic=" not in line, f"a retired key came back: {line}"


def test_the_source_value_set_is_exactly_the_registered_route_names() -> None:
    # `source=` invents no vocabulary: its value is `Event.source`, which a route sets
    # to its own `name`. So the closed set a dashboard groups by is the registry's own
    # names — and adding a route extends it without touching the core, which is the
    # core/routes split holding at the log layer too.
    registry = RouteRegistry()
    for route in (GithubRoute(TRUSTED_ACTORS), BasecradleRoute(), ProbeRoute()):
        registry.register(route)

    assert registry.names() == frozenset({"basecradle", "github", "probe"})
    assert {route.name for route in registry.routes() if route.synthetic} == {"probe"}


def _send_real(pipeline, *, dup: bool) -> None:
    """One real github handoff delivery — sharing an id with the last when ``dup``."""
    pipeline.handle("github", _github_request(delivery=DELIVERY_DUP if dup else None))


_probe_seq = itertools.count(1)


def _send_probe(pipeline, *, dup: bool) -> None:
    """One synthetic probe delivery, straight into the slow half the key lives in."""
    delivery = PROBE_DELIVERY if dup else f"{next(_probe_seq):032x}"
    pipeline.execute(NOVA, _probe_event(delivery), PipelineResult())


def test_every_line_about_a_wake_names_its_source_however_that_wake_ended(caplog) -> None:
    # A refused probe and a failed probe pollute a wake-failure count exactly as a
    # successful one pollutes a duration chart, so the key rides on every outcome — not
    # only the happy one. Each scenario is driven twice, over two different sources, and
    # between them they must cover every stage of the wake half.
    held = {"nova": WakeLockState.HELD}
    one_wake = BreakerConfig(max_wakes=1, window=60.0, cooldown=60.0)
    covered: set[str] = set()

    for send, expected in ((_send_real, "source=github"), (_send_probe, "source=probe")):
        # (name, pipeline, deliveries to send, whether they share one delivery id)
        scenarios = (
            ("a clean wake", _pipeline()[0], 1, False),
            ("a collapsed duplicate", _pipeline(deduper=DeliveryDeduper(ttl=600.0))[0], 2, True),
            ("a frozen agent", _pipeline(wake_lock=_StubWakeLock(held))[0], 1, False),
            ("a tripped breaker", _pipeline(breaker=WakeRateBreaker(one_wake))[0], 2, False),
            ("an exhausted wake", _pipeline(waker=_StubWaker(fail_times=99))[0], 1, False),
        )
        for name, pipeline, deliveries, dup in scenarios:
            caplog.clear()
            with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
                for _ in range(deliveries):
                    send(pipeline, dup=dup)

            lines = _wake_lines(caplog)
            assert lines, f"{expected}/{name}: no line about the wake was emitted at all"
            for line in lines:
                assert expected in line, f"{expected}/{name}: a wake line lost the source: {line}"
            covered.update(
                stage.value
                for stage in _WAKE_STAGES
                for line in lines
                if f"stage={stage.value} " in line
            )

    # Every stage of the wake half was actually exercised above — so the assertion
    # covers the whole surface rather than whichever part these scenarios happened to
    # reach, and a new stage cannot land unlabelled and untested.
    assert covered == {stage.value for stage in _WAKE_STAGES}


def test_both_halves_of_the_pipeline_name_the_source(caplog) -> None:
    # The point of reusing the router's own word rather than minting a new one: ONE key
    # now spans a delivery's whole trip, so `source=` groups the fast half's lines and
    # the slow half's alike and the two halves cannot drift into two vocabularies.
    pipeline, _ = _pipeline()
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        pipeline.handle("github", _github_request(delivery=DELIVERY_A))

    lines = _pipeline_lines(caplog)
    # The gates log only when they STOP a wake, so a clean trip is exactly these eight —
    # asserted by name so a stage that silently stopped logging is a failure here rather
    # than a vacuously-passing loop over whatever happened to be emitted. The two
    # bookends (#228) bracket the wake: `wake_start` before the attempt, `wake_end`
    # after the `stage=wake` record it closes over.
    assert [_leading_token(line) for line in lines] == [
        f"stage={stage.value}"
        for stage in (Stage.ROUTE, Stage.VERIFY, Stage.NORMALIZE, Stage.RESOLVE, Stage.LOCK)
    ] + ["event=wake_start", "stage=wake", "event=wake_end"]
    for line in lines:
        assert "source=github" in line, f"a pipeline line lost the source: {line}"


def test_a_retried_wake_names_its_source_on_every_attempt(caplog) -> None:
    # The retry WARNING is a per-wake line too: a flapping *probe* must not read as a
    # flapping agent on a wake-failure chart.
    pipeline, _ = _pipeline(waker=_StubWaker(fail_times=2))
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        pipeline.handle("github", _github_request(delivery=DELIVERY_A))

    retries = [r.getMessage() for r in caplog.records if "event=wake_retry" in r.getMessage()]
    assert len(retries) == 2
    assert all("source=github" in message for message in retries)


def test_the_journal_and_the_evidence_ledger_name_the_same_source(caplog) -> None:
    # One definition, two surfaces. The `source=` on the line and the `route` the
    # evidence store writes beside the very same outcome are the same string read from
    # the same `Event.source`, so the journal a dashboard charts and the ledger the
    # NOC's claims rest on can never disagree about where a wake came from. In-memory
    # store: no test writes to the box's state dir.
    evidence = EvidenceStore(None)
    pipeline, _ = _pipeline(evidence=evidence)

    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        pipeline.handle("github", _github_request(delivery=DELIVERY_B))
    assert "source=github" in _stage_line(caplog, Stage.WAKE)
    assert evidence.snapshot().agent_wakes[NOVA.harness_key].last_ok_route == "github"

    caplog.clear()
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        pipeline.execute(NOVA, _probe_event(), PipelineResult())
    assert "source=probe" in _stage_line(caplog, Stage.WAKE)
    assert evidence.snapshot().agent_wakes[NOVA.harness_key].last_ok_route == "probe"


# --- the wake lifecycle bookends, and the colour that reads them (#228) -----
#
# The router used to be SILENT at launch: its first word about a wake was the
# `stage=wake` line minutes later, at completion, so a Live Tail could not tell an
# agent hard at work from an agent that was never woken — the green-while-absent shape
# again. `event=wake_start` / `event=wake_end` bracket the wake for the human watching
# it; the `stage=wake` record stays as the pipeline machinery behind it (the same
# two-surface split Rails draws between `Started`/`Completed` and its instrumentation
# events). @origin decided the pair, the field order, and the palette on 2026-08-17.


def _bookends(caplog, phase: str) -> list[str]:
    """Every `event=wake_start` / `event=wake_end` line, painted as it will ship."""
    return [line for line in _pipeline_lines(caplog) if f"event={phase}" in _uncoloured(line)]


class _WatchingWaker(_StubWaker):
    """A waker that snapshots the journal at the moment it is called.

    The only way to prove `wake_start` lands *before the first attempt* rather than
    merely before the completion line: the start line must already be in the journal
    when the subprocess boundary is reached.
    """

    def __init__(self, caplog, **kwargs) -> None:
        super().__init__(**kwargs)
        self._caplog = caplog
        self.journal_at_call: list[list[str]] = []

    def wake(self, agent: Agent, event: Event) -> WakeResult:
        self.journal_at_call.append([r.getMessage() for r in self._caplog.records])
        return super().wake(agent, event)


def test_the_wake_start_line_is_emitted_before_the_first_attempt(caplog) -> None:
    waker = _WatchingWaker(caplog)
    pipeline, _ = _pipeline(waker=waker)
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        pipeline.handle("github", _github_request(delivery=DELIVERY_A))

    assert len(waker.journal_at_call) == 1
    already_logged = [_uncoloured(line) for line in waker.journal_at_call[0]]
    assert any(line.startswith("event=wake_start ") for line in already_logged), (
        f"the launch was still silent when the wake ran: {already_logged}"
    )
    # And it says WHICH wake is starting — a bare "starting" would be no better than
    # the silence it replaces once two agents are woken at once.
    start = _uncoloured(_bookends(caplog, "wake_start")[0])
    assert start == (
        f"event=wake_start delivery={DELIVERY_A} agent={NOVA.harness_key} source=github"
    )


def test_the_wake_end_line_closes_a_clean_wake_with_its_verdict(caplog) -> None:
    pipeline, _ = _pipeline(clock=_FakeClock(step=93.4))
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        pipeline.handle("github", _github_request(delivery=DELIVERY_A))

    end = _uncoloured(_bookends(caplog, "wake_end")[0])
    assert end == (
        f"event=wake_end delivery={DELIVERY_A} agent={NOVA.harness_key} source=github "
        "outcome=ok exit=0 duration=93.4s"
    )


def test_the_wake_end_line_closes_an_exhausted_wake_too(caplog) -> None:
    # A bracket must always close. Retry exhaustion is the other path that produces a
    # `stage=wake` record today, so it produces a `wake_end` too — and at WARNING, so a
    # bracket whose close was the one line filtered out of a warnings-only view cannot
    # happen.
    pipeline, _ = _pipeline(waker=_StubWaker(fail_times=99), clock=_FakeClock(step=4.1))
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        pipeline.handle("github", _github_request(delivery=DELIVERY_B))

    record = next(r for r in caplog.records if "event=wake_end" in _uncoloured(r.getMessage()))
    assert record.levelname == "WARNING"
    end = _uncoloured(record.getMessage())
    assert end.startswith(
        f"event=wake_end delivery={DELIVERY_B} agent={NOVA.harness_key} source=github "
        "outcome=failed attempts=3 duration=4.1s error="
    )
    assert "transient blip" in end


def test_the_bracket_closes_even_when_the_wake_path_raises_unexpectedly(caplog) -> None:
    # The one path that reaches the close with no verdict of its own: a bug (not a
    # WakeError) escaping to the caller's last-resort handler. An unclosed `wake_start`
    # would read, forever, as a wake still running — the worst possible lie for a line
    # whose whole job is saying whether an agent is awake.
    class _ExplodingWaker(_StubWaker):
        def wake(self, agent: Agent, event: Event) -> WakeResult:
            raise RuntimeError("the wake path is broken")

    pipeline, _ = _pipeline(waker=_ExplodingWaker())
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        result = pipeline.handle("github", _github_request(delivery=DELIVERY_A))

    assert (Stage.WAKE, Outcome.FAILED) in result.stages  # the daemon survived it
    assert len(_bookends(caplog, "wake_start")) == 1
    end = _uncoloured(_bookends(caplog, "wake_end")[0])
    assert end.startswith(f"event=wake_end delivery={DELIVERY_A} agent={NOVA.harness_key} ")
    assert "outcome=failed" in end


def test_both_halves_of_the_bracket_carry_the_same_field_prefix(caplog) -> None:
    # The close mirrors the open, so the eye reading a Live Tail matches them on sight
    # and a query joins them on the same keys. Guaranteed by construction — one emitter
    # renders one `_who` mapping for both halves — and pinned here so a second, drifting
    # order cannot be introduced.
    for waker in (_StubWaker(), _StubWaker(fail_times=99)):
        caplog.clear()
        pipeline, _ = _pipeline(waker=waker)
        with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
            pipeline.handle("github", _github_request(delivery=DELIVERY_A))

        start = _uncoloured(_bookends(caplog, "wake_start")[0]).split(" ", 1)[1]
        end = _uncoloured(_bookends(caplog, "wake_end")[0]).split(" ", 1)[1]
        assert end.startswith(start), f"the close does not mirror the open:\n{start}\n{end}"


def test_a_probe_is_never_bracketed(caplog) -> None:
    # A probe never launches the agent — the wake-runner acks after the privilege drop
    # without ever exec'ing the model — so bracketing one would put a start and an end
    # around nothing and blur the exact signal the pair exists to show. The gate is
    # `Event.synthetic`, the same property that already decides the attempt count.
    pipeline, _ = _pipeline()
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        pipeline.execute(NOVA, _probe_event(), PipelineResult())

    assert _bookends(caplog, "wake_start") == []
    assert _bookends(caplog, "wake_end") == []
    # ...but the machinery line is unchanged: a probe still proves the edge.
    assert "outcome=ok" in _uncoloured(_stage_line(caplog, Stage.WAKE))


def test_the_slow_half_leads_with_the_delivery_then_the_agent_then_the_source(caplog) -> None:
    # @origin's field-order decision: the line is ordered for the human reading it, not
    # the query reading it. The delivery id is what a person copies to follow one wake
    # through the router and on into the agent's own journal, so it leads — uniformly,
    # across every line the slow half emits.
    pipeline, _ = _pipeline(waker=_StubWaker(fail_times=2))
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        pipeline.handle("github", _github_request(delivery=DELIVERY_A))

    slow = [
        _uncoloured(line)
        for line in _pipeline_lines(caplog)
        if "event=wake_" in _uncoloured(line)
        or any(f"stage={stage.value} " in _uncoloured(line) for stage in _WAKE_STAGES)
    ]
    assert slow, "no line of the slow half was emitted at all"
    prefix = rf"delivery={DELIVERY_A} agent={NOVA.harness_key} source=github"
    for line in slow:
        assert re.search(prefix, line), f"a slow-half line is out of order: {line}"


def test_the_verdict_tokens_are_painted_with_the_fleet_palette(caplog) -> None:
    # Green for a wake that started and a stage that succeeded, blue for the bookend's
    # own identity, red for a failure — the colours @origin decided, applied at the
    # journal-emission boundary. Both a clean wake and a failing one are driven, since
    # they are the two verdict colours.
    for waker in (_StubWaker(), _StubWaker(fail_times=99)):
        pipeline, _ = _pipeline(waker=waker)
        with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
            pipeline.handle("github", _github_request())
    lines = _pipeline_lines(caplog)

    assert any(line.startswith(f"{GREEN}event=wake_start{RESET} ") for line in lines)
    assert any(line.startswith(f"{BLUE}event=wake_end{RESET} ") for line in lines)
    assert any(f"{GREEN}outcome=ok{RESET}" in line for line in lines)
    assert any(f"{RED}outcome=failed{RESET}" in line for line in lines)
    assert any(f"{YELLOW}event=wake_retry{RESET} " in line for line in lines)


def test_a_painted_line_is_still_searchable_token_by_token(caplog) -> None:
    # The token-integrity rule, asserted on the SHIPPED bytes rather than on `paint` in
    # isolation: every token an operator or an extraction regex looks for must survive
    # colour intact. (A consumer matching ACROSS the space between two tokens —
    # `stage=wake outcome=` — is the one thing colour does move, which is why the NOC's
    # extraction is re-pointed in lockstep with the deploy; see #228.)
    pipeline, _ = _pipeline()
    with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
        pipeline.handle("github", _github_request(delivery=DELIVERY_A))
    lines = _pipeline_lines(caplog)

    for token in ("event=wake_start", "event=wake_end", "outcome=ok", f"delivery={DELIVERY_A}"):
        assert any(token in line for line in lines), f"colour broke the search for {token!r}"


def test_colour_never_reaches_the_records_the_status_surface_is_built_from(caplog) -> None:
    # The containment constraint. `StageRecord.detail` is data the admin/status API
    # serves; the journal line is that same data plus presentation. They agree on every
    # VALUE — an escape byte in a JSON payload would be a rendering artefact leaking
    # into a machine surface, and would make the two disagree about what a stage said.
    for waker in (_StubWaker(), _StubWaker(fail_times=99)):
        pipeline, _ = _pipeline(waker=waker)
        with caplog.at_level("INFO", logger="basecradle_router.pipeline"):
            result = pipeline.handle("github", _github_request())
        # a rejection too — `outcome=rejected` is a painted token on the journal side
        rejected = pipeline.handle("github", _github_request(sign=False))

        for record in (*result.records, *rejected.records):
            assert "\x1b" not in record.detail, f"colour leaked into {record.stage}: {record!r}"
            assert "\x1b" not in record.stage.value
            assert "\x1b" not in record.outcome.value
    # ...while the journal line for that same rejection IS painted.
    assert any(f"{RED}outcome=rejected{RESET}" in line for line in _pipeline_lines(caplog))

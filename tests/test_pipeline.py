"""The core pipeline driven offline end-to-end, every collaborator mocked.

A fabricated, correctly-signed handoff flows through verify → normalize →
resolve → lock → wake; the ordered stage outcomes are asserted. The pipeline
ends at the wake — the router never merges (the agent enables GitHub native
auto-merge on its own PR; see issue #38). No network, model, or live agent.
Test cast: John Doe (human) hands off; Nova Digital (``nova``, AI) is woken.
"""

import itertools
import json
from types import MappingProxyType

from basecradle_router.breaker import BreakerConfig, WakeRateBreaker
from basecradle_router.config import Config
from basecradle_router.dedup import DeliveryDeduper
from basecradle_router.models import Agent, Event, WakeKind
from basecradle_router.pipeline import Outcome, Pipeline, Stage
from basecradle_router.routes import BasecradleRoute, InboundRequest, RouteRegistry
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
    return Pipeline(**kwargs), waker


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


def test_a_timed_out_wake_releases_the_lock_and_a_second_delivery_proceeds() -> None:
    # #135: a timed-out wake surfaces as a WakeError (the boundary maps the
    # subprocess timeout to one). The lock the hung wake held MUST be released, or
    # that agent — and, once the thread pool saturates, the whole fleet — wedges.
    from basecradle_router.concurrency import AgentLocks

    locks = AgentLocks()
    waker = _StubWaker(fail_times=99)  # every attempt raises, as a persistent timeout would
    pipeline, _ = _pipeline(waker=waker, locks=locks)

    first = pipeline.handle("github", _github_request())
    assert first.stages[-1] == (Stage.WAKE, Outcome.FAILED)
    # The lock is free again — the guard releases it even though the wake raised.
    assert locks.acquire(NOVA.harness_key, blocking=False) is True
    locks.release(NOVA.harness_key)

    # A fresh delivery for the same agent is not blocked behind the dead wake: it
    # reaches the wake stage and runs (no lingering hold from the timed-out one).
    before = len(waker.calls)
    second = pipeline.handle("github", _github_request())
    assert second.stages[-1] == (Stage.WAKE, Outcome.FAILED)
    assert len(waker.calls) > before

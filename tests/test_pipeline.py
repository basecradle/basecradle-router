"""The core pipeline driven offline end-to-end, every collaborator mocked.

A fabricated, correctly-signed handoff flows through verify → normalize →
resolve → lock → wake; the ordered stage outcomes are asserted. The pipeline
ends at the wake — the router never merges (the agent enables GitHub native
auto-merge on its own PR; see issue #38). No network, model, or live agent.
Test cast: John Doe (human) hands off; Nova Digital (``nova``, AI) is woken.
"""

import json
from types import MappingProxyType

from basecradle_router.config import Config
from basecradle_router.models import Agent, Event
from basecradle_router.pipeline import Outcome, Pipeline, Stage
from basecradle_router.routes import InboundRequest, RouteRegistry
from basecradle_router.routes.github import GithubRoute
from basecradle_router.wake import WakeError, WakeResult

SECRET = "whsec_" + "0" * 32
HANDOFF_SENDER = "john"  # John Doe, a trusted human org member, files the handoff
TRUSTED_ACTORS = frozenset({HANDOFF_SENDER})
UNTRUSTED_SENDER = "drive-by-stranger"
NOVA = Agent(
    repo="basecradle/basecradle-python",
    os_user="nova",
    clone_path="/home/nova/basecradle-python",
    bot_slug="basecradle-python-ai",
)
ISSUE_URL = "https://github.com/basecradle/basecradle-python/issues/42"


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
        agents=MappingProxyType({NOVA.repo: NOVA} if agents is None else agents),
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
    return Pipeline(**kwargs), waker


def _github_request(
    *,
    action: str = "opened",
    labels: tuple[str, ...] = ("handoff",),
    repo: str = "basecradle/basecradle-python",
    event: str = "issues",
    sign: bool = True,
    delivery: str = "0192f3a4-5b6c-7d8e-9f01-23456789abcd",
    sender: str = HANDOFF_SENDER,
) -> InboundRequest:
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
    assert woken_event.trigger.startswith(f"Cross-repo handoff: work {ISSUE_URL}\n")
    assert result.agent is NOVA


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
        agents=MappingProxyType({NOVA.repo: NOVA}),
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

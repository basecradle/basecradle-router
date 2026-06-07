"""End-to-end: a signed GitHub handoff webhook → the ASGI server → a woken agent.

The capstone. Everything but the code under test is fabricated and mocked — the
transport (the ASGI app is driven in-process), the GitHub API (the merger), and
the ``claude`` invocation (the waker). No network, no model, no live agent: the
human courier is gone.

Cast: John Doe (``john``, human) files a handoff; Nova Digital (``nova``, AI) is
woken in her own clone.
"""

import asyncio
import hashlib
import hmac
import json
import threading
from types import MappingProxyType

from basecradle_router.concurrency import RepoLocks
from basecradle_router.config import Config
from basecradle_router.merge_policy import MergePolicy, PullRequest
from basecradle_router.models import Agent, Event
from basecradle_router.pipeline import Pipeline
from basecradle_router.routes import RouteRegistry
from basecradle_router.routes.github import GithubRoute
from basecradle_router.server import WebhookServer
from basecradle_router.wake import WakeResult

SECRET = "whsec_" + "0" * 32
HANDOFF_SENDER = "john"  # John Doe, a trusted human org member, files the handoff
TRUSTED_ACTORS = frozenset({HANDOFF_SENDER})
NOVA = Agent(
    repo="basecradle/basecradle-python",
    os_user="nova",
    clone_path="/home/nova/basecradle-python",
    bot_slug="basecradle-python-ai",
)
ISSUE_URL = "https://github.com/basecradle/basecradle-python/issues/42"


class _RecordingWaker:
    def __init__(self) -> None:
        self.calls: list[tuple[Agent, Event]] = []

    def wake(self, agent: Agent, event: Event) -> WakeResult:
        self.calls.append((agent, event))
        return WakeResult(exit_code=0, stdout="opened PR")


class _RecordingMerger:
    def __init__(self) -> None:
        self.merged: list[PullRequest] = []

    def merge(self, pr: PullRequest) -> None:
        self.merged.append(pr)


def _config() -> Config:
    return Config(
        agents=MappingProxyType({NOVA.repo: NOVA}),
        enabled_routes=frozenset({"github"}),
        webhook_secrets=MappingProxyType({"github": SECRET}),
    )


def _registry() -> RouteRegistry:
    registry = RouteRegistry()
    registry.register(GithubRoute(TRUSTED_ACTORS))
    return registry


def _build(*, waker=None, merger=None, pr_provider=None, locks=None):
    waker = waker or _RecordingWaker()
    merger = merger or _RecordingMerger()
    kwargs = dict(
        registry=_registry(),
        config=_config(),
        waker=waker,
        merge_policy=MergePolicy(merger),
        sleep=lambda _d: None,
    )
    if pr_provider is not None:
        kwargs["pr_provider"] = pr_provider
    if locks is not None:
        kwargs["locks"] = locks
    server = WebhookServer(Pipeline(**kwargs))
    return server, waker, merger


def _signed_body(
    *,
    action: str = "opened",
    labels: tuple[str, ...] = ("handoff",),
    repo: str = "basecradle/basecradle-python",
    sender: str = HANDOFF_SENDER,
) -> tuple[bytes, dict[str, str]]:
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
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": "0192f3a4-5b6c-7d8e-9f01-23456789abcd",
        "X-Hub-Signature-256": f"sha256={digest}",
        "Content-Type": "application/json",
    }
    return body, headers


def _post(server: WebhookServer, path: str, body: bytes, headers: dict[str, str]):
    """Drive the ASGI app in-process and return (status, parsed-json-body)."""

    async def _drive():
        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [
                (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
            ],
        }
        incoming = [{"type": "http.request", "body": body, "more_body": False}]
        sent: list[dict] = []

        async def receive():
            return incoming.pop(0)

        async def send(message):
            sent.append(message)

        await server(scope, receive, send)
        # The response is sent from the fast accept half; the wake runs in the
        # background. Drain it so post-wake state (the woken agent, the merge) is
        # observable — without changing that the status was acked before the wake.
        await server.drain()
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        raw = next(m["body"] for m in sent if m["type"] == "http.response.body")
        return status, json.loads(raw)

    return asyncio.run(_drive())


def _get(server: WebhookServer, path: str):
    """Drive a GET through the ASGI app; return (status, content-type, raw body)."""

    async def _drive():
        scope = {"type": "http", "method": "GET", "path": path, "headers": []}
        incoming = [{"type": "http.request", "body": b"", "more_body": False}]
        sent: list[dict] = []

        async def receive():
            return incoming.pop(0)

        async def send(message):
            sent.append(message)

        await server(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        body = next(m["body"] for m in sent if m["type"] == "http.response.body")
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in start["headers"]}
        return start["status"], headers.get("content-type", ""), body

    return asyncio.run(_drive())


# --- the capstone: no human courier ----------------------------------------


def test_signed_handoff_wakes_the_target_agent_no_human_courier() -> None:
    server, waker, _ = _build()
    body, headers = _signed_body()

    status, summary = _post(server, "/webhooks/github", body, headers)

    assert status == 202  # fast-acked: accepted and now processing in the background
    assert summary["outcome"] == "ok"  # the accept half (through resolve) succeeded
    # The right agent was woken with the right trigger — directly, no relay.
    assert len(waker.calls) == 1
    agent, event = waker.calls[0]
    assert agent is NOVA
    assert agent.os_user == "nova"
    assert agent.clone_path == "/home/nova/basecradle-python"
    assert event.trigger == f"Cross-repo handoff: work {ISSUE_URL}"


def test_green_pr_auto_merges_end_to_end() -> None:
    def green_pr(_a, _e, _w) -> PullRequest:
        return PullRequest(number=99, repo="basecradle/basecradle-python", ci_green=True)

    server, _, merger = _build(pr_provider=green_pr)
    body, headers = _signed_body()
    status, _ = _post(server, "/webhooks/github", body, headers)

    # The merge happens in the background, after the ack — so its outcome is not in
    # the 202 body; we observe it through the (mocked) merger instead. Per Earned
    # Autonomy a green PR auto-merges with no per-PR human gate.
    assert status == 202
    assert len(merger.merged) == 1
    assert merger.merged[0].number == 99


def test_red_pr_is_not_merged_end_to_end() -> None:
    def red_pr(_a, _e, _w) -> PullRequest:
        return PullRequest(number=99, repo="basecradle/basecradle-python", ci_green=False)

    server, _, merger = _build(pr_provider=red_pr)
    body, headers = _signed_body()
    status, _ = _post(server, "/webhooks/github", body, headers)

    assert status == 202
    assert merger.merged == []  # not green → not merged yet (no human pause, just CI)


def test_non_handoff_event_is_ignored_end_to_end() -> None:
    server, waker, _ = _build()
    body, headers = _signed_body(labels=("bug",))
    status, summary = _post(server, "/webhooks/github", body, headers)

    assert status == 200
    assert summary["outcome"] == "ignored"
    assert waker.calls == []


def test_handoff_from_untrusted_sender_is_rejected_end_to_end() -> None:
    # A correctly-signed handoff whose actor is not on the fleet allow-list is
    # rejected (400, malformed/unacceptable) and wakes no agent — the trust gate
    # holds through the full server stack, not just the route in isolation.
    server, waker, _ = _build()
    body, headers = _signed_body(sender="drive-by-stranger")
    status, summary = _post(server, "/webhooks/github", body, headers)

    assert status == 400
    assert summary["outcome"] == "rejected"
    assert waker.calls == []


def test_bad_signature_is_unauthorized_end_to_end() -> None:
    server, waker, _ = _build()
    body, headers = _signed_body()
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64  # wrong digest
    status, _ = _post(server, "/webhooks/github", body, headers)

    assert status == 401
    assert waker.calls == []


def test_unknown_source_is_not_found() -> None:
    server, _, _ = _build()
    body, headers = _signed_body()
    status, _ = _post(server, "/webhooks/gitlab", body, headers)
    assert status == 404  # path is a webhook, but no such source is registered


def test_non_webhook_path_is_not_found() -> None:
    server, _, _ = _build()
    status, _ = _post(server, "/not-a-webhook", b"", {})
    assert status == 404


# --- /up: the fleet-uniform liveness endpoint ------------------------------


def test_up_is_the_fleet_liveness_endpoint() -> None:
    server, _, _ = _build()
    status, content_type, body = _get(server, "/up")

    assert status == 200
    assert content_type.startswith("text/html")
    # Byte-for-byte the Rails health body, so basecradle.com/up and
    # ai.basecradle.com/up are indistinguishable to any checker.
    assert body == b'<!DOCTYPE html><html><body style="background-color: green"></body></html>'


def test_up_is_the_only_liveness_path_no_competing_healthz() -> None:
    # /up is the single public liveness contract; /healthz is deliberately not a
    # route, so the fleet never has two competing health endpoints to keep in sync.
    server, _, _ = _build()
    status, _, _ = _get(server, "/healthz")
    assert status == 404


def test_up_does_not_wake_anything() -> None:
    # A liveness probe must never touch the pipeline — no agent woken, no merge.
    server, waker, merger = _build()
    _get(server, "/up")
    assert waker.calls == []
    assert merger.merged == []


def test_unregistered_repo_is_accepted_then_logged_not_a_retry_storm() -> None:
    # A valid, signed handoff for a repo we don't manage: accepted (2xx) and
    # recorded as a failed resolve — never a 5xx that GitHub would retry forever.
    server, waker, _ = _build()
    body, headers = _signed_body(repo="basecradle/not-managed")
    status, summary = _post(server, "/webhooks/github", body, headers)

    assert status == 200
    assert summary["outcome"] == "failed"
    assert summary["stages"][-1] == ["resolve", "failed"]
    assert waker.calls == []


# --- fast-ack: the response precedes the wake ------------------------------


def test_webhook_is_fast_acked_without_waiting_for_the_wake() -> None:
    # A wake takes minutes; GitHub abandons the delivery after ~10s. So the 202
    # must be sent *while the wake is still running*. We block the wake on an event
    # we control and assert the ack is observed before the wake completes. The
    # discriminator is `wake_done`: a correct server reaches the assertion with the
    # wake still blocked (not done); a server that awaited the wake before acking
    # would only get there after the wake finished — and fail the assertion.
    release = threading.Event()
    wake_done = threading.Event()

    class _BlockingWaker:
        def __init__(self) -> None:
            self.calls: list[tuple[Agent, Event]] = []

        def wake(self, agent: Agent, event: Event) -> WakeResult:
            self.calls.append((agent, event))
            release.wait(timeout=5)  # safety net so a broken test can never hang CI
            wake_done.set()
            return WakeResult(exit_code=0)

    waker = _BlockingWaker()
    server, _, _ = _build(waker=waker)
    body, headers = _signed_body()

    async def _drive() -> int:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/webhooks/github",
            "headers": [
                (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
            ],
        }
        incoming = [{"type": "http.request", "body": body, "more_body": False}]
        sent: list[dict] = []

        async def receive():
            return incoming.pop(0)

        async def send(message):
            sent.append(message)

        await server(scope, receive, send)
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        # The wake is still blocked on `release`, so it cannot have completed — the
        # ack provably preceded it. (A blocking-ack server would only reach here
        # after the wake's own timeout fired and set wake_done.)
        assert not wake_done.is_set()
        release.set()  # let the backgrounded wake finish, then drain it cleanly
        await server.drain()
        assert wake_done.is_set()  # it did run — in the background, after the ack
        return status

    assert asyncio.run(_drive()) == 202
    assert len(waker.calls) == 1


# --- the per-repo lock prevents a concurrent double-wake -------------------


def test_lock_prevents_a_concurrent_double_wake_end_to_end() -> None:
    locks = RepoLocks()
    in_wake = threading.Event()
    release = threading.Event()

    class _BlockingWaker:
        def wake(self, agent: Agent, event: Event) -> WakeResult:
            in_wake.set()
            release.wait(timeout=5)
            return WakeResult(exit_code=0)

    server, _, _ = _build(waker=_BlockingWaker(), locks=locks)
    body, headers = _signed_body()

    worker = threading.Thread(target=lambda: _post(server, "/webhooks/github", body, headers))
    worker.start()
    assert in_wake.wait(timeout=5)  # the first wake is running, holding the repo lock

    # While that wake runs, the repo is locked — a second wake could not start.
    assert locks.acquire(NOVA.repo, blocking=False) is False

    release.set()
    worker.join(timeout=5)
    assert locks.acquire(NOVA.repo, blocking=False) is True  # released afterwards
    locks.release(NOVA.repo)

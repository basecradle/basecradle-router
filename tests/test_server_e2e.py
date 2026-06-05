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
from basecradle_router.merge_policy import MergeDecision, MergePolicy, PullRequest
from basecradle_router.models import Agent, Event
from basecradle_router.pipeline import Pipeline
from basecradle_router.routes import RouteRegistry
from basecradle_router.routes.github import GithubRoute
from basecradle_router.server import WebhookServer
from basecradle_router.wake import WakeResult

SECRET = "whsec_" + "0" * 32
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
    registry.register(GithubRoute())
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
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        raw = next(m["body"] for m in sent if m["type"] == "http.response.body")
        return status, json.loads(raw)

    return asyncio.run(_drive())


# --- the capstone: no human courier ----------------------------------------


def test_signed_handoff_wakes_the_target_agent_no_human_courier() -> None:
    server, waker, _ = _build()
    body, headers = _signed_body()

    status, summary = _post(server, "/webhooks/github", body, headers)

    assert status == 200
    assert summary["outcome"] == "ok"
    # The right agent was woken with the right trigger — directly, no relay.
    assert len(waker.calls) == 1
    agent, event = waker.calls[0]
    assert agent is NOVA
    assert agent.os_user == "nova"
    assert agent.clone_path == "/home/nova/basecradle-python"
    assert event.trigger == f"Cross-repo handoff: work {ISSUE_URL}"


def test_low_risk_pr_auto_merges_end_to_end() -> None:
    def docs_pr(_a, _e, _w) -> PullRequest:
        return PullRequest(
            number=99,
            repo="basecradle/basecradle-python",
            changed_paths=("README.md",),
            labels=frozenset(),
            ci_green=True,
        )

    server, _, merger = _build(pr_provider=docs_pr)
    body, headers = _signed_body()
    status, summary = _post(server, "/webhooks/github", body, headers)

    assert status == 200
    assert summary["decision"] == MergeDecision.AUTO_MERGE.value
    assert len(merger.merged) == 1


def test_gated_handoff_pauses_end_to_end() -> None:
    def gated_pr(_a, _e, _w) -> PullRequest:
        return PullRequest(
            number=99,
            repo="basecradle/basecradle-python",
            changed_paths=("CLAUDE.md",),  # unmarked charter edit → scope gate
            labels=frozenset(),
            ci_green=True,
        )

    server, _, merger = _build(pr_provider=gated_pr)
    body, headers = _signed_body()
    status, summary = _post(server, "/webhooks/github", body, headers)

    assert status == 200
    assert summary["decision"] == MergeDecision.PAUSE.value
    assert merger.merged == []  # a gate never auto-merges


def test_non_handoff_event_is_ignored_end_to_end() -> None:
    server, waker, _ = _build()
    body, headers = _signed_body(labels=("bug",))
    status, summary = _post(server, "/webhooks/github", body, headers)

    assert status == 200
    assert summary["outcome"] == "ignored"
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
    status, _ = _post(server, "/healthz", b"", {})
    assert status == 404


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

"""End-to-end: a signed GitHub handoff webhook → the ASGI server → a woken agent.

The capstone. Everything but the code under test is fabricated and mocked — the
transport (the ASGI app is driven in-process) and the ``claude`` invocation (the
waker). No network, no model, no live agent: the human courier is gone. The
pipeline ends at the wake — the router never merges (the woken agent enables
GitHub native auto-merge on its own PR; see issue #38).

Cast: John Doe (``john``, human) files a handoff; Nova Digital (``nova``, AI) is
woken in her own clone.
"""

import asyncio
import hashlib
import hmac
import io
import json
import logging
import threading
from types import MappingProxyType

from basecradle_router.breaker import BreakerConfig, WakeRateBreaker
from basecradle_router.concurrency import AgentLocks
from basecradle_router.config import Config
from basecradle_router.dedup import DeliveryDeduper
from basecradle_router.models import Agent, Event
from basecradle_router.pipeline import Pipeline
from basecradle_router.routes import RouteRegistry
from basecradle_router.routes.github import GithubRoute
from basecradle_router.server import WebhookServer, configure_logging, deployed_sha
from basecradle_router.wake import WakeResult

SECRET = "whsec_" + "0" * 32
HANDOFF_SENDER = "john"  # John Doe, a trusted human org member, files the handoff
TRUSTED_ACTORS = frozenset({HANDOFF_SENDER})
NOVA = Agent(
    key="basecradle/basecradle-python",
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


def _config() -> Config:
    return Config(
        agents=MappingProxyType({NOVA.key: NOVA}),
        enabled_routes=frozenset({"github"}),
        webhook_secrets=MappingProxyType({"github": SECRET}),
    )


def _registry() -> RouteRegistry:
    registry = RouteRegistry()
    registry.register(GithubRoute(TRUSTED_ACTORS))
    return registry


def _build(*, waker=None, locks=None):
    waker = waker or _RecordingWaker()
    kwargs = dict(
        registry=_registry(),
        config=_config(),
        waker=waker,
        sleep=lambda _d: None,
    )
    if locks is not None:
        kwargs["locks"] = locks
    server = WebhookServer(Pipeline(**kwargs))
    return server, waker


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
        # background. Drain it so post-wake state (the woken agent) is observable
        # — without changing that the status was acked before the wake.
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
    server, waker = _build()
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
    # Leads with the verbatim recognition marker; the quarantine envelope follows
    # (asserted in detail in test_github_route).
    assert event.wake_arg.startswith(f"Cross-repo handoff: work {ISSUE_URL}\n")


def test_non_handoff_event_is_ignored_end_to_end() -> None:
    server, waker = _build()
    body, headers = _signed_body(labels=("bug",))
    status, summary = _post(server, "/webhooks/github", body, headers)

    assert status == 200
    assert summary["outcome"] == "ignored"
    assert waker.calls == []


def test_handoff_from_untrusted_sender_is_rejected_end_to_end() -> None:
    # A correctly-signed handoff whose actor is not on the fleet allow-list is
    # rejected (400, malformed/unacceptable) and wakes no agent — the trust gate
    # holds through the full server stack, not just the route in isolation.
    server, waker = _build()
    body, headers = _signed_body(sender="drive-by-stranger")
    status, summary = _post(server, "/webhooks/github", body, headers)

    assert status == 400
    assert summary["outcome"] == "rejected"
    assert waker.calls == []


def test_bad_signature_is_unauthorized_end_to_end() -> None:
    server, waker = _build()
    body, headers = _signed_body()
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64  # wrong digest
    status, _ = _post(server, "/webhooks/github", body, headers)

    assert status == 401
    assert waker.calls == []


def test_unknown_source_is_not_found() -> None:
    server, _ = _build()
    body, headers = _signed_body()
    status, _ = _post(server, "/webhooks/gitlab", body, headers)
    assert status == 404  # path is a webhook, but no such source is registered


def test_non_webhook_path_is_not_found() -> None:
    server, _ = _build()
    status, _ = _post(server, "/not-a-webhook", b"", {})
    assert status == 404


# --- /up: the fleet-uniform liveness endpoint ------------------------------


def test_up_is_the_fleet_liveness_endpoint() -> None:
    server, _ = _build()
    status, content_type, body = _get(server, "/up")

    assert status == 200
    assert content_type.startswith("text/html")
    # Byte-for-byte the Rails health body, so basecradle.com/up and
    # ai.basecradle.com/up are indistinguishable to any checker.
    assert body == b'<!DOCTYPE html><html><body style="background-color: green"></body></html>'


def test_up_is_the_only_liveness_path_no_competing_healthz() -> None:
    # /up is the single public liveness contract; /healthz is deliberately not a
    # route, so the fleet never has two competing health endpoints to keep in sync.
    server, _ = _build()
    status, _, _ = _get(server, "/healthz")
    assert status == 404


def test_up_does_not_wake_anything() -> None:
    # A liveness probe must never touch the pipeline — no agent woken.
    server, waker = _build()
    _get(server, "/up")
    assert waker.calls == []


def test_unregistered_repo_is_accepted_then_logged_not_a_retry_storm() -> None:
    # A valid, signed handoff for a repo we don't manage: accepted (2xx) and
    # recorded as a failed resolve — never a 5xx that GitHub would retry forever.
    server, waker = _build()
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
    server, _ = _build(waker=waker)
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


# --- the per-agent lock prevents a concurrent double-wake ------------------


def test_lock_prevents_a_concurrent_double_wake_end_to_end() -> None:
    locks = AgentLocks()
    in_wake = threading.Event()
    release = threading.Event()

    class _BlockingWaker:
        def wake(self, agent: Agent, event: Event) -> WakeResult:
            in_wake.set()
            release.wait(timeout=5)
            return WakeResult(exit_code=0)

    server, _ = _build(waker=_BlockingWaker(), locks=locks)
    body, headers = _signed_body()

    worker = threading.Thread(target=lambda: _post(server, "/webhooks/github", body, headers))
    worker.start()
    assert in_wake.wait(timeout=5)  # the first wake is running, holding the agent lock

    # While that wake runs, the agent is locked — a second wake could not start.
    assert locks.acquire(NOVA.harness_key, blocking=False) is False

    release.set()
    worker.join(timeout=5)
    assert locks.acquire(NOVA.harness_key, blocking=False) is True  # released afterwards
    locks.release(NOVA.harness_key)


# --- the daemon's own INFO observability actually reaches stdout (#91) ------
#
# The deployed-box gap: the decision lines and pipeline stage records log at INFO
# under `basecradle_router.*`, but nothing configured those loggers, so on the box
# (where uvicorn configures only its own loggers) every INFO record was dropped and
# the observability was itself silent live. These pin the fix.


def _with_clean_package_logger(fn):
    """Run ``fn`` with the package logger's handlers/level snapshotted and restored.

    configure_logging mutates the process-global ``basecradle_router`` logger; the
    snapshot keeps that mutation from leaking into other tests.
    """
    pkg = logging.getLogger("basecradle_router")
    saved_handlers, saved_level = pkg.handlers[:], pkg.level
    pkg.handlers.clear()
    try:
        fn(pkg)
    finally:
        pkg.handlers[:] = saved_handlers
        pkg.setLevel(saved_level)


def test_configure_logging_emits_info_records_to_the_stream() -> None:
    def check(pkg: logging.Logger) -> None:
        buffer = io.StringIO()
        configure_logging(stream=buffer)
        assert pkg.level == logging.INFO
        # A child logger's INFO record (a decision line) must reach the handler —
        # this is exactly what was dropped at WARNING on the box.
        logging.getLogger("basecradle_router.routes").info("delivery decision=woke")
        assert "delivery decision=woke" in buffer.getvalue()

    _with_clean_package_logger(check)


def test_configure_logging_is_idempotent_no_duplicate_handlers() -> None:
    def check(pkg: logging.Logger) -> None:
        configure_logging(stream=io.StringIO())
        configure_logging(stream=io.StringIO())  # a second startup must not stack
        tagged = [h for h in pkg.handlers if getattr(h, "_basecradle_router", False)]
        assert len(tagged) == 1

    _with_clean_package_logger(check)


def test_lifespan_startup_configures_logging() -> None:
    # The wiring: uvicorn drives the ASGI lifespan on real startup, which is where
    # the daemon configures its own logging — so the running process is observable
    # even though no unit test (HTTP-only) ever triggers it.
    def check(pkg: logging.Logger) -> None:
        server, _ = _build()

        async def _drive() -> None:
            incoming = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
            sent: list[dict] = []

            async def receive():
                return incoming.pop(0)

            async def send(message):
                sent.append(message)

            await server({"type": "lifespan"}, receive, send)
            assert {"type": "lifespan.startup.complete"} in sent

        asyncio.run(_drive())
        assert pkg.level == logging.INFO
        assert any(getattr(h, "_basecradle_router", False) for h in pkg.handlers)

    _with_clean_package_logger(check)


# --- observability: the startup banner and the /up access-log noise (#170) ---
#
# Two live-only defects the audit found. (1) The config a *running* router booted
# with — its routes, its dedup TTL, its breaker thresholds, and above all WHICH
# COMMIT it is — was unknowable from its own logs. (2) Better Stack's uptime monitor
# probes `/up` about once a minute forever, and every probe landed in the journal and
# in Live Tail as pure noise.


def _with_clean_access_logger(fn):
    """Snapshot/restore uvicorn's access-logger filters — configure_logging adds one."""
    access = logging.getLogger("uvicorn.access")
    saved = access.filters[:]
    access.filters.clear()
    try:
        fn(access)
    finally:
        access.filters[:] = saved


def _access_record(path: str) -> logging.LogRecord:
    """A record shaped exactly as uvicorn's access logger emits one."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", "GET", path, "1.1", 200),
        exc_info=None,
    )


def test_configure_logging_suppresses_the_up_access_line_and_nothing_else() -> None:
    def check(access: logging.Logger) -> None:
        _with_clean_package_logger(lambda _pkg: configure_logging(stream=io.StringIO()))
        assert access.filter(_access_record("/up")) is False  # the monitor's probe: dropped
        assert access.filter(_access_record("/up?x=1")) is False  # query string and all
        # Everything else still logs — this is a scalpel, not a mute button.
        assert access.filter(_access_record("/webhooks/github")) is True
        assert access.filter(_access_record("/upload")) is True  # a prefix match would be a bug

    _with_clean_access_logger(check)


def test_the_up_filter_keeps_a_record_it_cannot_parse() -> None:
    # Fail-direction: if uvicorn ever changes its access-record shape, the filter must
    # log too much, never silently swallow. A record with no args is kept.
    def check(access: logging.Logger) -> None:
        _with_clean_package_logger(lambda _pkg: configure_logging(stream=io.StringIO()))
        odd = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="something uvicorn does not do today",
            args=None,
            exc_info=None,
        )
        assert access.filter(odd) is True

    _with_clean_access_logger(check)


def test_the_up_filter_is_installed_once_across_restarts() -> None:
    def check(access: logging.Logger) -> None:
        def configure_twice(_pkg: logging.Logger) -> None:
            configure_logging(stream=io.StringIO())
            configure_logging(stream=io.StringIO())

        _with_clean_package_logger(configure_twice)
        tagged = [f for f in access.filters if getattr(f, "_basecradle_router", False)]
        assert len(tagged) == 1

    _with_clean_access_logger(check)


def test_the_startup_banner_states_the_live_config_the_router_booted_with(caplog) -> None:
    breaker = WakeRateBreaker(
        BreakerConfig(max_wakes=7, window=30.0, cooldown=90.0, stream_max_wakes=3)
    )
    pipeline = Pipeline(
        registry=_registry(),
        config=_config(),
        waker=_RecordingWaker(),
        breaker=breaker,
        deduper=DeliveryDeduper(ttl=120.0),
        sleep=lambda _d: None,
    )
    server = WebhookServer(pipeline)

    with caplog.at_level("INFO", logger="basecradle_router.server"):
        server.log_startup_banner()

    line = next(r.getMessage() for r in caplog.records if "event=startup" in r.getMessage())
    # The thresholds come from the LIVE collaborators, not a restatement of the
    # defaults — a banner that could not disagree with the running config is useless.
    assert "routes=github" in line
    assert "dedup_ttl=120s" in line
    assert "breaker_max=7" in line
    assert "breaker_stream_max=3" in line
    assert "breaker_window=30s" in line
    assert "breaker_cooldown=90s" in line
    assert "wake_attempts=3" in line
    assert "sha=" in line  # "unknown" off-box; the deployed commit on it


def test_deployed_sha_reads_the_stamp_and_never_raises_when_it_is_absent(tmp_path) -> None:
    # The stamp is the NOC deploy op's, so the daemon must tolerate its absence: it
    # runs on a laptop and in CI too, and no observability nicety may block a boot.
    assert deployed_sha(str(tmp_path / "nope")) == "unknown"

    stamp = tmp_path / "deployed-sha"
    stamp.write_text("93bae15abc123\n")
    assert deployed_sha(str(stamp)) == "93bae15abc123"

    stamp.write_text("   \n")
    assert deployed_sha(str(stamp)) == "unknown"  # an empty stamp is not a sha

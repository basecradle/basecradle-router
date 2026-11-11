"""The request body is the last thing read, and never more of it than its route can send (#427).

Until #427 the server read and buffered the whole body before the pipeline looked the
source up or checked a signature, so an unauthenticated caller could make the daemon hold
up to the edge's 25 MB for a request it was about to answer ``404`` or ``401``. These tests
drive the ASGI app in-process with a ``receive`` that counts what it hands over, so "the
body was not read" is a measured fact, never an inference from a status code.

Cast: John Doe (``john``, human) and Nova Digital (``nova``, AI). Every secret, uuid and
signature is fabricated.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from types import MappingProxyType

import pytest

from basecradle_router.config import Config
from basecradle_router.evidence import EvidenceStore
from basecradle_router.models import Agent, Event
from basecradle_router.pipeline import Outcome, Pipeline, Stage
from basecradle_router.routes import BasecradleRoute, GithubRoute, InboundRequest, RouteRegistry
from basecradle_router.secret import Secret
from basecradle_router.server import WebhookServer
from basecradle_router.wake import WakeResult

GITHUB_SECRET = "fake-github-webhook-secret-" + "0" * 32
PLATFORM_SECRET = "fake-basecradle-webhook-secret-" + "1" * 32
NOVA = Agent(
    key="basecradle/basecradle-python",
    os_user="nova",
    clone_path="/home/nova/basecradle-python",
    bot_slug="basecradle-python-ai",
)
PLATFORM_CAP = BasecradleRoute.max_body_bytes


class _RecordingWaker:
    def __init__(self) -> None:
        self.calls: list[tuple[Agent, Event]] = []

    def wake(self, agent: Agent, event: Event) -> WakeResult:
        self.calls.append((agent, event))
        return WakeResult(exit_code=0, stdout="opened PR")


def _server(*, routes: frozenset[str] = frozenset({"github", "basecradle"})):
    """A server with the github and platform routes as a box would register them."""
    registry = RouteRegistry()
    secrets: dict[str, Secret] = {}
    if "github" in routes:
        registry.register(GithubRoute(frozenset({"john"})))
        secrets["github"] = Secret(GITHUB_SECRET)
    if "basecradle" in routes:
        registry.register(BasecradleRoute())
        secrets["basecradle"] = Secret(PLATFORM_SECRET)
    config = Config(
        agents=MappingProxyType({NOVA.key: NOVA}),
        enabled_routes=routes,
        webhook_secrets=MappingProxyType(secrets),
    )
    waker = _RecordingWaker()
    evidence = EvidenceStore(None)
    pipeline = Pipeline(
        registry=registry, config=config, waker=waker, sleep=lambda _d: None, evidence=evidence
    )
    return WebhookServer(pipeline), waker, evidence


class _Receive:
    """An ASGI ``receive`` that hands the body over in chunks and counts each one taken."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.taken = 0

    async def __call__(self) -> dict:
        if not self._chunks:
            return {"type": "http.disconnect"}
        chunk = self._chunks.pop(0)
        self.taken += 1
        return {"type": "http.request", "body": chunk, "more_body": bool(self._chunks)}


def _call(server: WebhookServer, path: str, chunks: list[bytes], headers: dict[str, str]):
    """POST through the ASGI app; return (status, raw response body, response headers, receive)."""
    receive = _Receive(chunks)
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
    }

    async def drive() -> None:
        await server(scope, receive, send)
        await server.drain()

    asyncio.run(drive())
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = next(m["body"] for m in sent if m["type"] == "http.response.body")
    return start["status"], body, start["headers"], receive


def _github_handoff() -> tuple[bytes, dict[str, str]]:
    payload = {
        "action": "opened",
        "issue": {
            "number": 42,
            "title": "Mirror the wire-shape change",
            "html_url": "https://github.com/basecradle/basecradle-python/issues/42",
            "labels": [{"name": "handoff"}],
        },
        "repository": {"full_name": "basecradle/basecradle-python"},
        "sender": {"login": "john", "type": "User"},
    }
    body = json.dumps(payload).encode("utf-8")
    digest = hmac.new(GITHUB_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, {
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": "0192f3a4-5b6c-7d8e-9f01-23456789abcd",
        "X-Hub-Signature-256": f"sha256={digest}",
        "Content-Type": "application/json",
    }


def _bad_platform_signature() -> dict[str, str]:
    return {
        "X-BaseCradle-Event": "message.created",
        "X-BaseCradle-Delivery": "0192f3a4-5b6c-7d8e-9f01-0000000000aa",
        "X-BaseCradle-Signature": "sha256=" + "0" * 64,
        "Content-Type": "application/json",
    }


OVERSIZED = b"x" * (PLATFORM_CAP + 1)


# --- 1. a source the path does not name is answered before a byte is read ------------------


@pytest.mark.parametrize("declare", [True, False], ids=["content-length", "chunked"])
def test_an_oversized_body_to_an_unknown_source_is_answered_without_reading_it(declare) -> None:
    server, waker, evidence = _server()
    headers = {"Content-Length": str(10 * 1024 * 1024)} if declare else {}
    status, _, _, receive = _call(server, "/webhooks/gitlab", [b"x" * 1024] * 64, headers)

    assert status == 404
    assert receive.taken == 0, "the body of a request for an unknown source was read"
    assert waker.calls == []
    # An unknown source never grows the evidence document, as before (#460, instance 5).
    assert evidence.snapshot().delivery_sinks == {}


def test_a_source_this_box_has_not_enabled_answers_exactly_as_an_unknown_one() -> None:
    # A platform-only box (red's shape) registers no github route, so a github path is as
    # unknown there as a made-up one — same status, same bytes, same headers, body unread.
    server, _, _ = _server(routes=frozenset({"basecradle"}))
    body, headers = _github_handoff()
    disabled = _call(server, "/webhooks/github", [body], headers)
    unknown = _call(server, "/webhooks/gitlab", [body], headers)

    assert disabled[0] == unknown[0] == 404
    assert disabled[1] == unknown[1]
    assert disabled[2] == unknown[2]
    assert disabled[3].taken == unknown[3].taken == 0


def test_a_long_unknown_source_name_is_logged_bounded(caplog) -> None:
    # The name is the caller's own path segment, logged before anything about the caller
    # is known: it is cut, and the registry's message that repeats it whole is not logged.
    server, _, _ = _server()
    name = "a" * 4000
    with caplog.at_level(logging.WARNING, logger="basecradle_router.pipeline"):
        _call(server, f"/webhooks/{name}", [b"{}"], {})

    (line,) = [r.getMessage() for r in caplog.records if "stage=route" in r.getMessage()]
    assert "outcome=rejected" in line
    assert name not in line
    assert f"source={'a' * 64}…(+{4000 - 64})" in line
    # What it does carry is the registered routes: the pointer at a source a box forgot
    # to enable.
    assert "known=basecradle,github" in line
    assert len(line) < 300


# --- 2. a known source: the cap binds before the signature is checked ----------------------


def test_a_declared_length_over_the_cap_is_refused_before_a_byte_is_read() -> None:
    server, waker, evidence = _server()
    headers = {**_bad_platform_signature(), "Content-Length": str(PLATFORM_CAP + 1)}
    status, raw, _, receive = _call(server, "/webhooks/basecradle", [OVERSIZED], headers)

    assert status == 413
    assert receive.taken == 0, "a body announced over the cap was read anyway"
    assert json.loads(raw) == {
        "outcome": "rejected",
        "stages": [["route", "ok"], ["body", "rejected"]],
    }
    assert waker.calls == []


def test_a_chunked_body_with_no_length_stops_being_read_at_the_cap() -> None:
    # No Content-Length: the server cannot know the size up front, so it reads — and stops
    # at the first chunk that crosses the cap, leaving every later chunk unread.
    server, waker, _ = _server()
    chunk = b"x" * 4096
    chunks = [chunk] * 64  # 256 KiB offered against a 64 KiB cap
    status, _, _, receive = _call(server, "/webhooks/basecradle", chunks, _bad_platform_signature())

    assert status == 413
    assert receive.taken == PLATFORM_CAP // len(chunk) + 1
    assert waker.calls == []


def test_an_oversized_body_with_a_bad_signature_is_refused_by_size_never_hashed() -> None:
    # The refusal is the body cap's, never the signature's: the 413 comes first, so the
    # oversized body is never fed to the HMAC (or to the platform route's pre-verify parse).
    server, _, _ = _server()
    status, raw, _, _ = _call(
        server, "/webhooks/basecradle", [OVERSIZED], _bad_platform_signature()
    )

    assert status == 413
    assert ["verify", "rejected"] not in json.loads(raw)["stages"]


@pytest.mark.parametrize("declare", [True, False], ids=["content-length", "chunked"])
def test_a_refused_body_is_one_warning_line_and_no_evidence(caplog, declare) -> None:
    # `rejected` means a delivery reached verification and failed it — the
    # mismatched-secret signal the NOC reads off accepted=0 rejected=N — and no source we
    # serve can send a body over its cap. So a 413 is logged and never counted: a header
    # with no body must not be able to blur that signal or rewrite the document.
    server, _, evidence = _server()
    headers = _bad_platform_signature()
    if declare:
        headers["Content-Length"] = str(PLATFORM_CAP + 1)
    with caplog.at_level(logging.WARNING, logger="basecradle_router.pipeline"):
        _call(server, "/webhooks/basecradle", [OVERSIZED], headers)

    assert evidence.snapshot().delivery_sinks == {}
    (line,) = [r.getMessage() for r in caplog.records if "stage=body" in r.getMessage()]
    assert "outcome=rejected" in line
    assert "source=basecradle" in line
    assert f"limit={PLATFORM_CAP}" in line
    assert (f"declared={PLATFORM_CAP + 1}" in line) is declare


def test_a_body_exactly_at_the_cap_still_reaches_the_signature() -> None:
    server, _, _ = _server()
    at_cap = b"x" * PLATFORM_CAP
    status, raw, _, _ = _call(server, "/webhooks/basecradle", [at_cap], _bad_platform_signature())

    assert status == 401
    assert json.loads(raw)["stages"] == [["route", "ok"], ["verify", "rejected"]]


@pytest.mark.parametrize("length", ["lots", "+70000", " 70000", "70_000"])
def test_an_unreadable_content_length_falls_back_to_the_streaming_cap(length: str) -> None:
    # h11 refuses a malformed length before the app is called; this does not lean on it.
    # Only digits are a length (int() alone takes the signed, padded and underscored
    # forms), and anything else can only cost a read, never widen one.
    server, _, _ = _server()
    headers = {**_bad_platform_signature(), "Content-Length": length}
    status, _, _, receive = _call(server, "/webhooks/basecradle", [OVERSIZED], headers)

    assert status == 413
    assert receive.taken == 1


def test_a_client_that_leaves_mid_body_is_never_verified_or_answered() -> None:
    # A truncated body is not the delivery: verifying it would record a signature failure
    # that never happened, and there is no one left to answer.
    server, waker, evidence = _server()
    handed = [
        {"type": "http.request", "body": b'{"partial": ', "more_body": True},
        {"type": "http.disconnect"},
    ]
    sent: list[dict] = []

    async def receive() -> dict:
        return handed.pop(0)

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/webhooks/basecradle",
        "headers": [(k.lower().encode(), v.encode()) for k, v in _bad_platform_signature().items()],
    }
    asyncio.run(server(scope, receive, send))

    assert sent == []
    assert handed == []
    assert waker.calls == []
    assert evidence.snapshot().delivery_sinks == {}


def test_a_small_body_with_a_bad_signature_is_still_unauthorized() -> None:
    server, waker, _ = _server()
    status, _, _, _ = _call(server, "/webhooks/basecradle", [b"{}"], _bad_platform_signature())

    assert status == 401
    assert waker.calls == []


# --- 3. the valid delivery is unchanged ------------------------------------------------------


@pytest.mark.parametrize("split", [1, 7], ids=["one-chunk", "chunked"])
def test_a_valid_delivery_still_wakes_its_agent(split: int) -> None:
    server, waker, evidence = _server()
    body, headers = _github_handoff()
    step = -(-len(body) // split)
    chunks = [body[i : i + step] for i in range(0, len(body), step)]
    status, raw, _, receive = _call(server, "/webhooks/github", chunks, headers)

    assert status == 202
    assert receive.taken == len(chunks)
    assert [agent.os_user for agent, _ in waker.calls] == ["nova"]
    stages = [tuple(pair) for pair in json.loads(raw)["stages"]]
    assert stages == [
        ("route", "ok"),
        ("verify", "ok"),
        ("normalize", "ok"),
        ("resolve", "ok"),
    ]
    assert evidence.snapshot().delivery_sinks["github"].accepted == 1


def test_accept_without_an_admission_still_runs_the_whole_fast_half() -> None:
    # The offline callers (handle, the tests) hold the body already and pass no admission;
    # the route stage then runs inside accept, exactly as it always did.
    server, _, _ = _server()
    body, headers = _github_handoff()
    accepted = server.pipeline.accept("github", InboundRequest(headers=headers, body=body))
    assert accepted.pending is not None
    assert [(r.stage, r.outcome) for r in accepted.result.records][0] == (Stage.ROUTE, Outcome.OK)


def test_an_admission_names_the_source_the_rest_of_the_trip_uses() -> None:
    # The route that verifies and the source the evidence and lines name come from one
    # place, so a caller handing a mismatched name cannot split them.
    server, _, evidence = _server()
    body, headers = _github_handoff()
    admission = server.pipeline.admit("github")
    accepted = server.pipeline.accept(
        "basecradle", InboundRequest(headers=headers, body=body), admission=admission
    )
    assert accepted.pending is not None
    assert set(evidence.snapshot().delivery_sinks) == {"github"}

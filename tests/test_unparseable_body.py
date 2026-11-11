"""A validly signed body the JSON decoder refuses is a 400, never a 200-and-drop (#433).

``parse_json_object`` used to catch only :class:`json.JSONDecodeError`, but ``json.loads``
refuses ordinary bodies in other ways too: an integer over the interpreter's 4300-digit
limit is a plain ``ValueError``, bytes that are not UTF-8 are a ``UnicodeDecodeError``, and
deep nesting is a ``RecursionError``, which is not a ``ValueError`` at all. Each escaped
``normalize`` into the core's last-resort handler, which records ``stage=route
outcome=failed`` and answers ``200``, so a delivery that passed its signature was
acknowledged and dropped with no wake, and GitHub never redelivers a ``200``.

These tests sign every body correctly, so the only thing under test is what happens after
verification. They drive each route through the real ASGI app, then the same request
through ``Pipeline.accept`` and ``_status_for``, so the status is the consequence of the
recorded stage and not a coincidence of the transport.

The last section pins the boundary that closes the class rather than one escape (#434):
anything else a route raises on a verified delivery is recorded at normalize and answered
``500``, so an escape nobody has found yet is never a ``200``-and-drop either.

Cast: John Doe (``john``, human) and Nova Digital (``nova``, AI). Every secret, uuid and
signature is fabricated.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
from collections.abc import Callable
from types import MappingProxyType

import pytest

from basecradle_router.config import Config
from basecradle_router.evidence import EvidenceStore
from basecradle_router.models import Agent, Event
from basecradle_router.pipeline import Outcome, Pipeline, PipelineResult, Stage, StageRecord
from basecradle_router.routes import (
    BasecradleRoute,
    GithubRoute,
    InboundRequest,
    PayloadError,
    RouteRegistry,
    parse_json_object,
)
from basecradle_router.routes.probe import ProbeRoute
from basecradle_router.secret import Secret
from basecradle_router.server import WebhookServer, _status_for
from basecradle_router.wake import WakeResult

SECRETS = {
    "github": "fake-github-webhook-secret-" + "0" * 32,
    "basecradle": "fake-basecradle-webhook-secret-" + "1" * 32,
    "probe": "fake-probe-route-secret-" + "2" * 32,
}
NOVA = Agent(
    key="basecradle/basecradle-python",
    os_user="nova",
    clone_path="/home/nova/basecradle-python",
    bot_slug="basecradle-python-ai",
)

#: The headers that carry each route's signature, and the ones that take a delivery past
#: every early ignore to the body parse: an actionable event type and a delivery id.
ROUTE_HEADERS = {
    "github": (
        "X-Hub-Signature-256",
        {"X-GitHub-Event": "issues", "X-GitHub-Delivery": "0192f3a4-5b6c-7d8e-9f01-23456789abcd"},
    ),
    "basecradle": (
        "X-BaseCradle-Signature",
        {
            "X-BaseCradle-Event": "message.created",
            "X-BaseCradle-Delivery": "0192f3a4-5b6c-7d8e-9f01-23456789abce",
        },
    ),
    "probe": (
        "X-BaseCradle-Probe-Signature",
        {
            "X-BaseCradle-Probe-Event": "probe.wake",
            "X-BaseCradle-Probe-Delivery": "0192f3a4-5b6c-7d8e-9f01-23456789abcf",
        },
    ),
}
ROUTE_CAPS = {
    "github": GithubRoute.max_body_bytes,
    "basecradle": BasecradleRoute.max_body_bytes,
    "probe": ProbeRoute.max_body_bytes,
}


def _long_integer(_cap: int) -> bytes:
    """One digit over CPython's default int-string limit: about 4.3 KB, under every cap."""
    return b'{"harness_key": ' + b"1" * 4301 + b"}"


def _not_utf8(_cap: int) -> bytes:
    return b'{"harness_key": "nova\xff"}'


def _deeply_nested(cap: int) -> bytes:
    """An object holding as deep an array as the route's cap allows.

    The depth is the cap's, never a guessed recursion threshold: the threshold belongs
    to the interpreter, and the class is what is under test. The object wrapper means a
    decoder that *did* manage the depth would hand back a dict, so a 400 here can only
    come from the parse refusing it, never from the not-an-object check.
    """
    depth = (cap - len(b'{"a":}')) // 2
    return b'{"a":' + b"[" * depth + b"]" * depth + b"}"


BODIES: dict[str, Callable[[int], bytes]] = {
    "long-integer": _long_integer,
    "not-utf8": _not_utf8,
    "deeply-nested": _deeply_nested,
}
#: The digit limit arrived in 3.10.7 and is switched off by ``PYTHONINTMAXSTRDIGITS=0``. Where
#: it is absent the long integer decodes, so there is no escape for those cases to pin.
_NO_DIGIT_LIMIT = pytest.mark.skipif(
    getattr(sys, "get_int_max_str_digits", lambda: 0)() == 0,
    reason="this interpreter has no int-string digit limit, so a long integer decodes",
)


def _case(*values: str) -> object:
    return pytest.param(*values, marks=[_NO_DIGIT_LIMIT] if "long-integer" in values else [])


KINDS = [_case(kind) for kind in BODIES]
CASES = [_case(route, kind) for route in ROUTE_HEADERS for kind in BODIES]


class _RecordingWaker:
    def __init__(self) -> None:
        self.calls: list[tuple[Agent, Event]] = []

    def wake(self, agent: Agent, event: Event) -> WakeResult:
        self.calls.append((agent, event))
        return WakeResult(exit_code=0, stdout="opened PR")


def _pipeline() -> tuple[Pipeline, _RecordingWaker]:
    registry = RouteRegistry()
    registry.register(GithubRoute(frozenset({"john"})))
    registry.register(BasecradleRoute())
    registry.register(ProbeRoute())
    config = Config(
        agents=MappingProxyType({NOVA.key: NOVA}),
        enabled_routes=frozenset(ROUTE_HEADERS),
        webhook_secrets=MappingProxyType({r: Secret(s) for r, s in SECRETS.items()}),
        harness_index=MappingProxyType({NOVA.harness_key: NOVA}),
    )
    waker = _RecordingWaker()
    pipeline = Pipeline(
        registry=registry,
        config=config,
        waker=waker,
        sleep=lambda _d: None,
        evidence=EvidenceStore(None),
    )
    return pipeline, waker


def _signed(route: str, body: bytes) -> dict[str, str]:
    header, extra = ROUTE_HEADERS[route]
    digest = hmac.new(SECRETS[route].encode(), body, hashlib.sha256).hexdigest()
    return {**extra, header: f"sha256={digest}", "Content-Type": "application/json"}


def _post(server: WebhookServer, path: str, body: bytes, headers: dict[str, str]):
    """POST through the ASGI app in-process; return (status, parsed response body)."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
    }
    incoming = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[dict] = []

    async def receive() -> dict:
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    async def drive() -> None:
        await server(scope, receive, send)
        await server.drain()

    asyncio.run(drive())
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    raw = next(m["body"] for m in sent if m["type"] == "http.response.body")
    return status, json.loads(raw)


@pytest.mark.parametrize("kind", KINDS)
def test_each_body_really_is_one_the_decoder_refuses_without_a_decode_error(kind) -> None:
    """The premise, checked against the interpreter running the suite.

    If a future interpreter accepted one of these bodies, or refused it with a plain
    ``JSONDecodeError``, the end-to-end tests below would still pass while no longer
    exercising the escape they exist to pin. This says so instead.
    """
    body = BODIES[kind](min(ROUTE_CAPS.values()))
    with pytest.raises((ValueError, RecursionError)) as caught:
        json.loads(body)
    assert not isinstance(caught.value, json.JSONDecodeError)


@pytest.mark.parametrize("raised", [ValueError("digits"), RecursionError("depth")])
def test_the_parse_boundary_turns_every_decoder_refusal_into_a_payload_error(
    monkeypatch, raised
) -> None:
    """The catch list itself, independent of where any interpreter puts its limits."""

    def refuse(_body):
        raise raised

    monkeypatch.setattr(json, "loads", refuse)
    with pytest.raises(PayloadError, match=type(raised).__name__) as caught:
        parse_json_object(b"{}")
    assert caught.value.__cause__ is raised


@pytest.mark.parametrize(("route", "kind"), CASES)
def test_a_signed_body_the_decoder_refuses_is_a_400_and_wakes_no_one(route, kind) -> None:
    body = BODIES[kind](ROUTE_CAPS[route])
    assert len(body) <= ROUTE_CAPS[route]
    pipeline, waker = _pipeline()

    status, summary = _post(
        WebhookServer(pipeline), f"/webhooks/{route}", body, _signed(route, body)
    )

    assert status == 400
    assert summary == {
        "outcome": "rejected",
        "stages": [["route", "ok"], ["verify", "ok"], ["normalize", "rejected"]],
    }
    assert waker.calls == []


@pytest.mark.parametrize(("route", "kind"), CASES)
def test_the_400_is_the_normalize_rejection_never_the_last_resort_failure(route, kind) -> None:
    """Through ``accept`` and ``_status_for``: the status follows from the recorded stage.

    The defect was a ``stage=route outcome=failed`` record, which ``_status_for`` answers
    ``200`` on purpose (a misconfiguration is ours, not the caller's). So the record is
    what is pinned: a rejection at normalize, no event, nothing pending, and no last-resort
    record anywhere in the trip.
    """
    body = BODIES[kind](ROUTE_CAPS[route])
    pipeline, waker = _pipeline()

    accepted = pipeline.accept(route, InboundRequest(headers=_signed(route, body), body=body))

    last = accepted.result.records[-1]
    assert (last.stage, last.outcome) == (Stage.NORMALIZE, Outcome.REJECTED)
    assert "could not be decoded as JSON" in last.detail
    assert all(r.outcome is not Outcome.FAILED for r in accepted.result.records)
    assert accepted.pending is None
    assert accepted.result.event is None
    assert _status_for(accepted.result) == 400
    assert waker.calls == []


# --- the class, closed at the boundary (#434) ------------------------------------------
#
# The tests above close the escapes found so far one call site at a time. These pin the
# boundary itself: whatever else a route raises after its signature verified is recorded
# at normalize as a failure and answered 500, so the next escape nobody has found yet is
# still never a 200-and-drop. Each route's real ``normalize`` is replaced by one that
# raises, so no particular payload is needed to reach the case.


class _RouteBug(Exception):
    """Stands for a defect in a route's own code: no route raises it on purpose."""


def _crash(*_args: object) -> Event:
    raise _RouteBug("nova's payload took a path nobody wrote")


@pytest.mark.parametrize("route", ROUTE_HEADERS)
def test_a_route_bug_on_a_verified_delivery_is_a_500_and_wakes_no_one(monkeypatch, route) -> None:
    pipeline, waker = _pipeline()
    monkeypatch.setattr(pipeline.registry.get(route), "normalize", _crash)
    body = b'{"harness_key": "nova"}'

    status, summary = _post(
        WebhookServer(pipeline), f"/webhooks/{route}", body, _signed(route, body)
    )

    assert status == 500
    assert summary == {
        "outcome": "failed",
        "stages": [["route", "ok"], ["verify", "ok"], ["normalize", "failed"]],
    }
    assert waker.calls == []


@pytest.mark.parametrize("route", ROUTE_HEADERS)
def test_a_route_bug_is_recorded_at_normalize_never_as_the_last_resort_route_failure(
    monkeypatch, caplog, route
) -> None:
    """The stage is the point: evidence and any alert keyed on it name where it broke."""
    pipeline, waker = _pipeline()
    monkeypatch.setattr(pipeline.registry.get(route), "normalize", _crash)
    body = b'{"harness_key": "nova"}'

    with caplog.at_level("INFO", logger="basecradle_router"):
        accepted = pipeline.accept(route, InboundRequest(headers=_signed(route, body), body=body))

    last = accepted.result.records[-1]
    assert (last.stage, last.outcome) == (Stage.NORMALIZE, Outcome.FAILED)
    assert "unexpected: _RouteBug: nova's payload took a path nobody wrote" in last.detail
    assert [(r.stage, r.outcome) for r in accepted.result.records].count(
        (Stage.ROUTE, Outcome.FAILED)
    ) == 0
    assert accepted.pending is None
    assert accepted.result.event is None
    assert _status_for(accepted.result) == 500
    assert waker.calls == []

    # One ERROR line with the traceback: a router bug, not a sender's fault.
    crashed = [r for r in caplog.records if "event=normalize_crashed" in r.getMessage()]
    assert len(crashed) == 1
    assert crashed[0].levelname == "ERROR"
    assert f"source={route}" in crashed[0].getMessage()
    assert "in _crash" in crashed[0].getMessage(), "the frames where it was raised"


@pytest.mark.parametrize("route", ROUTE_HEADERS)
def test_a_route_bug_counts_the_delivery_accepted_and_records_no_decision(
    monkeypatch, route
) -> None:
    """Evidence is unchanged by #434: the signature verified, and the route decided nothing."""
    pipeline, _waker = _pipeline()
    monkeypatch.setattr(pipeline.registry.get(route), "normalize", _crash)
    body = b'{"harness_key": "nova"}'

    pipeline.accept(route, InboundRequest(headers=_signed(route, body), body=body))

    sink = pipeline.evidence.snapshot().delivery_sinks[route]
    assert (sink.accepted, sink.rejected, sink.woke, sink.ignored) == (1, 0, 0, 0)


def test_only_a_failure_at_normalize_is_a_500_every_other_failure_stays_accepted() -> None:
    """A missing secret (verify) and an unregistered repo (resolve) are ours to log: 200."""

    def ending_at(stage: Stage) -> PipelineResult:
        result = PipelineResult()
        result.records.append(StageRecord(stage=stage, outcome=Outcome.FAILED, detail=""))
        return result

    assert _status_for(ending_at(Stage.NORMALIZE)) == 500
    for stage in (Stage.ROUTE, Stage.VERIFY, Stage.RESOLVE):
        assert _status_for(ending_at(stage)) == 200


def test_a_route_bugs_message_is_bounded_wherever_it_is_written(monkeypatch, caplog) -> None:
    """A route bug's message can carry the sender's bytes: one signed body never fills a log."""
    flood = "nova" * 10_000

    def crash_with_the_payload(*_args: object) -> Event:
        raise KeyError(flood)

    pipeline, _waker = _pipeline()
    monkeypatch.setattr(pipeline.registry.get("github"), "normalize", crash_with_the_payload)
    body = b'{"harness_key": "nova"}'

    with caplog.at_level("INFO", logger="basecradle_router"):
        accepted = pipeline.accept(
            "github", InboundRequest(headers=_signed("github", body), body=body)
        )

    assert len(accepted.result.records[-1].detail) < 400
    assert all(len(r.getMessage()) < 4_000 for r in caplog.records)
    assert not any(flood in r.getMessage() for r in caplog.records)
    assert all(r.exc_info is None for r in caplog.records), "a traceback would repeat the message"

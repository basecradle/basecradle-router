"""The fleet's own traffic is never evidence on a real route's delivery sink (#464).

A real route's ``delivery_sinks.<route>`` is evidence about the *source*: ``accepted``
proves the secret on this box matches the one the source signs with. The deploy smoke
test signs its deliveries from this box's own ``router.env``, so before #464 every deploy
refreshed ``last_accepted_at`` on the github and basecradle sinks whether or not GitHub or
the platform could still reach the box, and moved ``rejected`` on every deploy too. Its
requests carry #461's synthetic mark, so the daemon now records nothing on a real sink
for a request whose mark verified.

Pinned here, through the real ASGI app with real minted marks (the server verifies them
itself, exactly as on the box), on each of the smoke test's six route cases:

1. **An authenticated mark moves no real sink**: not ``accepted``, not ``rejected``, not
   ``woke``/``ignored``, and no row is even created.
2. **A mark that does not authenticate moves it exactly as no mark does**: that request
   is the internet's, and a forged mark must never be a way to hide from the ledger.
3. **The response is unchanged**, so the smoke test, which asserts on responses, still
   proves what it proved.
4. **The probe route's own sink still counts a marked delivery** (smoke case 8): it is
   already the fleet's and says so (``detail.synthetic``).

No network, no model, no live agent. Fabricated cast: John Doe (``john``) is the trusted
GitHub actor, Nova Digital (``nova``) the harness agent. Secrets are correctly-shaped fakes.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import asdict
from types import MappingProxyType

import pytest

from basecradle_router.config import Config
from basecradle_router.models import Agent, Event, WakeKind
from basecradle_router.pipeline import Pipeline
from basecradle_router.routes import RouteRegistry
from basecradle_router.routes import basecradle as bc
from basecradle_router.routes import probe as pr
from basecradle_router.routes.basecradle import BasecradleRoute, RecipientKeyring
from basecradle_router.routes.github import GithubRoute
from basecradle_router.routes.probe import ProbeRoute
from basecradle_router.secret import Secret
from basecradle_router.server import WebhookServer
from basecradle_router.synthetic import MARK_HEADER, WINDOW, mint
from basecradle_router.wake import WakeAttempt, WakeResult
from basecradle_router.wakelock import WakeLockGuard

GITHUB_SECRET = "fake-github-webhook-secret-" + "0" * 32
BC_SHARED_SECRET = "fake-basecradle-route-wide-secret-" + "2" * 32
PROBE_SECRET = "fake-probe-route-secret-" + "1" * 32
NOVA_UUID = "01a161c5-9ff1-76f8-a04c-c476e36c30b7"
NOVA_KEY = "bc_isk_fakenovaintegrationsigningkey002"
NOVA = Agent(
    key="nova",
    os_user="nova",
    clone_path="/home/nova/harness",
    wake_kind=WakeKind.HARNESS,
    recipient_uuid=NOVA_UUID,
    wake_bin="/home/nova/venv/bin/basecradle-harness-wake",
)
# The smoke test's own fixed values (deploy/smoke-test.sh).
UNREGISTERED_REPO = "basecradle/__router-smoke-test-do-not-register__"
UNREGISTERED_RECIPIENT = "00000000-0000-7000-8000-000000000000"
SMOKE_TIMELINE = "00000000-0000-7000-8000-0000000000aa"
BC_IGNORED_EVENT = "timeline.locked"
REAL_SINKS = ("github", "basecradle")


class _Waker:
    def wake(
        self, agent: Agent, event: Event, attempt: WakeAttempt | None = None
    ) -> WakeResult:  # pragma: no cover - no case wakes
        raise AssertionError(f"no smoke case may wake an agent: {event.delivery_id}")


def _server(tmp_path, *, fallback: bool) -> WebhookServer:
    registry = RouteRegistry()
    registry.register(GithubRoute(frozenset({"john"})))
    registry.register(
        BasecradleRoute(
            RecipientKeyring(
                by_recipient=MappingProxyType({NOVA_UUID: Secret(NOVA_KEY)}),
                shared_fallback=fallback,
            )
        )
    )
    registry.register(ProbeRoute())
    return WebhookServer(
        Pipeline(
            registry=registry,
            config=Config(
                agents=MappingProxyType({NOVA.key: NOVA}),
                enabled_routes=frozenset({"github", "basecradle", "probe"}),
                webhook_secrets=MappingProxyType(
                    {
                        "github": Secret(GITHUB_SECRET),
                        "basecradle": Secret(BC_SHARED_SECRET),
                        "probe": Secret(PROBE_SECRET),
                    }
                ),
                recipient_index=MappingProxyType({NOVA_UUID: NOVA}),
                harness_index=MappingProxyType({NOVA.harness_key: NOVA}),
            ),
            waker=_Waker(),
            wake_lock=WakeLockGuard(lock_dir=str(tmp_path)),
            sleep=lambda _d: None,
        )
    )


def _digest(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _github(*, sender: str = "john", forge: bool = False):
    body = json.dumps(
        {
            "action": "labeled",
            "label": {"name": "handoff"},
            "issue": {
                "number": 1,
                "html_url": f"https://github.com/{UNREGISTERED_REPO}/issues/1",
                "title": "Router live smoke test (no-op)",
                "labels": [{"name": "handoff"}],
            },
            "repository": {"full_name": UNREGISTERED_REPO},
            "sender": {"login": sender, "type": "User"},
        }
    ).encode()
    digest = "0" * 64 if forge else _digest(GITHUB_SECRET, body)
    return (
        "/webhooks/github",
        body,
        {
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": f"sha256={digest}",
        },
    )


def _basecradle(recipient: str, *, key: str | None, forge: bool = False):
    delivery = str(uuid.uuid4())
    body = json.dumps(
        {
            "event": BC_IGNORED_EVENT,
            "event_id": delivery,
            "occurred_at": "2026-01-01T00:00:00Z",
            "actor_uuid": None,
            "recipient_uuid": recipient,
            "timeline_uuid": SMOKE_TIMELINE,
            "resource": {
                "type": "timeline",
                "uuid": SMOKE_TIMELINE,
                "url": "https://example.com/smoke",
            },
        }
    ).encode()
    digest = "0" * 64 if forge else _digest(key or "", body)
    return (
        "/webhooks/basecradle",
        body,
        {
            bc.EVENT_HEADER: BC_IGNORED_EVENT,
            bc.DELIVERY_HEADER: delivery,
            bc.SIGNATURE_HEADER: f"sha256={digest}",
        },
    )


def _probe_bad_signature():
    body = json.dumps({"harness_key": NOVA.harness_key, "marker": "BCNOC1 smoke"}).encode()
    return (
        "/webhooks/probe",
        body,
        {
            pr.SIGNATURE_HEADER: "sha256=" + "0" * 64,
            pr.DELIVERY_HEADER: uuid.uuid4().hex,
            "X-BaseCradle-Probe-Event": "probe.wake",
        },
    )


#: The smoke test's route cases: ``name → (request, fallback armed, status, real sink)``.
SMOKE_CASES = {
    "1 github bad signature": (lambda: _github(forge=True), False, 401, "github"),
    "2 github untrusted sender": (
        lambda: _github(sender="router-smoke-untrusted-actor"),
        False,
        400,
        "github",
    ),
    "3 github trusted, unregistered repo": (lambda: _github(), False, 200, "github"),
    "4 basecradle forged digest": (
        lambda: _basecradle(NOVA_UUID, key=NOVA_KEY, forge=True),
        False,
        401,
        "basecradle",
    ),
    "5 basecradle own key, ignored event": (
        lambda: _basecradle(NOVA_UUID, key=NOVA_KEY),
        False,
        200,
        "basecradle",
    ),
    "6 basecradle unknown recipient, fallback retired": (
        lambda: _basecradle(UNREGISTERED_RECIPIENT, key=BC_SHARED_SECRET),
        False,
        401,
        "basecradle",
    ),
    "6 basecradle unknown recipient, fallback armed": (
        lambda: _basecradle(UNREGISTERED_RECIPIENT, key=BC_SHARED_SECRET),
        True,
        200,
        "basecradle",
    ),
}


def _mark(*, secret: str = PROBE_SECRET, timestamp: int | None = None) -> str:
    stamp = int(time.time()) if timestamp is None else timestamp
    return mint(Secret(secret), timestamp=stamp, nonce=uuid.uuid4().hex)


#: Every way a mark can fail to authenticate: each leaves the request the internet's.
UNAUTHENTICATED_MARKS = {
    "forged": lambda: _mark(secret=GITHUB_SECRET),
    "stale": lambda: _mark(timestamp=int(time.time() - 10 * WINDOW)),
    "malformed": lambda: "BCSYN1 i-am-the-fleet-honest",
}


def _post(server: WebhookServer, path: str, body: bytes, headers: dict[str, str]) -> int:
    async def drive() -> int:
        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
        incoming = [{"type": "http.request", "body": body, "more_body": False}]
        statuses: list[int] = []

        async def receive():
            return incoming.pop(0)

        async def send(message):
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        await server(scope, receive, send)
        await server.drain()
        return statuses[0]

    return asyncio.run(drive())


def _run(tmp_path, name: str, *, mark=None):
    """One smoke case on a fresh daemon: ``(status, delivery_sinks)``."""
    make, fallback, _status, _sink = SMOKE_CASES[name]
    server = _server(tmp_path, fallback=fallback)
    path, body, headers = make()
    if mark is not None:
        headers = {**headers, MARK_HEADER: mark()}
    status = _post(server, path, body, headers)
    sinks = server.pipeline.evidence.snapshot().delivery_sinks
    return status, {route: asdict(sink) for route, sink in sinks.items()}


def _counters(sink: dict) -> dict:
    return {k: v for k, v in sink.items() if not k.endswith(("_at", "_reason"))}


@pytest.mark.parametrize("name", list(SMOKE_CASES))
def test_an_authenticated_mark_moves_no_real_sink(name: str, tmp_path) -> None:
    status, sinks = _run(tmp_path, name, mark=_mark)
    assert status == SMOKE_CASES[name][2]
    assert not [route for route in REAL_SINKS if route in sinks], sinks


@pytest.mark.parametrize("name", list(SMOKE_CASES))
def test_the_same_request_unmarked_does_move_its_sink(name: str, tmp_path) -> None:
    # The control: without it, the test above would pass on a daemon that recorded nothing
    # for anyone. Every smoke case reaches verify, so every one moves its route's sink.
    status, sinks = _run(tmp_path, name)
    assert status == SMOKE_CASES[name][2]
    sink = sinks[SMOKE_CASES[name][3]]
    assert sink["accepted"] + sink["rejected"] == 1, sink


@pytest.mark.parametrize("reason", list(UNAUTHENTICATED_MARKS))
@pytest.mark.parametrize("name", list(SMOKE_CASES))
def test_an_unauthenticated_mark_counts_exactly_as_no_mark(
    name: str, reason: str, tmp_path
) -> None:
    marked_status, marked = _run(tmp_path, name, mark=UNAUTHENTICATED_MARKS[reason])
    plain_status, plain = _run(tmp_path, name)
    assert marked_status == plain_status == SMOKE_CASES[name][2]
    route = SMOKE_CASES[name][3]
    assert route in marked, f"{reason} mark hid the delivery from the ledger: {marked}"
    assert _counters(marked[route]) == _counters(plain[route])
    assert marked[route]["last_reject_reason"] == plain[route]["last_reject_reason"]


def test_the_probe_routes_own_sink_still_counts_a_marked_delivery(tmp_path) -> None:
    # Smoke case 8: the probe injection point on loopback, bad signature. Its sink is
    # already the fleet's (`detail.synthetic`), so the mark changes nothing there.
    server = _server(tmp_path, fallback=False)
    path, body, headers = _probe_bad_signature()
    assert _post(server, path, body, {**headers, MARK_HEADER: _mark()}) == 401
    sinks = server.pipeline.evidence.snapshot().delivery_sinks
    assert sinks["probe"].rejected == 1
    assert not [route for route in REAL_SINKS if route in sinks]


def test_a_real_delivery_after_a_marked_one_is_counted(tmp_path) -> None:
    # The verdict is per request: a marked delivery leaves nothing behind that could keep
    # the next, real one off the sink.
    server = _server(tmp_path, fallback=False)
    for mark in (_mark(), None):
        path, body, headers = _github()
        if mark is not None:
            headers = {**headers, MARK_HEADER: mark}
        assert _post(server, path, body, headers) == 200
    github = server.pipeline.evidence.snapshot().delivery_sinks["github"]
    assert (github.accepted, github.woke, github.rejected) == (1, 1, 0)

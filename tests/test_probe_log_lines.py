"""Every line a probe request produces carries ``source=probe`` (basecradle-router#459).

The telemetry contract's rule for ``source`` is that every line the fleet's own
instrumentation manufactures carries ``source=probe``, so a person watching Live Tail
hides every synthetic line with one filter. A six-hour read of the fleet-server log
source found exactly one line breaking it: uvicorn's own access line for the probe
route, ``127.0.0.1:<port> - "POST /webhooks/probe HTTP/1.1" 202 Accepted``. The
sweep that fixed it found two more — the wake-lock guard's and the breaker's own
``event=wake_refused`` lines, handed a slug and never told which source asked.

So this drives real probe requests through the real ASGI app and pipeline, in every
way a probe can end, and reads **every** record any logger emitted for them — the
router's own and uvicorn's access logger alike — rather than the pipeline's lines
alone, which an earlier test already pinned and which were never the gap.

Four lines are exempt by design, and named here so the exemption is a decision rather
than an accident: they state a fact about the box, not about the request that
happened to observe it. A breaker trip is shared by every wake of its scope, and a
freeze surface that cannot be read is broken for every wake that reads it; tagging
either would let the synthetic filter hide a genuine fault the first time a probe was
the one to find it (the breaker's own docstring: a probe that drives the router to
ERROR is a genuine fault the fleet must see).

No network, no model, no live agent: the ASGI app is driven in-process, the waker is
a stub, and the freeze surface is a directory under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import re
import uuid
from types import MappingProxyType

import pytest

from basecradle_router.breaker import BreakerConfig, WakeRateBreaker
from basecradle_router.config import Config
from basecradle_router.dedup import DeliveryDeduper
from basecradle_router.marker import SCHEME
from basecradle_router.models import Agent, Event
from basecradle_router.pipeline import Pipeline
from basecradle_router.routes import RouteRegistry
from basecradle_router.routes.github import GithubRoute
from basecradle_router.routes.probe import DELIVERY_HEADER, SIGNATURE_HEADER, ProbeRoute
from basecradle_router.secret import Secret
from basecradle_router.server import WebhookServer, configure_logging
from basecradle_router.wake import WakeAttempt, WakeError, WakeResult
from basecradle_router.wakelock import WakeLockGuard

PROBE_SECRET = "fake-probe-route-secret-" + "1" * 32
GITHUB_SECRET = "fake-github-webhook-secret-" + "0" * 32
NOVA = Agent(
    key="basecradle/basecradle-python",
    os_user="nova",
    clone_path="/home/nova/basecradle-python",
    bot_slug="basecradle-python-ai",
)
PROBE_PATH = "/webhooks/probe"
TAG = "source=probe"

#: The lines that state the box's state rather than the request's — see the module
#: docstring. Matched on the line's leading token, colour stripped.
STATE_LINES = frozenset(
    {
        "event=breaker_tripped",
        "event=breaker_reset",
        "event=wake_lock_unreadable",
        "event=wake_lock_stale",
    }
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class _Waker:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def wake(self, agent: Agent, event: Event, attempt: WakeAttempt | None = None) -> WakeResult:
        if self.fail:
            raise WakeError("probe verifier refused the marker", exit_code=3)
        return WakeResult(exit_code=0, stdout="acked")


def _config() -> Config:
    return Config(
        agents=MappingProxyType({NOVA.key: NOVA}),
        enabled_routes=frozenset({"github", "probe"}),
        webhook_secrets=MappingProxyType(
            {"github": Secret(GITHUB_SECRET), "probe": Secret(PROBE_SECRET)}
        ),
        harness_index=MappingProxyType({NOVA.harness_key: NOVA}),
    )


def _registry() -> RouteRegistry:
    registry = RouteRegistry()
    registry.register(GithubRoute(frozenset({"john"})))
    registry.register(ProbeRoute())
    return registry


def _server(tmp_path, *, waker=None, breaker=None, deduper=None) -> WebhookServer:
    kwargs = dict(
        registry=_registry(),
        config=_config(),
        waker=waker or _Waker(),
        wake_lock=WakeLockGuard(lock_dir=str(tmp_path)),
        sleep=lambda _d: None,
    )
    if breaker is not None:
        kwargs["breaker"] = breaker
    if deduper is not None:
        kwargs["deduper"] = deduper
    return WebhookServer(Pipeline(**kwargs))


def _marker() -> str:
    return f"{SCHEME} {uuid.uuid4().hex} {'a' * 64}"


def _probe(delivery: str | None = None, *, secret: str = PROBE_SECRET, body: bytes | None = None):
    """A probe delivery, signed as the NOC's prober signs one: ``(body, headers)``."""
    if body is None:
        body = json.dumps(
            {"harness_key": NOVA.harness_key, "marker": _marker()},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, {
        SIGNATURE_HEADER: f"sha256={digest}",
        DELIVERY_HEADER: delivery or uuid.uuid4().hex,
        "X-BaseCradle-Probe-Event": "probe.wake",
    }


def _post(server: WebhookServer, body: bytes, headers: dict[str, str], *, path=PROBE_PATH) -> int:
    """POST through the ASGI app, drain the wake, and log the access line uvicorn would."""

    async def drive() -> int:
        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
        incoming = [{"type": "http.request", "body": body, "more_body": False}]
        sent: list[dict] = []

        async def receive():
            return incoming.pop(0)

        async def send(message):
            sent.append(message)

        await server(scope, receive, send)
        await server.drain()
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    status = asyncio.run(drive())
    # uvicorn's access line for the same request, in uvicorn's own record shape. It is
    # emitted through the real logger, so the filter the daemon installs decides it.
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:51234", "POST", path, "1.1", status
    )
    return status


def _hold_the_freeze(tmp_path, content: str) -> None:
    (tmp_path / f"{NOVA.harness_key}.lock").write_text(content)


@pytest.fixture
def daemon_logging():
    """The daemon's logging, as its lifespan installs it, restored after the test."""
    pkg = logging.getLogger("basecradle_router")
    access = logging.getLogger("uvicorn.access")
    saved = (pkg.handlers[:], pkg.level, access.filters[:])
    pkg.handlers.clear()
    access.filters.clear()

    def install(server: WebhookServer) -> None:
        configure_logging(stream=io.StringIO(), synthetic_sources=server.synthetic_sources())

    try:
        yield install
    finally:
        pkg.handlers[:], level, access.filters[:] = saved[0], saved[1], saved[2]
        pkg.setLevel(level)


def _scenarios(tmp_path):
    """Every way a probe can end: ``name → (server, sends, statuses)``."""
    freeze = tmp_path / "freeze"
    freeze.mkdir()
    one_wake = BreakerConfig(max_wakes=1, window=600.0, cooldown=600.0)
    dup = uuid.uuid4().hex
    deduper = DeliveryDeduper(ttl=600.0)
    yield "woken", _server(tmp_path), [_probe()]
    yield "bad signature", _server(tmp_path), [_probe(secret="not-the-route-secret")]
    yield "malformed payload", _server(tmp_path), [_probe(body=b'{"harness_key": 7}')]
    yield "deduped", _server(tmp_path, deduper=deduper), [_probe(dup), _probe(dup)]
    yield "failed wake", _server(tmp_path, waker=_Waker(fail=True)), [_probe()]
    # The real breaker: the first admitted, the second trips, the third refused while open.
    breaker = WakeRateBreaker(one_wake)
    yield "breaker open", _server(tmp_path, breaker=breaker), [_probe(), _probe(), _probe()]
    held = '{"reason": "converge", "expires_at": "2099-01-01T00:00:00Z"}'
    yield "freeze held", _server(freeze), [_probe()], held
    yield "freeze unparseable", _server(freeze), [_probe()], "not json"
    stale = '{"reason": "converge", "expires_at": "2000-01-01T00:00:00Z"}'
    yield "freeze stale", _server(freeze), [_probe()], stale


def test_no_line_a_probe_request_produces_lacks_the_tag(tmp_path, caplog, daemon_logging) -> None:
    seen_events: set[str] = set()
    for name, server, deliveries, *lock in _scenarios(tmp_path):
        freeze = tmp_path / "freeze"
        for stale_lock in freeze.glob("*.lock"):
            stale_lock.unlink()
        if lock:
            _hold_the_freeze(freeze, lock[0])
        daemon_logging(server)
        caplog.clear()
        with caplog.at_level(logging.INFO):
            for body, headers in deliveries:
                _post(server, body, headers)

        lines = [_ANSI.sub("", record.getMessage()) for record in caplog.records]
        assert lines, f"{name}: a probe request produced no line at all"
        for line in lines:
            leading = line.split(" ", 1)[0]
            seen_events.add(leading)
            if leading in STATE_LINES:
                continue
            assert TAG in line, f"{name}: a line a probe request produced lacks {TAG}: {line}"
        assert not any("/webhooks/probe" in line for line in lines), (
            f"{name}: uvicorn's access line for the probe route reached the journal"
        )

    # The scenarios reach every refusal the sweep tagged, so none can regress unseen.
    for reached in ("event=wake_refused", "event=breaker_tripped", "event=wake_lock_stale"):
        assert reached in seen_events, f"no scenario reached {reached}"


def test_the_filter_drops_only_a_post_to_a_synthetic_route(daemon_logging, tmp_path) -> None:
    server = _server(tmp_path)
    daemon_logging(server)
    access = logging.getLogger("uvicorn.access")

    def kept(method: str, path: str) -> bool:
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:51234", method, path, "1.1", 202),
            None,
        )
        return access.filter(record)

    assert server.synthetic_sources() == frozenset({"probe"})
    # Every spelling the server reads as the probe source is dropped…
    for path in ("/webhooks/probe", "/webhooks/probe/", "/webhooks//probe", "/webhooks/probe?x=1"):
        assert not kept("POST", path), path
    # …and nothing else: a real route, the edge's mask, a lookalike, and a method the
    # pipeline never logs (answered 404 before it), whose access line is its only trace.
    for method, path in (
        ("POST", "/webhooks/github"),
        ("POST", "/webhooks/masked-probe"),
        ("POST", "/webhooks/probe/below"),
        ("POST", "/probe"),
        ("GET", "/webhooks/probe"),
    ):
        assert kept(method, path), (method, path)


def test_a_box_without_the_probe_route_drops_nothing(daemon_logging, tmp_path) -> None:
    # The sources come from the registry, so a box that never enabled the probe — where a
    # POST to its path is an unknown source — keeps the access line, and a restart under a
    # different registry re-points the one installed filter rather than stacking a second.
    registry = RouteRegistry()
    registry.register(GithubRoute(frozenset({"john"})))
    bare = WebhookServer(
        Pipeline(registry=registry, config=_config(), waker=_Waker(), sleep=lambda _d: None)
    )
    daemon_logging(_server(tmp_path))
    daemon_logging(bare)
    access = logging.getLogger("uvicorn.access")
    tagged = [f for f in access.filters if getattr(f, "_basecradle_router", False)]
    assert len(tagged) == 1
    assert bare.synthetic_sources() == frozenset()
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:51234", "POST", PROBE_PATH, "1.1", 404),
        None,
    )
    assert access.filter(record)


def test_a_refusal_names_a_real_wakes_source_too(caplog, tmp_path) -> None:
    # The tag is the wake's own source, not a probe flag: a real wake refused at either
    # gate says which route it came on, as every other line about a wake does.
    _hold_the_freeze(tmp_path, '{"reason": "converge", "expires_at": "2099-01-01T00:00:00Z"}')
    guard = WakeLockGuard(lock_dir=str(tmp_path))
    breaker = WakeRateBreaker(BreakerConfig(max_wakes=1, window=600.0, cooldown=600.0))
    with caplog.at_level(logging.INFO):
        guard.check(NOVA.harness_key, source="github")
        for _ in range(3):
            breaker.admit(NOVA.harness_key, source="github")
    refusals = [
        _ANSI.sub("", r.getMessage())
        for r in caplog.records
        if "event=wake_refused" in r.getMessage()
    ]
    assert len(refusals) == 2
    assert all(line.endswith("source=github") for line in refusals), refusals
    trip = next(r.getMessage() for r in caplog.records if "event=breaker_tripped" in r.getMessage())
    assert "source=" not in trip  # a genuine trip stays byte-identical

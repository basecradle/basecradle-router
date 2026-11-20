"""The synthetic mark: the fleet's own test traffic is tagged, and only the fleet's (#461).

The deploy smoke test sends signed and forged deliveries through the real github and
basecradle routes, and through the edge's mask, to prove their boundaries on the running
bytes. Its requests carry ``X-BaseCradle-Synthetic``, an HMAC under the probe route's own
secret, and every line the daemon writes for such a request reads ``source=probe via=<the
route it exercised>`` so the fleet's one filter hides it. Four properties are pinned here,
each against the real ASGI app and pipeline, reading every record any logger emitted:

1. **A marked request's lines are all tagged**, in every way the smoke test's requests end
   and in a wake that runs on the scheduler's thread, and its access line is dropped.
2. **A forged, stale, replayed, malformed or missing mark is logged in full**, with one
   ``event=synthetic_mark_refused`` line naming why, and its access line kept.
3. **A mark never changes how a request is judged**: the status, the stages, the wake and
   its evidence are identical with and without one. Only a real route's delivery sink
   differs, on purpose (#464, ``tests/test_smoke_sink_evidence.py``).
4. **A box with no synthetic route honours no mark**, so the mark is only ever the fleet's.

No network, no model, no live agent. Fabricated cast: John Doe (``john``) is the trusted
GitHub actor, Nova Digital (``nova``) the agent. Secrets are correctly-shaped fakes.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import re
import time
import uuid
from types import MappingProxyType

import pytest

from basecradle_router.config import Config
from basecradle_router.marker import SCHEME as MARKER_SCHEME
from basecradle_router.models import Agent, Event
from basecradle_router.pipeline import Pipeline
from basecradle_router.routes import RouteRegistry
from basecradle_router.routes.github import GithubRoute
from basecradle_router.routes.probe import DELIVERY_HEADER, SIGNATURE_HEADER, ProbeRoute
from basecradle_router.secret import Secret
from basecradle_router.server import WebhookServer, configure_logging
from basecradle_router.synthetic import (
    MARK_HEADER,
    UNMARKED,
    WINDOW,
    MarkVerdict,
    SyntheticMarks,
    mint,
)
from basecradle_router.wake import WakeAttempt, WakeResult
from basecradle_router.wakelock import WakeLockGuard

PROBE_SECRET = "fake-probe-route-secret-" + "1" * 32
GITHUB_SECRET = "fake-github-webhook-secret-" + "0" * 32
NOVA = Agent(
    key="basecradle/basecradle-python",
    os_user="nova",
    clone_path="/home/nova/basecradle-python",
    bot_slug="basecradle-python-ai",
)
UNREGISTERED_REPO = "basecradle/__router-smoke-test-do-not-register__"
MASK_PATH = "/webhooks/masked-probe"
STATE_LINES = frozenset(
    {"event=breaker_tripped", "event=breaker_reset", "event=wake_lock_unreadable"}
)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_SOURCE = re.compile(r"(?:^| )source=(\S+)")


def _nonce() -> str:
    return uuid.uuid4().hex


def _mark(*, secret: str = PROBE_SECRET, timestamp: int | None = None, nonce: str | None = None):
    stamp = int(time.time()) if timestamp is None else timestamp
    return mint(Secret(secret), timestamp=stamp, nonce=nonce or _nonce())


# --- the scheme --------------------------------------------------------------------


def _marks(now: float = 1_800_000_000.0) -> tuple[SyntheticMarks, list[float]]:
    """A verifier whose process started long before ``now``, so only the window binds."""
    clock = [now]
    return SyntheticMarks(clock=lambda: clock[0], not_before=now - 100 * WINDOW), clock


SECRETS = {"probe": Secret(PROBE_SECRET)}


def test_a_mark_under_a_synthetic_routes_secret_names_that_route() -> None:
    marks, clock = _marks()
    mark = _mark(timestamp=int(clock[0]))
    assert marks.verify(mark, SECRETS) == MarkVerdict(source="probe")


def test_no_mark_is_the_unmarked_verdict() -> None:
    marks, _ = _marks()
    assert marks.verify(None, SECRETS) is UNMARKED
    assert MarkVerdict(source=None, refused=None) == UNMARKED


@pytest.mark.parametrize(
    "value",
    [
        "",
        "BCSYN1",
        f"BCSYN2 1800000000 {'a' * 32} {'0' * 64}",
        f"BCSYN1 1800000000 {'a' * 15} {'0' * 64}",
        f"BCSYN1 1800000000 {'a' * 65} {'0' * 64}",
        f"BCSYN1 1800000000 {'a' * 32} {'0' * 63}",
        f"BCSYN1 1800000000 {'a' * 32} {'A' * 64}",
        f"BCSYN1 -1 {'a' * 32} {'0' * 64}",
        f" BCSYN1 1800000000 {'a' * 32} {'0' * 64}",
        f"BCSYN1 1800000000 {'a' * 32} {'0' * 64}\n",
        f"BCSYN1  1800000000 {'a' * 32} {'0' * 64}",
    ],
)
def test_anything_not_the_schemes_shape_is_malformed(value: str) -> None:
    marks, _ = _marks()
    assert marks.verify(value, SECRETS) == MarkVerdict(refused="malformed")


def test_a_box_with_no_synthetic_secret_honours_no_mark() -> None:
    marks, clock = _marks()
    assert marks.verify(_mark(timestamp=int(clock[0])), {}) == MarkVerdict(refused="unarmed")


def test_a_mark_under_any_other_secret_is_forged() -> None:
    marks, clock = _marks()
    mark = _mark(secret=GITHUB_SECRET, timestamp=int(clock[0]))
    assert marks.verify(mark, SECRETS) == MarkVerdict(refused="forged")
    # The real route's own secret is not a synthetic one, so it can vouch for nothing.
    assert marks.verify(mark, SECRETS) == MarkVerdict(refused="forged")


def test_a_mark_is_never_a_probe_signature() -> None:
    # The mark's key is derived from the probe secret, never the secret itself: the probe
    # route signs the raw body under the secret, so a mark under the secret would be a
    # valid probe signature for a body of the mark's own bytes, and every mark reaches
    # the journal through the edge's access log.
    from basecradle_router.routes import InboundRequest, SignatureError

    stamp, nonce = 1_800_000_000, _nonce()
    mark = _mark(timestamp=stamp, nonce=nonce)
    message, digest = mark.rsplit(" ", 1)
    forged_probe = InboundRequest(
        headers={SIGNATURE_HEADER: f"sha256={digest}"}, body=message.encode()
    )
    with pytest.raises(SignatureError):
        ProbeRoute().verify(forged_probe, Secret(PROBE_SECRET))
    # ...and an HMAC under the bare secret, the shape a probe signature has, is no mark.
    marks, _ = _marks(now=stamp)
    bare = hmac.new(PROBE_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    assert marks.verify(f"{message} {bare}", SECRETS) == MarkVerdict(refused="forged")


def test_a_mark_stamped_before_the_daemon_started_is_stale() -> None:
    # The memory of used nonces is the process's own, and every merge restarts the
    # daemon: a mark logged before a restart and replayed into the fresh process, inside
    # the window, finds no memory to catch it. So the fresh process refuses it on its
    # timestamp instead.
    now = 1_800_000_000.0
    before = SyntheticMarks(clock=lambda: now, not_before=now - 100 * WINDOW)
    mark = _mark(timestamp=int(now - 5))
    assert before.verify(mark, SECRETS).source == "probe"
    restarted = SyntheticMarks(clock=lambda: now)  # started just now
    assert restarted.not_before == int(now)
    assert restarted.verify(mark, SECRETS) == MarkVerdict(refused="stale")
    # A mark minted after the restart, as the smoke test mints one, is honoured.
    assert restarted.verify(_mark(timestamp=int(now)), SECRETS).source == "probe"


@pytest.mark.parametrize("offset", [-WINDOW - 1, WINDOW + 1], ids=["old", "future"])
def test_a_mark_outside_the_window_is_stale(offset: float) -> None:
    marks, clock = _marks()
    mark = _mark(timestamp=int(clock[0] + offset))
    assert marks.verify(mark, SECRETS) == MarkVerdict(refused="stale")


@pytest.mark.parametrize("offset", [-WINDOW, WINDOW], ids=["oldest", "newest"])
def test_a_mark_at_the_windows_edge_verifies(offset: float) -> None:
    marks, clock = _marks()
    assert marks.verify(_mark(timestamp=int(clock[0] + offset)), SECRETS).source == "probe"


def test_a_mark_verifies_once() -> None:
    marks, clock = _marks()
    mark = _mark(timestamp=int(clock[0]))
    assert marks.verify(mark, SECRETS).source == "probe"
    clock[0] += 60
    assert marks.verify(mark, SECRETS) == MarkVerdict(refused="replayed")
    # Once it could not be fresh, it is stale, and the memory of it is gone.
    clock[0] += WINDOW
    assert marks.verify(mark, SECRETS) == MarkVerdict(refused="stale")
    assert marks._seen == {}


def test_only_a_verified_mark_is_remembered() -> None:
    marks, clock = _marks()
    for _ in range(50):
        marks.verify(_mark(secret=GITHUB_SECRET, timestamp=int(clock[0])), SECRETS)
        marks.verify(_mark(timestamp=int(clock[0] - 10 * WINDOW)), SECRETS)
    assert marks._seen == {}


def test_the_mark_names_whichever_synthetic_route_verified_it() -> None:
    marks, clock = _marks()
    other = "fake-other-synthetic-secret-" + "2" * 32
    secrets = {"probe": Secret(PROBE_SECRET), "canary": Secret(other)}
    assert marks.verify(_mark(secret=other, timestamp=int(clock[0])), secrets).source == "canary"
    assert marks.verify(_mark(timestamp=int(clock[0])), secrets).source == "probe"


def test_mint_refuses_a_nonce_the_verifier_would_not_read() -> None:
    with pytest.raises(ValueError):
        mint(Secret(PROBE_SECRET), timestamp=1_800_000_000, nonce="short")


# --- the daemon --------------------------------------------------------------------


class _Waker:
    def __init__(self) -> None:
        self.woken: list[Event] = []

    def wake(self, agent: Agent, event: Event, attempt: WakeAttempt | None = None) -> WakeResult:
        # The wake runs on a scheduler lane, so this line is written on that thread: it
        # must read as the request's lines do.
        logging.getLogger("basecradle_router.wake").info(
            "event=test_wake_ran %s", _fields(source=event.source)
        )
        self.woken.append(event)
        return WakeResult(exit_code=0, stdout="ok")


def _fields(**pairs: object) -> str:
    from basecradle_router.logfmt import log_fields

    return log_fields(**pairs)


def _config(*routes: str) -> Config:
    secrets = {"github": Secret(GITHUB_SECRET), "probe": Secret(PROBE_SECRET)}
    return Config(
        agents=MappingProxyType({NOVA.key: NOVA}),
        enabled_routes=frozenset(routes),
        webhook_secrets=MappingProxyType({name: secrets[name] for name in routes}),
        harness_index=MappingProxyType({NOVA.harness_key: NOVA}),
    )


def _server(tmp_path, *routes: str) -> WebhookServer:
    routes = routes or ("github", "probe")
    registry = RouteRegistry()
    if "github" in routes:
        registry.register(GithubRoute(frozenset({"john"})))
    if "probe" in routes:
        registry.register(ProbeRoute())
    return WebhookServer(
        Pipeline(
            registry=registry,
            config=_config(*routes),
            waker=_Waker(),
            wake_lock=WakeLockGuard(lock_dir=str(tmp_path)),
            sleep=lambda _d: None,
        )
    )


def _github(*, sender: str = "john", repo: str = UNREGISTERED_REPO, forge: bool = False):
    payload = {
        "action": "labeled",
        "label": {"name": "handoff"},
        "issue": {
            "number": 1,
            "html_url": f"https://github.com/{repo}/issues/1",
            "title": "Router live smoke test (no-op)",
            "labels": [{"name": "handoff"}],
        },
        "repository": {"full_name": repo},
        "sender": {"login": sender, "type": "User"},
    }
    body = json.dumps(payload).encode()
    digest = "0" * 64 if forge else hmac.new(GITHUB_SECRET.encode(), body, "sha256").hexdigest()
    return (
        "/webhooks/github",
        body,
        {
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": f"sha256={digest}",
        },
    )


def _probe(*, forge: bool = False):
    marker = f"{MARKER_SCHEME} {_nonce()} {'a' * 64}"
    body = json.dumps({"harness_key": NOVA.harness_key, "marker": marker}).encode()
    secret = "not-the-probe-secret" if forge else PROBE_SECRET
    return (
        "/webhooks/probe",
        body,
        {
            SIGNATURE_HEADER: "sha256=" + hmac.new(secret.encode(), body, "sha256").hexdigest(),
            DELIVERY_HEADER: _nonce(),
            "X-BaseCradle-Probe-Event": "probe.wake",
        },
    )


def _masked():
    return MASK_PATH, b"{}", {}


#: Every way the smoke test's requests end, plus a wake: ``name → (request, status)``.
SCENARIOS = {
    "github bad signature": (lambda: _github(forge=True), 401),
    "github untrusted sender": (lambda: _github(sender="router-smoke-untrusted-actor"), 400),
    "github trusted, no agent": (lambda: _github(), 200),
    "github handoff woken": (lambda: _github(repo=NOVA.key), 202),
    "the edge's mask": (_masked, 404),
    "probe bad signature": (lambda: _probe(forge=True), 401),
    "probe woken": (_probe, 202),
}


def _post(server: WebhookServer, path: str, body: bytes, headers: dict[str, str]) -> int:
    """POST through the ASGI app and drain its wake, writing uvicorn's access line.

    uvicorn writes the line from inside the app's ``send`` for ``http.response.start``
    (``h11_impl`` and ``httptools_impl`` alike), in the request's own context, so it is
    written there here too: that placement is what lets the filter see the verdict.
    """

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
                logging.getLogger("uvicorn.access").info(
                    '%s - "%s %s HTTP/%s" %d',
                    "127.0.0.1:51234",
                    "POST",
                    path,
                    "1.1",
                    message["status"],
                )

        await server(scope, receive, send)
        await server.drain()
        return statuses[0]

    return asyncio.run(drive())


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


def _run(tmp_path, caplog, daemon_logging, name: str, *, mark=None, routes=()):
    """One scenario, fresh daemon: ``(status, lines, server)``. ``mark`` may be a callable."""
    server = _server(tmp_path, *routes)
    daemon_logging(server)
    make, _status = SCENARIOS[name]
    path, body, headers = make()
    if mark is not None:
        headers = {**headers, MARK_HEADER: mark() if callable(mark) else mark}
    caplog.clear()
    with caplog.at_level(logging.INFO):
        status = _post(server, path, body, headers)
    return status, [_ANSI.sub("", r.getMessage()) for r in caplog.records], server


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_every_line_a_marked_request_produces_is_tagged(
    name: str, tmp_path, caplog, daemon_logging
) -> None:
    status, lines, server = _run(tmp_path, caplog, daemon_logging, name, mark=_mark)
    assert status == SCENARIOS[name][1]
    assert lines, f"{name}: no line at all"
    for line in lines:
        if line.split(" ", 1)[0] in STATE_LINES:
            continue
        sources = _SOURCE.findall(line)
        assert sources == ["probe"], f"{name}: {line}"
    assert not [line for line in lines if '"POST /webhooks/' in line], f"{name}: access line"
    assert not [line for line in lines if "synthetic_mark_refused" in line]
    # The route it exercised stays readable, beside the tag, on every line of a real route.
    if not name.startswith("probe"):
        # The mask target names no route, so its lines read `unrouted` (#462).
        exercised = "unrouted" if name == "the edge's mask" else "github"
        tagged = [line for line in lines if "source=probe" in line]
        assert all(f"source=probe via={exercised}" in line for line in tagged), (name, tagged)
    if name.endswith("woken"):
        assert any("event=test_wake_ran" in line for line in lines), "the wake never ran"
        assert any(line.startswith("stage=wake ") for line in lines)


def _refusals(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("event=synthetic_mark_refused")]


#: Every way a mark can fail, and the reason the refusal line names.
BAD_MARKS = {
    "forged": lambda: _mark(secret=GITHUB_SECRET),
    "stale": lambda: _mark(timestamp=int(time.time() - 10 * WINDOW)),
    "malformed": lambda: "BCSYN1 i-am-the-fleet-honest",
}


@pytest.mark.parametrize("reason", list(BAD_MARKS))
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_a_mark_that_does_not_verify_is_logged_in_full(
    name: str, reason: str, tmp_path, caplog, daemon_logging
) -> None:
    status, lines, _ = _run(tmp_path, caplog, daemon_logging, name, mark=BAD_MARKS[reason])
    _, unmarked, _ = _run(tmp_path, caplog, daemon_logging, name)
    assert status == SCENARIOS[name][1]
    (refusal,) = _refusals(lines)
    assert f"reason={reason}" in refusal
    assert "via=" not in refusal
    # Everything else is byte for byte what the same request without a mark produces,
    # its access line included, once the per-request ids are set aside. Compared as a
    # multiset, because a wake's lines are written on its own thread while the response
    # is sent, so the two interleave differently from run to run.
    assert lines[0] == refusal
    assert sorted(_shape(lines[1:])) == sorted(_shape(unmarked))
    access = [line for line in lines if '"POST /webhooks/' in line]
    # A probe route's access line is dropped for every request (#459); anything else keeps it.
    assert len(access) == (0 if name.startswith("probe") else 1), access


def _shape(lines: list[str]) -> list[str]:
    """``lines`` with the per-request ids and timings blanked, so two runs compare."""
    blanked = []
    for line in lines:
        line = re.sub(r"delivery=\S+", "delivery=*", line)
        line = re.sub(r"(duration|elapsed)=\S+", r"\1=*", line)
        line = re.sub(r"BCNOC1 \S+", "BCNOC1 *", line)
        blanked.append(line)
    return blanked


def test_a_mark_is_honoured_once(tmp_path, caplog, daemon_logging) -> None:
    server = _server(tmp_path)
    daemon_logging(server)
    mark = _mark()
    for expected in ("tagged", "replayed"):
        path, body, headers = _github(forge=True)
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert _post(server, path, body, {**headers, MARK_HEADER: mark}) == 401
        lines = [_ANSI.sub("", r.getMessage()) for r in caplog.records]
        if expected == "tagged":
            assert not _refusals(lines)
            assert all("source=github" not in line for line in lines)
        else:
            (refusal,) = _refusals(lines)
            assert "reason=replayed" in refusal
            assert any("stage=verify outcome=rejected source=github" in line for line in lines)


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_a_mark_never_changes_how_a_request_is_judged(
    name: str, tmp_path, caplog, daemon_logging
) -> None:
    marked = _run(tmp_path, caplog, daemon_logging, name, mark=_mark)
    plain = _run(tmp_path, caplog, daemon_logging, name)
    assert marked[0] == plain[0] == SCENARIOS[name][1]
    # The stage verdicts, in order, are the same...
    assert _verdicts(marked[1]) == _verdicts(plain[1])
    # ...and so is everything the router recorded as evidence, but for the one difference
    # #464 made on purpose: a real route's delivery sink never counts the fleet's own
    # traffic (tests/test_smoke_sink_evidence.py). A synthetic route's sink still does.
    synthetic = marked[2].synthetic_sources()
    expected = _evidence(plain[2])
    expected["delivery_sinks"] = {
        route: sink for route, sink in expected["delivery_sinks"].items() if route in synthetic
    }
    assert _evidence(marked[2]) == expected
    assert len(marked[2].pipeline.waker.woken) == len(plain[2].pipeline.waker.woken)


def _verdicts(lines: list[str]) -> list[str]:
    return [" ".join(line.split(" ", 2)[:2]) for line in lines if line.startswith("stage=")]


def _evidence(server: WebhookServer) -> dict:
    doc = server.pipeline.evidence.snapshot().to_json()

    def scrub(value):
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items() if not k.endswith(("_at", "delivery"))}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    return scrub(doc)


@pytest.mark.parametrize("name", ["github bad signature", "github trusted, no agent"])
def test_a_box_with_no_synthetic_route_honours_no_mark(
    name: str, tmp_path, caplog, daemon_logging
) -> None:
    status, lines, _ = _run(tmp_path, caplog, daemon_logging, name, mark=_mark, routes=("github",))
    assert status == SCENARIOS[name][1]
    (refusal,) = _refusals(lines)
    assert "reason=unarmed" in refusal
    assert any("source=github" in line and "via=" not in line for line in lines)
    assert any('"POST /webhooks/github' in line for line in lines)


def test_the_mark_header_is_read_first_value_wins(tmp_path, caplog, daemon_logging) -> None:
    # A second copy appended after a forged first can never stand in for it.
    server = _server(tmp_path)
    daemon_logging(server)
    path, body, headers = _github(forge=True)
    scope_headers = [
        *headers.items(),
        (MARK_HEADER, _mark(secret=GITHUB_SECRET)),
        (MARK_HEADER, _mark()),
    ]

    async def drive() -> None:
        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(k.lower().encode(), v.encode()) for k, v in scope_headers],
        }
        incoming = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return incoming.pop(0)

        async def send(_message):
            return None

        await server(scope, receive, send)
        await server.drain()

    with caplog.at_level(logging.INFO):
        asyncio.run(drive())
    lines = [_ANSI.sub("", r.getMessage()) for r in caplog.records]
    (refusal,) = _refusals(lines)
    assert "reason=forged" in refusal


@pytest.mark.parametrize(
    "path",
    ["/webhooks/masked-probe/below", "/webhooks/", "/webhooks//"],
    ids=["below", "empty", "slashes"],
)
def test_a_mark_on_a_path_naming_no_route_is_spent_and_its_access_line_dropped(
    path: str, tmp_path, caplog, daemon_logging
) -> None:
    # Answered from the path alone, `no route in path`, which writes no pipeline line: the
    # mark is still checked first, so it is spent there, and the access line goes with it.
    server = _server(tmp_path)
    daemon_logging(server)
    mark = _mark()
    for expected in ("spent", "replayed"):
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert _post(server, path, b"{}", {MARK_HEADER: mark}) == 404
        lines = [_ANSI.sub("", r.getMessage()) for r in caplog.records]
        if expected == "spent":
            assert lines == [], lines
        else:
            (refusal,) = _refusals(lines)
            assert "reason=replayed" in refusal
            assert any('"POST /webhooks/' in line for line in lines)


def test_no_verdict_outlives_its_request(tmp_path, caplog, daemon_logging) -> None:
    # A marked request, then an unmarked one on the same daemon: the second is in full.
    server = _server(tmp_path)
    daemon_logging(server)
    path, body, headers = _github(forge=True)
    _post(server, path, body, {**headers, MARK_HEADER: _mark()})
    path, body, headers = _github(forge=True)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        _post(server, path, body, headers)
    lines = [_ANSI.sub("", r.getMessage()) for r in caplog.records]
    assert any("stage=verify outcome=rejected source=github" in line for line in lines)
    assert any('"POST /webhooks/github' in line for line in lines)
    assert _fields(source="github") == "source=github"


class _Transport(asyncio.Transport):
    """Just enough of a socket for uvicorn's h11 protocol to answer one request into."""

    def __init__(self) -> None:
        super().__init__()
        self.written = b""
        self.closed = False

    def write(self, data) -> None:
        self.written += bytes(data)

    def get_extra_info(self, name, default=None):
        return {"peername": ("127.0.0.1", 51234), "sockname": ("127.0.0.1", 8000)}.get(
            name, default
        )

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    def pause_reading(self) -> None:
        return None

    def resume_reading(self) -> None:
        return None


@pytest.mark.parametrize("marked", [True, False], ids=["marked", "unmarked"])
def test_uvicorns_own_access_line_is_dropped_only_for_a_marked_request(
    marked: bool, tmp_path, daemon_logging
) -> None:
    # The drop depends on one fact about uvicorn rather than about this repo: it writes
    # the access line from inside the app's `send`, in the request's own context, where
    # the verdict is set. So the real daemon is served here by uvicorn's real h11 protocol
    # (the one the box runs: the lock carries no httptools), over a fake socket.
    from uvicorn.config import Config as UvicornConfig
    from uvicorn.protocols.http.h11_impl import H11Protocol
    from uvicorn.server import ServerState

    server = _server(tmp_path)
    daemon_logging(server)
    access = logging.getLogger("uvicorn.access")
    written: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            written.append(record)

    keep, level = _Keep(), access.level
    access.addHandler(keep)  # uvicorn writes access lines only when the logger has one
    access.setLevel(logging.INFO)
    path, body, headers = _github(forge=True)
    if marked:
        headers = {**headers, MARK_HEADER: _mark()}
    request = (
        f"POST {path} HTTP/1.1\r\nHost: ai.example.com\r\nContent-Length: {len(body)}\r\n"
        + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        + "\r\n"
    ).encode() + body

    async def serve() -> bytes:
        config = UvicornConfig(app=server, access_log=True, lifespan="off", log_config=None)
        protocol = H11Protocol(config, ServerState(), {}, asyncio.get_running_loop())
        transport = _Transport()
        protocol.connection_made(transport)
        protocol.data_received(request)
        for _ in range(200):
            if b"\r\n\r\n" in transport.written:
                break
            await asyncio.sleep(0)
        await server.drain()
        return transport.written

    try:
        answer = asyncio.run(serve())
    finally:
        access.removeHandler(keep)
        access.setLevel(level)
    assert answer.startswith(b"HTTP/1.1 401 "), answer[:40]
    assert len(written) == (0 if marked else 1), [r.getMessage() for r in written]

"""The ASGI webhook front-end: receive a POST, hand it to the core pipeline.

A deliberately tiny, framework-free ASGI app — one endpoint shape,
``POST /webhooks/<source>`` — so the box that holds the fleet's credentials runs
the smallest surface we can manage: Python stdlib, no web framework.

The app is async but the pipeline is synchronous and blocking (the threaded
model: a wake is a minutes-long subprocess). The bridge is the
:class:`~basecradle_router.scheduler.WakeScheduler` — an explicitly-sized,
per-agent-fair thread pool that runs each wake off the event loop. It replaced a
naive :func:`asyncio.to_thread` hand-off onto the *implicit default executor*,
whose ``cpu_count``-derived size and global-FIFO queue let one busy agent's
backlog starve every other agent's wakes — including the NOC's transport probe
(basecradle-router#182). The scheduler serialises an agent's wakes by *scheduling*
(one in flight per agent) rather than by parking a pool thread on a blocking lock,
and dispatches fairly across agents, so a hot timeline can no longer monopolise the
pool. See its module docstring for the full incident and design.

**Fast-ack.** A wake takes minutes; GitHub abandons a webhook after ~10s. So the
server runs only the pipeline's fast :meth:`~basecradle_router.pipeline.Pipeline.accept`
half (route → verify → normalize → resolve — microseconds, run inline) on the
request path, answers immediately, and runs the slow
:meth:`~basecradle_router.pipeline.Pipeline.execute` half (lock → wake)
as a *background task* on the thread pool. The HTTP response therefore reflects
the **accept** stage:
``202`` accepted-and-now-processing, ``401`` bad signature, ``404`` no such
source, ``400`` malformed payload, ``200`` ignored or logged-failure. The wake's
own outcome is not in the response — it lands in the structured log, and the
woken agent reports separately by commenting on the issue.

In-flight and queued wakes are owned by the scheduler, so
:meth:`WebhookServer.drain` awaits *its* idle rather than tracking asyncio tasks —
the drain the shutdown lifespan (and the tests) rely on to let a wake finish.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from basecradle_router import __version__
from basecradle_router.config import DEFAULT_WAKE_LANES
from basecradle_router.models import Agent, Event
from basecradle_router.pipeline import Outcome, Pipeline, PipelineResult, Stage, log_fields
from basecradle_router.routes import InboundRequest
from basecradle_router.scheduler import WakeScheduler

WEBHOOK_PREFIX = "/webhooks/"

# The package logger every router log line descends from — the pipeline's per-stage
# records and the routes' ignore-vs-act decision lines (#91) all log under it.
_ROOT_LOGGER = "basecradle_router"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

#: uvicorn's HTTP access logger — the one we filter ``/up`` out of (see
#: :class:`_SuppressLivenessAccessLog`). Not our package, so it is configured by
#: uvicorn and only *adjusted* here.
_ACCESS_LOGGER = "uvicorn.access"

logger = logging.getLogger("basecradle_router.server")

#: Where the NOC's ``deploy-router`` op stamps the commit it deployed (world-readable
#: ``0644``, part of the on-box contract in ``deploy/README.md`` — the same file
#: ``drift-check.sh`` reads). The startup banner quotes it, so the log *itself* says
#: which bytes the running daemon is: the merged≠live gap (#54) made visible at boot.
DEPLOYED_SHA_FILE = "/etc/basecradle-router/deployed-sha"


def deployed_sha(path: str = DEPLOYED_SHA_FILE) -> str:
    """The deployed commit from the stamp file, or ``"unknown"``.

    Never raises: the daemon runs on a laptop and in tests too, where the stamp does
    not exist, and no observability nicety may be able to stop the daemon booting.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip() or "unknown"
    except OSError:
        return "unknown"


class _SuppressLivenessAccessLog(logging.Filter):
    """Drop uvicorn's access line for ``GET /up`` — and only that one (#170).

    Better Stack's uptime monitor probes ``/up`` about once a minute, forever, so its
    access lines are the single largest source of volume in the journal and in Live
    Tail — pure noise that buries the lines an operator actually reads. Every *other*
    access line is kept.

    uvicorn logs access as ``('%s - "%s %s HTTP/%s" %d', client, method, path, ver,
    status)``, so the path is ``args[2]``. If that shape ever changes the filter
    **keeps the record** rather than guessing — the fail-direction for a log filter is
    to log too much, never to silently swallow.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 3 or not isinstance(args[2], str):
            return True
        return args[2].split("?", 1)[0] != LIVENESS_PATH


def configure_logging(level: int = logging.INFO, *, stream=None) -> None:
    """Route the daemon's own loggers to stdout at ``level`` (systemd → journald).

    Without this the observability was *silent on the deployed box* (#91): uvicorn
    configures only its ``uvicorn.*`` loggers, so our ``basecradle_router.*``
    loggers inherit the root's default ``WARNING`` — which dropped every INFO
    record (the per-stage pipeline log *and* the ignore-vs-act decision lines)
    while WARNING rejections still surfaced. The very signal meant to kill
    "green-while-dead" was itself green-while-dead. Attaching a stdout handler to
    the package logger at INFO is what makes those lines actually reach the
    operator's journal.

    Also installs the ``/up`` access-log filter on uvicorn's access logger — done
    here because this is the one function that runs inside the *live* daemon, after
    uvicorn has built its loggers, and never in a unit test.

    Called from the ASGI lifespan startup, so it configures the *running daemon*
    and never the unit tests (which drive the HTTP path, never lifespan). It is
    idempotent — a second startup re-uses the existing handler instead of stacking
    a duplicate — and leaves ``propagate`` on, so a caplog-based test still sees
    records through the root.
    """
    _suppress_liveness_access_log()
    pkg = logging.getLogger(_ROOT_LOGGER)
    pkg.setLevel(level)
    for existing in pkg.handlers:
        if getattr(existing, "_basecradle_router", False):
            existing.setLevel(level)
            return
    handler = logging.StreamHandler(sys.stdout if stream is None else stream)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler._basecradle_router = True  # tag our own handler so re-config is idempotent
    pkg.addHandler(handler)


def _suppress_liveness_access_log() -> None:
    """Attach :class:`_SuppressLivenessAccessLog` to uvicorn's access logger, once.

    Tagged and re-checked so a second lifespan startup does not stack a duplicate
    filter, exactly as the stdout handler above is.
    """
    access = logging.getLogger(_ACCESS_LOGGER)
    for existing in access.filters:
        if getattr(existing, "_basecradle_router", False):
            return
    log_filter = _SuppressLivenessAccessLog()
    log_filter._basecradle_router = True
    access.addFilter(log_filter)


# The fleet-uniform liveness path (constitution → Operational Baselines). Served
# from the app itself — so a green ``GET /up`` proves *uvicorn* is up (true service
# health), not merely that Caddy or the host replied. The status, body, and
# content type match Rails' ``Rails::HealthController`` output verbatim, so
# ``basecradle.com/up`` (the Rails platform) and ``ai.basecradle.com/up`` (this
# service) are indistinguishable to any checker — one path, checked the same way
# everywhere. It is the single public liveness contract: there is deliberately no
# competing ``/healthz``.
LIVENESS_PATH = "/up"
LIVENESS_BODY = b'<!DOCTYPE html><html><body style="background-color: green"></body></html>'


class WebhookServer:
    """An ASGI application wrapping a :class:`~basecradle_router.pipeline.Pipeline`."""

    def __init__(
        self, pipeline: Pipeline, *, prefix: str = WEBHOOK_PREFIX, lanes: int = DEFAULT_WAKE_LANES
    ) -> None:
        self.pipeline = pipeline
        self.prefix = prefix
        # The wake scheduler owns the thread pool and the per-agent queues: it runs the
        # pipeline's slow half fairly, one wake in flight per agent, on `lanes` threads
        # (basecradle-router#182). `lanes` is injected from config by the app factory;
        # the default keeps a bare WebhookServer(pipeline) runnable in tests.
        self._scheduler = WakeScheduler(pipeline.execute, lanes=lanes)

    async def __call__(self, scope: dict, receive, send) -> None:
        scope_type = scope["type"]
        if scope_type == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope_type != "http":  # websockets etc. are not part of this surface
            return

        # Liveness probe: a tiny, unauthenticated GET answered before the webhook
        # dispatch and entirely outside the pipeline — no body read, no signature,
        # no route/verify machinery. A health check must stay cheap and never look
        # like traffic the core has to reason about.
        if scope["method"] == "GET" and scope["path"] == LIVENESS_PATH:
            await self._send_liveness(send)
            return

        if scope["method"] != "POST" or not scope["path"].startswith(self.prefix):
            await self._send(send, 404, {"error": "not found"})
            return

        source = scope["path"][len(self.prefix) :].strip("/")
        if not source or "/" in source:
            await self._send(send, 404, {"error": "no route in path"})
            return

        body = await self._read_body(receive)
        request = InboundRequest(headers=self._headers(scope), body=body)

        # Fast-ack: the accept half (HMAC verify + JSON normalize + a dict lookup)
        # is microseconds, so run it inline and answer at once; hand only the
        # minutes-long wake to a background task. Keeping accept off the thread
        # pool is deliberate — if wakes saturate the pool, acks must not queue
        # behind them, or fast-ack would defeat itself under load.
        accepted = self.pipeline.accept(source, request)
        if accepted.pending is not None:
            # Snapshot the accept-half summary *before* spawning the wake: the wake
            # appends to this same result object, and the ack must reflect only what
            # was decided synchronously. (Today's evaluation order makes spawn-then-
            # read safe too, but reading first is robust to any future edit.)
            summary = _summary(accepted.result)
            self._spawn_wake(accepted.pending, accepted.result)
            await self._send(send, 202, summary)
        else:
            await self._send(send, _status_for(accepted.result), _summary(accepted.result))

    def _spawn_wake(self, pending: tuple[Agent, Event], result: PipelineResult) -> None:
        """Hand the slow ``execute`` half to the scheduler; it runs it off the loop.

        Returns at once — the scheduler enqueues the wake and dispatches it onto a free
        lane, one in flight per agent, so the ack (already sent) never waits and a busy
        agent's backlog never parks the pool.
        """
        agent, event = pending
        self._scheduler.submit(agent, event, result)

    async def drain(self) -> None:
        """Await all queued and in-flight wakes — called on shutdown, and by tests.

        Blocks (off the event loop) until the scheduler is idle. Idempotent: it does
        not close the pool, so a caller may drain then submit again (the tests do); the
        shutdown lifespan closes the pool after its final drain.
        """
        await asyncio.to_thread(self._scheduler.wait_idle)

    def log_startup_banner(self) -> None:
        """State, in one INFO line, what config this running router booted with (#170).

        The config a *live* router is running under was previously unknowable from its
        logs: which routes it accepts, how long it dedups, where the breaker trips,
        and — the one that matters most — *which commit it is*. All of it had to be
        inferred from the box. So the daemon now says it at boot, read from the live
        collaborators (not restated defaults), which makes the banner the natural first
        thing to read when a Live Tail looks wrong: it is either absent (the daemon did
        not start), stale (`sha=` is not the merged one — the #54 merged≠live gap), or
        it disagrees with what you expected to be configured.
        """
        breaker = self.pipeline.breaker.config
        logger.info(
            "event=startup %s",
            log_fields(
                version=__version__,
                sha=deployed_sha(),
                routes=",".join(sorted(self.pipeline.config.enabled_routes)),
                dedup_ttl=f"{self.pipeline.deduper.ttl:.0f}s",
                wake_lanes=self._scheduler.lanes,
                wake_attempts=self.pipeline.wake_attempts,
                breaker_max=breaker.max_wakes,
                breaker_stream_max=breaker.stream_max_wakes,
                breaker_window=f"{breaker.window:.0f}s",
                breaker_cooldown=f"{breaker.cooldown:.0f}s",
            ),
        )

    async def _lifespan(self, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                # Configure logging here — when the daemon actually starts serving —
                # so the running process emits its INFO observability to journald,
                # without mutating global logging state during unit tests (which
                # drive the HTTP path, never the lifespan protocol). See #91.
                configure_logging()
                # Strictly after configure_logging, or the banner would be the one
                # line the daemon drops on the floor at the root's default WARNING.
                self.log_startup_banner()
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                # Let in-flight wakes finish before we go down, so a deploy/restart
                # never severs an agent mid-task. (A wake's own timeout, plus the
                # service's stop timeout, bound how long this can take.) uvicorn has
                # already stopped accepting connections, so no new wake is submitted
                # between the drain and the pool teardown that follows it.
                await self.drain()
                await asyncio.to_thread(self._scheduler.shutdown)
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _read_body(self, receive) -> bytes:
        # Collect the whole raw body before verifying: the signature is over the
        # exact bytes, so a partial read would reject a valid request.
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break
        return b"".join(chunks)

    @staticmethod
    def _headers(scope: dict) -> dict[str, str]:
        # ASGI delivers header names/values as latin-1 bytes; InboundRequest does
        # the case-insensitive lookup, so we keep them as decoded strings. First
        # value wins on a duplicated name, so a trailing injected header (e.g. a
        # second X-Hub-Signature-256) can never shadow the legitimate leading one.
        headers: dict[str, str] = {}
        for key, value in scope["headers"]:
            headers.setdefault(key.decode("latin-1"), value.decode("latin-1"))
        return headers

    @staticmethod
    async def _send_liveness(send) -> None:
        # Verbatim Rails health output (status + body + content type) so the fleet's
        # liveness check is uniform across the Rails platform and this service.
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/html; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": LIVENESS_BODY})

    @staticmethod
    async def _send(send, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": payload})


def _status_for(result: PipelineResult) -> int:
    """Map a *non-actionable* accept terminal to an HTTP status.

    Called only when ``accept`` produced no wake to run (the actionable case is
    answered ``202`` in ``__call__``). The contract is the webhook convention: a
    request that fails verification or parsing is a client error (4xx); an event
    that is well-formed but not actionable — ignored, or a misconfiguration we
    log (no agent for the repo, no secret for the route) — is *accepted* (200),
    because returning 5xx would only invite a retry storm that cannot help.
    """
    if not result.records:
        return 500  # never happens — accept() always records at least the route
    last = result.records[-1]
    if last.outcome is Outcome.REJECTED:
        if last.stage is Stage.VERIFY:
            return 401  # bad or missing signature
        if last.stage is Stage.ROUTE:
            return 404  # no such webhook source
        return 400  # malformed payload
    return 200  # IGNORED, or an internally-logged FAILED — the event is accepted


def _summary(result: PipelineResult) -> dict:
    """A compact, machine-readable record of the trip — the response body."""
    return {
        "outcome": result.terminal.value if result.terminal else "none",
        "stages": [[stage.value, outcome.value] for stage, outcome in result.stages],
    }

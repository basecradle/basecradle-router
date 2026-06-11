"""The ASGI webhook front-end: receive a POST, hand it to the core pipeline.

A deliberately tiny, framework-free ASGI app — one endpoint shape,
``POST /webhooks/<source>`` — so the box that holds the fleet's credentials runs
the smallest surface we can manage: Python stdlib, no web framework.

The app is async but the pipeline is synchronous and blocking (the threaded
model: a wake is a minutes-long subprocess). The bridge is
:func:`asyncio.to_thread`, which runs the blocking pipeline on a worker thread
and leaves the event loop free — and lets the per-agent ``threading.Lock``
serialize same-agent wakes (from any source) across those threads.

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

In-flight background wakes are tracked so they are not garbage-collected
mid-flight and so :meth:`WebhookServer.drain` can await them on shutdown.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from basecradle_router.models import Agent, Event
from basecradle_router.pipeline import Outcome, Pipeline, PipelineResult, Stage
from basecradle_router.routes import InboundRequest

WEBHOOK_PREFIX = "/webhooks/"

# The package logger every router log line descends from — the pipeline's per-stage
# records and the routes' ignore-vs-act decision lines (#91) all log under it.
_ROOT_LOGGER = "basecradle_router"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


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

    Called from the ASGI lifespan startup, so it configures the *running daemon*
    and never the unit tests (which drive the HTTP path, never lifespan). It is
    idempotent — a second startup re-uses the existing handler instead of stacking
    a duplicate — and leaves ``propagate`` on, so a caplog-based test still sees
    records through the root.
    """
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

    def __init__(self, pipeline: Pipeline, *, prefix: str = WEBHOOK_PREFIX) -> None:
        self.pipeline = pipeline
        self.prefix = prefix
        # Strong references to in-flight background wakes: without these the tasks
        # could be garbage-collected mid-run, and drain() needs them on shutdown.
        self._pending: set[asyncio.Task] = set()

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
        """Run the slow ``execute`` half in the background, tracked so it survives."""
        agent, event = pending
        task = asyncio.create_task(asyncio.to_thread(self.pipeline.execute, agent, event, result))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self) -> None:
        """Await all in-flight background wakes — called on shutdown, and by tests.

        A snapshot is taken because each task's done-callback mutates ``_pending``
        as it completes. ``return_exceptions`` keeps one failed wake from
        cancelling the others' drain (the pipeline already records failures).
        """
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)

    async def _lifespan(self, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                # Configure logging here — when the daemon actually starts serving —
                # so the running process emits its INFO observability to journald,
                # without mutating global logging state during unit tests (which
                # drive the HTTP path, never the lifespan protocol). See #91.
                configure_logging()
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                # Let in-flight wakes finish before we go down, so a deploy/restart
                # never severs an agent mid-task. (A wake's own timeout, plus the
                # service's stop timeout, bound how long this can take.)
                await self.drain()
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

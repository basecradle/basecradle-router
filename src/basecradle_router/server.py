"""The ASGI webhook front-end: receive a POST, hand it to the core pipeline.

A deliberately tiny, framework-free ASGI app — one endpoint shape,
``POST /webhooks/<source>`` — so the box that holds the fleet's credentials runs
the smallest surface we can manage: Python stdlib, no web framework.

The app is async but the pipeline is synchronous and blocking (the threaded
model: a wake is a minutes-long subprocess). The bridge is
:func:`asyncio.to_thread`, which runs the blocking pipeline on a worker thread
and leaves the event loop free — and lets the per-repo ``threading.Lock``
serialize same-repo wakes across those threads.

**Fast-ack.** A wake takes minutes; GitHub abandons a webhook after ~10s. So the
server runs only the pipeline's fast :meth:`~basecradle_router.pipeline.Pipeline.accept`
half (route → verify → normalize → resolve — microseconds, run inline) on the
request path, answers immediately, and runs the slow
:meth:`~basecradle_router.pipeline.Pipeline.execute` half (lock → wake → merge)
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

from basecradle_router.models import Agent, Event
from basecradle_router.pipeline import Outcome, Pipeline, PipelineResult, Stage
from basecradle_router.routes import InboundRequest

WEBHOOK_PREFIX = "/webhooks/"


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
        "decision": result.decision.value if result.decision else None,
    }

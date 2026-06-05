"""The ASGI webhook front-end: receive a POST, hand it to the core pipeline.

A deliberately tiny, framework-free ASGI app — one endpoint shape,
``POST /webhooks/<source>`` — so the box that holds the fleet's credentials runs
the smallest surface we can manage: Python stdlib, no web framework.

The app is async but the pipeline is synchronous and blocking (the threaded
model: a wake is a minutes-long subprocess). The bridge is
:func:`asyncio.to_thread`, which runs the blocking pipeline on a worker thread
and leaves the event loop free — and lets the per-repo ``threading.Lock``
serialize same-repo wakes across those threads.

The HTTP response reflects the pipeline's terminal stage: ``2xx`` accepted,
``401`` bad signature, ``400`` malformed/unroutable, ``5xx`` an internal stage
failure. (Production fast-ack — answering GitHub's ~10s webhook timeout *before*
the wake runs — is a deploy concern deferred with the home server.)
"""

from __future__ import annotations

import asyncio
import json

from basecradle_router.pipeline import Outcome, Pipeline, PipelineResult, Stage
from basecradle_router.routes import InboundRequest

WEBHOOK_PREFIX = "/webhooks/"


class WebhookServer:
    """An ASGI application wrapping a :class:`~basecradle_router.pipeline.Pipeline`."""

    def __init__(self, pipeline: Pipeline, *, prefix: str = WEBHOOK_PREFIX) -> None:
        self.pipeline = pipeline
        self.prefix = prefix

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

        # Run the blocking pipeline off the event loop; the threaded model relies
        # on each wake holding a real thread so the per-repo lock can serialize.
        result = await asyncio.to_thread(self.pipeline.handle, source, request)

        await self._send(send, _status_for(result), _summary(result))

    async def _lifespan(self, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
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
    """Map the pipeline's terminal stage outcome to an HTTP status.

    The contract is the webhook convention: a request that fails verification or
    parsing is a client error (4xx); once the signature is valid the event is
    *accepted* (2xx), and a downstream stage failure (no agent for the repo, a
    wake that exhausted its retries) is ours to log and handle, not GitHub's to
    retry — returning 5xx there would only invite a retry storm that cannot help.
    """
    if not result.records:
        return 500  # never happens — handle() always records at least the route
    last = result.records[-1]
    if last.outcome is Outcome.REJECTED:
        if last.stage is Stage.VERIFY:
            return 401  # bad or missing signature
        if last.stage is Stage.ROUTE:
            return 404  # no such webhook source
        return 400  # malformed payload
    return 200  # OK, IGNORED, or an internally-logged FAILED — the event is accepted


def _summary(result: PipelineResult) -> dict:
    """A compact, machine-readable record of the trip — the response body."""
    return {
        "outcome": result.terminal.value if result.terminal else "none",
        "stages": [[stage.value, outcome.value] for stage, outcome in result.stages],
        "decision": result.decision.value if result.decision else None,
    }

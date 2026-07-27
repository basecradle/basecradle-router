"""The route contract: what every event-source module must implement.

A route owns exactly one source's specifics — verify its signature, normalize
its payload into a core :class:`~basecradle_router.models.Event`. The core
pipeline depends only on this protocol, so adding a source is implementing one
route, never forking the daemon.
"""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Any, Protocol, runtime_checkable

from basecradle_router.models import Event

HMAC_SHA256_PREFIX = "sha256="

logger = logging.getLogger("basecradle_router.routes")


class RouteError(Exception):
    """Base for route-layer failures."""


class SignatureError(RouteError):
    """The request signature was missing or did not match the secret."""


class PayloadError(RouteError):
    """The payload was structurally malformed and could not be parsed."""


class UntrustedSenderError(RouteError):
    """A verified webhook asked for a wake, but its actor is not a trusted fleet member.

    Distinct from :class:`PayloadError` (which means *malformed*): the payload is
    well-formed and genuinely from the source, but the actor who triggered it
    (e.g. who applied the ``handoff`` label) is not on the route's allow-list.
    Defense-in-depth behind the source's own permission model — the core records
    it as a *rejection* and no agent is woken.
    """


@dataclass(frozen=True, slots=True)
class InboundRequest:
    """A transport-neutral inbound webhook: the raw body and its headers.

    The HTTP server builds this from the live request; routes operate only on it,
    so signature/normalize logic is testable with no real transport.
    """

    headers: Mapping[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        """Case-insensitive header lookup (HTTP header names are case-insensitive)."""
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return value
        return None


def verify_hmac_sha256(request: InboundRequest, secret: str, *, header: str) -> None:
    """Raise :class:`SignatureError` unless ``request`` carries a valid signature.

    The shared signature boundary for every HMAC-signed source (GitHub's
    ``X-Hub-Signature-256``, BaseCradle's ``X-BaseCradle-Signature``): both sign
    the **raw** request body with HMAC-SHA256 under a per-route shared secret and
    deliver the digest as ``sha256=<hexdigest>``. Valid means a present, correctly
    prefixed header whose digest equals the HMAC of the exact body bytes under
    ``secret``; the comparison is constant-time. One audited implementation keeps
    every route's security boundary identical and impossible to subtly diverge.
    """
    provided = request.header(header)
    if provided is None:
        raise SignatureError(f"missing {header} header")
    if not provided.startswith(HMAC_SHA256_PREFIX):
        raise SignatureError(f"malformed {header}: expected '{HMAC_SHA256_PREFIX}<hexdigest>'")

    expected = (
        HMAC_SHA256_PREFIX + hmac.new(secret.encode("utf-8"), request.body, sha256).hexdigest()
    )

    # Compare as bytes: hmac.compare_digest raises TypeError on a str with
    # non-ASCII chars, and the header value is attacker-controlled.
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("ascii")):
        raise SignatureError(f"{header} does not match the request body")


def parse_json_object(body: bytes) -> dict[str, Any]:
    """Decode a webhook body into a JSON object, or raise :class:`PayloadError`.

    The shared body-parse boundary every JSON source uses, so a malformed body is
    a uniform, well-described :class:`PayloadError` (a 400) rather than a per-route
    re-implementation that could drift in its error shape.
    """
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise PayloadError(f"body is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PayloadError("webhook body must be a JSON object")
    return data


class DeliveryDecision(Enum):
    """What a route decided to do with one *verified* inbound delivery.

    The two **quiet** outcomes a route chooses between in ``normalize`` — the pair
    that used to be indistinguishable in the logs (basecradle-router#91): a
    deliberate ignore looked byte-identical to an event class silently falling on
    the floor, so a dead ``task.activated`` capability read as healthy until a
    human poked it. ``WOKE`` means the delivery was classified actionable and
    dispatched to the wake path (the server answers ``202``); ``IGNORED`` means it
    was well-formed but deliberately not actionable (the server answers ``200``).
    Loud outcomes — a bad signature, a malformed payload, an untrusted sender —
    are *rejections* the pipeline already logs at ``WARNING`` with their cause, so
    they are deliberately not part of this quiet pair.
    """

    WOKE = "woke"
    IGNORED = "ignored"


def log_delivery_decision(
    source: str,
    event_type: str | None,
    decision: DeliveryDecision,
    *,
    recipient: str | None = None,
    delivery: str | None = None,
) -> None:
    """Emit the one uniform, structured line recording a delivery's ignore-vs-act fate.

    One format for every route, so an operator (or a metric scraper over the logs)
    can answer *"did this event type wake an agent or get ignored, and for whom?"*
    from observability alone — the signal the silent ``task.activated`` drop lacked
    (basecradle-router#91). It is the **route** that emits it, because only the
    route knows its source's ``event_type`` for a delivery it chose to ignore (the
    core never learns the source's vocabulary); the format is centralized here so
    the contract cannot drift route-to-route. ``recipient`` is logged when the
    route already knows it — the actionable path, where the body is parsed — and
    left ``<unknown>`` for an ignore that short-circuits before parsing, because
    the load-bearing field for spotting a silently-dropped *class* is the
    ``event_type``, not the per-recipient firehose target (deliveries are already
    per-recipient). ``decision`` is the *router's* dispatch choice, not the wake's
    eventual exit code: a ``WOKE`` whose later resolve/wake fails is recorded
    loudly and separately by the pipeline.

    ``delivery`` is the source's per-delivery id, read from a *header* — so unlike
    ``recipient`` the route knows it even for an ignore that never parses the body,
    and it is logged on **every** decision line (basecradle-router#170). It is the
    same key the pipeline's stage lines carry, so ``delivery=<id>`` selects one
    delivery's whole trip — route decision, every stage, and the wake — out of a
    Live Tail of interleaved concurrent deliveries.
    """
    logger.info(
        "event=delivery_decision source=%s event_type=%s decision=%s recipient=%s delivery=%s",
        source,
        event_type or "<none>",
        decision.value,
        recipient or "<unknown>",
        delivery or "<unknown>",
    )


@runtime_checkable
class Route(Protocol):
    """One event source's module. ``name`` is the source key the registry uses.

    ``recipient_kind`` is the :attr:`~basecradle_router.models.Recipient.by` tag
    this route stamps on the events it normalizes — ``"repo"``, ``"recipient_uuid"``,
    or whatever a future source resolves by. It is declared here, on the route, so
    that asking *"can any enabled route reach this agent?"* stays a question the
    core can answer without naming a single source: the wake-edge claims emitter
    (:mod:`basecradle_router.claims`) reads it off the registry rather than
    carrying a hard-coded source→agent-kind map, which would have made adding a
    route touch the emitter. Adding a source is still implementing one route.
    """

    name: str
    recipient_kind: str

    def verify(self, request: InboundRequest, secret: str) -> None:
        """Raise :class:`SignatureError` if the request is unsigned or tampered."""
        ...

    def normalize(self, request: InboundRequest) -> Event | None:
        """Normalize the payload into an :class:`Event`.

        Return ``None`` when the request is well-formed but not actionable (e.g. a
        non-handoff webhook) — that is an ignore, not an error. Raise
        :class:`PayloadError` only when the payload is malformed.
        """
        ...

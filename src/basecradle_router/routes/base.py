"""The route contract: what every event-source module must implement.

A route owns exactly one source's specifics — verify its signature, normalize
its payload into a core :class:`~basecradle_router.models.Event`. The core
pipeline depends only on this protocol, so adding a source is implementing one
route, never forking the daemon.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol, runtime_checkable

from basecradle_router.models import Event

HMAC_SHA256_PREFIX = "sha256="


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


@runtime_checkable
class Route(Protocol):
    """One event source's module. ``name`` is the source key the registry uses."""

    name: str

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

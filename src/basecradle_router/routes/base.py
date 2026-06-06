"""The route contract: what every event-source module must implement.

A route owns exactly one source's specifics — verify its signature, normalize
its payload into a core :class:`~basecradle_router.models.Event`. The core
pipeline depends only on this protocol, so adding a source is implementing one
route, never forking the daemon.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from basecradle_router.models import Event


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

"""The ``basecradle`` route — the first non-GitHub event source.

The BaseCradle platform signs each outbound integration delivery with HMAC-SHA256
over the raw request body, keyed by the recipient agent's ``integration_secret``,
and delivers the digest in ``X-BaseCradle-Signature`` as ``sha256=<hexdigest>``
(mirroring GitHub's contract). When an event occurs on a timeline the agent views,
the platform POSTs to the agent's ``integration_url`` (this route's endpoint).

:meth:`BasecradleRoute.verify` is the security boundary — it shares the same
audited HMAC implementation as the github route, so nothing unsigned or tampered
reaches the core. The platform itself decides what to deliver to whom (deliveries
are per-recipient), so a valid signature *is* the trust: there is no extra
actor allow-list here as there is on github, where any org actor can fire a
webhook.

:meth:`BasecradleRoute.normalize` turns a verified ``message.created`` delivery
into a core :class:`~basecradle_router.models.Event` that resolves to the agent by
its BaseCradle user uuid (``recipient_uuid``) and wakes its harness for the
delivery's ``timeline_uuid``. A non-message delivery is a well-formed *ignore*,
not an error — the same shape as github's non-handoff ignore.
"""

from __future__ import annotations

from typing import Any

from basecradle_router.models import Event, EventKind, Recipient
from basecradle_router.routes.base import (
    InboundRequest,
    PayloadError,
    parse_json_object,
    verify_hmac_sha256,
)

SIGNATURE_HEADER = "X-BaseCradle-Signature"
EVENT_HEADER = "X-BaseCradle-Event"
DELIVERY_HEADER = "X-BaseCradle-Delivery"

# Only a new message on a timeline wakes the agent's harness. The wake is
# timeline-scoped and idempotent (the harness re-processes only *unseen* messages
# and makes no provider call when there are none), so other delivery types — were
# the platform to send them — are clean ignores rather than needless wakes.
MESSAGE_CREATED_EVENT = "message.created"
_ACTIONABLE_EVENTS = frozenset({MESSAGE_CREATED_EVENT})


class BasecradleRoute:
    """The BaseCradle webhook route. ``name`` is the source key the registry uses."""

    name = "basecradle"

    def verify(self, request: InboundRequest, secret: str) -> None:
        """Raise :class:`SignatureError` unless the request carries a valid signature.

        Valid means: a present ``X-BaseCradle-Signature`` header of the form
        ``sha256=<hexdigest>`` whose digest equals the HMAC-SHA256 of the raw
        body under ``secret``. Delegates to the shared
        :func:`~basecradle_router.routes.base.verify_hmac_sha256` boundary so this
        route verifies byte-for-byte identically to github.
        """
        verify_hmac_sha256(request, secret, header=SIGNATURE_HEADER)

    def normalize(self, request: InboundRequest) -> Event | None:
        """Turn a verified delivery into an :class:`Event`, or ignore it.

        Returns ``None`` (a well-formed ignore) for any delivery that is not a
        ``message.created`` event. Raises :class:`PayloadError` when an actionable
        delivery is structurally malformed — missing the delivery id, the
        recipient uuid, or the timeline uuid the wake needs.
        """
        if request.header(EVENT_HEADER) not in _ACTIONABLE_EVENTS:
            return None

        data = parse_json_object(request.body)

        delivery_id = request.header(DELIVERY_HEADER)
        if not delivery_id:
            raise PayloadError(f"missing {DELIVERY_HEADER} header")

        recipient_uuid = _text(data, "recipient_uuid", "recipient_uuid")
        timeline_uuid = _text(data, "timeline_uuid", "timeline_uuid")

        try:
            return Event(
                source=self.name,
                kind=EventKind.PLATFORM_EVENT,
                recipient=Recipient(by="recipient_uuid", value=recipient_uuid),
                wake_arg=timeline_uuid,
                delivery_id=delivery_id,
            )
        except ValueError as exc:
            raise PayloadError(f"malformed basecradle payload: {exc}") from exc


def _text(obj: dict[str, Any], key: str, label: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise PayloadError(f"{label} must be a non-empty string")
    return value

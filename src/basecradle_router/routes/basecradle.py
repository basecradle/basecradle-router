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

:meth:`BasecradleRoute.normalize` turns a verified delivery in the actionable set
(``_ACTIONABLE_EVENTS`` — a new message, a peer's asset, an activated scheduled
task, or an inbound webhook event) into a core
:class:`~basecradle_router.models.Event` that resolves to the agent by its
BaseCradle user uuid (``recipient_uuid``) and wakes its harness for the delivery's
``timeline_uuid``. Every other delivery type is a well-formed *ignore*, not an
error — the same shape as github's non-handoff ignore, and logged as a deliberate
ignore so it is never a silent drop.

The route is deliberately **actor-agnostic**: it wakes timeline-scoped and never
reads ``actor_uuid``, so it cannot itself tell a peer's post from the agent's own.
That is why ``asset.created`` — which the agent self-authors via ``generate_image``
— is safe to wake on *only* once the harness self-filters its own authored items
(``actor_uuid`` == self) on wake; otherwise the agent would wake-loop on its own
output. The self-filter is the harness's invariant, not the router's; this event
must not ship ahead of it (basecradle-router#95, gated on basecradle-harness#95).
"""

from __future__ import annotations

from typing import Any

from basecradle_router.models import Event, EventKind, Recipient
from basecradle_router.routes.base import (
    DeliveryDecision,
    InboundRequest,
    PayloadError,
    log_delivery_decision,
    parse_json_object,
    verify_hmac_sha256,
)

SIGNATURE_HEADER = "X-BaseCradle-Signature"
EVENT_HEADER = "X-BaseCradle-Event"
DELIVERY_HEADER = "X-BaseCradle-Delivery"

# The platform's full event catalog is eleven events (BaseCradle ``docs/api.md`` →
# "Event Catalog"); the router wakes the recipient agent's harness for an explicit
# subset and treats every other delivery as a deliberate, *visible* ignore (the
# decision is logged — see ``normalize`` and basecradle-router#91). A wake is
# timeline-scoped and idempotent (the harness reconciles only *unseen* timeline
# items and makes no provider call when there are none), so an ignore costs
# nothing and a needless wake costs a wasted session — the set stays tight, and an
# event is added only when acting on it is real.
MESSAGE_CREATED_EVENT = "message.created"
ASSET_CREATED_EVENT = "asset.created"
TASK_ACTIVATED_EVENT = "task.activated"
WEBHOOK_EVENT_RECEIVED_EVENT = "webhook_event.received"

# Actionable — the founder's minimum required wake set (basecradle-router#95):
#   message.created        — a new message on a timeline the agent views.
#   asset.created          — a peer posted a file asset to a timeline the agent
#                            views. UNLIKE the others, this is *self-authorable*:
#                            the agent's own ``generate_image`` posts an asset with
#                            ``actor_uuid`` == itself, so waking on it would loop the
#                            agent on its own output — UNLESS the harness self-filters
#                            its own authored items (``actor_uuid`` == self) on wake.
#                            The router stays actor-agnostic by design (it wakes
#                            timeline-scoped and never reads ``actor_uuid``); the
#                            self-filter is the harness's, and this event MUST NOT
#                            ship ahead of it (see the route docstring + #95).
#   task.activated         — a scheduled instruction comes due; the harness
#                            reconciles newly-activated tasks on wake (the wake is
#                            NOT message-only — that earlier assumption was wrong;
#                            the task reconciler shipped in basecradle-harness#95).
#   webhook_event.received — an external service POSTed to the agent's inbound
#                            webhook endpoint; the harness surfaces the unseen
#                            delivery on wake (basecradle-harness#91).
#
# Deliberately NOT actionable (kept a clean, logged ignore):
#   participant.added — also self-authorable and NOT in the founder's required set;
#                       stays deferred (Tier 2) behind the same harness self-filter.
#   No action implied: ``task.created`` (not due yet), ``webhook_endpoint.created``
#   (admin), ``timeline.created`` (reaches only the creator), ``timeline.locked`` /
#   ``timeline.unlocked`` (informational), ``participant.removed`` (nothing to do).
_ACTIONABLE_EVENTS = frozenset(
    {
        MESSAGE_CREATED_EVENT,
        ASSET_CREATED_EVENT,
        TASK_ACTIVATED_EVENT,
        WEBHOOK_EVENT_RECEIVED_EVENT,
    }
)


class BasecradleRoute:
    """The BaseCradle webhook route. ``name`` is the source key the registry uses."""

    name = "basecradle"
    #: A harness persona is addressed by its BaseCradle user uuid, so its events
    #: resolve by ``Recipient(by="recipient_uuid", …)`` — see
    #: :class:`~basecradle_router.routes.base.Route`.
    recipient_kind = "recipient_uuid"

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

        Returns ``None`` (a well-formed ignore) for any delivery that is not in
        the actionable set. Raises :class:`PayloadError` when an actionable
        delivery is structurally malformed — missing the delivery id, the
        recipient uuid, or the timeline uuid the wake needs. Emits a structured
        decision line either way (basecradle-router#91) so an ignore is a
        *visible* deliberate ignore, never a silent drop.
        """
        event_type = request.header(EVENT_HEADER)
        # A *header*, so it is known even on the ignore path that never parses the
        # body — the key that joins this line to the core's stage lines and the
        # wake's own journal (basecradle-router#170).
        delivery_id = request.header(DELIVERY_HEADER)
        if event_type not in _ACTIONABLE_EVENTS:
            log_delivery_decision(
                self.name, event_type, DeliveryDecision.IGNORED, delivery=delivery_id
            )
            return None

        data = parse_json_object(request.body)

        if not delivery_id:
            raise PayloadError(f"missing {DELIVERY_HEADER} header")

        recipient_uuid = _text(data, "recipient_uuid", "recipient_uuid")
        timeline_uuid = _text(data, "timeline_uuid", "timeline_uuid")

        try:
            event = Event(
                source=self.name,
                kind=EventKind.PLATFORM_EVENT,
                recipient=Recipient(by="recipient_uuid", value=recipient_uuid),
                wake_arg=timeline_uuid,
                delivery_id=delivery_id,
            )
        except ValueError as exc:
            raise PayloadError(f"malformed basecradle payload: {exc}") from exc
        log_delivery_decision(
            self.name,
            event_type,
            DeliveryDecision.WOKE,
            recipient=recipient_uuid,
            delivery=delivery_id,
        )
        return event


def _text(obj: dict[str, Any], key: str, label: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise PayloadError(f"{label} must be a non-empty string")
    return value

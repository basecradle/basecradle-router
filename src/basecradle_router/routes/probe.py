"""The ``probe`` route — the router's synthetic wake, injected at its own front door.

Every other route in this package exists because something outside the fleet has an
event to deliver. This one exists because of the opposite problem: **a wake edge that
is silently absent emits no signal at all.** An agent can sit registered, healthy, and
permanently unreachable while every dashboard stays green
(basecradle/basecradle#460, instance 4), and the only honest way to close that is to
*use* the edge and see whether it answers.

**Why the probe lives at this layer.** Shared law now says a monitor never depends on a
consent or trust surface (``constitution.md`` → Operational Baselines): a check runs at
the layer that *owns* the property it proves, and the system is never widened to make a
monitor go green. The retired design probed the wake seam by posting a signed marker
into each agent's own BaseCradle timeline, which required mutual trust between the
monitor and every persona — a prompt-injection channel into the fleet's most privileged
agent, manufactured N times over, to make a health check pass. **The router owns the
router→agent wake edge, so the router proves it** — on-box, over loopback, with no
platform account, no timeline, and no relationship with anyone
(`basecradle-noc#421`, basecradle-router#208).

**It is a real delivery, not a shortcut into the middle of the pipeline.** The injector
(:mod:`basecradle_router.probe`) POSTs a signed body at the daemon's own listener, so a
probe traverses precisely what a genuine delivery traverses: the HTTP front end, the
registry lookup, this route's HMAC ``verify``, ``normalize``, resolve, the per-agent
lock, the delivery-dedup cache, the NOC wake-lock (a probe fired during a converge is
*refused*, correctly), the wake-rate breaker, and the wake itself. A synthetic that
bypassed verification would prove nothing about the edge that matters.

**Two secrets, two independent jobs — and neither is a trust surface.**

- This route's ``BASECRADLE_ROUTER_PROBE_WEBHOOK_SECRET`` (``router.env``, 0600,
  daemon-owned) authorises **injection into the router**, exactly as every other route's
  secret authorises its source. Nothing else on the box can manufacture a wake here.
- The recipient agent's own ``NOC_PROBE_SECRET`` (its ``agent.env``, 0600, agent-owned)
  proves the wake **arrived in that agent's own context** — checked after the privilege
  drop by the wake-runner, never here. The router carries the marker and cannot read it;
  see :mod:`basecradle_router.marker` for why that split is what makes a pass honest.

**Recipients are named by ``harness_key``** — the agent's OS user, its universal identity
(``CLAUDE.md`` → Naming: one slug, everywhere). Deliberately not a repo and not a
platform uuid: those are one source's naming, and a builder has no uuid while a persona
has no repo. Every registered agent has a harness key, so one probe mechanism reaches
every agent — builder and persona alike, @jt included, with no special case
(`basecradle-noc#421`, the founder's "one rule, every agent").

**What a probe must never be able to do is masquerade as real traffic.** It cannot,
structurally: ``synthetic = True`` on this route, the events it normalizes carry
:attr:`~basecradle_router.models.Event.synthetic`, and the evidence store tags every
outcome it records with it. Probe deliveries land in their own ``delivery_sinks.probe``
counters and their own ``agent_wakes.<slug>.by_route.probe`` proof, so no production
sink or per-recipient row is ever touched by one — and the claims emitter leaves this
route out of an agent's *production* wake edges entirely, so a probe can never inflate
``edge_count`` and green the parked builder it exists to expose.

**The breaker still binds, at the agent scope.** A probe's
:attr:`~basecradle_router.models.Event.stream_key` is its marker, whose nonce is fresh
every cycle, so each probe is its own sub-stream and the per-stream cap never fires on
one. That is the correct reading — a probe has no loop to catch — and the per-*agent*
cap is untouched, so a runaway prober is still stopped by the same backstop as anything
else.
"""

from __future__ import annotations

from typing import Any

from basecradle_router.marker import is_marker, nonce_of
from basecradle_router.models import Event, EventKind, Recipient
from basecradle_router.routes.base import (
    DeliveryDecision,
    InboundRequest,
    PayloadError,
    log_delivery_decision,
    parse_json_object,
    verify_hmac_sha256,
)

SIGNATURE_HEADER = "X-BaseCradle-Probe-Signature"
DELIVERY_HEADER = "X-BaseCradle-Probe-Delivery"

#: The one event this route carries. Named (rather than left implicit) so a delivery
#: whose intent is anything else is a *rejection* and not a quiet ignore: unlike a real
#: source, whose catalogue grows underneath us, every byte on this route is manufactured
#: by the fleet — an unrecognised one is a defect in the injector, never traffic.
WAKE_EVENT = "probe.wake"


class ProbeRoute:
    """The synthetic-probe route. ``name`` is the source key the registry uses."""

    name = "probe"
    #: Recipients are named by their OS-user slug — the identity every agent has, and
    #: the same key the per-agent lock, the breaker, and the claim subject use.
    recipient_kind = "harness_key"
    #: Manufactured, by definition. This is the flag that keeps a probe out of the
    #: production edge count and out of the production sinks — see the module docstring.
    synthetic = True

    def verify(self, request: InboundRequest, secret: str) -> None:
        """Raise :class:`SignatureError` unless the request carries a valid signature.

        The same audited HMAC-SHA256 boundary the github and basecradle routes use, over
        the same raw-body contract — because the point of injecting at the front door is
        that the probe is verified the way real traffic is. A probe that could skip this
        would prove nothing about the path a real delivery takes.
        """
        verify_hmac_sha256(request, secret, header=SIGNATURE_HEADER)

    def normalize(self, request: InboundRequest) -> Event | None:
        """Turn a verified probe delivery into a synthetic :class:`Event`.

        **Never returns a quiet ignore.** Every other route has an ignore path because
        its source's event catalogue is wider than the router's interest; this route's
        traffic is manufactured by the fleet for exactly one purpose, so anything that
        does not parse as a wake request is a :class:`PayloadError` — loud, and a 400
        the injector reports. A silently-ignored probe would be a monitor that fails to
        monitor and says nothing, which is the shape this whole instrument exists to
        kill.

        The marker is **shape-checked, never verified**: the router holds no agent's
        probe secret, and only the recipient's own context can answer authenticity (see
        :mod:`basecradle_router.marker`). The check is a security boundary all the same
        — the marker crosses ``sudo`` as an ``argv`` element — so a malformed one is
        refused here rather than carried.
        """
        event_type = request.header("X-BaseCradle-Probe-Event") or WAKE_EVENT
        delivery_id = request.header(DELIVERY_HEADER)
        data = parse_json_object(request.body)

        if event_type != WAKE_EVENT:
            raise PayloadError(
                f"unknown probe event {event_type!r}; this route carries only {WAKE_EVENT!r}"
            )
        if not delivery_id:
            raise PayloadError(f"missing {DELIVERY_HEADER} header")

        harness_key = _text(data, "harness_key")
        marker = _text(data, "marker")
        if not is_marker(marker):
            # The value, not just its shape, is safe to name: a marker is a one-time
            # nonce plus an HMAC over it, so it reveals nothing about the secret — and
            # an operator debugging a rejected probe needs to see what was actually sent.
            raise PayloadError(
                f"marker is not a well-formed BCNOC1 probe marker: {marker!r} "
                "(expected 'BCNOC1 <nonce> <64-hex-hmac>')"
            )

        try:
            event = Event(
                source=self.name,
                kind=EventKind.SYNTHETIC_PROBE,
                recipient=Recipient(by=self.recipient_kind, value=harness_key),
                wake_arg=marker,
                delivery_id=delivery_id,
            )
        except ValueError as exc:
            raise PayloadError(f"malformed probe payload: {exc}") from exc

        log_delivery_decision(
            self.name,
            event_type,
            DeliveryDecision.WOKE,
            recipient=harness_key,
            delivery=delivery_id,
        )
        return event

    @staticmethod
    def nonce(event: Event) -> str | None:
        """The probe nonce carried by ``event``, for logging and reporting."""
        return nonce_of(event.wake_arg)


def _text(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise PayloadError(f"{key} must be a non-empty string")
    return value

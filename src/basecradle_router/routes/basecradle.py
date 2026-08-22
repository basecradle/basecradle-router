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

**The key is chosen per recipient, not per route** (basecradle/basecradle#497).
An ``integration_secret`` belongs to *one persona*, so a single route-wide value
made all seven fleet personas share one signing key: any one of them (or anyone
who ever saw that value) could forge a delivery addressed to any other, and one
leak meant rotating the whole fleet. So the route carries a
:class:`RecipientKeyring` and selects the verification key by the delivery's
``recipient_uuid`` — the standard multi-tenant webhook pattern: **parse to route,
then verify before trusting anything else.** The parse ahead of verification reads
one field out of a still-untrusted body and is used for *nothing* but key
selection; the value is shape-checked to a uuid before it is looked up or logged,
and a body that is unparseable, non-object, or carries no usable ``recipient_uuid``
selects no per-recipient key at all — it falls to the shared secret and is rejected
there exactly as it is today. Nothing skips verification, and no branch of the
selection can widen what a valid signature means.

During the cutover the route-wide secret the core passes in remains the
**fallback**, so the platform rotates personas one at a time; the key path that
verified each delivery is logged (``event=verify_key … key_path=recipient|fallback``)
so the cutover is watchable in Better Stack rather than inferred. Retiring the
fallback is one env flag (:data:`SHARED_FALLBACK_VAR`), and it is *safe* to flip
because :func:`load_recipient_keyring` refuses to boot a daemon whose fallback is
retired while any registered persona still has no key of its own — the exact
"capability silently absent" shape this repo instruments everywhere else.

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

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from basecradle_router.config import ENV_PREFIX, ConfigError, route_secret_var
from basecradle_router.logfmt import log_fields
from basecradle_router.models import Agent, Event, EventKind, Recipient
from basecradle_router.routes.base import (
    DeliveryDecision,
    InboundRequest,
    PayloadError,
    SignatureError,
    log_delivery_decision,
    parse_json_object,
    verify_hmac_sha256,
)

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-BaseCradle-Signature"
EVENT_HEADER = "X-BaseCradle-Event"
DELIVERY_HEADER = "X-BaseCradle-Delivery"

#: The source key this route is registered under — also the stem of every env var
#: that configures it, so the route-wide secret and its per-recipient variants are
#: derived from one string rather than three spellings.
ROUTE_NAME = "basecradle"

#: Env var prefix for a **per-recipient** signing key: the route-wide secret's own
#: variable plus ``_<SLUG>``, where ``<SLUG>`` is the recipient agent's registry key
#: upper-cased with ``-`` → ``_`` (``jt`` → ``BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET_JT``).
#: Keyed by the *slug* and not the uuid on purpose: an operator provisioning @jt's
#: rotated secret should be able to see, in ``router.env``, that it is @jt's — a
#: 36-character uuid in the variable name is unreadable at exactly the moment it
#: matters, and a mistyped one is indistinguishable from a correct one. The registry
#: is the authority that maps the slug back to the uuid the platform signs for.
RECIPIENT_SECRET_PREFIX = f"{route_secret_var(ROUTE_NAME)}_"

#: Env flag retiring the shared route-wide fallback at the end of the cutover.
#: Defaults to **enabled**, which is today's behaviour exactly; setting it false is
#: the single act that ends the shared-secret era. See :func:`load_recipient_keyring`
#: for why flipping it cannot silently strand a persona.
SHARED_FALLBACK_VAR = f"{ENV_PREFIX}{ROUTE_NAME.upper()}_SHARED_SECRET_FALLBACK"

#: The two values of the ``key=`` field on a ``verify_key`` line — the whole point of
#: the log line is that these two are distinguishable, so they are named constants and
#: not literals sprinkled through the branches.
KEY_PATH_RECIPIENT = "recipient"
KEY_PATH_FALLBACK = "fallback"

#: A ``recipient_uuid`` read out of a **not-yet-verified** body is only used if it is
#: uuid-shaped. It reaches a dict lookup (harmless whatever it is) and a journal line
#: (not harmless: an attacker-controlled string with newlines or ANSI escapes would be
#: forging log records), so the shape check is what makes the pre-verification parse
#: safe to act on at all.
_RECIPIENT_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)

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


@dataclass(frozen=True, slots=True)
class RecipientKeyring:
    """Which signing key verifies which recipient's deliveries.

    ``by_recipient`` maps a persona's BaseCradle user uuid to *that persona's own*
    ``integration_secret``. ``shared_fallback`` says whether a recipient with no key
    of its own may still be verified with the route-wide secret the core passes into
    :meth:`BasecradleRoute.verify` — true during the cutover, false once every persona
    has been rotated.

    The keyring holds only the *selection* rule, never the comparison: the digest is
    still computed by the one audited
    :func:`~basecradle_router.routes.base.verify_hmac_sha256`, so per-recipient keys
    change **which** secret is used and nothing about how a signature is checked.

    The default is an empty keyring with the fallback enabled — byte-for-byte today's
    behaviour — so a ``BasecradleRoute()`` built without one (every test that predates
    this, and any future caller that has no keys to give it) verifies exactly as it did
    before rather than silently rejecting everything.
    """

    by_recipient: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    shared_fallback: bool = True

    def select(self, recipient_uuid: str | None, shared_secret: str) -> tuple[str, str]:
        """The ``(secret, key_path)`` to verify a delivery for ``recipient_uuid`` with.

        Per-recipient key first, shared secret second, and — once the fallback is
        retired — a :class:`~basecradle_router.routes.base.SignatureError` rather than
        anything resembling a pass. The rejection is deliberately the *same* class the
        HMAC boundary raises: a delivery the router holds no key for is untrusted for
        the same reason a bad digest is, and the core already records that as a
        ``VERIFY``/``REJECTED`` 401 with its reason in the evidence document. It is
        never a :class:`ConfigError` — that would read as *our* misconfiguration and
        answer the caller differently, when a post-cutover delivery for an unknown
        recipient is simply not ours to trust.

        The message names the key path but never the recipient: at this point the uuid
        is still attacker-supplied, and this string reaches both the journal and the
        evidence document.
        """
        if recipient_uuid is not None:
            secret = self.by_recipient.get(recipient_uuid)
            if secret is not None:
                return secret, KEY_PATH_RECIPIENT
        if self.shared_fallback:
            return shared_secret, KEY_PATH_FALLBACK
        raise SignatureError(
            "no per-recipient signing key for this delivery's recipient, and the "
            f"shared fallback is retired ({SHARED_FALLBACK_VAR} is off)"
        )


def load_recipient_keyring(
    recipients: Mapping[str, Agent], env: Mapping[str, str] | None = None
) -> RecipientKeyring:
    """Build the route's :class:`RecipientKeyring` from ``env`` and the agent registry.

    ``recipients`` is the registry's by-``recipient_uuid`` index
    (:attr:`~basecradle_router.config.Config.recipient_index`) — the authority on which
    uuid is which persona, so a key provisioned as ``…_SECRET_JT`` lands under @jt's
    uuid without an operator ever transcribing one.

    Three things are loud here rather than silent, because each of them is a way the
    cutover fails while looking fine:

    * **A key set for an unknown slug** is a :class:`ConfigError` naming the variable
      and listing the slugs that do exist. A typo'd persona name would otherwise leave
      that persona verifying against the *shared* secret while the platform had already
      rotated it — five failed deliveries auto-disable the integration, and the box
      would have shown nothing wrong.
    * **A key set to an empty value** is a :class:`ConfigError`. An empty secret is a
      real HMAC key, so it would verify a signature an attacker can compute.
    * **A retired fallback with an unprovisioned persona** is a :class:`ConfigError`
      naming every such persona. That combination is a wake edge that can never fire
      again — permanently unreachable, perfectly green — which is precisely the class
      of failure this repo exists to make impossible to hold silently.

    Two registry keys that normalise to the same variable name are also a
    :class:`ConfigError`: the mapping from slug to env var must be a bijection or one
    persona's key silently verifies another's deliveries.
    """
    env = os.environ if env is None else env
    fallback = _bool_env(env, SHARED_FALLBACK_VAR, default=True)

    # var name -> the (uuid, agent) it provisions. The uuid is taken from the index's
    # own key rather than from ``agent.recipient_uuid``: it is the value resolution
    # will actually look this persona up by, so the keyring and the resolver cannot
    # disagree about who a key belongs to.
    var_for_persona: dict[str, tuple[str, Agent]] = {}
    for uuid, agent in recipients.items():
        var = RECIPIENT_SECRET_PREFIX + _slug_suffix(agent.key)
        claimed = var_for_persona.get(var)
        if claimed is not None and claimed[0] != uuid:
            raise ConfigError(
                f"agent keys {claimed[1].key!r} and {agent.key!r} both map to {var}; "
                "per-recipient signing keys must be addressable one persona at a time"
            )
        var_for_persona[var] = (uuid, agent)

    by_recipient: dict[str, str] = {}
    for var, value in env.items():
        if not var.startswith(RECIPIENT_SECRET_PREFIX):
            continue
        persona = var_for_persona.get(var)
        if persona is None:
            known = ", ".join(sorted(var_for_persona)) or "(no harness personas registered)"
            raise ConfigError(f"{var} names no registered agent; expected one of: {known}")
        if not value.strip():
            raise ConfigError(f"{var} is set but empty; unset it to fall back, or give it a key")
        by_recipient[persona[0]] = value

    if not fallback:
        unprovisioned = sorted(
            agent.key for uuid, agent in recipients.items() if uuid not in by_recipient
        )
        if unprovisioned:
            raise ConfigError(
                f"{SHARED_FALLBACK_VAR} is off but no per-recipient signing key is set for "
                f"{', '.join(unprovisioned)}; those personas could never be verified again "
                f"(set {RECIPIENT_SECRET_PREFIX}<SLUG>, or leave the fallback on until they "
                "are rotated)"
            )

    return RecipientKeyring(by_recipient=MappingProxyType(by_recipient), shared_fallback=fallback)


def _slug_suffix(key: str) -> str:
    """An agent's registry key as an env-var suffix — upper-cased, ``-`` → ``_``.

    ``jt`` → ``JT``; ``basecradle-harness`` → ``BASECRADLE_HARNESS``. Only harness
    personas are ever passed here, and their keys are bare universal-identity slugs
    (``[a-z0-9-]``), so the normalisation is total; a key that somehow normalises onto
    another's is caught as a collision by the caller rather than assumed away.
    """
    return key.upper().replace("-", "_")


_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _bool_env(env: Mapping[str, str], var: str, *, default: bool) -> bool:
    """Parse a boolean env var, or raise :class:`ConfigError` naming it.

    Unset or blank is ``default``; anything unrecognised is loud. A security switch
    that quietly read ``BASECRADLE_ROUTER_BASECRADLE_SHARED_SECRET_FALLBACK=flase`` as
    "leave the fallback on" would be a retirement that silently never happened.
    """
    raw = (env.get(var) or "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{var} must be one of {sorted(_TRUE | _FALSE)}, got {env[var]!r}")


class BasecradleRoute:
    """The BaseCradle webhook route. ``name`` is the source key the registry uses."""

    name = ROUTE_NAME
    #: A harness persona is addressed by its BaseCradle user uuid, so its events
    #: resolve by ``Recipient(by="recipient_uuid", …)`` — see
    #: :class:`~basecradle_router.routes.base.Route`.
    recipient_kind = "recipient_uuid"
    #: Real traffic: the platform delivers these because something happened on a
    #: timeline the agent views.
    synthetic = False

    def __init__(self, keyring: RecipientKeyring | None = None) -> None:
        """``keyring`` selects the per-recipient verification key; see :class:`RecipientKeyring`.

        Optional, defaulting to an empty keyring with the shared fallback enabled, so a
        route built with no keys behaves exactly as it did before per-recipient keys
        existed. Production passes the loaded one from the composition root
        (:func:`~basecradle_router.app.build_registry`), which is also the registry the
        claims emitter builds — one construction path, so the emitter can never describe
        a router that does not exist.
        """
        self.keyring = keyring if keyring is not None else RecipientKeyring()

    def boot_summary(self) -> str:
        """What this route booted with, for the line beside the startup banner.

        Two fields, and the second is the load-bearing one: once every persona holds
        its own key, an *armed* shared fallback and a *retired* one produce byte-
        identical traffic — every delivery reads ``key_path=recipient`` either way — so no
        per-delivery line can tell a completed cutover from one where the last step was
        forgotten. The daemon states it instead. See
        :func:`~basecradle_router.routes.base.route_boot_summary`.
        """
        return log_fields(
            recipient_keys=len(self.keyring.by_recipient),
            shared_fallback=self.keyring.shared_fallback,
        )

    def verify(self, request: InboundRequest, secret: str) -> None:
        """Raise :class:`SignatureError` unless the request carries a valid signature.

        Valid means: a present ``X-BaseCradle-Signature`` header of the form
        ``sha256=<hexdigest>`` whose digest equals the HMAC-SHA256 of the raw body
        under **the key this delivery's recipient is signed for** — its own
        ``integration_secret`` when one is provisioned, otherwise the route-wide
        ``secret`` the core passes in, while the shared fallback survives. The
        comparison itself is the shared
        :func:`~basecradle_router.routes.base.verify_hmac_sha256` boundary, so this
        route still verifies byte-for-byte identically to github.

        Which key path was used is stated in one ``event=verify_key`` line per
        *verified* delivery, and named in the rejection reason otherwise — so a
        cutover step that half-landed (the platform rotated, the box did not) reads as
        ``key_path=fallback`` failures on exactly one persona, rather than as a mute
        rise in 401s. The line is emitted only after the signature checks out: before
        that the recipient is a claim, not a fact, and a log line asserting an
        unverified one would be the forgeable field in an otherwise-trustworthy record.
        """
        recipient_uuid = _recipient_hint(request.body)
        key, path = self.keyring.select(recipient_uuid, secret)
        try:
            verify_hmac_sha256(request, key, header=SIGNATURE_HEADER)
        except SignatureError as exc:
            raise SignatureError(f"{exc} (key_path={path})") from None
        logger.info(
            "event=verify_key %s",
            log_fields(
                source=self.name,
                # `key_path`, not `key`: the breaker's trip line already spends `key=`
                # on its scope, and a consumer extracting one must never silently lift
                # the other. The value set here is closed — recipient | fallback.
                key_path=path,
                recipient=recipient_uuid or "<unknown>",
                delivery=request.header(DELIVERY_HEADER) or "<unknown>",
            ),
        )

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


def _recipient_hint(body: bytes) -> str | None:
    """The ``recipient_uuid`` in a **not-yet-verified** body, if it is uuid-shaped.

    The one field read before the signature is checked, and it is read for exactly one
    purpose: choosing which key to check that signature with. Every way the body can
    disappoint — not JSON, not an object, no ``recipient_uuid``, a non-string, a string
    that is not uuid-shaped — answers ``None``, which selects no per-recipient key and
    leaves the delivery to the shared secret and, failing that, to rejection. So a body
    an attacker controls cannot steer key selection anywhere except *away* from a
    persona's own key, and it can never reach the journal in a shape that would forge a
    record there.

    ``normalize`` re-reads the field from the verified body through the strict
    :func:`_text` path; this deliberately does not raise, because a malformed body must
    still be *rejected at the signature*, exactly as it is today, rather than answered
    with a 400 that tells an unauthenticated caller its JSON was bad.
    """
    try:
        data = parse_json_object(body)
    except Exception:  # noqa: BLE001 — see below; the hint is never load-bearing
        # Deliberately broader than :class:`PayloadError`. This is the one parse that
        # runs on **unauthenticated** input, and not every way a hostile body breaks a
        # decoder is a decode *error*: a deeply-nested one raises ``RecursionError``,
        # which is not a ``JSONDecodeError`` and would otherwise escape a security
        # boundary that has not yet checked a signature. Swallowing it costs nothing —
        # every failure here means "no per-recipient key", and the delivery is still
        # put through the same HMAC verify it faces today. No branch of this can widen
        # what a valid signature means; the worst it can do is fall back.
        return None
    value = data.get("recipient_uuid")
    if not isinstance(value, str) or not _RECIPIENT_UUID_RE.match(value):
        return None
    return value


def _text(obj: dict[str, Any], key: str, label: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise PayloadError(f"{label} must be a non-empty string")
    return value

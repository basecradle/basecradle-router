"""The synthetic wake probe — fire a signed test delivery at our own real path.

The router is the fleet's wake edge, and until now that edge could only be *observed*,
never *exercised*. Its claims are ``evidence``-kind: they report the last time a wake
demonstrably happened, and for an agent nobody happens to address, nothing ever does.
Exercising such a claim only re-reads the pointer — it cannot cause a wake — so a
healthy edge and a permanently dead one look identical from outside, and the only
remedies left were social ones. Shared law closes that door: **a monitor never depends
on a consent or trust surface**, and the system is never widened to make a monitor go
green (``constitution.md`` → Operational Baselines). So the lever moves to the layer
that owns the property: **the router proves the router→agent edge itself**
(`basecradle-noc#421`, basecradle-router#208).

**What this module does.** It mints a delivery id, signs a probe body with this box's
own ``probe`` route secret, and POSTs it at **the daemon's own loopback listener** —
then waits for the *daemon's* evidence to move and reports what it saw. Nothing here
short-circuits into the middle of the pipeline: the delivery is answered by the real
HTTP front end, verified by the real HMAC boundary, normalized by the real route,
resolved through the real registry, and dispatched through the real per-agent lock,
dedup cache, freeze interlock, and wake-rate breaker. A synthetic that bypassed
verification would prove nothing about the path a genuine delivery takes.

**The probe's own PASS is not the proof, and it structurally cannot be.** This process
never records anything — the daemon is the evidence document's sole writer, and a probe
that could write its own proof would be grading its own homework. It reports ``proven``
only when the *daemon* recorded a successful wake carrying **the delivery id this run
minted**, which no earlier wake and no self-satisfied exit code can supply. That is why
the reported verdict and the ledger row can never disagree: they are the same fact,
read from the same file (`basecradle-noc#421`, requirement 5).

**Three verdicts, the contract's own three.** Identical to
:mod:`basecradle_router.selftest`, because the ledger reads exactly three things:

- ``proven`` / exit ``0`` — the daemon recorded ``stage=wake outcome=ok`` for this
  delivery. The only thing that counts as evidence.
- ``broken`` / exit ``1`` — we asked and the answer is no: the injection was refused
  structurally (a bad route secret, an unregistered recipient), or the wake was
  dispatched and **failed**. An agent whose probe secret is missing or does not match
  the minted marker lands here, and that is the right reading — the NOC only fires at
  agents it has armed, so a refusal at the far end means the two copies have drifted.
- ``unprovable`` / exit ``75`` — we never got an answer: the probe route is not enabled,
  a gate *refused* the wake (a live converge freeze, the breaker), the injection was
  collapsed as a duplicate, or nothing was recorded before the deadline. Still red, still
  immediate — never quieter.

The refused-vs-failed split the evidence store already draws maps exactly onto
``unprovable``-vs-``broken``, which is not a coincidence: a refusal is the router
working correctly and declining to answer, and that is precisely *we never got an
answer*. A collapsed duplicate reads the same way here for the same reason, and it is
watched on its own counter since basecradle-router#218 moved it out of ``refused`` — the
distinction that matters to the ledger (a dedup implies a wake that *worked*, a refusal a
wake that was stopped) makes no difference to a probe, whose question is only whether
*this* injection was answered.

**The marker is supplied, never minted here.** The router holds no agent's
``NOC_PROBE_SECRET`` and must not — the wake-runner's whole boundary is that an agent's
secrets are read by the agent, after the privilege drop. So the caller (the NOC, which
holds one 0600 file per agent) pipes a minted marker in on **stdin**, and this module
carries it. See :mod:`basecradle_router.marker` for why that split is what makes a pass
mean something.
"""

from __future__ import annotations

import hmac
import http.client
import json
import logging
import time
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256

from basecradle_router.evidence import (
    AgentWakeEvidence,
    EvidenceDocument,
    RouteWakeEvidence,
    read_evidence,
)
from basecradle_router.marker import is_marker, nonce_of
from basecradle_router.routes.base import HMAC_SHA256_PREFIX
from basecradle_router.routes.probe import (
    DELIVERY_HEADER,
    SIGNATURE_HEADER,
    WAKE_EVENT,
    ProbeRoute,
)
from basecradle_router.selftest import EXIT_UNPROVABLE

logger = logging.getLogger("basecradle_router.probe")

#: The verdicts, mirroring :mod:`basecradle_router.selftest`'s three-way contract.
PROVEN = "proven"
BROKEN = "broken"
UNPROVABLE = "unprovable"

#: Exit codes, pinned by the NOC's probe contract (basecradle-noc#408, ruling 4).
EXIT_CODES = {PROVEN: 0, BROKEN: 1, UNPROVABLE: EXIT_UNPROVABLE}

#: The header naming which probe event a delivery carries — see
#: :data:`~basecradle_router.routes.probe.WAKE_EVENT`.
EVENT_HEADER = "X-BaseCradle-Probe-Event"

#: How long to wait for the daemon to record an outcome. Generous rather than tight: the
#: wake scheduler runs **one wake in flight per agent**, so a probe fired while that
#: agent is mid-session queues behind a session that can legitimately run for minutes. A
#: deadline that expired under normal load would report ``unprovable`` about a perfectly
#: healthy edge, which is the crying-wolf failure this program is not allowed to add.
DEFAULT_TIMEOUT = 300.0

#: How often to re-read the evidence document while waiting. The read is a small JSON
#: parse of a world-readable file the daemon replaces atomically, so a tight-ish interval
#: costs nothing and keeps the measured fire-to-proof window meaningful.
DEFAULT_POLL_INTERVAL = 0.5

#: How the *far side* of the sudo boundary says "we never got an answer".
#:
#: A refused probe and a broken one both come back to the router as a non-zero exit and
#: are both recorded as a wake **failure** — the pipeline is source-agnostic and must not
#: learn probe semantics to tell them apart. But the difference is exactly the contract's
#: own question: an agent nobody has armed yet, or a verifier not installed on this box,
#: is *unprovable*; a marker that does not verify is *broken*. Reporting the first as the
#: second would page for a configuration step.
#:
#: So the far side prefixes its reason with this token, and the router reads it back off
#: the recorded reason. Both sides of the token are ours — the root-owned wrapper and the
#: root-owned verifier are the only things that write it on the probe path — so this is a
#: contract between two files we ship, not a guess about someone else's output.
UNPROVABLE_MARKER = "could not prove:"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class Injection:
    """What the daemon's HTTP front end said about the delivery we posted.

    ``status`` is the HTTP status; ``stages`` is the accept half's own record, which the
    server returns as its body. Both are kept because a non-202 is the one case where
    the probe knows the answer without waiting: a ``401`` is this box's probe secret
    disagreeing with the one we signed with, a ``404`` is the route not enabled, and a
    ``200`` means the accept half declined to schedule a wake at all (an unregistered
    recipient, most often) — and the stage list names which.
    """

    status: int
    stages: tuple[tuple[str, str], ...] = ()
    body: str = ""

    @property
    def accepted(self) -> bool:
        """Whether a wake was actually scheduled (the server's fast-ack ``202``)."""
        return self.status == 202

    def terminal(self) -> str:
        """``"<stage>/<outcome>"`` for the last accept stage, or ``"none"``."""
        return f"{self.stages[-1][0]}/{self.stages[-1][1]}" if self.stages else "none"

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "accepted": self.accepted,
            "stages": [list(pair) for pair in self.stages],
            "terminal": self.terminal(),
        }


@dataclass(frozen=True, slots=True)
class WakeProof:
    """One reading of the daemon's evidence for this (agent, route) — before, and after.

    Deliberately a snapshot of both granularities. ``agent_last_ok_at`` is the field the
    agent-wide wake-edge claim points at — *the* number the whole exercise exists to
    move, and the one the NOC's census carries — while the per-route fields are what the
    synthetic claim points at and what tells this probe's outcome apart from a real
    wake's that happened to land in the same window.
    """

    agent_last_ok_at: str | None = None
    agent_last_ok_route: str | None = None
    agent_last_ok_synthetic: bool | None = None
    route_ok: int = 0
    route_last_ok_at: str | None = None
    route_last_ok_delivery: str | None = None
    route_failed: int = 0
    route_last_failed_at: str | None = None
    route_last_failed_reason: str | None = None
    route_refused: int = 0
    route_last_refused_at: str | None = None
    route_last_refused_reason: str | None = None
    route_deduped: int = 0
    queued: int = 0

    @classmethod
    def read(cls, document: EvidenceDocument, harness_key: str, route: str) -> WakeProof:
        wake = document.agent_wakes.get(harness_key) or AgentWakeEvidence()
        per_route = wake.by_route.get(route) or RouteWakeEvidence()
        return cls(
            agent_last_ok_at=wake.last_ok_at,
            agent_last_ok_route=wake.last_ok_route,
            agent_last_ok_synthetic=wake.last_ok_synthetic,
            route_ok=per_route.ok,
            route_last_ok_at=per_route.last_ok_at,
            route_last_ok_delivery=per_route.last_ok_delivery,
            route_failed=per_route.failed,
            route_last_failed_at=per_route.last_failed_at,
            route_last_failed_reason=per_route.last_failed_reason,
            route_refused=per_route.refused,
            route_last_refused_at=per_route.last_refused_at,
            route_last_refused_reason=per_route.last_refused_reason,
            route_deduped=per_route.deduped,
            queued=wake.queued,
        )

    def to_json(self) -> dict:
        return {
            "agent_last_ok_at": self.agent_last_ok_at,
            "agent_last_ok_route": self.agent_last_ok_route,
            "agent_last_ok_synthetic": self.agent_last_ok_synthetic,
            "route_ok": self.route_ok,
            "route_last_ok_at": self.route_last_ok_at,
            "route_last_ok_delivery": self.route_last_ok_delivery,
            "route_failed": self.route_failed,
            "route_last_failed_at": self.route_last_failed_at,
            "route_last_failed_reason": self.route_last_failed_reason,
            "route_refused": self.route_refused,
            "route_last_refused_at": self.route_last_refused_at,
            "route_last_refused_reason": self.route_last_refused_reason,
            "route_deduped": self.route_deduped,
            "queued": self.queued,
        }


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The whole run: what we fired, what the daemon did, and the before/after proof."""

    status: str
    harness_key: str
    route: str
    delivery_id: str
    nonce: str | None
    detail: str
    checked_at: str
    waited: float
    injection: Injection
    before: WakeProof
    after: WakeProof

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    @property
    def proven(self) -> bool:
        return self.status == PROVEN

    def summary(self) -> str:
        """One line an operator (and the NOC's forwarded stderr) can read on its own."""
        moved = (
            f"{self.before.agent_last_ok_at} -> {self.after.agent_last_ok_at}"
            if self.proven
            else str(self.before.agent_last_ok_at)
        )
        return f"{self.detail} [agent last_ok_at {moved}]"

    def to_json(self) -> dict:
        return {
            "probe": "synthetic-wake",
            "status": self.status,
            "harness_key": self.harness_key,
            "route": self.route,
            "delivery_id": self.delivery_id,
            "nonce": self.nonce,
            "detail": self.detail,
            "checked_at": self.checked_at,
            "waited_seconds": round(self.waited, 3),
            "injection": self.injection.to_json(),
            "before": self.before.to_json(),
            "after": self.after.to_json(),
        }


class ProbeError(Exception):
    """The probe could not be *attempted* — bad input, or the daemon was unreachable."""


#: The two injectable seams. ``Poster`` performs the HTTP round trip (replaced in tests
#: and by anything that wants to drive the daemon differently); ``Reader`` re-reads the
#: evidence document. Both exist so the whole probe is drivable offline without a socket
#: or a state directory — the same discipline as the wake boundary's ``Runner``.
Poster = Callable[[str, bytes, dict], Injection]
Reader = Callable[[], EvidenceDocument]


@dataclass(frozen=True, slots=True)
class WakeProbe:
    """Fires one synthetic wake and reports what the daemon's own evidence then said.

    Every collaborator is injected, so a test drives the entire probe with no socket and
    no state directory: ``post`` performs the HTTP round trip, ``read`` re-reads the
    evidence, and ``sleep``/``clock``/``now`` make the wait deterministic. Production
    builds it from the daemon's own config (:func:`build_probe`), so the probe and the
    daemon can never disagree about the secret, the route, or where the evidence lives.
    """

    secret: str
    evidence_path: str | None
    self_url: str
    route: str = ProbeRoute.name
    timeout: float = DEFAULT_TIMEOUT
    poll_interval: float = DEFAULT_POLL_INTERVAL
    post: Poster | None = None
    read: Reader | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = _utc_now
    #: A bare random id, carrying **no type prefix** (basecradle-router#222, a founder
    #: order). It used to be minted ``probe-<hex>`` so a human reading journald could see
    #: the delivery was manufactured — but a delivery id identifies one delivery and must
    #: never also *type* it: the prefix put a low-cardinality fact inside a
    #: high-cardinality join key, where label extraction cannot see it, and every wake
    #: chart mixed the fleet's own probe traffic with its real work while the raw line
    #: looked perfectly informative. That question is answered by ``source=probe`` on the
    #: line and by ``route`` in the evidence, so the id goes back to identifying. The hex
    #: stays inside the inert charset the wake boundary and the wake-runner both pin.
    mint_delivery_id: Callable[[], str] = field(default=lambda: uuid.uuid4().hex)

    def run(self, harness_key: str, marker: str) -> ProbeResult:
        """Fire one probe at ``harness_key`` carrying ``marker``; never raises on a verdict.

        Raises :class:`ProbeError` only when the probe could not be *attempted* at all —
        a malformed marker, or a daemon that would not answer its own socket. Those are
        not verdicts about the wake edge and must not be reported as one; the CLI turns
        them into ``unprovable`` with the cause on stderr.
        """
        if not is_marker(marker):
            raise ProbeError(
                "marker is not a well-formed BCNOC1 probe marker "
                "(expected 'BCNOC1 <nonce> <64-hex-hmac>'); mint it with the NOC's "
                "per-agent probe secret and pipe it in on stdin"
            )
        delivery_id = self.mint_delivery_id()
        before = self._proof(harness_key)
        started = self.clock()
        injection = self._inject(harness_key, marker, delivery_id)
        logger.info(
            "event=probe_injected agent=%s route=%s delivery=%s status=%d terminal=%s",
            harness_key,
            self.route,
            delivery_id,
            injection.status,
            injection.terminal(),
        )
        status, detail, after = self._await_outcome(harness_key, delivery_id, before, injection)
        return ProbeResult(
            status=status,
            harness_key=harness_key,
            route=self.route,
            delivery_id=delivery_id,
            nonce=nonce_of(marker),
            detail=detail,
            checked_at=self.now().astimezone(timezone.utc).isoformat(),
            waited=self.clock() - started,
            injection=injection,
            before=before,
            after=after,
        )

    # --- firing ------------------------------------------------------------

    def body_for(self, harness_key: str, marker: str) -> bytes:
        """The exact bytes posted — and therefore the exact bytes signed.

        Serialized with fixed separators and sorted keys so the body is a function of
        its content alone. The signature covers these bytes, so anything that reformats
        them (a pretty-printer, a different key order) is a rejected delivery rather
        than a subtly different one.
        """
        return json.dumps(
            {"harness_key": harness_key, "marker": marker},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def headers_for(self, body: bytes, delivery_id: str) -> dict[str, str]:
        """The delivery's headers, including the HMAC the route will verify.

        The same ``sha256=<hexdigest>`` contract every route in this package shares —
        the probe is signed the way real traffic is signed, because injecting at the
        front door is the whole point.
        """
        digest = hmac.new(self.secret.encode("utf-8"), body, sha256).hexdigest()
        return {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            SIGNATURE_HEADER: f"{HMAC_SHA256_PREFIX}{digest}",
            DELIVERY_HEADER: delivery_id,
            EVENT_HEADER: WAKE_EVENT,
        }

    def _inject(self, harness_key: str, marker: str, delivery_id: str) -> Injection:
        body = self.body_for(harness_key, marker)
        headers = self.headers_for(body, delivery_id)
        path = f"/webhooks/{self.route}"
        post = self.post or _post_over_http
        return post(f"{self.self_url}{path}", body, headers)

    # --- waiting -----------------------------------------------------------

    def _proof(self, harness_key: str) -> WakeProof:
        read = self.read or (lambda: read_evidence(self.evidence_path))
        return WakeProof.read(read(), harness_key, self.route)

    def _await_outcome(
        self,
        harness_key: str,
        delivery_id: str,
        before: WakeProof,
        injection: Injection,
    ) -> tuple[str, str, WakeProof]:
        """Wait for the daemon to record *this* delivery's outcome, and read the verdict.

        A non-``202`` injection is decided at once: the accept half already refused, so
        there is no wake to wait for and waiting would only delay a known answer.

        Otherwise the loop watches for exactly four things, in the order that keeps them
        unambiguous. **Success is matched on the delivery id, never on the timestamp**,
        because a real wake for the same agent can land in the same window and a probe
        that accepted somebody else's proof would be worse than no probe at all.

        The three non-success outcomes record no delivery id, so they are matched on the
        **counter** moving rather than the timestamp — a counter is monotonic and cannot
        repeat a value, whereas two outcomes recorded in the same instant would produce
        the same ISO timestamp and read as "nothing happened". That the counter belongs to
        this probe rather than to some other traffic is guaranteed one level up: the
        per-agent lock serialises this agent's wakes, and the counters are scoped to the
        *probe* route, so nothing else can be moving them.
        """
        if not injection.accepted:
            return (*_injection_verdict(injection), before)

        deadline = self.clock() + self.timeout
        after = before
        while True:
            after = self._proof(harness_key)
            if after.route_last_ok_delivery == delivery_id:
                return (
                    PROVEN,
                    f"the wake edge answered: the daemon recorded stage=wake outcome=ok for "
                    f"delivery {delivery_id} at {after.route_last_ok_at}",
                    after,
                )
            if after.route_failed != before.route_failed:
                reason = after.route_last_failed_reason or ""
                if UNPROVABLE_MARKER in reason:
                    # The far side refused rather than answered — see UNPROVABLE_MARKER.
                    return (UNPROVABLE, f"the probe could not be completed: {reason}", after)
                return (BROKEN, f"the wake was dispatched and failed: {reason}", after)
            if after.route_refused != before.route_refused:
                return (
                    UNPROVABLE,
                    f"the router refused the wake, so nothing was proven either way: "
                    f"{after.route_last_refused_reason}",
                    after,
                )
            if after.route_deduped != before.route_deduped:
                # Watched on its own counter since basecradle-router#218 split it out of
                # `refused`. Unreachable in practice — every run mints a fresh delivery
                # id, so the dedup cache cannot hold it — but the verdict is the same
                # class as any other gate that stops the wake: no wake ran, so nothing
                # was proven. Dropping the check would silently downgrade that to a
                # timeout, which reports the same colour with a far worse reason.
                return (
                    UNPROVABLE,
                    "the router collapsed the injection as a duplicate delivery, so no "
                    "wake ran and nothing was proven either way",
                    after,
                )
            if self.clock() >= deadline:
                queued = f", {after.queued} wake(s) queued for this agent" if after.queued else ""
                return (
                    UNPROVABLE,
                    f"no outcome recorded for delivery {delivery_id} within "
                    f"{self.timeout:.0f}s{queued}; the wake may still be in flight — "
                    f"re-read the evidence at {self.evidence_path or '(in-memory)'} before "
                    "treating this as a finding",
                    after,
                )
            self.sleep(self.poll_interval)


def _injection_verdict(injection: Injection) -> tuple[str, str]:
    """Read the verdict straight off a non-``202`` accept — no wake was ever scheduled.

    The split follows the contract's own question, *did we get an answer?*: a signature
    the daemon rejected, a malformed body, and a recipient the registry does not know are
    all definite negatives about this seam, whereas a route the box does not serve means
    the probe is not armed here at all — nothing was asked, so nothing can be concluded.
    """
    if injection.status == 404:
        return (
            UNPROVABLE,
            "the daemon does not serve this route: add it to "
            "BASECRADLE_ROUTER_ENABLED_ROUTES (with its webhook secret) and restart",
        )
    if injection.status == 401:
        return (
            BROKEN,
            "the daemon rejected the probe's signature — the secret this run signed with "
            "is not the one the daemon holds for this route",
        )
    return (
        BROKEN,
        f"the daemon accepted no wake for this delivery (HTTP {injection.status}, "
        f"terminal {injection.terminal()}) — most often an unregistered recipient",
    )


def _post_over_http(url: str, body: bytes, headers: dict) -> Injection:
    """POST ``body`` to ``url`` with the stdlib, and read the daemon's fast-ack.

    ``http.client`` rather than a third-party client on purpose: this box holds the
    fleet's crown jewels, and the daemon's whole dependency footprint is ``uvicorn``. A
    loopback POST is not worth widening it.
    """
    parts = urllib.parse.urlsplit(url)
    connection_cls = (
        http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    )
    try:
        connection = connection_cls(parts.hostname or "127.0.0.1", parts.port, timeout=30)
        try:
            connection.request("POST", parts.path or "/", body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read().decode("utf-8", "replace")
            return Injection(status=response.status, stages=_stages(raw), body=raw)
        finally:
            connection.close()
    except OSError as exc:
        raise ProbeError(
            f"could not reach the router daemon at {url}: {exc} — is basecradle-router "
            "running, and is BASECRADLE_ROUTER_SELF_URL the address it binds?"
        ) from exc


def _stages(raw: str) -> tuple[tuple[str, str], ...]:
    """The accept half's ``[[stage, outcome], …]`` from the response body, best-effort.

    A body this cannot parse yields no stages rather than raising: the status code is the
    load-bearing signal, and a probe that crashed on an unexpected body would turn a
    diagnosable answer into no answer at all.
    """
    try:
        parsed = json.loads(raw)
        stages = parsed["stages"]
    except (ValueError, KeyError, TypeError):
        return ()
    return tuple(
        (str(pair[0]), str(pair[1]))
        for pair in stages
        if isinstance(pair, (list, tuple)) and len(pair) == 2
    )


__all__ = [
    "BROKEN",
    "DEFAULT_TIMEOUT",
    "EXIT_CODES",
    "PROVEN",
    "UNPROVABLE",
    "Injection",
    "ProbeError",
    "ProbeResult",
    "WakeProbe",
    "WakeProof",
]

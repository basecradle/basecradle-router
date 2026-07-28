"""The evidence store — what the router has actually *proven*, on disk.

The router's structured log already says what happened (``stage=wake outcome=ok``,
``stage=verify outcome=rejected``). A log is a poor ledger source, though: it
rotates, it is unindexed, and reading it means grepping journald from another
process. So the daemon also keeps a small, durable **evidence store** — the
answer to the only question the claims-vs-evidence ledger asks of us: *when did
this capability last demonstrably work, and how many times has it failed?*

**Why this exists (basecradle/basecradle#460 — the green-while-absent program).**
Fleet observability catches failures that *happen*. The night of 2026-07-26→27
produced five failures where nothing happened at all — a capability was silently
**absent**, and absence emits no signal. Two of those classes are ours:

- **A parked builder with no wake edge** (instance 4). An agent can be perfectly
  healthy, registered, and idle *forever* because nothing in existence will ever
  wake it again. Only a record of the last *successful* wake — or its absence —
  distinguishes "quiet" from "unreachable".
- **An integration armed on paper whose every delivery was rejected** (instance
  5). A route with a mismatched secret rejects deliveries at ``verify`` and looks
  exactly like a route nobody has sent to. Accept/reject counters separate the
  two: ``accepted=0 rejected=417`` is a broken secret; ``accepted=0 rejected=0``
  is a sink nothing has ever tried.

Both fall out of the same primitive: **last demonstrable success + counters**,
per subject, readable by a different process (the NOC's converge runs the claims
emitter; the daemon writes this file).

**And instance 5 is asked per *recipient*, not per route.** A route-wide accept
counter says the sink works for *somebody*; the ledger's rows ask whether it works
for *this agent*. Neither the per-route counters nor the per-agent scalars can
answer that alone — one healthy recipient would green six dead ones, and a wake
delivered by one route would green another route that 401s every delivery to the
same agent. So the wake proof is also kept at **(agent, route)** granularity
(:class:`RouteWakeEvidence`), which is the granularity the question is asked at
(basecradle-noc#408).

**Design constraints, and how each is met.**

- *A different process must read it.* One JSON document at
  :data:`DEFAULT_EVIDENCE_FILE`, written by ``write-temp + os.replace`` so a
  reader either sees the whole previous document or the whole new one — never a
  torn one. No lockfile protocol is needed on the read side.
- *It must survive a restart.* ``/var/lib``, not ``/run``: the whole point is an
  age-of-proof that spans reboots. The daemon reloads the document at startup and
  keeps it in memory, so a restart never resets a counter to zero and never makes
  a proven capability read as never-proven.
- *It must never break a wake.* Every write is best-effort: an unwritable state
  dir (a laptop, a test, a botched deploy) degrades to in-memory only, with **one**
  warning per process rather than a line per delivery. Recording evidence is an
  observability nicety, exactly like :func:`~basecradle_router.server.deployed_sha`,
  and the fail-direction for a nicety is to go quiet, never to take the daemon
  down. An instrument that can wedge the thing it instruments is a worse bug than
  the blind spot it closes.
- *It is written from the wake threads.* One :class:`threading.Lock` guards the
  in-memory document and the replace, held only for the microseconds of an update
  — never across a wake.

The store holds **no secrets and no payloads** — slugs, route names, counters,
timestamps, and a truncated reason string. It is deliberately world-readable so
the NOC's converge (running as root or as its own user) can read it without a
privilege grant, and so an operator can ``cat`` it during an incident.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("basecradle_router.evidence")

#: The on-box evidence document. ``/var/lib`` (not ``/run``) because age-of-proof
#: must span reboots — a wake proven yesterday is still proven after a restart.
#: The daemon's unit declares ``StateDirectory=basecradle-router``, so systemd
#: creates the parent owned by the service user before the daemon starts.
DEFAULT_EVIDENCE_FILE = "/var/lib/basecradle-router/evidence.json"

#: Bump only on an incompatible reshape of the document; a reader that sees a
#: version it does not know should treat the file as absent rather than guess.
EVIDENCE_VERSION = 1

#: A recorded failure reason is a log detail, not a payload — cap it so a pathological
#: exception string can never grow the document without bound.
_MAX_REASON = 200


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _reason(text: object) -> str:
    return str(text)[:_MAX_REASON]


@dataclass
class DeliverySinkEvidence:
    """What one route (delivery sink) has demonstrably accepted and rejected.

    ``accepted`` counts deliveries whose **signature verified** — deliberately not
    "produced a wake". Verification passing is the proof that the integration is
    genuinely armed: the secret on the box matches the secret at the source. A
    verified delivery the route then ignores (a non-handoff event) still proves the
    sink works, which is exactly the distinction instance 5 lacked. ``woke`` and
    ``ignored`` split that accepted total by what the route decided.
    """

    accepted: int = 0
    rejected: int = 0
    woke: int = 0
    ignored: int = 0
    last_accepted_at: str | None = None
    last_rejected_at: str | None = None
    last_reject_reason: str | None = None


@dataclass
class RouteWakeEvidence:
    """One **(agent, route)** pair's wake proof — the per-recipient arming gate.

    The pair exists because neither of the two projections above it is sound, and
    the NOC's ledger refused to arm its per-recipient delivery-sink rows on either
    (basecradle-noc#408). Per-**route** alone (:class:`DeliverySinkEvidence`), one
    healthy recipient greens every dead one on that route. Per-**agent** alone
    (:class:`AgentWakeEvidence`'s scalars), a github-route wake greens a basecradle
    integration that 401s every delivery to the same agent — instance 5 surviving
    the very instrument built to catch it.

    So the proof is recorded at the granularity the question is asked at: *has
    **this** route ever demonstrably woken **this** agent, and when?* A route whose
    entry is absent has never woken the agent, which is what a never-proven edge
    means — kept as an absence rather than a zero row so "never tried" and "tried
    and it worked" can never be confused.
    """

    ok: int = 0
    last_ok_at: str | None = None
    last_ok_delivery: str | None = None


@dataclass
class AgentWakeEvidence:
    """What one agent's wake edge has demonstrably done — the re-wake proof.

    ``last_ok_at``/``last_ok_delivery``/``last_ok_route`` are the ledger's evidence
    pointer: the timestamp, delivery id, and event source of the last ``stage=wake
    outcome=ok``, joinable straight back to both halves of that wake in journald.
    ``None`` means the router has **never** successfully woken this agent —
    never-proven, the state that makes a parked builder visible.

    ``refused`` counts wakes the router deliberately declined (dedup, a held NOC
    wake-lock, a tripped breaker). It is kept apart from ``failed`` because the two
    mean opposite things: a refusal is the router working correctly, a failure is
    the wake path broken. An agent whose only recent activity is refusals is
    *gated*, not dead — and a ledger that conflated them would cry wolf on every
    converge.

    ``by_route`` is the same proof at **(agent, route)** granularity — see
    :class:`RouteWakeEvidence` for why the scalars above it are not enough on their
    own. The three ``last_ok_*`` scalars describe one event (the most recent
    successful wake, whatever route delivered it) and are written together in one
    update, so they cannot drift apart from each other or from ``by_route``.

    ``queued`` is the scheduler's pending-wake depth for this agent as of the last
    change — the transient wake edge. Non-zero means a wake is queued or in flight
    right now, so the agent will be woken again regardless of anything else.
    """

    ok: int = 0
    failed: int = 0
    refused: int = 0
    last_ok_at: str | None = None
    last_ok_delivery: str | None = None
    last_ok_route: str | None = None
    last_failed_at: str | None = None
    last_failed_reason: str | None = None
    last_refused_at: str | None = None
    last_refused_reason: str | None = None
    queued: int = 0
    by_route: dict[str, RouteWakeEvidence] = field(default_factory=dict)


@dataclass
class SelfTestEvidence:
    """The last freeze-readability self-test result — boot's, or the NOC probe's.

    Recorded by both the boot check and the CLI probe so the ledger can read an
    age-of-proof for the freeze surface without running anything: ``status`` plus
    ``at`` is a complete ledger row on its own.
    """

    status: str | None = None
    at: str | None = None
    detail: str | None = None


@dataclass
class EvidenceDocument:
    """The whole on-disk document: sinks by route, wakes by agent, last self-test."""

    version: int = EVIDENCE_VERSION
    updated_at: str | None = None
    delivery_sinks: dict[str, DeliverySinkEvidence] = field(default_factory=dict)
    agent_wakes: dict[str, AgentWakeEvidence] = field(default_factory=dict)
    freeze_selftest: SelfTestEvidence = field(default_factory=SelfTestEvidence)

    def to_json(self) -> dict:
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "delivery_sinks": {k: asdict(v) for k, v in sorted(self.delivery_sinks.items())},
            "agent_wakes": {k: _wake_json(v) for k, v in sorted(self.agent_wakes.items())},
            "freeze_selftest": asdict(self.freeze_selftest),
        }

    @classmethod
    def from_json(cls, raw: object) -> EvidenceDocument:
        """Rebuild a document from parsed JSON, ignoring anything unrecognised.

        Tolerant by design: a document written by an older (or newer, or partly
        hand-edited) build must never stop the daemon booting, so an unusable
        field falls back to its default rather than raising. The worst case is an
        under-counted ledger row, which the next real event corrects.
        """
        if not isinstance(raw, dict) or raw.get("version") != EVIDENCE_VERSION:
            return cls()
        doc = cls(updated_at=_str_or_none(raw.get("updated_at")))
        for name, fields in _items(raw.get("delivery_sinks")):
            doc.delivery_sinks[name] = _rebuild(DeliverySinkEvidence, fields)
        for name, fields in _items(raw.get("agent_wakes")):
            doc.agent_wakes[name] = _rebuild_agent_wake(fields)
        doc.freeze_selftest = _rebuild(SelfTestEvidence, raw.get("freeze_selftest"))
        return doc


def _wake_json(wake: AgentWakeEvidence) -> dict:
    """One agent's wake evidence as JSON, with ``by_route`` in a stable order.

    Sorted for the same reason the two outer maps are: the document is diffed by the
    ledger and read by a human mid-incident, and a map that reorders between writes
    shows churn where there was no change.
    """
    fields = asdict(wake)
    fields["by_route"] = {r: asdict(e) for r, e in sorted(wake.by_route.items())}
    return fields


def _rebuild_agent_wake(fields: dict) -> AgentWakeEvidence:
    """Rebuild one agent's wake evidence, including its nested per-route map.

    :func:`_rebuild` alone would leave ``by_route`` holding the raw dicts it read
    off disk, which every reader downstream would then have to defend against.
    Rebuilding it here keeps the in-memory document uniformly typed whatever the
    file said — the same tolerance the rest of :meth:`EvidenceDocument.from_json`
    applies, one level down.
    """
    wake = _rebuild(AgentWakeEvidence, fields)
    wake.by_route = {
        route: _rebuild(RouteWakeEvidence, entry) for route, entry in _items(fields.get("by_route"))
    }
    return wake


def _items(raw: object):
    if not isinstance(raw, dict):
        return []
    return [(k, v) for k, v in raw.items() if isinstance(k, str) and isinstance(v, dict)]


def _rebuild(kind, fields: object):
    """Build a dataclass from ``fields``, keeping only keys it declares."""
    if not isinstance(fields, dict):
        return kind()
    known = {f for f in kind.__dataclass_fields__}
    return kind(**{k: v for k, v in fields.items() if k in known})


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


class EvidenceStore:
    """The daemon's write side and the emitter's read side of the evidence document.

    Construct once at startup with the document's path; the daemon calls the
    ``record_*`` methods from its wake threads. Out-of-process readers (the claims
    emitter, the CLI) use :func:`read_evidence` instead — they must not create or
    write the file.

    ``path=None`` makes the store purely in-memory, which is what the offline tests
    and any laptop run use: every ``record_*`` still updates the document, nothing
    ever touches the filesystem.
    """

    def __init__(
        self, path: str | None = DEFAULT_EVIDENCE_FILE, *, now: Callable[[], datetime] = _utc_now
    ) -> None:
        self.path = path
        self._now = now
        self._lock = threading.Lock()
        self._doc = _load(path) if path else EvidenceDocument()
        # One warning per process for an unwritable store, not one per delivery: a
        # broken state dir must be visible exactly once, never a firehose that buries
        # the wake lines an operator is actually reading.
        self._write_failed = False

    def snapshot(self) -> EvidenceDocument:
        """A detached copy of the current document, for an in-process reader.

        Deep-copied so a caller cannot mutate the live document behind the lock, and
        taken under the lock so it can never catch a half-applied update.
        """
        with self._lock:
            return deepcopy(self._doc)

    # --- delivery-sink evidence (instance 5: armed on paper, never accepted) ---

    def record_delivery_accepted(self, route: str) -> None:
        """A delivery for ``route`` passed signature verification — the sink works."""
        with self._lock:
            sink = self._doc.delivery_sinks.setdefault(route, DeliverySinkEvidence())
            sink.accepted += 1
            sink.last_accepted_at = _iso(self._now())
            self._flush_locked()

    def record_delivery_rejected(self, route: str, reason: str) -> None:
        """A delivery for ``route`` was rejected (bad signature, malformed payload)."""
        with self._lock:
            sink = self._doc.delivery_sinks.setdefault(route, DeliverySinkEvidence())
            sink.rejected += 1
            sink.last_rejected_at = _iso(self._now())
            sink.last_reject_reason = _reason(reason)
            self._flush_locked()

    def record_delivery_decision(self, route: str, *, woke: bool) -> None:
        """Split an already-accepted delivery by what the route decided to do with it."""
        with self._lock:
            sink = self._doc.delivery_sinks.setdefault(route, DeliverySinkEvidence())
            if woke:
                sink.woke += 1
            else:
                sink.ignored += 1
            self._flush_locked()

    # --- agent wake evidence (instance 4: parked with nothing to re-wake it) ---

    def record_wake_ok(self, agent: str, delivery: str, *, route: str) -> None:
        """The proof that matters: this agent's wake edge fired and succeeded.

        ``route`` is **required and keyword-only**, deliberately. It is the event
        source that delivered the wake, and without it the record collapses to "some
        route woke this agent" — which greens every *other* route wired to the same
        agent, including one whose every delivery is being rejected. That is instance
        5 surviving the instrument, so the field cannot be optional: a caller must
        not be able to drop it by omission, and it must not be positionally
        confusable with ``delivery``.

        Both granularities are written here, in one update under one lock, so the
        scalars and ``by_route`` can never disagree about the same wake.
        """
        with self._lock:
            wake = self._doc.agent_wakes.setdefault(agent, AgentWakeEvidence())
            at = _iso(self._now())
            wake.ok += 1
            wake.last_ok_at = at
            wake.last_ok_delivery = delivery
            wake.last_ok_route = route
            per_route = wake.by_route.setdefault(route, RouteWakeEvidence())
            per_route.ok += 1
            per_route.last_ok_at = at
            per_route.last_ok_delivery = delivery
            self._flush_locked()

    def record_wake_failed(self, agent: str, reason: str) -> None:
        with self._lock:
            wake = self._doc.agent_wakes.setdefault(agent, AgentWakeEvidence())
            wake.failed += 1
            wake.last_failed_at = _iso(self._now())
            wake.last_failed_reason = _reason(reason)
            self._flush_locked()

    def record_wake_refused(self, agent: str, reason: str) -> None:
        """A wake the router deliberately declined — gated, not broken (see the class)."""
        with self._lock:
            wake = self._doc.agent_wakes.setdefault(agent, AgentWakeEvidence())
            wake.refused += 1
            wake.last_refused_at = _iso(self._now())
            wake.last_refused_reason = _reason(reason)
            self._flush_locked()

    def record_queue_depth(self, agent: str, pending: int) -> None:
        """The scheduler's pending-wake depth for ``agent`` — the transient wake edge.

        Called on every enqueue and completion, so the emitted claim can say
        whether a wake is queued or in flight *right now*, not merely that one
        happened once. Kept as a plain integer rather than a timestamped history:
        the ledger asks "is there an edge", not "how deep was the queue".
        """
        with self._lock:
            wake = self._doc.agent_wakes.setdefault(agent, AgentWakeEvidence())
            if wake.queued == pending:
                return  # no change — skip the write entirely
            wake.queued = pending
            self._flush_locked()

    # --- freeze self-test evidence (instance 2: the control nobody could read) ---

    def record_freeze_selftest(self, status: str, detail: str = "") -> None:
        with self._lock:
            self._doc.freeze_selftest = SelfTestEvidence(
                status=status, at=_iso(self._now()), detail=_reason(detail) or None
            )
            self._flush_locked()

    def _flush_locked(self) -> None:
        """Persist the document atomically. Caller holds ``_lock``. Never raises.

        ``write temp + os.replace`` in the target's own directory, so the swap is
        atomic on the same filesystem and a concurrent reader always sees one whole
        document. A failure here is logged **once** and then swallowed forever: the
        daemon's job is waking agents, and losing the ledger's evidence is strictly
        better than losing a wake.
        """
        self._doc.updated_at = _iso(self._now())
        if self.path is None:
            return
        try:
            _atomic_write(self.path, json.dumps(self._doc.to_json(), indent=2, sort_keys=True))
        except OSError as exc:
            if not self._write_failed:
                self._write_failed = True
                logger.warning(
                    "event=evidence_write_failed path=%s detail=%s "
                    "(evidence is now in-memory only for this process)",
                    self.path,
                    exc,
                )


def _atomic_write(path: str, text: str) -> None:
    """Replace ``path`` with ``text`` in one step a concurrent reader cannot see torn.

    The temp file is created **in the target's own directory** so ``os.replace`` is a
    same-filesystem rename and therefore atomic; a reader (the claims emitter, an
    operator's ``cat``) always sees either the whole previous document or the whole
    new one.
    """
    directory = os.path.dirname(path) or "."
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".evidence-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        # Deliberately **no fsync**. Atomicity is what a reader needs and ``os.replace``
        # already gives it; durability across a power cut is not worth what an fsync
        # costs here — this runs on the webhook ack path and under the store's one
        # lock, so every recorder would serialise behind the disk. The fail-direction
        # is already safe: an unreadable document reports never-proven, so the worst a
        # lost write can do is understate what we have proven, never overstate it.
        #
        # 0644: no secrets live here, and the NOC's converge must read it without a
        # privilege grant. mkstemp creates 0600, so widen it before the swap.
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    except BaseException:
        # Never leave a stray .evidence-*.tmp behind on a failed write — the state dir
        # would fill with them one per failed flush.
        with suppress(OSError):
            os.unlink(temp)
        raise


def _load(path: str) -> EvidenceDocument:
    """Read the document at ``path``, or an empty one. Never raises.

    A missing file is the normal first-boot case. A corrupt one is logged and
    treated as empty — evidence resets to never-proven, which is the safe
    direction: the ledger reports a capability as unproven until it is proven
    again, rather than trusting a document it could not parse.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return EvidenceDocument.from_json(json.load(handle))
    except FileNotFoundError:
        return EvidenceDocument()
    except (OSError, ValueError) as exc:
        logger.warning("event=evidence_unreadable path=%s detail=%s", path, exc)
        return EvidenceDocument()


def read_evidence(path: str | None = DEFAULT_EVIDENCE_FILE) -> EvidenceDocument:
    """The read side, for a process that must not write: the claims emitter and CLI.

    Returns an empty document when the file is absent or unreadable — the emitter
    then reports every claim as never-proven, which is the honest answer when the
    daemon has produced no evidence this reader can see.
    """
    if path is None:
        return EvidenceDocument()
    return _load(path)

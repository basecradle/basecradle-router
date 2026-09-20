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

**And an idempotency dedup is not a refusal** (basecradle-router#218). A duplicate
delivery the router collapses is the *only* outcome here that can be reached exclusively
**through a success**: the dedup cache is marked only after a wake has already fired and
succeeded, so a ``duplicate_delivery`` outcome is a downstream consequence of a recorded
``stage=wake outcome=ok`` within the cache's TTL. Every other refusal means the opposite
— a wake that should have run did not (a live converge freeze, a tripped breaker). Lumped
into one counter, the newest recorded attempt on a perfectly healthy route reads as a
rejection, which is what it did read live: ``ok=4 failed=0 refused=2``, the last refusal
2.6 ms after the last success, both of them dedups. So the dedup gets its own counter and
its own ``last_deduped_*`` fields, and ``refused`` narrows to mean exactly *a gate declined
a wake that would otherwise have run*. **The counter is the classification**, deliberately:
a consumer must never have to parse this store's reason strings to tell a benign collapse
from a real refusal — that would be a second spelling of our contract living in someone
else's repo (basecradle-noc#344/#366).

**A coalesce is counted as a dedup, because it is one** (basecradle-router#272). A delivery
the pipeline collapses into a wake that already read it — its event happened before a
successful wake on the same stream started — is the same benign collapse at stream
granularity rather than delivery granularity, and it is reachable only through a success
in exactly the same way: coverage is recorded only when a wake succeeds. So it moves
``deduped``, never ``refused``. A wake *dropped* at dispatch because its work ended (a
closed issue) records nothing here at all: no wake was attempted and no gate held the
agent, so it says nothing about the edge either way.

**And every outcome records whether it was real.** Since basecradle-router#208 the
router can wake an agent with its own signed synthetic probe, which is what gives the
wake edge a lever it otherwise lacks — an ``evidence``-kind claim cannot exercise
itself. That lever is only safe if a synthetic proof can never be mistaken for
production traffic, so provenance is recorded at write time beside every outcome:
which ``route`` delivered it, and whether that delivery was ``synthetic``. A probe
therefore lands in its own delivery sink and its own ``by_route`` row, and the
agent-wide scalars say plainly which kind of traffic last proved the edge.

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
  a proven capability read as never-proven. The one field that does *not* survive
  is ``queued``, because it is not evidence: it is a live reading of the scheduler
  that wrote it, and that scheduler is gone after a restart
  (:meth:`EvidenceStore._discard_stale_queue_depths`).
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
- *Nothing it leaves behind lacks an end* (basecradle-router#281, under
  ``constitution.md`` → How We Build, "Whatever creates, cleans up"). The document is
  the deliverable: one fixed name, replaced in place, never a dated copy. A flush's
  one by-product is the ``.evidence-*.tmp`` it swaps in, and a flush killed before
  its swap leaves that temp where no handler ever reaches it, so the daemon sweeps
  those when it starts (:meth:`EvidenceStore._remove_orphaned_temps`). The entries of
  an agent since deregistered or a route since disabled are *not* a by-product: they
  are a record, kept on purpose (:class:`EvidenceDocument`).

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

from basecradle_router.dedup import DUPLICATE_DELIVERY

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

#: The name every flush's temp file carries in the document's own directory. One
#: spelling for the writer and for the startup sweep that removes what a killed writer
#: left behind, so the two can never drift apart (basecradle-router#281).
_TEMP_PREFIX = ".evidence-"
_TEMP_SUFFIX = ".tmp"


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

    ``woke`` is **the route's decision, not a count of wakes that ran** — it is recorded
    at ``normalize``, before an agent is even resolved, so a delivery the dedup cache
    later collapses or a gate later refuses is counted here all the same. That is the
    right meaning for a *sink* claim, which asks whether the integration is armed and
    actionable; how many wakes actually fired is :class:`AgentWakeEvidence`'s question,
    and its four outcome counters are the only honest answer to it. Named explicitly
    because the two readings differ by exactly the collapses and refusals that
    basecradle-router#218 exists to keep legible.
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

    All four outcomes are kept here, not just the successes (basecradle-router#208).
    Once one route is **synthetic** — the probe firing at the fleet's own wake path —
    a route-less failure or refusal counter would let a probe's outcome read as a
    production one and vice versa, which is precisely the masquerade the synthetic is
    forbidden to commit. Each row therefore stands alone: it says what *this* route
    did to *this* agent, with the reason, so a reader never has to borrow a scalar
    that might describe a different route's event.

    ``deduped`` is the fourth, and it is kept out of ``refused`` for the reason the class
    above spells out: it is the one outcome only a *successful* wake can produce
    (basecradle-router#218). This is also the granularity the misreading was measured at
    — the per-route row is what the NOC's per-recipient claim points into, so a dedup
    counted as a refusal here is a healthy route reading as a rejected one.
    """

    ok: int = 0
    failed: int = 0
    refused: int = 0
    deduped: int = 0
    last_ok_at: str | None = None
    last_ok_delivery: str | None = None
    last_failed_at: str | None = None
    last_failed_reason: str | None = None
    last_refused_at: str | None = None
    last_refused_reason: str | None = None
    last_deduped_at: str | None = None


@dataclass
class AgentWakeEvidence:
    """What one agent's wake edge has demonstrably done — the re-wake proof.

    ``last_ok_at``/``last_ok_delivery``/``last_ok_route`` are the ledger's evidence
    pointer: the timestamp, delivery id, and event source of the last ``stage=wake
    outcome=ok``, joinable straight back to both halves of that wake in journald.
    ``None`` means the router has **never** successfully woken this agent —
    never-proven, the state that makes a parked builder visible.

    ``refused`` counts wakes a gate deliberately declined — **a held NOC wake-lock or a
    tripped breaker**, and nothing else. It is kept apart from ``failed`` because the two
    mean opposite things: a refusal is the router working correctly, a failure is
    the wake path broken. An agent whose only recent activity is refusals is
    *gated*, not dead — and a ledger that conflated them would cry wolf on every
    converge.

    ``deduped`` counts deliveries collapsed into a wake that already ran for them — a
    duplicate caught by the dedup cache, or a delivery coalesced into a wake that read it
    (basecradle-router#272) — and it is a third thing again (basecradle-router#218). A
    refusal says *a wake that should have run did not*; a dedup says *a wake that must not
    run did not* — and, because the cache is marked only after a successful wake, a dedup is
    reachable **only** through an ``ok``. Both halves of the old lumped counter are still
    visible, so nothing is lost: what changes is that ``last_refused_at`` no longer moves
    for an event that proves the edge is working. Note the two live *outside* ``refused``
    and outside ``failed`` both — a genuine rejection of a *delivery* (a bad signature, a
    malformed payload, an untrusted sender) never reaches this class at all; it is counted
    at the sink, in :class:`DeliverySinkEvidence`.

    ``by_route`` is the same proof at **(agent, route)** granularity — see
    :class:`RouteWakeEvidence` for why the scalars above it are not enough on their
    own. The three ``last_ok_*`` scalars describe one event (the most recent
    successful wake, whatever route delivered it) and are written together in one
    update, so they cannot drift apart from each other or from ``by_route``.

    ``queued`` is the scheduler's pending-wake depth for this agent as of the last
    change — the transient wake edge. Non-zero means a wake is queued or in flight
    right now, so the agent will be woken again regardless of anything else. It is the
    one field here that is not history: it describes the process that wrote it, so the
    daemon zeroes it when it loads the document at boot (basecradle-router#264).

    Each ``last_*`` trio carries **which route** produced it and **whether it was
    synthetic** (basecradle-router#208). ``last_ok_route`` alone was enough while every
    route was real; it stopped being enough the moment the router could wake an agent
    with its own probe, because a reader would then have to know which route names
    happen to be synthetic — a fact that lives in the registry, changes with
    deployment, and answers *no* for a route since removed, silently promoting a
    synthetic proof to a real one. So provenance is recorded at write time, from the
    event, where it is a fact rather than an inference.
    """

    ok: int = 0
    failed: int = 0
    refused: int = 0
    deduped: int = 0
    last_ok_at: str | None = None
    last_ok_delivery: str | None = None
    last_ok_route: str | None = None
    last_ok_synthetic: bool | None = None
    last_failed_at: str | None = None
    last_failed_reason: str | None = None
    last_failed_route: str | None = None
    last_failed_synthetic: bool | None = None
    last_refused_at: str | None = None
    last_refused_reason: str | None = None
    last_refused_route: str | None = None
    last_refused_synthetic: bool | None = None
    last_deduped_at: str | None = None
    last_deduped_route: str | None = None
    last_deduped_synthetic: bool | None = None
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
    """The whole on-disk document: sinks by route, wakes by agent, last self-test.

    **No key is ever removed, and that is deliberate** (basecradle-router#281).
    ``delivery_sinks``, ``agent_wakes``, and each agent's ``by_route`` are filled by
    ``setdefault`` and never pruned, so an agent deregistered or a route disabled keeps
    its entry for the life of the box. That entry is a record, not a leftover ("logs and
    audit records are not temp files," as the ruling on #281 put it), and keeping it is
    the decided answer, for three reasons:

    - *It is worth most exactly when the removal was a mistake.* After an accidental
      deregistration, the departed agent's last proven wake, its last failure reason, and
      its counters are the history an operator reaches for. A prune at boot would discard
      them at the one moment they matter.
    - *Nothing is armed on it.* Claims are emitted only for registered agents and enabled
      routes (:mod:`~basecradle_router.claims`), so a departed agent's entry and a
      disabled route's sink reach no claim at all. A disabled route's ``by_route`` row
      still shows in its agent's ``detail``, as history and never as an edge, because an
      agent whose only proof came from a route nobody enables any more is the
      parked-builder shape this store exists to expose. If a slug is registered again,
      its old proof is still true of that account, and the NOC's 7-day age-of-proof TTL
      decides whether it still counts, exactly as it does for every other proof.
    - *It is bounded.* The document grows with the agents ever registered and the routes
      ever enabled on this box, never with traffic.

    The TTL already retires stale proof as evidence without destroying it as a record,
    and a prune would do the opposite on a guess about what an operator no longer needs.
    """

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
    _reclassify_legacy_dedup(wake, provenance=_LEGACY_DEDUP_PROVENANCE)
    for per_route in wake.by_route.values():
        _reclassify_legacy_dedup(per_route)
    return wake


#: The provenance fields the agent-wide record carries beside each ``last_*`` timestamp,
#: as ``(refusal field, dedup field)`` pairs to move together. The per-route record has
#: none — its row already *is* one route's, and a route cannot be synthetic on one line
#: and real on the next.
_LEGACY_DEDUP_PROVENANCE = (
    ("last_refused_route", "last_deduped_route"),
    ("last_refused_synthetic", "last_deduped_synthetic"),
)


def _reclassify_legacy_dedup(
    record: AgentWakeEvidence | RouteWakeEvidence,
    *,
    provenance: tuple[tuple[str, str], ...] = (),
) -> None:
    """Move a pre-#218 trailing ``duplicate_delivery`` out of the refusal fields.

    **Why migrate at all.** This document is durable on purpose — it lives in ``/var/lib``
    precisely so an age-of-proof survives a deploy — which makes the misreading durable
    too. The two live per-route rows that surfaced this carry a
    ``last_refused_reason: duplicate_delivery`` stamped 2.6 ms after their last success,
    and code that merely stops *writing* that would never overwrite it: the field only
    moves when a genuine refusal happens, and the whole point is that one has not. So the
    fix has to reach the rows already on disk, or the page it is meant to prevent still
    fires.

    **Why not bump** :data:`EVIDENCE_VERSION`. That would clear it, and the cure is far
    worse than the disease: :meth:`EvidenceDocument.from_json` discards a document whose
    version it does not recognise, so every capability this box has ever proven would read
    *never-proven* on the next boot — the exact reset ``/var/lib`` was chosen to prevent.
    The shape is additive anyway (new fields, old ones unchanged in meaning), so a version
    that means *incompatible reshape* would be lying about what changed.

    **Why this is a reclassification and not a guess.** The reason string is an exact
    classifier for the single event it describes, and ``duplicate_delivery`` had exactly
    one writer. So the ``last_*`` group moves faithfully: what was recorded as the last
    refusal provably was not one.

    **What it cannot recover.** Nothing ever stored the *mix* of a cumulative counter, so
    history cannot be split. Exactly one count moves — the event we can positively
    identify — which conserves ``refused + deduped``; any earlier dedup stays miscounted
    in ``refused`` and decays as real events accumulate. On a row that had several
    refusals this can leave ``refused >= 1`` beside a null ``last_refused_at``: read that
    as *there were refusals, and the most recent thing we filed as one turned out not to
    be*. It understates refusals, which is this store's standing fail-direction — never
    overstate what we have proven, and never invent a rejection that did not happen.

    Idempotent by construction: after one pass the condition is false, and no write since
    #218 ever puts :data:`~basecradle_router.dedup.DUPLICATE_DELIVERY` in a refusal reason
    again.
    """
    if record.last_refused_reason != DUPLICATE_DELIVERY:
        return
    record.deduped += 1
    record.refused = max(0, record.refused - 1)
    record.last_deduped_at = record.last_refused_at
    record.last_refused_at = None
    record.last_refused_reason = None
    for refusal_field, dedup_field in provenance:
        setattr(record, dedup_field, getattr(record, refusal_field))
        setattr(record, refusal_field, None)


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
        if path:
            self._remove_orphaned_temps(path)
        self._doc = _load(path) if path else EvidenceDocument()
        # One warning per process for an unwritable store, not one per delivery: a
        # broken state dir must be visible exactly once, never a flood that buries
        # the wake lines an operator is actually reading.
        self._write_failed = False
        self._discard_stale_queue_depths()

    @staticmethod
    def _remove_orphaned_temps(path: str) -> None:
        """Remove every ``.evidence-*.tmp`` a previous process left beside the document.

        :func:`_atomic_write` unlinks its own temp on any failure it survives. It cannot
        survive being killed: a SIGKILL at ``TimeoutStopSec`` on a stuck drain, an OOM
        kill, or a power cut between ``mkstemp`` and ``os.replace`` never reaches that
        handler, and the temp then sits in the state dir with nothing that will ever
        remove it (basecradle-router#281). The window is microseconds, but it opens on
        every flush, and a flush follows every wake outcome.

        **Why here, and only here.** Construction is the one moment no temp in this
        directory can be live: the daemon is the document's sole writer (a single worker,
        by the unit's design), and no flush of this process has begun yet. Any later, the
        sweep could race this process's own swap. Out-of-process readers go through
        :func:`read_evidence`, which never writes and so never sweeps.

        **Removed, never recovered.** An orphan may be complete or torn, and nothing
        committed it, so the document on disk stays the last one that was. The update it
        carried is lost, which understates what we have proven and never overstates it.

        **Narrow, and quiet when it cannot act.** Only regular files carrying the temp's
        name in the document's own directory are touched: never the document itself, a
        symlink, or anything else there. A directory that is missing or cannot be listed,
        or a temp that cannot be unlinked, gets no line of its own: on the box's state dir
        (``0755``, owned by the daemon's user) each in practice means a directory this
        process cannot write, which the first flush already reports exactly once
        (``event=evidence_write_failed``).
        """
        directory = os.path.dirname(path) or "."
        document = os.path.basename(path)
        try:
            with os.scandir(directory) as entries:
                orphans = sorted(
                    entry.name
                    for entry in entries
                    if entry.name.startswith(_TEMP_PREFIX)
                    and entry.name.endswith(_TEMP_SUFFIX)
                    and entry.name != document
                    and entry.is_file(follow_symlinks=False)
                )
        except OSError:
            return
        removed = []
        for name in orphans:
            with suppress(OSError):
                os.unlink(os.path.join(directory, name))
                removed.append(name)
        if removed:
            logger.warning(
                "event=evidence_orphaned_temps_removed dir=%s removed=%s "
                "(a flush was interrupted before its swap; the update it carried never "
                "reached the document)",
                directory,
                ",".join(removed),
            )

    def _discard_stale_queue_depths(self) -> None:
        """Zero every ``queued`` loaded from disk, because no scheduler in this process owns it.

        Every other field in the document is history (a count, a timestamp, a proof), and
        history survives a restart. ``queued`` is a live reading of one process's
        scheduler, and that scheduler ended with its process. A graceful stop publishes
        ``pending=0`` for every agent before it exits, because the drain waits for the
        observer (:meth:`~basecradle_router.scheduler.WakeScheduler.wait_idle`). So a
        non-zero value on disk at load means the previous daemon stopped *without*
        draining: it was SIGKILLed at ``TimeoutStopSec``, OOM-killed, or crashed with a wake
        queued or in flight. Reloaded as-is, that value advertised a pending wake no live
        process owned, until that agent was next woken. Meanwhile it held the NOC's deploy
        idle-gate deferred on every tick, and it read as a live ``queued-wake`` edge on an
        agent that may be unreachable (basecradle-router#264).

        **Why here, and only here.** Only the daemon constructs a store over the real file;
        every other reader goes through :func:`read_evidence`, which must keep the live
        value, because it is exactly what the NOC reads while the daemon runs. The scheduler
        that will feed this store has not been built yet, so the true depth is zero for
        every agent. Waiting for the scheduler's first report instead would not work: that
        report arrives only when the agent is woken again, and that wait is the bug.

        **Flushed at once when anything changed**, rather than riding the next record: the
        NOC reads the file, not this process's memory, so a correction held only in memory
        corrects nothing it can see. A clean boot finds nothing to change and writes
        nothing. Each discard is logged, never silent: a stale depth is the only trace that
        a wake queued or in flight under the previous process may never have completed.
        """
        stale = {agent: wake.queued for agent, wake in self._doc.agent_wakes.items() if wake.queued}
        if not stale:
            return
        for agent, pending in sorted(stale.items()):
            logger.warning(
                "event=evidence_stale_queue_cleared agent=%s pending=%s "
                "(the previous daemon stopped without draining; no scheduler in this process "
                "owns that work)",
                agent,
                pending,
            )
        with self._lock:
            for agent in stale:
                self._doc.agent_wakes[agent].queued = 0
            self._flush_locked()

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

    def record_wake_ok(self, agent: str, delivery: str, *, route: str, synthetic: bool) -> None:
        """The proof that matters: this agent's wake edge fired and succeeded.

        ``route`` and ``synthetic`` are **required and keyword-only**, deliberately.

        ``route`` is the event source that delivered the wake, and without it the
        record collapses to "some route woke this agent" — which greens every *other*
        route wired to the same agent, including one whose every delivery is being
        rejected. That is instance 5 surviving the instrument.

        ``synthetic`` is whether the wake was a probe the fleet fired at itself rather
        than real traffic (basecradle-router#208). It cannot be optional for the same
        reason: a defaulted ``False`` is a caller's silence being recorded as the
        assertion *this was real*, and a synthetic proof quietly reading as production
        traffic is the one thing a synthetic must never be able to do.

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
            wake.last_ok_synthetic = synthetic
            per_route = wake.by_route.setdefault(route, RouteWakeEvidence())
            per_route.ok += 1
            per_route.last_ok_at = at
            per_route.last_ok_delivery = delivery
            self._flush_locked()

    def record_wake_failed(self, agent: str, reason: str, *, route: str, synthetic: bool) -> None:
        """The wake path is broken for this agent — over this route, really or synthetically.

        Route- and provenance-tagged for the same reasons :meth:`record_wake_ok` is: an
        untagged failure counter would let a probe's refusal read as a production
        outage, and a production outage read as a probe's.
        """
        with self._lock:
            wake = self._doc.agent_wakes.setdefault(agent, AgentWakeEvidence())
            at = _iso(self._now())
            wake.failed += 1
            wake.last_failed_at = at
            wake.last_failed_reason = _reason(reason)
            wake.last_failed_route = route
            wake.last_failed_synthetic = synthetic
            per_route = wake.by_route.setdefault(route, RouteWakeEvidence())
            per_route.failed += 1
            per_route.last_failed_at = at
            per_route.last_failed_reason = _reason(reason)
            self._flush_locked()

    def record_wake_refused(self, agent: str, reason: str, *, route: str, synthetic: bool) -> None:
        """A wake a gate deliberately declined — gated, not broken (see the class).

        A held NOC wake-lock or a tripped breaker, and nothing else: a collapsed duplicate
        delivery goes to :meth:`record_wake_deduped` instead. Both are the router working
        correctly, but only this one means a wake that *should* have run did not, and only
        this one should move the newest-attempt reading of a route's health.
        """
        with self._lock:
            wake = self._doc.agent_wakes.setdefault(agent, AgentWakeEvidence())
            at = _iso(self._now())
            wake.refused += 1
            wake.last_refused_at = at
            wake.last_refused_reason = _reason(reason)
            wake.last_refused_route = route
            wake.last_refused_synthetic = synthetic
            per_route = wake.by_route.setdefault(route, RouteWakeEvidence())
            per_route.refused += 1
            per_route.last_refused_at = at
            per_route.last_refused_reason = _reason(reason)
            self._flush_locked()

    def record_wake_deduped(self, agent: str, *, route: str, synthetic: bool) -> None:
        """A delivery collapsed into the wake that already ran for it.

        Its own outcome rather than a refusal, because it is the only one here that a
        *success* produces: the dedup cache is marked only after a wake has fired and
        succeeded, so this record can exist only downstream of an ``ok`` recorded within
        the cache's TTL (basecradle-router#218). Counting it as a refusal made the newest
        recorded attempt on a healthy route read as a rejection. A delivery *coalesced*
        into a wake that read it lands here for the same reason (basecradle-router#272):
        stream coverage, like the dedup mark, is recorded only on a success.

        Takes no ``reason``: there is exactly one, and **the counter is the
        classification**. Handing a consumer a reason string to parse is what would
        oblige the NOC to keep a second spelling of this contract, which its own rulings
        forbid (basecradle-noc#344/#366). ``route`` and ``synthetic`` stay required and
        keyword-only for the reasons :meth:`record_wake_ok` gives.
        """
        with self._lock:
            wake = self._doc.agent_wakes.setdefault(agent, AgentWakeEvidence())
            at = _iso(self._now())
            wake.deduped += 1
            wake.last_deduped_at = at
            wake.last_deduped_route = route
            wake.last_deduped_synthetic = synthetic
            per_route = wake.by_route.setdefault(route, RouteWakeEvidence())
            per_route.deduped += 1
            per_route.last_deduped_at = at
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
    fd, temp = tempfile.mkstemp(dir=directory, prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX)
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

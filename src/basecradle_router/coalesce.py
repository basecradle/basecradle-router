"""Wake coverage — collapse a queued delivery into the wake that already read it.

**The incident (basecradle-router#272, from basecradle/basecradle#548).** One handoff,
basecradle-ruby#149, produced five full agent sessions in 23 minutes. Every accepted
delivery became its own queued wake, launched the second the previous session exited:
the ``opened`` and the ``labeled`` of a single labeled create, a capital comment, a
reopen comment, a re-applied label. Each redundant session re-read the repo and
re-verified finished work; two ran against a closed issue.

**The rule this module holds: a delivery is covered by a successful wake of its agent on
its stream that started after the delivery's event happened.** A woken agent reads its
stream's *whole current state* when it starts — a builder its issue thread, a harness
agent every unseen item past its marks on its timeline — so an event that happened before
that start is one the session saw. Collapsing it loses nothing; waking again for it is
the waste.

**Per agent, never per stream alone** (basecradle-router#311). A handoff issue belongs to
one agent, but a timeline is shared by every agent that views it, and one message on it
is a delivery to each of them. What one agent's session read says nothing about what
another agent's did, so coverage is keyed on ``(agent, stream)`` — the same scope the
wake-rate breaker counts a stream in — and one agent's wake can never collapse another
agent's delivery.

**Why the collapse happens at dispatch, not at enqueue.** The per-agent queue is left
exactly as it was — every delivery still takes its place in its agent's FIFO — and the
pipeline asks this store, as each one reaches the front, whether a wake that has already
*succeeded* covered it. Three properties fall out of deciding it there, and each is one a
collapse at enqueue would have had to buy back:

- **At most one follow-up, by construction.** Deliveries that queued up behind a running
  wake all happened before the *next* wake on their stream starts. So the first of them
  to reach the front wakes the agent once, and every one behind it is covered by that
  wake the moment it succeeds. N deliveries during a session cost exactly one follow-up
  session, never N — which is the ask, without the scheduler learning what an issue is.
- **A failed wake covers nothing.** Coverage is recorded only on success, so a delivery
  behind a wake that failed keeps its own chance to wake the agent — the same resilience
  the delivery dedup has (:mod:`basecradle_router.dedup`), for the same reason. Merging
  deliveries at enqueue would have let one failed wake silently take its whole batch down.
- **The decision is made with the facts, not a forecast.** At enqueue the covering wake
  has not started, or has not finished; here it has done both, and its actual start time
  is the one compared against.

**The comparison is deliberately one-sided.** ``occurred_at`` comes from the source and
is never later than the truth by its own clock — github's timestamps and the platform's
(``created_at.utc.iso8601``) are both whole seconds, truncated — while ``started_at`` is
the router's own clock the instant the successful attempt launched. So an event judged
covered happened at most a second after that launch, plus whatever the two hosts' NTP
clocks disagree by (milliseconds) — and a session spends seconds booting before it reads
anything (a harness wake loads its MCP servers first). Every error truncation introduces
errs towards *not* covering: an extra wake, the old behaviour, never a lost one.

**Opt-in, per route.** A route opts its streams in by stamping
:attr:`~basecradle_router.models.Event.occurred_at`; an event without it is never
covered and never records coverage, so a source that does not stamp it — the synthetic
``probe``, whose every delivery is a measurement in its own right — keeps
one-delivery-one-wake exactly as before. ``github`` opted in with #272; the
``basecradle`` platform route opted in with #311, a capital decision: a harness wake
reconciles every unseen item on its timeline from the timeline uuid alone, whatever
delivery woke it, so N messages that queued behind a long wake cost one follow-up rather
than N process starts that each find nothing new — and a burst of empty wakes can no
longer trip the harness's own per-timeline breaker and drop the live delivery behind it.

**What coverage claims — and what it does not.** It claims the covering session *had*
the delivery's event to read, because the event predates its start. It does not claim
the session acted on it: whatever a successful wake leaves for later — a harness item it
left unsettled, or a whole wake the harness's own breaker declined (that exits ``0``
having read nothing) — waits for that stream's next wake. It always did whenever nothing
happened to be queued behind the wake; what the collapse changes is that the deliveries
which queued up *with* the one that launched it no longer each re-offer it at once.
Nothing is lost for good — the item stays unread past the agent's mark, and the stream's
next wake reads it — but a success is trusted to mean "read", so an agent that exits
``0`` without reading is where a later wake has to pick the work up.

Thread-safe: recorded and consulted from the pipeline's worker threads, under one lock
held for a dictionary operation. Bounded: an LRU of streams, so a long-running daemon
keeps only the streams it has woken recently — an evicted stream only means a later
delivery for it is not collapsed, which is the old behaviour, never a lost wake.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

from basecradle_router.models import Event

#: How many (agent, stream) pairs' coverage is kept. A stream is one handoff issue or one
#: timeline; the entries are two small values each, and coverage only has to outlive the
#: queue drain behind the wake that recorded it — so this is generous by orders of
#: magnitude.
DEFAULT_CAPACITY = 4096


@dataclass(frozen=True, slots=True)
class Coverage:
    """The successful wake that covers a stream: which delivery launched it, and when."""

    delivery: str
    started_at: datetime


class WakeCoverage:
    """Per-(agent, stream) record of the latest successful wake's start — see the module.

    ``agent_key`` is the agent's :attr:`~basecradle_router.models.Agent.harness_key` —
    its one harness instance, the thing that did the reading — the same key the per-agent
    lock and the breaker are held on.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._capacity = capacity
        self._lock = threading.Lock()
        self._streams: OrderedDict[tuple[str, str], Coverage] = OrderedDict()

    def record(self, agent_key: str, event: Event, started_at: datetime) -> None:
        """``agent_key``'s wake for ``event`` succeeded, from an attempt started at ``started_at``.

        A no-op for an event whose route does not stamp ``occurred_at`` — the opt-in.
        Keeps the later of two starts for one agent's stream, so the record can only
        move forward in time.
        """
        if event.occurred_at is None:
            return
        key = (agent_key, event.stream_key)
        with self._lock:
            known = self._streams.get(key)
            if known is None or started_at >= known.started_at:
                self._streams[key] = Coverage(event.delivery_id, started_at)
            self._streams.move_to_end(key)
            while len(self._streams) > self._capacity:
                self._streams.popitem(last=False)

    def covering(self, agent_key: str, event: Event) -> Coverage | None:
        """The successful wake that already covered ``event`` for ``agent_key``, or ``None``.

        Covered means a wake of that agent on the event's stream succeeded, and the
        attempt that succeeded started strictly after the event happened.
        """
        if event.occurred_at is None:
            return None
        with self._lock:
            known = self._streams.get((agent_key, event.stream_key))
        if known is None or not event.occurred_at < known.started_at:
            return None
        return known

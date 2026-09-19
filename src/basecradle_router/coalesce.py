"""Wake coverage — collapse a queued delivery into the wake that already read it.

**The incident (basecradle-router#272, from basecradle/basecradle#548).** One handoff,
basecradle-ruby#149, produced five full agent sessions in 23 minutes. Every accepted
delivery became its own queued wake, launched the second the previous session exited:
the ``opened`` and the ``labeled`` of a single labeled create, a capital comment, a
reopen comment, a re-applied label. Each redundant session re-read the repo and
re-verified finished work; two ran against a closed issue.

**The rule this module holds: a delivery is covered by a successful wake on its stream
that started after the delivery's event happened.** A woken agent reads its issue's
*whole current state* when it starts, so an event that happened before that start is
one the session saw. Collapsing it loses nothing; waking again for it is the waste.

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
is never later than the truth (github's timestamps are whole seconds, truncated), while
``started_at`` is the router's own clock the instant the successful attempt launched.
So an event judged covered happened at most a second after that launch — and a session
spends far longer than that booting before it reads anything. Every error the source's
clock can introduce errs towards *not* covering: an extra wake, the old behaviour, never
a lost one.

**Opt-in, per route.** A route opts its streams in by stamping
:attr:`~basecradle_router.models.Event.occurred_at`; an event without it is never
covered and never records coverage, so a source that does not stamp it — the
``basecradle`` platform route, the synthetic ``probe`` — keeps one-delivery-one-wake
exactly as before. Whether the platform route should opt in is a platform-semantics
decision, deliberately not taken here.

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

#: How many streams' coverage is kept. A stream is one handoff issue; the entries are
#: two small values each, and coverage only has to outlive the queue drain behind the
#: wake that recorded it — so this is generous by orders of magnitude.
DEFAULT_CAPACITY = 4096


@dataclass(frozen=True, slots=True)
class Coverage:
    """The successful wake that covers a stream: which delivery launched it, and when."""

    delivery: str
    started_at: datetime


class WakeCoverage:
    """Per-stream record of the latest successful wake's start — see the module."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._capacity = capacity
        self._lock = threading.Lock()
        self._streams: OrderedDict[str, Coverage] = OrderedDict()

    def record(self, event: Event, started_at: datetime) -> None:
        """A wake for ``event`` succeeded, and the attempt that did started at ``started_at``.

        A no-op for an event whose route does not stamp ``occurred_at`` — the opt-in.
        Keeps the later of two starts for one stream, so the record can only move
        forward in time.
        """
        if event.occurred_at is None:
            return
        key = event.stream_key
        with self._lock:
            known = self._streams.get(key)
            if known is None or started_at >= known.started_at:
                self._streams[key] = Coverage(event.delivery_id, started_at)
            self._streams.move_to_end(key)
            while len(self._streams) > self._capacity:
                self._streams.popitem(last=False)

    def covering(self, event: Event) -> Coverage | None:
        """The successful wake that already covered ``event``, or ``None`` if none did.

        Covered means a wake on the event's stream succeeded, and the attempt that
        succeeded started strictly after the event happened.
        """
        if event.occurred_at is None:
            return None
        with self._lock:
            known = self._streams.get(event.stream_key)
        if known is None or not event.occurred_at < known.started_at:
            return None
        return known

"""Delivery dedup — collapse a duplicate webhook delivery into one wake.

A single logical event can reach the router as **more than one webhook
delivery**: e.g. two fleet GitHub Apps installed on the same repo, both
subscribed to the same event, each POSTing to the router
(basecradle-router#133). GitHub stamps every such delivery of *one event* with
the **same** ``X-GitHub-Delivery`` GUID, so they are byte-distinct envelopes
carrying one identical event. The per-agent lock serialises them (no collision,
no split-brain), but without dedup each delivery wakes the agent independently —
**N subscribed Apps cost N full agent sessions for one event**. That is pure
waste: a woken agent re-reads the *whole* issue thread, so the 2nd..Nth wake do
exactly what the 1st already did.

This is the **recently-woke delivery cache**: once the router *successfully*
wakes for a delivery key, it remembers that key for a short TTL; a later delivery
bearing the same key is collapsed to a visible IGNORE instead of a second wake.

**Why key on the delivery id** (the source's per-delivery GUID), not the event's
content:

- It is **identical** across the duplicate deliveries — GitHub holds the
  ``X-GitHub-Delivery`` GUID constant for every delivery of one event, which is
  exactly the duplication observed live in #133 (the same GUID logged twice). So
  deduping on it provably collapses the duplicate.
- It is **globally unique per genuine delivery**, so dedup can *never* drop a
  *distinct* event — the dangerous failure (lost work). Worst case, if some
  future duplication path ever used *distinct* GUIDs, dedup degrades to a no-op
  (the harmless, serialized double-wake of today) — never an over-collapse. A
  content-derived key would trade that safety for breadth; here the conservative
  key is the right one.

**Where it sits.** Consulted from the pipeline's slow half *inside the per-agent
lock*, and marked **only after a successful wake** (hence "recently-*woke*"). The
lock guarantees a duplicate enters the critical section *after* the original has
woken and marked, so it sees the mark and collapses; and if the original wake
*failed*, the duplicate is not suppressed and gets its own chance — resilient by
construction. The TTL therefore only has to outlast the gap from the original's
mark to the duplicate's check (milliseconds in the serialized case), but defaults
generous so a near-term redelivery is collapsed too. A ``0`` TTL disables dedup.

Thread-safe: :meth:`DeliveryDeduper.seen` / :meth:`mark` are called from the
pipeline's worker threads, so the cache is guarded by one short-held lock (the
check is microseconds). The clock is injectable — defaults to
:func:`time.monotonic`, immune to wall-clock adjustments, the right choice for a
TTL — so tests are deterministic and never sleep.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

# The cache accumulates one entry per distinct delivery ever woken — unbounded
# over a long-running daemon. So expired keys are swept periodically: every
# this-many marks, any entry older than the TTL is dropped (it would read as
# unseen anyway). The interval is large so the sweep's cost amortises to nothing;
# a key is also reclaimed eagerly the moment a `seen` check finds it expired.
_GC_INTERVAL = 1024


class DeliveryDeduper:
    """A short-TTL "recently-woke delivery key" cache, checked once per wake.

    Construct with a ``ttl`` (seconds a woken key stays remembered; ``0`` disables
    dedup) and an optional ``clock`` (defaults to :func:`time.monotonic`; tests
    inject a fake). Call :meth:`seen` before dispatching a wake and, iff the wake
    succeeds, :meth:`mark` the same key — the pipeline does both inside the
    per-agent lock so a duplicate serialised behind the original observes the mark.
    """

    def __init__(self, ttl: float = 600.0, *, clock: Callable[[], float] = time.monotonic) -> None:
        if ttl < 0:
            raise ValueError(f"ttl must be >= 0 (0 disables dedup), got {ttl}")
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}  # delivery key -> monotonic mark time
        self._marks_since_gc = 0

    @property
    def ttl(self) -> float:
        return self._ttl

    def seen(self, key: str) -> bool:
        """Whether ``key`` was successfully woken within the TTL (and not expired).

        An expired entry is reclaimed eagerly here, so the cache never reports a
        stale hit and a long-delayed redelivery wakes afresh.
        """
        if self._ttl <= 0:
            return False
        now = self._clock()
        with self._lock:
            marked = self._seen.get(key)
            if marked is None:
                return False
            if now - marked >= self._ttl:
                del self._seen[key]
                return False
            return True

    def mark(self, key: str) -> None:
        """Remember that the router just woke for ``key`` (call only after success)."""
        if self._ttl <= 0:
            return
        now = self._clock()
        with self._lock:
            self._seen[key] = now
            self._marks_since_gc += 1
            if self._marks_since_gc >= _GC_INTERVAL:
                self._gc(now)
                self._marks_since_gc = 0

    def _gc(self, now: float) -> None:
        """Drop expired keys so the cache stays bounded. Called under the lock."""
        cutoff = now - self._ttl
        stale = [key for key, marked in self._seen.items() if marked <= cutoff]
        for key in stale:
            del self._seen[key]

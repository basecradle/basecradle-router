"""Concurrency primitives guarding the wake step: per-agent locks and retry.

The router may have several wakes in flight at once — one per agent — but
**never two for the same agent**: an agent is one harness instance (one OS user,
one home, one memory), and two headless Claude sessions for it would clobber each
other's working tree *and* split-brain its memory. The constitution's
unified-identity rule — every input path for an agent converges on that one
instance — makes this serialization load-bearing, not just a clone-safety nicety:
when a second input source (BaseCradle, …) ships, its events and GitHub's must
funnel into the *same* ordered stream the single instance drains, never two
parallel invocations. This module provides the two mechanisms that enforce that,
both source-agnostic and independent of any wake's specifics:

- :class:`AgentLocks` — lock-striping by agent key (the agent's
  :attr:`~basecradle_router.models.Agent.harness_key`, i.e. its harness
  instance's identity). Different agents get different locks (they proceed
  concurrently); the same agent gets one lock, so every input for it — from any
  source — serializes into one ordered stream. This commits the daemon to a
  **threaded** execution model — the natural fit for wakes that are minutes-long
  blocking subprocesses — rather than asyncio.
- :func:`with_retry` — a bounded, deterministic retry around an operation that
  may fail transiently. Only :class:`TransientError` is retried; anything else
  propagates at once. The clock is injectable so tests never really sleep.

The thread pool that actually runs wakes concurrently — and the per-agent FIFO
queues that decide *which* wake runs *when* — live in
:mod:`~basecradle_router.scheduler`; this module is just the primitives it stands
on. The scheduler guarantees at most one wake in flight per agent, so
:class:`AgentLocks` is never actually contended in production: it stays in
:meth:`~basecradle_router.pipeline.Pipeline.execute` as the last-line guarantee of the
unified-identity invariant (and to serialise the direct, single-process ``handle``
path the offline tests drive), always acquired uncontended. Serialising by *parking a
pool thread on this lock* — rather than by scheduling — is exactly the starvation the
scheduler was built to end (basecradle-router#182).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

T = TypeVar("T")


class TransientError(Exception):
    """A wake failure worth retrying — a transient blip, not a permanent fault.

    The wake mechanism raises this (or a subclass) to opt a failure into
    :func:`with_retry`. Every other exception is treated as permanent and is not
    retried.
    """


class RetryExhausted(Exception):
    """Raised by :func:`with_retry` when the attempt bound is reached.

    The last :class:`TransientError` is chained as ``__cause__``.
    """

    def __init__(self, attempts: int) -> None:
        super().__init__(f"operation still failing after {attempts} attempt(s); giving up")
        self.attempts = attempts


class AgentLocks:
    """Per-agent mutual exclusion via lock-striping.

    One :class:`threading.Lock` per agent key, created on first use. The key is
    the agent's :attr:`~basecradle_router.models.Agent.harness_key` — its harness
    instance's identity — so holding it guarantees no other thread is mid-wake for
    that agent, *regardless of which input source the wake came from*. Threads
    working *different* agents never contend. The lock is non-reentrant: a thread
    must not nest :meth:`guard` for the same key (there is no such path — a wake
    holds its agent once).

    The lock registry only ever grows by distinct agent — bounded in practice by
    the agent registry, so it is left unbounded by design.
    """

    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _lock_for(self, key: str) -> threading.Lock:
        # The registry lock is held only for the dict lookup/insert, never across
        # the agent lock's hold time, so distinct agents never serialize here.
        with self._registry_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def acquire(self, key: str, *, blocking: bool = True) -> bool:
        """Acquire ``key``'s lock. Returns ``True`` if acquired.

        With ``blocking=False`` returns ``False`` immediately instead of waiting
        when the lock is held — the caller then has *not* acquired it and must
        not :meth:`release` it.
        """
        return self._lock_for(key).acquire(blocking=blocking)

    def release(self, key: str) -> None:
        """Release ``key``'s lock. Must pair with a successful :meth:`acquire`."""
        self._lock_for(key).release()

    @contextmanager
    def guard(self, key: str) -> Iterator[None]:
        """Hold ``key``'s lock for the duration of the block (blocking acquire).

        A second wake for the same agent serializes behind the first; the lock is
        released on exit, including when the guarded block raises.
        """
        self.acquire(key)
        try:
            yield
        finally:
            self.release(key)


def with_retry(
    operation: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, int, TransientError], None] | None = None,
) -> T:
    """Run ``operation``, retrying on :class:`TransientError` up to ``attempts``.

    Returns the operation's result on the first success. Sleeps with exponential
    backoff (``base_delay * 2 ** (attempt - 1)``) between tries — ``sleep`` is
    injectable so tests stay deterministic. Raises :class:`RetryExhausted` (the
    last :class:`TransientError` chained) once the bound is reached; any
    non-:class:`TransientError` propagates immediately without a retry.

    ``on_retry(failed_attempt, attempts, error)`` is called for each failure that
    a retry will follow — so a caller can make the backoff *visible* instead of
    silent (basecradle-router#170), which is why it is a hook rather than a log
    call here: only the caller knows what the operation was for (which agent, which
    delivery), and this primitive stays generic. It is not called for the final,
    exhausting failure — :class:`RetryExhausted` is that signal, and the caller
    logs it once.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except TransientError as exc:
            if attempt >= attempts:
                raise RetryExhausted(attempts) from exc
            if on_retry is not None:
                on_retry(attempt, attempts, exc)
            sleep(base_delay * 2 ** (attempt - 1))

    # Unreachable: the loop either returns or raises on every path.
    raise AssertionError("with_retry exhausted its loop without returning or raising")

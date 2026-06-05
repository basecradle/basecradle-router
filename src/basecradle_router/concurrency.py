"""Concurrency primitives guarding the wake step: per-repo locks and retry.

The router may have several wakes in flight at once — one per repo — but **never
two on the same repo**: two headless Claude sessions sharing one repo clone
would clobber each other's working tree. This module provides the two mechanisms
that enforce that, both source-agnostic and independent of any wake's specifics:

- :class:`RepoLocks` — lock-striping by repo key. Different repos get different
  locks (they proceed concurrently); the same repo gets one lock (wakes on it
  serialize). This commits the daemon to a **threaded** execution model — the
  natural fit for wakes that are minutes-long blocking subprocesses — rather
  than asyncio.
- :func:`with_retry` — a bounded, deterministic retry around an operation that
  may fail transiently. Only :class:`TransientError` is retried; anything else
  propagates at once. The clock is injectable so tests never really sleep.

The thread pool that actually runs wakes concurrently is assembled by the core
pipeline (a later step); this module is just the primitives it stands on.
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


class RepoLocks:
    """Per-repo mutual exclusion via lock-striping.

    One :class:`threading.Lock` per repo key, created on first use. Holding a
    repo's lock guarantees no other thread is mid-wake on that repo; threads
    working *different* repos never contend. The lock is non-reentrant: a thread
    must not nest :meth:`guard` for the same repo (there is no such path — a wake
    holds its repo once).

    The per-repo lock registry only ever grows by distinct repo — bounded in
    practice by the agent registry, so it is left unbounded by design.
    """

    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _lock_for(self, repo: str) -> threading.Lock:
        # The registry lock is held only for the dict lookup/insert, never across
        # the repo lock's hold time, so distinct repos never serialize here.
        with self._registry_lock:
            lock = self._locks.get(repo)
            if lock is None:
                lock = threading.Lock()
                self._locks[repo] = lock
            return lock

    def acquire(self, repo: str, *, blocking: bool = True) -> bool:
        """Acquire ``repo``'s lock. Returns ``True`` if acquired.

        With ``blocking=False`` returns ``False`` immediately instead of waiting
        when the lock is held — the caller then has *not* acquired it and must
        not :meth:`release` it.
        """
        return self._lock_for(repo).acquire(blocking=blocking)

    def release(self, repo: str) -> None:
        """Release ``repo``'s lock. Must pair with a successful :meth:`acquire`."""
        self._lock_for(repo).release()

    @contextmanager
    def guard(self, repo: str) -> Iterator[None]:
        """Hold ``repo``'s lock for the duration of the block (blocking acquire).

        A second wake for the same repo serializes behind the first; the lock is
        released on exit, including when the guarded block raises.
        """
        self.acquire(repo)
        try:
            yield
        finally:
            self.release(repo)


def with_retry(
    operation: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``operation``, retrying on :class:`TransientError` up to ``attempts``.

    Returns the operation's result on the first success. Sleeps with exponential
    backoff (``base_delay * 2 ** (attempt - 1)``) between tries — ``sleep`` is
    injectable so tests stay deterministic. Raises :class:`RetryExhausted` (the
    last :class:`TransientError` chained) once the bound is reached; any
    non-:class:`TransientError` propagates immediately without a retry.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except TransientError as exc:
            if attempt >= attempts:
                raise RetryExhausted(attempts) from exc
            sleep(base_delay * 2 ** (attempt - 1))

    # Unreachable: the loop either returns or raises on every path.
    raise AssertionError("with_retry exhausted its loop without returning or raising")

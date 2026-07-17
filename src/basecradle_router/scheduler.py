"""The wake scheduler: many inputs → a bounded, per-agent-fair pool of wakes.

This is the layer that decides *which* pending wake runs *when*, sitting between
the async webhook front-end (:mod:`~basecradle_router.server`) and the synchronous,
blocking slow half of the pipeline (:meth:`~basecradle_router.pipeline.Pipeline.execute`).
It exists because of a specific production failure (basecradle-router#182): the
router used to background each wake with :func:`asyncio.to_thread`, which runs on
the event loop's *implicit default executor* — ``min(32, os.cpu_count() + 4)``
threads, ≈6 on the 2-vCPU home server — and :meth:`Pipeline.execute`'s first act is
a **blocking** ``AgentLocks.guard(harness_key)``. Two design gaps compounded:

1. **A wake parked a pool thread just to wait on its agent's lock.** A deep queue of
   deliveries for one busy agent (a hot two-peer timeline) filled the pool with
   same-agent wakes that all blocked on that one agent's lock, doing nothing.
2. **No cross-agent fairness.** The default executor's queue is global FIFO, so a
   third agent's instant wake — including the NOC's synthetic transport probe —
   waited behind the entire backlog. In the incident an idle agent (@jt) got *zero*
   threads for 30+ minutes while two chatty agents monopolised the pool, and the
   starved probe looked exactly like a dead transport (Fleet Transport incident,
   2026-07-17).

The scheduler closes both gaps by making dispatch explicit rather than an artifact
of ``cpu_count`` and a blocking lock:

- **Per-agent FIFO queues, at most one wake in flight per agent.** The scheduler —
  not a blocking lock — is what serialises an agent's wakes: it never submits a
  second wake for an agent while one is running, so a worker thread is *never* spent
  waiting on an agent lock. This is the same single-ordered-stream-per-agent
  invariant the constitution's unified-identity rule requires (one harness instance
  per agent draining one ordered stream), now enforced by scheduling instead of by
  parking threads. The per-agent :class:`~basecradle_router.concurrency.AgentLocks`
  guard *stays* in :meth:`Pipeline.execute` as the last-line correctness net for that
  invariant — but because the scheduler guarantees single-in-flight, that guard is now
  always uncontended (it never blocks a thread).
- **An explicit, deliberately-sized pool.** ``lanes`` is the stated ceiling on
  *total* concurrent wakes across all agents — a
  :class:`~concurrent.futures.ThreadPoolExecutor` sized once, from config
  (``BASECRADLE_ROUTER_WAKE_LANES``), not from ``cpu_count``. Because a busy agent
  holds exactly one lane, ``lanes`` binds only when that many *distinct* agents are
  mid-wake at once; it is a capacity ceiling, not a per-agent limit.
- **Round-robin fairness across agents with pending work.** Agents ready to run are
  dispatched from a FIFO of agent keys; an agent that finishes and still has queued
  work re-enters at the *back*, behind any other agent that was waiting. So one hot
  timeline can never jump its own next wake ahead of a peer's first wake — the
  starvation from the incident cannot recur: an idle agent's wake waits behind at most
  the ``lanes`` currently-running wakes (one per busy agent), never behind a busy
  agent's whole backlog.

Thread-safety: all scheduling state is guarded by one :class:`threading.Condition`
held only for the microseconds of a dispatch decision — never across a wake, which
runs lock-free on a worker thread. The condition doubles as the drain signal
(:meth:`wait_idle`), so shutdown can block until every queued and in-flight wake has
drained. The scheduler is framework-free (pure :mod:`threading`), so it is driven
directly in offline tests without asyncio.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from basecradle_router.models import Agent, Event
from basecradle_router.pipeline import PipelineResult

logger = logging.getLogger("basecradle_router.scheduler")

#: The slow half the scheduler runs on a worker thread: the pipeline's
#: ``execute(agent, event, result)`` — never raises (it records failures itself),
#: so the scheduler treats it as a black box and only guards against a defect.
WakeRunner = Callable[[Agent, Event, PipelineResult], None]


@dataclass(frozen=True, slots=True)
class _Job:
    """One queued wake — the tuple :meth:`WakeScheduler.submit` was handed."""

    agent: Agent
    event: Event
    result: PipelineResult


class WakeScheduler:
    """Dispatch wakes across ``lanes`` threads, one in flight per agent, fairly.

    Construct with the ``run`` callable (the pipeline's ``execute``) and a lane
    count, then :meth:`submit` a wake per accepted webhook. The scheduler owns the
    thread pool and the per-agent queues; a wake runs off any caller's thread.
    :meth:`wait_idle` blocks until every queued and in-flight wake has finished (the
    server's drain), and :meth:`shutdown` tears the pool down on stop.

    The lock registry (per-agent queues) grows only by distinct agent — bounded in
    practice by the agent registry, like :class:`~basecradle_router.concurrency.AgentLocks`
    — and each agent's entry is dropped the moment its queue empties, so a burst
    leaves no residue.
    """

    def __init__(self, run: WakeRunner, *, lanes: int) -> None:
        if lanes < 1:
            raise ValueError(f"lanes must be >= 1, got {lanes}")
        self._run = run
        self._lanes = lanes
        # One lock for all scheduling state; it is also the drain condition's lock, so
        # a state change and the idle check can never interleave. Held only for a
        # dispatch decision (microseconds), never across a wake.
        self._cv = threading.Condition()
        # Per-agent FIFO of pending jobs; an agent key is present only while it has
        # queued work (removed when its queue drains).
        self._queues: dict[str, deque[_Job]] = {}
        # Agents with pending work that are NOT in flight, in the order they became
        # ready — the round-robin queue. The set mirrors the deque for O(1) membership,
        # so an agent is never enqueued twice.
        self._ready: deque[str] = deque()
        self._ready_set: set[str] = set()
        # Agents with a wake currently running — the single-in-flight guarantee: a key
        # here is never also in `_ready`, so its next wake is not dispatched until this
        # one finishes.
        self._inflight: set[str] = set()
        # The number of wakes running == the number of lanes in use is exactly
        # `len(self._inflight)`: single-in-flight-per-agent means an agent key occupies
        # one lane while present here, so there is no separate active counter to keep in
        # step (and drift out of step) with this set.
        #
        # Whether every lane is busy with an agent still waiting — logged on the
        # transition only, so saturation is a visible capacity signal without per-wake
        # noise (the invisible starvation of #182 made loud, without crying wolf).
        self._saturated = False
        self._executor = ThreadPoolExecutor(max_workers=lanes, thread_name_prefix="wake")

    @property
    def lanes(self) -> int:
        """The configured concurrent-wake ceiling — read by the startup banner."""
        return self._lanes

    def submit(self, agent: Agent, event: Event, result: PipelineResult) -> None:
        """Enqueue a wake for ``agent`` and dispatch whatever the free lanes allow.

        Returns at once — the wake runs on a worker thread. The webhook has already
        been fast-acked by the time this is called, so nothing here is on the request
        path. Serialisation is by the agent's
        :attr:`~basecradle_router.models.Agent.harness_key` (its harness instance),
        deliberately not its repo, so every input source for one agent converges on
        one ordered stream.
        """
        key = agent.harness_key
        with self._cv:
            self._queues.setdefault(key, deque()).append(_Job(agent, event, result))
            # Newly schedulable iff it has work, is not already running, and is not
            # already queued to run — otherwise it is left where it is (FIFO order and
            # the single-in-flight rule both preserved).
            if key not in self._inflight and key not in self._ready_set:
                self._ready.append(key)
                self._ready_set.add(key)
            self._pump_locked()

    def _pump_locked(self) -> None:
        """Dispatch ready agents onto free lanes, most-fair first. Caller holds ``_cv``.

        One wake per ready agent per pass: an agent moves from ``_ready`` to
        ``_inflight`` and its head job goes to the pool. Stops when lanes are full or
        no agent is ready. Fairness is the FIFO pop from ``_ready``.
        """
        while len(self._inflight) < self._lanes and self._ready:
            key = self._ready.popleft()
            self._ready_set.discard(key)
            queue = self._queues[key]  # a ready key always has a non-empty queue
            job = queue.popleft()
            if not queue:
                # Tidy the registry the instant an agent's queue drains; it is
                # recreated on the next submit for that agent. Keeps `_queues` bounded
                # by *active* agents, not by every agent ever seen.
                del self._queues[key]
            self._inflight.add(key)  # now occupies a lane until _run_job's finally
            self._executor.submit(self._run_job, key, job)
        self._note_saturation_locked()

    def _run_job(self, key: str, job: _Job) -> None:
        """Run one wake on a worker thread, then release its lane and re-dispatch.

        The wake itself runs *outside* ``_cv`` — the lock is taken only for the
        microseconds of the post-wake bookkeeping. ``run`` (the pipeline's ``execute``)
        never raises; the ``except`` is a last-resort guard so a defect there can
        never leak the lane or wedge the scheduler — the ``finally`` always frees the
        lane, re-queues the agent if it has more work, and re-pumps.
        """
        try:
            self._run(job.agent, job.event, job.result)
        except Exception:  # noqa: BLE001 — pipeline.execute never raises; guard a defect
            logger.exception(
                "wake run raised for agent=%s delivery=%s (lane freed regardless)",
                key,
                job.event.delivery_id,
            )
        finally:
            with self._cv:
                self._inflight.discard(key)  # frees the lane
                # More work for this agent? Re-enter at the BACK — behind any peer that
                # was already waiting — so a busy agent takes turns and never starves
                # an idle one (the fairness guarantee, and the #182 fix).
                if self._queues.get(key) and key not in self._ready_set:
                    self._ready.append(key)
                    self._ready_set.add(key)
                self._pump_locked()
                # Wake any drain() waiter: this completion may have made us idle.
                self._cv.notify_all()

    def _note_saturation_locked(self) -> None:
        """Log the saturated↔not-saturated transition. Caller holds ``_cv``.

        Saturated = every lane busy *and* at least one agent still waiting for one.
        Logged only on the edge, so an operator sees when capacity is the bottleneck
        (raise ``BASECRADLE_ROUTER_WAKE_LANES``) without a line per wake. This is the
        capacity signal the incident lacked — but it is INFO, not an error: with the
        per-agent fairness rule, an idle agent is no longer *starved* by saturation,
        only briefly delayed behind the currently-running wakes.
        """
        saturated = len(self._inflight) >= self._lanes and bool(self._ready)
        if saturated and not self._saturated:
            logger.info(
                "wake scheduler saturated: all %d lane(s) busy, %d agent(s) waiting; "
                "raise BASECRADLE_ROUTER_WAKE_LANES if this persists",
                self._lanes,
                len(self._ready),
            )
        elif not saturated and self._saturated:
            logger.info("wake scheduler no longer saturated: a lane freed")
        self._saturated = saturated

    def _idle_locked(self) -> bool:
        """True when nothing is running and nothing is queued. Caller holds ``_cv``."""
        # No lane in use and nothing ready implies every `_queues` entry is drained: a
        # pending agent is always either in `_ready` or `_inflight`, so neither can hide
        # queued work once both are empty.
        return not self._inflight and not self._ready

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until every queued and in-flight wake has finished; the server's drain.

        Returns ``True`` when idle, ``False`` if ``timeout`` elapsed first. Does *not*
        shut the pool down — it may be called repeatedly (each webhook test drains,
        then submits again), and on shutdown :meth:`shutdown` closes the pool after a
        final drain.
        """
        with self._cv:
            return self._cv.wait_for(self._idle_locked, timeout=timeout)

    def shutdown(self, *, wait: bool = True) -> None:
        """Tear the thread pool down — called once, after the final drain, on stop."""
        self._executor.shutdown(wait=wait)

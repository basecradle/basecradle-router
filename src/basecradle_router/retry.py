"""Scheduled retries: a wake that failed for a temporary reason is tried again later (#527).

**The incident.** On 2026-11-02 two wakes died on a vendor's 429 and the reply never came.
The router did retry them, but its three attempts all fell inside about ten seconds, and a
rate limit or a short outage outlasts that, so every attempt met the same refusal and the
User who wrote saw nothing.

**The rule, a job queue's** (``@origin``, 2026-11-20). A wake that gives up on an outage
exits :data:`~basecradle_router.wake.TEMPFAIL_EXIT`, and its delivery is tried again at
about 1, 5 and 30 minutes (:data:`DEFAULT_DELAYS`). Each delivery has its own schedule, as a
job queue gives each failed job its own retries, and nothing carries from one delivery to
the next. A schedule ends when its delivery's work is done or its attempts are spent. Any
other non-zero exit is the crash it always was: three quick tries, then ``outcome=failed``.

**A success cancels the retries that are left.** The retry that succeeds schedules nothing
more. A retry whose work a newer delivery's successful wake read is cancelled the moment that
wake succeeds, by the one rule coverage already holds
(:mod:`basecradle_router.coalesce`): the waiting delivery's event happened before the
successful session started, so that session read it (:meth:`RetrySchedule.cancel_covered`).
The cancellation is written to the file, so it holds across a restart, which the in-memory
coverage would not; a retry that slips past it while queued still collapses at dispatch.

**A retry is a delivery, dispatched again.** When its time comes it is submitted to the
agent's queue like any delivery and passes every gate a delivery passes, in the same order. A
github issue closed in the wait is dropped by its route's recheck. A wake that an operator
stops ends its schedule, as a stop ends its delivery. A duplicate of a delivery whose retry
is waiting (two Apps sending one GUID) collapses into that schedule rather than waking into
the same outage. Under the NOC's wake lock (a converge, or an operator's wake hold) a retry
never runs and is never dropped: it waits as long as its own step's wait, then tries again.

**What this module holds is only what a queue must remember:** which deliveries are waiting,
which attempt each is on, and when it is due. The pipeline asks it which attempt a dispatch
is (:meth:`RetrySchedule.attempt`) and tells it how the dispatch ended; the lines about it
are the pipeline's.

**A scheduled retry survives a restart.** Every change is written to
:data:`DEFAULT_RETRY_FILE`, and a daemon that starts re-arms what the last one left, a retry
already due firing at once. A NOC deploy restarts the daemon on every merge to ``main``, so a
30-minute wait spans one often; a schedule kept only in memory would lose exactly the
deliveries this exists for. The file is replaced whole, by the evidence store's writer
(:func:`~basecradle_router.statefile.atomic_write`), and its one by-product, the temp file
of a write killed before its swap, is swept at construction, never later. An entry whose
agent the registry no longer resolves is dropped with one line. A file that cannot be
written degrades to memory with one warning, so a broken state dir costs a restart's
retries, never a wake.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from basecradle_router import statefile
from basecradle_router.logfmt import log_fields
from basecradle_router.models import Agent, Event, EventKind, IssueRef, Recipient
from basecradle_router.wake import WakeAttempt

logger = logging.getLogger("basecradle_router.retry")

#: The waits after the first wake, then after each retry, in seconds: about 1, 5 and 30
#: minutes. A delivery gets one attempt more than there are waits.
DEFAULT_DELAYS: tuple[float, ...] = (60.0, 300.0, 1800.0)

#: Where the waiting deliveries are kept across a restart. ``/var/lib`` for the reason the
#: evidence document is there: the unit's ``StateDirectory=`` makes it, owned by the daemon.
DEFAULT_RETRY_FILE = "/var/lib/basecradle-router/wake-retries.json"

#: Bump only on an incompatible reshape; a version this reader does not know is read as
#: no file, so a downgrade drops the waiting retries rather than misreading them.
RETRY_VERSION = 1

#: The temp name every write of the file carries in its directory, swept at construction.
_TEMP_PREFIX = ".wake-retries-"

#: Submit a delivery to its agent's queue; the server binds the scheduler's own submit.
Submit = Callable[[Agent, Event], None]
#: Run ``fire`` after ``delay`` seconds, and return what cancels it. A test fires by hand.
Arm = Callable[[float, Callable[[], None]], Callable[[], None]]
#: The agent a restored delivery is for now, or ``None`` when the registry has no such agent.
Resolve = Callable[[Event], Agent | None]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _instant(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _thread_timer(delay: float, fire: Callable[[], None]) -> Callable[[], None]:
    """The daemon's :data:`Arm`: a daemon thread, so a waiting retry never holds up exit."""
    timer = threading.Timer(delay, fire)
    timer.daemon = True
    timer.start()
    return timer.cancel


@dataclass(frozen=True, slots=True)
class Scheduled:
    """A retry the schedule has just armed: the attempt it will be, and when."""

    attempt: WakeAttempt
    delay: float
    retry_at: datetime


@dataclass(slots=True)
class _Waiting:
    agent: Agent
    event: Event
    #: The attempt this retry will be when it runs.
    number: int
    retry_at: datetime
    #: Set when its time came and it was submitted, until the dispatch says how it ended.
    due: bool = False
    cancel: Callable[[], None] | None = None
    #: The scheduling dispatch's context, which the retry is submitted in, so a line about it
    #: renders as that dispatch's did. Never persisted: a restored retry renders as itself.
    context: contextvars.Context | None = None


class RetrySchedule:
    """The deliveries waiting for a retry — see the module docstring.

    Thread-safe: consulted and changed from the wake threads and fired from timer threads,
    under one lock held for a dictionary operation and the file write, never across a wake.
    Keyed by the agent and :attr:`~basecradle_router.models.Event.dedup_key`, so a delivery
    is one schedule however many agents a platform event reaches.
    """

    def __init__(
        self,
        path: str | None = None,
        *,
        delays: tuple[float, ...] = DEFAULT_DELAYS,
        resolve: Resolve | None = None,
        arm: Arm = _thread_timer,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        if any(delay < 0 for delay in delays):
            raise ValueError(f"a retry delay is never negative, got {delays}")
        self.path = path
        self._delays = delays
        self._arm = arm
        self._now = now
        self._lock = threading.Lock()
        self._waiting: dict[tuple[str, str], _Waiting] = {}
        self._submit: Submit | None = None
        self._write_failed = False
        if path:
            removed = statefile.remove_orphaned_temps(path, prefix=_TEMP_PREFIX)
            if removed:
                logger.warning(
                    "event=wake_retries_orphaned_temps_removed %s",
                    log_fields(dir=statefile.directory_of(path), removed=",".join(removed)),
                )
            self._restore(path, resolve)

    @property
    def attempts(self) -> int:
        """How many attempts a delivery gets: the first wake, then one per wait."""
        return len(self._delays) + 1

    def pending(self, agent_key: str, event: Event) -> bool:
        """Whether ``event``'s delivery is waiting for a retry that is not yet due."""
        with self._lock:
            waiting = self._waiting.get((agent_key, event.dedup_key))
            return waiting is not None and not waiting.due

    def cancel_covered(self, agent_key: str, event: Event, started_at: datetime) -> list[Event]:
        """A wake of ``event``'s stream succeeded from ``started_at``: end what it covered.

        The coverage rule, applied to what is waiting: a waiting delivery of the same agent's
        same stream whose event happened before the successful session started was read by
        it, so its work is done. A retry already due is left to its dispatch, which collapses
        it the same way. Returns the deliveries cancelled, for their lines.
        """
        if event.occurred_at is None:
            return []
        with self._lock:
            covered = [
                key
                for key, waiting in self._waiting.items()
                if key[0] == agent_key
                and not waiting.due
                and waiting.event.stream_key == event.stream_key
                and waiting.event.occurred_at is not None
                and waiting.event.occurred_at < started_at
            ]
            cancelled = []
            for key in covered:
                waiting = self._waiting.pop(key)
                if waiting.cancel is not None:
                    waiting.cancel()
                cancelled.append(waiting.event)
            if cancelled:
                self._flush_locked()
        return cancelled

    def attempt(self, agent_key: str, event: Event) -> WakeAttempt:
        """Which attempt this dispatch of ``event`` is: its retry's number if one is due."""
        with self._lock:
            waiting = self._waiting.get((agent_key, event.dedup_key))
            number = waiting.number if waiting is not None and waiting.due else 1
        return WakeAttempt(number, self.attempts)

    def schedule(self, agent: Agent, event: Event, failed: WakeAttempt) -> Scheduled | None:
        """``failed`` ended temporarily: arm the next attempt, or ``None`` when none is left."""
        number = failed.number + 1
        if number > self.attempts:
            return None
        return self._arm_next(agent, event, number, self._delays[failed.number - 1])

    def wait(self, agent: Agent, event: Event, attempt: WakeAttempt) -> Scheduled:
        """``attempt`` was due but could not run yet: try it again after its own step's wait.

        Its own step's, so a long wake hold costs a waiting delivery one line per wait of
        its schedule, never one a minute.
        """
        return self._arm_next(agent, event, attempt.number, self._delays[attempt.number - 2])

    def settle(self, agent_key: str, event: Event) -> None:
        """The due retry of ``event`` has been dispatched and ended: its schedule is over.

        Called on every way out of a retry's dispatch. A retry that ended temporarily again,
        or waited on a lock, was re-armed in the meantime and is no longer due, so it is
        left alone; anything else (a success, a collapse into a wake that covered it, a
        drop, a refusal, a crash, a stop, the last attempt spent) ends the schedule here.
        """
        with self._lock:
            key = (agent_key, event.dedup_key)
            waiting = self._waiting.get(key)
            if waiting is None or not waiting.due:
                return
            del self._waiting[key]
            self._flush_locked()

    def start(self, submit: Submit) -> None:
        """Begin firing retries into ``submit``, the restored ones included."""
        with self._lock:
            self._submit = submit
            for key, waiting in self._waiting.items():
                if waiting.cancel is None and not waiting.due:
                    self._arm_locked(key, waiting)

    def stop(self) -> None:
        """Fire nothing more. What is waiting stays in the file for the next start."""
        with self._lock:
            self._submit = None
            for waiting in self._waiting.values():
                if waiting.cancel is not None:
                    waiting.cancel()
                    waiting.cancel = None

    def waiting(self) -> list[tuple[str, str, int, datetime]]:
        """``(agent, delivery, attempt, retry_at)`` for each waiting retry — for tests."""
        with self._lock:
            return sorted(
                (agent, w.event.delivery_id, w.number, w.retry_at)
                for (agent, _), w in self._waiting.items()
            )

    def _arm_next(self, agent: Agent, event: Event, number: int, delay: float) -> Scheduled:
        key = (agent.harness_key, event.dedup_key)
        retry_at = self._now() + timedelta(seconds=delay)
        with self._lock:
            known = self._waiting.get(key)
            if known is not None and known.cancel is not None:
                known.cancel()
            waiting = _Waiting(agent, event, number, retry_at, context=contextvars.copy_context())
            self._waiting[key] = waiting
            if self._submit is not None:
                self._arm_locked(key, waiting)
            self._flush_locked()
        return Scheduled(WakeAttempt(number, self.attempts), delay, retry_at)

    def _arm_locked(self, key: tuple[str, str], waiting: _Waiting) -> None:
        delay = max(0.0, (waiting.retry_at - self._now()).total_seconds())
        waiting.cancel = self._arm(delay, lambda: self._fire(key, waiting))

    def _fire(self, key: tuple[str, str], waiting: _Waiting) -> None:
        with self._lock:
            # Only the arming this timer belongs to may fire: a retry re-armed since, or
            # finished, or a schedule stopped, leaves this one with nothing to do.
            if self._waiting.get(key) is not waiting or self._submit is None:
                return
            waiting.cancel = None
            waiting.due = True
            submit = self._submit
        context = waiting.context or contextvars.Context()
        try:
            context.run(submit, waiting.agent, waiting.event)
        except Exception:  # noqa: BLE001 — a timer thread must never die on a defect
            with self._lock:
                # Never dispatched, so never due: it stays in the file, and the next start
                # (this daemon's, or the next one's) arms it again.
                if self._waiting.get(key) is waiting:
                    waiting.due = False
            logger.exception(
                "event=wake_retry_unsubmitted %s",
                log_fields(
                    delivery=waiting.event.delivery_id,
                    agent=key[0],
                    source=waiting.event.source,
                ),
            )

    def _restore(self, path: str, resolve: Resolve | None) -> None:
        entries = _load(path)
        for raw in entries:
            try:
                event = _event_from_json(raw["event"])
                agent_key = str(raw["agent"])
                number = int(raw["attempt"])
                retry_at = datetime.fromisoformat(str(raw["retry_at"]).replace("Z", "+00:00"))
                if not 2 <= number <= self.attempts or retry_at.tzinfo is None:
                    raise ValueError(f"attempt {number} or retry_at {retry_at} is not one")
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "event=wake_retry_unrestored %s",
                    log_fields(path=path, reason="unreadable", error=exc),
                )
                continue
            agent = resolve(event) if resolve is not None else None
            if agent is None or agent.harness_key != agent_key:
                # The registry no longer wakes this agent for this delivery: there is no
                # one to retry it for, and guessing at another would wake the wrong agent.
                logger.warning(
                    "event=wake_retry_unrestored %s",
                    log_fields(
                        delivery=event.delivery_id,
                        agent=agent_key,
                        source=event.source,
                        reason="unresolved",
                    ),
                )
                continue
            self._waiting[(agent_key, event.dedup_key)] = _Waiting(agent, event, number, retry_at)
        if self._waiting:
            logger.info(
                "event=wake_retries_restored %s", log_fields(path=path, count=len(self._waiting))
            )
        if len(entries) != len(self._waiting):
            # What could not be restored is gone from the file too, never read again.
            with self._lock:
                self._flush_locked()

    def _flush_locked(self) -> None:
        """Write what is waiting, whole. Caller holds ``_lock``. Never raises."""
        if self.path is None:
            return
        document = {
            "version": RETRY_VERSION,
            "retries": [
                {
                    "agent": agent,
                    "attempt": waiting.number,
                    "retry_at": _instant(waiting.retry_at),
                    "event": _event_json(waiting.event),
                }
                for (agent, _), waiting in sorted(self._waiting.items())
            ],
        }
        try:
            # 0600: it names timelines and triggers, and nothing but this daemon reads it.
            statefile.atomic_write(
                self.path,
                json.dumps(document, indent=2, sort_keys=True),
                prefix=_TEMP_PREFIX,
                mode=0o600,
            )
        except OSError as exc:
            if not self._write_failed:
                self._write_failed = True
                logger.warning(
                    "event=wake_retries_write_failed %s "
                    "(waiting retries are now in memory only, and a restart loses them)",
                    log_fields(path=self.path, detail=exc),
                )


def _load(path: str) -> list[dict]:
    """The waiting entries in ``path``, or none. Never raises."""
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logger.warning(
            "event=wake_retry_unrestored %s", log_fields(path=path, reason="unreadable", error=exc)
        )
        return []
    if not isinstance(document, dict) or document.get("version") != RETRY_VERSION:
        logger.warning(
            "event=wake_retry_unrestored %s",
            log_fields(path=path, reason="unknown_version"),
        )
        return []
    retries = document.get("retries")
    if not isinstance(retries, list):
        return []
    return [entry for entry in retries if isinstance(entry, dict)]


def _event_json(event: Event) -> dict:
    origin = event.origin
    return {
        "source": event.source,
        "kind": event.kind.value,
        "recipient": {"by": event.recipient.by, "value": event.recipient.value},
        "wake_arg": event.wake_arg,
        "delivery_id": event.delivery_id,
        "origin": None
        if origin is None
        else {
            "repo": origin.repo,
            "number": origin.number,
            "url": origin.url,
            "title": origin.title,
        },
        "occurred_at": None if event.occurred_at is None else _instant(event.occurred_at),
        "event_type": event.event_type,
    }


def _event_from_json(raw: object) -> Event:
    if not isinstance(raw, dict):
        raise ValueError("an event is an object")
    origin = raw.get("origin")
    occurred_at = raw.get("occurred_at")
    recipient = raw["recipient"]
    if not isinstance(recipient, dict):
        raise ValueError("a recipient is an object")
    return Event(
        source=raw["source"],
        kind=EventKind(raw["kind"]),
        recipient=Recipient(by=recipient["by"], value=recipient["value"]),
        wake_arg=raw["wake_arg"],
        delivery_id=raw["delivery_id"],
        origin=None
        if origin is None
        else IssueRef(
            repo=origin["repo"], number=origin["number"], url=origin["url"], title=origin["title"]
        ),
        occurred_at=None
        if occurred_at is None
        else datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00")),
        event_type=raw.get("event_type"),
    )

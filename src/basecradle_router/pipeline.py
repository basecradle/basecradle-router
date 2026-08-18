"""The source-agnostic core pipeline: the stages, in order, tied together.

One inbound webhook flows through fixed stages — **verify → normalize → resolve
→ lock → wake** — and the core knows nothing about any specific source: it asks
the registry for the route, and the route owns the GitHub-specific parts. Adding
a source never touches this file.

The stages split at the wake into a fast :meth:`Pipeline.accept` half
(route → verify → normalize → resolve — no subprocess, milliseconds) and a slow
:meth:`Pipeline.execute` half (lock → wake — a minutes-long ``claude``
subprocess). This is what lets the server *fast-ack*: answer the webhook from the
accept half and run the wake in the background. :meth:`Pipeline.handle` is simply
``accept`` then ``execute`` — the synchronous whole, unchanged for callers that
want it (the offline tests).

Every stage records a structured :class:`StageRecord` (the router's own log; the
*agent* reports separately by commenting on the issue). A non-handoff event
short-circuits cleanly as ``IGNORED``; a bad signature or malformed payload is
``REJECTED``; a stage that errors is ``FAILED`` and logged — the daemon never
crashes on one bad event.

**Every stage line is key=value, and carries the delivery id from the moment the
core knows it** (basecradle-router#170). Before that the trailing detail was one
bare positional value — ``stage=wake outcome=ok exit 0`` — which named neither the
agent nor the delivery, so two concurrent wakes interleaved ambiguously in Live
Tail and no line could be joined to any other. Now the delivery id (known from
``normalize`` onward) and the agent's slug (from ``resolve`` onward) ride as named
keys on every line that follows, so ``delivery=<id>`` selects one delivery's whole
trip through the router — and, because the wake child is handed the same id in its
environment (:data:`~basecradle_router.wake.DELIVERY_ID_ENV`), through the agent's
own journal too.

**And every line about a wake names the source that asked for it** (``source=``,
basecradle-router#222, a founder order). The router's own probe traverses this exact
path, so its wakes land on the same ``stage=wake`` lines a real handoff does — and
those lines are what a log-metric extractor lifts a per-wake metric from. Extraction
lifts only *low-cardinality* keys, so the probe's one previous marker — a ``probe-``
prefix typed into the high-cardinality ``delivery=`` id — was dropped on the floor,
and every duration chart and wake-rate alert built on the resulting metric silently
mixed the fleet's own test traffic with its real work. ``source=`` is the route the
delivery arrived on, said as a label: ``source=probe`` is the fleet probing itself,
anything else is traffic something outside it actually sent. Three properties are
deliberate:

- **It is the router's own existing vocabulary, carried through rather than invented.**
  The value is :attr:`~basecradle_router.models.Event.source` — the same string the
  fast half's stage lines already log, the same one the routes layer's
  ``event=delivery_decision`` line already logs, and the same one the evidence store
  records as ``route`` beside every outcome. The slow half was the only half that had
  dropped it; this restores it, so one key spans a delivery's whole trip and no
  surface can drift from another.
- **The kind marker is a field, never a prefix inside another field.** A delivery id
  identifies one delivery; typing a class into it makes a high-cardinality join key
  carry a low-cardinality fact that extraction cannot see, which is how the mixing
  went unnoticed. Whether a wake was manufactured is answered by ``source``, and by
  the route's own :attr:`~basecradle_router.routes.base.Route.synthetic` declaration
  behind it — never by reading characters off an id.
- **It is on every line of the slow half, not only the happy one.** A refused probe
  and a failed probe pollute a wake-failure count exactly as a successful one pollutes
  a duration chart, so the key rides in :func:`_who` alongside ``agent`` and
  ``delivery`` — which means a gate added later carries it by construction rather
  than by remembering to.

The key is deliberately *not* ``origin``: an :class:`~basecradle_router.models.Event`
already has one, meaning something else entirely (the issue the agent reports back on).

**A wake is bracketed for the human watching it** — ``event=wake_start`` before the
first attempt, ``event=wake_end`` when it resolves (basecradle-router#228, a founder
decision). The router used to be *silent at launch*: its first word about a wake was
the ``stage=wake`` line minutes later, at completion, so a Live Tail could not
distinguish an agent hard at work from an agent that was never woken at all — the
exact green-while-absent shape this repo exists to instrument against. The pair is
the human lifecycle surface and the ``stage=`` records are the pipeline machinery
serving :class:`StageRecord` and the status API; both stay, the same two-surface split
Rails draws between its ``Started``/``Completed`` request lines and its instrumentation
events. See :meth:`Pipeline._wake` for the two properties that make the bracket
trustworthy — it always closes, and both halves carry one field prefix.

**Colour is presentation the journal adds, and stops there.** The fleet ANSI palette
(:mod:`basecradle_router.logfmt`, @origin 2026-08-17) paints the verdict tokens as the
line is handed to the logger — green for ``outcome=ok``, red for ``outcome=failed`` —
around the *whole* ``key=value`` token, so every substring search still matches. It
never enters a :class:`StageRecord` or a status payload: those carry the data, the
journal line carries the data *and* its colour, and the two can never disagree about a
value.

**The pipeline ends at the wake — the router never merges.** Auto-merge of a
captain's own green PR (constitution → Earned Autonomy) is performed by **GitHub
native auto-merge**: during its wake the agent opens its PR and runs
``gh pr merge --auto --squash`` under its *own* bot identity, so the platform
merges the instant required checks pass. The router holds no GitHub credential by
design (the wake-runner sources each agent's ``agent.env`` only after the
privilege drop; the crown-jewels box carries no standing token), so a router-side
merger would have meant concentrating a merge-capable token there — declined in
favour of letting the platform do, for free and under the captain's identity,
exactly what a green-only merge policy would have. See issue #38 for the decision
and rationale.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from basecradle_router.breaker import WakeRateBreaker
from basecradle_router.concurrency import (
    AgentLocks,
    RetryExhausted,
    TransientError,
    with_retry,
)
from basecradle_router.config import Config, ConfigError
from basecradle_router.dedup import DUPLICATE_DELIVERY, DeliveryDeduper
from basecradle_router.evidence import EvidenceStore
from basecradle_router.logfmt import log_fields, paint
from basecradle_router.models import Agent, Event
from basecradle_router.resolve import resolve_agent
from basecradle_router.routes import (
    InboundRequest,
    PayloadError,
    RouteRegistry,
    SignatureError,
    UnknownRouteError,
    UntrustedSenderError,
)
from basecradle_router.wake import WakeError, Waker, WakeResult
from basecradle_router.wakelock import WakeLockGuard

logger = logging.getLogger("basecradle_router.pipeline")


def _seconds(elapsed: float) -> str:
    """A wall-clock duration as a log value: ``23.1s``."""
    return f"{elapsed:.1f}s"


def _who(agent: Agent, event: Event) -> dict[str, object]:
    """The keys every line of the slow half carries: which delivery, which agent, which source.

    ``source`` is the route the delivery arrived on
    (:attr:`~basecradle_router.models.Event.source`) — the low-cardinality label a
    log-metric extractor can lift, and the one that says whether a wake was the fleet's
    own probe (``source=probe``) or real traffic (basecradle-router#222). The fast half
    has carried it all along; it rides *here* so the slow half carries it too, beside
    the identity keys rather than spelled onto each wake line, which means every line
    the slow half emits — the gates' refusals and the wake's failures as much as its
    successes — carries it, and a gate added later cannot forget to. See the module
    docstring for why the key is ``source`` and not ``origin``.

    ``agent`` is the OS-user slug (:attr:`~basecradle_router.models.Agent.harness_key`),
    deliberately not the registry key: it is the same slug the wake's *own* journal
    entries are tagged with (``basecradle-wake-<slug>``), so it is what makes the
    router's half of a wake and the agent's half joinable.

    **Ordered for the human reading the line, not the query reading the key**
    (basecradle-router#228, a founder decision): ``delivery`` first, then ``agent``,
    then ``source``. The order was labels-first before, on the reasoning that a query
    groups by the closed-set keys and joins on the unbounded id — but a query addresses
    a key *by name*, and no consumer of these lines has ever cared where in the line a
    key sits. A person tailing the journal does: the delivery id is the thing they copy
    to follow one wake through the router and on into the agent's own journal, so it
    leads, and the identity narrows from there. One order across every line this half
    emits — the wake bookends, ``wake_retry``, and each ``stage=`` line from ``lock``
    on, gates included — because a prefix that reads the same on every line is what
    makes an interleaved Live Tail scannable at all. (The wake-lock guard's and the
    breaker's own ``event=wake_refused`` lines are a different surface: they are handed
    a slug, never an event, so they carry no ``delivery``/``source`` to order.)
    """
    return {
        "delivery": event.delivery_id,
        "agent": agent.harness_key,
        "source": event.source,
    }


class Stage(Enum):
    """The ordered stages a webhook passes through."""

    ROUTE = "route"
    VERIFY = "verify"
    NORMALIZE = "normalize"
    RESOLVE = "resolve"
    LOCK = "lock"
    DEDUP = "dedup"
    WAKE_LOCK = "wake_lock"
    BREAKER = "breaker"
    WAKE = "wake"


class Outcome(Enum):
    """How a stage resolved."""

    OK = "ok"
    IGNORED = "ignored"  # well-formed but not actionable (not a handoff)
    REJECTED = "rejected"  # bad signature or malformed payload
    FAILED = "failed"  # the stage errored


@dataclass(frozen=True, slots=True)
class StageRecord:
    """One stage's outcome — the unit of the router's structured status log.

    ``detail`` is the rendered ``key=value`` run for this stage (``delivery=…
    agent=nova exit=0 duration=23.1s``) — the same string the log line carries, so
    the in-memory record and the journal can never disagree about what a stage
    said.

    They agree on the *data*; the journal line adds *presentation* the record does
    not carry (basecradle-router#228). ``stage``/``outcome`` ride on the line as their
    own tokens rather than inside ``detail``, and the outcome token is painted with
    the fleet ANSI colour for its verdict at the moment it is handed to the logger —
    so no escape byte can reach this record, the admin/status payload built from it,
    or anything else that reads the router's state rather than watches its journal.
    :mod:`basecradle_router.logfmt` holds the palette and the containment rule.
    """

    stage: Stage
    outcome: Outcome
    detail: str = ""


@dataclass(slots=True)
class PipelineResult:
    """The ordered record of a single webhook's trip through the pipeline."""

    records: list[StageRecord] = field(default_factory=list)
    event: Event | None = None
    agent: Agent | None = None

    @property
    def stages(self) -> list[tuple[Stage, Outcome]]:
        """The (stage, outcome) pairs in order — convenient for assertions."""
        return [(r.stage, r.outcome) for r in self.records]

    @property
    def terminal(self) -> Outcome | None:
        """The outcome of the last stage reached, or ``None`` if none ran."""
        return self.records[-1].outcome if self.records else None


@dataclass(frozen=True, slots=True)
class AcceptResult:
    """The outcome of the fast :meth:`Pipeline.accept` half.

    ``result`` carries the accept-stage records (route → resolve). ``pending`` is
    the ``(agent, event)`` to run through :meth:`Pipeline.execute` when the event
    is an actionable handoff, or ``None`` when it was rejected, ignored, or failed
    to resolve — i.e. when there is no wake to run.
    """

    result: PipelineResult
    pending: tuple[Agent, Event] | None


@dataclass(frozen=True, slots=True)
class Pipeline:
    """Orchestrates the stages for one source's webhooks.

    All collaborators are injected so the whole pipeline is drivable offline:
    ``registry``/``config`` from the route + config layers, a ``waker`` (mocked
    in tests), per-agent ``locks``, a ``sleep`` used by the wake retry (injected
    as a no-op in tests so nothing really waits), and a ``clock`` used to time the
    wake subprocess (injected in tests so a logged duration is deterministic).
    """

    registry: RouteRegistry
    config: Config
    waker: Waker
    locks: AgentLocks = field(default_factory=AgentLocks)
    breaker: WakeRateBreaker = field(default_factory=WakeRateBreaker)
    deduper: DeliveryDeduper = field(default_factory=DeliveryDeduper)
    wake_lock: WakeLockGuard = field(default_factory=WakeLockGuard)
    # Durable proof of what this router has actually done, for the NOC's
    # claims-vs-evidence ledger (basecradle/basecradle#460). Defaults to an
    # in-memory store so a bare Pipeline stays constructible offline and no test
    # ever writes to the box's state dir; the app factory injects the real path.
    evidence: EvidenceStore = field(default_factory=lambda: EvidenceStore(None))
    wake_attempts: int = 3
    sleep: Callable[[float], None] = time.sleep
    # Monotonic by design: the wake's duration must not jump if the wall clock is
    # stepped mid-wake (a wake runs for minutes; ntp can and does correct in that
    # window), and it is a *duration*, never a timestamp.
    clock: Callable[[], float] = time.monotonic

    def handle(self, source: str, request: InboundRequest) -> PipelineResult:
        """Run ``request`` for ``source`` synchronously, end to end; never raises.

        The synchronous whole — :meth:`accept` then :meth:`execute` — used by
        callers that want the full trip in one call (the offline tests). The
        server uses the two halves separately so it can fast-ack. Returns a
        :class:`PipelineResult` whose ``records`` are the ordered stage outcomes;
        expected conditions resolve to records and unexpected errors are caught
        and recorded ``FAILED`` so one bad event cannot take down the daemon.
        """
        accepted = self.accept(source, request)
        if accepted.pending is not None:
            agent, event = accepted.pending
            self.execute(agent, event, accepted.result)
        return accepted.result

    def accept(self, source: str, request: InboundRequest) -> AcceptResult:
        """The fast half: route → verify → normalize → resolve; never raises.

        Returns an :class:`AcceptResult` whose ``pending`` is the ``(agent,
        event)`` to wake when the event is an actionable handoff, or ``None``
        otherwise (rejected, ignored, or failed to resolve). No wake runs here —
        this half is cheap enough to answer a webhook within its timeout.
        """
        result = PipelineResult()
        try:
            pending = self._accept(source, request, result)
        except Exception as exc:  # last-resort: a stage bug must never crash the daemon
            self._record(
                result, Stage.ROUTE, Outcome.FAILED, source=source, error=f"unexpected: {exc}"
            )
            pending = None
        return AcceptResult(result=result, pending=pending)

    def execute(self, agent: Agent, event: Event, result: PipelineResult) -> None:
        """The slow half (lock, dedup, wake-lock, breaker, wake), into ``result``; never raises.

        Serialized per agent — by the agent's harness-instance identity, not its
        repo — so an agent never runs two concurrent sessions against its one
        home and memory (and so never two sessions sharing its clone). This is
        where the constitution's unified-identity rule lands in the core: every
        input source for an agent funnels through this one lock into a single
        ordered stream the lone harness instance drains, so a future input module
        can never fan a second parallel session onto the same agent. In production
        the :class:`~basecradle_router.scheduler.WakeScheduler` already guarantees a
        single in-flight wake per agent, so this guard is acquired *uncontended* — it
        is the last-line correctness net for the invariant, not the thing a thread
        blocks on (that blocking was basecradle-router#182's starvation).

        Inside the lock, **delivery dedup** is the first gate
        (basecradle-router#133): a single logical event can arrive as two webhook
        deliveries (e.g. two fleet Apps on one repo, both subscribed) carrying the
        *same* ``X-GitHub-Delivery`` GUID, and without dedup each one wakes the
        agent independently — N subscribed Apps cost N sessions for one event. The
        :class:`~basecradle_router.dedup.DeliveryDeduper` is a short-TTL
        "recently-*woke*" cache keyed on :attr:`Event.dedup_key`: a duplicate the
        router has *already successfully woken for* is recorded ``DEDUP``/``IGNORED``
        and no second wake runs. It is checked *before* the wake-lock and breaker so
        a duplicate consumes neither budget, and the key is marked **only after a
        successful wake** — so the lock (which serialises the duplicate behind the
        original) guarantees the duplicate observes the mark, while a *failed*
        original leaves the duplicate free to retry. See :mod:`basecradle_router.dedup`.

        Then the **NOC wake-lock** is honoured (basecradle-router#120): while the
        NOC converges (upgrades) this agent's
        harness it holds a lock at ``/run/basecradle-noc/wake-locks/<slug>.lock``, and
        the router *refuses* the wake rather than land it on a half-installed venv.
        A held lock is recorded ``WAKE_LOCK``/``IGNORED`` and no wake runs (the
        message stays on the platform's read API and the agent picks it up on its
        next wake once the lock clears); a stale or absent lock falls through to the
        wake (the guard logs the stale case). It is checked *before* the breaker so a
        NOC-quiesced agent never consumes breaker budget. See
        :mod:`basecradle_router.wakelock` for every edge and its fail-direction.

        Then, immediately before the wake, the **wake-rate circuit
        breaker** gates the dispatch (basecradle-router#110): it counts the rate at
        which wakes actually fire for this agent (and this sub-stream) and, over a
        generous sanity cap, *refuses* the wake — a logged, visible decision and a
        loud escalation, the runaway-loop backstop. Because it sits inside the lock
        (which already serializes same-agent wakes one at a time), it measures true
        dispatch rate: a runaway fires back-to-back and trips, a legitimate burst of
        queued deliveries drains one slow wake at a time and never does. A refused
        wake is recorded ``BREAKER``/``IGNORED`` and no wake runs; an admitted one
        falls through to the wake unrecorded, so the happy path is unchanged.

        Runs the minutes-long ``claude`` wake off the request path (in the
        background) after acking. The pipeline ends here: the woken agent opens its
        own PR and enables GitHub native auto-merge, so the router never merges (see
        the module docstring and issue #38).
        """
        # On every line the slow half logs — so two concurrent wakes are trivially
        # separable in Live Tail, which they were not before (#170).
        who = _who(agent, event)
        try:
            with self.locks.guard(agent.harness_key):
                self._record(result, Stage.LOCK, Outcome.OK, **who)
                # Every non-wake outcome below is tagged with the event's own source
                # and provenance, so a probe stopped by a gate is never mistaken for a
                # production wake stopped by it (and vice versa) — see
                # :meth:`~basecradle_router.evidence.EvidenceStore.record_wake_refused`.
                # The ledger's `route` and the journal's `source=` (via `_who`) are the
                # same string read from the same event, so the two surfaces cannot
                # disagree about where a wake came from. The evidence document keeps its
                # own `synthetic` flag beside it because it is read long after the fact,
                # when the registry can no longer be asked which routes were manufactured
                # — a log line is read in place and needs no such snapshot.
                provenance = {"route": event.source, "synthetic": event.synthetic}
                if self.deduper.seen(event.dedup_key):
                    # A duplicate delivery of an event we already woke for — a
                    # deliberate, visible collapse, never a silent drop. Checked
                    # ahead of the wake-lock and breaker so a duplicate consumes
                    # neither's budget.
                    #
                    # Recorded as a *dedup*, never a refusal (basecradle-router#218).
                    # The cache is marked only after a successful wake, so reaching
                    # here means the edge demonstrably worked moments ago — the exact
                    # opposite of what the gates below record, which is a wake that
                    # should have run and did not.
                    self.evidence.record_wake_deduped(agent.harness_key, **provenance)
                    self._record(
                        result, Stage.DEDUP, Outcome.IGNORED, **who, reason=DUPLICATE_DELIVERY
                    )
                    return
                decision = self.wake_lock.check(agent.harness_key)
                if not decision.should_wake:
                    # The NOC holds a converge wake-lock for this agent — a
                    # deliberate, visible drop (the guard already logged it loudly).
                    # A *probe* refused here is the freeze interlock working, and the
                    # honest answer for that cycle is "we never got an answer", not a
                    # dead edge — which is exactly what a refusal (never a failure)
                    # tells the ledger.
                    self.evidence.record_wake_refused(
                        agent.harness_key, decision.detail, **provenance
                    )
                    self._record(
                        result, Stage.WAKE_LOCK, Outcome.IGNORED, **who, reason=decision.detail
                    )
                    return
                outcome = self.breaker.admit(agent.harness_key, event.stream_key)
                if not outcome.admitted:
                    # A trip/refusal is a deliberate, visible drop — recorded like the
                    # route's IGNORED decisions; the breaker already escalated loudly.
                    self.evidence.record_wake_refused(
                        agent.harness_key, outcome.detail, **provenance
                    )
                    self._record(
                        result, Stage.BREAKER, Outcome.IGNORED, **who, reason=outcome.detail
                    )
                    return
                if self._wake(agent, event, result):
                    # Remember the delivery *only* once a wake actually succeeded, so
                    # the duplicate serialised behind us collapses — but a failed wake
                    # leaves the duplicate free to retry the work (see dedup module).
                    self.deduper.mark(event.dedup_key)
        except Exception as exc:  # last-resort: a stage bug must never crash the daemon
            self._record(result, Stage.WAKE, Outcome.FAILED, **who, error=f"unexpected: {exc}")

    def _accept(
        self, source: str, request: InboundRequest, result: PipelineResult
    ) -> tuple[Agent, Event] | None:
        """Drive the accept stages; return ``(agent, event)`` iff there is a wake to run."""
        # Route: find the source's module. Unknown source is a rejection. The core
        # cannot know the delivery id yet — the header carrying it is the *source's*
        # vocabulary, which only the route reads — so the accept stages ahead of
        # normalize are keyed by `source` alone. That is the "from the point it is
        # known" boundary; the route logs its own decision line with the id.
        try:
            route = self.registry.get(source)
        except UnknownRouteError as exc:
            self._record(result, Stage.ROUTE, Outcome.REJECTED, source=source, error=str(exc))
            return None
        self._record(result, Stage.ROUTE, Outcome.OK, source=source)

        # Verify: the security boundary. A missing secret is our misconfiguration,
        # not a bad request — fail the stage rather than reject the caller.
        #
        # This is also the delivery-sink evidence boundary (basecradle/basecradle#460,
        # instance 5). Verification passing is what proves the integration is genuinely
        # armed — that the secret on this box matches the one at the source — so it is
        # `accepted`, whatever the route later decides to do with the payload. It is
        # recorded only for a *known* route, deliberately: the webhook path is
        # unauthenticated, and counting an unknown source would let anyone grow the
        # evidence document one bogus `/webhooks/<anything>` at a time.
        try:
            route.verify(request, self.config.webhook_secret(source))
        except SignatureError as exc:
            self.evidence.record_delivery_rejected(source, str(exc))
            self._record(result, Stage.VERIFY, Outcome.REJECTED, source=source, error=str(exc))
            return None
        except ConfigError as exc:
            self.evidence.record_delivery_rejected(source, f"no secret configured: {exc}")
            self._record(result, Stage.VERIFY, Outcome.FAILED, source=source, error=str(exc))
            return None
        self.evidence.record_delivery_accepted(source)
        self._record(result, Stage.VERIFY, Outcome.OK, source=source)

        # Normalize: payload → Event, or a clean ignore. A malformed payload or a
        # handoff from an untrusted actor is a rejection (logged, no wake).
        try:
            event = route.normalize(request)
        except (PayloadError, UntrustedSenderError) as exc:
            self._record(result, Stage.NORMALIZE, Outcome.REJECTED, source=source, error=str(exc))
            return None
        if event is None:
            self.evidence.record_delivery_decision(source, woke=False)
            self._record(result, Stage.NORMALIZE, Outcome.IGNORED, source=source)
            return None
        self.evidence.record_delivery_decision(source, woke=True)
        result.event = event
        self._record(
            result,
            Stage.NORMALIZE,
            Outcome.OK,
            source=source,
            delivery=event.delivery_id,
            kind=event.kind.value,
        )

        # Resolve: which agent owns the target repo.
        try:
            agent = resolve_agent(event, self.config)
        except ConfigError as exc:
            self._record(
                result,
                Stage.RESOLVE,
                Outcome.FAILED,
                source=source,
                delivery=event.delivery_id,
                error=str(exc),
            )
            return None
        result.agent = agent
        self._record(
            result,
            Stage.RESOLVE,
            Outcome.OK,
            source=source,
            delivery=event.delivery_id,
            agent=agent.harness_key,
        )

        return agent, event

    def _wake(self, agent: Agent, event: Event, result: PipelineResult) -> bool:
        """Dispatch the wake (with retry); return ``True`` iff it succeeded.

        The boolean drives delivery dedup: the caller marks the delivery as woken
        only on success, so a failed wake never suppresses the duplicate.

        Each attempt is timed, and ``duration`` is always *the last attempt's*
        wall-clock — the wake subprocess's own, never the retry backoff's — so the
        key means one thing on the OK line, the FAILED line, and every retry
        warning between them. A **transient failure is logged as it happens**
        (basecradle-router#170): the backoff used to swallow attempts 1..n-1
        entirely, so a flapping agent that eventually succeeded looked perfectly
        healthy, and one that did not showed a single failure where there had been
        three. Only exhaustion was ever visible.

        **A synthetic probe gets exactly one attempt** (basecradle-router#208). Retry
        exists so a flaky transport does not cost a real unit of work; a probe *is* the
        measurement, and a measurement that retries reports the best of N rather than
        the state of the system. Its two realistic failures argue the same way: an
        agent with no probe secret armed refuses identically every time, so retrying
        only triples the noise, and a genuinely transient fault is answered by the next
        scheduled cycle — which is the honest place to answer it.

        **The wake is bracketed for a human** (basecradle-router#228, a founder
        decision): ``event=wake_start`` immediately before the first attempt and
        ``event=wake_end`` when it resolves, whichever way it resolved. Until now the
        router said nothing at launch — its first word about a wake arrived minutes
        later, at completion — so a Live Tail could not tell an agent working from an
        agent never woken. The pair is the *human* lifecycle surface and the
        ``stage=wake`` record is the pipeline machinery behind it; both stay, the same
        two-surface split Rails draws between its ``Started``/``Completed`` request
        lines and its instrumentation events.

        Two properties are structural rather than remembered. **The bracket always
        closes** — ``wake_end`` is emitted from a ``finally``, so an unexpected error
        on its way out of this method closes the bracket before it propagates; an
        unclosed ``wake_start`` would read, forever, as a wake still running. And
        **both lines carry the same field prefix**, because both render the one
        :func:`_who` mapping — the verdict is appended to it, never spelled into a
        second, drifting order.

        **Real wakes only.** A probe never launches the agent, so bracketing one would
        put a start and an end around nothing and blur the very signal the pair exists
        to show. The gate is :attr:`~basecradle_router.models.Event.synthetic` — the
        same property that already decides the attempt count above.
        """
        who = _who(agent, event)
        attempts = 1 if event.synthetic else self.wake_attempts
        last_duration = 0.0

        # A wake failure is retryable transient by policy here (the boundary
        # reports it as a plain WakeError); the bound stops a permanent fault.
        def attempt() -> WakeResult:
            nonlocal last_duration
            started = self.clock()
            try:
                return self.waker.wake(agent, event)
            except WakeError as exc:
                raise TransientError(str(exc)) from exc
            finally:
                last_duration = self.clock() - started

        def on_retry(failed: int, of: int, exc: Exception) -> None:
            logger.warning(
                "%s %s",
                paint("event=wake_retry"),
                log_fields(
                    attempt=f"{failed}/{of}",
                    **who,
                    duration=_seconds(last_duration),
                    error=str(exc),
                ),
            )

        self._bookend(event, who, "wake_start")
        verdict: dict[str, object] | None = None
        try:
            try:
                woke = with_retry(attempt, attempts=attempts, sleep=self.sleep, on_retry=on_retry)
            except RetryExhausted as exc:
                error = str(exc.__cause__ or exc)
                self.evidence.record_wake_failed(
                    agent.harness_key, error, route=event.source, synthetic=event.synthetic
                )
                # One dict, two surfaces: the stage record's detail and the bookend's
                # verdict are the same fields under the same names, so the machinery
                # line and the human line cannot come to say different things.
                how = {
                    "attempts": attempts,
                    "duration": _seconds(last_duration),
                    "error": error,
                }
                self._record(result, Stage.WAKE, Outcome.FAILED, **who, **how)
                verdict = {"outcome": Outcome.FAILED.value, **how}
                return False
            # The evidence the whole wake-edge claim rests on: this agent was demonstrably
            # woken, at this time, by this delivery, **over this route**. Nothing else in
            # the router proves an agent is reachable rather than merely registered — and
            # without the route it proves only that *some* source reached the agent, which
            # would green a sibling route rejecting every delivery to that same agent.
            self.evidence.record_wake_ok(
                agent.harness_key,
                event.delivery_id,
                route=event.source,
                synthetic=event.synthetic,
            )
            how = {"exit": woke.exit_code, "duration": _seconds(last_duration)}
            self._record(result, Stage.WAKE, Outcome.OK, **who, **how)
            verdict = {"outcome": Outcome.OK.value, **how}
            return True
        finally:
            # Closed from `finally` so no exit path can leave the bracket open — not a
            # return added later, and not an unexpected exception on its way out to the
            # caller's last-resort handler, which is the one path that reaches here with
            # no verdict of its own.
            if verdict is None:
                verdict = {
                    "outcome": Outcome.FAILED.value,
                    "duration": _seconds(last_duration),
                    "error": "unexpected: the wake path raised",
                }
            self._bookend(event, who, "wake_end", **verdict)

    def _bookend(self, event: Event, who: dict[str, object], phase: str, **verdict: object) -> None:
        """Emit one half of the wake lifecycle bracket — ``wake_start`` or ``wake_end``.

        One emitter for both halves, so the end line mirrors the start line's field
        prefix by construction: both render the same ``who`` mapping, and only the
        verdict (``outcome``, ``exit``/``attempts``, ``duration``, ``error``) is
        appended. Nothing is emitted for a synthetic event — see :meth:`_wake`.

        The level follows the verdict, so a failed ``wake_end`` is as visible as the
        ``stage=wake outcome=failed`` record beside it: a bracket whose close was the
        one line filtered out of a WARNING view would be worse than no bracket.

        **And the colour follows it too** (basecradle-router#230). Blue names the
        bookend's *identity*; the verdict's colour is carried by the token that states
        the verdict, so ``outcome=`` is lifted out of the rendered run and painted whole
        — the same token :meth:`_record` already paints on the machinery line beside it.
        Lifting it here rather than teaching :func:`log_fields` about colour is what
        keeps the renderer emitting values only, so no field can smuggle an escape into
        the middle of a token; ``wake_start`` carries no verdict and so stays unpainted
        past its own identity.
        """
        if event.synthetic:
            return
        outcome = verdict.pop("outcome", None)
        level = logging.WARNING if outcome == Outcome.FAILED.value else logging.INFO
        parts = [paint(f"event={phase}"), log_fields(**who)]
        if outcome:
            parts.append(paint(f"outcome={outcome}"))
        parts.append(log_fields(**verdict))
        # One empty-drop rule for the whole line, applied once: `log_fields` already
        # drops an empty *field*, and this drops an empty *run* of them, so an open
        # (no verdict at all) ends at its own field prefix rather than at a separator
        # left behind by a token that was not there.
        logger.log(level, "%s", " ".join(part for part in parts if part))

    def _record(
        self, result: PipelineResult, stage: Stage, outcome: Outcome, **detail: object
    ) -> None:
        rendered = log_fields(**detail)
        record = StageRecord(stage=stage, outcome=outcome, detail=rendered)
        result.records.append(record)
        level = logging.WARNING if outcome in (Outcome.REJECTED, Outcome.FAILED) else logging.INFO
        logger.log(
            level,
            "stage=%s %s%s",
            stage.value,
            # Painted here and nowhere else: `rendered` above is the record's own
            # bytes, so colour reaches the journal without ever reaching the record.
            paint(f"outcome={outcome.value}"),
            f" {rendered}" if rendered else "",
        )

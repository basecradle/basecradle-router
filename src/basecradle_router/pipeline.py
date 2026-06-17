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

logger = logging.getLogger("basecradle_router.pipeline")


class Stage(Enum):
    """The ordered stages a webhook passes through."""

    ROUTE = "route"
    VERIFY = "verify"
    NORMALIZE = "normalize"
    RESOLVE = "resolve"
    LOCK = "lock"
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
    """One stage's outcome — the unit of the router's structured status log."""

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
    in tests), per-agent ``locks``, and a ``sleep`` used by the wake retry
    (injected as a no-op in tests so nothing really waits).
    """

    registry: RouteRegistry
    config: Config
    waker: Waker
    locks: AgentLocks = field(default_factory=AgentLocks)
    breaker: WakeRateBreaker = field(default_factory=WakeRateBreaker)
    wake_attempts: int = 3
    sleep: Callable[[float], None] = time.sleep

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
            self._record(result, Stage.ROUTE, Outcome.FAILED, f"unexpected: {exc}")
            pending = None
        return AcceptResult(result=result, pending=pending)

    def execute(self, agent: Agent, event: Event, result: PipelineResult) -> None:
        """The slow half: lock → breaker → wake, appended to ``result``; never raises.

        Serialized per agent — by the agent's harness-instance identity, not its
        repo — so an agent never runs two concurrent sessions against its one
        home and memory (and so never two sessions sharing its clone). This is
        where the constitution's unified-identity rule lands in the core: every
        input source for an agent funnels through this one lock into a single
        ordered stream the lone harness instance drains, so a future input module
        can never fan a second parallel session onto the same agent.

        Inside the lock, immediately before the wake, the **wake-rate circuit
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
        try:
            with self.locks.guard(agent.harness_key):
                self._record(result, Stage.LOCK, Outcome.OK, agent.harness_key)
                outcome = self.breaker.admit(agent.harness_key, event.stream_key)
                if not outcome.admitted:
                    # A trip/refusal is a deliberate, visible drop — recorded like the
                    # route's IGNORED decisions; the breaker already escalated loudly.
                    self._record(result, Stage.BREAKER, Outcome.IGNORED, outcome.detail)
                    return
                self._wake(agent, event, result)
        except Exception as exc:  # last-resort: a stage bug must never crash the daemon
            self._record(result, Stage.WAKE, Outcome.FAILED, f"unexpected: {exc}")

    def _accept(
        self, source: str, request: InboundRequest, result: PipelineResult
    ) -> tuple[Agent, Event] | None:
        """Drive the accept stages; return ``(agent, event)`` iff there is a wake to run."""
        # Route: find the source's module. Unknown source is a rejection.
        try:
            route = self.registry.get(source)
        except UnknownRouteError as exc:
            self._record(result, Stage.ROUTE, Outcome.REJECTED, str(exc))
            return None
        self._record(result, Stage.ROUTE, Outcome.OK, source)

        # Verify: the security boundary. A missing secret is our misconfiguration,
        # not a bad request — fail the stage rather than reject the caller.
        try:
            route.verify(request, self.config.webhook_secret(source))
        except SignatureError as exc:
            self._record(result, Stage.VERIFY, Outcome.REJECTED, str(exc))
            return None
        except ConfigError as exc:
            self._record(result, Stage.VERIFY, Outcome.FAILED, str(exc))
            return None
        self._record(result, Stage.VERIFY, Outcome.OK)

        # Normalize: payload → Event, or a clean ignore. A malformed payload or a
        # handoff from an untrusted actor is a rejection (logged, no wake).
        try:
            event = route.normalize(request)
        except (PayloadError, UntrustedSenderError) as exc:
            self._record(result, Stage.NORMALIZE, Outcome.REJECTED, str(exc))
            return None
        if event is None:
            self._record(result, Stage.NORMALIZE, Outcome.IGNORED)
            return None
        result.event = event
        self._record(result, Stage.NORMALIZE, Outcome.OK, event.delivery_id)

        # Resolve: which agent owns the target repo.
        try:
            agent = resolve_agent(event, self.config)
        except ConfigError as exc:
            self._record(result, Stage.RESOLVE, Outcome.FAILED, str(exc))
            return None
        result.agent = agent
        self._record(result, Stage.RESOLVE, Outcome.OK, agent.key)

        return agent, event

    def _wake(self, agent: Agent, event: Event, result: PipelineResult) -> None:
        # A wake failure is retryable transient by policy here (the boundary
        # reports it as a plain WakeError); the bound stops a permanent fault.
        def attempt() -> WakeResult:
            try:
                return self.waker.wake(agent, event)
            except WakeError as exc:
                raise TransientError(str(exc)) from exc

        try:
            woke = with_retry(attempt, attempts=self.wake_attempts, sleep=self.sleep)
        except RetryExhausted as exc:
            self._record(result, Stage.WAKE, Outcome.FAILED, str(exc.__cause__ or exc))
            return
        self._record(result, Stage.WAKE, Outcome.OK, f"exit {woke.exit_code}")

    def _record(
        self, result: PipelineResult, stage: Stage, outcome: Outcome, detail: str = ""
    ) -> None:
        record = StageRecord(stage=stage, outcome=outcome, detail=detail)
        result.records.append(record)
        level = logging.WARNING if outcome in (Outcome.REJECTED, Outcome.FAILED) else logging.INFO
        logger.log(level, "stage=%s outcome=%s %s", stage.value, outcome.value, detail)

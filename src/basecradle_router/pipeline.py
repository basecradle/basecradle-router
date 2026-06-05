"""The source-agnostic core pipeline: the stages, in order, tied together.

One inbound webhook flows through fixed stages — **verify → normalize → resolve
→ lock → wake → merge → report** — and the core knows nothing about any specific
source: it asks the registry for the route, and the route owns the
GitHub-specific parts. Adding a source never touches this file.

Every stage records a structured :class:`StageRecord` (the router's own log; the
*agent* reports separately by commenting on the issue). A non-handoff event
short-circuits cleanly as ``IGNORED``; a bad signature or malformed payload is
``REJECTED``; a stage that errors is ``FAILED`` and logged — the daemon never
crashes on one bad event.

The merge stage is wired through a ``pr_provider`` seam: in reality the agent
opens its PR during the wake and the router learns of it (and its CI state)
later, on a separate event; v0 mocks that seam so the orchestration is provable
offline in a single pass.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from basecradle_router.concurrency import (
    RepoLocks,
    RetryExhausted,
    TransientError,
    with_retry,
)
from basecradle_router.config import Config, ConfigError
from basecradle_router.merge_policy import MergeDecision, MergePolicy, PullRequest
from basecradle_router.models import Agent, Event
from basecradle_router.resolve import resolve_agent
from basecradle_router.routes import (
    InboundRequest,
    PayloadError,
    RouteRegistry,
    SignatureError,
    UnknownRouteError,
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
    WAKE = "wake"
    MERGE = "merge"


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
    decision: MergeDecision | None = None

    @property
    def stages(self) -> list[tuple[Stage, Outcome]]:
        """The (stage, outcome) pairs in order — convenient for assertions."""
        return [(r.stage, r.outcome) for r in self.records]

    @property
    def terminal(self) -> Outcome | None:
        """The outcome of the last stage reached, or ``None`` if none ran."""
        return self.records[-1].outcome if self.records else None


# "What PR did this wake produce?" — the agent opened it during the wake; the
# real impl queries GitHub, v0 mocks it. ``None`` means no PR to evaluate.
PrProvider = Callable[[Agent, Event, WakeResult], PullRequest | None]


def _no_pr(_agent: Agent, _event: Event, _result: WakeResult) -> PullRequest | None:
    return None


@dataclass(frozen=True, slots=True)
class Pipeline:
    """Orchestrates the stages for one source's webhooks.

    All collaborators are injected so the whole pipeline is drivable offline:
    ``registry``/``config`` from the route + config layers, a ``waker`` and a
    ``merge_policy`` (both mocked in tests), per-repo ``locks``, an optional
    ``pr_provider`` for the merge stage, and a ``sleep`` used by the wake retry
    (injected as a no-op in tests so nothing really waits).
    """

    registry: RouteRegistry
    config: Config
    waker: Waker
    merge_policy: MergePolicy
    locks: RepoLocks = field(default_factory=RepoLocks)
    pr_provider: PrProvider = _no_pr
    wake_attempts: int = 3
    sleep: Callable[[float], None] = time.sleep

    def handle(self, source: str, request: InboundRequest) -> PipelineResult:
        """Run ``request`` for ``source`` through the pipeline; never raises.

        Returns a :class:`PipelineResult` whose ``records`` are the ordered stage
        outcomes. Expected conditions (ignore, reject, stage failure) resolve to
        records; an unexpected error is caught, recorded ``FAILED``, and
        swallowed so one bad event cannot take down the daemon.
        """
        result = PipelineResult()
        current = Stage.ROUTE
        try:
            current = self._run(source, request, result)
        except Exception as exc:  # last-resort: a stage bug must never crash the daemon
            self._record(result, current, Outcome.FAILED, f"unexpected: {exc}")
        return result

    def _run(self, source: str, request: InboundRequest, result: PipelineResult) -> Stage:
        """Drive the stages, returning the stage reached (for the catch-all's label)."""
        # Route: find the source's module. Unknown source is a rejection.
        try:
            route = self.registry.get(source)
        except UnknownRouteError as exc:
            self._record(result, Stage.ROUTE, Outcome.REJECTED, str(exc))
            return Stage.ROUTE
        self._record(result, Stage.ROUTE, Outcome.OK, source)

        # Verify: the security boundary. A missing secret is our misconfiguration,
        # not a bad request — fail the stage rather than reject the caller.
        try:
            route.verify(request, self.config.webhook_secret(source))
        except SignatureError as exc:
            self._record(result, Stage.VERIFY, Outcome.REJECTED, str(exc))
            return Stage.VERIFY
        except ConfigError as exc:
            self._record(result, Stage.VERIFY, Outcome.FAILED, str(exc))
            return Stage.VERIFY
        self._record(result, Stage.VERIFY, Outcome.OK)

        # Normalize: payload → Event, or a clean ignore.
        try:
            event = route.normalize(request)
        except PayloadError as exc:
            self._record(result, Stage.NORMALIZE, Outcome.REJECTED, str(exc))
            return Stage.NORMALIZE
        if event is None:
            self._record(result, Stage.NORMALIZE, Outcome.IGNORED)
            return Stage.NORMALIZE
        result.event = event
        self._record(result, Stage.NORMALIZE, Outcome.OK, event.delivery_id)

        # Resolve: which agent owns the target repo.
        try:
            agent = resolve_agent(event, self.config)
        except ConfigError as exc:
            self._record(result, Stage.RESOLVE, Outcome.FAILED, str(exc))
            return Stage.RESOLVE
        result.agent = agent
        self._record(result, Stage.RESOLVE, Outcome.OK, agent.repo)

        # Lock + wake + merge: serialized per repo so no two sessions share a clone.
        with self.locks.guard(event.target_repo):
            self._record(result, Stage.LOCK, Outcome.OK, event.target_repo)
            wake_result = self._wake(agent, event, result)
            if wake_result is None:
                return Stage.WAKE
            self._merge(agent, event, wake_result, result)
        return Stage.MERGE

    def _wake(self, agent: Agent, event: Event, result: PipelineResult) -> WakeResult | None:
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
            return None
        self._record(result, Stage.WAKE, Outcome.OK, f"exit {woke.exit_code}")
        return woke

    def _merge(self, agent: Agent, event: Event, woke: WakeResult, result: PipelineResult) -> None:
        pr = self.pr_provider(agent, event, woke)
        if pr is None:
            return  # no PR to evaluate (the merge stage simply does not run)
        decision = self.merge_policy.run(pr)
        result.decision = decision
        self._record(result, Stage.MERGE, Outcome.OK, decision.value)

    def _record(
        self, result: PipelineResult, stage: Stage, outcome: Outcome, detail: str = ""
    ) -> None:
        record = StageRecord(stage=stage, outcome=outcome, detail=detail)
        result.records.append(record)
        level = logging.WARNING if outcome in (Outcome.REJECTED, Outcome.FAILED) else logging.INFO
        logger.log(level, "stage=%s outcome=%s %s", stage.value, outcome.value, detail)

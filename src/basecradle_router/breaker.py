"""The wake-rate circuit breaker — the router's cross-agent runaway backstop.

The router is the **single chokepoint for every wake**: every delivery, from every
source, that survives verify → normalize → resolve funnels through one wake path,
and the router alone has the **cross-agent view** of how often each agent is being
woken. So it is the natural home for the *backstop* that catches a runaway loop the
per-agent harness layer can't — a harness that crashes before it can self-track, a
multi-agent ping-pong, a novel loop from a drop-in ``tools/`` or MCP server
(basecradle-router#110; the harness-side self-breaker is its independent sibling
layer, basecradle-harness#138 — defense-in-depth, no shared protocol).

This is a classic circuit breaker shaped for *wake rate*:

- **Track wakes in a rolling window, per scope.** The primary scope is the
  **agent** (keyed on its :attr:`~basecradle_router.models.Agent.harness_key` — the
  same harness-instance identity the per-agent lock serializes on). A secondary,
  optional scope is the **(agent, stream)** pair — one looping timeline or handoff
  issue — so a single sub-stream spinning trips even while the agent's *overall*
  rate stays under the per-agent cap. A wake is refused if **either** scope is over
  its threshold.
- **Over threshold → TRIP.** Stop dispatching wakes for that scope and **escalate
  loudly** (a structured ``ERROR`` the NOC can detect — a trip is an ops/security
  event: "nothing is silent; everything is escalated"). A trip is never a silent
  drop; the refusal is logged exactly like the route's deliberate ignores.
- **Auto-reset after a cooldown.** A transient burst self-heals: once the cooldown
  elapses the window is cleared and wakes resume, with the reset logged. The breaker
  never latches forever silently.

The thresholds are a **generous sanity cap**, not a precise rate limiter — tuned so
only a genuine runaway trips and legitimate multi-peer activity never does. They are
configured from ``router.env`` (see :func:`~basecradle_router.config.load_breaker_config`).

A dropped wake is recoverable — the platform's cursor-paginated read API is the
source of truth and push is best-effort — so refusing a wake never loses data; it
only pauses the push until the loop is understood and the breaker resets.

The breaker is consulted from the core pipeline's slow half, *inside* the per-agent
lock and immediately before the wake, so it counts the rate at which wakes are
**actually dispatched** (the lock already serializes same-agent wakes one at a time).
A real runaway fires wakes back-to-back as fast as each completes and trips the cap;
a legitimate burst of queued deliveries drains one slow wake at a time and never does.

**Its three lines speak the router's grammar** (basecradle-router#228): a leading
``event=`` token and ``key=value`` fields, painted with the fleet palette
(:mod:`basecradle_router.logfmt`) — ``event=breaker_tripped`` (red), ``event=wake_refused
reason=breaker_open`` (yellow, the same spelling the wake-lock guard's refusals use, so
one query finds every refused wake whatever gate refused it), and ``event=breaker_reset``
(green). They were the last prose lines in the daemon: ``CIRCUIT BREAKER TRIPPED: wake
rate exceeded scope=… key=…`` mixed a shouty sentence with kv fields, so the loudest line
the router can emit was the one line a consumer had to special-case. The data is
unchanged — scope, key, agent, count, threshold, window, cooldown — and the level stays
``ERROR``; only the shape moved. **The literal moved with it**, and the NOC's
``breaker_tripped`` log-metric matches the old sentence, so its detection is re-pointed at
``event=breaker_tripped`` in lockstep with the deploy that lands this (handed to the
capital on basecradle-router#228 — it is a NOC-repo change, not ours to make).

Thread-safe: :meth:`WakeRateBreaker.admit` is called from the pipeline's worker
threads, so all window state is guarded by one short-held lock (the check is
microseconds). The clock is injectable so tests are deterministic and never sleep;
it defaults to :func:`time.monotonic` — immune to wall-clock adjustments, the right
choice for a rate window.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from basecradle_router.logfmt import log_fields, paint

logger = logging.getLogger("basecradle_router.breaker")

AGENT_SCOPE = "agent"
STREAM_SCOPE = "stream"

#: The ``reason=`` a wake refused mid-cooldown carries. A refusal is spelled
#: ``event=wake_refused reason=<why>`` wherever it happens, so the wake-lock guard's
#: refusals and the breaker's are one queryable class with a distinguishing reason —
#: never two vocabularies for one fact (see :mod:`basecradle_router.wakelock`).
BREAKER_OPEN = "breaker_open"

#: The trip line's leading token — **the grammar the fleet's Circuit Breaker Tripped
#: alarm matches on**, spelled once in this repository and imported everywhere else it
#: is named (the claims emitter's ``log-grammar:breaker_tripped`` row, the probe that
#: proves it). A second literal is how the manifest comes to describe a line the daemon
#: no longer writes, which is the whole failure this claim exists to catch
#: (basecradle-noc#509, basecradle-router#232).
TRIP_EVENT = "event=breaker_tripped"

# The per-(agent, stream) window dict accumulates one entry per distinct timeline
# or issue ever seen — unbounded over a long-running daemon, unlike the per-agent
# entries (bounded by the agent registry, like AgentLocks). So stale windows are
# swept periodically: every this-many admits, any window that is not tripped and
# whose newest wake has aged out of the rolling window is dropped (it would be
# recreated empty on the next wake anyway). The interval is large so the sweep's
# cost is amortised to nothing; a tripped window is always kept so its cooldown is
# never lost.
_GC_INTERVAL = 1024


@dataclass(frozen=True, slots=True)
class BreakerConfig:
    """The breaker's tunable thresholds — a generous sanity cap, not a rate limiter.

    ``max_wakes`` wakes within ``window`` seconds for one **agent** trips the
    per-agent scope; ``stream_max_wakes`` does the same for one **(agent, stream)**
    sub-stream (set it to ``0`` to disable the per-stream scope and run agent-only).
    A trip halts that scope's wakes for ``cooldown`` seconds, after which the window
    self-heals. Defaults (``20`` / ``60`` s per agent, ``15`` / ``60`` s per stream,
    ``60`` s cooldown) are the issue's suggested floor — high enough that only a
    runaway trips, low enough to stop one fast.
    """

    max_wakes: int = 20
    window: float = 60.0
    cooldown: float = 60.0
    stream_max_wakes: int = 15

    def __post_init__(self) -> None:
        if self.max_wakes < 1:
            raise ValueError(f"max_wakes must be >= 1, got {self.max_wakes}")
        if self.window <= 0:
            raise ValueError(f"window must be > 0, got {self.window}")
        if self.cooldown < 0:
            raise ValueError(f"cooldown must be >= 0, got {self.cooldown}")
        if self.stream_max_wakes < 0:
            raise ValueError(f"stream_max_wakes must be >= 0, got {self.stream_max_wakes}")


class BreakerState(Enum):
    """The outcome of one :meth:`WakeRateBreaker.admit` call.

    ``ADMITTED`` — under every scope's threshold; dispatch the wake. ``TRIPPED`` —
    this wake pushed a scope over its threshold *now* (the transition; escalated
    loudly). ``OPEN`` — a scope was already tripped and is mid-cooldown, so the wake
    is refused without re-escalating.
    """

    ADMITTED = "admitted"
    TRIPPED = "tripped"
    OPEN = "open"


@dataclass(frozen=True, slots=True)
class BreakerOutcome:
    """What the breaker decided for one wake — the pipeline acts on this.

    ``scope``/``key``/``count`` describe the refusing scope when the wake is not
    admitted (which scope, which agent-or-stream key, how many wakes were in the
    window at the trip). They are empty/zero for an admit.
    """

    state: BreakerState
    scope: str = ""
    key: str = ""
    count: int = 0

    @property
    def admitted(self) -> bool:
        return self.state is BreakerState.ADMITTED

    @property
    def detail(self) -> str:
        """A compact one-line reason, for the pipeline's stage record."""
        if self.admitted:
            return "admitted"
        return f"{self.state.value} scope={self.scope} key={self.key} count={self.count}"


@dataclass(slots=True)
class _Window:
    """One scope's rolling-window state — its recent wake timestamps and trip clock."""

    timestamps: deque[float] = field(default_factory=deque)
    tripped_until: float | None = None


class WakeRateBreaker:
    """A rolling-window wake-rate breaker, checked once per wake at the chokepoint.

    Construct with a :class:`BreakerConfig` (defaults if omitted) and an optional
    ``clock`` (defaults to :func:`time.monotonic`; tests inject a fake). Call
    :meth:`admit` for each wake the pipeline is about to dispatch; act on the
    returned :class:`BreakerOutcome`. The breaker logs its own trip/refuse/reset
    lines — the loud ops/security signal — so the pipeline only records the stage.

    **``synthetic_source`` — the log-grammar probe's one switch, and why it is one.**
    A manufactured trip must be readable by the NOC's extraction guard and invisible to
    the alarms that extraction feeds, and those are two different consumers reading two
    different channels. So the switch moves both at once, and cannot move half:

    - **the message** gains a trailing ``source=<value>`` token (``probe``, the fleet's
      founder-ratified wake-origin stamp — reused rather than re-minted, capital ruling
      on basecradle-noc#509 §1), which is what the *Circuit Breaker Tripped* alarm
      block-lists. It is appended **last**, so the synthetic is a strict
      prefix-extension of the genuine line;
    - **the level** drops from ``ERROR`` to ``INFO``, which is what keeps the
      *severity*-fed alarms clean **with no filter at all** — *Server Errors* counts
      ERROR/CRITICAL on this identifier, and a blanket synthetic filter there would be a
      regression (a probe wake that drives the router to ERROR is a genuine fault the
      fleet must see). Severity is safe to move because no extraction gates on it: the
      NOC's ``breaker_tripped`` column matches the message and the ``level`` column
      lifts the token as data. ``tests/test_breaker.py`` pins that.

    Left empty — the daemon's only construction — :func:`~basecradle_router.logfmt.log_fields`
    drops the field, so a genuine trip is **byte-identical** to what it emitted before
    the switch existed. Not ``source=false``: an absent field is the one that cannot
    change a production line.
    """

    def __init__(
        self,
        config: BreakerConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        synthetic_source: str = "",
    ) -> None:
        self._config = config or BreakerConfig()
        self._clock = clock
        # ONE switch, both channels — see the class docstring. Setting only half of it
        # is the failure mode (a manufactured line that pages, or a real trip filtered
        # out of its own alarm), so there is deliberately no way to set half.
        self._synthetic_source = synthetic_source
        self._trip_level = logging.INFO if synthetic_source else logging.ERROR
        self._lock = threading.Lock()
        self._windows: dict[tuple, _Window] = {}
        self._admits_since_gc = 0

    @property
    def config(self) -> BreakerConfig:
        """The thresholds this breaker is running with — read-only.

        Exposed so the daemon's startup banner can state the *live* thresholds
        rather than restate the defaults (basecradle-router#170): the whole point of
        the banner is to answer "what config did this running router boot with?"
        from the log alone, which a hard-coded restatement could not.
        """
        return self._config

    def admit(self, agent_key: str, stream_key: str | None = None) -> BreakerOutcome:
        """Record a wake for ``agent_key`` (and its ``stream_key``) and decide its fate.

        Returns an admitted :class:`BreakerOutcome` when every applicable scope is
        under threshold; otherwise the wake is refused — ``TRIPPED`` on the trip
        transition (escalated), ``OPEN`` while a tripped scope cools down. A refused
        wake is *not* recorded in the window (so cooldown can clear it); the trip and
        every subsequent refusal are logged so the drop is never silent.
        """
        cfg = self._config
        now = self._clock()
        # (scope name, window dict-key, threshold, key shown in logs/outcome).
        scopes: list[tuple[str, tuple, int, str]] = [
            (AGENT_SCOPE, (AGENT_SCOPE, agent_key), cfg.max_wakes, agent_key)
        ]
        if stream_key is not None and cfg.stream_max_wakes > 0:
            scopes.append(
                (
                    STREAM_SCOPE,
                    (STREAM_SCOPE, agent_key, stream_key),
                    cfg.stream_max_wakes,
                    stream_key,
                )
            )

        with self._lock:
            self._admits_since_gc += 1
            if self._admits_since_gc >= _GC_INTERVAL:
                self._gc(now)
                self._admits_since_gc = 0

            # 1) If any scope is mid-cooldown, refuse now (don't record). A scope
            #    whose cooldown has elapsed self-heals: clear it and carry on.
            for scope, dict_key, _threshold, display in scopes:
                window = self._windows.get(dict_key)
                if window is None or window.tripped_until is None:
                    continue
                if now < window.tripped_until:
                    logger.warning(
                        "%s %s",
                        paint("event=wake_refused"),
                        log_fields(
                            reason=BREAKER_OPEN,
                            agent=agent_key,
                            scope=scope,
                            key=display,
                            source=self._synthetic_source,
                        ),
                    )
                    return BreakerOutcome(BreakerState.OPEN, scope=scope, key=display)
                window.timestamps.clear()
                window.tripped_until = None
                logger.info(
                    "%s %s",
                    paint("event=breaker_reset"),
                    log_fields(
                        agent=agent_key,
                        scope=scope,
                        key=display,
                        source=self._synthetic_source,
                    ),
                )

            # 2) Record this wake on each scope; trip the first that goes over.
            for scope, dict_key, threshold, display in scopes:
                window = self._windows.setdefault(dict_key, _Window())
                cutoff = now - cfg.window
                while window.timestamps and window.timestamps[0] <= cutoff:
                    window.timestamps.popleft()
                window.timestamps.append(now)
                count = len(window.timestamps)
                if count > threshold:
                    window.tripped_until = now + cfg.cooldown
                    logger.log(
                        self._trip_level,
                        "%s %s",
                        paint(TRIP_EVENT),
                        log_fields(
                            agent=agent_key,
                            scope=scope,
                            key=display,
                            count=count,
                            threshold=threshold,
                            window=f"{cfg.window:.0f}s",
                            cooldown=f"{cfg.cooldown:.0f}s",
                            # LAST, always. The stamp trails the grammar under proof so a
                            # synthetic line is a strict prefix-extension of the genuine
                            # one: no re-point of the NOC's expression can match the
                            # synthetic while failing on a real trip.
                            source=self._synthetic_source,
                        ),
                    )
                    return BreakerOutcome(
                        BreakerState.TRIPPED, scope=scope, key=display, count=count
                    )

        return BreakerOutcome(BreakerState.ADMITTED)

    def _gc(self, now: float) -> None:
        """Drop stale, untripped windows so the per-stream dict stays bounded.

        Called under the lock. A window is reclaimable once its newest wake has
        aged past the rolling window and it is not mid-cooldown — it carries no
        live state and would be recreated empty on the next wake. A tripped window
        is always kept (its ``tripped_until`` is the cooldown we must honour)."""
        cutoff = now - self._config.window
        stale = [
            key
            for key, window in self._windows.items()
            if window.tripped_until is None
            and (not window.timestamps or window.timestamps[-1] <= cutoff)
        ]
        for key in stale:
            del self._windows[key]

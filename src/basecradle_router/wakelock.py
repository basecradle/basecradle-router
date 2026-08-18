"""The NOC wake-lock interlock — refuse to wake an agent mid-converge.

The NOC (the fleet's "AI mechanic") upgrades an agent's harness in place. While it
converges an agent's install it must not be woken: a wake landing on a
half-installed venv is the failure to avoid. The NOC's only guard was a ``pgrep``
quiesce check — a check-then-act race. This closes it with an explicit **wake-lock**
the NOC writes and the router honours (counterpart to basecradle-noc#38; the NOC
builds the writer side, this is the reader side).

**The seam (pinned by the capital — this builds to it exactly).**

- **Lock file:** ``/run/basecradle-noc/wake-locks/<slug>.lock`` — root-owned, on the
  ``/run`` tmpfs (a reboot clears stale locks). **Present = locked.**
- **Contents (JSON):** ``{"agent": "<slug>", "reason": "...", "acquired_at":
  "<ISO8601>", "expires_at": "<ISO8601>"}``. ``expires_at`` is a TTL (converge
  timeout + buffer) — the stale-lock backstop so a dead NOC never wedges an agent
  forever.

``<slug>`` is the agent's universal-identity slug — its OS user and home name — which
the router already carries as :attr:`~basecradle_router.models.Agent.harness_key`
(the same key the per-agent lock and the wake-rate breaker serialize on). The lock
path is the only contract; nothing else about the wake mechanism changes.

**The decisions (and why each is safe).** Refusing a wake never loses data — the
platform's read API is the source of truth and push is best-effort — so a refused
wake only *pauses* the push; the agent processes the message on its next wake once
the lock clears. That asymmetry (corrupting a converge ≫ delaying a recoverable
push) sets the fail-direction for every edge:

- **Absent** → wake normally (the common case; no log — the happy path is silent).
- **Held** (present and ``now < expires_at``) → **refuse**; emit
  ``event=wake_refused reason=wake_lock_held agent=<slug>`` so the interlock is
  visible in BetterStack Live Tail (the founder's observability requirement).
- **Stale** (present but ``now >= expires_at`` — the NOC died mid-converge) →
  **wake**, but emit ``event=wake_lock_stale agent=<slug>`` so a wedged converge is
  visible.
- **Unparseable** (present but malformed JSON, or no usable ``expires_at``) →
  **refuse** — ``Present = locked`` is the primary rule and the timestamps are only
  the staleness *escape*; when we cannot read them we fall back to safe. Logged
  loudly (``reason=wake_lock_unparseable``) so a NOC contract violation surfaces;
  ``/run`` + reboot + the NOC overwriting the file keep it from wedging forever.
- **Unreadable** (a ``PermissionError``/``OSError`` reading the lock — a *deployment*
  fault, not a per-agent one) → **wake** (fail-open) with a loud ``ERROR``. Wedging
  every wake on a perms misconfiguration would be far worse than the interlock being
  temporarily disabled, and the NOC's ``pgrep`` pre-check still backstops. The lock
  is non-secret (slug + converge reason + timestamps), so the NOC publishes it
  world-readable (root-owned ``0644`` in a ``0755`` dir) and this path stays cold.

The clock is injectable so tests are deterministic and never depend on wall time;
it defaults to :func:`datetime.now` in UTC (the lock's timestamps are wall-clock
ISO8601, so the comparison must be wall-clock too — not the breaker's monotonic).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from basecradle_router.logfmt import paint

logger = logging.getLogger("basecradle_router.wakelock")

#: The pinned lock directory on the home server (root-owned, ``/run`` tmpfs).
DEFAULT_LOCK_DIR = "/run/basecradle-noc/wake-locks"


class WakeLockState(Enum):
    """The outcome of one :meth:`WakeLockGuard.check`.

    ``ABSENT``/``STALE``/``UNREADABLE`` permit the wake; ``HELD``/``UNPARSEABLE``
    refuse it. See the module docstring for why each edge fails the way it does.
    """

    ABSENT = "absent"
    HELD = "held"
    STALE = "stale"
    UNPARSEABLE = "unparseable"
    UNREADABLE = "unreadable"


_WAKE_STATES = frozenset({WakeLockState.ABSENT, WakeLockState.STALE, WakeLockState.UNREADABLE})


@dataclass(frozen=True, slots=True)
class WakeLockDecision:
    """What the guard decided for one agent — the pipeline acts on this.

    ``detail`` is a compact one-line reason for the pipeline's stage record; the
    guard logs the loud, filterable line itself (so the pipeline only records the
    stage), exactly as the wake-rate breaker does.

    The remaining fields are the decision's *raw* parts, kept beside the composed
    ``detail`` so a second consumer can render them its own way rather than parse
    the sentence back apart: ``reason`` is the bare cause of an ``UNREADABLE`` or
    ``UNPARSEABLE`` verdict, and ``lock_reason``/``expires_at`` are the NOC's own
    stated converge reason and TTL from a lock that parsed. The freeze
    self-test (:mod:`basecradle_router.selftest`) reports on these; :meth:`check`
    logs from them.
    """

    state: WakeLockState
    detail: str = ""
    reason: str = ""
    lock_reason: str = ""
    expires_at: str = ""

    @property
    def should_wake(self) -> bool:
        return self.state in _WAKE_STATES


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _unparseable(reason: str) -> WakeLockDecision:
    # Present but unreadable as a lock: honour ``Present = locked`` and refuse. The
    # loud line is emitted by ``check`` so the NOC contract violation surfaces in Live
    # Tail on the wake path, and stays silent on the self-test's read-only pass.
    return WakeLockDecision(
        WakeLockState.UNPARSEABLE, f"wake_lock_unparseable: {reason}", reason=reason
    )


def _parse_iso8601(value: object) -> datetime | None:
    """Parse an ISO8601 timestamp to an aware UTC ``datetime``, or ``None``.

    Tolerant of a trailing ``Z`` (``datetime.fromisoformat`` only accepts it from
    3.11, and the project floor is 3.10) and of a naive timestamp (assumed UTC),
    so a small format drift on the NOC side never silently mis-compares.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class WakeLockGuard:
    """Reads the NOC wake-lock for an agent and decides whether the wake may fire.

    ``lock_dir`` is the pinned lock directory (overridable for tests); ``now`` is
    the injectable clock (defaults to UTC wall-clock). Construct once and call
    :meth:`check` per wake, immediately before dispatch, with the agent's
    ``harness_key`` (its slug). Stateless beyond its config — safe to share across
    the pipeline's worker threads (each call only reads a file).
    """

    lock_dir: str = DEFAULT_LOCK_DIR
    now: Callable[[], datetime] = _utc_now

    def path_for(self, slug: str) -> str:
        """The lock file this guard would read for ``slug`` — what the self-test names.

        Public because a report about the freeze surface is useless unless it names
        the exact file, and the *guard's* view of that path (its configured
        ``lock_dir``) is the only one that matters: a router pointed at a different
        directory than the NOC writes is precisely the "the control existed but was
        never read" failure the self-test exists to catch.
        """
        return os.path.join(self.lock_dir, f"{slug}.lock")

    def check(self, slug: str) -> WakeLockDecision:
        """Decide the wake's fate for ``slug``, logging the decision. The wake path.

        :meth:`inspect` makes the decision; this adds the loud, filterable log line
        for every non-``ABSENT`` outcome (the happy path stays silent). The split
        exists because the freeze self-test must exercise the *same* read-and-decide
        code the daemon uses — that is what makes it a proof rather than a
        re-implementation — without emitting ``event=wake_refused`` lines for wakes
        that were never attempted.
        """
        decision = self.inspect(slug)
        state = decision.state
        # The leading `event=` token is painted with the fleet palette
        # (basecradle-router#228) — red for the one that says the guard itself is
        # broken, yellow for a wake that did not run. It wraps the WHOLE token, so
        # every existing search for `event=wake_refused` still matches; see
        # :mod:`basecradle_router.logfmt`.
        if state is WakeLockState.UNREADABLE:
            logger.error(
                "%s agent=%s detail=%s",
                paint("event=wake_lock_unreadable"),
                slug,
                decision.reason,
            )
        elif state is WakeLockState.HELD:
            logger.warning(
                "%s reason=wake_lock_held agent=%s lock_reason=%s expires_at=%s",
                paint("event=wake_refused"),
                slug,
                decision.lock_reason,
                decision.expires_at,
            )
        elif state is WakeLockState.STALE:
            logger.warning(
                "%s agent=%s expires_at=%s",
                paint("event=wake_lock_stale"),
                slug,
                decision.expires_at,
            )
        elif state is WakeLockState.UNPARSEABLE:
            logger.warning(
                "%s reason=wake_lock_unparseable agent=%s detail=%s",
                paint("event=wake_refused"),
                slug,
                decision.reason,
            )
        return decision

    def inspect(self, slug: str) -> WakeLockDecision:
        """Read and decide ``slug``'s lock **without logging** — total, never raises.

        The pure half of :meth:`check`: same file, same parse, same fail-directions,
        no side effects. The freeze self-test calls this so it can report on every
        registered agent's lock in one pass without polluting the journal with
        refusals that did not happen.
        """
        # The slug is the agent's OS-user name from the root-owned registry (trusted
        # config), but a path-separator in it would escape the lock dir — guard it
        # cheaply and fail open rather than ever reading an arbitrary path.
        if slug != os.path.basename(slug) or slug in ("", ".", ".."):
            return WakeLockDecision(
                WakeLockState.UNREADABLE, f"unsafe slug {slug!r}", reason="unsafe-slug"
            )

        try:
            with open(self.path_for(slug), encoding="utf-8") as handle:
                raw = handle.read()
        except FileNotFoundError:
            return WakeLockDecision(WakeLockState.ABSENT)
        except (OSError, ValueError) as exc:
            # Permission denied or any other read fault is a deployment problem, not
            # a per-agent one: fail open (wake) but escalate loudly. ValueError covers
            # an embedded-NUL slug (``open`` raises it, not OSError) so this is
            # total — it never raises, whatever the slug, and the guard stays the sole
            # decision point rather than leaking into the pipeline's last-resort catch.
            return WakeLockDecision(WakeLockState.UNREADABLE, f"unreadable: {exc}", reason=str(exc))

        return self._decide(raw)

    def _decide(self, raw: str) -> WakeLockDecision:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return _unparseable(f"invalid JSON: {exc}")
        if not isinstance(data, dict):
            return _unparseable("lock is not a JSON object")

        expires_at = _parse_iso8601(data.get("expires_at"))
        if expires_at is None:
            return _unparseable("missing or invalid expires_at")

        until = expires_at.isoformat()
        if self.now() < expires_at:
            # A valid, live lock: the NOC is converging this agent. Refuse the wake.
            reason = data.get("reason")
            return WakeLockDecision(
                WakeLockState.HELD,
                f"wake_lock_held until {until}",
                lock_reason=reason if isinstance(reason, str) else "",
                expires_at=until,
            )

        # Expired: the NOC died mid-converge (or the TTL elapsed). Wake, but flag it.
        return WakeLockDecision(
            WakeLockState.STALE, f"wake_lock_stale (expired {until})", expires_at=until
        )

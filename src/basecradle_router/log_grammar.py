"""The log-grammar probe — emit the real breaker-tripped line, so the alarm can be watched.

Fleet observability catches failures that **happen**. The NOC's extraction guard
(basecradle-noc#504) closes one half of what it cannot catch: it evaluates every
git-tracked ``sql_expression`` against the fleet's own log lines and reddens the drift
heartbeat when a column that *should* be extracting is extracting nothing. It works by
watching traffic — and that is exactly why it cannot watch this one.

``breaker_tripped`` powers the *Circuit Breaker Tripped* alarm, and **every clause in its
pattern is a whole line that exists only on the failure path**. Nothing in it arrives on a
healthy fleet, so nothing is there to be watched: a rename would take the alarm silently
to zero and no live-traffic instrument could see it. basecradle-router#228 was that
rename — caught only because it was made deliberately, with the NOC's re-point handed over
in lockstep. The next one will not be.

**A needle alarm's grammar is a claim about the emitter's source, so the proof belongs to
the emitter** (basecradle-noc#509, basecradle-router#232). This module is the router's
half: it drives a *real* :class:`~basecradle_router.breaker.WakeRateBreaker` past its own
threshold so the daemon's **own** trip statement renders the bytes, writes that line to
the daemon's **own** journald identifier, and reads it back out of the journal. The line
then ships through Vector like any other and the guard reads it off the live stream.

**It contains no renderer, and that is the whole design.** A probe with its own format
string would be a second spelling of the grammar — the very thing that lets a manifest go
on describing a line the daemon stopped writing. Here a rename in
:mod:`basecradle_router.breaker` moves the probe's bytes in the same commit, by
construction. What the probe *chooses* is which arguments to pass; it cannot choose the
shape.

**The two channels that keep it out of production alerting** are the ratified joint shape
(capital ruling on basecradle-noc#509, shared with the harness's ``billing_blocked``
probe), and they are two because their consumers are two:

- the message carries a trailing ``source=probe`` — the fleet's founder-ratified
  wake-origin stamp (basecradle-noc#473), **reused rather than re-minted**, which the
  *Circuit Breaker Tripped* alarm block-lists exactly as four production charts already
  do. Appended **last**, so the synthetic is a strict prefix-extension of a genuine trip:
  no re-point can pass here and fail on the real thing;
- the line is logged at **INFO** rather than ``ERROR``, which keeps the *severity*-fed
  alarms clean with no filter at all.

Both are one switch on the breaker (``synthetic_source``) precisely so neither can be set
without the other.

**What this proves, and what it does not.** The probe proves **emission**: these bytes,
this grammar, in this journal. It does not — and must not — assert **extraction**: it
cannot see ClickHouse, must not gain a way to, and a probe that graded the NOC's regex
would be the second spelling again. The guard owns that half, off the live stream. The
composition is what localizes a fault: claim green + guard deaf is a stale expression;
claim red is an emitter defect.

**Three verdicts, the contract's own three** (basecradle-noc#408, ruling 4):

- ``proven`` / exit ``0`` — the trip statement ran, the bytes carry the declared grammar,
  and the line was found in the journal.
- ``broken`` / exit ``1`` — we asked and the answer is no: the rendered bytes do **not**
  carry the grammar this component's manifest declares (a ``breaker.py`` change that
  :mod:`basecradle_router.claims` did not follow), or the line was written and never
  landed in the journal.
- ``unprovable`` / exit ``75`` — we never got an answer: ``systemd-cat`` or ``journalctl``
  is missing or failed. Still red, still immediate.

**It writes nothing but a log line.** The admin CLI's read-only rule holds: no evidence
document, no daemon state, no live breaker — the instance here is local to this process,
so no real wake is refused and no real window moves.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from basecradle_router.breaker import TRIP_EVENT, BreakerConfig, BreakerState, WakeRateBreaker
from basecradle_router.logfmt import LOG_FORMAT
from basecradle_router.probe import BROKEN, EXIT_CODES, PROVEN
from basecradle_router.routes.probe import ProbeRoute

logger = logging.getLogger("basecradle_router.log_grammar")

#: The journald identifier the synthetic line is written under — **the daemon's own**
#: (``SyslogIdentifier=`` in ``deploy/systemd/basecradle-router.service``). The capital
#: ruled per-emitter identifier ownership on basecradle-noc#509 §6: the router's line
#: lands where a genuine trip lands, for maximal fidelity, and the ``INFO`` level is what
#: keeps the identifier-scoped ``error_lines`` column clean with no predicate. It is also
#: what the NOC's witness declares as its ``parent``.
IDENTIFIER = "basecradle-router"

#: The NOC column this proves, and — by the capital's ruling on basecradle-noc#509 §5 —
#: the claim id's suffix, spelled exactly as the column is. The ledger is where the two
#: repos' vocabularies meet, so a claim id is shared vocabulary by design.
LINE_CLASS = "breaker_tripped"

#: The synthetic's agent key. A reserved literal that cannot collide with an OS-user slug
#: (no agent is named for a route), approved on basecradle-noc#509 §7. It rides the
#: ``agent`` label, which the alarm's own filter drops along with the rest of the line.
SYNTHETIC_AGENT = "probe"

#: How long to wait for journald to make the line readable, and how often to re-ask. The
#: write is asynchronous, so a single immediate read would report a healthy journal as a
#: fault; the budget is generous because the cost of being wrong here is a page.
READBACK_TIMEOUT = 10.0
READBACK_INTERVAL = 0.25

#: How long the two journal commands may run before we call it *no answer*. They are
#: local IPC against journald; anything near this is a broken box, not a slow one.
COMMAND_TIMEOUT = 20.0

#: The emitted stamp, read off the probe route rather than spelled here. The value is the
#: same string the pipeline's ``source=`` carries for a synthetic delivery, so the fleet's
#: one wake-origin vocabulary has one author.
SOURCE = ProbeRoute.name

#: The daemon's package logger. Only the running daemon attaches a tagged handler to it
#: (:func:`~basecradle_router.server.configure_logging`), which is what lets
#: :func:`capture_trip` tell "the admin CLI" from "inside the daemon" without being told.
PACKAGE_LOGGER = "basecradle_router"

#: The logger the trip line is emitted under — part of the grammar, because the NOC's
#: envelope carries ``%(name)s`` and its ``level`` column reads the message text.
BREAKER_LOGGER = "basecradle_router.breaker"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class LogGrammarResult:
    """One run: what was rendered, what was written, and whether the journal has it."""

    status: str
    line_class: str
    identifier: str
    grammar: str
    rendered: str
    detail: str
    checked_at: str
    waited: float

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    @property
    def proven(self) -> bool:
        return self.status == PROVEN

    def summary(self) -> str:
        """One line an operator (and the NOC's forwarded stderr) can read on its own."""
        return f"{self.detail} [grammar={self.grammar!r} identifier={self.identifier}]"

    def to_json(self) -> dict:
        return {
            "probe": "log-grammar",
            "status": self.status,
            "line_class": self.line_class,
            "identifier": self.identifier,
            "grammar": self.grammar,
            "rendered": self.rendered,
            "detail": self.detail,
            "checked_at": self.checked_at,
            "waited_seconds": round(self.waited, 3),
        }


class LogGrammarError(Exception):
    """The probe could not be *attempted* — the journal tooling did not answer."""


#: The two injectable seams, so the whole probe is drivable offline without journald —
#: the same discipline :mod:`basecradle_router.probe` applies to its HTTP boundary.
#: ``Writer`` puts one already-formatted line in the journal under ``IDENTIFIER``;
#: ``Reader`` returns the journal's lines for that identifier since a given epoch.
Writer = Callable[[str], None]
Reader = Callable[[float], Sequence[str]]


def capture_trip(*, synthetic: bool) -> logging.LogRecord:
    """Drive a real breaker past its threshold and return the trip record it emitted.

    The bytes come from :meth:`~basecradle_router.breaker.WakeRateBreaker.admit`'s own
    trip statement — this function passes arguments and captures output, and has no
    format string of its own. The breaker is local to this call, so the daemon's live
    windows are untouched; the clock is frozen so every admit lands inside one window and
    nothing sleeps.

    A :class:`logging.LogRecord` rather than a string, so every caller derives what it
    needs from the *same* emission — the journal line from
    :data:`~basecradle_router.logfmt.LOG_FORMAT`, the grammar checks from
    :meth:`~logging.LogRecord.getMessage`. Splitting the envelope back off a formatted
    line would be a second, guessing parser of our own format (``asctime`` alone is two
    space-separated tokens), and a parser that guesses wrong reports a healthy line as a
    grammar fault.

    ``synthetic=False`` renders what a **genuine** trip writes, which is what makes the
    prefix invariant checkable at exercise time rather than only in the test suite.

    **This must not run inside the daemon, and it refuses to.** Capturing means muting the
    breaker's logger for the duration — otherwise the genuine render would print an
    ``ERROR`` reading ``event=breaker_tripped`` onto the CLI's own stderr, which the NOC
    forwards verbatim on a non-proven verdict. In the daemon that same mute would swallow
    a **real** trip: the loudest line the router can emit, lost to the instrument that
    exists to protect it. It cannot happen today — every caller is the admin CLI, which
    the NOC runs in its own process — so the guard costs nothing now and is the difference
    between a constraint that is written down and one that holds.
    """
    package = logging.getLogger(PACKAGE_LOGGER)
    if any(getattr(handler, "_basecradle_router", False) for handler in package.handlers):
        raise LogGrammarError(
            "refusing to render a trip inside the daemon: capturing would mute a real one"
        )
    # One config, used both to build the breaker and to bound the loop that trips it —
    # two constructions could drift apart and leave the loop unable to reach a trip.
    # The DEFAULTS, deliberately, never the daemon's live thresholds: this probe reads no
    # configuration at all (see `_cmd_probe_log_grammar`), so nothing stale can make it
    # answer about the wrong daemon. Its numeric fields are a grammar SAMPLE, not a
    # statement about what the running router is configured with.
    config = BreakerConfig()
    breaker = WakeRateBreaker(
        config,
        clock=lambda: 0.0,
        synthetic_source=SOURCE if synthetic else "",
    )
    captured: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = captured.append  # type: ignore[method-assign]
    breaker_logger = logging.getLogger(BREAKER_LOGGER)
    previous_level, previous_propagate = breaker_logger.level, breaker_logger.propagate
    breaker_logger.addHandler(handler)
    breaker_logger.setLevel(logging.INFO)
    # The daemon's handlers are not this probe's business, and a stray copy on stderr
    # would be a second, unstamped rendering of the very line under proof.
    breaker_logger.propagate = False
    try:
        for _ in range(config.max_wakes + 1):
            if breaker.admit(SYNTHETIC_AGENT).state is BreakerState.TRIPPED:
                break
    finally:
        breaker_logger.removeHandler(handler)
        breaker_logger.setLevel(previous_level)
        breaker_logger.propagate = previous_propagate
    if len(captured) != 1:
        raise LogGrammarError(
            f"one trip must emit exactly one line; the breaker emitted {len(captured)}"
        )
    return captured[0]


def render_trip(*, synthetic: bool) -> str:
    """One trip, formatted exactly as the daemon's own handler formats its lines."""
    return logging.Formatter(LOG_FORMAT).format(capture_trip(synthetic=synthetic))


def trip_message(*, synthetic: bool) -> str:
    """One trip's ``%(message)s`` half — the bytes the NOC's expression matches on."""
    return capture_trip(synthetic=synthetic).getMessage()


def _write_to_journal(line: str) -> None:
    """Put one formatted line in the journal under the daemon's own identifier.

    ``systemd-cat`` is the box's established mechanism for this — ``wake-runner`` already
    routes every wake through it for ``basecradle-wake-<slug>`` — so this adds no
    dependency the deploy does not already carry. ``--priority=info`` matches the level
    the switch already put in the message text, so the journal's ``PRIORITY`` and the
    ``level`` token the NOC extracts agree.
    """
    try:
        completed = subprocess.run(
            ["systemd-cat", f"--identifier={IDENTIFIER}", "--priority=info"],
            input=line + "\n",
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LogGrammarError(f"could not run systemd-cat: {exc}") from exc
    if completed.returncode != 0:
        raise LogGrammarError(
            f"systemd-cat exited {completed.returncode}: {completed.stderr.strip()[:200]}"
        )


def _read_from_journal(since: float) -> Sequence[str]:
    """The journal's message lines for this identifier since ``since`` (epoch seconds).

    Read as the daemon's own user, which is the only principal that needs to succeed:
    the line this probe wrote is that user's own entry, so no privileged group membership
    is involved (verified live on the box, basecradle-noc#509 §7).
    """
    try:
        completed = subprocess.run(
            [
                "journalctl",
                f"--identifier={IDENTIFIER}",
                f"--since=@{since:.0f}",
                "--output=cat",
                "--no-pager",
            ],
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LogGrammarError(f"could not run journalctl: {exc}") from exc
    if completed.returncode != 0:
        raise LogGrammarError(
            f"journalctl exited {completed.returncode}: {completed.stderr.strip()[:200]}"
        )
    return completed.stdout.splitlines()


class LogGrammarProbe:
    """Render the real trip line, put it in the journal, and prove the journal has it."""

    def __init__(
        self,
        *,
        writer: Writer | None = None,
        reader: Reader | None = None,
        timeout: float | None = None,
        interval: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # Resolved here rather than bound as default argument values: a default binds at
        # *definition* time, which would freeze the module's own constants into the
        # signature and make them un-overridable — including for the tests that must drive
        # this without a journald on the machine.
        self._writer = writer if writer is not None else _write_to_journal
        self._reader = reader if reader is not None else _read_from_journal
        self._timeout = READBACK_TIMEOUT if timeout is None else timeout
        self._interval = READBACK_INTERVAL if interval is None else interval
        self._clock = clock
        self._sleep = sleep

    def run(self) -> LogGrammarResult:
        """Emit one synthetic trip and report whether the journal carries it.

        Raises :class:`LogGrammarError` when the probe could not be *attempted* — the
        journal tooling did not answer — which the CLI reports as the contract's
        inconclusive sentinel rather than as a verdict about the grammar.
        """
        started = self._clock()
        record = capture_trip(synthetic=True)
        rendered = logging.Formatter(LOG_FORMAT).format(record)
        broken = self._grammar_fault(record.getMessage())
        if broken is not None:
            return self._result(BROKEN, rendered, broken, self._clock() - started)

        # Bound the read-back to this run. The formatted line carries its own millisecond
        # timestamp, so a match is already this run's rather than a previous probe's, but
        # the bound keeps the scan small and says so out loud.
        since = time.time() - 1
        self._writer(rendered)
        deadline = self._clock() + self._timeout
        while True:
            if rendered in self._reader(since):
                return self._result(
                    PROVEN,
                    rendered,
                    f"rendered by the breaker's own trip statement and read back from the "
                    f"{IDENTIFIER} journal",
                    self._clock() - started,
                )
            if self._clock() >= deadline:
                return self._result(
                    BROKEN,
                    rendered,
                    f"the line was written but never appeared in the {IDENTIFIER} journal "
                    f"within {self._timeout:.0f}s",
                    self._clock() - started,
                )
            self._sleep(self._interval)

    def _grammar_fault(self, message: str) -> str | None:
        """Why these bytes do not satisfy the manifest's declaration — or ``None``.

        Two checks, and each one catches a change the other cannot. The **grammar** check
        is the declaration↔reality tie: it fails the moment ``breaker.py`` renames the
        token without :mod:`basecradle_router.claims` following, which is precisely the
        drift that made the manifest able to lie. The **prefix** check is the
        synthetic↔genuine tie: it fails if the stamp ever stops trailing the grammar under
        proof, at which point a re-point could pass here and still go dark on a real trip.

        What neither can catch is a rename made in both files at once — and that is the
        NOC's half, which goes deaf because *its* expression did not move.
        """
        if TRIP_EVENT not in message:
            return f"the rendered line does not carry the declared grammar {TRIP_EVENT!r}"
        stamp = f"source={SOURCE}"
        if not message.endswith(stamp):
            return f"the synthetic stamp {stamp!r} does not trail the line"
        genuine = trip_message(synthetic=False)
        if message != f"{genuine} {stamp}":
            return (
                "the synthetic line is not a strict prefix-extension of a genuine trip "
                f"({genuine!r} + {stamp!r})"
            )
        return None

    def _result(self, status: str, rendered: str, detail: str, waited: float) -> LogGrammarResult:
        return LogGrammarResult(
            status=status,
            line_class=LINE_CLASS,
            identifier=IDENTIFIER,
            grammar=TRIP_EVENT,
            rendered=rendered,
            detail=detail,
            checked_at=_utc_now().isoformat(),
            waited=max(waited, 0.0),
        )


def manifest_detail() -> dict:
    """What this probe declares about itself, for the claim's ``detail``.

    **Read from the probe rather than restated**, the same discipline
    :func:`~basecradle_router.claims._freeze_claim` applies to its exit codes: the
    manifest publishes the bytes this build actually renders, so it cannot describe a
    grammar the daemon no longer writes — which is the exact drift the whole claim
    exists to catch.

    It publishes the **message half** rather than the whole formatted line: the envelope
    carries a millisecond timestamp, and a manifest re-emitted on every claims pass must
    not churn on the clock. The envelope itself is published beside it, because the NOC
    lifts its ``level`` column out of the message text and this is where that token sits.

    ``rendered`` is a statement about the line's **shape**, never about the daemon's
    configuration: its ``threshold``/``window``/``cooldown`` values are the breaker's
    defaults, because the probe reads no config. What the claim asserts is the grammar a
    consumer must match on; the live thresholds are the startup banner's job.
    """
    return {
        "line_class": LINE_CLASS,
        "identifier": IDENTIFIER,
        "logger": BREAKER_LOGGER,
        "envelope": LOG_FORMAT,
        "grammar": TRIP_EVENT,
        "discriminator": f"source={SOURCE}",
        "genuine_level": logging.getLevelName(logging.ERROR),
        "synthetic_level": logging.getLevelName(logging.INFO),
        "rendered": trip_message(synthetic=True),
        "exit_codes": {status: code for status, code in EXIT_CODES.items()},
    }


__all__ = [
    "BREAKER_LOGGER",
    "PACKAGE_LOGGER",
    "COMMAND_TIMEOUT",
    "EXIT_CODES",
    "IDENTIFIER",
    "LINE_CLASS",
    "READBACK_TIMEOUT",
    "SOURCE",
    "SYNTHETIC_AGENT",
    "LogGrammarError",
    "LogGrammarProbe",
    "LogGrammarResult",
    "manifest_detail",
    "capture_trip",
    "render_trip",
    "trip_message",
]

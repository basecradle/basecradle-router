"""The freeze-readability self-test — proving the control surface can be read.

The router honours a NOC **wake-lock** (:mod:`basecradle_router.wakelock`): while
the NOC converges an agent it writes ``/run/basecradle-noc/wake-locks/<slug>.lock``
and the router refuses to wake that agent. The lock is a *freeze* — a safety
control — and it has a demonstrated failure class: **the control exists but
cannot be read when it matters.** On 2026-06-28 a config file replaced with the
wrong owner/group (``root:router 640``) locked the daemon out of its own control
surface; the same shape applied to the wake-lock directory would silently disable
the interlock, because the guard's deliberate fail-direction for an unreadable
lock is to **wake anyway** (wedging every wake on a perms fault would be worse).
Fail-open plus silence is exactly the green-while-absent shape
(basecradle/basecradle#460, incident instance 2): nothing breaks, nothing alerts,
and the freeze simply stops working.

This module makes readability a **provable claim** instead of an assumption. It
reads the surface the same way the daemon does — through the daemon's own
:class:`~basecradle_router.wakelock.WakeLockGuard`, using
:meth:`~basecradle_router.wakelock.WakeLockGuard.inspect` so the probe exercises
the real read-and-parse path without logging refusals for wakes that never
happened — and reports, per file, whether it is readable, parseable, and what
decision it *would* produce right now.

**Three statuses, and why the middle one exists.**

- ``ok`` — the directory is readable and every lock present parses. A **held**
  lock is ``ok``: a live freeze is the control working, not a fault.
- ``degraded`` — the surface is not disproven, but this run could not prove it.
  Three causes: the directory does not exist (the NOC has not created it on this
  box — a lock written later would still be honoured, but a router pointed at the
  *wrong* directory is indistinguishable, so it must not read green); a **stale**
  lock (the NOC died mid-converge — the router wakes, correctly, but a wedged
  converge should be seen); and the probe running as **root**, which bypasses file
  permissions and therefore proves nothing about the daemon's credentials.
- ``failed`` — the surface is broken, loudly, naming the exact file: the directory
  is unreadable, a lock file is unreadable (the ``root:router 640`` class), or a
  lock is malformed (which silently refuses *every* wake for that agent).

**Where it runs, and what "fail" means in each place.** At **daemon boot** it runs
and logs loudly — but it does **not** abort startup. That is deliberate and it is
not a softening: the wake-lock guard's whole fail-direction is to keep waking when
the lock cannot be read, and a boot check that killed the daemon would invert that
decision and turn a permissions typo into a fleet-wide outage. The **fail-closed**
half of the program is Layer 1, and it lives at the NOC: converge runs this same
probe and turns the converge **red**, which is where refusing to proceed is the
correct and safe response. The two compose — loud here, closed there — exactly as
the layered proof model specifies.

It is also exposed as a CLI subcommand (``python -m basecradle_router selftest
freeze``) so the NOC's Layer-3 scheduler can force-exercise it on a TTL and after
every converge, pin change, or secret rotation.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from basecradle_router.wakelock import WakeLockGuard, WakeLockState

logger = logging.getLogger("basecradle_router.selftest")

#: The self-test's verdicts, weakest to strongest — ``_worst`` folds over this order.
OK = "ok"
DEGRADED = "degraded"
FAILED = "failed"
_SEVERITY = (OK, DEGRADED, FAILED)

#: The probe contract's **inconclusive** sentinel: ``EX_TEMPFAIL`` from sysexits, the
#: same vocabulary the NOC op family's ``64`` comes from. The ledger reads exactly
#: three things — ``0`` PASS (the only evidence), this code ERROR/*unprovable* (**we
#: never got an answer**), and any other non-zero FAIL (**we asked; the answer is
#: no**). *Cannot prove* is therefore still red and still immediate: it is
#: distinguished from FAIL by its name and its ledger row, never by being quieter. A
#: softened "could not run" tier is the silent-death shape this whole program exists
#: to kill (basecradle-noc#408, ruling 4).
EXIT_UNPROVABLE = 75

#: Process exit codes for the CLI probe. ``degraded`` maps to the inconclusive
#: sentinel rather than a code of its own, because *no answer* is one state however it
#: arose — a wake-lock directory the NOC has not created yet, a stale lock, a run as
#: root. What must stay distinct is *no answer* from ``failed``: a fresh box must
#: never look identical to a box whose freeze surface is genuinely unreadable. The
#: cause rides on stderr, which the NOC forwards on any non-proven verdict.
EXIT_CODES = {OK: 0, FAILED: 1, DEGRADED: EXIT_UNPROVABLE}

_LOCK_SUFFIX = ".lock"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _worst(statuses: Iterable[str]) -> str:
    return max(statuses, key=_SEVERITY.index, default=OK)


@dataclass(frozen=True, slots=True)
class FreezeCheck:
    """One thing the self-test looked at — a directory, or one agent's lock file.

    ``target`` is the exact path (or ``"process"`` for the credentials check), so a
    ``failed`` result always names the file an operator has to go fix — the single
    property the 2026-06-28 outage's diagnosis lacked. ``state`` is the
    machine-readable finding (``held``, ``unparseable``, ``dir_unreadable``, …);
    ``detail`` is the human sentence.
    """

    target: str
    status: str
    state: str
    detail: str = ""

    def to_json(self) -> dict:
        return {
            "target": self.target,
            "status": self.status,
            "state": self.state,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class FreezeSelfTest:
    """The whole run: an overall status plus the per-target findings.

    ``ran_as`` records the effective user, because *who ran the probe* changes what
    it proves: root bypasses file permissions, so a root run cannot demonstrate
    that the daemon's own credentials reach the surface. ``lock_dir`` is recorded
    for the same reason a check names its file — a router configured for a
    different directory than the NOC writes to is the failure mode that looks
    healthiest.
    """

    status: str
    lock_dir: str
    ran_as: str
    checked_at: str
    checks: tuple[FreezeCheck, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    def problems(self) -> tuple[FreezeCheck, ...]:
        """The checks that are not ``ok``, worst first — what a log line should say."""
        return tuple(
            sorted(
                (c for c in self.checks if c.status != OK),
                key=lambda c: _SEVERITY.index(c.status),
                reverse=True,
            )
        )

    def summary(self) -> str:
        """One line naming the problems (with their files), or stating the all-clear."""
        problems = self.problems()
        if not problems:
            return f"{len(self.checks)} check(s) passed"
        return "; ".join(f"{c.target}: {c.state} ({c.detail})" for c in problems)

    def to_json(self) -> dict:
        return {
            "probe": "freeze-readability",
            "status": self.status,
            "lock_dir": self.lock_dir,
            "ran_as": self.ran_as,
            "checked_at": self.checked_at,
            "summary": self.summary(),
            "checks": [c.to_json() for c in self.checks],
        }


def run_freeze_selftest(
    guard: WakeLockGuard,
    slugs: Iterable[str],
    *,
    now: Callable[[], datetime] = _utc_now,
) -> FreezeSelfTest:
    """Prove — or disprove — that ``guard``'s freeze surface is readable right now.

    ``slugs`` are the registered agents' ``harness_key``s: every one is inspected
    even when it has no lock, because "this agent's lock path is reachable" is the
    claim, and an unreadable *directory* only shows up when you try to open a file
    inside it. Any lock file present for an agent the registry does not know is
    inspected too — a malformed or unreadable stray is the same NOC contract
    violation, and perms faults never stay confined to one file.

    Never raises: this runs at daemon boot, and a self-test that can crash the
    daemon is a worse defect than the blind spot it closes.
    """
    registered = set(slugs)
    entries, dir_check = _scan_directory(guard)
    checks = [_credentials_check(), dir_check]
    for slug in sorted(registered | _stray_slugs(entries, registered)):
        checks.append(_lock_check(guard, slug))
    return FreezeSelfTest(
        status=_worst(c.status for c in checks),
        lock_dir=guard.lock_dir,
        ran_as=_effective_user(),
        checked_at=now().astimezone(timezone.utc).isoformat(),
        checks=tuple(checks),
    )


def _credentials_check() -> FreezeCheck:
    """Whether this run can prove anything about the *daemon's* access to the surface."""
    geteuid = getattr(os, "geteuid", None)  # absent off POSIX: nothing to assert
    if geteuid is not None and geteuid() == 0:
        return FreezeCheck(
            "process",
            DEGRADED,
            "ran_as_root",
            "root bypasses file permissions, so this run cannot prove the daemon's own "
            "credentials can read the surface; re-run as the daemon's user",
        )
    return FreezeCheck("process", OK, "ran_as_user", f"running as {_effective_user()}")


def _scan_directory(guard: WakeLockGuard) -> tuple[list[str], FreezeCheck]:
    """List the wake-lock directory once: its entries, and the check that describes it.

    One ``listdir`` serves both the readability verdict and the stray-lock sweep, so
    a directory whose state changes mid-run cannot produce two findings that
    disagree. Entries are empty on any failure — the failure is already reported in
    the returned check, and reporting it twice would double-count one problem.
    """
    try:
        entries = os.listdir(guard.lock_dir)
    except FileNotFoundError:
        return [], FreezeCheck(
            guard.lock_dir,
            DEGRADED,
            "dir_absent",
            "the wake-lock directory does not exist, so no freeze can be observed; a lock "
            "written here later would still be honoured, but a router configured for the "
            "wrong directory looks identical — compare this path against the one the NOC writes",
        )
    except NotADirectoryError:
        return [], FreezeCheck(
            guard.lock_dir,
            FAILED,
            "dir_not_a_directory",
            "the wake-lock path exists but is not a directory",
        )
    except OSError as exc:
        return [], FreezeCheck(
            guard.lock_dir,
            FAILED,
            "dir_unreadable",
            f"the wake-lock directory cannot be read with these credentials: {exc}",
        )
    locks = [e for e in entries if e.endswith(_LOCK_SUFFIX)]
    return entries, FreezeCheck(
        guard.lock_dir, OK, "dir_readable", f"readable, {len(locks)} lock file(s)"
    )


def _stray_slugs(entries: list[str], known: set[str]) -> set[str]:
    """Slugs with a lock file on disk that the agent registry does not list.

    A junk entry whose stem is not a usable slug (``.lock``, ``..lock``) is dropped
    rather than reported: the guard would call it an unsafe slug and the ledger
    would carry a ``failed`` row for a file that gates nothing.
    """
    found = {e[: -len(_LOCK_SUFFIX)] for e in entries if e.endswith(_LOCK_SUFFIX)}
    return {slug for slug in found - known if slug not in ("", ".", "..")}


def _lock_check(guard: WakeLockGuard, slug: str) -> FreezeCheck:
    """Inspect one agent's lock through the daemon's own read-and-decide path."""
    path = guard.path_for(slug)
    decision = guard.inspect(slug)
    state = decision.state
    if state is WakeLockState.ABSENT:
        return FreezeCheck(path, OK, "absent", "no lock; wakes for this agent are permitted")
    if state is WakeLockState.HELD:
        return FreezeCheck(
            path,
            OK,
            "held",
            f"live freeze honoured until {decision.expires_at}"
            + (f" ({decision.lock_reason})" if decision.lock_reason else ""),
        )
    if state is WakeLockState.STALE:
        return FreezeCheck(
            path,
            DEGRADED,
            "stale",
            f"lock expired at {decision.expires_at} and was never cleared — the NOC may "
            "have died mid-converge; wakes proceed",
        )
    if state is WakeLockState.UNPARSEABLE:
        return FreezeCheck(
            path,
            FAILED,
            "unparseable",
            f"present but malformed ({decision.reason}) — every wake for this agent is "
            "being refused",
        )
    return FreezeCheck(
        path,
        FAILED,
        "unreadable",
        f"present but unreadable with these credentials ({decision.reason}) — the freeze "
        "interlock is failing open for this agent",
    )


def _effective_user() -> str:
    """``name(uid=N)`` for the effective user, or ``"unknown"`` — never raises."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return "unknown"
    uid = geteuid()
    try:
        import pwd

        return f"{pwd.getpwuid(uid).pw_name}(uid={uid})"
    except (ImportError, KeyError):
        return f"uid={uid}"


def log_freeze_selftest(result: FreezeSelfTest) -> None:
    """Emit the one ``key=value`` line for a self-test run — loud when it should be.

    ``ERROR`` on ``failed`` and ``WARNING`` on ``degraded``, so an unreadable freeze
    is never silently ignored, and ``INFO`` on ``ok`` so a healthy boot still states
    positively that the control surface was checked (an absent line is itself a
    finding — the check did not run).
    """
    level = {OK: logging.INFO, DEGRADED: logging.WARNING, FAILED: logging.ERROR}[result.status]
    logger.log(
        level,
        "event=freeze_selftest status=%s lock_dir=%s ran_as=%s checks=%d detail=%s",
        result.status,
        result.lock_dir,
        result.ran_as,
        len(result.checks),
        result.summary(),
    )

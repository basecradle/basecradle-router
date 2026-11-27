"""The shutdown-drain contract, pinned against the shipped unit files (#180).

Draining in-flight wakes on stop/restart is *systemd config*, not Python — none of
it is reachable from a unit test of the daemon, and all of it is silently droppable
by a one-line edit to a `.service` file. The router-AI never deploys, so a regression
here would only be caught on the box, by the NOC, after the fact (a severed wake, or
a reboot that skips because the orchestrator was killed mid-drain). These tests are
the offline gate instead — the same posture as test_log_surface.py — asserting the
exact properties the live drain depends on, against the files as they will ship.

No model, agent, or network — this reads two files off disk.
"""

from __future__ import annotations

import re
from pathlib import Path

_SYSTEMD = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
ROUTER_UNIT = _SYSTEMD / "basecradle-router.service"
REBOOT_UNIT = _SYSTEMD / "basecradle-router-reboot.service"

# systemd time-span units, in seconds. Covers the forms these units use (and then
# some), so a future edit that switches `30min` to `1800s` or `1800` still parses.
_UNIT_SECONDS = {
    "us": 1e-6, "usec": 1e-6,
    "ms": 1e-3, "msec": 1e-3,
    "s": 1, "sec": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}  # fmt: skip


def _to_seconds(span: str) -> float:
    """Parse a systemd time span (e.g. '30min', '300', '1min 30s') to seconds.

    A bare number is seconds (systemd's default for these Timeout* directives).
    """
    total = 0.0
    for token in re.findall(r"(\d+)\s*([a-z]*)", span.strip()):
        value, unit = token
        total += int(value) * _UNIT_SECONDS[unit if unit else "s"]
    assert total > 0, f"could not parse time span {span!r}"
    return total


def _directive(unit: Path, key: str) -> str:
    """The value of the last `Key=value` line for `key` in a unit file (comments ignored)."""
    value = None
    for line in unit.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        m = re.match(rf"{re.escape(key)}\s*=\s*(.+)$", stripped)
        if m:
            value = m.group(1).strip()
    assert value is not None, f"{unit.name} sets no {key}"
    return value


def test_the_drain_kills_only_the_main_process_first() -> None:
    # KillMode=mixed sends the stop SIGTERM to uvicorn (the main process) ONLY, so its
    # lifespan drain runs while the wake subtree (sudo->wake-runner->runuser->claude)
    # keeps working. The systemd default (control-group) SIGTERMs the whole cgroup at
    # t=0 — severing the very wakes the drain exists to protect (#180). SIGKILL still
    # sweeps the cgroup at the stop timeout as the backstop.
    assert _directive(ROUTER_UNIT, "KillMode") == "mixed", (
        "router.service must set KillMode=mixed or the stop SIGTERM severs in-flight wakes"
    )


def test_the_drain_tolerates_a_full_length_wake() -> None:
    # Wakes are deliberately UNBOUNDED (the router sets no wake deadline, #389) and real agent
    # turns run up to ~15 min, so the drain window must comfortably exceed that or a
    # legitimate in-flight wake is SIGKILLed on stop/restart/reboot (#180).
    drain = _to_seconds(_directive(ROUTER_UNIT, "TimeoutStopSec"))
    assert drain >= 20 * 60, (
        f"TimeoutStopSec is {drain:.0f}s — too short to drain a ~15 min wake; the drain "
        "must tolerate a full-length agent turn"
    )


def _exit_statuses(unit: Path, key: str) -> set[str]:
    """Every status a list directive such as ``SuccessExitStatus=`` names, as systemd reads it.

    The directive accumulates across lines and an empty assignment resets the list, so a
    second line could widen it where a last-line read would never see it.
    """
    statuses: set[str] = set()
    for line in unit.read_text().splitlines():
        m = re.match(rf"{re.escape(key)}\s*=(.*)$", line.strip())
        if m:
            values = m.group(1).split()
            statuses = statuses | set(values) if values else set()
    return statuses


def test_a_clean_stop_reads_as_a_success_and_nothing_wider_does() -> None:
    # The main process is `uv run`, and uv reports uvicorn's SIGTERM death (it drains, then
    # re-raises the stop's signal) as exit 143. Unlisted, every clean stop read
    # `Result=exit-code` and a bare stop left the unit `failed` (#559). Exactly 143, so an
    # application error, any other signal death and a stop killed at the timeout still fail.
    assert _exit_statuses(ROUTER_UNIT, "SuccessExitStatus") == {"143"}, (
        "router.service must list exactly SuccessExitStatus=143: without it every clean stop "
        "reads as a failed unit, and anything wider hides a real failure"
    )


def test_a_sigterm_outside_a_stop_still_restarts_the_daemon() -> None:
    # A clean exit is never restarted by Restart=on-failure, so listing 143 as a success
    # alone would leave the fleet's only wake path down after a SIGTERM from outside a stop
    # job. Forcing the restart on 143 keeps it; a stop or restart job never restarts (#559).
    assert _directive(ROUTER_UNIT, "Restart") == "on-failure"
    assert _exit_statuses(ROUTER_UNIT, "RestartForceExitStatus") == {"143"}, (
        "router.service must list RestartForceExitStatus=143, or a stray SIGTERM leaves the "
        "daemon down where it used to restart"
    )


def test_no_drop_in_can_rewrite_the_router_units_exit_statuses() -> None:
    # The exit-status tests read the unit file alone. A drop-in the deploy installed beside it
    # could widen or reset either list and they would still pass, so none may ship.
    assert not (_SYSTEMD / "basecradle-router.service.d").exists(), (
        "a basecradle-router.service.d drop-in would escape the exit-status tests; read it "
        "into them before shipping one"
    )


def test_the_reboot_orchestrator_outlasts_the_drain() -> None:
    # The reboot orchestrator (reboot-if-required.sh) blocks on `systemctl stop
    # basecradle-router`, which blocks on the drain above. Its own TimeoutStartSec must
    # stay ABOVE the drain bound, or systemd kills the orchestrator mid-drain — skipping
    # the reboot and marking the unit failed, so a staged kernel patch never lands (#180,
    # #66). This coupling is enforced only by a comment in the unit; this test is the guard.
    #
    # Since #557 the drain is preceded by a bounded wait for an idle router, so the bound
    # the orchestrator must outlast is the whole run end to end: the reboot that will not
    # defer again waits the full wait, can start one last reading up to a poll later, lets
    # that reading run to its timeout and the kill after it, and then drains.
    drain = _to_seconds(_directive(ROUTER_UNIT, "TimeoutStopSec"))
    orchestrator = _to_seconds(_directive(REBOOT_UNIT, "TimeoutStartSec"))
    wait = _script_default("REBOOT_IDLE_WAIT_S")
    poll = _script_default("REBOOT_IDLE_POLL_S")
    reading = _script_default("REBOOT_READ_TIMEOUT_S") + _kill_after()
    assert orchestrator > wait + poll + reading + drain, (
        f"reboot TimeoutStartSec ({orchestrator:.0f}s) must exceed the idle wait ({wait}s), "
        f"one more poll ({poll}s) and reading ({reading}s) and router TimeoutStopSec "
        f"({drain:.0f}s) together, or systemd kills the reboot orchestrator mid-wait or "
        "mid-drain"
    )


REBOOT_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "bin" / "reboot-if-required.sh"


def _script_default(name: str) -> int:
    """The default the shipped reboot script gives ``name`` (``NAME="${NAME:-<n>}"``)."""
    match = re.search(
        rf'^{name}="\$\{{{name}:-([0-9]+)\}}"$', REBOOT_SCRIPT.read_text(), re.MULTILINE
    )
    assert match, f"{name} has no numeric default in {REBOOT_SCRIPT.name}"
    return int(match.group(1))


def _kill_after() -> int:
    """The grace ``timeout`` gives a reading past its bound before it kills it."""
    match = re.search(r"timeout --kill-after=([0-9]+) ", REBOOT_SCRIPT.read_text())
    assert match, f"the idle reading in {REBOOT_SCRIPT.name} is not bounded by timeout"
    return int(match.group(1))

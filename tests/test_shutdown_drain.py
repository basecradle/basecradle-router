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
    # Wakes are deliberately UNBOUNDED (HomeServerWaker timeout=None) and real agent
    # turns run up to ~15 min, so the drain window must comfortably exceed that or a
    # legitimate in-flight wake is SIGKILLed on stop/restart/reboot (#180).
    drain = _to_seconds(_directive(ROUTER_UNIT, "TimeoutStopSec"))
    assert drain >= 20 * 60, (
        f"TimeoutStopSec is {drain:.0f}s — too short to drain a ~15 min wake; the drain "
        "must tolerate a full-length agent turn"
    )


def test_the_reboot_orchestrator_outlasts_the_drain() -> None:
    # The reboot orchestrator (reboot-if-required.sh) blocks on `systemctl stop
    # basecradle-router`, which blocks on the drain above. Its own TimeoutStartSec must
    # stay ABOVE the drain bound, or systemd kills the orchestrator mid-drain — skipping
    # the reboot and marking the unit failed, so a staged kernel patch never lands (#180,
    # #66). This coupling is enforced only by a comment in the unit; this test is the guard.
    drain = _to_seconds(_directive(ROUTER_UNIT, "TimeoutStopSec"))
    orchestrator = _to_seconds(_directive(REBOOT_UNIT, "TimeoutStartSec"))
    assert orchestrator > drain, (
        f"reboot TimeoutStartSec ({orchestrator:.0f}s) must exceed router TimeoutStopSec "
        f"({drain:.0f}s), or systemd kills the reboot orchestrator mid-drain"
    )

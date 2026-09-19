"""The daemon's start keeps no durable uv cache — pinned offline (#280).

``/home/router/.cache/uv`` had an owner and a home but no end: the unit's start-time
``uv run`` wrote it on every restart, and nothing ever removed it. The unit now sets
``UV_NO_CACHE=1``, so each start's cache is a throwaway one uv removes when it exits.
Dropping the variable changes nothing a unit test of the daemon can see, and the
router-AI never deploys, so only the box would notice, as a cache that quietly comes
back. This is the offline gate instead.

No model, agent, or network — this reads one file off disk.
"""

from __future__ import annotations

import shlex
from pathlib import Path

UNIT = Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "basecradle-router.service"


def _environment_assignments() -> list[str]:
    # One Environment= line may carry several space-separated, optionally quoted
    # assignments, so split each the way systemd does rather than matching the line.
    return [
        assignment
        for line in UNIT.read_text().splitlines()
        if line.startswith("Environment=")
        for assignment in shlex.split(line.removeprefix("Environment="))
    ]


def test_the_daemon_start_keeps_no_durable_uv_cache() -> None:
    assert "UV_NO_CACHE=1" in _environment_assignments()

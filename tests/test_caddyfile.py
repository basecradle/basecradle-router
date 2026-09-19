"""The ingress retry contract, pinned against the shipped Caddyfile (#264).

Every merge to main restarts the daemon, and while uvicorn's listener is down Caddy's
dial is refused. Holding a delivery across that gap is Caddy config, not Python, so no
unit test of the daemon can reach it, and a one-line edit can drop it silently. The
router-AI never deploys, so a regression would surface only as lost webhooks on the
box. This is the offline gate instead, in the same posture as test_shutdown_drain.py:
it asserts the properties the live retry depends on, against the file as it ships.

No network or Caddy binary is involved. This test reads one file off disk.
"""

from __future__ import annotations

import re
from pathlib import Path

CADDYFILE = Path(__file__).resolve().parents[1] / "deploy" / "caddy" / "Caddyfile"

#: GitHub abandons a webhook delivery that has not been answered within 10 s.
GITHUB_DELIVERY_TIMEOUT = 10.0

# Go duration units (what Caddy's durations are), in seconds.
_DURATION_UNITS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600}


def _to_seconds(duration: str) -> float:
    """Parse a Caddy (Go) duration such as '5s', '1500ms' or '1m30s' to seconds."""
    parts = re.findall(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)", duration)
    assert parts and "".join(n + u for n, u in parts) == duration, f"bad duration {duration!r}"
    return sum(float(n) * _DURATION_UNITS[u] for n, u in parts)


def _reverse_proxy_block() -> list[str]:
    """The directive lines inside the daemon's ``reverse_proxy`` block, comments dropped."""
    lines = [line.strip() for line in CADDYFILE.read_text().splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    start = next(i for i, line in enumerate(lines) if line.startswith("reverse_proxy "))
    assert lines[start].endswith("{"), "reverse_proxy has no block, so it sets no retry"
    depth, block = 0, []
    for line in lines[start:]:
        depth += line.count("{") - line.count("}")
        block.append(line)
        if depth == 0:
            return block[1:-1]
    raise AssertionError("reverse_proxy block is never closed")


def _directive(block: list[str], name: str) -> str | None:
    values = [line.split(None, 1)[1] for line in block if line.split()[0] == name]
    assert len(values) <= 1, f"{name} is set more than once"
    return values[0] if values else None


def test_the_proxy_holds_a_delivery_across_a_restart() -> None:
    # Without it a delivery arriving while the listener is down is answered 502 at
    # once, and GitHub never redelivers it, so the wake it carried is lost.
    duration = _directive(_reverse_proxy_block(), "lb_try_duration")
    assert duration is not None, "reverse_proxy must set lb_try_duration (#264)"
    assert _to_seconds(duration) >= 1.0, (
        f"lb_try_duration {duration} is too short to cover a daemon restart"
    )


def test_a_held_delivery_is_still_answered_inside_githubs_timeout() -> None:
    # A hold longer than GitHub's timeout buys nothing: GitHub gives up first and
    # records a failure anyway, and meanwhile Caddy keeps the request open.
    duration = _to_seconds(_directive(_reverse_proxy_block(), "lb_try_duration") or "0s")
    assert duration < GITHUB_DELIVERY_TIMEOUT, (
        f"lb_try_duration {duration:.1f}s reaches GitHub's {GITHUB_DELIVERY_TIMEOUT:.0f}s "
        "delivery timeout"
    )


def test_only_a_failed_dial_is_retried_so_no_delivery_runs_twice() -> None:
    # Caddy's default retries a non-GET only when the dial failed, which means the
    # request never reached the daemon. An lb_retry_match would widen that to a POST
    # that reached the daemon and then failed, and so could deliver one webhook twice.
    assert _directive(_reverse_proxy_block(), "lb_retry_match") is None, (
        "lb_retry_match would let Caddy re-send a delivery the daemon already received"
    )

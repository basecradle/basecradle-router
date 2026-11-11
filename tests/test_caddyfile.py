"""The ingress retry contract, pinned against the shipped Caddyfile (#264).

Every merge to main restarts the daemon, and while uvicorn's listener is down Caddy's
dial is refused. Holding a delivery across that gap is Caddy config, not Python, so no
unit test of the daemon can reach it, and a one-line edit can drop it silently. The
router-AI never deploys, so a regression would surface only as lost webhooks on the
box. This is the offline gate instead, in the same posture as test_shutdown_drain.py:
it asserts the properties the live retry depends on, against the file as it ships.

Every host's Caddyfile is checked (#326): each box fronts its own daemon, so each one
must hold a delivery across that daemon's restart.

No network or Caddy binary is involved. This test reads files off disk.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CADDYFILES = sorted((Path(__file__).resolve().parents[1] / "deploy" / "hosts").glob("*/Caddyfile"))

#: GitHub abandons a webhook delivery that has not been answered within 10 s.
GITHUB_DELIVERY_TIMEOUT = 10.0

# Go duration units (what Caddy's durations are), in seconds.
_DURATION_UNITS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600}


def _to_seconds(duration: str) -> float:
    """Parse a Caddy (Go) duration such as '5s', '1500ms' or '1m30s' to seconds."""
    parts = re.findall(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)", duration)
    assert parts and "".join(n + u for n, u in parts) == duration, f"bad duration {duration!r}"
    return sum(float(n) * _DURATION_UNITS[u] for n, u in parts)


def _directive_lines(caddyfile: Path) -> list[str]:
    """Every directive line, stripped, with blank lines and comments dropped."""
    lines = [line.strip() for line in caddyfile.read_text().splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def _block_at(lines: list[str], start: int) -> tuple[list[str], int]:
    """The lines inside the block opened at ``lines[start]``, and the index just past it.

    The one brace walk every helper here uses. It counts braces per line, which is enough
    for these files: none puts a brace inside a quoted value.
    """
    assert lines[start].endswith("{"), f"no block opens at {lines[start]!r}"
    depth = 0
    for end in range(start, len(lines)):
        depth += lines[end].count("{") - lines[end].count("}")
        if depth == 0:
            return lines[start + 1 : end], end + 1
    raise AssertionError(f"the block opened at {lines[start]!r} is never closed")


def _reverse_proxy_block(caddyfile: Path) -> list[str]:
    """The directive lines inside the daemon's ``reverse_proxy`` block, comments dropped."""
    lines = _directive_lines(caddyfile)
    start = next(i for i, line in enumerate(lines) if line.startswith("reverse_proxy "))
    assert lines[start].endswith("{"), "reverse_proxy has no block, so it sets no retry"
    return _block_at(lines, start)[0]


def _directive(block: list[str], name: str) -> str | None:
    values = [line.split(None, 1)[1] for line in block if line.split()[0] == name]
    assert len(values) <= 1, f"{name} is set more than once"
    return values[0] if values else None


def test_the_glob_found_every_hosts_caddyfile() -> None:
    # An empty glob would parametrize the tests below over nothing and pass them all.
    assert len(CADDYFILES) >= 2, "deploy/hosts/ carries suspiciously few Caddyfiles"


@pytest.mark.parametrize("caddyfile", CADDYFILES, ids=lambda p: p.parent.name)
def test_the_proxy_holds_a_delivery_across_a_restart(caddyfile: Path) -> None:
    # Without it a delivery arriving while the listener is down is answered 502 at
    # once, and GitHub never redelivers it, so the wake it carried is lost.
    duration = _directive(_reverse_proxy_block(caddyfile), "lb_try_duration")
    assert duration is not None, "reverse_proxy must set lb_try_duration (#264)"
    assert _to_seconds(duration) >= 1.0, (
        f"lb_try_duration {duration} is too short to cover a daemon restart"
    )


@pytest.mark.parametrize("caddyfile", CADDYFILES, ids=lambda p: p.parent.name)
def test_a_held_delivery_is_still_answered_inside_githubs_timeout(caddyfile: Path) -> None:
    # A hold longer than GitHub's timeout buys nothing: GitHub gives up first and
    # records a failure anyway, and meanwhile Caddy keeps the request open.
    duration = _to_seconds(_directive(_reverse_proxy_block(caddyfile), "lb_try_duration") or "0s")
    assert duration < GITHUB_DELIVERY_TIMEOUT, (
        f"lb_try_duration {duration:.1f}s reaches GitHub's {GITHUB_DELIVERY_TIMEOUT:.0f}s "
        "delivery timeout"
    )


@pytest.mark.parametrize("caddyfile", CADDYFILES, ids=lambda p: p.parent.name)
def test_only_a_failed_dial_is_retried_so_no_delivery_runs_twice(caddyfile: Path) -> None:
    # Caddy's default retries a non-GET only when the dial failed, which means the
    # request never reached the daemon. An lb_retry_match would widen that to a POST
    # that reached the daemon and then failed, and so could deliver one webhook twice.
    assert _directive(_reverse_proxy_block(caddyfile), "lb_retry_match") is None, (
        "lb_retry_match would let Caddy re-send a delivery the daemon already received"
    )


# --- the edge hardening (#428) -------------------------------------------------------------
#
# Each property below was measured against Caddy 2.11.4 (the boxes' build) in Docker before it
# was pinned here: the property is Caddy's behaviour, so the file is all an offline test can
# hold, and these hold the lines that produce it.


def _top_level_blocks(caddyfile: Path) -> dict[str, list[str]]:
    """Each top-level block's directive lines, keyed by its address ("" for global options)."""
    lines = _directive_lines(caddyfile)
    blocks: dict[str, list[str]] = {}
    i = 0
    while i < len(lines):
        head = lines[i]
        assert head.endswith("{"), f"top-level line outside any block: {head!r}"
        address = head[:-1].strip()
        assert address not in blocks, f"block {address!r} is declared twice"
        blocks[address], i = _block_at(lines, i)
    return blocks


def _depth_one(block: list[str]) -> list[str]:
    """The lines of ``block`` that sit directly in it, not inside a nested block."""
    out, i = [], 0
    while i < len(block):
        out.append(block[i])
        i = _block_at(block, i)[1] if block[i].endswith("{") else i + 1
    return out


def _site(caddyfile: Path) -> list[str]:
    """The host's own TLS site block: the one named for the host's FQDN."""
    return _top_level_blocks(caddyfile)[caddyfile.parent.name]


@pytest.mark.parametrize("caddyfile", CADDYFILES, ids=lambda p: p.parent.name)
def test_the_file_has_exactly_the_global_options_the_site_and_the_port_80_catch_all(
    caddyfile: Path,
) -> None:
    assert set(_top_level_blocks(caddyfile)) == {"", caddyfile.parent.name, "http://"}


@pytest.mark.parametrize("caddyfile", CADDYFILES, ids=lambda p: p.parent.name)
def test_the_site_sends_hsts_for_its_own_name_only(caddyfile: Path) -> None:
    # F-002. Two years; no includeSubDomains, because the box serves one name and does not
    # own the names below it; no preload, which is a commitment for a registrable domain.
    # Set directly in the site, so it covers every answer the site gives, and touched
    # nowhere else, so nothing nested can delete or override it.
    pattern = r'header Strict-Transport-Security "max-age=(\d+)"'
    direct = [line for line in _depth_one(_site(caddyfile)) if re.fullmatch(pattern, line)]
    assert len(direct) == 1, "the site must set HSTS once, directly, not inside a nested block"
    assert int(re.fullmatch(pattern, direct[0]).group(1)) >= 365 * 24 * 3600
    touching = [line for line in _directive_lines(caddyfile) if "strict-transport" in line.lower()]
    assert touching == direct, f"another line touches the HSTS header: {touching}"


@pytest.mark.parametrize("caddyfile", CADDYFILES, ids=lambda p: p.parent.name)
def test_a_foreign_host_under_this_boxs_sni_is_refused(caddyfile: Path) -> None:
    # F-005, port 443: without strict_sni_host a foreign Host matched no site and Caddy
    # answered an empty 200. With it, Caddy answers 421 before any site is consulted.
    assert _top_level_blocks(caddyfile)[""] == ["servers {", "strict_sni_host on", "}"]


@pytest.mark.parametrize("caddyfile", CADDYFILES, ids=lambda p: p.parent.name)
def test_port_80_refuses_every_name_but_its_own_and_never_reflects_a_host(
    caddyfile: Path,
) -> None:
    # F-005, port 80: the catch-all answers 421 with no redirect, so Caddy's automatic
    # redirect is left to serve only the managed name. That automatic redirect is what
    # keeps this box's own http:// working, so nothing may switch it off.
    assert _top_level_blocks(caddyfile)["http://"] == ["respond 421"]
    assert not any(line.startswith("auto_https") for line in _directive_lines(caddyfile))
    assert not any(line.startswith("redir") for line in _site(caddyfile))


#: Where the Caddyfile sends the probe path. The daemon must answer it as an unknown source,
#: so it must name no route the daemon can register.
PROBE_MASK_TARGET = "/webhooks/probe-masked"


@pytest.mark.parametrize("caddyfile", CADDYFILES, ids=lambda p: p.parent.name)
def test_the_probe_path_is_rewritten_to_a_path_the_daemon_answers_as_unknown(
    caddyfile: Path,
) -> None:
    # F-009. Answering the probe path at the edge gave Caddy's empty 404 against the
    # daemon's JSON one, which told them apart. Rewriting it means the daemon's own
    # unknown-source code writes both answers, so status, body and headers are identical.
    # The path itself (and a trailing slash) goes to the one-segment target, answered as
    # `/webhooks/<unknown>`; anything below it goes below the target, answered as
    # `/webhooks/<unknown>/<anything>`. Each answer then matches its unknown-source twin.
    site = _site(caddyfile)
    probe = [line for line in site if "probe" in line]
    assert probe == [
        "@probe path /webhooks/probe /webhooks/probe/",
        f"rewrite @probe {PROBE_MASK_TARGET}",
        "@probe_below path /webhooks/probe/*",
        f"rewrite @probe_below {PROBE_MASK_TARGET}/below",
    ]
    assert not any(line.startswith("respond") for line in site), (
        "an edge-written answer is distinguishable from the daemon's unknown-source answer"
    )


def test_the_probe_mask_target_names_no_route_the_daemon_can_register(tmp_path) -> None:
    # Asked of the daemon's own composition root, with every route enabled AND the target's
    # own name enabled with a secret: if any code path could register a route by that name,
    # it would be in this registry, and the public probe path would reach it.
    import json

    from basecradle_router.app import build_registry
    from basecradle_router.config import load_config, route_secret_var

    source = PROBE_MASK_TARGET.removeprefix("/webhooks/")
    assert source and "/" not in source, "the target must be one segment, as a source is"
    agents = tmp_path / "agents.json"
    agents.write_text(
        json.dumps(
            {
                "basecradle/basecradle-python": {
                    "os_user": "nova",
                    "clone_path": "/home/nova/basecradle-python",
                    "bot_slug": "basecradle-python-ai",
                }
            }
        )
    )
    secret = "fake-webhook-secret-" + "0" * 32
    env = {
        "BASECRADLE_ROUTER_AGENTS": str(agents),
        "BASECRADLE_ROUTER_ENABLED_ROUTES": f"github,basecradle,probe,{source}",
        "BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS": "john",
        "BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET": secret,
        "BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET": secret,
        "BASECRADLE_ROUTER_PROBE_WEBHOOK_SECRET": secret,
        route_secret_var(source): secret,
    }
    registry = build_registry(load_config(env), env)
    assert {"github", "basecradle", "probe"} <= registry.names()
    assert source not in registry


@pytest.mark.parametrize("caddyfile", CADDYFILES, ids=lambda p: p.parent.name)
def test_the_proxy_line_keeps_the_shape_the_capitals_fleet_audit_reads(
    caddyfile: Path,
) -> None:
    # audit-fleet-server (basecradle/basecradle, provision-fleet-server) tells the per-host
    # router Caddyfile from the bootstrap responder by grepping `^\s*reverse_proxy
    # 127.0.0.1:8000`, so a reshaped proxy line would read as a box with no router front end.
    assert any(re.match(r"\s*reverse_proxy 127\.0\.0\.1:8000", line) for line in _site(caddyfile))

"""The deploy-side half of the log surface, pinned against the shipped bytes (#170).

Naming the program, exporting the delivery id across the privilege drop, and the
Vector presentation/scrub are all *config*, not Python — none of it is reachable from
a unit test of the daemon, and all of it is silently droppable by a refactor. The
router-AI never deploys, so a regression here would only be caught on the box, by the
NOC, after the fact. These tests are the offline gate instead: they assert the exact
properties the live behaviour depends on, against the files as they will ship.

No model, agent, or network — this reads three files off disk.
"""

from __future__ import annotations

import re
from pathlib import Path

_DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
UNIT = _DEPLOY / "systemd" / "basecradle-router.service"
WAKE_RUNNER = _DEPLOY / "bin" / "wake-runner"
VECTOR = _DEPLOY / "vector.yaml"


# --- 1. name the program ---------------------------------------------------


def test_the_daemon_names_itself_in_the_journal() -> None:
    # The daemon logs to stdout under `uv run uvicorn`, so with no SyslogIdentifier
    # journald stamps every line SYSLOG_IDENTIFIER=uv — the router's own log was
    # attributed to its *launcher*, and no `journalctl -t` could address it. This is
    # also what vector.yaml prefixes each shipped line with, so losing it makes every
    # Live Tail line from this daemon read `[uv] …`.
    assert "SyslogIdentifier=basecradle-router" in UNIT.read_text()


# --- 2. the delivery id crosses the privilege drop -------------------------


def test_wake_runner_accepts_the_delivery_flag() -> None:
    script = WAKE_RUNNER.read_text()
    assert "--delivery)" in script, "wake-runner must parse --delivery <id>"
    assert 'delivery="$2"' in script


def test_wake_runner_exports_the_delivery_id_to_the_agent() -> None:
    # The router passes NO environment across the sudo boundary (that is the whole
    # security posture), so the only way the delivery id reaches the child is the
    # wrapper exporting it after the privilege drop. Without this the harness cannot
    # stamp its own lines with the id, and the two journals stay unjoinable.
    assert 'export BASECRADLE_DELIVERY_ID="$1"' in WAKE_RUNNER.read_text()


def test_the_delivery_id_is_exported_after_the_agent_env_loads() -> None:
    # Order is load-bearing: the router's value must win over a stale one left in the
    # agent's own agent.env, or a wake would report the id of some previous wake.
    script = WAKE_RUNNER.read_text()
    assert script.index('load_agent_env "$env_file"') < script.index(
        'export BASECRADLE_DELIVERY_ID="$1"'
    ), "BASECRADLE_DELIVERY_ID must be exported AFTER agent.env loads, so it cannot be overridden"


def test_wake_runner_shape_checks_the_delivery_id() -> None:
    # It crosses a sudo boundary as an argv element and lands in the journal. It is
    # never eval'd (the agent's bash assigns it literally), so this is defence in
    # depth — but the wrapper validates every input it is handed, without exception.
    assert re.search(r"\$delivery =~ \^\[A-Za-z0-9_\.:-\]\{1,128\}\$", WAKE_RUNNER.read_text()), (
        "wake-runner must pin --delivery to an inert charset"
    )


def test_the_delivery_id_is_passed_as_an_inert_positional_never_interpolated() -> None:
    # Same discipline as the trigger: root must never interpolate a caller-supplied
    # value into the shell it runs as the agent. It rides as a positional argument to
    # the single-quoted inner script, where it is only ever assigned, never evaluated.
    assert '\' wake-runner "$real_cwd" "$delivery" "$launch_bin" "$@"' in WAKE_RUNNER.read_text()


# --- 3. Vector presentation + scrub ----------------------------------------


def _scrub_source() -> str:
    """The body of the `ai_scrub` remap — the transform every shipped log passes."""
    text = VECTOR.read_text()
    start = text.index("ai_scrub:")
    return text[start : text.index("sinks:", start)]


def test_every_shipped_line_is_prefixed_with_the_emitting_programs_identifier() -> None:
    # The presentation half: a Live Tail line reads `[basecradle-router] …` or
    # `[basecradle-wake-jt] …`, so a human can see which program on the box emitted it.
    # Done in Vector, once, so it covers EVERY program (sshd, Caddy, a wake) — never
    # hand-prefixed into the daemon's own log strings, which would cover only the daemon.
    source = _scrub_source()
    assert '.message = "[" + ident + "] " + msg' in source
    assert "ident = to_string(.SYSLOG_IDENTIFIER)" in source
    # A line with no identifier (a kernel message) must still get a prefix, never `[] `.
    assert 'ident = "unknown"' in source


def test_the_prefix_is_applied_after_redaction_not_before() -> None:
    # The prefix is not secret-bearing; redaction must run on the raw message and the
    # prefix must never be scanned or mangled by a later pattern.
    source = _scrub_source()
    assert source.index("[REDACTED_API_KEY]") < source.index('.message = "[" + ident + "] " + msg')


def test_provider_api_keys_are_redacted() -> None:
    # Harness and builder agents run with provider keys in their env, and a wake's
    # stdout+stderr now flows into journald (#168) and therefore through here. Nothing
    # is known to print one; this is the belt for the braces.
    source = _scrub_source()
    assert r"sk-[A-Za-z0-9_-]{16,}" in source  # openai / anthropic / openrouter / …
    assert r"xai-[A-Za-z0-9]{16,}" in source
    assert r"AIza[A-Za-z0-9_-]{20,}" in source


def test_the_pre_existing_scrub_rules_survive() -> None:
    # The redaction set only ever grows: #33/#338 added these after a live journald
    # secret leak, and a refactor of this transform must not quietly drop one.
    source = _scrub_source()
    for pattern in ("REDACTED_GH_TOKEN", "REDACTED_HEARTBEAT_URL", "REDACTED_BC_TOKEN"):
        assert pattern in source
    assert 'SYSLOG_IDENTIFIER == "sudo"' in source  # the whole-event drop for argv leaks

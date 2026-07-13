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


def _component(component_id: str) -> str:
    """The YAML body of one vector.yaml component, bounded by the next key at its own indent.

    Bounding a component's body with a *named sibling* is a trap this file already fell into:
    `_host_metrics_source` used to slice to `transforms:`, and inserting `ai_internal_metrics`
    ahead of it silently widened the slice so a sibling component could satisfy an assertion
    meant for this one. Naming the new sibling instead would only move the bug one component
    along, and a slice whose end lands *before* its start collapses to `""` — where every
    `not in` assertion passes vacuously and the test dies silently. The next same-indent key
    is the real boundary, whatever it happens to be called.
    """
    text = VECTOR.read_text()
    key = re.search(rf"^(?P<indent> *){re.escape(component_id)}:[ \t]*$", text, re.MULTILINE)
    assert key, f"vector.yaml defines no component {component_id!r}"
    body = text[key.end() :]
    end = re.search(rf"^ {{0,{len(key.group('indent'))}}}\S", body, re.MULTILINE)
    return body[: end.start()] if end else body


def _scrub_source() -> str:
    """The body of the `ai_scrub` remap — the transform every shipped log passes."""
    return _component("ai_scrub")


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


# --- 4. host_metrics does not scrape what it cannot read -------------------


def _host_metrics_source() -> str:
    """The body of the `ai_host_metrics` source — the box's metrics collector."""
    return _component("ai_host_metrics")


def test_host_metrics_excludes_the_pseudo_filesystems_it_cannot_read() -> None:
    # The `filesystem` collector stats every mount in /proc/mounts, and the unprivileged
    # `vector` user cannot reach /sys/kernel/debug/tracing (tracefs, under a 0700 debugfs):
    # every 30s scrape logged a `statvfs` permission-denied ERROR — ~2,880/day into the
    # crown-jewels box's journal, which is exactly how a human learns to skim past ERROR
    # (basecradle#414). Vector checks these excludes BEFORE the statvfs, so the syscall is
    # never attempted. Dropping them re-arms the error carpet, and the router-AI never
    # deploys — nobody would notice until the NOC read the box.
    source = _host_metrics_source()
    for fs in ("tracefs", "debugfs"):
        assert re.search(rf"""excludes:.*["']{fs}["']""", source), (
            f"host_metrics must exclude the unreadable pseudo-filesystem {fs!r}"
        )


def test_host_metrics_still_collects_real_filesystems() -> None:
    # The other way to silence the ERROR was to drop the `filesystem` collector entirely —
    # rejected, because disk-usage metrics are the point (a full disk is how this box dies).
    # The fix must stay surgical: exclude the pseudo-filesystems, keep the collector. An
    # excludes-only list includes everything else, so every real filesystem is still scraped.
    source = _host_metrics_source()
    assert "filesystem," in source or "filesystem]" in source, (
        "the `filesystem` collector must remain enabled — the fix is to exclude "
        "unreadable pseudo-filesystems, not to stop collecting disk usage"
    )


# --- 5. Vector's own health is visible to the NOC (basecradle#419) ----------
#
# Vector is the transport that ships this box's entire log stream, and it is the one
# component nothing watches: its own logs are deliberately DROPPED from the stream
# (they echo the sink token — basecradle#338), and `introspect-vector` reads only
# "installed and running", which a running-but-BROKEN Vector satisfies perfectly. A VRL
# aborting 100% of events logs nothing, passes `vector validate`, and shows `active`
# while the sink receives zero. These counters are the only thing that sees that.


def test_the_health_component_ids_are_a_contract_with_the_noc() -> None:
    # NOT cosmetic, and the reason `_component` anchors on the YAML key rather than searching
    # the file for the bare string: the NOC's wrapper carries a reviewed allow-list and will
    # never name an id that is not on it — because on a Better Stack *generated* config the
    # component id IS the ingest token (that is how it leaked ~40x). Renaming either id here
    # does not fail loudly; it surfaces on the box as an `unknown_components` finding and
    # needs a change in a repo we do not own to clear. A comment that merely mentions the id
    # must not be able to satisfy this.
    for component_id in ("ai_internal_metrics", "ai_vector_health_exporter"):
        assert _component(component_id), (
            f"{component_id!r} is allow-listed by the NOC's introspect-vector-health op — "
            "renaming it silently strands the guard (basecradle-noc#215)"
        )


def test_vector_exports_its_own_health_counters() -> None:
    # The source of truth for received/sent/discarded. Without it the exporter below has
    # nothing to serve and the NOC's guard reads `endpoint_reachable: false` forever.
    assert re.search(r"""type:\s*["']internal_metrics["']""", _component("ai_internal_metrics")), (
        "`ai_internal_metrics` must be an `internal_metrics` source"
    )


def test_the_health_exporter_is_bound_to_localhost_only() -> None:
    # This is not a network service. The only reader is a root-owned wrapper op on this same
    # machine, pulling out-of-band. Binding 0.0.0.0 would publish the box's internals to the
    # internet — the box holds the fleet's crown jewels.
    exporter = _component("ai_vector_health_exporter")
    assert re.search(r"""address:\s*["']127\.0\.0\.1:9598["']""", exporter), (
        "the health exporter must bind 127.0.0.1 — never 0.0.0.0"
    )


def test_the_health_counters_are_pulled_out_of_band_not_shipped() -> None:
    # A Vector whose SINK is broken cannot ship the metric that says its sink is broken — a
    # self-report through the failing channel is not a monitor. So the counters are exposed
    # on localhost and PULLED by the NOC over a channel independent of the pipe under test.
    # Routing them into a Better Stack sink would make the guard blind in the exact case it
    # exists to catch.
    exporter = _component("ai_vector_health_exporter")
    assert re.search(r"""inputs:\s*\[["']ai_internal_metrics["']\]""", exporter), (
        "the exporter must serve the `ai_internal_metrics` counters"
    )
    for sink in ("better_stack_logs", "better_stack_metrics"):
        assert "ai_internal_metrics" not in _component(sink), (
            f"the health counters must not be shipped through {sink} — a broken transport "
            "cannot report its own breakage through itself (basecradle-noc#215)"
        )


def test_the_health_counters_never_enter_the_log_scrub() -> None:
    # `ai_scrub` is log-shaped: it rewrites `.timestamp` -> `.dt` and reads log-only fields.
    # These are METRIC events; routing them through it would corrupt them. Same reasoning
    # already applies to `ai_host_metrics`, which is likewise kept out of the scrub.
    assert "ai_internal_metrics" not in _scrub_source(), (
        "`ai_internal_metrics` is metric events — the log scrub would corrupt them"
    )

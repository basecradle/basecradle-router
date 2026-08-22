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
    # The privilege drop is assembled once into `drop` and launched two ways (direct for
    # a probe, journald-wrapped for a real wake — basecradle-router#208), so this pins
    # the one assembly rather than each launch.
    assert (
        'drop=(runuser -u "$user" -- /bin/bash -c "$AGENT_SCRIPT" '
        'wake-runner "$real_cwd" "$delivery" "$launch_bin" "$@")'
    ) in WAKE_RUNNER.read_text()


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


def _statement_indent(source: str, statement: str) -> int:
    """The leading-whitespace width of the line `statement` starts, asserting it is there.

    Used to tell a top-level VRL statement from one nested inside a conditional, without
    hard-coding the block's own indentation (which is a YAML detail, not a security one).
    """
    match = re.search(rf"^(?P<indent> *){re.escape(statement)}", source, re.MULTILINE)
    assert match, f"ai_scrub has no statement {statement!r}"
    return len(match.group("indent"))


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


def test_the_process_command_line_is_never_shipped() -> None:
    # journald attaches the emitting process's full argv as the `_CMDLINE` metadata FIELD,
    # and Vector ships the whole journal record — so every redaction above, which operates
    # on `msg`, is blind to it. That gap put all seven harness agents' credentials into the
    # Better Stack store: the NOC's fleet-deploy-runner hands each agent's whole agent.env
    # to `runuser -u <agent> -- env -i KEY=VALUE …` on argv (basecradle-noc#443).
    #
    # The router's own launch paths are clean — wake-runner passes NO environment across the
    # sudo boundary — but this box's scrub guards every program on it, not only ours.
    source = _scrub_source()
    assert 'del(."_CMDLINE")' in source, (
        "ai_scrub must delete the `_CMDLINE` journald field before the logs sink — "
        "any secret on any program's argv ships in it, unredacted (basecradle-noc#443)"
    )


def test_the_command_line_drop_is_unconditional_not_an_identifier_allow_list() -> None:
    # The pre-existing `sudo` rule is an identifier allow-list, and `runuser` is not `sudo` —
    # which is precisely how the leak above got past it. Guarding the field holds for programs
    # this repo has never heard of; guarding a list of names holds only until the next one.
    # Pinned because the tempting "fix" is to narrow this to the identifier we caught, which
    # would restore the exact hole. The router-AI never deploys, so a regression here is only
    # visible on the box, in the log store, after the secrets are already in it.
    #
    # Read structurally, off indentation: a VRL statement nested in a conditional is indented
    # deeper than the block's own statements. Compare against the `sudo` drop, which is known
    # to sit at the top level, so this keeps holding if the whole block is ever reindented.
    source = _scrub_source()
    top_level = _statement_indent(source, 'if .SYSLOG_IDENTIFIER == "sudo"')
    assert _statement_indent(source, 'del(."_CMDLINE")') == top_level, (
        "the `_CMDLINE` deletion must not sit inside a conditional — it applies to every "
        "event, whatever program emitted it (an identifier allow-list is what leaked)"
    )


def test_basecradle_integration_signing_keys_are_redacted() -> None:
    # THIS box is where `bc_isk_…` values live: the router verifies each platform
    # delivery's HMAC with the recipient persona's own integration secret, so all of
    # them sit in router.env and are read into the daemon's environment
    # (basecradle/basecradle#497). The pre-existing `bc_uat_` rule covers *user access
    # tokens* and never matched these — a distinct prefix needs its own rule, and the
    # box that holds the secrets is the one that must carry it.
    source = _scrub_source()
    assert r"bc_isk_[A-Za-z0-9]+" in source
    assert r"bc_uat_[A-Za-z0-9]+" in source


def test_the_integration_secret_redaction_is_unconditional() -> None:
    # Same rule as the `_CMDLINE` deletion: a redaction nested inside a conditional
    # holds only for the programs that conditional happens to name. Read structurally,
    # off indentation, against the `sudo` drop which is known to sit at the top level.
    source = _scrub_source()
    top_level = _statement_indent(source, 'if .SYSLOG_IDENTIFIER == "sudo"')
    assert _statement_indent(source, "msg = replace(msg, r'bc_isk_") == top_level


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


# --- 4. the fleet colours survive the shipping path (#228) ------------------


def test_the_scrub_never_strips_the_ansi_colour_the_journal_lines_carry() -> None:
    # The router paints its verdict tokens (@origin's fleet palette, 2026-08-17) and
    # Better Stack Live Tail renders them — but only because every hop between the two
    # passes the escape bytes through. `ai_scrub` is the one hop this repo owns, and VRL
    # ships a `strip_ansi_escape_codes` function that a future sanitising edit could
    # reach for in good faith. That edit would delete a decided fleet convention with
    # nothing failing: the daemon's own unit tests cannot see this file, and the loss
    # would surface only as colourless lines nobody happens to look at. Redaction is
    # unaffected — every rule here rewrites secret-SHAPED text, and an escape is not one.
    assert "strip_ansi_escape_codes" not in _scrub_source(), (
        "ai_scrub must pass ANSI through untouched — the palette is presentation the "
        "journal carries end to end (basecradle-router#228)"
    )

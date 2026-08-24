"""The live smoke test's own assertions — exercised offline against bash.

`deploy/smoke-test.sh` is the gate that decides whether a deployed daemon is healthy.
In #172 the gate itself was the bug: it piped a live `journalctl` into `grep -q`, which
exits on its first match, SIGPIPE-kills the still-writing producer (141), and — under
`set -o pipefail` — hands that 141 to the `if`. The assertion read FALSE with the line
sitting right there in the journal, so a good #170 daemon was rejected and rolled back.

The tell was that it was **position-dependent**: the `github` decision line is last in
the stream (producer already done, nothing to SIGPIPE) so that assertion passed, while
the `basecradle` line has records after it and so ALWAYS failed.

These tests run the EXACT shipped function bodies — extracted from `deploy/smoke-test.sh`
between its marker comments — against a fake `journalctl`, so a regression fails in CI
instead of on the box. Two properties are pinned, and both matter: the gate must PASS a
line that is present but followed by more output (the bug), and it must still FAIL a line
that is genuinely absent (a gate that always passes is as useless as one that always
fails). A control test runs the ORIGINAL broken shape over the same fixture to prove
these tests actually discriminate.

The second half pins the gate's **basecradle** cases to the daemon (#243). The old case
5 signed an unregistered recipient uuid with the route-wide secret and expected 200 —
an assertion about the shared-secret fallback, which the per-recipient keyring cutover
retired. It then failed on every deploy *and* every rollback, so `deploy-router` was
hard-blocked at every SHA. The replacement signs a **registered** persona's delivery
with that persona's own key and a deliberately **non-actionable** event, which means
the gate now depends on three daemon-side facts. Each is pinned here rather than
remembered: the two shell mirrors (`slug_suffix`, `bool_env`) are run against the
daemon's own functions over one table; the registry reader is run against a fabricated
`agents.json`; the chosen event type is asserted to be outside `_ACTIONABLE_EVENTS`
(the one edit that would turn a safe smoke run into a live wake at a real persona);
and the journal regexes are matched against the bytes the route really renders, so a
field rename moves both halves in the same commit.

No model, agent, network, or real journal is touched. Fabricated cast only: Nova Digital
(`nova`, AI) and Aurora 5.2 (`aurora-5.2`, AI). Secrets are correctly-shaped fakes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from basecradle_router.config import _AGENTS_VAR, ConfigError, route_secret_var
from basecradle_router.routes import BasecradleRoute, InboundRequest, RecipientKeyring
from basecradle_router.routes.basecradle import (
    _ACTIONABLE_EVENTS,
    DELIVERY_HEADER,
    EVENT_HEADER,
    KEY_PATH_FALLBACK,
    KEY_PATH_RECIPIENT,
    RECIPIENT_SECRET_PREFIX,
    SHARED_FALLBACK_VAR,
    SIGNATURE_HEADER,
    _bool_env,
    _slug_suffix,
)

SMOKE_TEST = Path(__file__).resolve().parents[1] / "deploy" / "smoke-test.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to exercise the smoke-test assertions"
)

# The decision line the gate looks for, and the pattern it looks for it with — both
# copied from the shapes the router actually emits and the gate actually asserts.
DECISION_LINE = (
    "Jul 09 12:00:01 ai basecradle-router[1]: event=delivery_decision source=basecradle "
    "decision=woke recipient=00000000-0000-7000-8000-000000000000 agent=nova"
)
PATTERN = (
    "event=delivery_decision source=basecradle .*decision=woke "
    ".*recipient=00000000-0000-7000-8000-000000000000"
)

# The original, broken assertion — kept ONLY as a control, to prove the fixture below
# really does reproduce #172 and that the passing tests are not vacuous. (Indented with
# spaces rather than the shell scripts' tabs: bash does not care, and a literal tab in a
# Python string is a lint error here.)
BROKEN_SHAPE = textwrap.dedent(
    """\
    assert_journal_has() {
      local name=$1 pattern=$2 since=$3
      for _ in 1 2 3 4 5; do
        if journalctl -u basecradle-router --since "$since" --no-pager 2>/dev/null |
          grep -qE "$pattern"; then
          green "  PASS  ${name}"
          return 0
        fi
        sleep 1
      done
      red "  FAIL  ${name}"
      rc=1
    }
    """
)


def _extract(marker: str) -> str:
    """Pull a real function body out of the shipped smoke test, between its markers."""
    text = SMOKE_TEST.read_text()
    match = re.search(
        rf"[ \t]*# >>> {marker} >>>\n(.*?)[ \t]*# <<< {marker} <<<\n", text, re.DOTALL
    )
    assert match, f"{marker} marker block not found in deploy/smoke-test.sh"
    return match.group(1)


def _fake_journalctl(tmp_path: Path, *, lines: list[str]) -> Path:
    """A `journalctl` that keeps writing long after its first line.

    The trailing bulk is the whole point: it must exceed the ~64 KB pipe buffer, so a
    consumer that exits early really does SIGPIPE the producer. A short fixture would
    fit in the buffer, let the producer finish, and quietly hide the bug.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    journal = tmp_path / "journal.txt"
    journal.write_text("\n".join(lines) + "\n")
    fake = bindir / "journalctl"
    fake.write_text(f'#!/usr/bin/env bash\ncat "{journal}"\n')
    fake.chmod(0o755)
    return bindir


def _run(body: str, tmp_path: Path, *, lines: list[str]) -> subprocess.CompletedProcess[str]:
    bindir = _fake_journalctl(tmp_path, lines=lines)
    script = tmp_path / "harness.sh"
    preamble = textwrap.dedent(
        """\
        set -euo pipefail
        rc=0
        green() { printf 'PASS %s\\n' "$*"; }
        red() { printf 'FAIL %s\\n' "$*"; }
        sleep() { :; }   # never actually wait during tests
        """
    )
    coda = 'assert_journal_has "decision line" "$1" "2026-07-09 12:00:00"\nexit $rc\n'
    script.write_text(preamble + body + coda)
    return subprocess.run(
        ["bash", str(script), PATTERN],
        capture_output=True,
        text=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )


def _filler(count: int) -> list[str]:
    return [
        f"Jul 09 12:00:02 ai basecradle-router[1]: event=heartbeat seq={i}" for i in range(count)
    ]


# A match EARLY in a long stream — the basecradle decision line's real position, and the
# case that failed on the box: 50k records still to write when grep short-circuits.
MATCH_EARLY = [DECISION_LINE] + _filler(50_000)
# A match at the very END — the github decision line's position. It passed even WITH the
# bug (nothing left to write, so no SIGPIPE), and must obviously still pass.
MATCH_LAST = _filler(50_000) + [DECISION_LINE]
# No match at all — a genuinely dead capability. The gate MUST still fail here.
NO_MATCH = _filler(200)


def test_shipped_assertion_passes_when_the_line_is_followed_by_more_output(
    tmp_path: Path,
) -> None:
    """#172 itself: the line is present, the producer is still writing — this must PASS."""
    result = _run(_extract("assert_journal_has"), tmp_path, lines=MATCH_EARLY)
    assert result.returncode == 0, f"the gate rejected a healthy daemon again:\n{result.stdout}"
    assert "PASS" in result.stdout


def test_shipped_assertion_passes_when_the_line_is_last(tmp_path: Path) -> None:
    """The position that passed even with the bug must not regress."""
    result = _run(_extract("assert_journal_has"), tmp_path, lines=MATCH_LAST)
    assert result.returncode == 0, result.stdout


def test_shipped_assertion_still_fails_when_the_line_is_absent(tmp_path: Path) -> None:
    """The gate must keep its teeth: a missing decision line is still a FAIL (#91).

    Without this, "fix the false negative" could quietly become "always pass", which
    would be the same bug wearing the opposite sign.
    """
    result = _run(_extract("assert_journal_has"), tmp_path, lines=NO_MATCH)
    assert result.returncode == 1
    assert "FAIL" in result.stdout


def test_control_the_original_shape_really_did_fail_on_this_fixture(tmp_path: Path) -> None:
    """Non-vacuity control: the OLD shape must FAIL the very fixture the new one passes.

    If this ever passes, the fixture stopped reproducing #172 and the tests above are
    proving nothing.
    """
    result = _run(BROKEN_SHAPE, tmp_path, lines=MATCH_EARLY)
    assert result.returncode == 1, "the broken shape passed — the fixture no longer reproduces #172"
    assert "FAIL" in result.stdout

    # ...and the old shape passed when the match was last — the position-dependence that
    # made this read as a real security-gate failure rather than a bug in the gate.
    assert _run(BROKEN_SHAPE, tmp_path, lines=MATCH_LAST).returncode == 0


# --- the env-file parsers, likewise the shipped bodies ------------------------------

ENV_FILE = textwrap.dedent(
    """\
    # basecradle-router daemon env (fabricated values)
    BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET=whsec_0123456789abcdef0123456789abcdef
    BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS=drawkkwast,basecradle-ai[bot],basecradle-router-ai[bot]
    BASECRADLE_ROUTER_WAKE_BREAKER_MAX=12
    """
)


def _run_parsers(env_text: str, snippet: str, tmp_path: Path) -> str:
    env_file = tmp_path / "router.env"
    env_file.write_text(env_text)
    script = tmp_path / "parsers.sh"
    preamble = 'set -euo pipefail\nROUTER_ENV_FILE="$1"\n'
    script.write_text(preamble + _extract("env_parsers") + snippet + "\n")
    result = subprocess.run(
        ["bash", str(script), str(env_file)], capture_output=True, text=True, check=True
    )
    return result.stdout


def test_env_value_reads_the_key(tmp_path: Path) -> None:
    out = _run_parsers(
        ENV_FILE, "env_value BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET; echo", tmp_path
    )
    assert out.strip() == "whsec_0123456789abcdef0123456789abcdef"


def test_env_value_is_empty_for_a_missing_key(tmp_path: Path) -> None:
    """A missing key must yield "" so the script's own die() fires with a real message."""
    out = _run_parsers(
        ENV_FILE, 'env_value BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET; echo "|"', tmp_path
    )
    assert out.strip() == "|"


def test_env_value_keeps_a_value_containing_equals_and_specials(tmp_path: Path) -> None:
    """The value is data, never code: `=`, `$`, and spaces survive verbatim."""
    env = "BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET=a=b$(echo hi) c==\n"
    out = _run_parsers(env, "env_value BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET; echo", tmp_path)
    assert out.strip() == "a=b$(echo hi) c=="


def test_env_value_takes_the_first_of_a_duplicated_key(tmp_path: Path) -> None:
    """Same first-match semantics the old `head -n1` had."""
    key = "BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS"
    env = f"{key}=first\n{key}=second\n"
    out = _run_parsers(env, "env_value BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS; echo", tmp_path)
    assert out.strip() == "first"


def test_env_value_ignores_a_key_that_is_only_a_substring(tmp_path: Path) -> None:
    """`FOO=` must not match `PREFIX_FOO=` — the old grep anchored with `^`."""
    key = "BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET"
    env = f"X_{key}=wrong\n{key}=right\n"
    out = _run_parsers(env, "env_value BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET; echo", tmp_path)
    assert out.strip() == "right"


def test_first_entry_trims_and_skips_empties(tmp_path: Path) -> None:
    out = _run_parsers(
        ENV_FILE, 'first_entry " , drawkkwast , basecradle-ai[bot] "; echo', tmp_path
    )
    assert out.strip() == "drawkkwast"


@pytest.mark.parametrize("raw", ["", "  ", "  ,, ", ","])
def test_first_entry_is_empty_for_an_empty_list(raw: str, tmp_path: Path) -> None:
    """An empty trusted-actor list must parse to "" so the script's die() fires.

    Run under the gate's real `set -euo pipefail` (`_run_parsers` uses `check=True`), so
    this fails if the helper aborts instead of returning empty. The bare-`""` case is the
    one that matters: `read -ra` leaves the array empty, and a plain `"${entries[@]}"` is
    an unbound-variable error under `set -u` on bash < 4.4 — the gate would die with no
    message instead of reporting an empty trusted-actor list.
    """
    out = _run_parsers(ENV_FILE, f'first_entry "{raw}"; echo "|"', tmp_path)
    assert out.strip() == "|"


def test_the_gate_resolves_a_real_trusted_actor_end_to_end(tmp_path: Path) -> None:
    """The two parsers compose to the value the gate actually tests the daemon with."""
    out = _run_parsers(
        ENV_FILE,
        'first_entry "$(env_value BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS)"; echo',
        tmp_path,
    )
    assert out.strip() == "drawkkwast"


# --- the basecradle cases: the gate's shell mirrors, pinned to the daemon (#243) -----

NOVA_UUID = "0192bbbb-cccc-7ddd-8eee-ffff00001111"  # Nova Digital's BaseCradle user uuid
AURORA_UUID = "0192cccc-dddd-7eee-8fff-000011112222"  # Aurora 5.2's, likewise fabricated

# A registry in the shape the daemon reads (deploy/README.md → "The registry"): one
# github builder (no `kind`) and two harness personas. `aurora-5.2` carries a dot on
# purpose — it is the key whose env-var suffix only survives the WHOLE-charset scrub
# (#236), so the discovery below fails if the shell mirror ever narrows back to hyphens.
REGISTRY = json.dumps(
    {
        "basecradle/basecradle-router": {
            "os_user": "basecradle-router-ai",
            "clone_path": "/home/basecradle-router-ai/repos/basecradle-router",
            "bot_slug": "basecradle-router-ai",
        },
        "nova": {
            "kind": "harness",
            "os_user": "nova",
            "clone_path": "/home/nova/harness",
            "recipient_uuid": NOVA_UUID,
            "wake_bin": "/home/nova/venv/bin/basecradle-harness-wake",
        },
        "aurora-5.2": {
            "kind": "harness",
            "os_user": "aurora-5-2",
            "clone_path": "/home/aurora-5-2/harness",
            "recipient_uuid": AURORA_UUID,
            "wake_bin": "/home/aurora-5-2/venv/bin/basecradle-harness-wake",
        },
    }
)

NOVA_KEY_VAR = RECIPIENT_SECRET_PREFIX + "NOVA"
AURORA_KEY_VAR = RECIPIENT_SECRET_PREFIX + "AURORA_5_2"

requires_jq = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq reads the agent registry (a box requirement)"
)


def _smoke_literal(name: str) -> str:
    """A top-level ``NAME="value"`` assignment's value, read out of the shipped script."""
    match = re.search(rf'^{name}="([^"]*)"$', SMOKE_TEST.read_text(), re.M)
    assert match, f"{name} assignment not found in deploy/smoke-test.sh"
    return match.group(1)


def _run_registry(env_text: str, snippet: str, tmp_path: Path) -> str:
    """Run the shipped registry parsers over a fabricated router.env + agents.json."""
    env_file = tmp_path / "router.env"
    env_file.write_text(env_text)
    registry_file = tmp_path / "agents.json"
    registry_file.write_text(REGISTRY)
    script = tmp_path / "registry.sh"
    preamble = (
        "set -euo pipefail\n"
        'ROUTER_ENV_FILE="$1"\n'
        f'BC_RECIPIENT_SECRET_PREFIX="{RECIPIENT_SECRET_PREFIX}"\n'
    )
    script.write_text(
        preamble + _extract("env_parsers") + _extract("registry_parsers") + snippet + "\n"
    )
    result = subprocess.run(
        ["bash", str(script), str(env_file), str(registry_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.mark.parametrize(
    "key", ["jt", "nova", "aurora-5.2", "aurora-5-2", "basecradle-harness", "glm_5_2"]
)
def test_shell_slug_suffix_agrees_with_the_daemons_own(key: str, tmp_path: Path) -> None:
    """The gate derives a persona's key variable; the daemon reads it. One rule, two runners.

    A divergence here is silent in both directions: the gate would either look up a
    variable that is not there (and quietly fall through to the shared-fallback branch,
    surrendering the `key_path=recipient` proof) or sign with the wrong persona's key
    and fail a healthy daemon. So the shell body and `_slug_suffix` are run over the
    same table and asserted equal.
    """
    out = _run_parsers(ENV_FILE, f'slug_suffix "{key}"; echo', tmp_path)
    assert out.strip() == _slug_suffix(key)


@pytest.mark.parametrize(
    "raw", ["1", "true", "TRUE", "yes", "on", "0", "false", "No", "off", " 0 ", ""]
)
def test_shell_bool_env_agrees_with_the_daemons_own(raw: str, tmp_path: Path) -> None:
    """Same rule, same two-runner pin — this one decides what case 6 expects."""
    env = f"{SHARED_FALLBACK_VAR}={raw}\n"
    out = _run_parsers(env, f"bool_env {SHARED_FALLBACK_VAR} true; echo", tmp_path)
    expected = _bool_env({SHARED_FALLBACK_VAR: raw}, SHARED_FALLBACK_VAR, default=True)
    assert out.strip() == ("true" if expected else "false")


def test_shell_bool_env_is_loud_on_a_value_the_daemon_would_refuse(tmp_path: Path) -> None:
    """`flase` must abort the gate, not silently read as "the fallback is still armed".

    The daemon refuses to boot on it, so a gate that guessed would assert the opposite
    of the box it is testing — the retirement that silently never happened.
    """
    env = f"{SHARED_FALLBACK_VAR}=flase\n"
    with pytest.raises(subprocess.CalledProcessError):
        _run_parsers(env, f"bool_env {SHARED_FALLBACK_VAR} true; echo", tmp_path)
    with pytest.raises(ConfigError):
        _bool_env({SHARED_FALLBACK_VAR: "flase"}, SHARED_FALLBACK_VAR, default=True)


@requires_jq
def test_registry_personas_lists_only_harness_entries_in_key_order(tmp_path: Path) -> None:
    """The github builder is not a basecradle recipient and must never be signed for."""
    out = _run_registry(ENV_FILE, 'registry_personas "$2"', tmp_path)
    assert out.splitlines() == [f"aurora-5.2\t{AURORA_UUID}", f"nova\t{NOVA_UUID}"]


@requires_jq
def test_discovery_skips_a_persona_whose_key_is_not_provisioned(tmp_path: Path) -> None:
    """Mid-cutover, only some personas hold their own key; the gate must find one that does.

    `aurora-5.2` sorts first but has no key here, so picking it would sign with an empty
    secret and fail a perfectly healthy daemon.
    """
    env = f"{NOVA_KEY_VAR}=whsec_fake_nova_integration_secret\n"
    out = _run_registry(env, 'discover_recipient "$2"; echo "$bc_slug|$bc_uuid|$bc_key"', tmp_path)
    assert out.strip() == f"nova|{NOVA_UUID}|whsec_fake_nova_integration_secret"


@requires_jq
def test_discovery_is_deterministic_and_applies_the_whole_charset_scrub(tmp_path: Path) -> None:
    """With both provisioned it takes the first in key order — and finds the dotted slug.

    `aurora-5.2` is reachable only through `…_SECRET_AURORA_5_2`; a hyphens-only scrub
    would leave the dot in, miss the variable, and silently fall through to nova.
    """
    env = (
        f"{AURORA_KEY_VAR}=whsec_fake_aurora_integration_secret\n"
        f"{NOVA_KEY_VAR}=whsec_fake_nova_integration_secret\n"
    )
    out = _run_registry(env, 'discover_recipient "$2"; echo "$bc_slug|$bc_uuid|$bc_key"', tmp_path)
    assert out.strip() == f"aurora-5.2|{AURORA_UUID}|whsec_fake_aurora_integration_secret"


@requires_jq
def test_discovery_reports_nothing_when_no_persona_holds_its_own_key(tmp_path: Path) -> None:
    """A pre-cutover box. The gate must fall to the shared fallback, not sign with "".

    Empty is the answer that routes the gate into its `elif` branch; an empty *secret*
    would be a real HMAC key and would fail the daemon for the wrong reason.
    """
    out = _run_registry(
        ENV_FILE, 'discover_recipient "$2"; echo "$bc_slug|$bc_uuid|$bc_key"', tmp_path
    )
    assert out.strip() == "||"


def test_the_smoke_tests_event_type_is_never_actionable() -> None:
    """The single fact that keeps case 5 from waking a real persona on every deploy.

    Case 5 is signed for a REGISTERED persona with that persona's own key, so verify
    admits it for real. What stops it there is that `normalize` does not act on this
    event type. Adding it to `_ACTIONABLE_EVENTS` would silently convert the deploy gate
    into a live wake at a real harness with a fabricated timeline uuid — so the event is
    read back out of the shipped script and checked against the route's own set.
    """
    assert _smoke_literal("BC_IGNORED_EVENT") not in _ACTIONABLE_EVENTS


def test_the_smoke_test_reads_the_variables_the_daemon_actually_uses() -> None:
    """The gate reads router.env by name; a rename must not orphan it silently."""
    assert _smoke_literal("BC_SECRET_VAR") == route_secret_var("basecradle")
    assert _smoke_literal("BC_FALLBACK_VAR") == SHARED_FALLBACK_VAR
    assert _smoke_literal("BC_AGENTS_VAR") == _AGENTS_VAR
    # Derived from BC_SECRET_VAR in the script exactly as the route derives it from
    # `route_secret_var`, so one rename moves both halves of the surface.
    assert _smoke_literal("BC_RECIPIENT_SECRET_PREFIX") == "${BC_SECRET_VAR}_"
    assert route_secret_var("basecradle") + "_" == RECIPIENT_SECRET_PREFIX


_SHELL_VAR = re.compile(r"\$\{(\w+)\}")


def _basecradle_journal_patterns(key_path: str) -> list[str]:
    """The gate's basecradle journal regexes, with its own shell variables resolved."""
    text = SMOKE_TEST.read_text()
    patterns = re.findall(r'"(event=[^"\n]*source=basecradle[^"\n]*)"', text)
    values = {
        "BC_DELIVERY": _smoke_literal("BC_DELIVERY"),
        "BC_IGNORED_EVENT": _smoke_literal("BC_IGNORED_EVENT"),
        "bc_case_key_path": key_path,
    }
    return [_SHELL_VAR.sub(lambda m: values[m.group(1)], pattern) for pattern in patterns]


@pytest.mark.parametrize(
    ("key_path", "keyring"),
    [
        (KEY_PATH_RECIPIENT, RecipientKeyring(by_recipient={NOVA_UUID: "whsec_fake_nova"})),
        (KEY_PATH_FALLBACK, RecipientKeyring()),
    ],
)
def test_the_gates_journal_patterns_match_what_the_route_really_renders(
    key_path: str, keyring: RecipientKeyring, caplog: pytest.LogCaptureFixture
) -> None:
    """Both basecradle assertions, run against the daemon's own bytes — no second renderer.

    `breaker_tripped`'s grammar needed a probe because it renders only on a failure path
    (#234). These two lines render on the *happy* path, so the cheaper proof is available:
    drive a real signed, non-actionable delivery through a real `BasecradleRoute` and match
    the shipped regexes against the records it emits. A renamed field (`key_path=`,
    `decision=`, `delivery=`) then fails here, in the same commit, instead of darkening a
    live deploy gate that would go on passing because it asserted nothing.
    """
    delivery = _smoke_literal("BC_DELIVERY")
    event_type = _smoke_literal("BC_IGNORED_EVENT")
    secret = keyring.by_recipient.get(NOVA_UUID, "whsec_fake_route_wide")
    body = json.dumps(
        {
            "event": event_type,
            "event_id": delivery,
            "occurred_at": "2026-01-01T00:00:00Z",
            "actor_uuid": None,
            "recipient_uuid": NOVA_UUID,
            "timeline_uuid": "0192dddd-eeee-7fff-8000-111122223333",
        }
    ).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    request = InboundRequest(
        headers={
            SIGNATURE_HEADER: f"sha256={digest}",
            EVENT_HEADER: event_type,
            DELIVERY_HEADER: delivery,
        },
        body=body,
    )

    route = BasecradleRoute(keyring)
    with caplog.at_level(logging.INFO, logger="basecradle_router"):
        route.verify(request, "whsec_fake_route_wide")
        assert route.normalize(request) is None  # non-actionable => no wake, ever

    rendered = [record.getMessage() for record in caplog.records]
    patterns = _basecradle_journal_patterns(key_path)
    assert len(patterns) == 2, f"expected the verify_key + decision patterns, got {patterns}"
    for pattern in patterns:
        assert any(re.search(pattern, line) for line in rendered), (pattern, rendered)

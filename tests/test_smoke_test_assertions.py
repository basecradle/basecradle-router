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

No model, agent, network, or real journal is touched. Fabricated cast only: Nova Digital
(`nova`, AI). Secrets are correctly-shaped fakes.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

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

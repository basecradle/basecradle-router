"""The interaction-limit renewal/audit script (basecradle-router#60, WS3).

Drives the real script as a subprocess with a **fake ``gh``** injected via ``$GH``
— the script's one external boundary — so the renewal logic, the fleet audit, and
the gap/usage exit codes are pinned without ever touching the GitHub API. Same
boundary-mock discipline as the rest of the suite: no network, no live anything.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent / ".github" / "scripts" / "interaction-limit-renewal.sh"
)

# A fake `gh`: $1=subcommand $2=next arg drives the canned response. The
# RENEW (PUT) call appends a marker to $GH_CALL_LOG so the test can assert it ran.
# `clean` → every repo has a limit + a reminder; `gap` → one repo missing both.
_FAKE_GH = r"""#!/usr/bin/env bash
echo "$*" >> "$GH_CALL_LOG"
case "$1 $2" in
  "repo list") printf 'router\nruby\n' ;;
  "api -X") : ;;  # PUT interaction-limits — the renew write
  "api /repos"*)
    if [[ "$MODE" == gap && "$*" == *ruby* ]]; then exit 1; fi
    echo "collaborators_only" ;;
  "issue list")
    if [[ "$MODE" == gap && "$*" == *ruby* ]]; then echo 0; else echo 1; fi ;;
  *) echo "fake-gh unhandled: $*" >&2; exit 99 ;;
esac
"""


def _run(tmp_path: Path, mode: str, *args: str) -> subprocess.CompletedProcess[str]:
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(_FAKE_GH)
    fake_gh.chmod(0o755)
    call_log = tmp_path / "gh-calls.log"
    call_log.touch()
    env = {
        "GH": str(fake_gh),
        "GH_CALL_LOG": str(call_log),
        "MODE": mode,
        "PATH": "/usr/bin:/bin",
    }
    result = subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return result


def test_clean_fleet_renews_and_passes(tmp_path: Path) -> None:
    result = _run(tmp_path, "clean")
    assert result.returncode == 0, result.stderr
    assert "audit clean" in result.stdout
    # The renew PUT actually fired (not just the read-only audit).
    calls = (tmp_path / "gh-calls.log").read_text()
    assert "api -X PUT /repos/basecradle/basecradle-router/interaction-limits" in calls
    assert "limit=collaborators_only" in calls
    assert "expiry=six_months" in calls


def test_dry_run_audits_but_writes_nothing(tmp_path: Path) -> None:
    result = _run(tmp_path, "clean", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "DRY-RUN" in result.stdout
    calls = (tmp_path / "gh-calls.log").read_text()
    assert "api -X PUT" not in calls  # no write in dry-run
    assert "repo list" in calls  # but the audit still ran


def test_audit_gap_fails(tmp_path: Path) -> None:
    # One repo is missing both its limit and its reminder → the run must fail so a
    # scheduled lapse is loud, not silent.
    result = _run(tmp_path, "gap", "--dry-run")
    assert result.returncode == 1
    assert "AUDIT FOUND" in result.stderr
    assert "MISSING" in result.stdout


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (["--repo", "no-slash"], "must be OWNER/NAME"),
        (["--bogus"], "unknown argument"),
    ],
)
def test_usage_errors_exit_2(tmp_path: Path, args: list[str], needle: str) -> None:
    result = _run(tmp_path, "clean", *args)
    assert result.returncode == 2
    assert needle in result.stderr

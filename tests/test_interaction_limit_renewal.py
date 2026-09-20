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
# `clean` → every repo has a limit + a reminder; `gap` → one repo missing both;
# `down` → every call fails, the way a 403 / rate limit / revoked token does;
# `empty` → the repo listing succeeds but returns nothing.
_FAKE_GH = r"""#!/usr/bin/env bash
echo "$*" >> "$GH_CALL_LOG"
if [[ "$MODE" == down && "$1 $2" != "auth status" ]]; then echo "fake-gh: 403" >&2; exit 1; fi
case "$1 $2" in
  "auth status") exit 1 ;;  # never a stored login: the guard must see a credential or refuse
  "repo list") [[ "$MODE" == empty ]] || printf 'router\nruby\n' ;;
  "api -X") : ;;  # PUT interaction-limits — the renew write
  "api /repos"*)
    if [[ "$MODE" == gap && "$*" == *ruby* ]]; then exit 1; fi
    echo "collaborators_only" ;;
  "issue list")
    if [[ "$MODE" == gap && "$*" == *ruby* ]]; then echo 0; else echo 1; fi ;;
  *) echo "fake-gh unhandled: $*" >&2; exit 99 ;;
esac
"""


def _run(
    tmp_path: Path,
    mode: str,
    *args: str,
    token: str | None = "ghs_" + "0" * 36,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
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
    # A correctly-shaped fake installation token: the script refuses to run without
    # one, because there is no ambient login anywhere for it to fall back to.
    if token is not None:
        env["GH_TOKEN"] = token
    env.update(env_extra or {})
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


def test_missing_credential_exits_2_before_any_audit_call(tmp_path: Path) -> None:
    # No GH_TOKEN, no GITHUB_TOKEN, and `gh auth status` finds no stored login — which
    # is the permanent state inside a session in this repo (#303). The run must name the
    # missing credential and exit 2 (usage/credential), not 1 ("the audit found a gap").
    result = _run(tmp_path, "clean", token=None)
    assert result.returncode == 2
    assert "no GitHub credential" in result.stderr
    # Nothing but the credential probe itself may reach `gh`.
    assert (tmp_path / "gh-calls.log").read_text().splitlines() == ["auth status"]


def test_github_token_alone_satisfies_the_guard(tmp_path: Path) -> None:
    # gh honours GITHUB_TOKEN too, so requiring GH_TOKEN specifically would refuse a
    # credential that works — a false refusal for anyone running this by hand.
    result = _run(tmp_path, "clean", token=None, env_extra={"GITHUB_TOKEN": "ghs_" + "0" * 36})
    assert result.returncode == 0, result.stderr
    assert "audit clean" in result.stdout


def test_a_failing_repo_listing_is_never_reported_as_clean(tmp_path: Path) -> None:
    # The listing used to run in a process substitution, whose failure `set -e` cannot
    # see: every call 403-ing yielded zero repos, zero gaps, and "audit clean" + exit 0
    # — a false green on the script whose whole job is to make a lapse loud.
    result = _run(tmp_path, "down", "--dry-run")
    assert result.returncode != 0
    assert "audit clean" not in result.stdout


def test_an_empty_repo_listing_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    # A listing that succeeds but returns nothing audits nothing, so it cannot be clean.
    result = _run(tmp_path, "empty", "--dry-run")
    assert result.returncode == 1
    assert "NO public repos" in result.stderr
    assert "audit clean" not in result.stdout

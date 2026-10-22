"""The CI commit-trailer guard (``.github/scripts/commit-trailers.sh``, #354).

CLAUDE.md → Fleet Bot Identity: a commit authored by this repo's bot carries no
``Co-Authored-By`` trailer. The guard drives that rule from CI, so these tests prove
it fires on a real violation and stays quiet on everything it must not judge. A
guard that has only ever been seen passing cannot be told apart from one that
never looks. Each test builds a fabricated git history under ``tmp_path`` and runs
the real script against it, with no network.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / ".github" / "scripts" / "commit-trailers.sh"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
CHARTER = REPO / "CLAUDE.md"

BOT_NAME = "basecradle-router-ai[bot]"
BOT_EMAIL = "291153759+basecradle-router-ai[bot]@users.noreply.github.com"
HUMAN = ("John Doe", "john@example.com")
TRAILER = "Co-Authored-By: Nova Digital <nova@example.com>"


def _git(repo: Path, *args: str, author: tuple[str, str] | None = None) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    if author is not None:
        name, email = author
        env |= {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env
    ).stdout.strip()


def _commit(repo: Path, message: str, *, author: tuple[str, str] = (BOT_NAME, BOT_EMAIL)) -> str:
    _git(repo, "commit", "-q", "--allow-empty", "-m", message, author=author)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "CLAUDE.md").write_text(f"| Commit-author | `{BOT_NAME} <{BOT_EMAIL}>` |\n")
    _git(root, "add", "CLAUDE.md")
    _git(root, "commit", "-q", "-m", "Charter", author=HUMAN)
    return root


def _run(repo: Path, base: str, head: str, **extra: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "BOT": BOT_NAME, "BASE_SHA": base, "HEAD_SHA": head, **extra}
    return subprocess.run(
        [str(SCRIPT)], cwd=repo, capture_output=True, text=True, check=False, env=env
    )


def test_a_bot_commit_without_a_trailer_passes(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "Fix a thing\n\nWhy it was wrong.")
    result = _run(repo, base, head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 commit(s)" in result.stdout


@pytest.mark.parametrize(
    "message",
    [
        f"Fix a thing\n\n{TRAILER}",
        f"Fix a thing\n\n{TRAILER.lower()}",
        # git's %(trailers) reads only the final paragraph; this one would slip past it.
        f"Fix a thing\n\n{TRAILER}\n\nA paragraph after the trailer.",
    ],
    ids=["plain", "lowercase", "not-the-last-paragraph"],
)
def test_a_bot_commit_with_a_trailer_fails_and_is_named(repo: Path, message: str) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "A clean one first")
    head = _commit(repo, message)
    result = _run(repo, base, head)
    assert result.returncode == 1
    assert f"{head[:7]} Fix a thing carries a Co-Authored-By trailer" in result.stdout
    assert "::error::" in result.stdout


def test_a_commit_by_anyone_else_is_not_judged(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, f"A human's commit\n\n{TRAILER}", author=HUMAN)
    result = _run(repo, base, head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 commit(s)" in result.stdout


def test_commits_already_on_the_base_are_never_relitigated(repo: Path) -> None:
    # main's squash commits cannot be rewritten; only what the PR contributes counts.
    _commit(repo, f"An old squash on main\n\n{TRAILER}")
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "The PR's own commit")
    assert _run(repo, base, head).returncode == 0


def test_main_merged_into_the_branch_is_excluded_by_the_live_base_ref(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "topic")
    _commit(repo, "The PR's own commit")
    _git(repo, "switch", "-q", "main")
    main_tip = _commit(repo, f"Landed on main after the PR opened\n\n{TRAILER}")
    _git(repo, "update-ref", "refs/remotes/origin/main", main_tip)
    _git(repo, "switch", "-q", "topic")
    _git(repo, "merge", "-q", "--no-edit", "main", author=(BOT_NAME, BOT_EMAIL))
    head = _git(repo, "rev-parse", "HEAD")

    assert _run(repo, base, head).returncode == 1, "control: without BASE_REF main's commit is read"
    result = _run(repo, base, head, BASE_REF="main")
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_identity_the_charter_no_longer_states_refuses(repo: Path) -> None:
    (repo / "CLAUDE.md").write_text("| Commit-author | `someone-else[bot]` |\n")
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "Fix a thing")
    result = _run(repo, base, head)
    assert result.returncode == 2
    assert "no longer the commit-author" in result.stderr


@pytest.mark.parametrize("missing", ["BOT", "BASE_SHA", "HEAD_SHA"])
def test_a_missing_input_is_a_bad_invocation(repo: Path, missing: str) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    result = _run(repo, head, head, **{missing: ""})
    assert result.returncode == 2
    assert f"{missing} is not set" in result.stderr


def test_the_charter_states_the_identity_ci_passes_in() -> None:
    match = re.search(r"\|\s*Commit-author\s*\|\s*`([^`<]+?) <", CHARTER.read_text())
    assert match is not None, "CLAUDE.md → Fleet Bot Identity lost its Commit-author row"
    assert match.group(1) == BOT_NAME
    assert f"BOT: {BOT_NAME}" in CI_WORKFLOW.read_text()


def test_ci_runs_the_guard_on_pull_requests_only_with_full_history() -> None:
    ci = CI_WORKFLOW.read_text()
    start = ci.index("      - name: Commit trailers")
    end = ci.find("\n      - ", start + 1)
    step = ci[start : end if end != -1 else None]
    assert "if: github.event_name == 'pull_request'" in step
    assert "run: .github/scripts/commit-trailers.sh" in step
    assert "fetch-depth: 0" in ci, "walking base..head needs both ends in the clone"

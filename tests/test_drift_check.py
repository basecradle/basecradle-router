"""The deployed-vs-main drift check — exercised offline against bash and a fixture repo.

`deploy/drift-check.sh` is the alarm that makes a never-deployed merge loud. Issue #189
was the alarm crying wolf on a schedule: this repo carries four of the fleet's five
verbatim-shared artifacts, so every fleet-wide re-sync lands a docs-only commit on main
and reddened a production signal until someone ran a deploy that changed nothing. An
alarm that is routinely red for a harmless reason converts a hard signal into a judgment
call, which is how the one genuine stale-daemon alarm gets waved through.

So the check now classifies the gap rather than comparing SHAs alone — and the risk that
introduces is the opposite failure: a path list that quietly disagrees with what a deploy
actually ships would trade a false-positive class for a FALSE-NEGATIVE one, hiding a
genuinely stale wake daemon. These tests are that pin, in two layers:

* the classifier layer runs the EXACT shipped ``is_inert_path`` — extracted from
  ``deploy/drift-check.sh`` between its marker comments — over every path the repo
  actually tracks (``git ls-files``) and every path the deploy contract actually
  consumes (globbed from ``deploy/systemd/`` and ``deploy/bin/``), so the assertions are
  DERIVED from the tree rather than restated by hand and a newly-added unit file or
  source module is covered without anyone remembering to add it here;
* the end-to-end layer runs the whole script against a local fixture git repo, so the
  outcomes that matter — green on a docs-only gap and *saying so*, red on any daemon
  path, red when the gap cannot be classified — are pinned as behavior, not as intent.

No model, agent, or network is touched: the fixture repo is a local ``file://`` remote.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIFT_CHECK = ROOT / "deploy" / "drift-check.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="bash and git are required to exercise the drift check",
)


# --------------------------------------------------------------------------------------
# Layer 1 — the classifier, run over paths derived from the real tree.
# --------------------------------------------------------------------------------------


def _extract_classifier() -> str:
    """Pull the real ``is_inert_path`` body from drift-check.sh, between its markers."""
    text = DRIFT_CHECK.read_text()
    match = re.search(
        r"[ \t]*# >>> is_inert_path >>>\n(.*?)[ \t]*# <<< is_inert_path <<<\n",
        text,
        re.DOTALL,
    )
    assert match, "is_inert_path marker block not found in deploy/drift-check.sh"
    return match.group(1)


def _classify(paths: list[str]) -> dict[str, bool]:
    """Run the shipped classifier over ``paths``; True means INERT (docs-only)."""
    script = _extract_classifier() + (
        "\nwhile IFS= read -r p; do\n"
        '  if is_inert_path "$p"; then printf "inert\\t%s\\n" "$p";'
        ' else printf "behavior\\t%s\\n" "$p"; fi\n'
        "done\n"
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        input="".join(p + "\n" for p in paths),
        capture_output=True,
        text=True,
        check=True,
    )
    out: dict[str, bool] = {}
    for line in proc.stdout.splitlines():
        verdict, path = line.split("\t", 1)
        out[path] = verdict == "inert"
    assert set(out) == set(paths)
    return out


def _tracked_files() -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return [p for p in proc.stdout.split("\0") if p]


def _deploy_contract_paths() -> list[str]:
    """The on-box contract from deploy/README.md Part 3 — globbed, never hand-listed.

    Every unit file and every root-owned binary the deploy op installs, plus the
    interpreter/dependency inputs and the scripts the box executes. Adding a unit or a
    module extends this set automatically, which is the whole point: the classifier is
    checked against what the repo really ships, not against a second model of it.
    """
    paths = [
        p.relative_to(ROOT).as_posix()
        for d in ("deploy/systemd", "deploy/bin", "src")
        for p in sorted((ROOT / d).rglob("*"))
        if p.is_file() and p.suffix != ".md"
    ]
    paths += [
        "deploy/smoke-test.sh",
        "deploy/drift-check.sh",
        "deploy/reboot-if-required.sh",
        "deploy/vector.yaml",
        "pyproject.toml",
        "uv.lock",
    ]
    assert len(paths) > 10, "the deploy contract glob found suspiciously little"
    return paths


def test_every_deploy_contract_path_is_daemon_relevant() -> None:
    """Nothing the deploy op installs or the box executes may be classified inert.

    This is the false-negative guard the tolerance buys its safety with: a stale wake
    daemon must always be loud.
    """
    verdicts = _classify(_deploy_contract_paths())
    assert [p for p, inert in verdicts.items() if inert] == []


def test_no_markdown_hides_inside_the_daemon_package() -> None:
    """`*.md` is inert at any depth — so no `.md` may become daemon input.

    The blanket rule is what makes the classifier robust (documentation is
    documentation wherever it lives), and its one latent false negative would be a
    Markdown file the running daemon actually READS — a prompt preamble, a template.
    Nothing under `src/` is Markdown today; this fails the moment that changes, so the
    choice gets made deliberately instead of silently blinding the alarm.
    """
    assert list((ROOT / "src").rglob("*.md")) == []


def test_tracked_tree_classification_matches_the_documented_rule() -> None:
    """Every path the repo tracks is classified, and only documentation/CI is inert."""
    tracked = _tracked_files()
    verdicts = _classify(tracked)

    def documented_as_inert(path: str) -> bool:
        return (
            path.endswith(".md")
            or path.startswith((".claude/", ".github/"))
            or path == ".gitignore"
        )

    disagreements = {p: verdicts[p] for p in tracked if verdicts[p] != documented_as_inert(p)}
    assert disagreements == {}
    # And the split is real in both directions — a rule that classified everything one
    # way would pass the line above vacuously.
    assert any(verdicts[p] for p in tracked)
    assert any(not verdicts[p] for p in tracked)


@pytest.mark.parametrize(
    "path",
    [
        "scripts/provision.sh",  # a new top-level directory
        "router.env.example",  # a new top-level file
        "src/basecradle_router/newmodule.py",
        "deploy/systemd/basecradle-router-newthing.timer",
        "tests/test_new.py",
    ],
)
def test_unknown_paths_are_daemon_relevant(path: str) -> None:
    """Fail-closed: a path nobody has classified must fail LOUD, not pass quietly."""
    assert _classify([path])[path] is False


@pytest.mark.parametrize(
    "path",
    [
        # The exact gap from issue #189 — a shared-artifact re-sync plus a charter edit.
        ".claude/skills/cross-repo-handoffs/SKILL.md",
        ".github/workflows/needs-human-alert.yml",
        "CLAUDE.md",
        "README.md",
        "deploy/README.md",
    ],
)
def test_shared_artifact_and_docs_paths_are_inert(path: str) -> None:
    assert _classify([path])[path] is True


# --------------------------------------------------------------------------------------
# Layer 2 — the whole script, end to end, against a local fixture remote.
# --------------------------------------------------------------------------------------

# A miniature of the real tree: one file from each classification neighbourhood.
BASE_TREE = {
    "CLAUDE.md": "# charter\n",
    ".gitignore": ".venv/\n",
    ".claude/skills/cross-repo-handoffs/SKILL.md": "# handoffs\n",
    ".github/workflows/ci.yml": "name: CI\n",
    "src/basecradle_router/pipeline.py": "WAKE = 1\n",
    "deploy/systemd/basecradle-router.service": "[Service]\n",
    "deploy/smoke-test.sh": "#!/usr/bin/env bash\n",
    "pyproject.toml": "[project]\n",
    "tests/test_pipeline.py": "def test_wake(): pass\n",
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(repo),
            "GIT_AUTHOR_NAME": "Nova Digital",
            "GIT_AUTHOR_EMAIL": "nova@example.com",
            "GIT_COMMITTER_NAME": "Nova Digital",
            "GIT_COMMITTER_EMAIL": "nova@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )
    return proc.stdout.strip()


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    _write(repo, files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def remote(tmp_path: Path) -> tuple[Path, str]:
    """A fixture repo on branch ``main`` with the base tree committed. Returns (path, sha)."""
    repo = tmp_path / "basecradle-router"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    return repo, _commit(repo, BASE_TREE, "base")


def _run(repo: Path, deployed: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    stamp = tmp_path / "deployed-sha"
    stamp.write_text(deployed + "\n")
    return subprocess.run(
        ["bash", str(DRIFT_CHECK)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "STAMP": str(stamp),
            "REMOTE_URL": f"file://{repo}",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )


def test_identical_shas_are_in_sync(remote: tuple[Path, str], tmp_path: Path) -> None:
    repo, base = remote
    result = _run(repo, base, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "IN SYNC" in result.stdout


def test_docs_only_gap_passes_and_says_so_out_loud(
    remote: tuple[Path, str], tmp_path: Path
) -> None:
    """Issue #189's exact scenario: a shared-artifact re-sync must not redden the box."""
    repo, base = remote
    _commit(
        repo,
        {
            ".claude/skills/cross-repo-handoffs/SKILL.md": "# handoffs, re-synced\n",
            ".github/workflows/needs-human-alert.yml": "name: Needs Human Alert\n",
            "CLAUDE.md": "# charter, amended\n",
        },
        "Re-sync shared artifacts",
    )

    result = _run(repo, base, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    # Green, but never silent — the operator must be able to see WHY it is green.
    assert "IN SYNC (daemon)" in result.stdout
    assert "DOCS-ONLY" in result.stdout
    assert base in result.stdout
    for path in (
        ".claude/skills/cross-repo-handoffs/SKILL.md",
        ".github/workflows/needs-human-alert.yml",
        "CLAUDE.md",
    ):
        assert path in result.stdout


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("src/basecradle_router/pipeline.py", "WAKE = 2\n"),
        ("deploy/systemd/basecradle-router.service", "[Service]\nRestart=always\n"),
        ("deploy/smoke-test.sh", "#!/usr/bin/env bash\nexit 0\n"),
        ("pyproject.toml", "[project]\nname = 'x'\n"),
        ("tests/test_pipeline.py", "def test_wake(): assert True\n"),
        ("deploy/bin/wake-runner", "#!/usr/bin/env bash\n"),  # added, not just changed
    ],
)
def test_daemon_path_gap_is_loud_drift(
    remote: tuple[Path, str], tmp_path: Path, path: str, body: str
) -> None:
    repo, base = remote
    _commit(repo, {path: body}, f"touch {path}")

    result = _run(repo, base, tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "DRIFT" in result.stdout
    assert path in result.stdout


def test_docs_plus_daemon_gap_is_drift(remote: tuple[Path, str], tmp_path: Path) -> None:
    """The tolerance is for gaps confined to docs — one daemon file in the gap is drift."""
    repo, base = remote
    _commit(
        repo,
        {"CLAUDE.md": "# charter, amended\n", "src/basecradle_router/pipeline.py": "WAKE = 3\n"},
        "docs and code",
    )

    result = _run(repo, base, tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "DRIFT" in result.stdout
    assert "src/basecradle_router/pipeline.py" in result.stdout
    # The docs file rode along but is not what makes this red.
    assert "daemon-relevant changes" in result.stdout


def test_unfetchable_deployed_sha_is_drift(remote: tuple[Path, str], tmp_path: Path) -> None:
    """A gap that cannot be classified is reported as drift, never as sync."""
    repo, _ = remote
    _commit(repo, {"CLAUDE.md": "# charter, amended\n"}, "docs")

    result = _run(repo, "0" * 40, tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "could NOT be classified" in result.stdout


def test_classification_survives_a_git_without_partial_clone(
    remote: tuple[Path, str], tmp_path: Path
) -> None:
    """No git-version assumption about the box: `--filter=blob:none` may be refused.

    A shim git that rejects any `--filter` argument and otherwise delegates to the real
    one stands in for an older client or a remote that declines partial clone. The check
    must still classify the gap, not degrade to an unclassifiable red.
    """
    repo, base = remote
    _commit(repo, {"CLAUDE.md": "# charter, amended\n"}, "docs")

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    real_git = shutil.which("git")
    shim = shim_dir / "git"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$a" in --filter=*) echo "fatal: unknown option $a" >&2;'
        " exit 129 ;; esac; done\n"
        f'exec {real_git} "$@"\n'
    )
    shim.chmod(0o755)

    stamp = tmp_path / "deployed-sha"
    stamp.write_text(base + "\n")
    result = subprocess.run(
        ["bash", str(DRIFT_CHECK)],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{shim_dir}:/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "STAMP": str(stamp),
            "REMOTE_URL": f"file://{repo}",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "IN SYNC (daemon)" in result.stdout
    assert "CLAUDE.md" in result.stdout


def test_missing_stamp_is_drift(remote: tuple[Path, str], tmp_path: Path) -> None:
    repo, _ = remote
    result = subprocess.run(
        ["bash", str(DRIFT_CHECK)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "STAMP": str(tmp_path / "absent"),
            "REMOTE_URL": f"file://{repo}",
        },
    )
    assert result.returncode == 1
    assert "no deploy stamp" in result.stdout

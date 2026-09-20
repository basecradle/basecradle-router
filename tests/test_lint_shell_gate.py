"""The CI shell-lint gate (``.github/scripts/lint-shell.sh``).

CI gated Python twice and never linted shell (basecradle-router#299), so the
root-run ``deploy/bin/wake-runner`` — the privilege boundary itself — and the
helper that reboots the box were the two programs with the most privilege and no
automated lint. These tests drive the real script as a subprocess against
fabricated repos, the same boundary-mock discipline the rest of the suite
follows: no network, no live anything, just the script's own bytes.

The gate's whole value is that it cannot quietly stop gating, so what is pinned
here is not "shellcheck ran" but the three ways it could go vacuous: discovery
losing a script, CI losing the step, and the linter finding nothing wrong because
nothing was handed to it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from test_shell_pipeline_safety import shell_scripts  # sibling test module's discovery

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / ".github" / "scripts" / "lint-shell.sh"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

pytestmark = pytest.mark.skipif(
    shutil.which("shellcheck") is None,
    reason="shellcheck is required to exercise the shipped gate",
)


def _run(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(GATE)], cwd=str(cwd), capture_output=True, text=True, check=False)


def _init_repo(root: Path) -> None:
    """A throwaway git repo — the gate discovers through ``git ls-files``."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


# --------------------------------------------------------------------------- #
# 1. The gate is wired into CI, and into the one required context.              #
# --------------------------------------------------------------------------- #


def test_ci_runs_the_gate() -> None:
    """A gate no job invokes is a file, not a gate."""
    assert ".github/scripts/lint-shell.sh" in CI_WORKFLOW.read_text()


def _job_ids() -> list[str]:
    """The keys under ``jobs:``. Read by indentation rather than a YAML parser —
    one test does not earn a dependency, and the block is two levels deep.
    """
    lines = CI_WORKFLOW.read_text().splitlines()
    start = lines.index("jobs:") + 1
    ids = []
    for line in lines[start:]:
        if line and not line.startswith(" "):
            break  # a new top-level key ends the jobs block
        stripped = line.strip()
        is_job_key = (
            line.startswith("  ")
            and not line.startswith("   ")
            and stripped.endswith(":")
            and not stripped.startswith("#")
        )
        if is_job_key:
            ids.append(stripped[:-1])
    return ids


def test_the_gate_is_a_step_in_the_required_ci_job() -> None:
    """#299 was decided as a STEP, deliberately: the ruleset requires the context
    ``CI``, and a separate job would publish a context nothing requires — green
    while unenforced — unless the ruleset were updated in lockstep. That is the
    job-id trap ci.yml's own header warns about, so pin it.
    """
    assert _job_ids() == ["CI"], "a second job would need a ruleset change"


# --------------------------------------------------------------------------- #
# 2. Discovery covers this repo's shell, and agrees with the suite's own test.   #
# --------------------------------------------------------------------------- #


def _discovered() -> set[str]:
    result = _run(REPO)
    assert result.returncode == 0, result.stdout + result.stderr
    return {
        line.strip()
        for line in result.stdout.splitlines()
        if line.startswith("  ") and line.strip()
    }


def test_the_repo_passes_its_own_gate() -> None:
    result = _run(REPO)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean" in result.stdout


def test_the_privileged_scripts_are_covered() -> None:
    """The two root-run programs are why this gate exists; name them explicitly so
    a discovery regression that silently drops them fails here.
    """
    discovered = _discovered()
    assert "deploy/bin/wake-runner" in discovered
    assert "deploy/bin/reboot-if-required.sh" in discovered
    assert "deploy/smoke-test.sh" in discovered
    assert ".github/scripts/lint-shell.sh" in discovered  # the gate lints itself


def test_discovery_matches_the_suites_own_shell_test() -> None:
    """Two definitions of "this is a shell script" in one repo is one too many.

    ``test_shell_pipeline_safety.shell_scripts`` asks the identical question over
    the identical scope — every tracked file, by ``.sh`` or by shebang — in Python
    where ``\\b`` is available. This gate asks it in bash, where it is not, so the
    boundary is spelled out by hand. That is exactly the kind of near-duplicate
    that drifts, so pin the two to the same set: a shebang form one accepts and
    the other rejects is a defect, not a difference.
    """
    expected = {p.relative_to(REPO).as_posix() for p in shell_scripts()}
    assert expected, "the repo tracks shell; finding none means this test is vacuous"
    assert _discovered() == expected


def test_python_scripts_are_excluded() -> None:
    """``deploy/bin/probe-ack`` is Python with no extension — a filter keyed on
    anything but the shebang would hand it to a shell linter.
    """
    assert "deploy/bin/probe-ack" not in _discovered()


# --------------------------------------------------------------------------- #
# 3. Positive controls: the gate actually fails, and never passes vacuously.     #
# --------------------------------------------------------------------------- #


def test_a_real_finding_fails_the_gate(tmp_path: Path) -> None:
    """The control that matters: a gate that cannot fail is decoration."""
    script = tmp_path / "bad.sh"
    script.write_text("#!/usr/bin/env bash\nrm $1\n")  # SC2086: unquoted expansion
    _init_repo(tmp_path)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "SC2086" in result.stdout


def test_default_severity_is_kept(tmp_path: Path) -> None:
    """#299 was decided at the default (``style``) floor. A style-only finding must
    still fail, or the decision has been quietly relaxed to ``--severity=warning``.
    """
    script = tmp_path / "styled.sh"
    # SC2006 is the discriminator: it is emitted at `style` and at no higher floor,
    # so this passes the moment someone adds `--severity=warning` (or `info`).
    script.write_text('#!/usr/bin/env bash\nx=`ls`\necho "$x"\n')
    _init_repo(tmp_path)
    result = _run(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "SC2006 (style)" in result.stdout


def test_an_extensionless_shell_script_is_discovered(tmp_path: Path) -> None:
    """``wake-runner`` has no extension; shebang discovery is the only thing that
    catches it, and the next root-run helper like it.
    """
    script = tmp_path / "helper"
    script.write_text("#!/usr/bin/env bash\nrm $1\n")
    _init_repo(tmp_path)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "helper" in result.stdout


def test_no_shell_found_is_an_error_not_a_pass(tmp_path: Path) -> None:
    """An empty discovery set is the gate's own failure mode: shellcheck with no
    arguments would read stdin, and a green step would mean nothing was checked.
    """
    (tmp_path / "only.py").write_text("print('hi')\n")
    _init_repo(tmp_path)
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "discovery is broken" in result.stderr

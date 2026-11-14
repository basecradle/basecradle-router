"""The CI workflow-lint gate (``.github/scripts/lint-workflows.sh``, #449).

The Public Repository Lockdown workflow merged as invalid YAML: a plain ``run:`` scalar
holding ": " made GitHub reject the file, so it had no triggers and would never have run,
while CI was green on every commit that carried it. CI now runs actionlint over every
workflow. These tests pin that the gate is wired into the one required context with a
pinned, verified binary, that the script cannot pass vacuously, and, so that a session can
catch this class before a pull request even without actionlint installed, that no workflow
line carries the exact shape that broke.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / ".github" / "scripts" / "lint-workflows.sh"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.y*ml"))


def _step(name: str) -> str:
    text = CI_WORKFLOW.read_text()
    assert f"- name: {name}\n" in text
    return text.split(f"- name: {name}\n", 1)[1].split("\n      - name:", 1)[0]


def test_ci_runs_actionlint_pinned_by_version_and_checksum() -> None:
    step = _step("Workflow Lint")
    assert re.search(r'ACTIONLINT_VERSION: "\d+\.\d+\.\d+"', step)
    assert re.search(r'ACTIONLINT_SHA256: "[0-9a-f]{64}"', step)
    assert "sha256sum --check --strict" in step
    assert ".github/scripts/lint-workflows.sh" in step
    # Inside the one CI job, so the required status context stays `CI`.
    assert CI_WORKFLOW.read_text().index("Workflow Lint") > CI_WORKFLOW.read_text().index("  CI:\n")


def _run(path: str, cwd: Path = REPO) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(GATE)],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": path},
    )


def _fake_actionlint(tmp_path: Path, code: int) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "actionlint"
    fake.write_text(f'#!/bin/sh\necho "actionlint $*" >> "{tmp_path}/calls"\nexit {code}\n')
    fake.chmod(0o755)
    return f"{bin_dir}:/usr/bin:/bin"


def test_a_missing_actionlint_is_a_refusal_never_a_pass() -> None:
    result = _run("/usr/bin:/bin")
    assert result.returncode == 2
    assert "actionlint is not installed" in result.stderr


def test_a_finding_fails_the_gate(tmp_path: Path) -> None:
    result = _run(_fake_actionlint(tmp_path, 1))
    assert result.returncode == 1
    assert "clean" not in result.stdout


def test_every_workflow_is_handed_to_actionlint(tmp_path: Path) -> None:
    result = _run(_fake_actionlint(tmp_path, 0))
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text()
    for workflow in WORKFLOWS:
        assert f".github/workflows/{workflow.name}" in calls


def test_no_workflows_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".github" / "scripts").mkdir(parents=True)
    (repo / ".github" / "workflows").mkdir()
    gate = repo / ".github" / "scripts" / "lint-workflows.sh"
    gate.write_text(GATE.read_text())
    result = subprocess.run(
        ["bash", str(gate)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": _fake_actionlint(tmp_path, 0)},
    )
    assert result.returncode == 1
    assert "no workflow files" in result.stderr


# `key: value` on one line, the value a plain scalar (not quoted, not a block, not a flow
# collection). In a plain scalar, ": " starts a mapping and " #" starts a comment.
_PLAIN = re.compile(r"^\s*(?:- )?[\w.-]+: (?P<value>[^\s|>\"'\[{&*!].*)$")


def test_no_workflow_line_holds_a_plain_scalar_yaml_would_misread() -> None:
    offenders = []
    for workflow in WORKFLOWS:
        for number, line in enumerate(workflow.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            match = _PLAIN.match(line)
            if match and (": " in match["value"] or " #" in match["value"]):
                offenders.append(f"{workflow.name}:{number}: {line.strip()}")
    assert offenders == [], "use a | block or quote the value:\n" + "\n".join(offenders)


def test_the_shape_check_catches_the_line_that_broke_the_lockdown() -> None:
    broken = (
        '        run: test -n "$ADMIN_TOKEN" || { echo "::error::FLEET_ADMIN_TOKEN is not set: '
        'nothing can be renewed" >&2; exit 1; }'
    )
    match = _PLAIN.match(broken)
    assert match and ": " in match["value"]

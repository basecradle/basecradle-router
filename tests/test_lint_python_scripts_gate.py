"""The CI gate for Python scripts with no ``.py`` (``.github/scripts/lint-python-scripts.sh``).

ruff discovers Python by extension, so ``ruff check .`` never opened the extensionless helpers
under ``deploy/bin``, which the NOC installs root-owned on the box. These tests drive the real
script against this tree and against fabricated repos, and pin the ways it could stop gating:
CI losing the step, discovery losing a script, and a finding that does not fail it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / ".github" / "scripts" / "lint-python-scripts.sh"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
#: The project's own ruff, the one `uv run` puts on PATH in CI.
VENV_BIN = Path(sys.executable).parent


def _run(cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{VENV_BIN}{os.pathsep}{os.environ.get('PATH', '')}"}
    return subprocess.run(
        [str(GATE)], cwd=str(cwd), capture_output=True, text=True, check=False, env=env
    )


def _init_repo(root: Path) -> None:
    """A throwaway git repo with its own ruff config, so no config outside it is read."""
    (root / "ruff.toml").write_text('line-length = 100\n[lint]\nselect = ["E", "F"]\n')
    for path in root.iterdir():
        if path.is_file() and path.read_text().startswith("#!"):
            path.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


def _discovered(result: subprocess.CompletedProcess[str]) -> set[str]:
    """The paths the gate lists under its header line, and nothing ruff printed after them."""
    lines = result.stdout.splitlines()
    start = next(i for i, line in enumerate(lines) if " over " in line and "script(s):" in line)
    found = set()
    for line in lines[start + 1 :]:
        if not line.startswith("  "):
            break
        found.add(line.strip())
    return found


#: The gate's rule, in Python: a shebang naming `python` as a whole word, optionally versioned.
_PYTHON_SHEBANG = re.compile(r"^#!.*[^A-Za-z0-9_]python[0-9.]*([^A-Za-z0-9_.]|$)")


def _python_scripts() -> set[str]:
    """Every tracked file with no ``.py`` whose first line is a Python shebang."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split("\0")
    found = set()
    for name in filter(None, tracked):
        path = REPO / name
        if name.endswith(".py") or not path.is_file():
            continue
        with path.open("rb") as handle:
            first = handle.readline(256).decode(errors="replace").rstrip("\n")
        if _PYTHON_SHEBANG.match(first):
            found.add(name)
    return found


def test_ci_runs_the_gate_through_the_projects_ruff() -> None:
    assert "run: uv run .github/scripts/lint-python-scripts.sh\n" in CI_WORKFLOW.read_text()


def test_the_repo_passes_its_own_gate_and_covers_every_python_script() -> None:
    result = _run(REPO)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _discovered(result) == _python_scripts()
    # The root-run helpers are why this gate exists: name them, so a discovery regression
    # that drops one fails here.
    assert {"deploy/bin/fleet-writers", "deploy/bin/stop-wake"} <= _discovered(result)
    assert {"deploy/bin/claude-stream-translate", "deploy/bin/probe-ack"} <= _discovered(result)


def test_shell_and_dot_py_files_are_not_handed_to_it(tmp_path: Path) -> None:
    for name, shebang in {
        "helper": "#!/usr/bin/python3 -I",
        "plain": "#!/usr/bin/env python",
        "versioned": "#!/usr/bin/python3.12",
        "named-alike": "#!/usr/local/bin/pythonista",
        "runner": "#!/usr/bin/env bash",
        "module.py": "#!/usr/bin/env python3",
    }.items():
        (tmp_path / name).write_text(f'{shebang}\nprint("ok")\n')
    _init_repo(tmp_path)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _discovered(result) == {"helper", "plain", "versioned"}


def test_a_broken_ruff_config_is_a_bad_invocation_not_a_finding(tmp_path: Path) -> None:
    (tmp_path / "helper").write_text('#!/usr/bin/env python3\nprint("ok")\n')
    _init_repo(tmp_path)
    (tmp_path / "ruff.toml").write_text("line-length = 'not a number'\n")
    result = _run(tmp_path)
    assert result.returncode == 2, result.stdout + result.stderr


def test_a_lint_finding_fails_the_gate(tmp_path: Path) -> None:
    (tmp_path / "helper").write_text("#!/usr/bin/env python3\nimport os\n")  # F401
    _init_repo(tmp_path)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "F401" in result.stdout


def test_a_format_finding_fails_the_gate(tmp_path: Path) -> None:
    (tmp_path / "helper").write_text("#!/usr/bin/env python3\nx = {'a':1}\nprint(x)\n")
    _init_repo(tmp_path)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "would be reformatted" in result.stdout


def test_no_script_found_is_an_error_not_a_pass(tmp_path: Path) -> None:
    (tmp_path / "only.py").write_text("print('hi')\n")
    _init_repo(tmp_path)
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "discovery is broken" in result.stderr

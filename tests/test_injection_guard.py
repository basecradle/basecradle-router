"""The CI injection-marker guard (``.github/scripts/injection-guard.sh``).

A structural backstop (basecradle-router#60): a change that lands the known
contamination artifacts of the rejected "memory middleware" injection
(``.agent-relay`` / ``.project-id``) must fail CI so it can never merge. These
tests drive the real script as a subprocess against fabricated trees — the same
boundary-mock discipline the rest of the suite follows: no network, no live
anything, just the script's own bytes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "injection-guard.sh"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(GUARD), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_clean_tree_passes(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ok.py").write_text("print('hi')\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


@pytest.mark.parametrize("marker", [".agent-relay", ".project-id"])
def test_marker_file_fails(tmp_path: Path, marker: str) -> None:
    nested = tmp_path / "deep" / "nested"
    nested.mkdir(parents=True)
    (nested / marker).write_text("contamination\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert marker in result.stderr
    assert "FORBIDDEN" in result.stderr


def test_marker_directory_fails(tmp_path: Path) -> None:
    # The injection drops markers as files, but a directory of the same name is
    # contamination too — the guard matches by basename, file or dir.
    (tmp_path / ".project-id").mkdir()
    result = _run(tmp_path)
    assert result.returncode == 1
    assert ".project-id" in result.stderr


def test_marker_in_pruned_dir_is_ignored(tmp_path: Path) -> None:
    # Vendored/generated dirs are pruned: a marker that only exists inside a
    # dependency's own files must never fail the fleet's CI.
    venv = tmp_path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / ".agent-relay").write_text("not ours\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_missing_root_is_a_usage_error(tmp_path: Path) -> None:
    result = _run(tmp_path / "does-not-exist")
    assert result.returncode == 2
    assert "not a directory" in result.stderr

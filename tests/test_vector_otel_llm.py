"""The Claude Code builders' `llm` line, rendered by the shipped Vector config (#380).

`ai_otel_llm` is VRL, and its output is the exact line Better Stack receives, so no Python
reading of the YAML can prove it. This runs the real thing: `vector test` executes the
transform over the fabricated events in `tests/vector/ai_otel_llm.yaml` and asserts the
bytes. It runs against every host's shipped `vector.yaml`, on the Vector release the boxes
run (deploy/README → `VECTOR_VERSION`), so a VRL behaviour that changed between releases
cannot pass here and fail on a box.

CI installs that release (ci.yml → "Install Vector"), so there the binary is required. A
laptop without it, or with a different release, skips: point `VECTOR_BIN` at the pinned
release to run it locally.

No network, model or agent: `vector test` runs transforms only, never a source or a sink.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
README = REPO / "deploy" / "README.md"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
HOSTS_DIR = REPO / "deploy" / "hosts"
TESTS = REPO / "tests" / "vector" / "ai_otel_llm.yaml"


def _pinned_version() -> str:
    match = re.search(r"^VECTOR_VERSION=(\S+)$", README.read_text(), re.MULTILINE)
    assert match, "deploy/README.md must pin VECTOR_VERSION — the release the boxes install"
    return match.group(1)


def _vector() -> str:
    """The pinned Vector binary; required in CI, a skip anywhere else."""
    binary = os.environ.get("VECTOR_BIN") or shutil.which("vector")
    if binary is None:
        assert not os.environ.get("CI"), "Vector is missing on the CI runner"
        pytest.skip("vector is not installed (set VECTOR_BIN to the pinned release)")
    got = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False)
    pinned = _pinned_version()
    if not got.stdout.startswith(f"vector {pinned} "):
        assert not os.environ.get("CI"), f"CI runs {got.stdout.strip()!r}, not Vector {pinned}"
        pytest.skip(f"{got.stdout.strip()!r} is not the boxes' Vector {pinned}")
    return binary


def test_ci_installs_the_release_the_boxes_run() -> None:
    # The proof below is only as good as the Vector it runs on. A CI pin that drifted from the
    # boxes' would green VRL the box's release rejects, or renders differently.
    pinned = _pinned_version()
    assert f'VECTOR_VERSION: "{pinned}"' in CI_WORKFLOW.read_text()


@pytest.mark.parametrize("host", sorted(p.name for p in HOSTS_DIR.iterdir() if p.is_dir()))
def test_the_shipped_transform_renders_the_contract_line(host: str) -> None:
    vector = _vector()
    # A sink token is interpolated at load; this fake one never leaves the process, because
    # `vector test` builds no sink.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "BETTERSTACK_AI_SOURCE_TOKEN": "fabricated-test-token",
        "NO_COLOR": "1",
    }
    got = subprocess.run(
        [vector, "test", str(HOSTS_DIR / host / "vector.yaml"), str(TESTS)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    report = got.stdout + got.stderr
    assert got.returncode == 0, report
    # Every declared test ran and passed: a test Vector silently skipped would pass vacuously.
    declared = re.findall(r'^  - name: "(.+)"$', TESTS.read_text(), re.MULTILINE)
    assert declared, "tests/vector/ai_otel_llm.yaml declares no tests"
    for name in declared:
        assert f"test {name} ... passed" in report, f"{name!r} did not pass:\n{report}"

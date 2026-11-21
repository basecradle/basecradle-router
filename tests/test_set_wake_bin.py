"""``deploy/bin/set-wake-bin`` judges a value by wake-runner's own gate, byte for byte (#535).

The setter writes one harness agent's ``wake_bin`` into the registry so the NOC can seal or
unseal an agent with no person in the loop. Its promise is that a value it writes is one the
next wake accepts, and that holds only while its copy of the gate is wake-runner's. So this
pins the two copies, and the constant they judge against, equal: an edit to either gate fails
here until the other carries it. Re-splice with the marker blocks, never by retyping.

What it does as root (the write, the lock, the refusals, the registry's owner and mode) is
proven by ``tests/test_sealed_wake_root.py`` against the real script.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "deploy" / "bin"
WAKE_RUNNER = BIN / "wake-runner"
SET_WAKE_BIN = BIN / "set-wake-bin"


def _block(path: Path, marker: str) -> str:
    match = re.search(rf"# >>> {marker} >>>\n.*?# <<< {marker} <<<\n", path.read_text(), re.S)
    assert match, f"{marker} marker block not found in {path.name}"
    return match.group(0)


@pytest.mark.parametrize("gate", ["confined_path", "harness_wake_bin"])
def test_the_setter_carries_wake_runners_gate_byte_for_byte(gate: str) -> None:
    assert _block(SET_WAKE_BIN, gate) == _block(WAKE_RUNNER, gate), (
        f"set-wake-bin's {gate} has drifted from wake-runner's: splice wake-runner's block in"
    )


def test_the_setter_judges_against_wake_runners_sealed_base() -> None:
    def base(path: Path) -> list[str]:
        return re.findall(r"^SEALED_BASE=(.*)$", path.read_text(), re.MULTILINE)

    assert base(SET_WAKE_BIN) == base(WAKE_RUNNER) == ["/opt/basecradle-harness"]


def test_the_setter_asks_the_gate_exactly_as_wake_runner_does() -> None:
    call = '"$(harness_wake_bin "wake_bin" "/home/$user" "$SEALED_BASE" "$user" "$wake_bin")"'
    assert call in WAKE_RUNNER.read_text()
    assert call in SET_WAKE_BIN.read_text()


def test_the_setter_is_installed_executable() -> None:
    assert SET_WAKE_BIN.stat().st_mode & 0o111 == 0o111


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash runs the setter")
def test_the_setter_refuses_anyone_but_root_before_reading_anything(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("run as root, the refusal is not reachable; the root suite covers it")
    done = subprocess.run(
        ["bash", str(SET_WAKE_BIN), "nova", "/opt/basecradle-harness/venv/nova/bin/x"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        check=False,
    )
    assert done.returncode == 1
    assert done.stderr == "set-wake-bin: must run as root\n"

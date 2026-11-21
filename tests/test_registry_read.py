"""``wake-runner`` reads its registry entry so every field arrives as its own (#541).

The entry's key, kind and ``wake_bin`` were read as ``@tsv`` into ``IFS=$'\\t' read``. A tab is
IFS whitespace, so ``read`` merged two around an empty field: ``"kind": ""`` handed ``kind`` the
``wake_bin`` path, and the refusal named the wrong field. The fields are now joined by the unit
separator, which is not whitespace, and a field holding a control character is refused before
``read`` sees it. These run the shipped lines, cut from ``deploy/bin/wake-runner``, against a
fabricated registry. Fabricated cast only (Nova Digital).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WAKE_RUNNER = Path(__file__).resolve().parents[1] / "deploy" / "bin" / "wake-runner"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None, reason="bash and jq read it"
)


def _lookup() -> str:
    match = re.search(
        r'^entry="\$\(jq -r --arg u "\$user".*?'
        r'^IFS=\$\'\\x1f\' read -r match kind wake_bin <<<"\$entry"$',
        WAKE_RUNNER.read_text(),
        re.S | re.M,
    )
    assert match, "the registry lookup moved in deploy/bin/wake-runner"
    return match.group(0)


def _read(tmp_path: Path, entry: dict) -> subprocess.CompletedProcess[str]:
    registry = tmp_path / "agents.json"
    registry.write_text(json.dumps({"nova": {"os_user": "nova", "clone_path": "/c", **entry}}))
    script = (
        'set -euo pipefail\ndie() { printf "%s\\n" "$*" >&2; exit 1; }\n'
        f'REGISTRY="$1" user=nova cwd=/c\n{_lookup()}\n'
        'printf "match=%s kind=%s wake_bin=%s\\n" "$match" "$kind" "$wake_bin"\n'
    )
    return subprocess.run(
        ["bash", "-c", script, "bash", str(registry)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"},
        check=False,
    )


def test_every_field_arrives_as_its_own(tmp_path: Path) -> None:
    done = _read(tmp_path, {"kind": "harness", "wake_bin": "/w"})
    assert done.stdout == "match=nova kind=harness wake_bin=/w\n", done.stderr


def test_an_empty_kind_stays_empty_and_shifts_nothing(tmp_path: Path) -> None:
    # The @tsv read gave kind the wake_bin path here; the refusal then named the wrong field.
    done = _read(tmp_path, {"kind": "", "wake_bin": "/w"})
    assert done.stdout == "match=nova kind= wake_bin=/w\n", done.stderr


def test_an_absent_kind_still_defaults_to_a_builder(tmp_path: Path) -> None:
    done = _read(tmp_path, {})
    assert done.stdout == "match=nova kind=github wake_bin=\n", done.stderr


@pytest.mark.parametrize(
    "value", ["/w\nx", "/w\tx", "/w\x1fx"], ids=["newline", "tab", "separator"]
)
def test_a_field_holding_a_control_character_is_refused(tmp_path: Path, value: str) -> None:
    done = _read(tmp_path, {"kind": "harness", "wake_bin": value})
    assert done.returncode == 1
    assert "could not read registry" in done.stderr
    assert done.stdout == ""

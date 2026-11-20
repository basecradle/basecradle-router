"""The wake-runner hands a wake its attempt (#527), run for real against bash.

The router retries a wake that exited 75 at about 1, 5 and 30 minutes, and tells each wake
which attempt it is, so the last one can say so on its timeline. On the home server that
crosses ``sudo`` as ``--attempt <n>/<of>``, and the agent script, after the privilege drop,
exports it as ``BASECRADLE_WAKE_ATTEMPT`` and ``BASECRADLE_WAKE_ATTEMPTS``.

These run the EXACT shipped agent script, extracted from ``deploy/bin/wake-runner``, in a
clean bash as an unprivileged stand-in for the agent, with ``env`` as the wake's binary: what
it prints is the environment the wake would have had. Cast: Nova Digital (``nova``).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from basecradle_router.wake import ATTEMPT_ENV, ATTEMPTS_ENV, DELIVERY_ID_ENV

WAKE_RUNNER = Path(__file__).resolve().parents[1] / "deploy" / "bin" / "wake-runner"
DELIVERY = "0192f3a4-5b6c-7d8e-9f01-23456789abcd"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")


def _agent_script() -> str:
    match = re.search(r"^AGENT_SCRIPT='\n(.*?)\n'$", WAKE_RUNNER.read_text(), re.S | re.M)
    assert match, "AGENT_SCRIPT not found"
    return match.group(1)


def _wake_env(tmp_path: Path, attempt: str, attempts: str, agent_env: str = "") -> dict[str, str]:
    home = tmp_path / "home"
    (home / ".config" / "basecradle").mkdir(parents=True)
    (home / ".config" / "basecradle" / "agent.env").write_text(agent_env)
    clone = tmp_path / "clone"
    clone.mkdir()
    proc = subprocess.run(
        ["bash", "-c", _agent_script(), "wake-runner", str(clone), DELIVERY, attempt, attempts]
        + ["", "", "", "", shutil.which("env") or "/usr/bin/env", "-0"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return dict(pair.split("=", 1) for pair in proc.stdout.split("\0") if "=" in pair)


def test_the_wake_is_told_its_attempt_and_the_router_wins(tmp_path: Path) -> None:
    # A stale value left in agent.env never stands: the router's is exported after it.
    env = _wake_env(tmp_path, "2", "4", agent_env=f"{ATTEMPT_ENV}=9\n{ATTEMPTS_ENV}=9\n")
    assert env[ATTEMPT_ENV] == "2"
    assert env[ATTEMPTS_ENV] == "4"
    assert env[DELIVERY_ID_ENV] == DELIVERY


def test_without_an_attempt_neither_variable_is_set(tmp_path: Path) -> None:
    env = _wake_env(tmp_path, "", "")
    assert ATTEMPT_ENV not in env and ATTEMPTS_ENV not in env
    assert env[DELIVERY_ID_ENV] == DELIVERY


def test_the_flag_is_parsed_strictly_and_refused_beside_a_probe() -> None:
    # The parse runs as root on the box, past the EUID check, so it is pinned as written:
    # two bounded integers, the first never past the second, and never on a probe.
    text = WAKE_RUNNER.read_text()
    assert "[[ $2 =~ ^([1-9][0-9]{0,2})/([1-9][0-9]{0,2})$ ]] ||" in text
    assert (
        '((attempt <= attempts)) || die "refusing: --attempt $2 is past its last attempt"' in text
    )
    assert '[[ -z $attempt ]] || die "refusing: --probe takes no --attempt"' in text
    assert re.match(r"^([1-9][0-9]{0,2})/([1-9][0-9]{0,2})$", "2/4")
    for bad in ("0/4", "2", "2/4/6", "-1/4", "1000/1000", "2/ 4", "2/4\n"):
        assert not re.fullmatch(r"([1-9][0-9]{0,2})/([1-9][0-9]{0,2})", bad), bad

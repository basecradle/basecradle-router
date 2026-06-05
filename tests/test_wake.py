"""The wake boundary, fully mocked — no real model, agent, or network.

A fake runner records the assembled invocation and returns a canned result; the
default subprocess runner is exercised via a monkeypatched ``subprocess.run``.
Test cast: Nova Digital (``nova``, AI) captains basecradle-python.
"""

import subprocess

import pytest

from basecradle_router import wake as wake_module
from basecradle_router.models import Agent, Event, EventKind, IssueRef
from basecradle_router.wake import (
    SubprocessWaker,
    WakeError,
    WakeInvocation,
    Waker,
    WakeResult,
)

NOVA = Agent(
    repo="basecradle/basecradle-python",
    os_user="nova",
    clone_path="/home/nova/basecradle-python",
    bot_slug="basecradle-python-ai",
)
ISSUE_URL = "https://github.com/basecradle/basecradle-python/issues/42"
EVENT = Event(
    source="github",
    kind=EventKind.HANDOFF,
    target_repo=NOVA.repo,
    origin=IssueRef(repo=NOVA.repo, number=42, url=ISSUE_URL, title="Mirror the wire-shape change"),
    trigger=f"Cross-repo handoff: work {ISSUE_URL}",
    delivery_id="0192f3a4-5b6c-7d8e-9f01-23456789abcd",
)


class _FakeRunner:
    """Records the invocation it is handed and returns a preset result."""

    def __init__(self, result: WakeResult) -> None:
        self.result = result
        self.invocation: WakeInvocation | None = None

    def __call__(self, invocation: WakeInvocation) -> WakeResult:
        self.invocation = invocation
        return self.result


def test_subprocess_waker_satisfies_the_protocol() -> None:
    assert isinstance(SubprocessWaker(), Waker)


def test_wake_assembles_command_cwd_env_and_run_as_user() -> None:
    env = {"GH_TOKEN": "ghs_" + "0" * 36, "PATH": "/usr/bin"}  # correctly-shaped fake
    runner = _FakeRunner(WakeResult(exit_code=0, stdout="done"))
    waker = SubprocessWaker(runner=runner, env_provider=lambda agent: env)

    result = waker.wake(NOVA, EVENT)

    assert runner.invocation is not None
    assert runner.invocation.argv == ("claude", "-p", EVENT.trigger)
    assert runner.invocation.cwd == "/home/nova/basecradle-python"
    assert runner.invocation.env == env
    assert runner.invocation.run_as_user == "nova"
    assert result.ok
    assert result.stdout == "done"


def test_env_provider_receives_the_agent() -> None:
    seen: list[Agent] = []

    def provider(agent: Agent) -> dict[str, str]:
        seen.append(agent)
        return {"BOT_SLUG": agent.bot_slug}

    runner = _FakeRunner(WakeResult(exit_code=0))
    SubprocessWaker(runner=runner, env_provider=provider).wake(NOVA, EVENT)

    assert seen == [NOVA]
    assert runner.invocation is not None
    assert runner.invocation.env == {"BOT_SLUG": "basecradle-python-ai"}


def test_default_env_is_empty() -> None:
    runner = _FakeRunner(WakeResult(exit_code=0))
    SubprocessWaker(runner=runner).wake(NOVA, EVENT)
    assert runner.invocation is not None
    assert runner.invocation.env == {}


def test_invocation_for_is_pure_and_does_not_run() -> None:
    runner = _FakeRunner(WakeResult(exit_code=0))
    waker = SubprocessWaker(runner=runner)
    invocation = waker.invocation_for(NOVA, EVENT)
    assert invocation.run_as_user == "nova"
    assert runner.invocation is None  # assembling the command runs nothing


def test_non_zero_exit_raises_wake_error_with_code_and_stderr() -> None:
    runner = _FakeRunner(WakeResult(exit_code=2, stderr="model overloaded"))
    waker = SubprocessWaker(runner=runner)
    with pytest.raises(WakeError, match="model overloaded") as excinfo:
        waker.wake(NOVA, EVENT)
    assert excinfo.value.exit_code == 2
    assert NOVA.repo in str(excinfo.value)


def test_non_zero_exit_falls_back_to_stdout_then_placeholder() -> None:
    # stderr empty but stdout has the detail → stdout is surfaced.
    runner = _FakeRunner(WakeResult(exit_code=1, stdout="no git credentials", stderr=""))
    with pytest.raises(WakeError, match="no git credentials"):
        SubprocessWaker(runner=runner).wake(NOVA, EVENT)
    # Nothing on either stream → a clear placeholder, never an empty message.
    silent = _FakeRunner(WakeResult(exit_code=1, stdout="", stderr=""))
    with pytest.raises(WakeError, match=r"\(no output\)"):
        SubprocessWaker(runner=silent).wake(NOVA, EVENT)


def test_timeout_is_carried_onto_the_invocation() -> None:
    runner = _FakeRunner(WakeResult(exit_code=0))
    SubprocessWaker(runner=runner, timeout=1800.0).wake(NOVA, EVENT)
    assert runner.invocation is not None
    assert runner.invocation.timeout == 1800.0


def test_default_timeout_is_unbounded() -> None:
    runner = _FakeRunner(WakeResult(exit_code=0))
    SubprocessWaker(runner=runner).wake(NOVA, EVENT)
    assert runner.invocation is not None
    assert runner.invocation.timeout is None


# --- the default subprocess runner -----------------------------------------


def test_default_runner_invokes_subprocess_with_assembled_command(monkeypatch) -> None:
    captured: dict = {}

    class _Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)

    env = {"GH_TOKEN": "ghs_" + "0" * 36}
    waker = SubprocessWaker(env_provider=lambda agent: env)  # default real runner
    result = waker.wake(NOVA, EVENT)

    assert captured["argv"] == ["claude", "-p", EVENT.trigger]
    assert captured["kwargs"]["cwd"] == "/home/nova/basecradle-python"
    assert captured["kwargs"]["env"] == env
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["check"] is False
    assert result.ok and result.stdout == "ok"


def test_default_runner_passes_timeout_to_subprocess(monkeypatch) -> None:
    captured: dict = {}

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    SubprocessWaker(timeout=900.0).wake(NOVA, EVENT)
    assert captured["timeout"] == 900.0


def test_default_runner_wraps_launch_failure_in_wake_error(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("claude: command not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(WakeError, match="could not launch"):
        SubprocessWaker().wake(NOVA, EVENT)


def test_default_runner_maps_timeout_to_wake_error(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(WakeError, match="timed out") as excinfo:
        SubprocessWaker(timeout=5.0).wake(NOVA, EVENT)
    assert excinfo.value.exit_code is None  # never produced an exit code


def test_default_runner_is_the_module_default() -> None:
    # Guards the "tests never cross the real boundary unless they monkeypatch it"
    # contract: the out-of-the-box runner is the subprocess one.
    assert SubprocessWaker().runner is wake_module._run_subprocess

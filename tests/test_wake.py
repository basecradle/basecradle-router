"""The wake boundary, fully mocked — no real model, agent, or network.

A fake runner records the assembled invocation and returns a canned result; the
default subprocess runner is exercised via a monkeypatched ``subprocess.run``.
Test cast: Nova Digital (``nova``, AI) captains basecradle-python.
"""

import dataclasses
import subprocess

import pytest

from basecradle_router import wake as wake_module
from basecradle_router.models import Agent, Event, EventKind, IssueRef, Recipient, WakeKind
from basecradle_router.wake import (
    DELIVERY_ID_ENV,
    WAKE_RUNNER,
    HomeServerWaker,
    SubprocessWaker,
    WakeError,
    WakeInvocation,
    Waker,
    WakeResult,
    WakeStopped,
)

NOVA = Agent(
    key="basecradle/basecradle-python",
    os_user="nova",
    clone_path="/home/nova/basecradle-python",
    bot_slug="basecradle-python-ai",
)
ISSUE_URL = "https://github.com/basecradle/basecradle-python/issues/42"
EVENT = Event(
    source="github",
    kind=EventKind.HANDOFF,
    recipient=Recipient(by="repo", value=NOVA.key),
    wake_arg=f"Cross-repo handoff: work {ISSUE_URL}",
    delivery_id="0192f3a4-5b6c-7d8e-9f01-23456789abcd",
    origin=IssueRef(repo=NOVA.key, number=42, url=ISSUE_URL, title="Mirror the wire-shape change"),
)

# A harness agent (@orion): woken via its own wake CLI, not claude.
ORION = Agent(
    key="orion",
    os_user="orion",
    clone_path="/home/orion/harness",
    wake_kind=WakeKind.HARNESS,
    recipient_uuid="01a161c5-8102-71e0-82f0-2d53fa00c27b",
    wake_bin="/home/orion/venv/bin/basecradle-harness-wake",
)
TIMELINE_UUID = "0192aaaa-bbbb-7ccc-8ddd-eeeeffff0000"
ORION_EVENT = Event(
    source="basecradle",
    kind=EventKind.PLATFORM_EVENT,
    recipient=Recipient(by="recipient_uuid", value=ORION.recipient_uuid),
    wake_arg=TIMELINE_UUID,
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
    assert runner.invocation.argv == ("claude", "-p", EVENT.wake_arg)
    assert runner.invocation.cwd == "/home/nova/basecradle-python"
    # The provider's env, plus the one variable the router itself contributes.
    assert runner.invocation.env == {**env, DELIVERY_ID_ENV: EVENT.delivery_id}
    assert runner.invocation.run_as_user == "nova"
    assert result.ok
    assert result.stdout == "done"


def test_wake_assembles_the_harness_command_for_a_harness_agent() -> None:
    runner = _FakeRunner(WakeResult(exit_code=0, stdout="replied"))
    waker = SubprocessWaker(runner=runner)

    waker.wake(ORION, ORION_EVENT)

    assert runner.invocation is not None
    assert runner.invocation.argv == (
        "/home/orion/venv/bin/basecradle-harness-wake",
        "--timeline",
        TIMELINE_UUID,
    )
    assert runner.invocation.cwd == "/home/orion/harness"
    assert runner.invocation.run_as_user == "orion"


def test_env_provider_receives_the_agent() -> None:
    seen: list[Agent] = []

    def provider(agent: Agent) -> dict[str, str]:
        seen.append(agent)
        return {"BOT_SLUG": agent.bot_slug}

    runner = _FakeRunner(WakeResult(exit_code=0))
    SubprocessWaker(runner=runner, env_provider=provider).wake(NOVA, EVENT)

    assert seen == [NOVA]
    assert runner.invocation is not None
    assert runner.invocation.env == {
        "BOT_SLUG": "basecradle-python-ai",
        DELIVERY_ID_ENV: EVENT.delivery_id,
    }


def test_default_env_carries_only_the_delivery_id() -> None:
    # The router contributes exactly one variable to the child — the delivery id, for
    # log correlation (#170). It carries no authority and no secret: everything the
    # agent needs to *act* still comes from the agent's own env, never from the router.
    runner = _FakeRunner(WakeResult(exit_code=0))
    SubprocessWaker(runner=runner).wake(NOVA, EVENT)
    assert runner.invocation is not None
    assert runner.invocation.env == {DELIVERY_ID_ENV: EVENT.delivery_id}


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
    assert NOVA.key in str(excinfo.value)


def test_non_zero_exit_falls_back_to_stdout_then_placeholder() -> None:
    # stderr empty but stdout has the detail → stdout is surfaced.
    runner = _FakeRunner(WakeResult(exit_code=1, stdout="no git credentials", stderr=""))
    with pytest.raises(WakeError, match="no git credentials"):
        SubprocessWaker(runner=runner).wake(NOVA, EVENT)
    # Nothing on either stream → a clear placeholder, never an empty message.
    silent = _FakeRunner(WakeResult(exit_code=1, stdout="", stderr=""))
    with pytest.raises(WakeError, match=r"\(no output\)"):
        SubprocessWaker(runner=silent).wake(NOVA, EVENT)


@pytest.mark.parametrize("cls", [WakeInvocation, SubprocessWaker, HomeServerWaker])
def test_no_wake_deadline_can_be_set(cls) -> None:
    # A deadline the router enforced could reach only the `sudo` at the top of the
    # wake: SIGKILL there orphans the live session under the agent's account, and the
    # retry then starts a second one on the same memory store (#389). The router holds
    # no way to end a wake whole (stop-wake is root-only and granted nothing, #386), so
    # the refusal is structural: there is no field to put a deadline in, and adding one
    # back is a deliberate act this test makes someone justify.
    assert "timeout" not in {f.name for f in dataclasses.fields(cls)}
    if cls is not WakeInvocation:
        with pytest.raises(TypeError, match="unexpected keyword argument 'timeout'"):
            cls(timeout=1800.0)


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

    assert captured["argv"] == ["claude", "-p", EVENT.wake_arg]
    assert captured["kwargs"]["cwd"] == "/home/nova/basecradle-python"
    assert captured["kwargs"]["env"] == {**env, DELIVERY_ID_ENV: EVENT.delivery_id}
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["check"] is False
    assert result.ok and result.stdout == "ok"


@pytest.mark.parametrize("waker", [SubprocessWaker(), HomeServerWaker()])
def test_default_runner_never_puts_a_deadline_on_the_wake(monkeypatch, waker) -> None:
    # The runner-side half of the same refusal (#389): `subprocess.run(timeout=…)`
    # SIGKILLs at most its direct child — `sudo`, on the deployed path — and its expiry
    # used to become a retryable WakeError, which is the split-brain the issue names.
    captured: dict = {}

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return _Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    waker.wake(NOVA, EVENT)
    assert "timeout" not in captured


def test_default_runner_wraps_launch_failure_in_wake_error(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("claude: command not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(WakeError, match="could not launch"):
        SubprocessWaker().wake(NOVA, EVENT)


def test_default_runner_is_the_module_default() -> None:
    # Guards the "tests never cross the real boundary unless they monkeypatch it"
    # contract: the out-of-the-box runner is the subprocess one.
    assert SubprocessWaker().runner is wake_module._run_subprocess


# --- the home-server waker (runs through the wake-runner wrapper) -----------


def test_home_server_waker_satisfies_the_protocol() -> None:
    assert isinstance(HomeServerWaker(), Waker)


def test_home_server_waker_assembles_the_sudo_wrapper_command() -> None:
    runner = _FakeRunner(WakeResult(exit_code=0, stdout="opened PR"))
    result = HomeServerWaker(runner=runner).wake(NOVA, EVENT)

    assert runner.invocation is not None
    # The exact contract:
    #   sudo <wrapper> --user U --cwd DIR --delivery ID -- claude -p TRIGGER.
    assert runner.invocation.argv == (
        "sudo",
        WAKE_RUNNER,
        "--user",
        "nova",
        "--cwd",
        "/home/nova/basecradle-python",
        "--delivery",
        EVENT.delivery_id,
        "--",
        "claude",
        "-p",
        EVENT.wake_arg,
    )
    assert runner.invocation.run_as_user == "nova"
    assert result.ok and result.stdout == "opened PR"


def test_home_server_waker_assembles_the_harness_wake_command() -> None:
    # A harness agent is woken via its registry-pinned wake CLI, not claude:
    # sudo <wrapper> --user orion --cwd <home> --delivery ID -- <wake_bin> --timeline <uuid>.
    runner = _FakeRunner(WakeResult(exit_code=0, stdout="replied"))
    result = HomeServerWaker(runner=runner).wake(ORION, ORION_EVENT)

    assert runner.invocation is not None
    assert runner.invocation.argv == (
        "sudo",
        WAKE_RUNNER,
        "--user",
        "orion",
        "--cwd",
        "/home/orion/harness",
        "--delivery",
        ORION_EVENT.delivery_id,
        "--",
        "/home/orion/venv/bin/basecradle-harness-wake",
        "--timeline",
        TIMELINE_UUID,
    )
    assert runner.invocation.run_as_user == "orion"
    assert result.ok and result.stdout == "replied"


def test_home_server_waker_passes_no_env_and_an_irrelevant_cwd() -> None:
    # The wrapper loads the agent's own env after the privilege drop, so the
    # router must hand over nothing — no secret ever flows through it.
    runner = _FakeRunner(WakeResult(exit_code=0))
    HomeServerWaker(runner=runner).wake(NOVA, EVENT)
    assert runner.invocation is not None
    assert runner.invocation.env == {}
    assert runner.invocation.cwd == "/"


def test_home_server_waker_carries_the_trigger_as_one_inert_argv_element() -> None:
    # A trigger full of shell metacharacters must remain a single argv element —
    # it is data passed to claude, never a string a shell could re-interpret.
    nasty = "work; rm -rf / `whoami` $(id) && echo pwned"
    event = Event(
        source="github",
        kind=EventKind.HANDOFF,
        recipient=Recipient(by="repo", value=NOVA.key),
        wake_arg=nasty,
        delivery_id=EVENT.delivery_id,
        origin=EVENT.origin,
    )
    runner = _FakeRunner(WakeResult(exit_code=0))
    HomeServerWaker(runner=runner).wake(NOVA, event)
    assert runner.invocation is not None
    assert runner.invocation.argv[-1] == nasty  # exactly one element, verbatim
    assert runner.invocation.argv.count(nasty) == 1


def test_home_server_waker_honours_a_custom_wrapper_path() -> None:
    runner = _FakeRunner(WakeResult(exit_code=0))
    HomeServerWaker(runner=runner, wrapper="/opt/x/wake-runner").wake(NOVA, EVENT)
    assert runner.invocation is not None
    assert runner.invocation.argv[1] == "/opt/x/wake-runner"


def test_home_server_waker_raises_wake_error_on_non_zero_exit() -> None:
    runner = _FakeRunner(WakeResult(exit_code=3, stderr="refusing: not a registered agent clone"))
    with pytest.raises(WakeError, match="not a registered agent clone") as excinfo:
        HomeServerWaker(runner=runner).wake(NOVA, EVENT)
    assert excinfo.value.exit_code == 3
    assert NOVA.key in str(excinfo.value)


def test_home_server_waker_default_runner_is_the_subprocess_one() -> None:
    assert HomeServerWaker().runner is wake_module._run_subprocess


# --- a signal death is a stop, never a crash (basecradle-router#386) --------


@pytest.mark.parametrize(
    ("exit_code", "signal"),
    [
        (-15, "TERM"),  # the signal reached sudo, which re-raised it (the 2026-11-03 pkill)
        (-9, "KILL"),
    ],
)
@pytest.mark.parametrize("waker_kind", [SubprocessWaker, HomeServerWaker])
def test_a_stop_signal_exit_raises_wake_stopped_not_wake_error(
    waker_kind, exit_code: int, signal: str
) -> None:
    waker = waker_kind(runner=_FakeRunner(WakeResult(exit_code=exit_code)))
    with pytest.raises(WakeStopped) as excinfo:
        waker.wake(NOVA, EVENT)
    assert excinfo.value.signal == signal
    assert excinfo.value.exit_code == exit_code
    assert f"SIG{signal}" in str(excinfo.value)


def test_a_stop_is_not_a_wake_error_so_no_failure_handler_can_swallow_it() -> None:
    # The retry converts WakeError into a transient; a stop must never reach it.
    assert not issubclass(WakeStopped, WakeError)


@pytest.mark.parametrize(
    "exit_code",
    # Crashes, aborts, SIGINT, SIGHUP — and 143/137, a signal death INSIDE the wake (the
    # harness's watchdog, the OOM killer), which the router cannot tell from a crash.
    [1, 2, 75, -11, -6, -2, 134, 139, 130, 129, -1, 143, 137],
)
def test_every_other_non_zero_exit_is_still_a_crash_with_its_retry(exit_code: int) -> None:
    runner = _FakeRunner(WakeResult(exit_code=exit_code))
    with pytest.raises(WakeError) as excinfo:
        HomeServerWaker(runner=runner).wake(NOVA, EVENT)
    assert excinfo.value.exit_code == exit_code

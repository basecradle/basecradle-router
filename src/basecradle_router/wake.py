"""The wake boundary: run a target agent's headless ``claude -p`` in its clone.

This is the single seam between the router and a live agent — the one place a
real model, agent, and the network are reached, and therefore the one place
tests mock. The router *delivers a trigger*; it never becomes the agent. The
woken ``claude`` is an independent, separately-chartered context running in the
agent's own repo clone under the agent's own identity.

The boundary is shaped for the home-server model — **run-as-user is carried on
every invocation** — but the privilege drop itself (and live per-agent
credentials) is deferred with the home server. The v0 subprocess runner does not
drop privileges; it assembles and runs the command, and the home-server runner
will consume :attr:`WakeInvocation.run_as_user` to execute as that OS user.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from basecradle_router.models import Agent, Event


class WakeError(Exception):
    """A wake failed: the headless session exited non-zero or could not launch.

    Carries the process ``exit_code`` (``None`` if the subprocess never started)
    so the caller can report it. Whether a wake failure is worth retrying is the
    pipeline's policy, not this boundary's — hence this is a plain exception, not
    coupled to the retry machinery.
    """

    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class WakeInvocation:
    """The fully-assembled command for one wake — the runner's sole input.

    Bundling the pieces (``argv``, ``cwd``, ``env``, ``run_as_user``, optional
    ``timeout``) makes the assembly a single testable object and keeps the runner
    seam narrow.
    """

    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str]
    run_as_user: str
    timeout: float | None = None


@dataclass(frozen=True, slots=True)
class WakeResult:
    """The outcome of running a wake: exit code and captured output."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


# The runner seam: turn an assembled invocation into a result. The default runs a
# real subprocess; tests inject a fake so the boundary is never actually crossed.
Runner = Callable[[WakeInvocation], WakeResult]

# Per-agent environment provider. The home-server impl reads the agent OS user's
# credentials (GitHub App token, later ANTHROPIC_API_KEY); v0 defaults to empty.
EnvProvider = Callable[[Agent], Mapping[str, str]]


@runtime_checkable
class Waker(Protocol):
    """Wake an agent for an event. The pipeline depends only on this protocol."""

    def wake(self, agent: Agent, event: Event) -> WakeResult:
        """Run ``agent``'s headless ``claude`` for ``event``.

        Returns a :class:`WakeResult` on a clean (exit 0) run; raises
        :class:`WakeError` on a non-zero exit or a launch failure.
        """
        ...


def _empty_env(_agent: Agent) -> Mapping[str, str]:
    return {}


def _run_subprocess(invocation: WakeInvocation) -> WakeResult:
    """The v0 runner: run the command as a subprocess, capturing its output.

    Does **not** drop privileges — ``run_as_user`` is carried but not applied;
    the home-server runner will (via ``runuser``/setuid). With the default empty
    ``env`` it would run with no ``PATH``, so this default runner is a placeholder
    for the mocked v0, not a deployable path — the home server supplies the real
    per-user environment and privilege drop. Never invoked in tests.
    """
    try:
        completed = subprocess.run(
            list(invocation.argv),
            cwd=invocation.cwd,
            env=dict(invocation.env),
            capture_output=True,
            text=True,
            check=False,
            timeout=invocation.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WakeError(
            f"wake in {invocation.cwd!r} timed out after {invocation.timeout}s"
        ) from exc
    except OSError as exc:
        raise WakeError(f"could not launch wake in {invocation.cwd!r}: {exc}") from exc
    return WakeResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _result_or_raise(result: WakeResult, agent: Agent) -> WakeResult:
    """Return a clean (exit 0) result, or raise :class:`WakeError` naming the repo.

    The shared tail of every waker: a non-zero exit becomes a ``WakeError`` carrying
    the exit code and the best available detail (stderr, then stdout, then a clear
    placeholder — never an empty message)."""
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
        raise WakeError(
            f"wake of {agent.repo} exited {result.exit_code}: {detail}",
            exit_code=result.exit_code,
        )
    return result


@dataclass(frozen=True, slots=True)
class SubprocessWaker:
    """A :class:`Waker` that runs ``claude -p "<trigger>"`` in the agent's clone.

    ``runner`` and ``env_provider`` are injectable seams: the defaults run a real
    subprocess with an empty environment, and tests replace them so no real
    model, agent, or network is ever reached. ``timeout`` bounds a wake's
    wall-clock — left ``None`` (unbounded) in v0 so a legitimately long agent is
    never killed mid-task; production should set a bound so a hung ``claude``
    cannot pin a worker thread forever.
    """

    runner: Runner = _run_subprocess
    env_provider: EnvProvider = _empty_env
    timeout: float | None = None

    def invocation_for(self, agent: Agent, event: Event) -> WakeInvocation:
        """Assemble the command to wake ``agent`` for ``event`` — pure, no I/O."""
        return WakeInvocation(
            argv=("claude", "-p", event.trigger),
            cwd=agent.clone_path,
            env=MappingProxyType(dict(self.env_provider(agent))),
            run_as_user=agent.os_user,
            timeout=self.timeout,
        )

    def wake(self, agent: Agent, event: Event) -> WakeResult:
        return _result_or_raise(self.runner(self.invocation_for(agent, event)), agent)


# The root-owned wrapper the home-server waker escalates through (see deploy/).
WAKE_RUNNER = "/opt/basecradle-router/bin/wake-runner"


@dataclass(frozen=True, slots=True)
class HomeServerWaker:
    """The deployable :class:`Waker` — runs the wake through the wake-runner wrapper.

    Assembles ``sudo <wrapper> --user <os_user> --cwd <clone> -- claude -p
    "<trigger>"`` and runs it. The root-owned wrapper validates the request, drops
    to the agent's own OS user, and (as the agent) sources that agent's
    ``agent.env`` — so the router itself passes **no** environment and never holds a
    secret. The trigger rides as a single ``argv`` element, never a shell string, so
    it is never re-interpreted. ``runner`` is the same injectable seam tests replace;
    ``wrapper`` is overridable for tests.
    """

    runner: Runner = _run_subprocess
    wrapper: str = WAKE_RUNNER
    timeout: float | None = None

    def invocation_for(self, agent: Agent, event: Event) -> WakeInvocation:
        """Assemble the sudo+wrapper command — pure, no I/O."""
        return WakeInvocation(
            argv=(
                "sudo",
                self.wrapper,
                "--user",
                agent.os_user,
                "--cwd",
                agent.clone_path,
                "--",
                "claude",
                "-p",
                event.trigger,
            ),
            # The wrapper cd's into the clone as the agent; the router's own cwd is
            # irrelevant, and "/" is always present. Env is empty by design — the
            # wrapper supplies the agent's environment after the privilege drop.
            cwd="/",
            env=MappingProxyType({}),
            run_as_user=agent.os_user,
            timeout=self.timeout,
        )

    def wake(self, agent: Agent, event: Event) -> WakeResult:
        return _result_or_raise(self.runner(self.invocation_for(agent, event)), agent)

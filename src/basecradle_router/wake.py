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

import logging
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from basecradle_router.marker import is_marker
from basecradle_router.models import Agent, Event, WakeKind

logger = logging.getLogger("basecradle_router.wake")

#: The delivery id is handed to the wake child in its environment under this name,
#: so the woken harness can stamp its *own* lifecycle lines with the same id the
#: router's stage lines carry (basecradle-router#170). One `delivery=<id>` grep then
#: spans the router's journal and the agent's — the two halves of one wake, which
#: are otherwise separate identifiers (`basecradle-router` and
#: `basecradle-wake-<slug>`) with nothing in common. The harness treats it as
#: optional-when-absent, so the two sides ship independently.
DELIVERY_ID_ENV = "BASECRADLE_DELIVERY_ID"

# The delivery id is observability, never authority — but it *is* source-supplied,
# and on the home-server path it crosses a `sudo` boundary as an argv element. So
# it is shape-checked here, at the router's edge, and pinned to the inert charset
# every real source's id already lives in (a GitHub delivery GUID, a BaseCradle
# event id). The fail-direction is deliberate and asymmetric: an id that does not
# match is *dropped* (the wake still runs — see :func:`_delivery_args`), because an
# observability field must never be able to cost a wake; whereas the root-owned
# wake-runner, whose caller is by then guaranteed to send only a matching id,
# *refuses* one that does not — a mismatch there means the argv was tampered with.
_SAFE_DELIVERY_ID = re.compile(r"\A[A-Za-z0-9_.:-]{1,128}\Z")


def wake_command(agent: Agent, event: Event) -> tuple[str, ...]:
    """The agent-kind-specific command (binary + args) the wake launches.

    A builder runs ``claude -p "<trigger>"``; a harness persona runs its own
    ``<wake_bin> --timeline "<uuid>"``. Either way the single event value
    (:attr:`Event.wake_arg` — the trigger or the timeline uuid) rides as one inert
    argv element, never a shell string, so it is never re-interpreted. The home
    server's wake-runner independently pins the binary against the registry, so a
    compromised router cannot substitute a different command here.

    **A synthetic event has no command, and asking for one raises.** The fleet's one
    hard constraint is that nothing burns a token at rest, and a probe delivered to
    ``claude -p "<marker>"`` *is* a model call — the marker would be read as a prompt.
    So the impossibility is enforced structurally rather than remembered: there is no
    argv this function could return for a probe that would not start a session, so it
    returns none. A probe travels the wrapper's own ``--probe`` mode instead
    (:class:`HomeServerWaker`), where the model binary is never exec'd at all
    (basecradle-router#208).
    """
    if event.synthetic:
        raise WakeError(
            f"refusing to build a wake command for a synthetic {event.kind.value} event: "
            "a probe must never launch the agent's model — it travels the wake-runner's "
            "--probe mode, which acks after the privilege drop without exec'ing anything"
        )
    if agent.wake_kind is WakeKind.HARNESS:
        return (agent.wake_bin, "--timeline", event.wake_arg)
    return ("claude", "-p", event.wake_arg)


def _correlation_id(event: Event) -> str | None:
    """``event``'s delivery id if it is inert enough to hand the child, else ``None``.

    The single gate both wakers pass the id through, so the shape check and the
    warning cannot diverge between them. An unsafe id is *dropped*, loudly and never
    silently: the wake proceeds without the correlation value rather than failing
    over a field that exists only to be read in a log. Unreachable for every source
    shipped today (both mint UUID-shaped ids); it is the guard that keeps a future
    source's odd id-space from being able to cost a wake.
    """
    if _SAFE_DELIVERY_ID.match(event.delivery_id):
        return event.delivery_id
    logger.warning(
        "event=unsafe_delivery_id source=%s delivery=%r; omitting %s from the wake "
        "environment — correlation is lost for this wake, which still runs",
        event.source,
        event.delivery_id,
        DELIVERY_ID_ENV,
    )
    return None


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
            f"wake of {agent.key} exited {result.exit_code}: {detail}",
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

    The child's environment is the provider's, plus :data:`DELIVERY_ID_ENV` — the
    one variable the *router* contributes. It carries no authority and no secret;
    the provider still owns everything that does.
    """

    runner: Runner = _run_subprocess
    env_provider: EnvProvider = _empty_env
    timeout: float | None = None

    def invocation_for(self, agent: Agent, event: Event) -> WakeInvocation:
        """Assemble the command to wake ``agent`` for ``event`` — pure, no I/O."""
        env = dict(self.env_provider(agent))
        delivery = _correlation_id(event)
        if delivery is not None:
            env[DELIVERY_ID_ENV] = delivery
        return WakeInvocation(
            argv=wake_command(agent, event),
            cwd=agent.clone_path,
            env=MappingProxyType(env),
            run_as_user=agent.os_user,
            timeout=self.timeout,
        )

    def wake(self, agent: Agent, event: Event) -> WakeResult:
        return _result_or_raise(self.runner(self.invocation_for(agent, event)), agent)


# The root-owned wrapper the home-server waker escalates through (see deploy/).
WAKE_RUNNER = "/opt/basecradle-router/bin/wake-runner"

#: The wrapper flag that selects probe mode — verify the marker as the agent and ack,
#: instead of exec'ing the agent's binary. One spelling, shared by the argv assembled
#: here and the wrapper that parses it.
PROBE_FLAG = "--probe"


def _mode_args(agent: Agent, event: Event) -> tuple[str, ...]:
    """The wrapper's mode-selecting tail: ``--probe <marker>``, or ``-- <wake command>``.

    The two modes are disjoint by construction — a probe never carries a command and a
    real wake never carries a marker — so the wrapper's own refusal of *both at once* is
    a defence against a caller this function cannot be, not a case it has to handle.

    The marker is re-checked here even though the route already refused a malformed one
    (:mod:`basecradle_router.marker`), because this is the last point before it becomes
    an ``argv`` element crossing ``sudo``. The fail-direction is a **refusal**, not the
    delivery id's silent drop: a probe with no carryable marker has nothing left to
    prove, and the one thing it must never do is degrade into something that wakes the
    model.
    """
    if not event.synthetic:
        return ("--", *wake_command(agent, event))
    if not is_marker(event.wake_arg):
        raise WakeError(
            f"refusing to wake {agent.key}: probe delivery {event.delivery_id} carries a "
            f"malformed BCNOC1 marker ({event.wake_arg!r})"
        )
    return (PROBE_FLAG, event.wake_arg)


@dataclass(frozen=True, slots=True)
class HomeServerWaker:
    """The deployable :class:`Waker` — runs the wake through the wake-runner wrapper.

    Assembles ``sudo <wrapper> --user <os_user> --cwd <clone> --delivery <id> --
    <wake command>`` and runs it, where the wake command is the agent-kind-specific
    :func:`wake_command` (``claude -p "<trigger>"`` for a builder, ``<wake_bin>
    --timeline "<uuid>"`` for a harness persona). The root-owned wrapper validates
    the request, drops to the agent's own OS user, and (as the agent) sources that
    agent's ``agent.env`` — so the router itself passes **no** environment and never
    holds a secret. The event value rides as a single ``argv`` element, never a
    shell string, so it is never re-interpreted. ``runner`` is the same injectable
    seam tests replace; ``wrapper`` is overridable for tests.

    ``--delivery`` is how the delivery id reaches the child *given* that the router
    passes no environment: the wrapper exports it as :data:`DELIVERY_ID_ENV` after
    the privilege drop. It sits before the ``--`` because everything after ``--`` is
    the inert wake command the wrapper pins against its registry.

    **A synthetic probe takes the wrapper's ``--probe`` mode instead of a command.**
    The wrapper then performs every step a real wake performs — validate the request
    against the root-owned registry, resolve the exact binary that agent's kind
    sanctions, drop to the agent, load its ``agent.env``, enter its clone — and, as
    the agent, verifies the marker against that agent's own ``NOC_PROBE_SECRET`` and
    acks. It stops one step short of ``exec``, which is the only step it *must* skip:
    exec'ing the model is what the fleet's zero-token-at-rest constraint forbids. The
    two modes are mutually exclusive in the wrapper's own argv parsing, so a probe
    cannot carry a command and a wake cannot carry a marker
    (basecradle-router#208).
    """

    runner: Runner = _run_subprocess
    wrapper: str = WAKE_RUNNER
    timeout: float | None = None

    def invocation_for(self, agent: Agent, event: Event) -> WakeInvocation:
        """Assemble the sudo+wrapper command — pure, no I/O."""
        delivery = _correlation_id(event)
        return WakeInvocation(
            argv=(
                "sudo",
                self.wrapper,
                "--user",
                agent.os_user,
                "--cwd",
                agent.clone_path,
                *(() if delivery is None else ("--delivery", delivery)),
                *_mode_args(agent, event),
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

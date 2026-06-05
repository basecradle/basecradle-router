"""Resolve a normalized event to the agent that must be woken.

The first stage of the core pipeline after a route hands back an
:class:`~basecradle_router.models.Event`: the event's ``target_repo`` is the
repo the handoff issue was filed on, and that repo's captain is the agent to
wake. Resolution is a pure registry lookup — no waking, no I/O — so the pipeline
decides *who* before it ever touches the home server.
"""

from __future__ import annotations

from basecradle_router.config import Config
from basecradle_router.models import Agent, Event


def resolve_agent(event: Event, config: Config) -> Agent:
    """Return the :class:`~basecradle_router.models.Agent` that owns ``event.target_repo``.

    Raises :class:`~basecradle_router.config.ConfigError`, naming the repo and
    the registry to fix, when no agent is registered for the target repo. In
    practice that is a registry gap rather than a stray event: GitHub only
    delivers webhooks for repos the App is installed on, so an unresolvable
    target means the agent registry was not updated to match.
    """
    return config.agent_for(event.target_repo)

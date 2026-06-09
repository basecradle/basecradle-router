"""Resolve a normalized event to the agent that must be woken.

The first stage of the core pipeline after a route hands back an
:class:`~basecradle_router.models.Event`: the event's
:class:`~basecradle_router.models.Recipient` is a source-tagged key (a github
handoff's target repo, a basecradle delivery's recipient uuid), and resolving it
yields the agent to wake. Resolution is a pure registry lookup — no waking, no
I/O — so the pipeline decides *who* before it ever touches the home server, and
stays source-agnostic: it dispatches on the recipient tag the route set without
knowing any source's specifics.
"""

from __future__ import annotations

from basecradle_router.config import Config
from basecradle_router.models import Agent, Event


def resolve_agent(event: Event, config: Config) -> Agent:
    """Return the :class:`~basecradle_router.models.Agent` for ``event.recipient``.

    Raises :class:`~basecradle_router.config.ConfigError`, naming the recipient
    and the registry to fix, when no agent is registered for it. In practice that
    is a registry gap rather than a stray event: a source only delivers events for
    agents it is wired to, so an unresolvable recipient means the agent registry
    was not updated to match.
    """
    return config.agent_for_recipient(event.recipient)

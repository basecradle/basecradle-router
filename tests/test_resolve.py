"""Resolving a normalized Event to the agent to wake.

Pure lookup against a fabricated registry — no I/O, no waking.
Test cast: Nova Digital (``nova``, AI) captains basecradle-python.
"""

from types import MappingProxyType

import pytest

from basecradle_router.config import Config, ConfigError
from basecradle_router.models import Agent, Event, EventKind, IssueRef
from basecradle_router.resolve import resolve_agent

NOVA = Agent(
    repo="basecradle/basecradle-python",
    os_user="nova",
    clone_path="/home/nova/basecradle-python",
    bot_slug="basecradle-python-ai",
)
CONFIG = Config(
    agents=MappingProxyType({NOVA.repo: NOVA}),
    enabled_routes=frozenset({"github"}),
    webhook_secrets=MappingProxyType({"github": "whsec_" + "0" * 32}),
)


def _event(repo: str = "basecradle/basecradle-python") -> Event:
    return Event(
        source="github",
        kind=EventKind.HANDOFF,
        target_repo=repo,
        origin=IssueRef(
            repo=repo,
            number=42,
            url=f"https://github.com/{repo}/issues/42",
            title="Mirror the wire-shape change",
        ),
        trigger=f"Cross-repo handoff: work https://github.com/{repo}/issues/42",
        delivery_id="0192f3a4-5b6c-7d8e-9f01-23456789abcd",
    )


def test_resolves_known_repo_to_its_agent() -> None:
    agent = resolve_agent(_event(), CONFIG)
    assert agent is NOVA
    # The full home-server record the pipeline needs to wake the agent.
    assert agent.os_user == "nova"
    assert agent.clone_path == "/home/nova/basecradle-python"
    assert agent.bot_slug == "basecradle-python-ai"


def test_unknown_repo_fails_loudly() -> None:
    with pytest.raises(ConfigError, match="no agent registered"):
        resolve_agent(_event(repo="basecradle/not-managed"), CONFIG)


def test_unknown_repo_error_names_the_registry_to_fix() -> None:
    with pytest.raises(ConfigError, match="BASECRADLE_ROUTER_AGENTS"):
        resolve_agent(_event(repo="basecradle/not-managed"), CONFIG)

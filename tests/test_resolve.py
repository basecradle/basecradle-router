"""Resolving a normalized Event to the agent to wake.

Pure lookup against a fabricated registry — no I/O, no waking.
Test cast: Nova Digital (``nova``, AI) captains basecradle-python.
"""

from types import MappingProxyType

import pytest

from basecradle_router.config import Config, ConfigError
from basecradle_router.models import (
    Agent,
    Event,
    EventKind,
    IssueRef,
    Recipient,
    WakeKind,
)
from basecradle_router.resolve import resolve_agent

NOVA = Agent(
    key="basecradle/basecradle-python",
    os_user="nova",
    clone_path="/home/nova/basecradle-python",
    bot_slug="basecradle-python-ai",
)
JT = Agent(
    key="jt",
    os_user="jt",
    clone_path="/home/jt/harness",
    wake_kind=WakeKind.HARNESS,
    recipient_uuid="019e916c-7f45-700e-afc0-f45557b237b7",
    wake_bin="/home/jt/venv/bin/basecradle-harness-wake",
)
CONFIG = Config(
    agents=MappingProxyType({NOVA.key: NOVA, JT.key: JT}),
    enabled_routes=frozenset({"github", "basecradle"}),
    webhook_secrets=MappingProxyType({"github": "whsec_" + "0" * 32}),
    recipient_index=MappingProxyType({JT.recipient_uuid: JT}),
)


def _github_event(repo: str = "basecradle/basecradle-python") -> Event:
    return Event(
        source="github",
        kind=EventKind.HANDOFF,
        recipient=Recipient(by="repo", value=repo),
        wake_arg=f"Cross-repo handoff: work https://github.com/{repo}/issues/42",
        delivery_id="0192f3a4-5b6c-7d8e-9f01-23456789abcd",
        origin=IssueRef(
            repo=repo,
            number=42,
            url=f"https://github.com/{repo}/issues/42",
            title="Mirror the wire-shape change",
        ),
    )


def _basecradle_event(recipient_uuid: str) -> Event:
    return Event(
        source="basecradle",
        kind=EventKind.PLATFORM_EVENT,
        recipient=Recipient(by="recipient_uuid", value=recipient_uuid),
        wake_arg="0192aaaa-bbbb-7ccc-8ddd-eeeeffff0000",
        delivery_id="0192f3a4-5b6c-7d8e-9f01-23456789abcd",
    )


def test_resolves_known_repo_to_its_agent() -> None:
    agent = resolve_agent(_github_event(), CONFIG)
    assert agent is NOVA
    # The full home-server record the pipeline needs to wake the agent.
    assert agent.os_user == "nova"
    assert agent.clone_path == "/home/nova/basecradle-python"
    assert agent.bot_slug == "basecradle-python-ai"


def test_resolves_basecradle_event_by_recipient_uuid() -> None:
    # A basecradle event resolves by the recipient's BaseCradle user uuid, not a
    # repo — the seam the whole route exists to prove.
    agent = resolve_agent(_basecradle_event(JT.recipient_uuid), CONFIG)
    assert agent is JT
    assert agent.os_user == "jt"


def test_unknown_repo_fails_loudly() -> None:
    with pytest.raises(ConfigError, match="no agent registered"):
        resolve_agent(_github_event(repo="basecradle/not-managed"), CONFIG)


def test_unknown_repo_error_names_the_registry_to_fix() -> None:
    with pytest.raises(ConfigError, match="BASECRADLE_ROUTER_AGENTS"):
        resolve_agent(_github_event(repo="basecradle/not-managed"), CONFIG)


def test_unknown_recipient_uuid_fails_loudly() -> None:
    with pytest.raises(ConfigError, match="no agent registered for recipient_uuid"):
        resolve_agent(_basecradle_event("019e0000-0000-7000-8000-000000000000"), CONFIG)

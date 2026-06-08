"""Shape and validation invariants for the core vocabulary.

Test cast: John Doe (``john``, human) and Nova Digital (``nova``, AI).
"""

from dataclasses import FrozenInstanceError

import pytest

from basecradle_router.models import Agent, Event, EventKind, IssueRef

DELIVERY_ID = "0192f3a4-5b6c-7d8e-9f01-23456789abcd"  # well-formed UUIDv7


def _issue() -> IssueRef:
    return IssueRef(
        repo="basecradle/basecradle-python",
        number=42,
        url="https://github.com/basecradle/basecradle-python/issues/42",
        title="Mirror the wire-shape change",
    )


def _event() -> Event:
    return Event(
        source="github",
        kind=EventKind.HANDOFF,
        target_repo="basecradle/basecradle-python",
        origin=_issue(),
        trigger="Cross-repo handoff: work https://github.com/basecradle/basecradle-python/issues/42",
        delivery_id=DELIVERY_ID,
    )


def test_event_round_trips_and_is_frozen() -> None:
    event = _event()
    assert event.kind is EventKind.HANDOFF
    assert event.target_repo == "basecradle/basecradle-python"
    assert event.origin.number == 42
    with pytest.raises(FrozenInstanceError):
        event.trigger = "tampered"  # type: ignore[misc]


def test_agent_round_trips() -> None:
    agent = Agent(
        repo="basecradle/basecradle-python",
        os_user="nova",
        clone_path="/home/nova/basecradle-python",
        bot_slug="basecradle-python-ai",
    )
    assert agent.os_user == "nova"


def test_harness_key_is_the_os_user_not_the_repo() -> None:
    # The serialization key is the agent's one harness instance — its OS user —
    # so every input source for the agent funnels onto the same lock. It must be
    # the OS-user identity, never the (GitHub-shaped) repo, which a non-GitHub
    # input need not carry. See Agent.harness_key and issue #78.
    agent = Agent(
        repo="basecradle/basecradle-python",
        os_user="nova",
        clone_path="/home/nova/basecradle-python",
        bot_slug="basecradle-python-ai",
    )
    assert agent.harness_key == "nova"
    assert agent.harness_key == agent.os_user
    assert agent.harness_key != agent.repo


@pytest.mark.parametrize("repo", ["", "no-slash", "too/many/slashes", "/leading", "trailing/"])
def test_repo_must_be_owner_slash_name(repo: str) -> None:
    with pytest.raises(ValueError, match="owner/name|non-empty"):
        Agent(repo=repo, os_user="nova", clone_path="/c", bot_slug="b")


@pytest.mark.parametrize("number", [0, -1])
def test_issue_number_must_be_positive(number: int) -> None:
    with pytest.raises(ValueError, match="positive int"):
        IssueRef(repo="basecradle/x", number=number, url="https://x", title="t")


def test_empty_required_fields_reject() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        IssueRef(repo="basecradle/x", number=1, url="", title="t")
    with pytest.raises(ValueError, match="non-empty"):
        Agent(repo="basecradle/x", os_user="", clone_path="/c", bot_slug="b")


def test_event_rejects_wrong_typed_members() -> None:
    with pytest.raises(ValueError, match="EventKind"):
        Event(
            source="github",
            kind="handoff",  # type: ignore[arg-type]
            target_repo="basecradle/x",
            origin=_issue(),
            trigger="t",
            delivery_id=DELIVERY_ID,
        )
    with pytest.raises(ValueError, match="IssueRef"):
        Event(
            source="github",
            kind=EventKind.HANDOFF,
            target_repo="basecradle/x",
            origin="not-an-issue",  # type: ignore[arg-type]
            trigger="t",
            delivery_id=DELIVERY_ID,
        )

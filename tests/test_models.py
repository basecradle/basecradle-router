"""Shape and validation invariants for the core vocabulary.

Test cast: John Doe (``john``, human) and Nova Digital (``nova``, AI); plus the
fleet harness persona @jt (``jt``, a non-builder agent) for the non-repo shape.
"""

from dataclasses import FrozenInstanceError

import pytest

from basecradle_router.models import (
    Agent,
    Event,
    EventKind,
    IssueRef,
    Recipient,
    WakeKind,
)

DELIVERY_ID = "0192f3a4-5b6c-7d8e-9f01-23456789abcd"  # well-formed UUIDv7
JT_UUID = "019e916c-7f45-700e-afc0-f45557b237b7"  # @jt's BaseCradle user uuid


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
        recipient=Recipient(by="repo", value="basecradle/basecradle-python"),
        wake_arg="Cross-repo handoff: work https://github.com/basecradle/basecradle-python/issues/42",
        delivery_id=DELIVERY_ID,
        origin=_issue(),
    )


def test_event_round_trips_and_is_frozen() -> None:
    event = _event()
    assert event.kind is EventKind.HANDOFF
    assert event.recipient == Recipient(by="repo", value="basecradle/basecradle-python")
    assert event.origin.number == 42
    with pytest.raises(FrozenInstanceError):
        event.wake_arg = "tampered"  # type: ignore[misc]


def test_event_origin_is_optional_for_sources_without_one() -> None:
    # A basecradle platform event has no issue to report on — origin is None.
    event = Event(
        source="basecradle",
        kind=EventKind.PLATFORM_EVENT,
        recipient=Recipient(by="recipient_uuid", value=JT_UUID),
        wake_arg="0192f3a4-5b6c-7d8e-9f01-23456789abcd",
        delivery_id=DELIVERY_ID,
    )
    assert event.origin is None
    assert event.recipient.by == "recipient_uuid"


def test_dedup_key_pairs_the_source_with_the_delivery_id() -> None:
    # The dedup key is source-prefixed so two sources' id-spaces can't collide,
    # and identical across duplicate deliveries of one event (same delivery_id).
    assert _event().dedup_key == f"github:{DELIVERY_ID}"
    basecradle_event = Event(
        source="basecradle",
        kind=EventKind.PLATFORM_EVENT,
        recipient=Recipient(by="recipient_uuid", value=JT_UUID),
        wake_arg="0192aaaa-bbbb-7ccc-8ddd-eeeeffff0000",
        delivery_id=DELIVERY_ID,
    )
    # Same delivery_id, different source → distinct keys (no cross-source collision).
    assert basecradle_event.dedup_key == f"basecradle:{DELIVERY_ID}"
    assert basecradle_event.dedup_key != _event().dedup_key


def test_builder_agent_round_trips() -> None:
    agent = Agent(
        key="basecradle/basecradle-python",
        os_user="nova",
        clone_path="/home/nova/basecradle-python",
        bot_slug="basecradle-python-ai",
    )
    assert agent.os_user == "nova"
    assert agent.wake_kind is WakeKind.CLAUDE  # the default


def test_harness_persona_agent_round_trips() -> None:
    # A non-builder persona: no repo, no bot_slug — a slug key, a wake_bin, and a
    # recipient_uuid its basecradle events resolve by.
    agent = Agent(
        key="jt",
        os_user="jt",
        clone_path="/home/jt/harness",
        wake_kind=WakeKind.HARNESS,
        recipient_uuid=JT_UUID,
        wake_bin="/home/jt/venv/bin/basecradle-harness-wake",
    )
    assert agent.harness_key == "jt"
    assert agent.recipient_uuid == JT_UUID
    assert agent.wake_bin.endswith("basecradle-harness-wake")


def test_harness_key_is_the_os_user_not_the_key() -> None:
    # The serialization key is the agent's one harness instance — its OS user — so
    # every input source for the agent funnels onto the same lock. It must be the
    # OS-user identity, never the (GitHub-shaped) registry key, which a non-GitHub
    # input need not share. See Agent.harness_key and issue #78.
    agent = Agent(
        key="basecradle/basecradle-python",
        os_user="nova",
        clone_path="/home/nova/basecradle-python",
        bot_slug="basecradle-python-ai",
    )
    assert agent.harness_key == "nova"
    assert agent.harness_key == agent.os_user
    assert agent.harness_key != agent.key


def test_builder_requires_a_bot_slug() -> None:
    with pytest.raises(ValueError, match="bot_slug"):
        Agent(key="basecradle/x", os_user="nova", clone_path="/c")


def test_harness_requires_wake_bin_and_recipient_uuid() -> None:
    with pytest.raises(ValueError, match="wake_bin"):
        Agent(
            key="jt",
            os_user="jt",
            clone_path="/home/jt/harness",
            wake_kind=WakeKind.HARNESS,
            recipient_uuid=JT_UUID,
        )
    with pytest.raises(ValueError, match="recipient_uuid"):
        Agent(
            key="jt",
            os_user="jt",
            clone_path="/home/jt/harness",
            wake_kind=WakeKind.HARNESS,
            wake_bin="/home/jt/venv/bin/basecradle-harness-wake",
        )


@pytest.mark.parametrize("number", [0, -1])
def test_issue_number_must_be_positive(number: int) -> None:
    with pytest.raises(ValueError, match="positive int"):
        IssueRef(repo="basecradle/x", number=number, url="https://x", title="t")


def test_empty_required_fields_reject() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        IssueRef(repo="basecradle/x", number=1, url="", title="t")
    with pytest.raises(ValueError, match="non-empty"):
        Agent(key="basecradle/x", os_user="", clone_path="/c", bot_slug="b")
    with pytest.raises(ValueError, match="non-empty"):
        Recipient(by="repo", value="")


def test_event_rejects_wrong_typed_members() -> None:
    with pytest.raises(ValueError, match="EventKind"):
        Event(
            source="github",
            kind="handoff",  # type: ignore[arg-type]
            recipient=Recipient(by="repo", value="basecradle/x"),
            wake_arg="t",
            delivery_id=DELIVERY_ID,
        )
    with pytest.raises(ValueError, match="Recipient"):
        Event(
            source="github",
            kind=EventKind.HANDOFF,
            recipient="basecradle/x",  # type: ignore[arg-type]
            wake_arg="t",
            delivery_id=DELIVERY_ID,
        )
    with pytest.raises(ValueError, match="IssueRef"):
        Event(
            source="github",
            kind=EventKind.HANDOFF,
            recipient=Recipient(by="repo", value="basecradle/x"),
            wake_arg="t",
            delivery_id=DELIVERY_ID,
            origin="not-an-issue",  # type: ignore[arg-type]
        )

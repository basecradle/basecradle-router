"""The core's source-agnostic vocabulary.

A route module's only job is to turn one event source's raw payload into an
:class:`Event` — "wake agent X with trigger Y". The core pipeline consumes
``Event`` and knows nothing about GitHub, BaseCradle, or any other source, so
no source-specific field may appear here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EventKind(Enum):
    """What a normalized event asks the core to do.

    v0 has a single kind; the enum exists so a new event source adds a member
    rather than reshaping :class:`Event`.
    """

    HANDOFF = "handoff"


def _require(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _require_repo(value: str, field: str) -> str:
    _require(value, field)
    owner, _, name = value.partition("/")
    if not owner or not name or "/" in name:
        raise ValueError(f"{field} must be 'owner/name', got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class IssueRef:
    """The issue an event originated from — where the woken agent reports back."""

    repo: str
    number: int
    url: str
    title: str

    def __post_init__(self) -> None:
        _require_repo(self.repo, "IssueRef.repo")
        if not isinstance(self.number, int) or self.number <= 0:
            raise ValueError(f"IssueRef.number must be a positive int, got {self.number!r}")
        _require(self.url, "IssueRef.url")
        _require(self.title, "IssueRef.title")


@dataclass(frozen=True, slots=True)
class Agent:
    """A fleet agent the router can wake: the captain of one repo.

    The home-server fields (``os_user``, ``clone_path``) describe *where* to run
    the agent's headless Claude Code; the router only ever delivers a trigger to
    it — it never becomes the agent.
    """

    repo: str
    os_user: str
    clone_path: str
    bot_slug: str

    def __post_init__(self) -> None:
        _require_repo(self.repo, "Agent.repo")
        _require(self.os_user, "Agent.os_user")
        _require(self.clone_path, "Agent.clone_path")
        _require(self.bot_slug, "Agent.bot_slug")


@dataclass(frozen=True, slots=True)
class Event:
    """A normalized, source-agnostic event: wake ``target_repo``'s captain.

    ``target_repo`` is the repo the handoff issue was filed on (its captain is
    the one to wake); ``trigger`` is the exact prompt handed to ``claude -p``.
    """

    source: str
    kind: EventKind
    target_repo: str
    origin: IssueRef
    trigger: str
    delivery_id: str

    def __post_init__(self) -> None:
        _require(self.source, "Event.source")
        if not isinstance(self.kind, EventKind):
            raise ValueError(f"Event.kind must be an EventKind, got {self.kind!r}")
        _require_repo(self.target_repo, "Event.target_repo")
        if not isinstance(self.origin, IssueRef):
            raise ValueError(f"Event.origin must be an IssueRef, got {self.origin!r}")
        _require(self.trigger, "Event.trigger")
        _require(self.delivery_id, "Event.delivery_id")

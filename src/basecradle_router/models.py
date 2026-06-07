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

    @property
    def harness_key(self) -> str:
        """The identity of this agent's one harness instance — the serialization key.

        The constitution makes an agent's identity **unified across every input
        path**: every channel that can address the agent (a GitHub event, a
        BaseCradle message, any future source) converges on a single
        identity-and-memory locus — today, one harness instance running as this
        agent's own OS user, against its own home and memory. The router enforces
        that convergence by serializing *every* input for the agent on this one
        key, so two sources can never fan into two parallel sessions writing the
        same memory (the split-brain the principle exists to prevent).

        The OS user *is* that instance's identity — slug == OS user == home, one
        agent to one harness instance — so it is the key. It is deliberately
        **not** ``repo``: a repo is a GitHub-shaped notion a non-GitHub input
        need not carry, whereas every input resolves to an agent. Keying on the
        repo only happens to work while GitHub is the sole source; keying on the
        agent is correct for all of them.
        """
        return self.os_user


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

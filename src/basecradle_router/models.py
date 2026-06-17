"""The core's source-agnostic vocabulary.

A route module's only job is to turn one event source's raw payload into an
:class:`Event` — "wake agent X with argument Y". The core pipeline consumes
``Event`` and knows nothing about GitHub, BaseCradle, or any other source, so
no source-specific field may appear here.

The vocabulary is deliberately keyed on **the agent**, not the repo. A repo is a
GitHub-shaped notion a non-GitHub input (a BaseCradle platform event) need not
carry; every input, whatever its source, resolves to an agent. So an event names
its agent through a source-tagged :class:`Recipient` the core resolves without
knowing any source's specifics, and an :class:`Agent` carries a stable ``key``
(its slug) plus a :class:`WakeKind` that says how to wake it — a builder's
``claude -p`` or a harness persona's own wake CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EventKind(Enum):
    """What a normalized event asks the core to do.

    Each event source adds a member rather than reshaping :class:`Event`:
    ``HANDOFF`` is a GitHub handoff issue; ``PLATFORM_EVENT`` is a BaseCradle
    timeline event (a new message on a timeline the agent views).
    """

    HANDOFF = "handoff"
    PLATFORM_EVENT = "platform_event"


class WakeKind(Enum):
    """How an agent's one harness instance is woken.

    ``CLAUDE`` — a builder agent's headless Claude Code: ``claude -p "<arg>"``.
    ``HARNESS`` — a non-builder harness persona's own wake CLI, invoked as
    ``<wake_bin> --timeline "<arg>"``. The single event value (the trigger, or
    the timeline uuid) rides as one inert argv element either way.
    """

    CLAUDE = "claude"
    HARNESS = "harness"


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
class Recipient:
    """How the core resolves which agent to wake — a source-tagged registry lookup.

    ``by`` names the index to look in (``"repo"`` for a GitHub handoff's target
    repo, ``"recipient_uuid"`` for a BaseCradle delivery's recipient user);
    ``value`` is the key within it. The route sets the tag for its source and the
    core dispatches on it without ever naming GitHub or BaseCradle — the seam
    that keeps resolution source-agnostic now that an event need not carry a repo.
    """

    by: str
    value: str

    def __post_init__(self) -> None:
        _require(self.by, "Recipient.by")
        _require(self.value, "Recipient.value")


@dataclass(frozen=True, slots=True)
class Agent:
    """A fleet agent the router can wake.

    ``key`` is the agent's stable slug — its identity in the registry and the
    target of resolution. For a GitHub builder it is the ``owner/name`` repo it
    captains; for a harness persona it is its bare slug (e.g. ``jt``). The
    home-server fields (``os_user``, ``clone_path``) describe *where* to run the
    wake; ``wake_kind`` describes *how*. The router only ever delivers a trigger —
    it never becomes the agent.

    Source-specific fields are optional and carried only for the kind that needs
    them: ``bot_slug`` (a builder's GitHub App bot identity); ``recipient_uuid``
    (a harness persona's BaseCradle user uuid, the key its events resolve by);
    ``wake_bin`` (a harness persona's wake CLI, the only command the home-server
    wrapper will launch for it).
    """

    key: str
    os_user: str
    clone_path: str
    wake_kind: WakeKind = WakeKind.CLAUDE
    bot_slug: str | None = None
    recipient_uuid: str | None = None
    wake_bin: str | None = None

    def __post_init__(self) -> None:
        _require(self.key, "Agent.key")
        _require(self.os_user, "Agent.os_user")
        _require(self.clone_path, "Agent.clone_path")
        if not isinstance(self.wake_kind, WakeKind):
            raise ValueError(f"Agent.wake_kind must be a WakeKind, got {self.wake_kind!r}")
        if self.wake_kind is WakeKind.CLAUDE:
            _require(self.bot_slug, "Agent.bot_slug")
        if self.wake_kind is WakeKind.HARNESS:
            _require(self.wake_bin, "Agent.wake_bin")
            _require(self.recipient_uuid, "Agent.recipient_uuid")

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
        **not** the repo: a repo is a GitHub-shaped notion a non-GitHub input
        need not carry, whereas every input resolves to an agent. Keying on the
        repo only happens to work while GitHub is the sole source; keying on the
        agent is correct for all of them.
        """
        return self.os_user


@dataclass(frozen=True, slots=True)
class Event:
    """A normalized, source-agnostic event: wake ``recipient``'s agent.

    ``recipient`` is the source-tagged key the core resolves to an agent;
    ``wake_arg`` is the single inert value handed to that agent's wake command (a
    GitHub handoff's trigger prompt, or a BaseCradle event's timeline uuid).
    ``origin`` records where the agent reports back when the source has such a
    place (a GitHub issue) — informational, and ``None`` for sources that don't
    (a harness persona replies on its timeline itself).
    """

    source: str
    kind: EventKind
    recipient: Recipient
    wake_arg: str
    delivery_id: str
    origin: IssueRef | None = None

    def __post_init__(self) -> None:
        _require(self.source, "Event.source")
        if not isinstance(self.kind, EventKind):
            raise ValueError(f"Event.kind must be an EventKind, got {self.kind!r}")
        if not isinstance(self.recipient, Recipient):
            raise ValueError(f"Event.recipient must be a Recipient, got {self.recipient!r}")
        _require(self.wake_arg, "Event.wake_arg")
        _require(self.delivery_id, "Event.delivery_id")
        if self.origin is not None and not isinstance(self.origin, IssueRef):
            raise ValueError(f"Event.origin must be an IssueRef or None, got {self.origin!r}")

    @property
    def stream_key(self) -> str:
        """A stable id for the finest-grained wake sub-stream this event belongs to.

        The per-(agent, stream) wake-rate breaker keys on this, so one looping
        timeline or issue trips even while the agent's overall rate stays under the
        per-agent cap. A source that reports an ``origin`` (a github handoff issue)
        keys on the issue ``url``; one that does not (a basecradle timeline event)
        keys on its ``wake_arg`` (the timeline uuid). Both are stable across a loop's
        repeated wakes and compact enough to log — and both are core vocabulary, so
        this stays source-agnostic.
        """
        return self.origin.url if self.origin is not None else self.wake_arg

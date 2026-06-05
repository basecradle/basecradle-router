"""Graduated merge autonomy: decide whether a resulting PR may auto-merge.

The fleet merges its own low-risk work but never its risky work. This engine
encodes that line, in three outcomes:

- **auto-merge** — clearly low-risk (docs-only, or a *labelled* shared-block
  re-sync) — merge automatically, but only once CI is green.
- **human review** — code/behavioural changes — wait for a human; never
  auto-merge.
- **pause** — a genuine human gate (a release, a credential change, a scope/
  charter decision) — stop and hand it to the founder.

The classifier is **conservative and fail-safe**: gates win over everything, and
anything not *clearly* low-risk falls through to human review. Editing the
charter (``CLAUDE.md``) or the constitution is a scope decision, not low-risk
docs — so an unmarked governance edit pauses, while a shared-block re-sync (which
legitimately rewrites the shared block *in* ``CLAUDE.md``) is low-risk only
because it is explicitly labelled.

The decision is pure on the change class; CI-green is the action-time gate, so
:func:`decide` stays a pure class→decision map and the merge action enforces
"auto-merge only on green". The GitHub merge call is the injected
:class:`Merger` seam — mocked in tests, real on the home server.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class ChangeClass(Enum):
    """The risk class of a PR's change — what kind of thing it touches."""

    DOCS = "docs"
    SHARED_BLOCK_RESYNC = "shared_block_resync"
    CODE = "code"
    RELEASE = "release"
    CREDENTIAL = "credential"
    SCOPE = "scope"


class MergeDecision(Enum):
    """What the router may do with a PR of a given class."""

    AUTO_MERGE = "auto_merge"
    HUMAN_REVIEW = "human_review"
    PAUSE = "pause"


_DECISION_BY_CLASS = {
    ChangeClass.DOCS: MergeDecision.AUTO_MERGE,
    ChangeClass.SHARED_BLOCK_RESYNC: MergeDecision.AUTO_MERGE,
    ChangeClass.CODE: MergeDecision.HUMAN_REVIEW,
    ChangeClass.RELEASE: MergeDecision.PAUSE,
    ChangeClass.CREDENTIAL: MergeDecision.PAUSE,
    ChangeClass.SCOPE: MergeDecision.PAUSE,
}

# Explicit, deliberate labels a PR carries to declare a human gate.
_GATE_LABELS = {
    "release": ChangeClass.RELEASE,
    "credential": ChangeClass.CREDENTIAL,
    "credentials": ChangeClass.CREDENTIAL,
    "scope": ChangeClass.SCOPE,
}
# The label a mechanical shared-block re-sync carries.
RESYNC_LABEL = "shared-block-resync"
# Governance files: editing one (unmarked) is a scope decision, never plain docs.
_GOVERNANCE_FILES = frozenset({"CLAUDE.md", "constitution.md"})
# What counts as low-risk documentation: Markdown only. The fleet writes Markdown,
# so a broader rule (``.txt``/``.rst``, or anything under ``docs/``) would let
# behavioural files — ``requirements.txt``, ``docs/deploy.sh`` — masquerade as docs
# and auto-merge. Non-Markdown docs fall to human review: the safe direction.
_DOC_SUFFIXES = (".md",)


@dataclass(frozen=True, slots=True)
class PullRequest:
    """The PR signals the policy reasons over — no live GitHub access needed."""

    number: int
    repo: str
    changed_paths: tuple[str, ...]
    labels: frozenset[str]
    ci_green: bool


@runtime_checkable
class Merger(Protocol):
    """The GitHub merge seam: perform the merge. Mocked in tests."""

    def merge(self, pr: PullRequest) -> None: ...


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _is_doc(path: str) -> bool:
    return path.endswith(_DOC_SUFFIXES)


def classify(pr: PullRequest) -> ChangeClass:
    """Assign ``pr`` its risk class — conservative, gates first.

    Precedence: an explicit gate label → that gate; a labelled shared-block
    re-sync → low-risk re-sync (even though it edits ``CLAUDE.md``); an unmarked
    governance-file edit → scope; an all-documentation change → docs; everything
    else → code. Anything ambiguous lands on ``CODE`` (human review), never on an
    auto-merge.
    """
    # Match labels case-insensitively so a title-cased "Release" cannot slip a
    # gate and let an otherwise low-risk PR auto-merge.
    labels = {label.lower() for label in pr.labels}

    # Explicit human gates win over everything. Sorted for a deterministic class
    # when several gate labels are present (the decision is PAUSE either way).
    for label in sorted(labels):
        gate = _GATE_LABELS.get(label)
        if gate is not None:
            return gate

    # A marked shared-block re-sync is low-risk even though it rewrites CLAUDE.md.
    if RESYNC_LABEL in labels:
        return ChangeClass.SHARED_BLOCK_RESYNC

    # An unmarked charter/constitution edit is a scope decision, not docs.
    if any(_basename(path) in _GOVERNANCE_FILES for path in pr.changed_paths):
        return ChangeClass.SCOPE

    if pr.changed_paths and all(_is_doc(path) for path in pr.changed_paths):
        return ChangeClass.DOCS

    return ChangeClass.CODE


def decide(change_class: ChangeClass) -> MergeDecision:
    """Map a change class to its merge decision — pure, total over the enum."""
    return _DECISION_BY_CLASS[change_class]


@dataclass(frozen=True, slots=True)
class MergePolicy:
    """The merge action: classify a PR, decide, and act through the merger seam."""

    merger: Merger

    def evaluate(self, pr: PullRequest) -> MergeDecision:
        """The pure decision for ``pr`` — no merge, no I/O."""
        return decide(classify(pr))

    def run(self, pr: PullRequest) -> MergeDecision:
        """Evaluate ``pr`` and merge it iff it is auto-merge class **and** green.

        Returns the decision so the caller can report it (announce a pause, note
        a PR left for review). The merger is called only on the auto-merge path.
        """
        decision = self.evaluate(pr)
        if decision is MergeDecision.AUTO_MERGE and pr.ci_green:
            self.merger.merge(pr)
        return decision

"""Merge policy: auto-merge a resulting PR once CI is green — no human gate here.

Under the constitution's **Earned Autonomy** principle, a per-PR human review of a
captain's own code is a *training wheel*, not a fixture: the destination is zero
human gates, and a PR merge to ``main`` is reversible, so it never needs one. This
policy therefore gates on exactly one thing — **green CI** — and nothing else; code,
docs, and charter edits all merge the same way. There is deliberately no risk
classification here: a classifier that withholds a captain's own merge *is* the
training wheel the principle retires.

The genuine firebreaks live elsewhere, by design. The *irreversible, outward* step
— publish/deploy — is gated at the **platform**: a GitHub Environment approval on the
release/deploy job, and only for agents that have not yet earned the trust to act
unsupervised there, retired as each captain matures. That separation is the point:
this module is the *merge* path (green-only, no human), and the platform is the
*deploy* gate (a training wheel, scaled to blast radius). We do not re-encode a
per-PR human review here.

The GitHub merge call is the injected :class:`Merger` seam — mocked in tests, real on
the home server.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class PullRequest:
    """The PR signals the policy reasons over — only its CI state gates the merge.

    The policy is content-blind by design (Earned Autonomy): it does not look at the
    changed paths or labels, because there is no per-PR human gate to route them to.
    """

    number: int
    repo: str
    ci_green: bool


@runtime_checkable
class Merger(Protocol):
    """The GitHub merge seam: perform the merge. Mocked in tests."""

    def merge(self, pr: PullRequest) -> None: ...


@dataclass(frozen=True, slots=True)
class MergePolicy:
    """Auto-merge a PR once its CI is green — the whole policy, per Earned Autonomy."""

    merger: Merger

    def run(self, pr: PullRequest) -> bool:
        """Merge ``pr`` iff its CI is green; return whether it was merged.

        Green is the sole action-time gate — there is no per-PR human review. A red
        PR is simply *not merged yet* (a later CI run, or the captain itself,
        resolves it); it is never "paused for a human." The firebreak for the
        irreversible step lives at the platform deploy gate, not here.
        """
        if pr.ci_green:
            self.merger.merge(pr)
            return True
        return False

"""The merge policy: auto-merge on green, no per-PR human gate (Earned Autonomy).

Green CI is the only action-time gate. The policy is content-blind by design — it
never reasons over a PR's files or labels, because there is no human-review tier to
route them to; the firebreak for the irreversible step lives at the platform deploy
gate, not here. So these tests pin exactly that: green merges, red does not, and the
change's nature makes no difference.
"""

from basecradle_router.merge_policy import MergePolicy, Merger, PullRequest


class _RecordingMerger:
    """A :class:`Merger` that records the PRs it was asked to merge."""

    def __init__(self) -> None:
        self.merged: list[PullRequest] = []

    def merge(self, pr: PullRequest) -> None:
        self.merged.append(pr)


def _pr(*, number: int = 99, repo: str = "basecradle/basecradle-python", ci_green: bool = True):
    return PullRequest(number=number, repo=repo, ci_green=ci_green)


def test_recording_merger_satisfies_the_protocol() -> None:
    assert isinstance(_RecordingMerger(), Merger)


def test_green_pr_auto_merges() -> None:
    merger = _RecordingMerger()
    pr = _pr(ci_green=True)
    assert MergePolicy(merger).run(pr) is True
    assert merger.merged == [pr]


def test_red_pr_is_not_merged() -> None:
    # Not green → not merged *yet* (a later CI run or the captain resolves it). This
    # is the absence of a merge, not a human pause.
    merger = _RecordingMerger()
    assert MergePolicy(merger).run(_pr(ci_green=False)) is False
    assert merger.merged == []


def test_policy_is_content_blind_every_green_pr_merges_alike() -> None:
    # Earned Autonomy: there is no code/docs/charter/"release" distinction — the
    # PullRequest carries no such signal and the policy gates only on green. Several
    # green PRs all merge identically; none is held back for a per-PR human review.
    merger = _RecordingMerger()
    policy = MergePolicy(merger)
    prs = [_pr(number=1), _pr(number=2), _pr(number=3)]
    assert all(policy.run(pr) for pr in prs)
    assert merger.merged == prs

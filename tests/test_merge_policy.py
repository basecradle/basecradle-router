"""Graduated merge autonomy — offline, the merge API mocked at the boundary.

Each PR class maps to its decision; auto-merge fires only on green CI for the
low-risk classes; code and gated classes never call merge.
"""

from basecradle_router.merge_policy import (
    RESYNC_LABEL,
    ChangeClass,
    MergeDecision,
    MergePolicy,
    Merger,
    PullRequest,
    classify,
    decide,
)


def _pr(
    *,
    changed_paths: tuple[str, ...] = ("src/basecradle_router/wake.py",),
    labels: frozenset[str] = frozenset(),
    ci_green: bool = True,
    number: int = 7,
) -> PullRequest:
    return PullRequest(
        number=number,
        repo="basecradle/basecradle-python",
        changed_paths=changed_paths,
        labels=labels,
        ci_green=ci_green,
    )


class _RecordingMerger:
    """A mock merge boundary: records the PRs it was asked to merge."""

    def __init__(self) -> None:
        self.merged: list[PullRequest] = []

    def merge(self, pr: PullRequest) -> None:
        self.merged.append(pr)


def test_recording_merger_satisfies_the_protocol() -> None:
    assert isinstance(_RecordingMerger(), Merger)


# --- classify --------------------------------------------------------------


def test_docs_only_change_is_docs() -> None:
    assert classify(_pr(changed_paths=("README.md", "docs/architecture.md"))) is ChangeClass.DOCS


def test_code_change_is_code() -> None:
    assert classify(_pr(changed_paths=("src/basecradle_router/wake.py",))) is ChangeClass.CODE


def test_mixed_code_and_docs_is_code() -> None:
    # Not *all* docs → falls through to code (conservative).
    assert classify(_pr(changed_paths=("README.md", "src/x.py"))) is ChangeClass.CODE


def test_empty_change_set_is_code() -> None:
    # Vacuous "all docs" must not classify an empty diff as auto-mergeable.
    assert classify(_pr(changed_paths=())) is ChangeClass.CODE


def test_labelled_resync_is_low_risk_even_when_it_edits_claude_md() -> None:
    pr = _pr(changed_paths=("CLAUDE.md",), labels=frozenset({RESYNC_LABEL}))
    assert classify(pr) is ChangeClass.SHARED_BLOCK_RESYNC


def test_unmarked_claude_md_edit_is_scope() -> None:
    assert classify(_pr(changed_paths=("CLAUDE.md",))) is ChangeClass.SCOPE


def test_unmarked_constitution_edit_is_scope() -> None:
    pr = _pr(changed_paths=("docs/constitution.md",))
    assert classify(pr) is ChangeClass.SCOPE


def test_gate_labels_map_to_their_classes() -> None:
    assert classify(_pr(labels=frozenset({"release"}))) is ChangeClass.RELEASE
    assert classify(_pr(labels=frozenset({"credential"}))) is ChangeClass.CREDENTIAL
    assert classify(_pr(labels=frozenset({"credentials"}))) is ChangeClass.CREDENTIAL
    assert classify(_pr(labels=frozenset({"scope"}))) is ChangeClass.SCOPE


def test_non_markdown_under_docs_is_not_low_risk() -> None:
    # A script or manifest must never masquerade as a doc and auto-merge.
    assert classify(_pr(changed_paths=("docs/deploy.sh",))) is ChangeClass.CODE
    assert classify(_pr(changed_paths=("requirements.txt",))) is ChangeClass.CODE
    assert classify(_pr(changed_paths=("docs/notes.rst",))) is ChangeClass.CODE


def test_gate_label_is_case_insensitive() -> None:
    # A title-cased gate label must still gate — even on an otherwise docs-only PR
    # that would otherwise auto-merge.
    pr = _pr(changed_paths=("README.md",), labels=frozenset({"Release"}))
    assert classify(pr) is ChangeClass.RELEASE


def test_gate_label_beats_resync_and_docs() -> None:
    # A gate label wins even on an otherwise low-risk PR.
    pr = _pr(changed_paths=("README.md",), labels=frozenset({"release", RESYNC_LABEL}))
    assert classify(pr) is ChangeClass.RELEASE


# --- decide ----------------------------------------------------------------


def test_decide_maps_every_class() -> None:
    assert decide(ChangeClass.DOCS) is MergeDecision.AUTO_MERGE
    assert decide(ChangeClass.SHARED_BLOCK_RESYNC) is MergeDecision.AUTO_MERGE
    assert decide(ChangeClass.CODE) is MergeDecision.HUMAN_REVIEW
    assert decide(ChangeClass.RELEASE) is MergeDecision.PAUSE
    assert decide(ChangeClass.CREDENTIAL) is MergeDecision.PAUSE
    assert decide(ChangeClass.SCOPE) is MergeDecision.PAUSE


def test_decide_is_total_over_the_enum() -> None:
    # Every class must have a decision — no KeyError on any member.
    for change_class in ChangeClass:
        assert isinstance(decide(change_class), MergeDecision)


# --- MergePolicy.run (the mocked merge action) -----------------------------


def test_low_risk_on_green_auto_merges() -> None:
    merger = _RecordingMerger()
    pr = _pr(changed_paths=("README.md",), ci_green=True)
    decision = MergePolicy(merger).run(pr)
    assert decision is MergeDecision.AUTO_MERGE
    assert merger.merged == [pr]


def test_resync_on_green_auto_merges() -> None:
    merger = _RecordingMerger()
    pr = _pr(changed_paths=("CLAUDE.md",), labels=frozenset({RESYNC_LABEL}), ci_green=True)
    assert MergePolicy(merger).run(pr) is MergeDecision.AUTO_MERGE
    assert merger.merged == [pr]


def test_low_risk_on_red_ci_does_not_merge() -> None:
    merger = _RecordingMerger()
    pr = _pr(changed_paths=("README.md",), ci_green=False)
    # Still classified auto-merge, but the action withholds the merge on red CI.
    assert MergePolicy(merger).run(pr) is MergeDecision.AUTO_MERGE
    assert merger.merged == []


def test_code_never_auto_merges() -> None:
    merger = _RecordingMerger()
    pr = _pr(changed_paths=("src/basecradle_router/wake.py",), ci_green=True)
    assert MergePolicy(merger).run(pr) is MergeDecision.HUMAN_REVIEW
    assert merger.merged == []


def test_gated_classes_pause_and_do_not_merge() -> None:
    for label in ("release", "credentials", "scope"):
        merger = _RecordingMerger()
        pr = _pr(changed_paths=("README.md",), labels=frozenset({label}), ci_green=True)
        assert MergePolicy(merger).run(pr) is MergeDecision.PAUSE
        assert merger.merged == []


def test_unmarked_claude_md_edit_pauses_and_does_not_merge() -> None:
    merger = _RecordingMerger()
    pr = _pr(changed_paths=("CLAUDE.md",), ci_green=True)
    assert MergePolicy(merger).run(pr) is MergeDecision.PAUSE
    assert merger.merged == []


def test_evaluate_does_not_merge() -> None:
    merger = _RecordingMerger()
    MergePolicy(merger).evaluate(_pr(changed_paths=("README.md",), ci_green=True))
    assert merger.merged == []  # evaluating is pure; only run() acts

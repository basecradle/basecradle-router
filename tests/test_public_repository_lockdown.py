"""Public Repository Lockdown (#449): every public repository renewed, read back, and either
all clear (the heartbeat pings) or a gap (the ping is withheld and one issue says why).

The script's one boundary is :class:`GitHub`, every call through ``gh api``. Most tests
replace it with :class:`FakeGitHub`, a fabricated organization; the gateway's own tests
replace ``subprocess.run`` beneath it. Nothing here reaches GitHub, and no real limit is
removed to prove a gap: the gaps are fabricated answers.

The script lives in ``.github/scripts/``, which is not a package, so it is loaded from its
path, registered in ``sys.modules`` first because ``dataclasses`` looks its module up there.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".github" / "scripts" / "public_repository_lockdown.py"
WORKFLOW = REPO / ".github" / "workflows" / "interaction-limit-renewal.yml"

_spec = importlib.util.spec_from_file_location("public_repository_lockdown", SCRIPT)
assert _spec is not None and _spec.loader is not None
lockdown = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = lockdown
_spec.loader.exec_module(lockdown)

NOW = datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc)
RUN_URL = "https://github.com/basecradle/basecradle-router/actions/runs/1"
HOME = lockdown.HOME_REPOSITORY
# The bot this workflow writes as (basecradle#870), and the capital's, which wrote as it before.
OWN_BOT = "basecradle-router-ai[bot]"
CAPITAL = "basecradle-ai[bot]"
FLEET = ["basecradle/.github", "basecradle/basecradle-python", HOME]


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def reminder_issue(number: int, date: str, author: str = OWN_BOT, **extra: Any) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"⏰ Renew interaction limit before {date} (public-repo write-lockdown)",
        "body": f"Standing reminder. This one **expires `{date}`**. Re-apply before then.",
        "user": {"login": author},
        "labels": [{"name": "do-not-work"}],
        **extra,
    }


class FakeGitHub:
    """A fabricated organization. ``limits`` maps a repository to its GET answer, or to an
    exception to raise; a renewal sets the limit six months out unless ``refuse`` names it
    (GitHub refuses it) or ``stale`` does (GitHub accepts it and still reads back the old
    answer: a gap that no refusal explains)."""

    def __init__(
        self,
        repositories: list[str] | None = None,
        *,
        limits: dict[str, Any] | None = None,
        issues: dict[str, Any] | None = None,
        refuse: set[str] | None = None,
        stale: set[str] | None = None,
    ) -> None:
        self.repositories = list(FLEET if repositories is None else repositories)
        self.limits: dict[str, Any] = {
            r: {"limit": "collaborators_only", "expires_at": _iso(NOW + timedelta(days=170))}
            for r in self.repositories
        }
        self.limits.update(limits or {})
        self.issues: dict[str, Any] = {r: [] for r in self.repositories}
        self.issues.update(issues or {})
        self.refuse = refuse or set()
        self.stale = stale or set()
        self.writes: list[tuple[str, ...]] = []
        self.home_reads = 0

    def public_repositories(self, org: str) -> list[str]:
        return sorted(self.repositories)

    def renew(self, repository: str) -> None:
        self.writes.append(("renew", repository))
        if repository in self.refuse:
            raise lockdown.GitHubError("HTTP 403: Resource not accessible by personal access token")
        if isinstance(self.limits[repository], Exception) or repository in self.stale:
            return  # a read that fails keeps failing after the write
        self.limits[repository] = {
            "limit": "collaborators_only",
            "expires_at": _iso(NOW + timedelta(days=181)),
        }

    def read_limit(self, repository: str) -> dict[str, Any]:
        answer = self.limits[repository]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def open_issues(self, repository: str) -> list[dict[str, Any]]:
        if repository == HOME:
            self.home_reads += 1
        answer = self.issues[repository]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def edit_issue(self, repository: str, number: int, fields: dict[str, Any]) -> None:
        self.writes.append(("edit", repository, str(number), json.dumps(fields, sort_keys=True)))

    def comment(self, repository: str, number: int, body: str) -> None:
        self.writes.append(("comment", repository, str(number), body))

    def create_issue(self, repository: str, title: str, body: str, labels: list[str]) -> int:
        self.writes.append(("create", repository, title, body, ",".join(labels)))
        return 500

    def remove_label(self, repository: str, number: int, label: str) -> None:
        self.writes.append(("unlabel", repository, str(number), label))

    def add_label(self, repository: str, number: int, label: str) -> None:
        self.writes.append(("label", repository, str(number), label))

    def kinds(self) -> list[str]:
        return [write[0] for write in self.writes]


def lapsed(*repositories: str, **kwargs: Any) -> FakeGitHub:
    """An organization where each named repository reads back no limit after its renewal."""
    return FakeGitHub(limits={r: {} for r in repositories}, stale=set(repositories), **kwargs)


def audit(github: FakeGitHub, *, dry_run: bool = False) -> Any:
    return lockdown.run_audit(
        github,  # type: ignore[arg-type]
        github.repositories,
        dry_run=dry_run,
        now=NOW,
        run_url=RUN_URL,
        sleep=lambda _: None,
    )


def created(github: FakeGitHub) -> tuple[str, ...]:
    (write,) = [w for w in github.writes if w[0] == "create"]
    return write


# --- the all-clear -----------------------------------------------------------------------


def test_the_all_clear_renews_every_public_repository_and_pings() -> None:
    github = FakeGitHub()
    result = audit(github)
    assert result.verdict == "clean"
    assert result.heartbeat == "ping"
    # Every one of them, not only this repository.
    assert sorted(w[1] for w in github.writes if w[0] == "renew") == sorted(FLEET)
    assert "create" not in github.kinds()


def test_the_all_clear_needs_no_issue_and_writes_none() -> None:
    # The job is the reminder (#524): no repository needs an issue for the heartbeat to ping.
    github = FakeGitHub()
    assert audit(github).heartbeat == "ping"
    assert github.kinds() == ["renew"] * len(FLEET)


def test_the_heartbeat_is_the_lock_alone_whatever_issues_are_open() -> None:
    date = f"{NOW + timedelta(days=170):%Y-%m-%d}"
    two = [reminder_issue(10, date), reminder_issue(12, date)]
    github = FakeGitHub(issues={HOME: two, "basecradle/.github": lockdown.GitHubError("HTTP 502")})
    result = audit(github)
    assert result.verdict == "clean" and result.heartbeat == "ping"
    assert result.state == {}


def test_the_result_records_what_each_repository_read_back() -> None:
    result = audit(FakeGitHub()).result()
    assert result["verdict"] == "clean" and result["heartbeat"] == "ping"
    row = result["repositories"]["basecradle/.github"]
    assert row["renewed"] is True
    assert row["limit"] == "collaborators_only"
    assert row["findings"] == []
    assert set(row) == {"renewed", "limit", "expires_at", "findings"}


# --- the gaps: each one withholds the ping and files the issue ---------------------------


def _assert_gap(github: FakeGitHub, repository: str, code: str, dry_run: bool = False) -> Any:
    result = audit(github, dry_run=dry_run)
    assert result.verdict == "gap"
    assert result.heartbeat == "withhold"
    assert code in [f.code for f in result.found[repository]]
    return result


def test_a_repository_with_no_limit_is_a_gap() -> None:
    # Read back empty after the renewal: GitHub answers `{}` for a repository with no limit.
    github = FakeGitHub(refuse={"basecradle/.github"}, limits={"basecradle/.github": {}})
    _assert_gap(github, "basecradle/.github", "limit-missing")
    _, repository, title, body, labels = created(github)
    assert repository == HOME
    assert title == lockdown.GAP_TITLE
    assert labels == "needs-capital"
    assert "`basecradle/.github`" in body
    assert "no interaction limit" in body


def test_a_missing_limit_is_renewed_by_the_run_itself() -> None:
    # The repository the old guard only reported is locked down by the next run.
    github = FakeGitHub(limits={"basecradle/.github": {}})
    result = audit(github)
    assert result.verdict == "clean"
    assert ("renew", "basecradle/.github") in github.writes


def test_a_limit_about_to_expire_is_a_gap_even_though_it_is_active() -> None:
    soon = {"limit": "collaborators_only", "expires_at": _iso(NOW + timedelta(days=12))}
    github = FakeGitHub(limits={HOME: soon})
    result = _assert_gap(github, HOME, "limit-expiring", dry_run=True)
    assert "12 days away" in result.found[HOME][0].detail


def test_the_margin_is_exclusive_at_its_edge() -> None:
    edge = {"limit": "collaborators_only", "expires_at": _iso(NOW + lockdown.MARGIN)}
    _assert_gap(FakeGitHub(limits={HOME: edge}), HOME, "limit-expiring", dry_run=True)
    past = {
        "limit": "collaborators_only",
        "expires_at": _iso(NOW + lockdown.MARGIN + timedelta(hours=1)),
    }
    assert audit(FakeGitHub(limits={HOME: past}), dry_run=True).verdict == "clean"


def test_a_weaker_limit_is_a_gap() -> None:
    weaker = {"limit": "existing_users", "expires_at": _iso(NOW + timedelta(days=170))}
    _assert_gap(FakeGitHub(limits={HOME: weaker}), HOME, "limit-wrong", dry_run=True)


def test_an_unreadable_limit_is_never_a_pass() -> None:
    github = FakeGitHub(limits={HOME: lockdown.GitHubError("gh: HTTP 503: Service Unavailable")})
    result = _assert_gap(github, HOME, "limit-unreadable")
    assert "HTTP 503" in result.found[HOME][0].detail


@pytest.mark.parametrize(
    "answer",
    [
        {"limit": "collaborators_only"},
        {"limit": "collaborators_only", "expires_at": "not a date"},
        {"limit": "collaborators_only", "expires_at": "2027-05-14T00:00:00"},
    ],
)
def test_a_limit_whose_expiry_does_not_read_is_never_a_pass(answer: dict[str, Any]) -> None:
    _assert_gap(FakeGitHub(limits={HOME: answer}), HOME, "limit-unreadable", dry_run=True)


def test_a_refused_renewal_is_a_gap_even_while_the_old_limit_stands() -> None:
    github = FakeGitHub(refuse={"basecradle/.github"})
    result = _assert_gap(github, "basecradle/.github", "renewal-forbidden")
    assert "the token was refused the renewal (HTTP 403)" in created(github)[3]
    # The others were still renewed: one refusal never stops the rest.
    assert ("renew", HOME) in github.writes
    assert result.result()["repositories"]["basecradle/.github"]["renewed"] is False


def test_a_renewal_that_fails_without_a_refusal_says_what_github_said() -> None:
    class Outage(FakeGitHub):
        def renew(self, repository: str) -> None:
            if repository == HOME:
                raise lockdown.GitHubError("gh: HTTP 502: Bad Gateway")
            super().renew(repository)

    result = _assert_gap(Outage(), HOME, "renewal-refused")
    assert "HTTP 502: Bad Gateway" in result.found[HOME][0].detail


def test_no_repositories_is_never_clean() -> None:
    result = lockdown.Audit(observations=[], now=NOW, dry_run=False, run_url=RUN_URL)
    assert result.verdict == "gap" and result.heartbeat == "withhold"


def test_a_run_that_cannot_read_its_own_issues_dies_rather_than_skip_the_filing() -> None:
    github = FakeGitHub(issues={HOME: lockdown.GitHubError("HTTP 502")})
    with pytest.raises(lockdown.GitHubError):
        audit(github)
    # But only after every repository was renewed.
    assert sorted(w[1] for w in github.writes if w[0] == "renew") == sorted(FLEET)


def test_an_app_token_that_could_not_be_minted_never_costs_a_renewal() -> None:
    # The mint step may fail (a public repository the App is not installed on); the issues
    # token is then empty and every issue call refuses. Every limit is still renewed.
    class NoIssuesToken(FakeGitHub):
        def open_issues(self, repository: str) -> list[dict[str, Any]]:
            raise lockdown.GitHubError(f"no token for GET repos/{repository}/issues")

    github = NoIssuesToken()
    with pytest.raises(lockdown.GitHubError, match="no token"):
        audit(github)
    assert sorted(w[1] for w in github.writes if w[0] == "renew") == sorted(FLEET)


def test_every_renewal_happens_before_any_issue_is_read() -> None:
    order: list[str] = []

    class Ordered(FakeGitHub):
        def renew(self, repository: str) -> None:
            order.append("renew")
            super().renew(repository)

        def open_issues(self, repository: str) -> list[dict[str, Any]]:
            order.append("issues")
            return super().open_issues(repository)

    audit(Ordered())
    assert order == ["renew"] * len(FLEET) + ["issues"] * len(FLEET)


def test_the_home_repositorys_issues_are_read_once() -> None:
    github = FakeGitHub()
    audit(github)
    assert github.home_reads == 1


# --- the one-time cleanup (#524): the job closes the reminders it kept ------------------


def _reminders(date: str = "2027-05-17") -> dict[str, Any]:
    # As they stand: one per public repository, every one the capital's bot's.
    return {repo: [reminder_issue(10 + i, date, CAPITAL)] for i, repo in enumerate(FLEET)}


def test_the_first_run_closes_every_reminder_and_still_pings() -> None:
    github = FakeGitHub(issues=_reminders())
    result = audit(github)
    assert result.heartbeat == "ping"
    for i, repository in enumerate(FLEET):
        number = str(10 + i)
        on_it = [w for w in github.writes if w[1] == repository and w[0] != "renew"]
        assert [w[0] for w in on_it] == ["comment", "edit"], repository
        assert on_it[0][2] == on_it[1][2] == number
        assert json.loads(on_it[1][3]) == {"state": "closed", "state_reason": "completed"}
    assert "create" not in github.kinds()


def test_the_closing_comment_is_one_line_naming_the_job_its_heartbeat_and_the_run() -> None:
    github = FakeGitHub(issues=_reminders())
    audit(github)
    comment = next(w[3] for w in github.writes if w[0] == "comment")
    assert "\n" not in comment
    assert "Public Repository Lockdown job, which replaces this reminder" in comment
    assert "heartbeat" in comment and comment.endswith(RUN_URL)


def test_the_second_run_finds_no_reminder_and_writes_nothing() -> None:
    github = FakeGitHub()
    result = audit(github)
    assert result.heartbeat == "ping"
    assert github.kinds() == ["renew"] * len(FLEET)


def test_the_gap_issue_is_filed_before_any_reminder_is_closed() -> None:
    github = lapsed("basecradle/.github", issues=_reminders())
    audit(github)
    kinds = [w[0] for w in github.writes if w[0] != "renew"]
    assert kinds[0] == "create"


def test_a_reminder_that_cannot_be_closed_or_read_never_touches_the_verdict() -> None:
    class Refuses(FakeGitHub):
        def comment(self, repository: str, number: int, body: str) -> None:
            if repository == HOME:
                raise lockdown.GitHubError("HTTP 403")
            super().comment(repository, number, body)

    issues = {**_reminders(), "basecradle/.github": lockdown.GitHubError("HTTP 502")}
    github = Refuses(issues=issues)
    result = audit(github)
    assert result.verdict == "clean" and result.heartbeat == "ping"
    # The one it could read and close is closed all the same.
    rest = [w[:3] for w in github.writes if w[0] != "renew"]
    assert rest == [
        ("comment", "basecradle/basecradle-python", "11"),
        ("edit", "basecradle/basecradle-python", "11"),
    ]


def test_a_dry_run_closes_no_reminder(capsys: pytest.CaptureFixture[str]) -> None:
    github = FakeGitHub(issues=_reminders())
    audit(github, dry_run=True)
    assert github.writes == []
    assert "would close the reminder basecradle/.github#10" in capsys.readouterr().out


@pytest.mark.parametrize("author", [OWN_BOT, CAPITAL, "drawkkwast"])
def test_a_reminder_any_of_its_writers_filed_is_closed(author: str) -> None:
    assert lockdown.reminders_in([reminder_issue(10, "2027-05-14", author)]) == [10]


@pytest.mark.parametrize("author", ["john", "basecradle-ruby-ai[bot]"], ids=["stranger", "sibling"])
def test_an_issue_with_the_reminders_title_from_anyone_else_is_never_closed(author: str) -> None:
    issues = [reminder_issue(70, "2027-05-14", author=author), reminder_issue(10, "2027-05-14")]
    assert lockdown.reminders_in(issues) == [10]


def test_the_issue_449_title_is_not_a_reminder() -> None:
    # GitHub's search read this title as a second reminder on this repository.
    issue = reminder_issue(449, "2027-05-01")
    issue["title"] = (
        "The Monthly Interaction-Limit Audit Failed Four Times and Told Nobody: Give It a "
        "Heartbeat, and Renew Every Public Repository"
    )
    assert lockdown.reminders_in([issue, reminder_issue(61, "2027-05-01")]) == [61]


@pytest.mark.parametrize(
    "title",
    [
        "⏰ Renew interaction limit before 2027-05-14 (public-repo write-lockdown) — draft",
        "Renew interaction limit before 2027-05-14 (public-repo write-lockdown)",
        "⏰ Renew interaction limit before 2027-05-140 (public-repo write-lockdown)",
    ],
)
def test_only_the_exact_reminder_title_counts(title: str) -> None:
    issue = reminder_issue(10, "2027-05-14")
    issue["title"] = title
    assert lockdown.reminders_in([issue]) == []


def test_an_entry_with_no_usable_number_is_skipped_never_raised() -> None:
    broken = reminder_issue(10, "2027-05-14")
    del broken["number"]
    odd = {**reminder_issue(11, "2027-05-14"), "number": "11"}
    assert lockdown.reminders_in([broken, odd, reminder_issue(12, "2027-05-14")]) == [12]


def test_a_pull_request_is_never_a_reminder() -> None:
    assert (
        lockdown.reminders_in([reminder_issue(11, "2027-05-14", pull_request={"url": "x"})]) == []
    )


# --- the dry run -------------------------------------------------------------------------


def test_a_dry_run_writes_nothing_and_never_pings() -> None:
    date = f"{NOW + timedelta(days=170):%Y-%m-%d}"
    github = lapsed("basecradle/.github", issues={HOME: [reminder_issue(61, date)]})
    result = audit(github, dry_run=True)
    assert github.writes == []
    assert result.heartbeat == "withhold"
    assert result.verdict == "gap"


def test_a_clean_dry_run_still_never_pings() -> None:
    result = audit(FakeGitHub(), dry_run=True)
    assert result.verdict == "clean" and result.heartbeat == "withhold"


# --- the gap issue: one, updated in place, closed by the run that finds none -------------


def _gap_issue(
    github: FakeGitHub,
    number: int = 500,
    labels: tuple[str, ...] = ("needs-capital",),
    author: str = OWN_BOT,
) -> dict[str, Any]:
    body = created(github)[3]
    return {
        "number": number,
        "title": lockdown.GAP_TITLE,
        "body": body,
        "user": {"login": author},
        "labels": [{"name": label} for label in labels],
    }


def _second_run(lapsing: list[str], open_gap: dict[str, Any]) -> FakeGitHub:
    return lapsed(*lapsing, issues={HOME: [open_gap]})


def test_a_standing_gap_is_said_once() -> None:
    first = lapsed("basecradle/.github")
    audit(first)
    again = _second_run(["basecradle/.github"], _gap_issue(first))
    audit(again)
    assert not [w for w in again.writes if w[0] in {"create", "comment", "label", "unlabel"}]


def test_a_changed_gap_comments_then_relabels_then_rewrites_the_body() -> None:
    first = lapsed("basecradle/.github")
    audit(first)
    again = _second_run(["basecradle/.github", "basecradle/basecradle-python"], _gap_issue(first))
    audit(again)
    home = [w for w in again.writes if w[1] == HOME and w[0] != "renew" and w[2] == "500"]
    assert [w[0] for w in home] == ["comment", "unlabel", "label", "edit"]
    assert "`basecradle/basecradle-python`" in home[0][3]
    assert "needs-capital" in home[0][3]
    assert "basecradle-python" in json.loads(home[3][3])["body"]


def test_a_repository_that_became_clean_is_said_without_paging_again() -> None:
    first = lapsed("basecradle/.github", "basecradle/basecradle-python")
    audit(first)
    again = _second_run(["basecradle/.github"], _gap_issue(first))
    audit(again)
    home = [w for w in again.writes if w[0] != "renew" and w[1] == HOME and w[2] == "500"]
    assert [w[0] for w in home] == ["comment", "edit"]
    assert "**Now Clean:** `basecradle/basecradle-python`" in home[0][3]
    assert "needs-capital" not in home[0][3]


def test_a_changed_gap_whose_label_was_cleared_only_adds_it_back() -> None:
    first = lapsed("basecradle/.github")
    audit(first)
    again = _second_run(
        ["basecradle/.github", "basecradle/basecradle-python"],
        _gap_issue(first, labels=()),
    )
    audit(again)
    assert "unlabel" not in again.kinds() and "label" in again.kinds()


def test_the_run_that_finds_no_gap_closes_the_issue() -> None:
    first = lapsed("basecradle/.github")
    audit(first)
    again = _second_run([], _gap_issue(first))
    result = audit(again)
    assert result.heartbeat == "ping"
    home = [w for w in again.writes if w[0] in {"comment", "edit"} and w[2] == "500"]
    assert [w[0] for w in home] == ["comment", "edit"]
    assert RUN_URL in home[0][3]
    assert "`needs-capital` removed" in home[0][3]
    assert json.loads(home[1][3]) == {"state": "closed", "state_reason": "completed"}
    # The routing label goes with the close, after the comment that says so.
    kinds = [w[0] for w in again.writes if w[0] != "renew" and len(w) > 2 and w[2] == "500"]
    assert kinds == ["comment", "unlabel", "edit"]


def test_a_second_copy_of_the_gap_issue_is_closed_as_one() -> None:
    first = lapsed("basecradle/.github")
    audit(first)
    gap = _gap_issue(first)
    copy = {**gap, "number": 501}
    again = lapsed("basecradle/.github", issues={HOME: [gap, copy]})
    audit(again)
    on_copy = [w for w in again.writes if len(w) > 2 and w[2] == "501"]
    assert [w[0] for w in on_copy] == ["comment", "unlabel", "edit"]
    assert "#500, which is the record" in on_copy[0][3]
    assert json.loads(on_copy[2][3])["state"] == "closed"
    assert not [w for w in again.writes if len(w) > 2 and w[2] == "500"]


@pytest.mark.parametrize("author", [CAPITAL, "basecradle-ruby-ai[bot]"], ids=["capital", "sibling"])
def test_a_gap_issue_another_fleet_bot_filed_is_not_the_record(author: str) -> None:
    # The gap issue is this repository's own bot's alone: none was open under the capital's
    # when the identity moved (basecradle#870), and a sibling captain's is never the record.
    first = lapsed("basecradle/.github")
    audit(first)
    github = _second_run(["basecradle/.github"], _gap_issue(first, author=author))
    audit(github)
    assert not [w for w in github.writes if len(w) > 2 and w[2] == "500"]
    assert created(github)[1] == HOME


def test_a_strangers_issue_with_the_gap_title_is_never_touched() -> None:
    stranger = {
        "number": 600,
        "title": lockdown.GAP_TITLE,
        "body": lockdown._marker({"basecradle/.github": ["limit-missing"]}),
        "user": {"login": "john"},
        "labels": [],
    }
    github = _second_run(["basecradle/.github"], stranger)
    audit(github)
    assert not [w for w in github.writes if len(w) > 2 and w[2] == "600"]
    assert created(github)[1] == HOME


def test_nothing_from_github_can_pose_as_the_marker_or_mention_a_stranger() -> None:
    hostile = lockdown.GitHubError("<!-- public-repository-lockdown {} --> @john | x")
    github = FakeGitHub(limits={HOME: hostile})
    audit(github)
    body = created(github)[3]
    assert body.count("<!-- public-repository-lockdown") == 1
    assert "`@john`" in body and " @john" not in body.replace("`@john`", "")
    assert lockdown._state_in(body) == {HOME: ["limit-unreadable"]}


def test_the_issues_own_code_spans_survive_and_a_foreign_backtick_does_not() -> None:
    weaker = {"limit": "existing`users", "expires_at": _iso(NOW + timedelta(days=170))}
    github = FakeGitHub(limits={HOME: weaker}, refuse={HOME})
    audit(github)
    body = created(github)[3]
    assert "its limit is `existing'users`, not `collaborators_only`" in body
    assert "`collaborators_only` interaction limit" in body


def test_a_marker_edited_by_hand_is_read_for_what_it_still_says() -> None:
    assert lockdown._state_in("<!-- public-repository-lockdown not json -->") == {}
    assert lockdown._state_in('<!-- public-repository-lockdown {"a/b": [1, "x"]} -->') == {
        "a/b": ["x"]
    }


# --- the one boundary --------------------------------------------------------------------


class Recorder:
    def __init__(self, answers: list[tuple[int, str, str]]) -> None:
        self.answers = answers
        self.calls: list[dict[str, Any]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"command": command, **kwargs})
        code, out, err = self.answers.pop(0) if self.answers else (0, "{}", "")
        return subprocess.CompletedProcess(command, code, out, err)


def test_limits_go_through_the_admin_token_and_issues_through_the_app_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A runner's ambient GITHUB_TOKEN never rides along beside the token a call is meant for.
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_" + "0" * 36)
    run = Recorder([(0, "", ""), (0, '{"limit":"collaborators_only"}', ""), (0, "[[]]", "")])
    github = lockdown.GitHub("github_pat_ADMIN", "ghs_ISSUES", run=run)
    github.renew(HOME)
    github.read_limit(HOME)
    github.open_issues(HOME)
    put, get, issues = run.calls
    assert put["command"][:4] == ["gh", "api", "--method", "PUT"]
    assert put["command"][-1] == f"repos/{HOME}/interaction-limits"
    assert json.loads(put["input"]) == {"limit": "collaborators_only", "expiry": "six_months"}
    assert put["env"]["GH_TOKEN"] == "github_pat_ADMIN"
    assert get["env"]["GH_TOKEN"] == "github_pat_ADMIN"
    assert issues["env"]["GH_TOKEN"] == "ghs_ISSUES"
    assert "--paginate" in issues["command"] and "--slurp" in issues["command"]
    # No token on argv, ever: argv is readable by every account on the machine.
    for call in run.calls:
        assert not [arg for arg in call["command"] if "ADMIN" in arg or "ISSUES" in arg]
        assert "GITHUB_TOKEN" not in call["env"]


def test_every_issue_write_goes_through_the_app_token() -> None:
    run = Recorder([(0, '{"number": 7}', "")])
    github = lockdown.GitHub("github_pat_ADMIN", "ghs_ISSUES", run=run)
    github.create_issue(HOME, "t", "b", ["needs-capital"])
    github.comment(HOME, 7, "c")
    github.edit_issue(HOME, 7, {"body": "b"})
    github.add_label(HOME, 7, "needs-capital")
    github.remove_label(HOME, 7, "needs-capital")
    assert {call["env"]["GH_TOKEN"] for call in run.calls} == {"ghs_ISSUES"}


def test_a_failed_call_raises_with_ghs_last_line_and_an_empty_token_never_calls() -> None:
    run = Recorder([(1, "", "gh: Not Found (HTTP 404)\n")])
    with pytest.raises(lockdown.GitHubError, match="HTTP 404"):
        lockdown.GitHub("github_pat_ADMIN", "", run=run).read_limit(HOME)
    with pytest.raises(lockdown.GitHubError, match="no token"):
        lockdown.GitHub("github_pat_ADMIN", "", run=run).open_issues(HOME)
    assert len(run.calls) == 1


def test_a_call_that_hangs_is_a_github_error_never_a_crash() -> None:
    # A timeout raised past the boundary would end the run before its result, paging for a
    # lockdown that is fine; as a GitHubError it is the gap or the annotation it should be.
    def hangs(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(lockdown.GitHubError, match="did not answer within 60 seconds") as raised:
        lockdown.GitHub("github_pat_ADMIN", "ghs_ISSUES", run=hangs).comment(HOME, 7, "c")
    assert raised.value.status is None
    assert "ADMIN" not in str(raised.value) and "ISSUES" not in str(raised.value)


def test_an_answer_that_is_not_json_is_never_a_limit() -> None:
    run = Recorder([(0, "<html>", "")])
    with pytest.raises(lockdown.GitHubError):
        lockdown.GitHub("a", "b", run=run).read_limit(HOME)


def test_the_listing_keeps_public_unarchived_repositories_only() -> None:
    page = [
        {"full_name": "basecradle/basecradle-router", "visibility": "public", "archived": False},
        {"full_name": "basecradle/old", "visibility": "public", "archived": True},
        {"full_name": "basecradle/basecradle", "visibility": "private", "archived": False},
    ]
    second = [{"full_name": "basecradle/.github", "visibility": "public", "archived": False}]
    run = Recorder([(0, json.dumps([page, second]), "")])
    names = lockdown.GitHub("a", run=run).public_repositories("basecradle")
    assert names == ["basecradle/.github", "basecradle/basecradle-router"]


def _rate_limit(expiry: str | None) -> tuple[int, str, str]:
    header = f"Github-Authentication-Token-Expiration: {expiry}\r\n" if expiry else ""
    return (0, f"HTTP/2.0 200 OK\r\n{header}\r\n{{}}", "")


def test_the_credential_line_names_only_the_kind_far_from_expiry() -> None:
    run = Recorder([_rate_limit("2027-06-07 05:20:00 UTC")])
    kind, warning = lockdown.GitHub("github_pat_SECRET", run=run).credential(NOW)
    assert kind == "fine-grained"
    # The log is public: no date to plan around, and no owner.
    assert warning is None
    assert [c["command"][-1] for c in run.calls] == ["rate_limit"]


def test_the_credential_warns_in_days_near_its_expiry_and_never_says_the_date() -> None:
    soon = NOW + timedelta(days=20, hours=1)
    run = Recorder([_rate_limit(soon.strftime("%Y-%m-%d %H:%M:%S UTC"))])
    kind, warning = lockdown.GitHub("github_pat_SECRET", run=run).credential(NOW)
    assert warning is not None and "expires in 20 days" in warning
    assert f"{soon:%Y-%m-%d}" not in warning and "SECRET" not in warning + kind


def test_the_credential_line_never_fails_the_run() -> None:
    run = Recorder([(1, "", "HTTP 401")])
    assert lockdown.GitHub("ghs_x", run=run).credential(NOW) == ("app", None)
    run = Recorder([_rate_limit(None)])
    assert lockdown.GitHub("weird", run=run).credential(NOW) == ("unknown", None)


# --- main ---------------------------------------------------------------------------------


def test_main_lists_into_a_file_and_an_empty_listing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "github_pat_x")
    out = tmp_path / "repos.txt"
    run = Recorder([(0, "[[]]", "")])
    assert lockdown.main(["repositories", "--out", str(out)], run=run) == 1
    assert not out.exists()


def test_main_refuses_without_the_admin_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    run = Recorder([])
    assert lockdown.main(["repositories", "--out", str(tmp_path / "r")], run=run) == 2
    assert run.calls == []


def test_main_writes_the_result_only_at_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "github_pat_x")
    monkeypatch.setenv("ISSUES_TOKEN", "ghs_y")
    repos = tmp_path / "repos.txt"
    repos.write_text(f"{HOME}\n")
    result = tmp_path / "result.json"

    # A run whose every call fails: the home repository's issues cannot be read, so it dies
    # before the end and leaves no result for the workflow to ping on.
    failing = Recorder([(1, "", "HTTP 503")] * 10)
    with pytest.raises(lockdown.GitHubError):
        lockdown.main(["audit", "--repositories", str(repos), "--result", str(result)], run=failing)
    assert not result.exists()


def test_main_dry_run_writes_a_withheld_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "github_pat_x")
    monkeypatch.setenv("ISSUES_TOKEN", "ghs_y")
    repos = tmp_path / "repos.txt"
    repos.write_text(f"{HOME}\n")
    result = tmp_path / "result.json"
    expires = _iso(datetime.now(timezone.utc) + timedelta(days=170))
    date = expires[:10]
    issues = json.dumps([[reminder_issue(61, date)]])
    run = Recorder(
        [
            (0, "HTTP/2.0 200 OK\n\n{}", ""),  # credential: rate_limit
            (0, json.dumps({"limit": "collaborators_only", "expires_at": expires}), ""),
            (0, issues, ""),  # reminders, and the gap issue's read
        ]
    )
    code = lockdown.main(
        ["audit", "--repositories", str(repos), "--result", str(result), "--dry-run"], run=run
    )
    assert code == 0
    written = json.loads(result.read_text())
    assert written["verdict"] == "clean"
    assert written["heartbeat"] == "withhold"
    assert written["dry_run"] is True
    assert not [c for c in run.calls if c["command"][3] != "GET"]


def test_a_repository_list_line_that_is_not_owner_and_name_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "repos.txt"
    path.write_text("basecradle/ok\n--method PUT\n")
    with pytest.raises(SystemExit):
        lockdown.read_repositories(path)


# --- the workflow's half of the contract ---------------------------------------------------


def _workflow() -> str:
    return WORKFLOW.read_text()


def test_the_workflow_runs_weekly_and_by_hand_never_on_a_pull_request() -> None:
    text = _workflow()
    assert 'cron: "17 9 * * 1"' in text
    assert "workflow_dispatch:" in text
    assert "pull_request" not in text.split("jobs:")[0]


def test_the_workflow_never_skips_quietly_on_a_missing_credential() -> None:
    text = _workflow()
    assert "exit 0" not in text.split("Ping the Public Repository Lockdown Heartbeat")[0]
    assert 'test -n "$ADMIN_TOKEN" ||' in text
    assert 'test -n "$HEARTBEAT_URL" ||' in text


def test_the_workflow_pings_only_on_the_scripts_word_and_fails_on_any_gap() -> None:
    text = _workflow()
    assert "secrets.PUBLIC_REPO_LOCKDOWN_HEARTBEAT_URL" in text
    assert "jq -er '.heartbeat'" in text
    assert 'if [ "$heartbeat" = "ping" ]; then' in text
    assert 'test "$(jq -er \'.verdict\' "$RUNNER_TEMP/result.json")" = "clean"' in text
    # The ping comes after the audit, and the verdict gate after the ping.
    assert text.index("Renew, Read Back, and Record") < text.index("Ping the Public")
    assert text.index("Ping the Public") < text.index("Fail Unless Every Public")


def test_the_admin_token_never_reaches_an_issue_write() -> None:
    text = _workflow()
    assert "permission-issues: write" in text
    assert "repositories: ${{ steps.repositories.outputs.names }}" in text
    assert "ISSUES_TOKEN: ${{ steps.app-token.outputs.token }}" in text
    assert "GH_TOKEN: ${{ secrets.FLEET_ADMIN_TOKEN }}" not in text
    assert "secrets.GITHUB_TOKEN" not in text and "github.token" not in text


def test_the_issues_token_is_this_repositorys_own_bot_with_no_fallback() -> None:
    # basecradle#870: FLEET_BOT_* hold this repository's captain's App; the capital's key
    # is needed nowhere here, so nothing may fall back to it once its copies are deleted.
    text = _workflow()
    mint = text.split("- name: Mint This Repository's Bot Token", 1)[1].split("- name:", 1)[0]
    assert "client-id: ${{ secrets.FLEET_BOT_CLIENT_ID }}\n" in mint
    assert "private-key: ${{ secrets.FLEET_BOT_APP_PRIVATE_KEY }}\n" in mint
    assert "BASECRADLE_AI" not in text
    assert re.search(r"uses: actions/create-github-app-token@[0-9a-f]{40} # v\d", mint), (
        "the step holding the App's private key runs a commit, never a tag that can move"
    )
    assert lockdown.WRITER_BOT == lockdown.GAP_AUTHOR == OWN_BOT


def test_a_failed_app_token_mint_never_stops_the_renewal_step() -> None:
    text = _workflow()
    mint = text.split("- name: Mint This Repository's Bot Token", 1)[1].split("- name:", 1)[0]
    assert "continue-on-error: true" in mint
    renew = text.split("- name: Renew, Read Back, and Record", 1)[1].split("- name:", 1)[0]
    assert "if:" not in renew


# --- the gap issue names the credential it needs (the first real run, #452) -------------

_REFUSED = lockdown.GitHubError("gh: Resource not accessible by personal access token (HTTP 403)")
_REJECTED = lockdown.GitHubError("gh: Bad credentials (HTTP 401)")


class RefusingGitHub(FakeGitHub):
    """A token GitHub refuses on some repositories, with the status a real refusal carries."""

    def __init__(self, refusals: dict[str, lockdown.GitHubError], **kwargs: Any) -> None:
        super().__init__(limits=dict(refusals), **kwargs)
        self.refusals = refusals

    def renew(self, repository: str) -> None:
        self.writes.append(("renew", repository))
        if repository in self.refusals:
            raise self.refusals[repository]


def _refused_run(
    credential: str = "fine-grained",
    refusals: dict[str, lockdown.GitHubError] | None = None,
    **kwargs: Any,
) -> RefusingGitHub:
    github = RefusingGitHub(refusals or {"basecradle/.github": _REFUSED}, **kwargs)
    lockdown.run_audit(
        github,  # type: ignore[arg-type]
        github.repositories,
        dry_run=False,
        now=NOW,
        run_url=RUN_URL,
        sleep=lambda _: None,
        credential=credential,
    )
    return github


def _section(body: str) -> str:
    return body.split("## The Credential It Needs", 1)[1].split("## What Happens Next", 1)[0]


def test_a_refusal_names_the_permission_and_every_repository_it_must_reach() -> None:
    body = created(_refused_run())[3]
    section = _section(body)
    assert (
        "`FLEET_ADMIN_TOKEN` was refused (HTTP 403) on `basecradle/.github`: it does not reach "
        "that repository, or lacks the permission there."
    ) in section
    assert "**Administration: Read and write**" in section
    for repository in FLEET:
        assert f"`{repository}`" in section
    assert "*Only select repositories*" in section
    assert "never organization administration, and no issue permission" in section
    assert "`@origin`'s to change" in section


def test_a_refusal_is_its_own_finding_never_an_unreadable_limit() -> None:
    github = _refused_run()
    body = created(github)[3]
    assert "the token was refused the read (HTTP 403)" in body
    assert "lacks Administration: read on it" in body
    assert lockdown._state_in(created(github)[3]) == {
        "basecradle/.github": ["limit-forbidden", "renewal-forbidden"]
    }


@pytest.mark.parametrize(
    ("kind", "says", "never"),
    [
        ("fine-grained", "*Only select repositories*", "classic"),
        ("classic", "GitHub documents no scope", "*Only select repositories*"),
        ("app", "must be installed on each repository above", "*Only select repositories*"),
        ("unknown", "**Administration: Read and write**", "This run read it as"),
    ],
)
def test_the_guidance_is_in_the_words_of_the_kind_of_token(
    kind: str, says: str, never: str
) -> None:
    section = _section(created(_refused_run(credential=kind))[3])
    assert says in section
    assert never not in section


def test_an_expired_or_revoked_token_is_said_as_such_not_as_a_missing_permission() -> None:
    github = _refused_run(refusals={r: _REJECTED for r in FLEET})
    body = created(github)[3]
    assert "rejected as invalid (HTTP 401)" in _section(body)
    assert "it has expired or been revoked" in body
    assert "refused (HTTP 403)" not in body
    assert set(lockdown._state_in(body)["basecradle/.github"]) == {"token-rejected"}


def test_a_failure_that_is_not_a_refusal_names_no_credential() -> None:
    github = FakeGitHub(limits={HOME: lockdown.GitHubError("gh: HTTP 502: Bad Gateway")})
    audit(github)
    body = created(github)[3]
    assert "## The Credential It Needs" not in body
    assert "limit-unreadable" in body


def test_a_gap_with_no_refusal_names_no_credential() -> None:
    github = lapsed("basecradle/.github")
    audit(github)
    assert "## The Credential It Needs" not in created(github)[3]


def test_a_token_that_can_read_but_not_renew_is_named_too() -> None:
    # Administration: Read-only reads the limit and is refused the renewal.
    github = FakeGitHub(refuse={"basecradle/.github"})
    audit(github)
    body = created(github)[3]
    assert "`FLEET_ADMIN_TOKEN` was refused (HTTP 403) on `basecradle/.github`" in body
    assert "lacks Administration: write on it" in body


def _open_gap(body: str) -> list[dict[str, Any]]:
    gap = {
        "number": 452,
        "title": lockdown.GAP_TITLE,
        "body": body,
        "user": {"login": OWN_BOT},
        "labels": [{"name": "needs-capital"}],
    }
    return [gap]


def test_the_open_452_is_told_the_credential_in_a_comment() -> None:
    # #452 as the first run filed it, under the codes that run used.
    old = "## What Was Found\n\nold body\n\n" + lockdown._marker(
        {"basecradle/.github": ["limit-unreadable", "renewal-refused"]}
    )
    again = _refused_run(issues={HOME: _open_gap(old)})
    on_issue = [w for w in again.writes if len(w) > 2 and w[2] == "452"]
    assert [w[0] for w in on_issue] == ["comment", "unlabel", "label", "edit"]
    comment = on_issue[0][3]
    assert "## The Credential It Needs" in comment
    assert "**Administration: Read and write**" in comment
    assert "## The Credential It Needs" in json.loads(on_issue[3][3])["body"]
    assert "create" not in again.kinds()


def test_a_standing_refusal_seen_by_a_later_run_is_said_once() -> None:
    body = created(_refused_run())[3]
    again = _refused_run(issues={HOME: _open_gap(body)})
    assert not [w for w in again.writes if len(w) > 2 and w[2] == "452"]


def test_the_status_survives_a_hint_line_after_it() -> None:
    stderr = (
        "gh: Resource not accessible by personal access token (HTTP 403)\n"
        "This API operation needs the admin:org scope.\n"
    )
    run = Recorder([(1, "", stderr)])
    with pytest.raises(lockdown.GitHubError) as raised:
        lockdown.GitHub("github_pat_x", "ghs_y", run=run).read_limit(HOME)
    assert raised.value.status == 403
    assert "(HTTP 403)" in str(raised.value)


def test_an_error_with_no_status_is_an_outage() -> None:
    assert lockdown.GitHubError("gh: connection reset").status is None
    assert lockdown.GitHubError("gh: Bad credentials (HTTP 401)").status == 401


def test_two_refusals_are_said_in_the_plural() -> None:
    both = {"basecradle/.github": _REFUSED, "basecradle/basecradle-python": _REFUSED}
    section = _section(created(_refused_run(refusals=both))[3])
    assert (
        "on `basecradle/.github`, `basecradle/basecradle-python`: it does not reach those "
        in section
    )

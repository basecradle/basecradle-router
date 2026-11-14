"""Public Repository Lockdown: keep every public repository's write surface members-only (#449).

Run from ``.github/workflows/interaction-limit-renewal.yml``, in two steps::

    python3 public_repository_lockdown.py repositories --org basecradle --out repos.txt
    python3 public_repository_lockdown.py audit --repositories repos.txt --result result.json

GitHub interaction limits last six months at most, and a public repository whose limit
lapses reopens to drive-by issues and comments: the surface the #60 injection came through.
This renews ``collaborators_only`` on **every** public repository in the organization, reads
each one back, keeps each repository's one renewal reminder dated to what GitHub reports,
and answers one question for the ``Public Repository Lockdown`` heartbeat: does every public
repository have an active members-only limit, with margin, and exactly one open reminder?

Its predecessor renewed one repository of five and reported only by a red cross nobody
opened: it found ``basecradle/.github`` unlimited four months running and told nobody
(#449). So a gap here does three things: it withholds the heartbeat ping (and the miss
pages), it files or updates one ``needs-capital`` issue in this repository, and it turns
the run red. A run that cannot read an answer treats it as a gap, never as a pass, and a run
that dies before the end writes no result, so the workflow pings nothing.

Two credentials, each for its own half, so neither is used for what the other is for:

- **The admin token** (``FLEET_ADMIN_TOKEN``) renews and reads the limits. Repository
  ``Administration`` is not a permission ``GITHUB_TOKEN`` can be granted, so it is a
  standing secret. It never writes an issue: whatever it posts would carry its owner's name.
- **The issues token** (the ``basecradle-ai`` App, minted per run, ``issues: write``, scoped
  to the public repositories) reads and edits the reminders and files the gap issue.

Every GitHub call goes through ``gh api`` in :class:`GitHub`, the one boundary, so the tests
replace it and nothing here has to reach GitHub to be exercised. Standard library only, so
the workflow runs it with the runner's own ``python3``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ORG = "basecradle"
HOME_REPOSITORY = "basecradle/basecradle-router"

LIMIT = "collaborators_only"
EXPIRY = "six_months"

# A limit with fewer days than this left is a gap even though it is still active. A run that
# renews leaves about 181; the margin is what a lapse would leave the capital to fix it in,
# with a weekly run and the heartbeat's grace already spent noticing.
MARGIN = timedelta(days=30)

# A reminder is an open issue with this title written by one of these. The repositories are
# public, so anyone can open an issue with any title; one nobody on the allow-list wrote is
# never the record. The whole title is matched exactly, not searched: GitHub's search read
# #449's own title ("... Interaction-Limit ... Renew ...") as a second reminder here.
REMINDER_AUTHORS = frozenset({"basecradle-ai[bot]", "drawkkwast"})
REMINDER_FORMAT = "⏰ Renew interaction limit before {date} (public-repo write-lockdown)"
REMINDER_TITLE = re.compile(
    "^" + re.escape(REMINDER_FORMAT).replace(re.escape("{date}"), r"(\d{4}-\d{2}-\d{2})") + "$"
)
REMINDER_BODY_DATE = re.compile(r"expires `(\d{4}-\d{2}-\d{2})`")

GAP_TITLE = "Public Repository Lockdown Found a Gap"
GAP_AUTHOR = "basecradle-ai[bot]"
GAP_LABEL = "needs-capital"
MARKER = re.compile(r"<!-- public-repository-lockdown (\{.*\}) -->")

LOCKDOWN = (
    f"the lockdown: an active `{LIMIT}` interaction limit with more than {MARGIN.days} days "
    "left, and exactly one open renewal reminder"
)
# What the admin token needs, per GitHub's documented permissions for these endpoints:
# repository Administration, write to renew and read to read back, and owner or admin access.
CREDENTIAL_NEED = (
    "To set an interaction limit, GitHub requires **Administration: Read and write** on the "
    "repository for a fine-grained token or a GitHub App, and owner or admin access for the "
    "token's owner."
)
CREDENTIAL_HOW = {
    "fine-grained": (
        "This run read it as a fine-grained personal access token. Its repository access is a "
        "fixed list that never grows by itself, so a repository made public after it was "
        "minted is outside it until someone adds it. In the token's settings: **Resource "
        "owner** `basecradle`; **Repository access** either *All repositories* or *Only select "
        "repositories* listing each repository above; **Repository permissions → "
        "Administration** *Read and write* (GitHub adds *Metadata: Read-only* with it)."
    ),
    "classic": (
        "This run read it as a classic personal access token. GitHub documents no scope for "
        "these endpoints, only that the token's owner must have owner or admin access on each "
        "repository above. A fine-grained token with *Administration: Read and write* on those "
        "repositories is the least-privilege replacement."
    ),
    "app": (
        "This run read it as a GitHub App installation token. The App needs the repository "
        "permission *Administration: Read and write* and must be installed on each repository "
        "above."
    ),
}
CREDENTIAL_LIMITS = (
    "Nothing broader is needed: never organization administration, and no issue permission, "
    "because the `basecradle-ai` App writes the reminders and this issue. A newly minted token "
    "is stored as the `FLEET_ADMIN_TOKEN` secret on `basecradle/basecradle-router`. The token "
    "is `@origin`'s to change, and the first clean run after that closes this issue."
)
GAP_NEXT = (
    "Every run renews every public repository's limit, so a limit that was only missing is "
    "usually back by the next run. A reminder that is missing or doubled is the capital's to "
    "fix by hand: one open reminder per public repository, titled exactly `⏰ Renew interaction "
    "limit before <YYYY-MM-DD> (public-repo write-lockdown)` and written by `@basecradle-ai` "
    "or `@origin`. "
    "This issue is updated in place while a gap stands, and the run that finds none closes it."
)


# How the admin token's kind is told from its prefix: an identifier the issue's guidance keys
# on, and the words the log line uses.
TOKEN_KINDS = (
    ("github_pat_", "fine-grained", "a fine-grained personal access token"),
    ("ghp_", "classic", "a classic personal access token"),
    ("ghs_", "app", "a GitHub App installation token"),
)
KIND_WORDS = {kind: words for _, kind, words in TOKEN_KINDS} | {
    "unknown": "a token of unknown kind"
}

# The log is public (this repository is), so the admin token's expiry is said only as a
# warning this close to it, never as a date an outsider could plan around.
EXPIRY_WARNING = timedelta(days=60)


class GitHubError(Exception):
    """A ``gh api`` call that did not answer. Never reads as a pass. ``status`` is GitHub's
    HTTP status when its answer carried one, so a refusal is told from an outage by a field,
    never by matching words."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        if status is None:
            found = HTTP_STATUS.search(message)
            status = int(found.group(1)) if found else None
        self.status = status


HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")


# --- what one repository looks like ------------------------------------------------------


@dataclass(frozen=True)
class Reminder:
    number: int
    title: str
    body: str
    date: str


@dataclass
class Observation:
    """What one run saw of one repository. ``None`` in an answer means it was not asked
    (``renewal`` in a dry run) or could not be read (the matching ``*_error`` says why)."""

    repository: str
    renewal: str | None = None  # "renewed", or None when not attempted
    renewal_error: str | None = None
    renewal_status: int | None = None
    limit: dict[str, Any] | None = None
    limit_error: str | None = None
    limit_status: int | None = None
    reminders: list[Reminder] | None = None
    reminders_error: str | None = None
    reminder_moved: tuple[int, str, str] | None = None  # (number, from, to)
    reminder_error: str | None = None


@dataclass(frozen=True)
class Finding:
    code: str
    detail: str


def reminders_in(issues: list[dict[str, Any]]) -> list[Reminder]:
    """The renewal reminders among a repository's open issues, from the REST issues list."""
    reminders = []
    for issue in issues:
        if "pull_request" in issue:
            continue
        user = issue.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        title = issue.get("title")
        if login not in REMINDER_AUTHORS or not isinstance(title, str):
            continue
        match = REMINDER_TITLE.match(title)
        if match:
            body = issue.get("body")
            reminders.append(
                Reminder(
                    number=int(issue["number"]),
                    title=title,
                    body=body if isinstance(body, str) else "",
                    date=match.group(1),
                )
            )
    return sorted(reminders, key=lambda reminder: reminder.number)


def expiry_of(limit: dict[str, Any] | None) -> datetime | None:
    """The limit's ``expires_at``, when it is ``collaborators_only`` and the date reads."""
    if not isinstance(limit, dict) or limit.get("limit") != LIMIT:
        return None
    raw = limit.get("expires_at")
    if not isinstance(raw, str):
        return None
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return expires if expires.tzinfo else None


def findings_for(observation: Observation, now: datetime) -> list[Finding]:
    """Every way this repository falls short. An empty list is the only pass."""
    found = []
    if observation.renewal_error is not None:
        found.append(
            _admin_refusal("renewal", observation.renewal_status, observation.renewal_error)
        )

    limit = observation.limit
    if observation.limit_error is not None and observation.limit_status in REFUSALS:
        found.append(_admin_refusal("read", observation.limit_status, observation.limit_error))
    elif observation.limit_error is not None or limit is None:
        reason = _foreign(observation.limit_error or "no answer")
        found.append(Finding("limit-unreadable", f"its limit could not be read: {reason}"))
    elif not limit.get("limit"):
        found.append(
            Finding("limit-missing", "it has no interaction limit: anyone can write to it")
        )
    elif limit.get("limit") != LIMIT:
        found.append(
            Finding("limit-wrong", f"its limit is `{_foreign(limit.get('limit'))}`, not `{LIMIT}`")
        )
    else:
        expires = expiry_of(limit)
        if expires is None:
            found.append(Finding("limit-unreadable", "its limit's expiry could not be read"))
        elif expires - now <= MARGIN:
            days = max((expires - now).days, 0)
            found.append(
                Finding(
                    "limit-expiring",
                    f"its limit expires {expires:%Y-%m-%d}, {days} days away "
                    f"(the margin is {MARGIN.days})",
                )
            )

    reminders = observation.reminders
    if observation.reminders_error is not None or reminders is None:
        reason = _foreign(observation.reminders_error or "no answer")
        found.append(
            Finding("reminders-unreadable", f"its open issues could not be read: {reason}")
        )
    elif not reminders:
        found.append(Finding("reminder-missing", "it has no open renewal reminder"))
    elif len(reminders) > 1:
        numbers = ", ".join(f"#{reminder.number}" for reminder in reminders)
        found.append(
            Finding(
                "reminders-duplicate",
                f"it has {len(reminders)} open renewal reminders ({numbers}); exactly one is "
                "the record",
            )
        )
    if observation.reminder_error is not None:
        found.append(
            Finding(
                "reminder-not-moved",
                "its reminder could not be moved to the limit's date: "
                f"{_foreign(observation.reminder_error)}",
            )
        )
    return found


# A refusal by GitHub is the credential's fault, said as such; anything else is an outage.
REFUSALS = frozenset({401, 403})


def _admin_refusal(call: str, status: int | None, error: str) -> Finding:
    if status == 401:
        return Finding(
            "token-rejected",
            "the token was rejected as invalid (HTTP 401): it has expired or been revoked",
        )
    if status == 403:
        need = "write" if call == "renewal" else "read"
        return Finding(
            f"{'renewal' if call == 'renewal' else 'limit'}-forbidden",
            f"the token was refused the {call} (HTTP 403): it does not reach this repository, "
            f"or lacks Administration: {need} on it",
        )
    return Finding("renewal-refused", f"the renewal was refused: {_foreign(error)}")


def reminder_move(observation: Observation) -> tuple[Reminder, str, str, str] | None:
    """The one reminder's new title and body, when its date is not the limit's.

    The reminder is a record of what GitHub reports, never the mechanism, so it follows the
    limit whenever both read: after a renewal, and after a renewal made by hand."""
    expires = expiry_of(observation.limit)
    reminders = observation.reminders
    if expires is None or reminders is None or len(reminders) != 1:
        return None
    reminder = reminders[0]
    date = f"{expires:%Y-%m-%d}"
    if reminder.date == date:
        return None
    title = REMINDER_FORMAT.format(date=date)
    body = REMINDER_BODY_DATE.sub(f"expires `{date}`", reminder.body)
    return reminder, date, title, body


# --- the verdict -------------------------------------------------------------------------


@dataclass
class Audit:
    observations: list[Observation]
    now: datetime
    dry_run: bool
    run_url: str
    credential: str = "unknown"  # the admin token's kind, from GitHub.credential
    found: dict[str, list[Finding]] = field(init=False)

    def __post_init__(self) -> None:
        self.found = {
            o.repository: findings_for(o, self.now)
            for o in sorted(self.observations, key=lambda o: o.repository)
        }

    @property
    def gaps(self) -> dict[str, list[Finding]]:
        return {repository: found for repository, found in self.found.items() if found}

    @property
    def verdict(self) -> str:
        # No repository is no answer: the organization always has public repositories.
        if not self.observations or self.gaps:
            return "gap"
        return "clean"

    @property
    def heartbeat(self) -> str:
        # A dry run renews nothing, so it never says the renewal happened.
        return "ping" if self.verdict == "clean" and not self.dry_run else "withhold"

    @property
    def state(self) -> dict[str, list[str]]:
        return {repo: sorted({f.code for f in found}) for repo, found in self.gaps.items()}

    def result(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "heartbeat": self.heartbeat,
            "dry_run": self.dry_run,
            "checked_at": f"{self.now:%Y-%m-%dT%H:%M:%SZ}",
            "repositories": {o.repository: self._row(o) for o in self.observations},
        }

    def _row(self, o: Observation) -> dict[str, Any]:
        return {
            "renewed": o.renewal == "renewed",
            "limit": o.limit.get("limit") if o.limit is not None else None,
            "expires_at": o.limit.get("expires_at") if o.limit is not None else None,
            "reminders": [r.number for r in o.reminders] if o.reminders is not None else None,
            "reminder_moved": list(o.reminder_moved) if o.reminder_moved else None,
            "findings": [f.code for f in self.found[o.repository]],
        }

    def gap_plan(self, open_issues: list[dict[str, Any]]) -> dict[str, Any] | None:
        """What to do with this repository's one gap issue: create it, update it, close it,
        or nothing. ``open_issues`` is the home repository's REST open-issues list. A second
        copy of its own (a create GitHub accepted after the call timed out) is closed as one."""
        own = sorted(
            (
                issue
                for issue in open_issues
                if "pull_request" not in issue
                and issue.get("title") == GAP_TITLE
                and _login(issue) == GAP_AUTHOR
            ),
            key=lambda issue: issue["number"],
        )
        state = self.state
        if not own:
            if not state:
                return None
            return {
                "action": "create",
                "title": GAP_TITLE,
                "body": self._body(),
                "labels": [GAP_LABEL],
                "extras": [],
            }
        existing, extras = own[0], [_extra(issue, own[0]["number"]) for issue in own[1:]]
        number = existing["number"]
        labelled = GAP_LABEL in _labels_of(existing)
        before = _state_in(existing.get("body"))
        if not state:
            removed = f" `{GAP_LABEL}` removed." if labelled else ""
            return {
                "action": "close",
                "number": number,
                "comment": (
                    f"Every public repository now meets {LOCKDOWN}.{removed} Closed by that "
                    f"run, which is the verification: {self.run_url}"
                ),
                "unlabel": [GAP_LABEL] if labelled else [],
                "extras": extras,
            }
        if state != before:
            # Only what is new pages the capital again; a repository that became clean is
            # said, never re-labelled for.
            new = any(before.get(repository) != codes for repository, codes in state.items())
            return {
                "action": "update",
                "number": number,
                "body": self._body(),
                "comment": self._change_comment(before),
                "relabel": labelled,
                "labels": [GAP_LABEL] if new else [],
                "extras": extras,
            }
        if extras:
            return {"action": "none", "extras": extras}
        return None

    def _statuses(self, status: int) -> list[str]:
        return [
            o.repository
            for o in sorted(self.observations, key=lambda o: o.repository)
            if status in (o.renewal_status, o.limit_status)
        ]

    def _credential(self) -> str:
        """What the admin token needs, when GitHub refused it: which repositories, which
        permission, and how to give it, in the words of the kind of token it is."""
        rejected, forbidden = self._statuses(401), self._statuses(403)
        if not rejected and not forbidden:
            return ""
        said = []
        if rejected:
            said.append(
                f"`FLEET_ADMIN_TOKEN` was rejected as invalid (HTTP 401) on {_listed(rejected)}: "
                "it has expired or been revoked, and a new one is needed."
            )
        if forbidden:
            said.append(
                f"`FLEET_ADMIN_TOKEN` was refused (HTTP 403) on {_listed(forbidden)}: it does not "
                f"reach {_plural(len(forbidden), 'that repository', 'those repositories')}, or "
                "lacks the permission there."
            )
        how = CREDENTIAL_HOW.get(self.credential)
        return f"""## The Credential It Needs

{" ".join(said)}

{CREDENTIAL_NEED} The token must reach every public repository in the organization; this run \
audited {_listed(list(self.found))}.

{f"{how}{chr(10)}{chr(10)}" if how else ""}{CREDENTIAL_LIMITS}

"""

    def _body(self) -> str:
        rows = "\n".join(
            f"| `{repository}` | {'<br>'.join(f.detail for f in found)} |"
            for repository, found in self.gaps.items()
        )
        count = len(self.gaps)
        return f"""## What Was Found

The lockdown audit found **{count}** public {_plural(count, "repository", "repositories")} \
short of {LOCKDOWN}.

| Repository | What Is Missing |
|---|---|
{rows}

- **When:** {self.now:%Y-%m-%d %H:%MZ}, by the workflow, not by a session: {self.run_url}
- **Heartbeat:** `Public Repository Lockdown` was not pinged; it pages when its grace passes.

{self._credential()}## What Happens Next

{GAP_NEXT}

{_marker(self.state)}
"""

    def _change_comment(self, before: dict[str, list[str]]) -> str:
        now = self.state
        parts = []
        new = {r: c for r, c in now.items() if before.get(r) != c}
        cleared = sorted(r for r in before if r not in now)
        if new:
            listed = "\n".join(
                f"- `{repository}`: {'; '.join(f.detail for f in self.gaps[repository])}"
                for repository in sorted(new)
            )
            parts.append(f"**Changed Since the Last Run:**\n\n{listed}")
        if cleared:
            parts.append("**Now Clean:** " + ", ".join(f"`{r}`" for r in cleared))
        if self._credential():
            parts.append(self._credential().rstrip())
        if new:
            parts.append(f"`{GAP_LABEL}` applied for what is new here.")
        parts.append(f"Run: {self.run_url}")
        return "\n\n".join(parts)


def _listed(repositories: list[str]) -> str:
    return ", ".join(f"`{repository}`" for repository in repositories)


def _labels_of(issue: dict[str, Any]) -> list[str]:
    labels = issue.get("labels") or []
    return [label.get("name") for label in labels if isinstance(label, dict)]


def _extra(issue: dict[str, Any], kept: int) -> dict[str, Any]:
    return {
        "number": issue["number"],
        "comment": f"A second copy of #{kept}, which is the record. Closed by the lockdown audit.",
        "unlabel": [GAP_LABEL] if GAP_LABEL in _labels_of(issue) else [],
    }


def _login(issue: dict[str, Any]) -> str | None:
    user = issue.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    return login if isinstance(login, str) else None


def _state_in(body: Any) -> dict[str, list[str]]:
    # The last marker: the real one closes the body. One edited by hand is read for what it
    # still says, never trusted for more.
    matches = MARKER.findall(body if isinstance(body, str) else "")
    try:
        state = json.loads(matches[-1]) if matches else {}
    except json.JSONDecodeError:
        return {}
    if not isinstance(state, dict):
        return {}
    return {
        repo: sorted(code for code in codes if isinstance(code, str))
        for repo, codes in state.items()
        if isinstance(repo, str) and isinstance(codes, list)
    }


def _marker(state: dict[str, list[str]]) -> str:
    data = json.dumps(state, separators=(",", ":"), sort_keys=True)
    data = data.replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<!-- public-repository-lockdown {data} -->"


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


# Text GitHub gave us (an error, a limit value), made safe where it is interpolated into
# the issue: a pipe splits a table row, a "<" could open a comment that reads as the marker,
# a backtick could unpair a code span, and GitHub reads a bare @name as a mention. The
# script's own markdown never passes through here.
def _foreign(text: Any) -> str:
    text = re.sub(r"\s+", " ", str(text)).replace("`", "'")
    text = text.replace("|", "\\|").replace("<", "&lt;")
    return re.sub(r"@[\w\-/.]+", lambda match: f"`{match.group(0)}`", text)


# --- the one boundary --------------------------------------------------------------------

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class GitHub:
    """Every call this audit makes, through ``gh api``, each under the token its half owns."""

    def __init__(self, admin_token: str, issues_token: str = "", run: Runner = subprocess.run):
        self._admin = admin_token
        self._issues = issues_token
        self._run = run

    def _api(
        self,
        token: str,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        paginate: bool = False,
        include: bool = False,
    ) -> str:
        if not token:
            raise GitHubError(f"no token for {method} {path}")
        command = ["gh", "api", "--method", method]
        if paginate:
            command += ["--paginate", "--slurp"]
        if include:
            command.append("--include")
        if payload is not None:
            command += ["--input", "-"]
        command.append(path)
        env = {**os.environ, "GH_TOKEN": token}
        env.pop("GITHUB_TOKEN", None)
        process = self._run(
            command,
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
        if process.returncode != 0:
            # gh can print a hint after the line that carries the status; keep that line.
            lines = (process.stderr or "").strip().splitlines()
            carrying = [line for line in lines if HTTP_STATUS.search(line)]
            message = (carrying or lines or [f"gh exited {process.returncode}"])[-1]
            raise GitHubError(message)
        return process.stdout

    def _json(self, token: str, path: str, **options: Any) -> Any:
        output = self._api(token, path, **options)
        try:
            return json.loads(output) if output.strip() else {}
        except json.JSONDecodeError:
            raise GitHubError(f"{path} answered with no readable JSON") from None

    def public_repositories(self, org: str) -> list[str]:
        pages = self._json(self._admin, f"orgs/{org}/repos?type=public&per_page=100", paginate=True)
        if not isinstance(pages, list):
            raise GitHubError("the repository listing answered with no list")
        names = set()
        for page in pages:
            for repo in page if isinstance(page, list) else []:
                if (
                    isinstance(repo, dict)
                    and repo.get("visibility") == "public"
                    and not repo.get("archived")
                    and isinstance(repo.get("full_name"), str)
                ):
                    names.add(repo["full_name"])
        return sorted(names)

    def renew(self, repository: str) -> None:
        self._api(
            self._admin,
            f"repos/{repository}/interaction-limits",
            method="PUT",
            payload={"limit": LIMIT, "expiry": EXPIRY},
        )

    def read_limit(self, repository: str) -> dict[str, Any]:
        answer = self._json(self._admin, f"repos/{repository}/interaction-limits")
        if not isinstance(answer, dict):
            raise GitHubError("the limit answered with no object")
        return answer

    def open_issues(self, repository: str) -> list[dict[str, Any]]:
        pages = self._json(
            self._issues, f"repos/{repository}/issues?state=open&per_page=100", paginate=True
        )
        if not isinstance(pages, list):
            raise GitHubError("the issue listing answered with no list")
        return [i for page in pages if isinstance(page, list) for i in page if isinstance(i, dict)]

    def edit_issue(self, repository: str, number: int, fields: dict[str, Any]) -> None:
        self._api(
            self._issues, f"repos/{repository}/issues/{number}", method="PATCH", payload=fields
        )

    def comment(self, repository: str, number: int, body: str) -> None:
        self._api(
            self._issues,
            f"repos/{repository}/issues/{number}/comments",
            method="POST",
            payload={"body": body},
        )

    def create_issue(self, repository: str, title: str, body: str, labels: list[str]) -> int:
        answer = self._api(
            self._issues,
            f"repos/{repository}/issues",
            method="POST",
            payload={"title": title, "body": body, "labels": labels},
        )
        return int(json.loads(answer)["number"])

    def remove_label(self, repository: str, number: int, label: str) -> None:
        self._api(
            self._issues, f"repos/{repository}/issues/{number}/labels/{label}", method="DELETE"
        )

    def add_label(self, repository: str, number: int, label: str) -> None:
        self._api(
            self._issues,
            f"repos/{repository}/issues/{number}/labels",
            method="POST",
            payload={"labels": [label]},
        )

    def credential(self, now: datetime) -> tuple[str, str | None]:
        """The admin token's kind, and a warning when it expires within ``EXPIRY_WARNING``,
        without a byte of the token, its owner, or its expiry date in either. Never fatal:
        the audit's own calls are what prove the token works."""
        kind = next(
            (kind for prefix, kind, _ in TOKEN_KINDS if self._admin.startswith(prefix)),
            "unknown",
        )
        try:
            raw = self._api(self._admin, "rate_limit", include=True)
        except GitHubError:
            return kind, None
        head = re.split(r"\r?\n\r?\n", raw, maxsplit=1)[0]
        expiry = None
        for line in head.splitlines()[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "github-authentication-token-expiration":
                expiry = value.strip()
        if not expiry:
            return kind, None
        try:
            expires = datetime.strptime(expiry.replace(" UTC", " +0000"), "%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            return kind, "FLEET_ADMIN_TOKEN reports an expiry this audit cannot read"
        left = expires - now
        if left > EXPIRY_WARNING:
            return kind, None
        return kind, (
            f"FLEET_ADMIN_TOKEN expires in {max(left.days, 0)} days: when it does, nothing is "
            "renewed. Rotating it is @origin's."
        )


# --- the run -----------------------------------------------------------------------------


def renew_and_read(github: GitHub, repository: str, dry_run: bool) -> Observation:
    """The guard's own half, under the admin token alone: nothing about issues can stop it."""
    observation = Observation(repository=repository)
    if not dry_run:
        try:
            github.renew(repository)
            observation.renewal = "renewed"
        except GitHubError as error:
            observation.renewal_error = str(error)
            observation.renewal_status = error.status
    # Read back after the write, never trusted from it.
    try:
        observation.limit = github.read_limit(repository)
    except GitHubError as error:
        observation.limit_error = str(error)
        observation.limit_status = error.status
    return observation


def read_and_move_reminder(
    github: GitHub, observation: Observation, dry_run: bool
) -> list[dict[str, Any]] | None:
    """The record's half, under the issues token. Returns the open issues it read."""
    repository = observation.repository
    try:
        issues = github.open_issues(repository)
    except GitHubError as error:
        observation.reminders_error = str(error)
        return None
    observation.reminders = reminders_in(issues)
    move = reminder_move(observation)
    if move is not None:
        reminder, date, title, body = move
        if dry_run:
            print(f"  DRY-RUN: would move {repository}#{reminder.number} {reminder.date} → {date}")
        else:
            try:
                github.edit_issue(repository, reminder.number, {"title": title, "body": body})
                observation.reminder_moved = (reminder.number, reminder.date, date)
            except GitHubError as error:
                observation.reminder_error = str(error)
    return issues


def _close(github: GitHub, number: int, comment: str, unlabel: list[str], reason: str) -> None:
    # The comment first, so the label change and the close ride it.
    github.comment(HOME_REPOSITORY, number, comment)
    for label in unlabel:
        github.remove_label(HOME_REPOSITORY, number, label)
    github.edit_issue(HOME_REPOSITORY, number, {"state": "closed", "state_reason": reason})


def apply_gap_plan(github: GitHub, plan: dict[str, Any], sleep: Callable[[float], None]) -> None:
    """A label rides its message: the body on a new issue, a comment posted first on an open
    one. The body is written last, because it carries what the next run compares against, so
    a run that fails midway repeats the step next time instead of losing it."""
    action = plan["action"]
    if action == "create":
        github.create_issue(HOME_REPOSITORY, plan["title"], plan["body"], plan["labels"])
    elif action == "close":
        _close(github, plan["number"], plan["comment"], plan["unlabel"], "completed")
    elif action == "update":
        number = plan["number"]
        github.comment(HOME_REPOSITORY, number, plan["comment"])
        for label in plan["labels"]:
            # Removed and re-added, because adding a label already there is no event.
            if plan["relabel"]:
                github.remove_label(HOME_REPOSITORY, number, label)
            for attempt in range(3):
                try:
                    github.add_label(HOME_REPOSITORY, number, label)
                    break
                except GitHubError:
                    if attempt == 2:
                        raise
                    sleep(10)
        github.edit_issue(HOME_REPOSITORY, number, {"body": plan["body"]})
    elif action != "none":
        raise ValueError(f"unknown plan action: {action}")
    for extra in plan["extras"]:
        _close(github, extra["number"], extra["comment"], extra["unlabel"], "not_planned")


def run_audit(
    github: GitHub,
    repositories: list[str],
    *,
    dry_run: bool,
    now: datetime,
    run_url: str,
    sleep: Callable[[float], None] = time.sleep,
    credential: str = "unknown",
) -> Audit:
    # Every renewal first, so a failure on the issues side (an App token that could not be
    # minted, an issue listing that errors) never costs a repository its renewal.
    observations = [renew_and_read(github, repository, dry_run) for repository in repositories]
    home_issues = None
    for observation in observations:
        issues = read_and_move_reminder(github, observation, dry_run)
        if observation.repository == HOME_REPOSITORY:
            home_issues = issues
    audit = Audit(
        observations=observations,
        now=now,
        dry_run=dry_run,
        run_url=run_url,
        credential=credential,
    )
    for observation in observations:
        print(_log_line(observation, dry_run))
    for repository, found in audit.gaps.items():
        for finding in found:
            print(f"::error::{repository}: {finding.detail}")

    # Reading the home repository's issues fails the run rather than skip the filing: a gap
    # nobody is told about is the failure this audit exists to end. The renewals are done.
    if home_issues is None:
        home_issues = github.open_issues(HOME_REPOSITORY)
    plan = audit.gap_plan(home_issues)
    if plan is not None:
        if dry_run:
            print(f"DRY-RUN: would apply {plan['action']} to the gap issue in {HOME_REPOSITORY}")
        else:
            apply_gap_plan(github, plan, sleep)
    return audit


def _log_line(o: Observation, dry_run: bool) -> str:
    limit = o.limit.get("limit") if o.limit is not None else "UNREADABLE"
    expires = o.limit.get("expires_at") if o.limit is not None else None
    reminders = [r.number for r in o.reminders] if o.reminders is not None else "UNREADABLE"
    renewed = "dry-run" if dry_run else o.renewal == "renewed"
    return (
        f"  {o.repository:<34} renewed={renewed} limit={limit} expires={expires} "
        f"reminders={reminders} moved={list(o.reminder_moved) if o.reminder_moved else None}"
    )


def read_repositories(path: Path) -> list[str]:
    names = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    for name in names:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", name):
            raise SystemExit(f"public-repository-lockdown: not OWNER/NAME: {name!r}")
    return names


def main(argv: list[str] | None = None, *, run: Runner = subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser(
        "repositories", help="List the organization's public repositories"
    )
    listing.add_argument("--org", default=ORG)
    listing.add_argument("--out", required=True, type=Path)
    audit = commands.add_parser("audit", help="Renew, read back, and record every repository")
    audit.add_argument("--repositories", required=True, type=Path)
    audit.add_argument("--result", required=True, type=Path)
    audit.add_argument("--dry-run", action="store_true", help="Read and report; write nothing")
    options = parser.parse_args(argv)

    admin = os.environ.get("ADMIN_TOKEN", "")
    if not admin:
        print(
            "public-repository-lockdown: ADMIN_TOKEN is empty. It is the FLEET_ADMIN_TOKEN "
            "secret, a token with repository Administration: write on every public repository.",
            file=sys.stderr,
        )
        return 2

    if options.command == "repositories":
        names = GitHub(admin, run=run).public_repositories(options.org)
        if not names:
            print(
                f"public-repository-lockdown: '{options.org}' answered with no public "
                "repositories. An audit of nothing cannot be clean.",
                file=sys.stderr,
            )
            return 1
        options.out.write_text("\n".join(names) + "\n")
        print(f"{len(names)} public repositories: {', '.join(names)}")
        return 0

    # Empty when the App token could not be minted. The renewals still run; every reminder
    # then reads as unreadable and the run dies at the gap issue, so nothing is pinged.
    issues = os.environ.get("ISSUES_TOKEN", "")
    if not issues:
        print("::error::ISSUES_TOKEN is empty: renewing, but no reminder or issue can be read")
    repositories = read_repositories(options.repositories)
    if not repositories:
        print("public-repository-lockdown: the repository list is empty.", file=sys.stderr)
        return 1

    github = GitHub(admin, issues, run=run)
    now = datetime.now(timezone.utc)
    kind, warning = github.credential(now)
    print(f"credential: FLEET_ADMIN_TOKEN is {KIND_WORDS[kind]}")
    if warning:
        print(f"::warning::{warning}")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_url = (
        f"{server}/{os.environ.get('GITHUB_REPOSITORY', HOME_REPOSITORY)}"
        f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '0')}"
    )
    result = run_audit(
        github,
        repositories,
        dry_run=options.dry_run,
        now=now,
        run_url=run_url,
        credential=kind,
    )
    # Written last: a run that dies before here leaves no result, so nothing is pinged.
    options.result.write_text(json.dumps(result.result(), indent=2) + "\n")
    print(f"verdict: {result.verdict}, heartbeat: {result.heartbeat}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

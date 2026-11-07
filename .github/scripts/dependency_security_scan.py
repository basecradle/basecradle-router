"""The daily dependency security scan's verdict and its routing (#410).

Run from ``.github/workflows/dependency-security-scan.yml``::

    python3 .github/scripts/dependency_security_scan.py --open-issues open.json --plan plan.json

Nothing in this repository audited its dependencies on its own: an advisory against a
package in ``uv.lock`` reached us only if Dependabot opened a pull request for it, and
Dependabot does not move a transitive pin (``anyio`` and ``certifi`` sat on two Critical
advisories that way until this scan's first read, #410). Here that matters more than in a
library: ``uv.lock`` is what the daemon's box installs (the deploy's ``uv sync --locked``),
so a stale pin is a stale package on the fleet's crown-jewels box. The capital learned the
lesson on 2026-11-04, when a Rails security release went unnoticed for 23 hours
(basecradle#750). This is the clock: ``osv-scanner`` against ``uv.lock``, every day,
against ``main``.

The decisions are a port of the capital's ``lib/dependency_security_scan.rb``, by way of
basecradle-python's ``scripts/dependency_security_scan.py`` (basecradle-python#317), kept
in their shape and their wording so that all three read the same: ``DependencySecurityScan``
decides and calls nothing. It takes each scanner's advisories and the repository's open issues and
returns what to do on GitHub -- nothing, a new issue, an update to the one already open, or
closing one -- and the workflow applies it. ``main()`` runs the scanner and writes that plan
as JSON; it never calls GitHub itself.

Standard library only, so the workflow runs it with the runner's own ``python3``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ADVISORIES_TITLE = "[SECURITY] Dependency Advisories Against main (Daily Scan)"
ERROR_TITLE = "Daily Dependency Security Scan Could Not Reach a Verdict"

# Ruled by @origin, 2026-11-05 (basecradle#750): an advisory rated High or Critical pages
# him; everything else goes to the capital's inbox. An unrated advisory is not known to be
# High, so it does not page; it pages the day it is rated High.
PAGING_SEVERITIES = frozenset({"critical", "high"})
PAGING_LABEL = "needs-human"
INBOX_LABEL = "needs-capital"

SEVERITY_ORDER = ("critical", "high", "medium", "moderate", "low", "none", "unknown")
ECOSYSTEM_NAMES = {"pypi": "Python package"}

# The only author whose issues are the scan's own. This repository is public, so anyone
# can open an issue with the scan's title and a marker of their choosing; one the scan did
# not write is never read as what it knows, and never edited, labelled or closed by it.
# (The capital's repository is private, so its scan can go by title alone.) `gh issue list`
# names a GitHub App's bot as `app/<slug>`.
SCAN_AUTHOR = "app/basecradle-ai"

# Each issue body carries the state it was written for, so the next run can tell a
# standing finding (say nothing) from a changed one.
MARKER = re.compile(r"<!-- dependency-security-scan (\{.*\}) -->")

# The scan's one scanner. Its JSON carries each advisory's CVSS score and, for a GitHub
# advisory, GitHub's own rating; pip-audit's carries neither, so it could not route.
OSV_SCANNER = ("osv-scanner", "scan", "source", "--lockfile", "uv.lock", "--format", "json")

# GitHub's rating vocabulary, which is what a CVSS score is translated into.
GHSA_SEVERITIES = frozenset({"critical", "high", "moderate", "low"})


class ScanError(Exception):
    """A scanner ran and did not reach a verdict. Never reads as clean."""


def _key(ecosystem: str, package: str, advisory_id: str) -> str:
    return f"{ecosystem}:{package.replace('@', '')}:{advisory_id}"


def _rank_of(severity: str) -> int:
    return SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else len(SEVERITY_ORDER)


@dataclass(frozen=True)
class Advisory:
    ecosystem: str
    package: str
    affected: str
    id: str
    severity: str
    title: str
    url: str | None
    # Every name osv-scanner grouped this problem under, its key's id included.
    aliases: tuple[str, ...] = ()

    # No "@": the key is written into the issue, and GitHub reads a bare @name as a
    # mention. A Python package name cannot carry one; the key keeps the capital's rule.
    @property
    def key(self) -> str:
        return _key(self.ecosystem, self.package, self.id)

    # The keys this problem could have been filed under on an earlier day: OSV links a
    # GitHub advisory to a PyPA one after the fact, which moves the primary id.
    @property
    def alias_keys(self) -> list[str]:
        return [_key(self.ecosystem, self.package, alias) for alias in self.aliases]

    @property
    def pages(self) -> bool:
        return self.severity in PAGING_SEVERITIES

    @property
    def rank(self) -> tuple[int, str, str, str]:
        return (_rank_of(self.severity), self.ecosystem, self.package, self.key)


@dataclass(frozen=True)
class Plan:
    action: str
    number: int | None
    title: str
    body: str | None
    comment: str | None
    labels: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_osv_scanner(output: str, exit_status: int) -> list[Advisory]:
    """Read ``osv-scanner scan source --format json``.

    Exit 0 means no finding and exit 1 means at least one; anything that does not fit that
    is not a verdict. An unreachable advisory source exits 127 *with* a well-formed, empty
    report, so the exit status is checked against what the report holds, never trusted on
    its own and never ignored.

    osv-scanner groups the records that describe one problem (a GHSA, its PYSEC twin, the
    CVE behind both) and each group is one advisory here.
    """
    try:
        report = json.loads(output)
    except json.JSONDecodeError as error:
        raise ScanError(
            f"osv-scanner produced no readable report (exit {exit_status}): {type(error).__name__}"
        ) from None

    results = report.get("results") if isinstance(report, dict) else None
    if not isinstance(results, list) or not all(isinstance(result, dict) for result in results):
        raise ScanError(f"osv-scanner produced no results list (exit {exit_status})")

    # One problem is one finding. uv forks a package into one version per Python range when
    # it must, and osv-scanner can group the same problem's records differently per version
    # ([GHSA-a, PYSEC-b] against one, [PYSEC-b] against the other), so findings on one
    # package that share any name are merged, never told apart by their primary id alone.
    # A new finding absorbs every one it shares a name with, so the merge is transitive and
    # the findings kept are always pairwise unrelated.
    advisories: list[Advisory] = []
    for result in results:
        for entry in _list_of_dicts(result.get("packages", [])):
            for advisory in _advisories_in(entry):
                for seen in [a for a in advisories if _related(a, advisory)]:
                    advisories.remove(seen)
                    advisory = _merged(seen, advisory)
                advisories.append(advisory)

    if not ((exit_status == 0 and not advisories) or (exit_status == 1 and advisories)):
        raise ScanError(f"osv-scanner exited {exit_status} with {len(advisories)} advisories")

    return advisories


# Every version in the order osv-scanner gave them, the most severe rating, every name, and
# the better of the two primary names with its own title and link.
def _related(one: Advisory, other: Advisory) -> bool:
    same_package = (one.ecosystem, one.package) == (other.ecosystem, other.package)
    return same_package and bool(set(one.aliases) & set(other.aliases))


def _merged(seen: Advisory, other: Advisory) -> Advisory:
    first, second = (seen, other) if _primary_id([seen.id, other.id]) == seen.id else (other, seen)
    versions = seen.affected.split(", ")
    versions += [v for v in other.affected.split(", ") if v not in versions]
    return replace(
        first,
        affected=", ".join(versions),
        severity=min(seen.severity, other.severity, key=_rank_of),
        aliases=tuple(sorted({*seen.aliases, *other.aliases})),
        # A title that is only its name gives way to the other's real one.
        title=second.title if first.title == first.id else first.title,
    )


def _advisories_in(entry: dict[str, Any]) -> list[Advisory]:
    package = entry.get("package")
    groups = entry.get("groups", [])
    records = entry.get("vulnerabilities", [])
    if (
        not isinstance(package, dict)
        or not isinstance(package.get("name"), str)
        or not package["name"]
        or not isinstance(package.get("version"), str)
        or not isinstance(groups, list)
        or not isinstance(records, list)
        or (records and not groups)
    ):
        raise ScanError("osv-scanner printed a package this scan cannot read")

    by_id = {record["id"]: record for record in _list_of_dicts(records) if "id" in record}
    advisories = []
    for group in groups:
        ids = group.get("ids") if isinstance(group, dict) else None
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
            raise ScanError("osv-scanner printed an advisory group this scan cannot read")

        primary = _primary_id(ids)
        group_records = [by_id[i] for i in ids if i in by_id]
        names = group.get("aliases")
        names = [n for n in names if isinstance(n, str) and n] if isinstance(names, list) else []
        advisories.append(
            Advisory(
                ecosystem="pypi",
                package=package["name"],
                affected=package["version"],
                id=primary,
                severity=_severity_of(group.get("max_severity"), group_records),
                title=_title_of(primary, group_records),
                aliases=tuple(sorted({*ids, *names})),
                url=(
                    f"https://github.com/advisories/{primary}"
                    if primary.startswith("GHSA-")
                    else f"https://osv.dev/vulnerability/{primary}"
                ),
            )
        )
    return advisories


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ScanError("osv-scanner printed a list this scan cannot read")
    return value


# One problem is known by one name, the same every run: its GitHub advisory when it has
# one (what Dependabot, GitHub and most upstream changelogs cite), then its PyPA advisory,
# then whatever sorts first.
def _primary_id(ids: list[str]) -> str:
    for prefix in ("GHSA-", "PYSEC-"):
        named = [i for i in ids if i.startswith(prefix)]
        if named:
            return min(named)
    return min(ids)


# An advisory is as severe as the most severe rating its records carry: the CVSS score
# osv-scanner computes for the group (the highest across its records), or the rating
# GitHub's reviewers gave it. Neither is "unknown", and unknown does not page.
def _severity_of(max_severity: Any, records: list[dict[str, Any]]) -> str:
    ratings = []
    try:
        score = float(max_severity)
    except (TypeError, ValueError):
        score = None
    if score is not None and 0 <= score <= 10:
        ratings.append(_cvss_rating(score))
    for record in records:
        specific = record.get("database_specific")
        rating = specific.get("severity") if isinstance(specific, dict) else None
        if isinstance(rating, str) and rating.lower() in GHSA_SEVERITIES:
            ratings.append(rating.lower())
    return min(ratings, key=_rank_of) if ratings else "unknown"


# CVSS's qualitative scale (the same bands for v3 and v4), in GitHub's words: GitHub calls
# CVSS "Medium" Moderate, and a mixed table would read as two ratings.
def _cvss_rating(score: float) -> str:
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "moderate"
    if score > 0:
        return "low"
    return "none"


# A table cell, never a paragraph: a details paragraph is often one long line, and an issue
# body has a size limit GitHub enforces by refusing the write.
TITLE_LIMIT = 200


# The issues' fixed paragraphs, each one unbroken line in the body as in the capital's and
# basecradle-python's, held here so the templates and this file both stay readable.
ADVISORIES_NEXT = (
    "The fix is a bump on its own branch: `uv lock --upgrade-package <package>==<fixed "
    "version>`, the minimum version that resolves the advisory, then the Critical and High "
    'advisories read against the router on this issue (CLAUDE.md → "Responding to the Daily '
    "Dependency Scan\"). `uv.lock` is what the daemon's box installs, development group "
    "included (the deploy's `uv sync --locked`), so the fix is live only once the NOC's "
    "unattended deploy has carried that merge to the box; nothing here is released. An "
    "advisory that does not apply here is dismissed in `osv-scanner.toml` (`[[IgnoredVulns]]`, "
    "with its reason and an `ignoreUntil`). Closing this issue dismisses nothing: while an "
    "advisory stands, the next scan files a new issue."
)

ERROR_HAPPENED = (
    "The daily dependency security scan of `main` ran and could not reach a verdict, so it "
    "says nothing about whether `main` is clean. While this stands, a new advisory goes "
    "unseen."
)

ERROR_NEXT = (
    "Read the run's log, fix what stopped the scan, and re-run the workflow by hand "
    "(`Dependency Security Scan`, *Run workflow*). One issue is filed while the scan cannot "
    "run, not one a day, and the run that reaches a verdict again closes it."
)


# Any record's summary before any record's details, the primary record's first.
def _title_of(primary: str, records: list[dict[str, Any]]) -> str:
    ordered = sorted(records, key=lambda record: record.get("id") != primary)
    texts = [record.get("summary") for record in ordered] + [r.get("details") for r in ordered]
    for text in texts:
        line = _first_line(text)
        if line:
            return line if len(line) <= TITLE_LIMIT else line[: TITLE_LIMIT - 1].rstrip() + "…"
    return primary


BOLD_HEADING = re.compile(r"(\*\*|__)[^*_]+\1:?")


# The first line that says something. Details often open with a heading ("### Impact",
# "Impact" over a "------" rule, "**Impact**"), which says nothing.
def _first_line(text: Any) -> str | None:
    lines = [line.strip() for line in text.splitlines()] if isinstance(text, str) else []
    in_fence = False
    for index, line in enumerate(lines):
        following = lines[index + 1] if index + 1 < len(lines) else ""
        # A code block says nothing a title can: its fences and everything between them.
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if (
            in_fence
            or not line
            or line.startswith("#")
            or set(line) <= set("-=*_`~ ")
            or (following and set(following) <= set("-="))
            or BOLD_HEADING.fullmatch(line)
        ):
            continue
        return line
    return None


class DependencySecurityScan:
    def __init__(
        self,
        *,
        advisories: Iterable[Advisory],
        ref: str,
        sha: str,
        run_url: str,
        errors: dict[str, str] | None = None,
        now: datetime | None = None,
    ) -> None:
        """errors: ``{"pypi": "why that scan reached no verdict"}``, one entry per scanner
        that failed. A failed scanner says nothing about its ecosystem, so its earlier
        findings are carried forward, never reported as resolved."""
        self.advisories = sorted(advisories, key=lambda advisory: advisory.rank)
        self.errors = dict(errors or {})
        self._ref = ref
        self._sha = sha
        self._run_url = run_url
        self._now = now or datetime.now(timezone.utc)

    @property
    def verdict(self) -> str:
        if self.errors:
            return "error"
        return "advisories" if self.advisories else "clean"

    def plans(self, open_issues: list[dict[str, Any]]) -> list[Plan]:
        """open_issues: the repository's open issues, as ``gh issue list --json
        number,title,body,author`` returns them."""
        own = [issue for issue in open_issues if _author_of(issue) == SCAN_AUTHOR]
        candidates = (self._advisories_plan(own), self._error_plan(own))
        return [plan for plan in candidates if plan is not None]

    def _advisories_plan(self, open_issues: list[dict[str, Any]]) -> Plan | None:
        existing = next((i for i in open_issues if i.get("title") == ADVISORIES_TITLE), None)
        state = _state_in(existing.get("body")).get("advisories", {}) if existing else {}
        # A marker edited by hand is read for what it still says, never trusted for more.
        known = {
            key: severity
            for key, severity in (state.items() if isinstance(state, dict) else [])
            if isinstance(severity, str)
        }
        # Filed under another of its names: the same finding, not a new one and a resolved
        # one, which would page again for what is already on the issue. Never a name that
        # is itself reported this run: that one is its own finding.
        # Two names filed apart and linked since are folded into the one now reported.
        reported = {advisory.key for advisory in self.advisories}
        renamed = []
        for advisory in self.advisories:
            for earlier in advisory.alias_keys:
                if earlier != advisory.key and earlier in known and earlier not in reported:
                    # The most severe rating either name was filed at, so what paged under
                    # one name is not news under the other.
                    severity = known.pop(earlier)
                    known[advisory.key] = min(
                        known.get(advisory.key, severity), severity, key=_rank_of
                    )
                    renamed.append((earlier, advisory.key))
        carried = {key: s for key, s in known.items() if key.split(":")[0] in self.errors}
        current = dict(sorted({**carried, **{a.key: a.severity for a in self.advisories}}.items()))

        added = [a for a in self.advisories if a.key not in known]
        rerated = [a for a in self.advisories if a.key in known and known[a.key] != a.severity]
        # Rated High or Critical after it was filed as something lower: that pages too.
        escalated = [a for a in rerated if a.pages and known[a.key] not in PAGING_SEVERITIES]
        resolved = [key for key in known if key not in current]
        if not added and not rerated and not resolved and not renamed:
            return None

        announced = added + escalated
        labels = [_label_for(announced)] if announced else []
        body = self._advisories_body(current, carried)

        if existing:
            return Plan(
                action="update",
                number=existing["number"],
                title=ADVISORIES_TITLE,
                body=body,
                comment=self._change_comment(
                    added, rerated, resolved, renamed, known, current, labels
                ),
                labels=labels,
            )
        return Plan(
            action="create",
            number=None,
            title=ADVISORIES_TITLE,
            body=body,
            comment=None,
            labels=labels,
        )

    # One issue while the scan cannot run, never one a day: filed when a scanner first
    # fails, updated when which scanners fail changes, and closed by the run that reaches a
    # verdict again (that run is the verification).
    def _error_plan(self, open_issues: list[dict[str, Any]]) -> Plan | None:
        existing = next((i for i in open_issues if i.get("title") == ERROR_TITLE), None)
        failing = sorted(self.errors)

        if existing is None:
            if not failing:
                return None
            return Plan(
                action="create",
                number=None,
                title=ERROR_TITLE,
                body=self._error_body(failing),
                comment=None,
                labels=[INBOX_LABEL],
            )
        if not failing:
            return Plan(
                action="close",
                number=existing["number"],
                title=ERROR_TITLE,
                body=None,
                comment=(
                    f"Every scanner reached a verdict on `{self._sha}` (`{self._ref}`), so the "
                    f"clock is running again. Closed by that run: {self._run_url}"
                ),
                labels=[],
            )
        if failing != _state_in(existing.get("body")).get("failing"):
            return Plan(
                action="update",
                number=existing["number"],
                title=ERROR_TITLE,
                body=self._error_body(failing),
                comment=(
                    f"Which scans fail has changed. Now failing: the {_names(failing)}. "
                    f"`{INBOX_LABEL}` applied for the change.\n\n{self._error_list()}\n\n"
                    f"Run: {self._run_url}"
                ),
                labels=[INBOX_LABEL],
            )
        return None

    def _advisories_body(self, current: dict[str, str], carried: dict[str, str]) -> str:
        table = _table(self.advisories) if self.advisories else ""
        return f"""## What Was Found

{self._headline(carried)}

- **Where:** `uv.lock` at `{self._sha}` (`{self._ref}`).
- **When:** scanned {self._when()}, by the scheduled scan and not by a session: {self._run_url}
- **Actor:** none. These are public upstream disclosures, not events on our systems.
- **Class:** a dependency advisory.
- **Contained So Far:** nothing. Nobody has read these against the router yet.

{table}
{self._carried_note(carried)}
## What Happens Next

{ADVISORIES_NEXT}

## How This Was Routed

{self._routing()}

{_marker({"advisories": current})}
"""

    def _headline(self, carried: dict[str, str]) -> str:
        if not self.advisories and carried:
            return (
                "This run found no advisory in what it could scan, and could not scan everything."
            )
        if not self.advisories:
            return "The daily scan of `main` finds no advisory now. Close this issue by hand."
        return (
            f"The daily scan of `main` found **{_count_of(self.advisories)}** against what "
            f"`main` resolves: {_tally(self.advisories)}."
        )

    def _routing(self) -> str:
        if not self.advisories:
            return "Nothing is reported now, so this run applied no label."
        if any(a.pages for a in self.advisories):
            return (
                f"`{PAGING_LABEL}` is applied when an advisory rated High or Critical appears, "
                "and it pages `@origin` (his ruling, 2026-11-05, basecradle#750)."
            )
        return (
            f"`{INBOX_LABEL}`: nothing here is rated High or Critical, so nothing paged. Only "
            "High and Critical page `@origin` (his ruling, 2026-11-05, basecradle#750)."
        )

    def _carried_note(self, carried: dict[str, str]) -> str:
        if not carried:
            return ""
        verb = "finding is" if len(carried) == 1 else "findings are"
        return (
            f"\n**Not Re-Checked This Run:** the {_names(self.errors)} could not run, so "
            f"{len(carried)} earlier {verb} carried forward unlisted, neither confirmed nor "
            "cleared.\n"
        )

    def _change_comment(
        self,
        added: list[Advisory],
        rerated: list[Advisory],
        resolved: list[str],
        renamed: list[tuple[str, str]],
        known: dict[str, str],
        current: dict[str, str],
        labels: list[str],
    ) -> str:
        parts = []
        if added:
            parts.append(
                f"**New Since the Last Scan ({_count_of(added)}: {_tally(added)}):**\n\n"
                f"{_table(added)}"
            )
        if rerated:
            changes = ", ".join(
                f"`{a.key}` {str(known[a.key]).capitalize()} → {a.severity.capitalize()}"
                for a in rerated
            )
            parts.append(f"**Re-Rated:** {changes}")
        if resolved:
            listed = ", ".join(f"`{key}`" for key in resolved)
            parts.append(f"**No Longer Reported ({len(resolved)}):** {listed}")
        if renamed:
            names = ", ".join(f"`{earlier}` is now `{key}`" for earlier, key in renamed)
            parts.append(f"**Known by Another Name Now:** {names}")
        if labels:
            parts.append(f"`{labels[0]}` applied for what is new here.")
        if not current:
            parts.append(f"The scan of `main` at `{self._sha}` is clean. Close this issue by hand.")
        parts.append(f"Run: {self._run_url}")
        return "\n\n".join(parts)

    def _error_body(self, failing: list[str]) -> str:
        return f"""## What Happened

{ERROR_HAPPENED}

- **Where:** `{self._sha}` (`{self._ref}`), {self._when()}: {self._run_url}

{self._error_list()}

## What Happens Next

{ERROR_NEXT}

{_marker({"failing": failing})}
"""

    def _error_list(self) -> str:
        return "\n".join(
            f"- **{ECOSYSTEM_NAMES.get(ecosystem, ecosystem)} scan:** {_cell(reason)}"
            for ecosystem, reason in sorted(self.errors.items())
        )

    def _when(self) -> str:
        return self._now.strftime("%Y-%m-%d %H:%MZ")


def _author_of(issue: dict[str, Any]) -> str | None:
    author = issue.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    return login if isinstance(login, str) else None


def _state_in(body: Any) -> dict[str, Any]:
    # The last marker, not the first: the real one closes the body, and nothing written
    # after it comes from upstream.
    matches = MARKER.findall(body if isinstance(body, str) else "")
    try:
        state = json.loads(matches[-1]) if matches else {}
    except json.JSONDecodeError:
        return {}
    return state if isinstance(state, dict) else {}


def _marker(state: dict[str, Any]) -> str:
    # "<" and ">" escaped inside the JSON (still the same JSON), so nothing in it can close
    # the comment early.
    data = json.dumps(state, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<!-- dependency-security-scan {data} -->"


def _label_for(advisories: list[Advisory]) -> str:
    return PAGING_LABEL if any(a.pages for a in advisories) else INBOX_LABEL


def _names(ecosystems: Iterable[str]) -> str:
    return " and ".join(
        f"{ECOSYSTEM_NAMES.get(ecosystem, ecosystem)} scan" for ecosystem in sorted(ecosystems)
    )


def _table(advisories: list[Advisory]) -> str:
    rows = []
    for a in advisories:
        link = f"[{a.id}]({a.url})" if a.url else a.id
        rows.append(
            f"| {a.severity.capitalize()} | {ECOSYSTEM_NAMES[a.ecosystem]} | {_code(a.package)} "
            f"| {_code(a.affected)} | {_cell(link)} | {_cell(a.title)} |"
        )
    header = [
        "| Severity | Ecosystem | Package | Installed or Affected | Advisory | Title |",
        "|---|---|---|---|---|---|",
    ]
    return "\n".join([*header, *rows]) + "\n"


def _count_of(advisories: list[Advisory]) -> str:
    return f"{len(advisories)} {'advisory' if len(advisories) == 1 else 'advisories'}"


def _tally(advisories: list[Advisory]) -> str:
    counts = Counter(a.severity for a in advisories)
    ordered = sorted(counts.items(), key=lambda item: _rank_of(item[0]))
    return ", ".join(f"{count} {severity.capitalize()}" for severity, count in ordered)


# Upstream text, made safe for a table cell: a pipe would split the row, a "<" could open a
# comment that reads as this scan's own marker, and GitHub reads a bare @name as a mention
# of whoever holds that account. Upstream backticks go first: a stray one pairs with the
# span opened around an @name, which leaves that name outside any span and mentions it (the
# harness's review finding, basecradle-harness#818).
def _cell(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text)).replace("`", "")
    text = text.replace("<", "&lt;").replace("|", "\\|")
    return re.sub(r"@[\w\-/.]+", lambda match: f"`{match.group(0)}`", text)


# The same, inside a code span, where the pipe is still a cell boundary. GitHub reads no
# mention inside a code span, and the "<" is escaped as in a cell: a lockfile's name and
# version never hold one, so it only ever stops a marker.
def _code(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text)).replace("|", "\\|")
    return f"`{text.replace('<', '&lt;')}`" if text else ""


def scan_pypi(root: Path) -> list[Advisory]:
    """``osv-scanner`` over ``uv.lock``: every package the daemon and its development
    environment resolve, runtime and dev group alike (the box installs both). It queries the
    OSV API at run time, so the advisory source is as fresh as the run."""
    process = subprocess.run(
        OSV_SCANNER, cwd=root, capture_output=True, text=True, timeout=600, check=False
    )
    print(process.stderr, file=sys.stderr)
    return parse_osv_scanner(process.stdout, process.returncode)


SCANNERS: dict[str, Callable[[Path], list[Advisory]]] = {"pypi": scan_pypi}

# The repository root, where ``uv.lock`` is: this file is ``.github/scripts/<name>``.
ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--open-issues",
        required=True,
        type=Path,
        help="Open issues, from `gh issue list --json number,title,body,author`",
    )
    parser.add_argument("--plan", required=True, type=Path, help="Where to write the plan")
    options = parser.parse_args(argv)

    advisories: list[Advisory] = []
    errors: dict[str, str] = {}
    for ecosystem, scanner in SCANNERS.items():
        try:
            advisories.extend(scanner(ROOT))
        except ScanError as error:
            errors[ecosystem] = str(error)
        # Whatever stopped a scanner, it reached no verdict, and that is filed, never lost
        # in a red run.
        except Exception as error:  # noqa: BLE001
            errors[ecosystem] = f"{type(error).__name__}: {error}"

    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repository = os.environ.get("GITHUB_REPOSITORY", "local")
    run_url = f"{server}/{repository}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '0')}"
    scan = DependencySecurityScan(
        advisories=advisories,
        errors=errors,
        ref=os.environ.get("GITHUB_REF_NAME", "local"),
        sha=os.environ.get("GITHUB_SHA", "local"),
        run_url=run_url,
    )
    plans = scan.plans(json.loads(options.open_issues.read_text()))

    plan = {"verdict": scan.verdict, "plans": [p.to_dict() for p in plans]}
    options.plan.write_text(json.dumps(plan, indent=2) + "\n")
    print(
        f"verdict: {scan.verdict} ({len(advisories)} advisories, {len(errors)} scan errors, "
        f"{len(plans)} GitHub actions planned)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

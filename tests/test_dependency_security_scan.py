"""The daily dependency security scan's decisions (#410): what it reads, how it routes, and
what it writes on GitHub.

The workflow (``.github/workflows/dependency-security-scan.yml``) only applies the plan
these decisions produce, so this file is where the rules are pinned: one issue and never a
second, High and Critical page and nothing else does, a scan that cannot reach a verdict
never reads as clean, and nothing upstream can break the issue, pose as its marker, or
mention a stranger. A port of basecradle-python's ``tests/test_dependency_security_scan.py``
(itself the capital's ``test/lib/dependency_security_scan_test.rb``, test for test, plus what
reading ``osv-scanner`` adds), plus where this repository keeps the script.

The script lives with this repository's other CI scripts in ``.github/scripts/``, which is
not an importable package, so it is loaded from its path. It is registered in
``sys.modules`` before it runs because ``dataclasses`` looks its own module up there.
"""

import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".github" / "scripts" / "dependency_security_scan.py"

_spec = importlib.util.spec_from_file_location("dependency_security_scan", SCRIPT)
assert _spec is not None and _spec.loader is not None
scan_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = scan_module
_spec.loader.exec_module(scan_module)

ADVISORIES_TITLE = scan_module.ADVISORIES_TITLE
ERROR_TITLE = scan_module.ERROR_TITLE
Advisory = scan_module.Advisory
DependencySecurityScan = scan_module.DependencySecurityScan
Plan = scan_module.Plan
ScanError = scan_module.ScanError
parse_osv_scanner = scan_module.parse_osv_scanner

# --- helpers ---------------------------------------------------------------------------


def sole(items: list[Any]) -> Any:
    assert len(items) == 1, items
    return items[0]


def _without_code_spans(text: str) -> str:
    return re.sub(r"`[^`]*`", "", text)


def group(
    *ids: str,
    max_severity: str = "9.3",
    rating: str | None = "CRITICAL",
    summary: str | None = None,
    details: str = "",
    aliases: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {"ids": list(ids), "max_severity": max_severity, "_rating": rating,
            "_summary": summary, "_details": details, "_aliases": list(aliases)}  # fmt: skip


def package(name: str, *groups: dict[str, Any]) -> dict[str, Any]:
    records = []
    for g in groups:
        for record_id in g["ids"]:
            record: dict[str, Any] = {
                "id": record_id,
                "summary": f"Possible problem in {name}"
                if g["_summary"] is None
                else g["_summary"],
                "details": g["_details"],
                "severity": [{"type": "CVSS_V4", "score": "CVSS:4.0/AV:N/AC:L"}],
            }
            if record_id.startswith("GHSA-") and g["_rating"]:
                record["database_specific"] = {"severity": g["_rating"], "github_reviewed": True}
            records.append(record)
    return {
        "package": {"name": name, "version": "4.13.0", "ecosystem": "PyPI"},
        "groups": [{"ids": g["ids"], "aliases": g["ids"] + g["_aliases"],
                    "max_severity": g["max_severity"]} for g in groups],
        "vulnerabilities": records,
    }  # fmt: skip


def report(*packages: dict[str, Any]) -> str:
    results = [
        {"source": {"path": "/repo/uv.lock", "type": "lockfile"}, "packages": list(packages)}
    ]
    return json.dumps({"results": results if packages else [], "experimental_config": {}})


def pypi(
    name: str, id: str, severity: str, title: str | None = None, aliases: tuple[str, ...] = ()
) -> Advisory:
    return Advisory(
        ecosystem="pypi",
        package=name,
        affected="0.28.1",
        id=id,
        severity=severity,
        title=title or f"Possible problem in {name}",
        url=f"https://github.com/advisories/{id}",
        aliases=aliases or (id,),
    )


def context() -> dict[str, Any]:
    return {
        "ref": "main",
        "sha": "0123abc",
        "run_url": "https://github.com/basecradle/basecradle-router/actions/runs/1",
        "now": datetime(2026, 11, 7, 14, 27, tzinfo=timezone.utc),
    }


def scan_of(*advisories: Advisory, errors: dict[str, str] | None = None) -> DependencySecurityScan:
    return DependencySecurityScan(advisories=advisories, errors=errors or {}, **context())


# How `gh issue list --json author` names the basecradle-ai App's bot.
SCAN_BOT = {"is_bot": True, "login": "app/basecradle-ai"}
STRANGER = {"id": "U_kgDOC0ffee", "is_bot": False, "login": "john"}


def open_issue(plan: Plan, author: dict[str, Any] = SCAN_BOT) -> dict[str, Any]:
    return {"number": 812, "title": plan.title, "body": plan.body, "author": author}


def unrelated_issue() -> dict[str, Any]:
    return {
        "number": 3,
        "title": "Something Else Entirely",
        "body": '<!-- dependency-security-scan {"keys":["pypi:httpx:GHSA-1111-2222-3333"]} -->',
    }


# --- reading osv-scanner ---------------------------------------------------------------


def test_reads_an_osv_scanner_report_into_advisories() -> None:
    advisory = sole(parse_osv_scanner(report(package("anyio", group("GHSA-1111-2222-3333"))), 1))

    assert advisory.ecosystem == "pypi"
    assert advisory.package == "anyio"
    assert advisory.affected == "4.13.0"
    assert advisory.id == "GHSA-1111-2222-3333"
    assert advisory.severity == "critical"
    assert advisory.url == "https://github.com/advisories/GHSA-1111-2222-3333"
    assert advisory.title == "Possible problem in anyio"
    assert advisory.key == "pypi:anyio:GHSA-1111-2222-3333"
    assert advisory.pages


def test_a_clean_osv_scanner_report_is_no_advisories() -> None:
    assert parse_osv_scanner(report(), 0) == []


def test_one_problem_under_several_names_is_one_advisory_known_by_its_github_name() -> None:
    advisory = sole(
        parse_osv_scanner(
            report(package("certifi", group("PYSEC-2026-4320", "GHSA-4444-5555-6666"))), 1
        )
    )

    assert advisory.id == "GHSA-4444-5555-6666"
    assert advisory.key == "pypi:certifi:GHSA-4444-5555-6666"


def test_an_advisory_with_no_github_name_is_known_by_its_pypa_name_and_linked_on_osv() -> None:
    advisory = sole(
        parse_osv_scanner(report(package("idna", group("PYSEC-2026-0007", "CVE-2026-0007"))), 1)
    )

    assert advisory.id == "PYSEC-2026-0007"
    assert advisory.url == "https://osv.dev/vulnerability/PYSEC-2026-0007"


@pytest.mark.parametrize(
    ("score", "severity"),
    [("9.0", "critical"), ("7.0", "high"), ("6.9", "moderate"), ("4.0", "moderate"),
     ("3.9", "low"), ("0.0", "none")],
)  # fmt: skip
def test_a_cvss_score_is_rated_on_cvss_bands_in_githubs_words(score: str, severity: str) -> None:
    unrated = package("idna", group("PYSEC-2026-0007", max_severity=score, rating=None))

    assert sole(parse_osv_scanner(report(unrated), 1)).severity == severity


def test_an_advisory_is_as_severe_as_its_most_severe_rating() -> None:
    nvd_higher = package(
        "idna", group("GHSA-1111-2222-3333", max_severity="7.5", rating="MODERATE")
    )
    github_higher = package("h11", group("GHSA-4444-5555-6666", max_severity="5.3", rating="HIGH"))

    advisories = parse_osv_scanner(report(nvd_higher, github_higher), 1)

    assert [a.severity for a in advisories] == ["high", "high"]


def test_an_unrated_advisory_is_unknown_and_does_not_page() -> None:
    unrated = package("idna", group("GHSA-1111-2222-3333", max_severity="", rating=None))
    advisory = sole(parse_osv_scanner(report(unrated), 1))

    assert advisory.severity == "unknown"
    assert not advisory.pages


def test_any_records_summary_beats_the_primary_records_details() -> None:
    entry = package("anyio", group("GHSA-1111-2222-3333", "PYSEC-2026-0007"))
    ghsa, pysec = entry["vulnerabilities"]
    ghsa["summary"], ghsa["details"] = "", "### Impact\nA long paragraph."
    pysec["summary"] = "TLS host name spoofing"

    assert sole(parse_osv_scanner(report(entry), 1)).title == "TLS host name spoofing"


@pytest.mark.parametrize(
    "details",
    ["### Impact\n\nFirst", "Impact\n------\nFirst", "Impact\n======\nFirst",
     "**Impact**\nFirst", "__Impact:__\nFirst", "---\nFirst", "```\npip install idna\n```\nFirst",
     "```python\nimport idna\n```\nFirst", "~~~\ncode\n~~~\nFirst"],
)  # fmt: skip
def test_a_details_heading_is_never_the_title(details: str) -> None:
    entry = package("idna", group("GHSA-1111-2222-3333", summary="", details=details))

    assert sole(parse_osv_scanner(report(entry), 1)).title == "First"


def test_a_line_that_merely_opens_and_closes_in_bold_is_still_a_title() -> None:
    entry = package("idna", group("GHSA-1111-2222-3333", summary="**Impact:** crash on **bold**"))

    assert sole(parse_osv_scanner(report(entry), 1)).title == "**Impact:** crash on **bold**"


def test_a_title_is_a_table_cell_never_a_paragraph() -> None:
    entry = package("idna", group("GHSA-1111-2222-3333", summary="word " * 200))

    title = sole(parse_osv_scanner(report(entry), 1)).title

    assert len(title) == scan_module.TITLE_LIMIT
    assert title.endswith("…")


def test_the_title_falls_back_to_the_details_past_any_heading_then_to_the_name() -> None:
    no_summary = package(
        "idna", group("GHSA-1111-2222-3333", summary="", details="### Impact\n\nFirst\nmore")
    )
    nothing = package("h11", group("GHSA-4444-5555-6666", summary="", details=""))

    titles = [a.title for a in parse_osv_scanner(report(no_summary, nothing), 1)]

    assert titles == ["First", "GHSA-4444-5555-6666"]


@pytest.mark.parametrize(
    ("output", "exit_status"),
    [
        ("", 1),
        ("failed to resolve path: stat uv.lock: no such file or directory", 127),
        # An unreachable OSV API exits 127 with a well-formed, empty report.
        (json.dumps({"results": []}), 127),
        # osv-scanner's "no packages found".
        (json.dumps({"results": []}), 128),
        (json.dumps({"results": []}), 1),
        (report(package("anyio", group("GHSA-1111-2222-3333"))), 0),
        (report(package("anyio", group("GHSA-1111-2222-3333"))), 127),
        ("[]", 0),
        ("null", 0),
        ('{"results": ["text"]}', 1),
        ('{"results": [{"packages": [{"package": {"name": "anyio"}}]}]}', 1),
        (('{"results": [{"packages": [{"package": {"name": "a", "version": "1"},'
          ' "vulnerabilities": [{"id": "GHSA-1111-2222-3333"}]}]}]}'), 1),
        # Records with no group to read them by: at exit 0 that would read as clean.
        (('{"results": [{"packages": [{"package": {"name": "a", "version": "1"},'
          ' "vulnerabilities": [{"id": "GHSA-1111-2222-3333"}]}]}]}'), 0),
        (('{"results": [{"packages": [{"package": {"name": "a", "version": "1"},'
          ' "groups": [{"ids": []}]}]}]}'), 1),
    ],
)  # fmt: skip
def test_osv_scanner_output_that_is_not_a_verdict_raises_never_reads_as_clean(
    output: str, exit_status: int
) -> None:
    with pytest.raises(ScanError):
        parse_osv_scanner(output, exit_status)


def test_a_package_forked_into_two_versions_is_one_finding_naming_both() -> None:
    older = package("anyio", group("GHSA-1111-2222-3333"))
    newer = package("anyio", group("GHSA-1111-2222-3333"))
    newer["package"]["version"] = "4.9.0"
    older["package"]["version"] = "4.1.0"

    advisory = sole(parse_osv_scanner(report(older, newer), 1))

    assert advisory.affected == "4.1.0, 4.9.0"


def test_one_problem_grouped_differently_per_fork_is_one_finding() -> None:
    older = package("idna", group("PYSEC-2026-0007", max_severity="7.5", rating=None))
    newer = package("idna", group("GHSA-1111-2222-3333", "PYSEC-2026-0007", rating="LOW"))
    older["package"]["version"] = "3.6"
    newer["package"]["version"] = "3.7"
    newer["vulnerabilities"][0]["summary"] = "The GitHub advisory's own title"

    advisory = sole(parse_osv_scanner(report(older, newer), 1))

    assert advisory.id == "GHSA-1111-2222-3333", "the better name, whichever fork carried it"
    assert advisory.title == "The GitHub advisory's own title"
    assert advisory.url == "https://github.com/advisories/GHSA-1111-2222-3333"
    assert advisory.affected == "3.6, 3.7"
    assert advisory.severity == "critical", "the GHSA record's group scores 9.3"


def test_findings_linked_only_through_a_third_fork_are_still_one_finding() -> None:
    first = package("anyio", group("GHSA-1111-2222-3333"))
    second = package("anyio", group("PYSEC-2026-0007"))
    third = package("anyio", group("GHSA-1111-2222-3333", "PYSEC-2026-0007"))
    for entry, version in ((first, "4.1.0"), (second, "4.2.0"), (third, "4.3.0")):
        entry["package"]["version"] = version

    advisory = sole(parse_osv_scanner(report(first, second, third), 1))

    assert set(advisory.affected.split(", ")) == {"4.1.0", "4.2.0", "4.3.0"}
    assert advisory.id == "GHSA-1111-2222-3333"


def test_a_merged_finding_keeps_a_real_title_over_a_bare_name() -> None:
    nameless = package("idna", group("GHSA-1111-2222-3333", summary="", details=""))
    titled = package("idna", group("PYSEC-2026-0007", "GHSA-1111-2222-3333", summary=""))
    titled["vulnerabilities"][0]["summary"] = "Path traversal in idna"
    titled["package"]["version"] = "3.7"

    assert sole(parse_osv_scanner(report(nameless, titled), 1)).title == "Path traversal in idna"


def test_every_name_of_the_problem_is_kept_as_an_alias() -> None:
    entry = package(
        "certifi",
        group("PYSEC-2026-4320", "GHSA-4444-5555-6666", aliases=("CVE-2026-4320",)),
    )

    advisory = sole(parse_osv_scanner(report(entry), 1))

    assert advisory.aliases == ("CVE-2026-4320", "GHSA-4444-5555-6666", "PYSEC-2026-4320")


def test_a_forked_finding_takes_its_most_severe_rating_and_every_name() -> None:
    older = package("anyio", group("GHSA-1111-2222-3333", max_severity="5.0", rating=None))
    newer = package(
        "anyio",
        group("GHSA-1111-2222-3333", "PYSEC-2026-0007", max_severity="9.8", rating=None),
    )
    older["package"]["version"] = "4.9.0"
    newer["package"]["version"] = "4.10.0"

    advisory = sole(parse_osv_scanner(report(older, newer), 1))

    assert advisory.severity == "critical", "the fork that pages decides"
    assert advisory.affected == "4.9.0, 4.10.0", "in osv-scanner's order, not string order"
    assert advisory.aliases == ("GHSA-1111-2222-3333", "PYSEC-2026-0007")


def test_two_advisories_on_one_package_are_two_findings() -> None:
    entry = package("anyio", group("GHSA-1111-2222-3333"), group("GHSA-4444-5555-6666"))

    keys = [a.key for a in parse_osv_scanner(report(entry), 1)]

    assert keys == ["pypi:anyio:GHSA-1111-2222-3333", "pypi:anyio:GHSA-4444-5555-6666"]


# --- what the scan does on GitHub ------------------------------------------------------


def test_a_clean_scan_with_nothing_open_does_nothing() -> None:
    scan = scan_of()

    assert scan.verdict == "clean"
    assert scan.plans([unrelated_issue()]) == []


def test_a_high_advisory_files_one_security_issue_that_pages() -> None:
    plan = sole(
        scan_of(pypi("httpx", "GHSA-1111-2222-3333", "high"),
                pypi("h11", "GHSA-4444-5555-6666", "low"))
        .plans([unrelated_issue()])
    )  # fmt: skip

    assert plan.action == "create"
    assert plan.title == ADVISORIES_TITLE
    assert plan.labels == ["needs-human"]
    assert plan.body is not None
    assert "**2 advisories**" in plan.body
    assert "1 High, 1 Low" in plan.body
    assert "0123abc" in plan.body
    assert plan.body.index("GHSA-1111-2222-3333") < plan.body.index("GHSA-4444-5555-6666"), (
        "most severe first"
    )


def test_a_critical_advisory_pages() -> None:
    plan = sole(scan_of(pypi("certifi", "GHSA-1111-2222-3333", "critical")).plans([]))

    assert plan.labels == ["needs-human"]


@pytest.mark.parametrize("severity", ["medium", "moderate", "low", "none", "unknown"])
def test_nothing_below_high_pages_it_goes_to_the_capitals_inbox(severity: str) -> None:
    plan = sole(scan_of(pypi("h11", "GHSA-4444-5555-6666", severity)).plans([]))

    assert plan.labels == ["needs-capital"]


def test_a_standing_finding_is_silent_no_second_issue_no_comment_no_label() -> None:
    scan = scan_of(pypi("httpx", "GHSA-1111-2222-3333", "high"))
    filed = sole(scan.plans([]))

    assert scan.plans([open_issue(filed)]) == []


def test_a_new_high_on_an_open_issue_updates_it_comments_and_pages() -> None:
    filed = sole(scan_of(pypi("h11", "GHSA-4444-5555-6666", "low")).plans([]))
    plan = sole(
        scan_of(pypi("h11", "GHSA-4444-5555-6666", "low"),
                pypi("httpx", "GHSA-1111-2222-3333", "high"))
        .plans([open_issue(filed)])
    )  # fmt: skip

    assert plan.action == "update"
    assert plan.number == 812
    assert plan.labels == ["needs-human"]
    assert plan.comment is not None and plan.body is not None
    assert "New Since the Last Scan (1 advisory: 1 High)" in plan.comment
    assert "GHSA-1111-2222-3333" in plan.comment
    assert "GHSA-4444-5555-6666" not in plan.comment
    assert "GHSA-4444-5555-6666" in plan.body


def test_a_new_low_on_an_issue_that_already_paged_does_not_page_again() -> None:
    filed = sole(scan_of(pypi("httpx", "GHSA-1111-2222-3333", "high")).plans([]))
    plan = sole(
        scan_of(pypi("httpx", "GHSA-1111-2222-3333", "high"),
                pypi("h11", "GHSA-4444-5555-6666", "low"))
        .plans([open_issue(filed)])
    )  # fmt: skip

    assert plan.labels == ["needs-capital"]


def test_an_advisory_rated_high_after_it_was_filed_lower_pages_then_and_is_rewritten() -> None:
    filed = sole(scan_of(pypi("httpx", "GHSA-1111-2222-3333", "unknown")).plans([]))
    assert filed.labels == ["needs-capital"]

    plan = sole(
        scan_of(pypi("httpx", "GHSA-1111-2222-3333", "critical")).plans([open_issue(filed)])
    )

    assert plan.action == "update"
    assert plan.labels == ["needs-human"]
    assert plan.comment is not None and plan.body is not None
    assert "**Re-Rated:** `pypi:httpx:GHSA-1111-2222-3333` Unknown → Critical" in plan.comment
    assert "| Critical |" in plan.body
    assert (
        scan_of(pypi("httpx", "GHSA-1111-2222-3333", "critical")).plans([open_issue(plan)]) == []
    ), "and is silent again after"


@pytest.mark.parametrize("severity", ["medium", "critical"])
def test_a_rerating_that_does_not_reach_high_rewrites_the_issue_and_labels_nothing(
    severity: str,
) -> None:
    filed = sole(scan_of(pypi("httpx", "GHSA-1111-2222-3333", "high")).plans([]))
    plan = sole(scan_of(pypi("httpx", "GHSA-1111-2222-3333", severity)).plans([open_issue(filed)]))

    assert plan.action == "update"
    assert plan.labels == [], f"High → {severity} is no new page"


def test_an_advisory_that_stops_being_reported_is_noted_and_nothing_is_labeled() -> None:
    filed = sole(
        scan_of(pypi("httpx", "GHSA-1111-2222-3333", "high"),
                pypi("h11", "GHSA-4444-5555-6666", "low"))
        .plans([])
    )  # fmt: skip
    plan = sole(scan_of(pypi("h11", "GHSA-4444-5555-6666", "low")).plans([open_issue(filed)]))

    assert plan.action == "update"
    assert plan.labels == []
    assert plan.comment is not None
    assert "No Longer Reported (1):** `pypi:httpx:GHSA-1111-2222-3333`" in plan.comment
    assert "is clean" not in plan.comment


def test_a_scan_that_comes_back_clean_says_so_once_and_never_closes_the_issue() -> None:
    filed = sole(scan_of(pypi("httpx", "GHSA-1111-2222-3333", "high")).plans([]))
    clean = scan_of()
    plan = sole(clean.plans([open_issue(filed)]))

    assert plan.action == "update"
    assert plan.labels == []
    assert plan.comment is not None
    assert "is clean" in plan.comment
    assert "Close this issue by hand" in plan.comment
    assert clean.plans([open_issue(plan)]) == [], "the next clean run is silent"


def test_a_problem_that_gains_a_github_name_is_renamed_on_the_issue_and_never_pages() -> None:
    filed = sole(scan_of(pypi("idna", "PYSEC-2026-0007", "high")).plans([]))
    renamed = pypi(
        "idna", "GHSA-1111-2222-3333", "high", aliases=("GHSA-1111-2222-3333", "PYSEC-2026-0007")
    )

    plan = sole(scan_of(renamed).plans([open_issue(filed)]))

    assert plan.action == "update"
    assert plan.labels == [], "no second page for one problem"
    assert plan.comment is not None and plan.body is not None
    assert (
        "**Known by Another Name Now:** `pypi:idna:PYSEC-2026-0007` is now "
        "`pypi:idna:GHSA-1111-2222-3333`" in plan.comment
    )
    assert "New Since" not in plan.comment and "No Longer Reported" not in plan.comment
    assert '"pypi:idna:GHSA-1111-2222-3333":"high"' in plan.body
    assert scan_of(renamed).plans([open_issue(plan)]) == [], "and silent once renamed"


def test_two_findings_filed_apart_and_linked_since_are_folded_never_reported_resolved() -> None:
    filed = sole(
        scan_of(pypi("idna", "GHSA-1111-2222-3333", "low"), pypi("idna", "PYSEC-2026-0007", "high"))
        .plans([])
    )  # fmt: skip
    linked = pypi(
        "idna", "GHSA-1111-2222-3333", "low", aliases=("GHSA-1111-2222-3333", "PYSEC-2026-0007")
    )

    plan = sole(scan_of(linked).plans([open_issue(filed)]))

    assert plan.labels == []
    assert plan.comment is not None and plan.body is not None
    assert "No Longer Reported" not in plan.comment
    # The surviving name carries the most severe rating either was filed at, so what paged
    # under one name is never news under the other.
    assert "**Re-Rated:** `pypi:idna:GHSA-1111-2222-3333` High → Low" in plan.comment
    assert "`pypi:idna:PYSEC-2026-0007` is now `pypi:idna:GHSA-1111-2222-3333`" in plan.comment
    assert "PYSEC-2026-0007" not in plan.body
    assert scan_of(linked).plans([open_issue(plan)]) == []


def test_a_fold_never_lowers_the_rating_a_finding_already_paged_at() -> None:
    filed = sole(
        scan_of(pypi("idna", "GHSA-1111-2222-3333", "high"), pypi("idna", "PYSEC-2026-0007", "low"))
        .plans([])
    )  # fmt: skip
    linked = pypi(
        "idna", "GHSA-1111-2222-3333", "high", aliases=("GHSA-1111-2222-3333", "PYSEC-2026-0007")
    )

    plan = sole(scan_of(linked).plans([open_issue(filed)]))

    assert plan.labels == [], "it paged as High already"
    assert plan.comment is not None
    assert "Re-Rated" not in plan.comment


def test_a_name_reported_as_its_own_finding_is_never_taken_as_an_earlier_name() -> None:
    filed = sole(scan_of(pypi("idna", "GHSA-1111-2222-3333", "low")).plans([]))
    own = pypi("idna", "GHSA-1111-2222-3333", "low")
    linked = pypi(
        "idna", "PYSEC-2026-0007", "low", aliases=("GHSA-1111-2222-3333", "PYSEC-2026-0007")
    )

    plan = sole(scan_of(own, linked).plans([open_issue(filed)]))

    assert plan.comment is not None
    assert "New Since the Last Scan (1 advisory: 1 Low)" in plan.comment
    assert "PYSEC-2026-0007" in plan.comment
    assert "Known by Another Name" not in plan.comment


def test_a_renamed_finding_that_is_rerated_reads_as_rerated_under_its_new_name() -> None:
    filed = sole(scan_of(pypi("idna", "PYSEC-2026-0007", "low")).plans([]))
    renamed = pypi(
        "idna",
        "GHSA-1111-2222-3333",
        "critical",
        aliases=("GHSA-1111-2222-3333", "PYSEC-2026-0007"),
    )

    plan = sole(scan_of(renamed).plans([open_issue(filed)]))

    assert plan.labels == ["needs-human"]
    assert plan.comment is not None and plan.body is not None
    assert "**Re-Rated:** `pypi:idna:GHSA-1111-2222-3333` Low → Critical" in plan.comment
    assert "No Longer Reported" not in plan.comment
    assert "PYSEC-2026-0007" not in plan.body


def test_an_issue_the_scan_did_not_write_is_never_read_or_touched() -> None:
    advisory = pypi("httpx", "GHSA-1111-2222-3333", "critical")
    # A stranger's issue with the scan's title and a marker saying it already knows.
    forged = open_issue(sole(scan_of(advisory).plans([])), author=STRANGER)
    forged_error = open_issue(sole(scan_of(errors={"pypi": "x"}).plans([])), author=STRANGER)
    forged_error["number"] = 813

    plans = scan_of(advisory).plans([forged, forged_error])

    assert [(p.action, p.number, p.labels) for p in plans] == [("create", None, ["needs-human"])]


def test_an_issue_with_no_author_is_not_the_scans() -> None:
    advisory = pypi("httpx", "GHSA-1111-2222-3333", "critical")
    issue = open_issue(sole(scan_of(advisory).plans([])))
    del issue["author"]

    assert sole(scan_of(advisory).plans([issue])).action == "create"


def test_an_issue_whose_marker_was_edited_away_is_treated_as_knowing_nothing() -> None:
    rewritten = {
        "number": 812,
        "title": ADVISORIES_TITLE,
        "body": "rewritten by hand",
        "author": SCAN_BOT,
    }
    plan = sole(scan_of(pypi("httpx", "GHSA-1111-2222-3333", "high")).plans([rewritten]))

    assert plan.action == "update"
    assert plan.labels == ["needs-human"]


def test_a_marker_edited_into_nonsense_is_read_for_what_it_still_says() -> None:
    edited = {
        "number": 812,
        "title": ADVISORIES_TITLE,
        "author": SCAN_BOT,
        "body": '<!-- dependency-security-scan {"advisories":'
        '{"pypi:httpx:GHSA-1111-2222-3333":["high"],"pypi:h11:GHSA-4444-5555-6666":"low"}} -->',
    }
    plan = sole(
        scan_of(pypi("httpx", "GHSA-1111-2222-3333", "high"),
                pypi("h11", "GHSA-4444-5555-6666", "low"))
        .plans([edited])
    )  # fmt: skip

    assert plan.labels == ["needs-human"], "the unreadable entry is new again"
    assert plan.comment is not None
    assert "New Since the Last Scan (1 advisory: 1 High)" in plan.comment


# --- a scan that could not run ---------------------------------------------------------


def test_a_scanner_that_reaches_no_verdict_files_one_issue_for_the_capital() -> None:
    scan = scan_of(errors={"pypi": "osv-scanner exited 127 with 0 advisories"})
    plan = sole(scan.plans([]))

    assert scan.verdict == "error"
    assert plan.action == "create"
    assert plan.title == ERROR_TITLE
    assert plan.labels == ["needs-capital"]
    assert plan.body is not None
    assert "Python package scan:** osv-scanner exited 127" in plan.body
    assert scan.plans([open_issue(plan)]) == [], "one issue while it cannot run, not one a day"


def test_a_second_scanner_failing_is_added_to_the_open_issue_and_the_capital_is_told_again() -> (
    None
):
    filed = sole(scan_of(errors={"pypi": "no verdict"}).plans([]))
    plan = sole(
        scan_of(errors={"pypi": "no verdict", "actions": "no readable report"}).plans(
            [open_issue(filed)]
        )
    )

    assert plan.action == "update"
    assert plan.labels == ["needs-capital"]
    assert plan.comment is not None and plan.body is not None
    assert "the actions scan and Python package scan" in plan.comment
    assert "actions scan:** no readable report" in plan.body


def test_the_run_that_reaches_a_verdict_again_closes_the_could_not_run_issue() -> None:
    filed = sole(scan_of(errors={"pypi": "no verdict"}).plans([]))
    plan = sole(scan_of().plans([open_issue(filed)]))

    assert plan.action == "close"
    assert plan.number == 812
    assert plan.comment is not None
    assert "the clock is running again" in plan.comment


def test_the_scanner_that_did_run_still_reports_beside_the_one_that_could_not() -> None:
    scan = scan_of(pypi("httpx", "GHSA-1111-2222-3333", "high"), errors={"actions": "no verdict"})

    assert scan.verdict == "error"
    assert [(plan.title, plan.labels) for plan in scan.plans([])] == [
        (ADVISORIES_TITLE, ["needs-human"]),
        (ERROR_TITLE, ["needs-capital"]),
    ]


def test_a_failed_scanners_earlier_findings_are_carried_forward_never_reported_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filed = sole(
        scan_of(pypi("httpx", "GHSA-1111-2222-3333", "high"),
                pypi("h11", "GHSA-4444-5555-6666", "low"))
        .plans([])
    )  # fmt: skip

    scan_down = scan_of(errors={"pypi": "osv-scanner exited 127 with 0 advisories"})
    assert [plan.title for plan in scan_down.plans([open_issue(filed)])] == [ERROR_TITLE]
    # The capital's version has two scanners and so tests one failing beside one finding;
    # this repository has one, so a finding beside a failure comes from another ecosystem.
    other = Advisory("actions", "checkout", "v6", "GHSA-7777-8888-9999", "high", "Bad", None)
    with_other = DependencySecurityScan(
        advisories=[other], errors={"pypi": "no verdict"}, **context()
    )
    monkeypatch.setitem(scan_module.ECOSYSTEM_NAMES, "actions", "GitHub Action")
    plan = with_other.plans([open_issue(filed)])[0]

    assert plan.labels == ["needs-human"]
    assert plan.body is not None and plan.comment is not None
    assert '"pypi:httpx:GHSA-1111-2222-3333":"high"' in plan.body, (
        "the carried finding stays in the marker"
    )
    assert "2 earlier findings are carried forward" in plan.body
    assert "No Longer Reported" not in plan.comment


# --- what it writes --------------------------------------------------------------------


def test_nothing_it_writes_can_mention_a_github_account() -> None:
    scan = scan_of(
        pypi("httpx", "GHSA-1111-2222-3333", "high",
             title="Bypass reported by @someone in @org/pkg"),
        errors={"pypi": "failed for @rails/thing"},
    )  # fmt: skip

    for plan in scan.plans([]):
        assert plan.body is not None
        bare = _without_code_spans(plan.body)
        assert "@" not in bare.replace("`@origin`", ""), plan.title


def test_a_change_comment_cannot_mention_a_github_account_either() -> None:
    filed = sole(scan_of(pypi("h11", "GHSA-4444-5555-6666", "low")).plans([]))
    plan = sole(
        scan_of(
            pypi("h11", "GHSA-4444-5555-6666", "low"),
            pypi("httpx", "GHSA-1111-2222-3333", "high", title="Found by @someone"),
        ).plans([open_issue(filed)])
    )

    assert plan.comment is not None
    assert "@" not in _without_code_spans(plan.comment)


def test_a_stray_backtick_upstream_cannot_free_a_mention() -> None:
    # One upstream backtick would pair with the span opened around "@evil" and leave the
    # name bare. Every backtick left in the cell must be one this scan put around an @name.
    # Ported from basecradle-harness#818, whose review found it.
    title = "Fix in `parse() reported by @evil and @org/pkg"
    plan = sole(scan_of(pypi("httpx", "GHSA-1111-2222-3333", "high", title=title)).plans([]))

    assert plan.body is not None
    row = next(line for line in plan.body.splitlines() if "GHSA-1111-2222-3333" in line)
    title_cell = row.split(" | ")[-1]
    assert title_cell == "Fix in parse() reported by `@evil` and `@org/pkg` |"
    assert re.sub(r"`@[\w\-/.]+`", "", title_cell).count("`") == 0


def test_upstream_text_cannot_break_the_table_or_pose_as_the_marker() -> None:
    hostile = 'Header a | b\nsplits <!-- dependency-security-scan {"advisories":{}} -->'
    scan = scan_of(
        pypi("h11", "GHSA-4444-5555-6666", "low", title=hostile),
        pypi("httpx", "GHSA-1111-2222-3333", "high"),
    )
    plan = sole(scan.plans([]))

    assert plan.body is not None
    assert "Header a \\| b splits &lt;!--" in plan.body
    assert scan.plans([open_issue(plan)]) == [], "the real marker is the one read back"
    rows = [line for line in plan.body.splitlines() if line.startswith(("| High ", "| Low "))]
    assert len(rows) == 2
    for row in rows:
        assert len([c for i, c in enumerate(row) if c == "|" and row[i - 1] != "\\"]) == 7, row


def test_the_real_marker_which_closes_the_body_is_the_one_read() -> None:
    filed = sole(scan_of(pypi("httpx", "GHSA-1111-2222-3333", "high")).plans([]))
    assert filed.body is not None
    # Anything that ever reached the body above the real marker (a hand edit, a quoted
    # earlier body) is not what this scan wrote last, so it is not what it compares with.
    posed = '<!-- dependency-security-scan {"advisories":{}} -->\n\n' + filed.body

    assert scan_module._state_in(posed) == {
        "advisories": {"pypi:httpx:GHSA-1111-2222-3333": "high"}
    }
    assert (
        scan_of(pypi("httpx", "GHSA-1111-2222-3333", "high")).plans(
            [{"number": 812, "title": ADVISORIES_TITLE, "body": posed, "author": SCAN_BOT}]
        )
        == []
    ), "a standing finding stays silent"


def test_the_marker_cannot_close_its_own_comment() -> None:
    marker = scan_module._marker({"advisories": {"pypi:x:-->": "high"}})

    assert marker.count("-->") == 1
    assert marker.endswith(" -->")
    assert scan_module._state_in(marker) == {"advisories": {"pypi:x:-->": "high"}}


# --- the plan the workflow applies -----------------------------------------------------


def test_main_writes_the_verdict_and_the_plans_the_workflow_applies(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    advisory = pypi("certifi", "GHSA-1111-2222-3333", "critical")
    monkeypatch.setitem(scan_module.SCANNERS, "pypi", lambda root: [advisory])
    open_issues = tmp_path / "open.json"
    open_issues.write_text("[]")
    plan_file = tmp_path / "plan.json"

    assert scan_module.main(["--open-issues", str(open_issues), "--plan", str(plan_file)]) == 0

    written = json.loads(plan_file.read_text())
    assert written["verdict"] == "advisories"
    assert [(p["action"], p["title"], p["labels"]) for p in written["plans"]] == [
        ("create", ADVISORIES_TITLE, ["needs-human"])
    ]
    assert set(written["plans"][0]) == {"action", "number", "title", "body", "comment", "labels"}


def test_main_files_a_scanner_that_crashed_never_loses_it_in_a_red_run(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(root: Any) -> list[Advisory]:
        raise FileNotFoundError("osv-scanner")

    monkeypatch.setitem(scan_module.SCANNERS, "pypi", crash)
    open_issues = tmp_path / "open.json"
    open_issues.write_text("[]")
    plan_file = tmp_path / "plan.json"

    scan_module.main(["--open-issues", str(open_issues), "--plan", str(plan_file)])

    written = json.loads(plan_file.read_text())
    assert written["verdict"] == "error"
    assert [p["title"] for p in written["plans"]] == [ERROR_TITLE]
    assert "FileNotFoundError: osv-scanner" in written["plans"][0]["body"]


# --- where it runs ----------------------------------------------------------------------


def test_the_scanner_runs_where_the_lock_is() -> None:
    # The script sits two directories below the root; a root one level off would scan
    # nothing, and osv-scanner's "no lockfile" is a ScanError, never a clean day.
    assert scan_module.ROOT == REPO
    assert (scan_module.ROOT / "uv.lock").is_file()
    assert "uv.lock" in scan_module.OSV_SCANNER


def test_the_script_runs_on_the_standard_library_alone() -> None:
    # The workflow runs it with the runner's own python3 and installs nothing first.
    imported = re.findall(r"^(?:from|import) (\w+)", SCRIPT.read_text(), flags=re.MULTILINE)

    assert set(imported) <= set(sys.stdlib_module_names) | {"__future__"}, imported


# --- the workflow's half of the contract --------------------------------------------------
#
# The script decides; the workflow feeds it and applies it. These are the lines of the
# workflow the decisions above silently depend on, each one a page lost or a page doubled if
# it drifts, with every test here still green.

WORKFLOW = REPO / ".github" / "workflows" / "dependency-security-scan.yml"


def _step(name: str) -> str:
    text = WORKFLOW.read_text()
    start = text.index(f"      - name: {name}\n")
    following = text.find("\n      - name: ", start + 1)
    return text[start : following if following != -1 else len(text)]


def test_the_workflow_reads_only_the_scans_own_issues_with_their_authors() -> None:
    read = _step("Read the Open Issues")

    # Without `author`, every issue reads as a stranger's: a new issue and a page every day.
    assert "--json number,title,body,author" in read
    # The server-side filter keeps a flood of strangers' issues from pushing the scan's own
    # past the limit; the script's own check (SCAN_AUTHOR) still stands behind it.
    assert "--author 'basecradle-ai[bot]'" in read
    assert scan_module.SCAN_AUTHOR == "app/basecradle-ai"


def test_every_github_write_is_the_basecradle_ai_apps_never_github_token() -> None:
    # A label GITHUB_TOKEN applies starts no workflow, so needs-human would page nobody.
    for name in ("Read the Open Issues", "File What the Scan Found"):
        assert "GH_TOKEN: ${{ steps.app-token.outputs.token }}" in _step(name), name
    text = WORKFLOW.read_text()
    assert "secrets.GITHUB_TOKEN" not in text and "github.token" not in text
    assert "permission-issues: write" in _step("Mint the basecradle-ai App Token")


def test_the_workflow_runs_on_its_clock_and_by_hand_never_on_a_pull_request() -> None:
    # It reads repository secrets: a PR's run would see them empty, a fork's must never.
    triggers = WORKFLOW.read_text().split("\non:\n")[1].split("\npermissions:")[0]

    assert "schedule:" in triggers and "workflow_dispatch:" in triggers
    assert "pull_request" not in triggers and "push:" not in triggers


def test_the_heartbeat_is_withheld_when_a_scanner_reached_no_verdict() -> None:
    ping = _step("Ping the basecradle-router Dependency Scan Heartbeat")

    assert '= "error" ]' in ping and "withheld" in ping
    assert 'test -n "$HEARTBEAT_URL"' in ping, "a missing secret fails loudly"

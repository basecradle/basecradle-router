"""The standing differential (#516): the box's scrub and the print site against the harness's.

The generator, the three readings and the table live in `tests/scrub_differential.py`, which also
runs deeper by hand. Here CI reads one seed of it and holds:

* every secret the harness takes, the box and the print site take too, unless a row of the table
  names the text's shape (`STATED`);
* each host's `ai_scrub` leaves nothing its own at the previous commit took (no table: any
  regression fails), and the print site nothing its previous commit's took, unless a
  `print-history` row states the shape;
* every row still explains a difference, so a closed one is removed rather than left to excuse
  more than it should, and every row anchored in `deploy/README.md` names a phrase it carries;
* the harness is the one CI fetched, its bytes verified, and the regression check bites.

Vector is required in CI and a skip elsewhere, as in `tests/test_vector_unit.py`; so is the
harness's wheel (`HARNESS_WHEEL`) and an interpreter that runs it (`HARNESS_PYTHON`, 3.11 or later).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import scrub_block
import scrub_differential as sd
import test_vector_unit as tvu
from basecradle_router.logfmt import redact_credentials


def _harness_or_skip() -> None:
    if sd.harness_python() and sd.harness_wheel():
        return
    assert not os.environ.get("CI"), "the harness's wheel is missing on the CI runner"
    pytest.skip("set HARNESS_WHEEL and HARNESS_PYTHON to run the harness's scrub")


@pytest.fixture(scope="module")
def vector() -> str:
    binary = tvu._vector()
    _harness_or_skip()
    return binary


@pytest.fixture(scope="module")
def corpus() -> list[str]:
    """What CI reads: one seed of the generator, and the rounds' reproductions."""
    return sd.texts(sd.SEED, sd.LINES) + sd.REPRODUCTIONS


def test_ci_fetches_the_pinned_harness() -> None:
    # The table is only as good as the harness it reads: CI must fetch the release pinned here,
    # by the same checksum `tests/harness_scrub.py` verifies.
    workflow = tvu.CI_WORKFLOW.read_text()
    assert f'HARNESS_VERSION: "{sd.HARNESS_VERSION}"' in workflow
    assert f'HARNESS_WHEEL_SHA256: "{sd.HARNESS_WHEEL_SHA256}"' in workflow


def test_the_harness_runs_only_from_the_pinned_bytes(tmp_path: Path) -> None:
    _harness_or_skip()
    got = subprocess.run(
        [sd.harness_python(), "-I", str(sd.REPO / "tests" / "harness_scrub.py")]
        + [sd.harness_wheel(), "0" * 64],
        input="[]",
        capture_output=True,
        text=True,
        check=False,
    )
    assert got.returncode != 0
    assert "not the pinned" in got.stderr


def test_every_text_carries_the_secret_once() -> None:
    # Once, so a row reads the copy that leaked: there is no other.
    texts = sd.texts(sd.SEED, 2000)
    assert all(text.count(sd.SECRET) == 1 for text in texts)
    assert len(set(texts)) > 1900
    # The secret is told apart from every piece, so a leak is the secret's and nothing else's.
    assert not sd.leaks("".join(sd._ALL_PIECES))


def test_the_box_and_the_print_site_leave_nothing_the_harness_takes(
    vector: str, corpus: list[str]
) -> None:
    # The property the differential exists for: a text the harness redacts and the box or the
    # print site ships fails here, with the text, unless a row of the table names it.
    box, print_site = sd.differences(vector, corpus)
    unexplained = box.get(None, []) + print_site.get(None, [])
    assert not unexplained, unexplained[:5]
    # It reads something: the table explains differences, it does not stand in for them.
    assert sum(len(rows) for rows in box.values()) > 1000


def test_nothing_the_previous_commit_took_is_left(
    vector: str, corpus: list[str], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # The bar #510's three rounds were held to: against the previous commit's ai_scrub (on a
    # pull request, the base branch's), any secret it took and the tree's leaves fails, with no
    # table. Nothing to read when the two are the same bytes.
    base = sd.base_ref()
    known = subprocess.run(["git", "rev-parse", "--verify", f"{base}^{{commit}}"], cwd=sd.REPO)
    if known.returncode != 0:
        assert not os.environ.get("CI"), f"{base} is not in the CI clone"
        pytest.skip(f"{base} is not in this clone")
    before = sd.host_sources(base)
    found, excused = sd.by_order(
        vector, before, sd.regressions(vector, before, sd.host_sources(), corpus), tmp_path
    )
    assert not found, found[:5]
    # The one excused row is counted, never silent (decided on #516): a secret the previous
    # sequential pass took only by the order of its rules.
    with capsys.disabled():
        print(
            f"\nexcused by order against {base}: {len({row[1] for row in excused})}"
            f" of {len(corpus)} texts"
        )


def test_the_regression_check_bites(vector: str) -> None:
    # Proven, not assumed: one host's ai_scrub with its bearer rule taken out is a regression
    # against its own, the check names that host and no other, and the tree against itself is
    # none.
    sources = sd.host_sources()
    host = sorted(sources)[-1]
    source = sources[host]
    assert source.count(scrub_block.BEARER) == 1 and source.count(scrub_block.BEARER_WORDS) == 1
    never = r"[^\s\S]"
    weakened = {
        **sources,
        host: source.replace(scrub_block.BEARER, never).replace(scrub_block.BEARER_WORDS, never),
    }
    lines = sd.texts(sd.SEED, 2000)
    found = sd.regressions(vector, sources, weakened, lines)
    assert found and {row[0] for row in found} == {host}
    assert not sd.regressions(vector, sources, sources, lines)


def test_nothing_the_previous_commits_print_site_took_is_left(corpus: list[str]) -> None:
    # The same bar for the print site (the capital, 2026-11-19 on #516), unless a print-history
    # row states the shape. Pure Python: no Vector, no harness.
    base = sd.base_ref()
    known = subprocess.run(["git", "rev-parse", "--verify", f"{base}^{{commit}}"], cwd=sd.REPO)
    if known.returncode != 0:
        assert not os.environ.get("CI"), f"{base} is not in the CI clone"
        pytest.skip(f"{base} is not in this clone")
    found = sd.print_regressions(sd.print_site_at(base), corpus)
    assert not found.get(None), found.get(None, [])[:5]


def test_the_print_site_check_bites_and_its_rows_hold(corpus: list[str]) -> None:
    # Proven on the change it exists for: against the print site before #514, the check finds
    # #514's regressions, and every one is the shape its row states; and each print-history row's
    # example is one its commit took and the tree leaves, explained by that row.
    found = sd.print_regressions(sd.print_site_at("ac3fe8a"), corpus)
    assert sum(len(rows) for rows in found.values()) > 0
    assert not found.get(None), found.get(None, [])[:5]
    assert not sd.print_regressions(
        (sd.REPO / "src/basecradle_router/logfmt.py").read_text(), corpus
    )
    for row in sd.STATED:
        if row.reading != "print-history":
            continue
        assert row.since, row.name
        old = sd._redactor(sd.print_site_at(row.since))
        assert not sd.leaks(old(row.example)) and sd.leaks(redact_credentials(row.example))
        assert sd.explained(row.reading, row.example, sd.Facts()) is row, row.name


def test_a_commit_with_no_host_is_refused() -> None:
    with pytest.raises(ValueError, match="holds no host"):
        sd.host_sources(git_empty_tree())


def git_empty_tree() -> str:
    """Git's empty tree, which every repository can name and which holds no file."""
    return subprocess.run(
        ["git", "hash-object", "-t", "tree", "/dev/null"],
        cwd=sd.REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_every_row_still_explains_its_example(vector: str) -> None:
    # A row stays only while its difference does: its example must still be one the harness takes
    # and the reading leaves, and the row must be the one that explains it. A row whose difference
    # is closed fails here until it is removed, so the table never excuses more than it should.
    # Read on every host's scrub: the example must differ on at least one, and wherever it
    # differs this row must be the one that explains it.
    stated = [row for row in sd.STATED if row.reading != "print-history"]
    examples = [row.example for row in stated]
    assert all(example.count(sd.SECRET) == 1 for example in examples)
    harness, harness_credentials = sd.harness_readings(examples)
    rules = sd.harness_rules(examples)
    readings = [
        (sd.box_readings(vector, source, examples), sd.taken_apart(vector, source, examples))
        for source in sd.scrub_sources()
    ]
    for i, row in enumerate(stated):
        if row.reading == "print":
            seen = [(redact_credentials(row.example), harness_credentials[i], False)]
        else:
            seen = [(box[i], harness[i], apart[i]) for box, apart in readings]
        differing = [
            (got, whole) for got, ref, whole in seen if sd.leaks(got) and not sd.leaks(ref)
        ]
        assert differing, (row.name, seen)
        for _, whole in differing:
            facts = sd.Facts(taken_apart=whole, harness_rules=rules[i])
            assert sd.explained(row.reading, row.example, facts) is row, row.name


def test_every_row_is_written_down() -> None:
    # Each row names where its difference is written down: a phrase deploy/README.md carries, or
    # this issue for a finding reported on it.
    readme = re.sub(r"\s+", " ", tvu.README.read_text())
    names = [(row.reading, row.name) for row in sd.STATED]
    assert len(names) == len(set(names))
    for row in sd.STATED:
        assert row.reading in ("box", "print", "print-history"), row.name
        assert row.reason and row.anchor, row.name
        quoted = re.findall(r"“([^”]+)”", row.anchor)
        assert quoted or "basecradle-router#516" in row.anchor, row.name
        for phrase in quoted:
            assert phrase in readme, (row.name, phrase)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__]))

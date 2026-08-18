"""The log-grammar probe driven offline — no journald, no subprocess, no network.

`breaker_tripped` powers the fleet's *Circuit Breaker Tripped* alarm, and every clause in
its pattern is a whole line that exists only when a breaker trips. Nothing arrives on a
healthy fleet, so the NOC's extraction guard has nothing to watch and a rename would take
the alarm silently to zero (basecradle-router#232, basecradle-noc#509). The probe closes
that by firing a real trip through the daemon's own render statement.

What these tests pin is the pair of properties that make the proof honest: the probe has
**no renderer of its own**, and the bytes it asserts are the bytes it writes. They also pin
the reach the capital ruled on 2026-08-18 (basecradle-router#234): the verdict is about the
line being **rendered and accepted**, never about its **landing** — the daemon is a system
user that structurally cannot read the journal back, and the NOC's witness owns that half.
The journald write seam is injected, so nothing here shells out.
Test cast: the synthetic key is the reserved literal ``probe`` — no agent is named for a
route, which is why it cannot collide with a real slug.
"""

import logging

import pytest

from basecradle_router import log_grammar as grammar_mod
from basecradle_router.claims import _log_grammar_claim
from basecradle_router.log_grammar import (
    BROKEN,
    IDENTIFIER,
    LINE_CLASS,
    PROVEN,
    SOURCE,
    LogGrammarError,
    LogGrammarProbe,
    capture_trip,
    manifest_detail,
    render_trip,
    trip_message,
)


class _Journal:
    """A stand-in for journald: the lines it accepted, and nothing to read them back.

    Deliberately write-only, because the real one is: a uid-999 system user may hand
    journald an entry and may not read its own entry back (basecradle-router#234). A test
    double with a reader would let a read-back grow back here without a single test
    failing.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)


def _probe(journal: _Journal) -> LogGrammarProbe:
    return LogGrammarProbe(writer=journal.write)


# --- the happy path, and what it actually demonstrates ----------------------


def test_a_line_journald_accepts_is_proven_without_reading_anything_back() -> None:
    # The whole verdict, from a write-only journal. On the box that is the only journal
    # this principal has: `router` is uid 999, and journald's SplitMode=uid gives a
    # readable per-user journal to uid >= 1000 only, so a read-back answered `unprovable`
    # on a healthy box while the line itself landed, shipped and extracted.
    journal = _Journal()
    result = _probe(journal).run()

    assert result.status == PROVEN
    assert result.exit_code == 0
    assert result.line_class == LINE_CLASS
    assert result.identifier == IDENTIFIER
    assert journal.lines == [result.rendered]


def test_the_verdict_says_out_loud_which_half_it_does_not_cover() -> None:
    # A green row that reads as "the alarm's line is in the journal" would be the
    # instrument overstating itself — the exact green-while-absent shape this arc exists
    # to close. The detail an operator reads names the landing half as the NOC's.
    result = _probe(_Journal()).run()

    assert "cannot read the journal back" in result.detail
    assert "NOC" in result.detail


def test_the_bytes_asserted_are_the_bytes_written() -> None:
    # The probe's whole claim is about specific bytes, so the line it checks and the line
    # it puts in the journal must be one string rather than two renderings that happen to
    # agree today. Two renderings is how a probe comes to prove a line nobody writes.
    journal = _Journal()
    result = _probe(journal).run()

    assert journal.lines == [result.rendered]
    assert result.grammar in result.rendered
    assert result.rendered.endswith(f"source={SOURCE}")


# --- the failure arms, each a different true statement ----------------------


def test_a_grammar_the_manifest_no_longer_matches_is_broken(monkeypatch) -> None:
    # The declaration<->reality tie. This is `breaker.py` renaming the token without
    # `claims.py` following — the drift that lets a manifest keep describing a line the
    # daemon stopped writing, which is the whole reason this claim exists.
    monkeypatch.setattr(grammar_mod, "TRIP_EVENT", "event=breaker_detonated")
    journal = _Journal()
    result = _probe(journal).run()

    assert result.status == BROKEN
    assert result.exit_code == 1
    assert "does not carry the declared grammar" in result.detail
    # And the bytes we already judged wrong never reached the fleet's journal: the write
    # is downstream of the verdict, so a broken probe is silent rather than noisy.
    assert journal.lines == []


def test_a_stamp_that_stops_trailing_the_grammar_is_refused() -> None:
    # The synthetic<->genuine tie, checked at the one place that can see it. If the stamp
    # ever stops trailing, the synthetic is no longer a prefix-extension of a real trip
    # and a re-point could pass here while going dark on the real thing.
    #
    # Driven against the checker rather than through a monkeypatched constant, because
    # the stamp's POSITION is decided by `breaker.py`'s field order — a patched constant
    # moves the render and the assertion together and proves nothing.
    probe = _probe(_Journal())
    genuine = trip_message(synthetic=False)

    interleaved = genuine.replace("agent=probe", f"source={SOURCE} agent=probe")
    assert "does not trail the line" in (probe._grammar_fault(interleaved) or "")


def test_a_synthetic_that_is_not_the_genuine_line_is_refused() -> None:
    # Ends with the stamp and is still not a prefix-extension: a field the synthetic
    # renders differently from a real trip. The probe would otherwise prove a pattern
    # production traffic never satisfies.
    probe = _probe(_Journal())
    mutated = f"{trip_message(synthetic=False).replace('count=21', 'count=99')} source={SOURCE}"

    assert "prefix-extension" in (probe._grammar_fault(mutated) or "")


def test_journald_tooling_that_does_not_answer_is_unprovable() -> None:
    # "We could not ask" is never quieted and never mistaken for a verdict about the
    # grammar — the contract's one inconclusive sentinel, straight through.
    def refuse(_line: str) -> None:
        raise LogGrammarError("could not run systemd-cat: no such file")

    probe = LogGrammarProbe(writer=refuse)
    with pytest.raises(LogGrammarError):
        probe.run()


# --- the probe owns no renderer --------------------------------------------


def test_the_synthetic_is_the_genuine_line_plus_one_trailing_token() -> None:
    assert trip_message(synthetic=True) == f"{trip_message(synthetic=False)} source={SOURCE}"


def test_one_trip_emits_exactly_one_line() -> None:
    # The probe asserts a whole line and writes a whole line; a trip that emitted two
    # would make its verdict about only one of them, and split the alarm's needle in the
    # journal besides.
    assert "\n" not in render_trip(synthetic=True)


def test_capturing_a_trip_leaves_the_breaker_logger_as_it_found_it() -> None:
    # The probe runs in the same process as the emitter it exercises. A logger left with
    # `propagate` off, or at a level it did not start at, is an instrument that broke the
    # thing it instruments.
    logger = logging.getLogger("basecradle_router.breaker")
    before = (logger.level, logger.propagate, list(logger.handlers))

    capture_trip(synthetic=True)

    assert (logger.level, logger.propagate, list(logger.handlers)) == before


def test_rendering_a_trip_inside_the_daemon_is_refused() -> None:
    # Capturing mutes the breaker's logger for the duration. In the CLI that is harmless
    # and necessary; in the daemon it would swallow a REAL trip — the loudest line the
    # router can emit, lost to the instrument meant to protect it. Every caller today is
    # the CLI, in its own process, so this guard is the difference between a constraint
    # that is written down and one that holds.
    package = logging.getLogger(grammar_mod.PACKAGE_LOGGER)
    handler = logging.NullHandler()
    handler._basecradle_router = True  # what configure_logging() tags the daemon's with
    package.addHandler(handler)
    try:
        with pytest.raises(LogGrammarError, match="inside the daemon"):
            capture_trip(synthetic=True)
    finally:
        package.removeHandler(handler)


def test_the_probe_never_touches_the_daemons_own_breaker() -> None:
    # The instance is local to the call, so no real window moves and no real wake is
    # refused. Two runs are therefore identical rather than cumulative — the second does
    # not arrive to find the first's window already full.
    assert trip_message(synthetic=True) == trip_message(synthetic=True)


# --- the manifest cannot describe a line the daemon does not write ----------


def test_the_manifest_publishes_the_bytes_this_build_actually_renders() -> None:
    detail = manifest_detail()

    assert detail["rendered"] == trip_message(synthetic=True)
    assert detail["grammar"] in detail["rendered"]
    assert detail["discriminator"] == f"source={SOURCE}"
    assert detail["genuine_level"] == "ERROR"
    assert detail["synthetic_level"] == "INFO"


def test_the_manifest_states_the_claims_reach_rather_than_leaving_it_to_lore() -> None:
    # The ledger row is read by people who were not here for the ruling. `proves` is how
    # a reader learns that green means the line was RENDERED, and that its landing is the
    # NOC's witness on the same identifier — not a second, weaker spelling of it.
    assert manifest_detail()["proves"] == "rendered"


def test_the_published_line_omits_the_timestamped_envelope() -> None:
    # The manifest is re-emitted on every claims pass. Publishing the formatted line
    # would make it churn on the clock, and a manifest that changes every pass is one
    # nobody can diff. The envelope is published as a format string instead.
    detail = manifest_detail()

    line = render_trip(synthetic=True)
    assert detail["rendered"] == trip_message(synthetic=True)
    assert line.endswith(detail["rendered"]) and line != detail["rendered"]
    assert "%(asctime)s" in detail["envelope"]


def test_the_claims_row_is_a_probe_kind_rare_claim_naming_its_own_cli() -> None:
    claim = _log_grammar_claim("/opt/basecradle-router/app/deploy/bin/router-admin")

    assert claim["claim"] == f"log-grammar:{LINE_CLASS}"
    assert claim["class"] == "rare"
    assert claim["prove"]["kind"] == "probe"
    # `evidence` names the journal, never a `<path>#<field>` pointer: a probe claim is
    # proven by RUNNING, and re-reading a pointer is the one thing that cannot move a
    # needle (basecradle-noc#421).
    assert "#" not in claim["evidence"]
    assert claim["evidence"] == f"journal:{IDENTIFIER}"
    assert claim["ttl_hours"] == 1
    # The cmd crosses to the box as an argv the wrapper splits on whitespace, so the line
    # class rides as its own token rather than inside a flag value.
    assert claim["prove"]["cmd"].split()[1:] == ["probe", "log-grammar", LINE_CLASS, "--json"]

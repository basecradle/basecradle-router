"""The router's log grammar: the one `key=value` renderer and the fleet ANSI palette.

Two properties are pinned here because both are invisible until they are wrong. The
renderer's contract (#170, #222) is what keeps a free-text error from decaying into
bare tokens a query cannot address. The palette's contract (#228) is the
**token-integrity rule**: a colour wraps a whole `key=value` token and never lands
between the key and its value, because every Live Tail search, `journalctl | grep`,
and ClickHouse `match()` in the fleet is a substring search over these bytes.

No model, agent, or network — this is pure string rendering.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from basecradle_router.logfmt import (
    BLUE,
    GREEN,
    PALETTE,
    RED,
    RESET,
    YELLOW,
    log_fields,
    paint,
)
from basecradle_router.routes.base import DeliveryDecision, log_delivery_decision

#: Any ANSI SGR sequence — used to read a painted line the way a colour-blind
#: consumer (a grep, an extraction regex) reads it.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

#: The Operator's Choice tier — neither success nor failure, an outcome an operator
#: chose — as basecradle-harness `docs/telemetry.md` → The Palette names the router's
#: members of it (basecradle-harness#799). It shares the warning tier's yellow on
#: purpose, and is never red, because nothing failed.
_OPERATORS_CHOICE = {"event=wake_stopped", "outcome=stopped"}

#: The palette as @origin decided it on 2026-08-17, transcribed from the decision
#: rather than from the implementation, so a drift in either direction fails here.
#: The families: success, the `wake_end` bookend's own identity, the warning tier
#: (neither success nor failure) with the Operator's Choice tier on the same SGR, and
#: failure.
_DECIDED = {
    GREEN: {"event=wake_start", "outcome=ok", "event=breaker_reset"},
    BLUE: {"event=wake_end"},
    YELLOW: {"event=wake_retry", "event=wake_refused", "event=wake_lock_stale"} | _OPERATORS_CHOICE,
    RED: {
        "outcome=failed",
        "outcome=rejected",
        "event=breaker_tripped",
        "event=wake_lock_unreadable",
    },
}


# --- the renderer ----------------------------------------------------------


def test_log_fields_renders_key_value_dropping_empties_and_quoting_spaces() -> None:
    # The one renderer behind every line the router logs. `exit=0` must survive
    # (0 is a value, not an absence) and an error message must stay ONE field, or a
    # grep for the key after it would silently miss.
    assert log_fields(agent="nova", exit=0) == "agent=nova exit=0"
    assert log_fields(agent="nova", error=None, reason="") == "agent=nova"
    assert log_fields(error="wake of x exited 1: boom") == 'error="wake of x exited 1: boom"'
    # A newline in a value cannot break the line into two.
    assert "\n" not in log_fields(error="line one\nline two")


#: The value the NOC's colour corpus pins as `router.stage_error_with_an_inner_quote_RESIDUAL`
#: (basecradle-noc `tests/fixtures/colour-corpus-2026-08-18.json`): an upstream body quoting
#: a `"`, followed by text shaped like the line's own tokens.
_RESIDUAL_ERROR = 'upstream said " stage=wake outcome=failed duration=99999.0s'

#: A fabricated, well-formed UUIDv7 delivery id.
_DELIVERY = "019a4b3c-5d6e-7f80-9a1b-2c3d4e5f6a7b"


def _outside_quotes(line: str) -> list[str]:
    """The tokens a fleet consumer reads as the line's own: those preceded by an even
    number of ``"`` — the NOC's anchoring rule (basecradle-noc#1217), applied literally."""
    return [
        match.group()
        for match in re.finditer(r"\S+", line)
        if line[: match.start()].count('"') % 2 == 0
    ]


def test_an_inner_double_quote_becomes_an_apostrophe_so_no_value_can_forge_a_token() -> None:
    # #396: `json.dumps` wrote `\"`, which is still a `"` to the quote count, so the
    # count flipped inside the value and `outcome=failed duration=99999.0s` read as the
    # line's own. The fleet's one value rule never escapes a quote: it swaps it.
    rendered = log_fields(delivery=_DELIVERY, error=_RESIDUAL_ERROR)
    assert rendered == (
        f'delivery={_DELIVERY} error="upstream said \' stage=wake outcome=failed duration=99999.0s"'
    )
    assert "\\" not in rendered
    assert rendered.count('"') == 2
    line = f"stage=verify outcome=failed {rendered}"
    # The line's own tokens, and nothing the value carried.
    assert _outside_quotes(line) == [
        "stage=verify",
        "outcome=failed",
        f"delivery={_DELIVERY}",
        'error="upstream',
    ]


def test_the_value_rule_treats_apostrophes_newlines_and_backslashes_as_the_harness_does() -> None:
    # basecradle-harness `_observability._value`: flatten whitespace to single spaces,
    # quote on a space, an `=` or a `"`, swap an inner `"` for `'`, and leave the rest
    # alone — an apostrophe and a backslash are ordinary characters, never escaped.
    assert log_fields(error="it's done") == 'error="it\'s done"'
    assert log_fields(error="it's") == "error=it's"
    assert log_fields(error="line one\nline two\r\n\tthree ") == 'error="line one line two three"'
    assert log_fields(path="C:\\tmp\\x") == "path=C:\\tmp\\x"
    assert log_fields(error="C:\\tmp\\x failed") == 'error="C:\\tmp\\x failed"'
    assert log_fields(error='a "quoted" word') == "error=\"a 'quoted' word\""
    assert log_fields(error='"') == 'error="\'"'
    # An `=` alone quotes the value too, or `a=b` would read as a second field.
    assert log_fields(error="a=b") == 'error="a=b"'
    # A value that flattens to nothing is dropped like `""`, never logged as `key=`.
    assert log_fields(agent="nova", error=" \n\t") == "agent=nova"


def test_a_control_character_in_a_value_is_a_space_never_a_raw_escape() -> None:
    # The router's `detail=` remap turns every `\p{Cc}` into a space, so this renderer
    # does too: an escape inside a value would paint a stretch of the line, and the
    # token-integrity rule says colour reaches a line only through `paint`.
    assert log_fields(error="upstream\x1b[31m red\x00\x7f") == 'error="upstream [31m red"'
    assert "\x1b" not in log_fields(error="\x1b[32moutcome=ok\x1b[0m")


def _vrl_string(literal: str) -> str:
    """A VRL double-quoted string literal's text, its escapes resolved (``\\u{1b}`` included)."""

    def resolve(match: re.Match[str]) -> str:
        if match[1]:
            return chr(int(match[1], 16))
        return {"n": "\n", "t": "\t", "r": "\r"}.get(match[2], match[2])

    return re.sub(r"\\(?:u\{([0-9A-Fa-f]+)\}|(.))", resolve, literal)


def test_log_fields_and_the_detail_remap_render_the_same_bytes() -> None:
    # Two renderers of one rule drift unless one test reads both. The remap's case runs
    # through the shipped VRL in tests/test_vector_unit.py; this reads that case's
    # input and expected `detail=` back out of the fixture and renders the input here.
    fixture = (Path(__file__).parent / "vector" / "ai_otel_llm.yaml").read_text()
    name = (
        "a detail renders by the fleet's one value rule, byte for byte with the router's log_fields"
    )
    case = fixture.split(f'  - name: "{name}"', 1)[1].split("\n  - name: ", 1)[0]
    error = re.search(r'"error": "((?:[^"\\]|\\.)*)"', case)
    expected = re.search(r'assert_eq!\(\.message, "((?:[^"\\]|\\.)*)"\)', case)
    assert error and expected, "the shared value-rule case is missing from the Vector fixture"
    value = _vrl_string(error.group(1))
    # Every class the rule treats specially is in the one shared input.
    for char in ('"', "\\", "'", "\n", "\x1b", "\x85"):
        assert char in value, repr(char)
    detail = _vrl_string(expected.group(1)).split(" detail=", 1)[1]
    assert log_fields(detail=value) == f"detail={detail}"


def test_log_fields_renders_a_bool_lowercase_and_never_drops_false() -> None:
    # No field passes a bool today — #222 retired `synthetic=`, the one that did — but
    # this is the RENDERER's contract, not that field's, and it is pinned so the next
    # bool field is correct on arrival. A bool is a LABEL a log-metric extractor lifts
    # and a dashboard filters on literally, so it must render the way every other
    # structured surface in the fleet writes it — not the way `str()` renders a Python
    # object — and `False` is a value like `0`, never an absence: a key whose `false`
    # were dropped as empty would leave a filter matching nothing, silently.
    assert log_fields(armed=True) == "armed=true"
    assert log_fields(armed=False) == "armed=false"
    assert log_fields(agent="nova", armed=False) == "agent=nova armed=false"


def test_the_renderer_itself_never_emits_colour() -> None:
    # Colour is applied to a token by `paint`, at the call site, as the line is handed
    # to the logger. The renderer only ever renders VALUES — so no field's value can
    # smuggle an escape into the middle of a token, and `StageRecord.detail` (which is
    # exactly this string) is escape-free by construction rather than by inspection.
    rendered = log_fields(outcome="ok", event="wake_end", agent="nova", exit=0)
    assert "\x1b" not in rendered


# --- the palette -----------------------------------------------------------


def test_the_palette_is_the_decided_table_exactly() -> None:
    # Colour is fleet law, carried on the harness's and the NOC's journald surfaces
    # too, so a colour must mean the same thing whichever daemon emitted the line.
    # Transcribed from @origin's decision (2026-08-17), so adding a token here without
    # the fleet agreeing to it fails, and dropping one fails too.
    inverted: dict[str, set[str]] = {}
    for token, colour in PALETTE.items():
        inverted.setdefault(colour, set()).add(token)
    assert inverted == _DECIDED


def test_the_operators_choice_tier_is_yellow_and_never_red() -> None:
    # A stopped wake is an operator's act, not a fault: painting it red would page a
    # reader's eye exactly as `outcome=failed` does. The contract page sets the tier's
    # SGR to `\x1b[33m`, the warning tier's, so the two read alike at a glance.
    for token in _OPERATORS_CHOICE:
        assert PALETTE[token] == "\x1b[33m", token
        assert paint(token) == f"\x1b[33m{token}\x1b[0m"


def test_the_colours_are_the_decided_sgr_codes() -> None:
    # Spelled out rather than derived: these exact bytes are what the render path was
    # verified against in Better Stack Live Tail (capital probe, 2026-08-17).
    assert (GREEN, BLUE, YELLOW, RED, RESET) == (
        "\x1b[32m",
        "\x1b[34m",
        "\x1b[33m",
        "\x1b[31m",
        "\x1b[0m",
    )


def test_paint_wraps_the_whole_token_so_a_substring_search_still_matches() -> None:
    # THE token-integrity rule. `event=\x1b[32mwake_start\x1b[0m` would break every
    # search for `event=wake_start` — in Live Tail, in `journalctl | grep`, in the
    # NOC's ClickHouse extraction — while still *looking* correct in a terminal.
    for token, colour in PALETTE.items():
        painted = paint(token)
        assert painted == f"{colour}{token}{RESET}"
        assert token in painted, f"{token!r} is no longer contiguous: {painted!r}"
        assert _ANSI.sub("", painted) == token


def test_paint_leaves_a_token_the_palette_does_not_name_untouched() -> None:
    # Silence is the default: an outcome with no decided colour (`outcome=ignored`) and
    # a line the palette says nothing about (`event=delivery_decision`) ship as they
    # always have, so this change cannot repaint a surface nobody asked to repaint.
    for token in ("outcome=ignored", "event=delivery_decision", "stage=wake", "agent=nova"):
        assert paint(token) == token


def test_every_painted_token_is_a_whole_key_value_pair() -> None:
    # The palette is keyed by the whole token, which is what makes the rule structural
    # rather than remembered: there is no call shape that can paint a bare key or a
    # bare value, because neither is ever a key of this table.
    for token in PALETTE:
        key, sep, value = token.partition("=")
        assert sep == "=" and key and value, f"{token!r} is not a key=value token"
        assert " " not in token


# --- every value reaches a line through the renderer -------------------------


def test_no_log_format_interpolates_a_value_after_a_key() -> None:
    # The value rule holds only for a value that goes through `log_fields`. A format
    # string such as `"delivery=%s"` drops a value in raw, so a `"` in it flips the
    # fleet's quote count, and a space in it splits it into tokens (#396: the decision
    # line's `delivery=` and `event_type=` come from unsigned request headers). A key
    # is followed by a rendered value, never by a placeholder.
    source = Path(__file__).resolve().parents[1] / "src" / "basecradle_router"
    raw = re.compile(r"""[A-Za-z_]=(?:%[-#0-9.]*[sdrfi]|["']%)""")
    offenders = [
        f"{path.relative_to(source)}:{number}: {line.strip()}"
        for path in sorted(source.rglob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if raw.search(line)
    ]
    assert offenders == []


def test_a_hostile_delivery_header_cannot_forge_the_decision_line(caplog) -> None:
    # A delivery id is read from a header no signature covers, so it is a stranger's
    # text until proven otherwise: it renders as one quoted value like any other.
    with caplog.at_level(logging.INFO, logger="basecradle_router"):
        log_delivery_decision(
            "github",
            "issues",
            DeliveryDecision.IGNORED,
            delivery='x" outcome=failed stage=wake',
        )
    (line,) = [r.getMessage() for r in caplog.records]
    assert line == (
        "event=delivery_decision source=github event_type=issues decision=ignored "
        'recipient=<unknown> delivery="x\' outcome=failed stage=wake"'
    )
    assert "outcome=failed" not in _outside_quotes(line)

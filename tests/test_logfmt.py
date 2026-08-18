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

import re

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

#: Any ANSI SGR sequence — used to read a painted line the way a colour-blind
#: consumer (a grep, an extraction regex) reads it.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

#: The palette as @origin decided it on 2026-08-17, transcribed from the decision
#: rather than from the implementation, so a drift in either direction fails here.
#: Four families: success, the `wake_end` bookend's own identity, the warning tier
#: (neither success nor failure), and failure.
_DECIDED = {
    GREEN: {"event=wake_start", "outcome=ok", "event=breaker_reset"},
    BLUE: {"event=wake_end"},
    YELLOW: {"event=wake_retry", "event=wake_refused", "event=wake_lock_stale"},
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

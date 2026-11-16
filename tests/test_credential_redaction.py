"""No line this repository renders carries a credential-named assignment (basecradle-router#491).

The NOC's log-store canary pages on a credential-named ``KEY=VALUE`` whatever the value is, and the
builders' wake translator quoted a refused command's text, ``export GH_TOKEN="$(gh-app-token …)"``,
into a tool line's ``error=``: no credential, and a page all the same. The redaction is
:func:`basecradle_router.logfmt.redact_credentials`, applied to every value the renderer writes,
to every bounded text and to every quoted line. These tests hold its definition of a credential's
name to the canary's own pattern (``conftest.NOC_CREDENTIAL_ASSIGNMENT``) and use that pattern as
the oracle every redacted line is checked against.
"""

from __future__ import annotations

import itertools
import json
import random
import re
import time

import pytest

from basecradle_router import logfmt
from basecradle_router.claude_stream import Translator, quoted
from basecradle_router.logfmt import (
    CANARY_VALUE_FLOOR,
    CANARY_VALUE_STOPS,
    CREDENTIAL_NAME,
    CREDENTIAL_WORDS,
    REDACTED_VALUE,
    bounded,
    log_fields,
    redact_credentials,
)
from conftest import NOC_CREDENTIAL_ASSIGNMENT

#: A fabricated GitHub App installation token: `ghs_` and 36 characters, the shape every bot mints.
FAKE_TOKEN = "ghs_FABRICATEDnovaDigitalToken0000000000"

#: The row the canary matched (basecradle/basecradle#875), as the issue quotes it: the refused
#: command's text, cut where the issue cuts it. Claude Code wrote the quote as `"`; the renderer
#: turned it into the `'` the row shows.
INCIDENT_TEXT = (
    '<tool_use_error>Blocked: sleep 45 followed by: cd … export GH_TOKEN="$(gh-app-token …'
)
INCIDENT_LINE = (
    'WARNING tool name=Bash error="<tool_use_error>Blocked: sleep 45 followed by: cd … export'
    " GH_TOKEN='<a redacted value>'\""
)

#: The whole command a builder's mint line is, as the skill gives it.
MINT = (
    'GH_TOKEN="$(~/Documents/claude-workspace/shared/fleet-identity/gh-app-token'
    ' basecradle-nova-ai)" || exit 1; [ -n "$GH_TOKEN" ] || exit 1; export GH_TOKEN'
)


def silent(canary: re.Pattern[str], *lines: str) -> bool:
    return not any(canary.search(line) for line in lines)


# --- one definition: the canary's -------------------------------------------------------------


def test_the_pieces_spell_the_canarys_rule_byte_for_byte() -> None:
    stops = CANARY_VALUE_STOPS
    spelled = f"[A-Z][A-Z0-9_]*({'|'.join(CREDENTIAL_WORDS)})=[^{stops}]{{{CANARY_VALUE_FLOOR},}}"
    assert spelled == NOC_CREDENTIAL_ASSIGNMENT


def _canary_name_ends(text: str) -> set[int]:
    """Every `=` the canary's own name pattern ends at (overlapping starts included)."""
    name = re.compile(NOC_CREDENTIAL_ASSIGNMENT.split("=")[0] + "=")
    return {m.end() for start in range(len(text)) if (m := name.match(text, start))}


def _linear_name_ends(text: str) -> set[int]:
    return {m.end() for m in re.finditer(CREDENTIAL_NAME, text)}


@pytest.mark.parametrize(
    "text",
    [
        "GH_TOKEN=",
        "export GH_TOKEN=x",
        "NOC_FLEET_GH_TOKEN=x",
        "AKEY=x KEY=x TOKEN=x _TOKEN=x _9TOKEN=x 9ATOKEN=x xGH_TOKEN=x",
        "KEYKEY=x PASSPHRASE=x A_PASSPHRASE=x APASSWORD=x A_SECRET_KEY=x",
        "gh_token=x Gh_TOKEN=x GH_TOKEN_FILE=x GH_TOKENS=x A_KEY==x",
        "A_TOKEN=B_TOKEN=C_TOKEN=x",
    ],
)
def test_the_linear_spelling_names_exactly_the_canarys_credentials(text: str) -> None:
    assert _linear_name_ends(text) == _canary_name_ends(text)


def test_the_linear_spelling_agrees_with_the_canary_on_random_text() -> None:
    rng = random.Random(491)
    alphabet = ["A", "K", "E", "Y", "T", "O", "N", "S", "_", "9", "a", "=", " "] + list(
        CREDENTIAL_WORDS
    )
    for _ in range(4000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 14)))
        assert _linear_name_ends(text) == _canary_name_ends(text), text


def test_a_bare_word_is_no_credentials_name_because_the_canarys_is_not() -> None:
    # The canary needs a capital before the word, so `PASSWORD=` alone names nothing to it, and a
    # lowercase name nothing either. One definition: what the canary does not page on is kept.
    for text in ("PASSWORD=hunter2hunter2", "TOKEN=abcdefghij", "gh_token=abcdefghij"):
        assert redact_credentials(text) == text


def test_a_long_run_of_capitals_is_read_in_linear_time() -> None:
    # The canary's own pattern took jq 30 s on 50,000 capitals; a stalled translator hangs a wake.
    # Linear, this is milliseconds; quadratic, tens of seconds: the bound is far from both.
    started = time.monotonic()
    redact_credentials("A" * 50_000 + "=x")
    redact_credentials("KEY" * 17_000 + "=x")
    redact_credentials("_" * 50_000 + "TOKEN=x")
    assert time.monotonic() - started < 5.0


def test_nesting_no_interpreter_stack_could_hold_is_read_whole() -> None:
    assert redact_credentials("A_TOKEN=" + "$(" * 100_000) == "A_TOKEN=" + REDACTED_VALUE


# --- the value, whatever it is and however it is quoted ---------------------------------------


@pytest.mark.parametrize(
    ("text", "redacted"),
    [
        (f"GH_TOKEN={FAKE_TOKEN} gh api", "GH_TOKEN='<a redacted value>' gh api"),
        ("A_TOKEN=short tail", "A_TOKEN='<a redacted value>' tail"),
        ('A_TOKEN="a b c" tail', "A_TOKEN='<a redacted value>' tail"),
        ("A_TOKEN='a b c' tail", "A_TOKEN='<a redacted value>' tail"),
        ('A_TOKEN="a \\" b" tail', "A_TOKEN='<a redacted value>' tail"),
        ("A_TOKEN=$'a\\'b c' tail", "A_TOKEN='<a redacted value>' tail"),
        ("A_TOKEN=$(gh-app-token nova) tail", "A_TOKEN='<a redacted value>' tail"),
        ('A_TOKEN="$(gh-app-token "a b" $(x y))" tail', "A_TOKEN='<a redacted value>' tail"),
        ("A_TOKEN=`gh-app-token nova` tail", "A_TOKEN='<a redacted value>' tail"),
        ("A_TOKEN=${SECRET:-a b} tail", "A_TOKEN='<a redacted value>' tail"),
        ("A_TOKEN=a\\ b tail", "A_TOKEN='<a redacted value>' tail"),
        ("A_KEY=(sk1 sk2secretsecret) x", "A_KEY='<a redacted value>' x"),
        ("A_TOKEN=a\x1cb\x85c\xa0d tail", "A_TOKEN='<a redacted value>' tail"),
        ("A_TOKEN=\tsecretsecret", "A_TOKEN='<a redacted value>'"),
        ('A_TOKEN="never closed and the rest', "A_TOKEN='<a redacted value>'"),
        # A quote still open at a record break ends there: prose, far more often than a value.
        (
            "GH_TOKEN=don't do that.\n\nThe rest of the reply.",
            "GH_TOKEN='<a redacted value>'\n\nThe rest of the reply.",
        ),
        (
            "Set `GH_TOKEN=xyz` first.\n\nThen `gh pr create`.",
            "Set `GH_TOKEN='<a redacted value>'\n\nThen `gh pr create`.",
        ),
        ('A_TOKEN="spans\na line" tail', "A_TOKEN='<a redacted value>'\na line\" tail"),
        (
            "x=1 A_KEY=1 y=2 B_SECRET=2",
            "x=1 A_KEY='<a redacted value>' y=2 B_SECRET='<a redacted value>'",
        ),
        (
            MINT,
            "GH_TOKEN='<a redacted value>' || exit 1; [ -n \"$GH_TOKEN\" ] || exit 1;"
            " export GH_TOKEN",
        ),
    ],
)
def test_the_name_stays_and_the_value_goes_whole(text: str, redacted: str, canary) -> None:
    assert redact_credentials(text) == redacted
    assert silent(canary, redact_credentials(text))


@pytest.mark.parametrize(
    "text", ["GH_TOKEN=", "GH_TOKEN= next", "GH_TOKEN=\nnext", "no=credential"]
)
def test_an_empty_value_has_nothing_to_hide_and_is_left_alone(text: str) -> None:
    assert redact_credentials(text) == text


def test_the_marker_is_no_value_to_the_canary_whatever_follows_it(canary) -> None:
    # `<redacted>` would not do: ten characters the canary reads as a value.
    assert canary.search("GH_TOKEN=<redacted>")
    for after in ("", "x" * 20, "\tx" * 10, ",", "}", '"'):
        assert silent(canary, "GH_TOKEN=" + REDACTED_VALUE + after)


def test_a_record_break_is_defined_once() -> None:
    from basecradle_router import claude_stream

    assert claude_stream.RECORD_BREAKS is logfmt.RECORD_BREAKS


@pytest.mark.parametrize("brk", list(logfmt._BREAK_CHARS))
def test_the_canarys_run_ends_at_every_record_break(brk: str) -> None:
    # No record holds a run across a break, so the value read as the canary's run stops there.
    assert redact_credentials(f"A_TOKEN=\t{brk}next") == f"A_TOKEN='<a redacted value>'{brk}next"


def test_redacting_twice_is_redacting_once() -> None:
    once = redact_credentials(MINT)
    assert redact_credentials(once) == once
    assert redact_credentials(bounded(MINT)) == bounded(MINT)


def test_hostile_text_never_leaves_a_line_the_canary_reads(canary) -> None:
    rng = random.Random(875)
    pieces = [
        "GH_TOKEN=", "A_KEY=", "X_SECRET=", "P_PASSWORD=", "PASSPHRASE=", FAKE_TOKEN, "abcdefghij",
        '"', "'", "$(", ")", "`", "${", "}", "\\", " ", "\t", "\n", "\r", ",", "=", "$'",
        REDACTED_VALUE, "<redacted>", "\x00", "…",
    ]  # fmt: skip
    for _ in range(3000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 12)))
        assert silent(canary, log_fields(error=text), bounded(text), *quoted(text)), repr(text)


# --- every place a line carries foreign text --------------------------------------------------


def translate(events: list[dict]) -> list[str]:
    ticks = itertools.count()
    out: list[str] = []
    translator = Translator(write=out.append, clock=lambda: next(ticks) * 0.25)
    for event in events:
        translator.feed(json.dumps(event))
    translator.finish()
    return out


def tool_run(content: object, *, name: str = "Bash") -> list[dict]:
    use = {"type": "tool_use", "id": "toolu_01FabNova491Tool01", "name": name, "input": {}}
    result = {
        "type": "tool_result",
        "tool_use_id": "toolu_01FabNova491Tool01",
        "is_error": True,
        "content": content,
    }
    return [
        {"type": "assistant", "message": {"id": "msg_01FabNova491", "content": [use]}},
        {"type": "user", "message": {"content": [result]}},
    ]


def test_the_incidents_row_is_written_with_its_value_redacted(canary) -> None:
    out = translate(tool_run(INCIDENT_TEXT))
    assert INCIDENT_LINE in out
    assert silent(canary, *out)


def test_the_incidents_row_as_rendered_reads_the_same(canary) -> None:
    # The row as the canary read it, its quote already an apostrophe, redacts to the same line.
    out = translate(tool_run(INCIDENT_TEXT.replace('"', "'")))
    assert INCIDENT_LINE in out
    assert silent(canary, *out)


def test_a_refused_mint_line_keeps_its_command_and_loses_its_value(canary) -> None:
    text = f"<tool_use_error>Blocked: sleep 45 followed by: cd /home/basecradle-nova-ai && {MINT}"
    (line,) = [line for line in translate(tool_run(text)) if line.startswith("WARNING tool")]
    assert line == (
        'WARNING tool name=Bash error="<tool_use_error>Blocked: sleep 45 followed by: cd'
        " /home/basecradle-nova-ai && GH_TOKEN='<a redacted value>' || exit 1; [ -n '$GH_TOKEN' ]"
        ' || exit 1; export GH_TOKEN"'
    )
    assert silent(canary, line)


def test_a_real_token_in_a_tool_error_never_reaches_the_line(canary) -> None:
    for content in (
        f"Exit code 1 GH_TOKEN={FAKE_TOKEN} gh api",
        [{"type": "text", "text": f"env: NOC_FLEET_GH_TOKEN='{FAKE_TOKEN}'"}],
    ):
        out = translate(tool_run(content))
        assert not any(FAKE_TOKEN in line for line in out)
        assert any("GH_TOKEN='<a redacted value>'" in line for line in out)
        assert silent(canary, *out)


def test_a_failed_calls_detail_is_redacted(canary) -> None:
    failure = {
        "type": "assistant",
        "message": {
            "model": "<synthetic>",
            "content": [{"type": "text", "text": f"API Error: X_API_KEY={FAKE_TOKEN} rejected"}],
        },
        "is_api_error_message": True,
        "api_error_status": 401,
    }
    for event in (failure, {**failure, "parent_tool_use_id": "toolu_01FabNova491Tool01"}):
        out = translate([event])
        (line,) = [line for line in out if "outcome=failed" in line]
        assert "detail=\"API Error: X_API_KEY='<a redacted value>' rejected\"" in line
        assert silent(canary, *out)


def test_the_reply_its_errors_and_plain_stdout_are_redacted(canary) -> None:
    result = {
        "type": "result",
        "subtype": "success",
        "result": f"Done. I ran:\n\n    {MINT}\n\nand PASSWORD_FILE=ok.",
        "errors": [f"A_SECRET={FAKE_TOKEN}"],
        "terminal_reason": "B_TOKEN=abcdefghijkl",
    }
    out = translate([result])
    stdout: list[str] = []
    Translator(write=stdout.append, clock=lambda: 0.0).feed(f"plain C_KEY={FAKE_TOKEN} line")
    assert (
        "> " + "    GH_TOKEN='<a redacted value>' || exit 1; [ -n \"$GH_TOKEN\" ] || exit 1;"
        " export GH_TOKEN" in out
    )
    assert "> A_SECRET='<a redacted value>'" in out
    assert "> and PASSWORD_FILE=ok." in out  # FILE names no credential
    assert stdout == ["> plain C_KEY='<a redacted value>' line"]
    assert not any(FAKE_TOKEN in line for line in out + stdout)
    assert silent(canary, *out, *stdout)


def test_a_field_that_is_not_free_text_is_redacted_too(canary) -> None:
    # A tool's name is the stream's, not the translator's: every rendered value passes the rule.
    out = translate(tool_run("failed", name=f"mcp__x__D_TOKEN={FAKE_TOKEN}"))
    assert not any(FAKE_TOKEN in line for line in out)
    assert silent(canary, *out)


@pytest.mark.parametrize("tail", [MINT, "A_TOKEN=abc and more words", "A_TOKEN= then words"])
def test_a_bound_cuts_an_assignment_whole_or_not_at_all(tail: str, canary) -> None:
    # The renderer redacts again: a cut marker, or a `…` glued to a marker or an `=`, would be
    # read as a value and rebuilt as a whole marker, past the bound and with no `…`.
    for pad in range(150, 260):
        text = "x" * pad + " " + tail
        cut = bounded(text)
        assert len(cut) <= logfmt.BOUND
        assert redact_credentials(cut) == cut, (pad, cut[-40:])
        rendered = log_fields(error=cut)
        assert len(rendered) <= len(cut) + len('error=""')
        assert not cut.endswith("…") or rendered.rstrip('"').endswith("…")
        assert silent(canary, cut)


def test_the_daemons_fields_pass_the_same_rule(canary) -> None:
    # The router daemon renders through the same function: a wake's stderr in `error=` included.
    line = log_fields(stage="wake", outcome="failed", error=f"wake of nova exited 1: {MINT}")
    assert "GH_TOKEN='<a redacted value>'" in line
    assert silent(canary, line)


def test_the_module_states_its_marker_once() -> None:
    assert logfmt.REDACTED_VALUE == "'<a redacted value>'"

"""The `jq` fallback's program in `wake-runner` (basecradle-router#491, #497, #502, #504), from its pieces.

The fallback writes a builder's reply when the stream translator has died, by the translator's own
rule (`claude_stream.quoted`): every credential-named assignment's value redacted, then every line
led by `> `, cut at its record breaks and into pieces. Its regular expressions run in jq's
Oniguruma, which backtracks, and the program sits inside a jq string inside the agent script's
double-quoted bash string, so each piece is spelled three times over. This module writes each piece
once, from the translator's own constants (`logfmt`), and `tests/test_wake_runner_stream.py` holds
the shipped program to `PROGRAM`, byte for byte. To change it, change it here and run this module from
the repository root (`uv run python tests/fallback_rule.py`), which writes `PROGRAM` into
`deploy/bin/wake-runner`, then run the suites.

The value is the translator's reading of one, degraded only where a backtracking engine needs it:
the scheme words before it (#497), the lead words a tab joins it across (#502), the value of every
credential name it holds outside a quote (#504's review), then its last word, a quote or a
substitution read one level deep. Two choices keep the engine under its retry limit on any text: a
piece before a credential name never crosses a name's `=`, so each name is one item with one
reading, and a run of pieces with one reading repeats possessively.
"""

# ruff: noqa: E501 — `PROGRAM` is one shell line of the shipped script.

from __future__ import annotations

import re
from pathlib import Path

from basecradle_router.claude_stream import QUOTED_PIECE
from basecradle_router.logfmt import (
    _BREAK_CHARS,
    CANARY_VALUE_FLOOR,
    CANARY_VALUE_STOPS,
    CREDENTIAL_NAME,
    CREDENTIAL_TAIL,
    CREDENTIAL_WORDS,
    LEAD_OPENERS,
    RECORD_BREAKS,
    REDACTED_VALUE,
    SCHEME_GLUE,
)

WAKE_RUNNER = Path(__file__).resolve().parents[1] / "deploy" / "bin" / "wake-runner"


def jq(chars: str) -> str:
    """`chars` spelled inside a jq string, to stand in a regular expression as themselves: a
    backslash as the regex's escaped one, a quote and the control characters escaped, and anything
    past ASCII as `\\uXXXX` (the agent script is single-quoted, so a `'` is never written raw)."""
    spelled = {"\\": "\\\\\\\\", '"': '\\"', "'": "\\u0027", "\t": "\\t", "\n": "\\n", "\r": "\\r"}
    return "".join(
        spelled.get(c) or (c if " " <= c < "\x7f" else f"\\u{ord(c):04x}") for c in chars
    )


#: Every record break, spelled for a class.
BRK = jq(_BREAK_CHARS)

#: A regex's escaped backslash, spelled in a jq string.
BACKSLASH = "\\\\\\\\"


def run_char(but: str = "") -> str:
    """A character of the canary's run (a tab is one), and none of `but`."""
    return "[^" + jq(CANARY_VALUE_STOPS + _BREAK_CHARS + but) + "]"


#: Each spelling of a credential word and its tail: a name's `=` is one of these before an `=`.
SPELLINGS = [word + tail for tail in ("", CREDENTIAL_TAIL) for word in CREDENTIAL_WORDS]

#: An `=` that ends no credential name, and one that ends one. jq's Oniguruma allows no look-behind
#: whose alternatives differ in length, so each spelling has a look-behind of its own.
NOT_A_NAMES_EQ = "".join(f"(?<!{spelling})" for spelling in SPELLINGS) + "="
A_NAMES_EQ = "(?:" + "|".join(f"(?<={spelling})" for spelling in SPELLINGS) + ")="

#: The last word's pieces, the translator's shell word read one level deep: a double-quoted piece
#: (in which a backslash escapes), a single-quoted one, a substitution, a backquoted one, each to its
#: close or the record's end, an escape (of anything but a tab), and any other character but a
#: blank or an opener.
DOUBLE = '\\"(?:[^\\"' + BACKSLASH + BRK + "]|" + BACKSLASH + '.)*\\"'
SINGLE = "\\u0027[^\\u0027" + BRK + "]*\\u0027"
SUBSTITUTION = "\\\\$?\\\\([^)" + BRK + "]*\\\\)"
BACKQUOTED = "`[^`" + BRK + "]*`"
ESCAPE = BACKSLASH + "(?:[^\\t]|(?=\\t))"  # of a tab, none: the backslash ends its word (logfmt)


def bare(but: str = "") -> str:
    """Any other character of the last word: none of a blank, an opener or `but`."""
    return "[^" + jq(" \t\n\"'`\\(" + but) + "]"


PIECE = f"(?:{DOUBLE}?|{SINGLE}?|{SUBSTITUTION}?|{BACKQUOTED}?|{ESCAPE}|{bare()})"

#: The same pieces, each closed, and no bare `=` that ends a credential name: what stands before an
#: inner name's `=`, so a name inside a quote is the quote's text.
CLOSED_PIECE = (
    f"(?:{DOUBLE}|{SINGLE}|{SUBSTITUTION}|{BACKQUOTED}|{ESCAPE}|{bare('=')}|{NOT_A_NAMES_EQ})"
)

#: Where a word after a tab may start (`logfmt._reached`): not at a blank, a canary stop other than
#: the `"` a quoted piece opens with, or a record break.
REACH = "[^" + jq(CANARY_VALUE_STOPS.replace('"', "") + _BREAK_CHARS) + "]"

#: A lead word and the tabs after it (`logfmt._LEAD_WORD`, from `logfmt.LEAD_OPENERS`), its bare
#: character barred from a credential name's `=`, so the items have one reading.
LEAD_PIECE = (
    "(?:"
    + "|".join(
        [
            run_char("\t" + LEAD_OPENERS + "=") + "|" + NOT_A_NAMES_EQ,
            BACKSLASH + run_char("\t"),
            "\\u0027" + run_char("'") + "*\\u0027",
            "`(?:" + run_char("`\\") + "|" + BACKSLASH + run_char() + ")*`",
            "\\\\(" + run_char("()'`\\") + "*\\\\)",  # a parenthesis, closed, one level, no quote
        ]
    )
    + ")"
)
LEAD = f"(?:{LEAD_PIECE}++(?:{BACKSLASH})?|{BACKSLASH})\\t+"

#: A bearer scheme word (`logfmt._SCHEME_WORD`) and its blanks, a word reached after them.
SCHEME = f"(?:(?i:bearer)|[{SCHEME_GLUE}]*Bearer)[ \\t]+(?={REACH})"

#: The tabs a value opens with, before a word the canary's run reaches.
OPENING_TABS = f"(?:\\t+(?={REACH}))?"

#: An inner credential name: the closed pieces up to its `=`, possessive, then its value's tabs.
TO_A_NAME = f"{CLOSED_PIECE}*+{A_NAMES_EQ}"
ITEM = f"(?:{TO_A_NAME}{OPENING_TABS}|{SCHEME}|{LEAD}(?={REACH}))"

#: A last inner name whose value is empty or tabs alone: the value ends there, past the tabs.
LAST_INNER_NAME = f"{TO_A_NAME}(?:\\t+(?!{REACH})|(?=[ {BRK}]|\\\\z))"

#: A marker where the canary's run would end after it is the whole value (`logfmt._MARKER_ENDS`).
MARKER = jq(REDACTED_VALUE)
KEPT_MARKER = f"{MARKER}(?=[{jq(chr(9) + CANARY_VALUE_STOPS + _BREAK_CHARS)}]|\\\\z)"

#: The value: a kept marker; or its items, then a last inner name, one lead word or a run of the last
#: word's pieces (one lead word, never a repeat of them: the possessive lead tried at every piece
#: would read the word again at each); or tabs alone.
VALUE = f"(?:{KEPT_MARKER}|{OPENING_TABS}{ITEM}*(?:{LAST_INNER_NAME}|{LEAD}|{PIECE}+)|\\t+)"

#: Any canary-length run a value left, as the canary reads one.
RUN = run_char() + "{" + str(CANARY_VALUE_FLOOR) + ",}"

PROGRAM_JQ = (
    'fromjson? | select(type == "object" and .type == "result") | .result | strings'
    f' | "{CREDENTIAL_NAME}" as $n | "{MARKER}" as $r'
    f' | gsub("(?<n>" + $n + "){VALUE}"; "\\(.n)\\($r)")'
    f' | gsub("(?<n>" + $n + "){RUN}"; "\\(.n)\\($r)")'
    f' | "{jq(RECORD_BREAKS)}" as $b | sub("(?:" + $b + ")\\\\z"; "") | splits($b)'
    f' | if . == "" then ">" else . as $l | range(0; length; {QUOTED_PIECE}) as $i'
    f' | "> " + $l[$i:$i+{QUOTED_PIECE}] end'
)


def bash(text: str) -> str:
    """`text` inside a double-quoted bash string."""
    return "".join({"\\": "\\\\", '"': '\\"', "$": "\\$", "`": "\\`"}.get(c, c) for c in text)


#: The program as the agent script carries it.
PROGRAM = bash(PROGRAM_JQ)

#: Where the program stands in the script: the argument of the one `jq` call.
CALL = re.compile(r'(\t+"\$jq" -R -r --unbuffered ")((?:[^"\\]|\\.)*)(" \|\|\n)')


def shipped(script: str) -> str:
    """The program as the script carries it."""
    (match,) = CALL.finditer(script)
    return match.group(2)


if __name__ == "__main__":
    script = WAKE_RUNNER.read_text()
    WAKE_RUNNER.write_text(CALL.sub(lambda m: m.group(1) + PROGRAM + m.group(3), script, count=1))
    print(f"wrote {WAKE_RUNNER.relative_to(WAKE_RUNNER.parents[2])}")

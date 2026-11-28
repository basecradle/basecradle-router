"""The `jq` fallback's program in `wake-runner` (basecradle-router#491, #497, #502, #504, #505), from its pieces.

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
the scheme words before it (#497; any word ending in one, #510), the lead words a tab joins it across (#502), the value of every
credential name it holds outside a quote (#504's review), then its last word, a quote or a
substitution read one level deep. Every piece the engine tries and fails costs it a retry, of
10,000,000 a search, and three choices keep them few: a piece before a credential name never
crosses a name's `=`, so each name is one item with one reading; a run of pieces with one reading
repeats possessively, a run of tabs included; and a value's plain character is the first piece
tried, and a name's `=` is looked behind only where an `=` stands (#509). So a run of plain
characters costs none and reads at any length, and every other piece costs a few: the costliest
value, a run of `=` each after the last two letters of a credential word (`SE=SE=…`), exhausts the
limit at some 430,000 characters, and jq then fails, closed, the reply lost.

jq 1.7 counts every match's and capture's offset from the start of the string, and a slice
measures the whole string, so one `gsub` search an assignment and one `splits` a record cost the
text's length each (#505). The redaction reads many assignments a search (`redactor`) and the
records are cut by literal splits (`CUT`); both write exactly what the program before them wrote
(`BEFORE_JQ`), which the suite holds by a seeded fuzz.
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
    SCHEME_WORD,
    VALUE_GAP_RANGES,
    value_gap_class,
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
#: whose alternatives differ in length, so each spelling has a look-behind of its own. Each looks
#: behind only at an `=`, and one after no spelling's last letter, or last two, ends no name at once
#: (#509).
LAST_ONE = "".join(sorted({spelling[-1] for spelling in SPELLINGS}))
LAST_TWO = "|".join(sorted({spelling[-2:] for spelling in SPELLINGS}))
if not (LAST_ONE + LAST_TWO.replace("|", "")).isupper():
    raise ValueError("a spelling's last two characters stand in a look-behind as themselves")
NOT_A_NAMES_EQ = (
    f"(?==)(?:(?<![{LAST_ONE}])|(?<!{LAST_TWO})|"
    + "".join(f"(?<!{spelling})" for spelling in SPELLINGS)
    + ")="
)
A_NAMES_EQ = "(?==)(?:" + "|".join(f"(?<={spelling})" for spelling in SPELLINGS) + ")="

#: The last word's pieces, the translator's shell word read one level deep: a double-quoted piece
#: (in which a backslash escapes), a single-quoted one, a substitution, a backquoted one, each to its
#: close or the record's end, an escape (of anything but a tab), and any other character but a
#: blank or an opener, which is tried first: a value is mostly made of it (#509).
DOUBLE = '\\"(?:[^\\"' + BACKSLASH + BRK + "]|" + BACKSLASH + '.)*\\"'
SINGLE = "\\u0027[^\\u0027" + BRK + "]*\\u0027"
SUBSTITUTION = "\\\\$?\\\\([^)" + BRK + "]*\\\\)"
BACKQUOTED = "`[^`" + BRK + "]*`"
ESCAPE = BACKSLASH + "(?:[^\\t]|(?=\\t))"  # of a tab, none: the backslash ends its word (logfmt)


def bare(but: str = "") -> str:
    """Any other character of the last word: none of a blank, an opener or `but`."""
    return "[^" + jq(" \t\n\"'`\\(" + but) + "]"


PIECE = f"(?:{bare()}|{DOUBLE}?|{SINGLE}?|{SUBSTITUTION}?|{BACKQUOTED}?|{ESCAPE})"

#: The gap characters a shell word holds (all but its blanks): a carriage return, a NUL, an escape.
WORD_GAP = "".join(
    chr(point)
    for a, b in VALUE_GAP_RANGES
    for point in range(a, b + 1)
    if chr(point) not in " \t\n"
)

#: The same pieces, each closed, and no bare `=` that ends a credential name: what stands before an
#: inner name's `=`, so a name inside a quote is the quote's text. A plain character first (#509).
#: A gap character a shell word holds is a piece of it, except right after a scheme word where it and
#: the ones after it end the word: there they are the scheme word's gap (`Bearer<CR><LF>token`,
#: #561), as the translator reads a scheme word before the gap characters its word ends in.
WORD_GAP_PIECE = (
    f"(?:(?<!{SCHEME_WORD})|(?![{jq(WORD_GAP)}]*+(?:[{jq(' ' + chr(9) + chr(10))}]|\\\\z)))"
    f"[{jq(WORD_GAP)}]"
)
CLOSED_PIECE = (
    f"(?:{bare('=' + WORD_GAP)}|{NOT_A_NAMES_EQ}|{DOUBLE}|{SINGLE}|{SUBSTITUTION}|{BACKQUOTED}"
    f"|{ESCAPE}|{WORD_GAP_PIECE})"
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

#: The gap a value opens after (`logfmt.VALUE_GAP_RANGES`, #561): whitespace or control characters,
#: line breaks included, each one spelled for a jq string.
VALUE_GAP = value_gap_class(lambda point: jq(chr(point)))

#: A gap's characters the canary's run crosses (a tab, an escape, a non-breaking space): where a value
#: that is a gap alone ends, as `logfmt._value_end` ends it, at the canary's run.
GAP_RUN = "(?:(?![ " + BRK + "])" + VALUE_GAP + ")"

#: Where a bearer scheme word ends: the scheme word, looked behind at the end of a word of closed
#: pieces (`logfmt.SCHEME_WORD`, glued to anything before it, #510), then its gap (#561), a word
#: reached after it.
SCHEME_END = f"(?<={SCHEME_WORD}){VALUE_GAP}++(?={REACH})"

#: The gap a value opens with (#561), read possessively: the value is whatever follows it.
OPENING_GAP = f"{VALUE_GAP}*+"

#: A value that is a gap alone, to the text's end or before a `,` or a `}`: the gap's run, as the
#: canary reads it (`logfmt._value_end`, as after a scheme word's gap).
GAP_ALONE = f"{GAP_RUN}*(?={VALUE_GAP}*+(?:[,}}]|\\\\z))"

#: An inner credential name: the closed pieces up to its `=`, possessive, then its value's tabs.
TO_A_NAME = f"{CLOSED_PIECE}*+{A_NAMES_EQ}"

#: A marker where the canary's run would end after it is the whole value (`logfmt._MARKER_ENDS`),
#: after the gap it opens with (#561), a name's or an inner name's alike.
MARKER = jq(REDACTED_VALUE)
KEPT = f"{MARKER}(?=[{jq(chr(9) + CANARY_VALUE_STOPS + _BREAK_CHARS)}]|\\\\z)"
KEPT_MARKER = f"{OPENING_GAP}{KEPT}"

#: One item: the closed pieces up to an inner name's `=` (and its value's tabs) or to the end of a
#: scheme word (and its blanks), in one possessive reading, so a word that ends in neither costs one
#: reading of it, as it did before the scheme word was any word ending in one (#510); or a lead word.
#: A reading of no pieces never looks behind at a scheme word: an item ends at an `=`, a tab or a
#: blank, and a value opens after an `=`.
ITEM = f"(?:{CLOSED_PIECE}*+(?:{A_NAMES_EQ}{OPENING_GAP}(?![,}}]|\\\\z|{KEPT})|{SCHEME_END})|{LEAD}(?={REACH}))"

#: A last inner name whose value is a kept marker, which ends the value after it (an inner name's
#: value is read as a value of its own, `logfmt._value_end`), or a gap alone, to the text's end,
#: which ends it at the gap's run. One reading of the pieces before the name, for both: each
#: reading of them costs the engine its retries (#509).
LAST_INNER_NAME = f"{TO_A_NAME}(?:{KEPT_MARKER}|{GAP_ALONE})"


#: The value: a kept marker; or, past its gap, its items, then a last inner name, one lead word or a
#: run of the last word's pieces (one lead word, never a repeat of them: the possessive lead tried
#: at every piece would read the word again at each); or a gap alone, to the text's end.
VALUE = f"(?:{KEPT_MARKER}|{OPENING_GAP}(?![,}}]){ITEM}*(?:{LAST_INNER_NAME}|{LEAD}|{PIECE}+)|{GAP_RUN}+(?={VALUE_GAP}*+(?:[,}}]|\\\\z)))"

#: The first reading's value (#561): the value as the translator's first reading reads it, the one
#: before the gap, byte for byte (pinned by a test): no gap after a name's or an inner name's `=`,
#: blanks after a scheme word, and every gap character a piece of the word. The program reads it
#: first, then `VALUE` on what it left, as the translator does (`logfmt.redact_credentials`). The
#: translator's first reading also leaves to the gap reading a value that reading reaches farther
#: (a scheme word's or an inner name's gap); this one leaves it only a value of gap characters alone,
#: since telling farther costs a reading of the rest of the text an assignment, which jq 1.7
#: measures from its start (#505). So where the translator reads past such a gap, the fallback
#: writes the first reading's marker over the shorter value, as it did before #561, and the value
#: past the gap ships in the clear (`A_TOKEN=Bearer<LF>token`, `A_TOKEN=aB_TOKEN= value`): named on
#: #561. The fallback writes the reply only when the translator has died.
CLOSED_PIECE_PLAIN = (
    f"(?:{bare('=')}|{NOT_A_NAMES_EQ}|{DOUBLE}|{SINGLE}|{SUBSTITUTION}|{BACKQUOTED}|{ESCAPE})"
)
_SCHEME_END_PLAIN = f"(?<={SCHEME_WORD})[ \\t]+(?={REACH})"
_OPENING_TABS = f"(?:\\t++(?={REACH}))?"
_TO_A_NAME_PLAIN = f"{CLOSED_PIECE_PLAIN}*+{A_NAMES_EQ}"
_ITEM_PLAIN = (
    f"(?:{CLOSED_PIECE_PLAIN}*+(?:{A_NAMES_EQ}{_OPENING_TABS}|{_SCHEME_END_PLAIN})"
    f"|{LEAD}(?={REACH}))"
)
_LAST_INNER_NAME_PLAIN = f"{_TO_A_NAME_PLAIN}(?:\\t+(?!{REACH})|(?=[ {BRK}]|\\\\z))"
_KEPT_MARKER_PLAIN = f"{MARKER}(?=[{jq(chr(9) + CANARY_VALUE_STOPS + _BREAK_CHARS)}]|\\\\z)"
VALUE_PLAIN = (
    f"(?:{_KEPT_MARKER_PLAIN}|{_OPENING_TABS}{_ITEM_PLAIN}*"
    f"(?:{_LAST_INNER_NAME_PLAIN}|{LEAD}|{PIECE}+)|\\t+)"
)

#: A value of gap characters alone, which the first reading leaves to the gap reading.
GAPS_ALONE = f"\\\\A{VALUE_GAP}+\\\\z"

#: Any canary-length run a value left, as the canary reads one.
RUN = run_char() + "{" + str(CANARY_VALUE_FLOOR) + ",}"

#: The most credential-named assignments one search reads (#505), and the most characters it reads
#: across between two of them. jq 1.7 counts every match's offset, and every capture's, from the
#: start of the string, and a slice measures the whole string, so `gsub` cost the string's length
#: for every assignment, and jq offers no linear construction. Reading many per search divides that
#: cost by as many; assignments further apart than `GAP` cost what each did before. A search reads
#: four times as many as the last only while four times the last one's reach stays under `SPAN`
#: characters, far below what exhausts the engine's retry limit (some 430,000 characters of one
#: value of the costliest pieces, #509).
UNITS = 256
GAP = 4096
SPAN = 65536


def redactor(
    units: int = UNITS, gap: int = GAP, *, name: str = "redact", keep_gaps: bool = False
) -> str:
    """The jq `redact($body)` (or `name`): what `GSUB` writes, with up to `units` assignments read
    per search; with `keep_gaps`, a value of gap characters alone left as it is (`GSUB_FIRST`).

    A search runs on the rest of the text, so every reading sees all that follows, as `gsub`'s
    did, and the rest starts where `gsub`'s next search would. The rest cannot see the character
    before it, which matters only to a name starting there, and none can: a value that ends in a
    name character is followed by none, since a name character would be the value's. Each further
    assignment is the next one `gsub` finds: the characters before it are those where none starts,
    each tried as `gsub` tries it. The name is the assignment up to its first `=`, the name's own.

    The first search reads one, and one that reads all it may lets the next read four times as many
    (see `SPAN`), so a reply with few assignments compiles no pattern longer than it needs. One
    search's assignments share one retry limit, which `gsub` gave each: a search that exhausts it is
    made again for a quarter as many, down to one, which is `gsub`'s own search; only that one
    failing fails the program, as `gsub` did, and so does any other error. The searches are a loop
    that holds only the rest of the text, never a copy for each search made."""
    unit = f'"(?:(?<g>(?:(?!\\\\g<x>)(?s:.)){{0,{gap}}}+)(?<u>\\\\g<x>)"'
    return (
        f'def {name}($body): ("(?<x>" + $n + $body + "){{0}}") as $x'
        ' | def search($k): ["(?<u>\\\\g<x>)", (range(1; $k) | '
        + unit
        + '), (range(1; $k) | ")?"), $x] | add;'
        " [label $done | foreach range(0; infinite) as $_ ({t: ., k: 1}; .t as $t | .k as $k"
        " | (try ([$t | match(search($k))] | first)"
        ' catch (if $k > 1 and test("retry-limit") then "retry" else error end)) as $m'
        ' | if $m == "retry" then {t: $t, k: ([$k / 4 | floor, 1] | max), out: []}'
        " elif $m == null then {t: null, out: [$t]}"
        ' else [$m.captures[] | select(.offset >= 0 and .name != "x")] as $c'
        " | {t: $t[$m.offset + $m.length:], k: (if $m.length * 4 < "
        + str(SPAN)
        + ' and ([$c[] | select(.name == "u")] | length) == $k then [$k * 4, '
        + str(units)
        + "] | min else $k end), out: [$t[:$m.offset], ($c[]"
        + (
            ' | if .name == "g" then .string else .string as $s | ($s | index("=")) as $e'
            f' | if ($s[$e + 1:] | test("{GAPS_ALONE}")) then $s else $s[:$e + 1], $r end end)]}}'
            " end;"
            if keep_gaps
            else ' | if .name == "g" then .string else (.string | .[:index("=") + 1]), $r end)]} end;'
        )
        + " .out[], if .t == null then break $done else empty end)] | add;"
    )


#: The reply's lines: its records, cut at every record break (a CRLF first, as `RECORD_BREAKS`
#: reads one) and a trailing one dropped, then into pieces, by literal splits and slices of each
#: record's characters (an empty one kept: jq splits it into none). jq 1.7's `splits` measures the
#: whole text for every record, and a slice of a string measures the string, where a slice of its
#: characters does not (#505).
if RECORD_BREAKS != "\r\n|[" + _BREAK_CHARS + "]":
    raise ValueError("the records are cut by a CRLF and each of the break characters")
_SINGLE_BREAKS = ", ".join('"' + jq(c) + '"' for c in _BREAK_CHARS)
CUT = (
    f'reduce ("\\r\\n", {_SINGLE_BREAKS}) as $c ([.]; [.[] | if . == "" then . else split($c)[] end])'
    ' | if length > 1 and .[-1] == "" then .[:-1] end'
    f' | .[] | if . == "" then ">" else explode as $c | range(0; $c | length; {QUOTED_PIECE})'
    f' as $i | "> " + ($c[$i:$i+{QUOTED_PIECE}] | implode) end'
)

#: The program before #505, its redaction and its cut, which the program must read exactly as: one
#: `gsub` search an assignment, and a `splits` and a slice of the string a record.
GSUB = 'def redact($body): gsub("(?<n>" + $n + ")" + $body; "\\(.n)\\($r)");'
GSUB_FIRST = (
    'def redact_first($body): gsub("(?<n>" + $n + ")(?<v>" + $body + ")";'
    f' if (.v | test("{GAPS_ALONE}")) then "\\(.n)\\(.v)" else "\\(.n)\\($r)" end);'
)
SPLITS = (
    f'"{jq(RECORD_BREAKS)}" as $b | sub("(?:" + $b + ")\\\\z"; "") | splits($b)'
    f' | if . == "" then ">" else . as $l | range(0; length; {QUOTED_PIECE}) as $i'
    f' | "> " + $l[$i:$i+{QUOTED_PIECE}] end'
)


def program_jq(
    units: int = UNITS, gap: int = GAP, *, redaction: str | None = None, cut: str = CUT
) -> str:
    """The fallback's program, reading `units` assignments per search (or by `redaction`)."""
    return (
        'fromjson? | select(type == "object" and .type == "result") | .result | strings'
        f' | "{CREDENTIAL_NAME}" as $n | "{MARKER}" as $r'
        f" | {redaction or redactor(units, gap, name='redact_first', keep_gaps=True)}"
        f" {'' if redaction else redactor(units, gap)}"
        f' redact_first("{VALUE_PLAIN}") | redact("{VALUE}") | redact("{RUN}") | {cut}'
    )


#: The program before #505.
BEFORE_JQ = program_jq(redaction=GSUB_FIRST + " " + GSUB, cut=SPLITS)

PROGRAM_JQ = program_jq()


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

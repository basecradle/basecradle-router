"""The fleet's scrub, pass 1, in `ai_scrub` (basecradle-router#516), from its pieces.

Every rule reads the message as it came. Each rule's matches are recorded as spans (a start, an
end and the rule), the spans are merged where they overlap or touch, and the message is rebuilt
once, each merged span written as one marker. That is the harness's `scrub`
(basecradle-harness `_observability.scrub`, issue #929): **what the scrub takes is the union of
what its rules take**, so no rule reads another rule's output and no rule can take less beside
another than alone. It replaces the box's sequence of `replace` statements, in which one rule's
value or written-back word became another rule's boundary (basecradle-router#510, three rounds).

**Why spans and not one alternation.** Rust's regex, under VRL, is leftmost-first: at each
position it keeps the first alternative that matches and goes on past it, so a span another rule
would take that starts inside the consumed text is never seen. The union needs every rule's own
matches, so each rule is its own `replace_with` over the text as it came, used only for the spans
its closure records: `(?P<pre>[\\s\\S]*?)` before the rule, grouped (a top-level alternation would
otherwise bind it to the first branch alone), makes every match start where the last one ended,
so a running sum of the match lengths is each match's offset. The offsets are bytes,
as `length` and `slice` count them.

**One block, copied by bytes.** The block reads only `.message` and the parameters declared
immediately before it (`PARAMETERS`, each host's own), and leaves the scrubbed message in `msg`.
So every host that runs the fleet's scrub (this repository's three, the NOC's `noc_scrub`, the web
hosts' `web_scrub`) carries the same bytes between `START` and `END`, which one sha256 holds
(`BLOCK_SHA256`), plus its own parameter lines.

**It fails closed.** No call in the block can raise a runtime error (none is written with `!`, and
VRL refuses a fallible call left unhandled). Every fallible call is handled, and one that fails,
an offset that does not fit the text, a message longer than `scrub_longest`, or more spans than
`scrub_most`, sets `scrub_failed`, and then `msg` is the fixed `FAILED` marker, never the message
as it came. The bounds are what keep the pass linear in the line: the spans live in an object
that VRL copies on every write, so their number is capped.

To change the pass, change it here and run this module from the repository root
(`uv run python tests/scrub_block.py`), which writes the block into every host's file; then run
the suites. `tests/test_log_surface.py` holds every host's block to it, byte for byte.

A single quote is `\\x27` and a dollar sign `\\x24` in every pattern, because a raw string cannot
hold the one and Vector interpolates the other across the whole file.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import bearer_rule
import credential_rule

# --- the rules ------------------------------------------------------------------------------------

#: What makes every match start where the last one ended (above).
PRE = r"(?P<pre>[\s\S]*?)"

BRK = credential_rule.BRK

#: The two markers a value may already carry, kept wherever a value would end after one.
MARKERS = credential_rule.MARKERS
MARKER_END = credential_rule.MARKER_END


def _between(text: str, start: str, end: str) -> str:
    at = text.index(start) + len(start)
    return text[at : text.index(end, at)]


#: The credential rule's two patterns, the very bytes the every-field block carries, so the
#: message and every other field read a value alike: the parity pass, whose matches already run
#: end to end, and the run, which reads the text as it came here, where in the every-field block
#: it reads the parity pass's output.
PARITY = credential_rule.PARITY
RUN = credential_rule.RUN
assert _between(credential_rule.BLOCK, "parity = replace_with(text, r'", "') -> |") == PARITY
assert RUN.startswith("(?x)\n"), RUN[:20]

#: The credential rule's gap reading (#561), beside the two above: the union takes what either
#: takes.
PARITY_GAP = credential_rule.PARITY_GAP
RUN_GAP = credential_rule.RUN_GAP
assert _between(credential_rule.BLOCK, "replace_with(taken, r'", "') -> |") == PARITY_GAP
assert _between(credential_rule.BLOCK, "replace_with(gapped, r'", "') -> |") == RUN_GAP

#: A third reading of a value, the run with two pieces more, for the message alone: a parameter
#: expansion (basecradle-router#516, F4), and a value opening at a `,` or a `}` (limit 2), as the
#: print site and the harness read both. It is a reading beside the run, never in its place, so
#: the union takes what either takes: in place, a closed `{…}` piece ended a value the run's open
#: quote inside it read on.
_EXPANSION = r"\x24\{(?:[^{}\"\\" + BRK + r"]|\\[^" + BRK + r"])*\}"
_PIECES = credential_rule.CLOSED_PIECES
CLOSED_PIECE_WIDE = "(?:" + "|".join([*_PIECES[:-1], _EXPANSION, _PIECES[-1]]) + ")"
_OPEN = credential_rule.OPEN_PIECES
OPEN_PIECE_WIDE = "(?:" + "|".join([*_OPEN[:-3], _EXPANSION + "?", *_OPEN[-3:]]) + ")"


def _widened(pattern: str) -> str:
    for narrow, wide in (
        (credential_rule.CLOSED_PIECE, CLOSED_PIECE_WIDE),
        (credential_rule.OPEN_PIECE, OPEN_PIECE_WIDE),
    ):
        assert pattern.count(narrow) >= 1, narrow[:40]
        pattern = pattern.replace(narrow, wide)
    return pattern


_LAST = f"                            | {credential_rule.OPEN_PIECE}+\n"
assert RUN.count(_LAST) == 1
RUN_WIDE = _widened(
    RUN.replace(
        _LAST, _LAST + f"                            | [,}}]+{credential_rule.OPEN_PIECE}+\n"
    )
)

#: The wide run's gap reading (#561), beside it, as the gap reading is beside the run: a value past
#: a gap may open at a `,` or a `}` as well (`A_TOKEN= }…`), as the block that first read the gap
#: (#562) read it, under the same guard, so the box never takes less than that block did and reads
#: no more widely than it.
assert RUN_GAP.count(_LAST) == 1
RUN_WIDE_GAP = _widened(
    RUN_GAP.replace(
        _LAST, _LAST + f"                            | [,}}]+{credential_rule.OPEN_PIECE}+\n"
    )
)

#: The value as the run reads it, and as the wide run reads it, for the reading a marker gets
#: again (`_reread`).
VALUE_NARROW = (
    f"{credential_rule.INNER_NAMES}"
    f"(?:{credential_rule.CLOSED_QUOTED_VALUE}"
    f"|{credential_rule.SCHEME_CHAIN}+{credential_rule.OPEN_PIECE}+"
    f"|{credential_rule.OPEN_PIECE}+)"
)
VALUE = _widened(
    f"{credential_rule.INNER_NAMES}"
    f"(?:{credential_rule.CLOSED_QUOTED_VALUE}"
    f"|{credential_rule.SCHEME_CHAIN}+{credential_rule.OPEN_PIECE}+"
    f"|{credential_rule.OPEN_PIECE}+"
    f"|[,}}]+{credential_rule.OPEN_PIECE}+)"
)

#: The same two values read by the gap reading's pieces (#561), the reread's other two readings: its
#: inner names and scheme words reach on past a gap, so the reread never stops shorter than the
#: block that first read the gap (#562) did.
VALUE_NARROW_GAP = (
    f"{credential_rule.INNER_NAMES_GAP}"
    f"(?:{credential_rule.CLOSED_QUOTED_VALUE}"
    f"|{credential_rule.SCHEME_CHAIN_GAP}+{credential_rule.OPEN_PIECE}+"
    f"|{credential_rule.OPEN_PIECE}+)"
)
VALUE_GAP = _widened(
    f"{credential_rule.INNER_NAMES_GAP}"
    f"(?:{credential_rule.CLOSED_QUOTED_VALUE}"
    f"|{credential_rule.SCHEME_CHAIN_GAP}+{credential_rule.OPEN_PIECE}+"
    f"|{credential_rule.OPEN_PIECE}+"
    f"|[,}}]+{credential_rule.OPEN_PIECE}+)"
)

#: The gap a value may open after its separator (`credential_rule.GAP`, #561): whitespace or control
#: characters, line breaks included, taken with the value. Each rule that anchors a value to a
#: separator is read twice, as one rule's two readings whose union the pass takes: as it read
#: before (`BLANK`, whitespace, or no gap at all), and with this gap. A reading beside the rule,
#: never in its place: widened in place, the gap let one rule's match consume a label or a name
#: whose own match took more (leftmost-first keeps the first reading that matches, not the
#: longest), and the union then took less than the rule had (the NOC's differential on #561).
GAP = credential_rule.GAP
BLANK = r"\s"

#: The heartbeat URL, from the scheme on where it has one; a kv value's closing quote ending the
#: run stays (basecradle-router#423), read from what follows it (`after`).
HEARTBEAT = (
    r"(?P<url>(?:https?://\S*)?/heartbeat/\S+)"
    r"(?P<after>[ \t]+(?:\x1b\[[0-9;]*m)?[A-Za-z_][A-Za-z0-9_]*=|[ \t]*(?:\n|\z))?"
)
#: The same URL read only to a `"`, where `scrub_heartbeat_to_quote` is set (the web hosts): their
#: request lines are JSON parsed after the scrub, and a path is text an outside caller chooses, so
#: a run that took the `"` closing the path would take the rest of the line with it and erase the
#: request's own fields, the caller's address included (the capital's web read of #516). A
#: backslash goes into the URL only before another character of it, never before the quote it
#: escapes (`API_RUN`), so a URL inside a JSON string (`\"…/heartbeat/K9\"`) keeps its line JSON.
#: The run never holds a `"`, so no closing quote is put back. With a scheme the URL may end right
#: after `/heartbeat/` (`url="https://john:…@host/heartbeat/"`), so userinfo before the host never
#: ships where the `\S` run took the quote after the path. A pattern is a literal in VRL, so the
#: two readings are two statements, and the block one sha256.
HB_RUN = r"""(?:[^\s"\\]|\\+[^\s"\\])"""
HEARTBEAT_TO_QUOTE = (
    rf"(?P<url>https?://{HB_RUN}*/heartbeat/{HB_RUN}*|/heartbeat/{HB_RUN}+)"
    + HEARTBEAT[HEARTBEAT.index("(?P<after>") :]
)
assert HEARTBEAT.startswith(r"(?P<url>(?:https?://\S*)?/heartbeat/\S+)(?P<after>"), HEARTBEAT


#: The `x-api-key` header (basecradle-router#423, basecradle-harness#861): its label, a quote at
#: any depth of escaping (basecradle-harness#935), and a value that reads on through every header
#: whose label it holds, as `_x_api_key_spans` does, by a repetition in its own pattern.
#: Its separator, and each bearer scheme word in its value, read `blank` after it.
def x_label(blank: str) -> str:
    return (
        rf"""(?i:x[-_]api[-_]?key)(?:\\*["\x27]{blank}*(?:=>|[:=,])|{blank}*(?::|=>))"""
        rf"""{blank}*(?:b?\\*["\x27])?"""
    )


X_RUN = r"""[^\s"\x27&\\]"""
X_WORD = r"""(?:[^\s"\x27&\\`\x24]|\x24[^\s"\x27&\\({])[^\s"\x27&\\]*"""


def x_api_key(blank: str) -> str:
    label = x_label(blank)
    glued = rf"""{X_RUN}*?(?i:bearer){blank}+[A-Za-z0-9._~+/-]{{8,}}"""
    return (
        f"{label}(?:(?:(?i:bearer){blank}+)*{X_RUN}*?(?:(?i:bearer){blank}+{X_RUN}*?)?{label})*"
        rf"""(?:(?:(?i:bearer){blank}+)+(?:{glued}|{X_WORD})|{glued}|{X_RUN}+)"""
    )


X_LABEL = x_label(BLANK)
X_API_KEY = x_api_key(BLANK)
X_API_KEY_GAP = x_api_key(GAP)

#: An `api[-_]?key=` value (the box's own query value too), and a key in a URL's query
#: (basecradle-router#516, F1: the harness's `_QUERY`, the web hosts' rule): one value reading.
#: A marker after the `=` is kept where a value would end after it; one with anything glued to it
#: is a value. The value may open with a quote at any depth of escaping (basecradle-router#515).
#: A backslash goes into a value only before another character of the value, never before the
#: quote it escapes, so a value inside a JSON string (`\"api_key=K9\"`) keeps its line JSON.
API_RUN = r"""(?:[^&\s"\\]|\\+[^&\s"\\])"""
API_WORD = rf"""(?:[^&\s"\x27`\x24\\]|\x24[^&\s"\x27({{]|\\+[^&\s"\x27\\]){API_RUN}*"""


def api_value(blank: str) -> str:
    """The value, each bearer scheme word in it read `blank` after it."""
    glued = rf"""{API_RUN}*?bearer{blank}+[A-Za-z0-9._~+/\-]{{8,}}"""
    return rf"""(?:\\*")?(?:(?:bearer{blank}+)+(?:{glued}|{API_WORD})|{glued}|{API_RUN}+)"""


API_VALUE = api_value(BLANK)
KEPT = f'(?P<kept>(?-i:{MARKERS})(?:[\\x20\\t",}}{BRK}]|\\z))'
GLUED_MARKER = f'(?-i:{MARKERS})(?:[^&\\x20\\t",}}\\\\{BRK}]|\\\\+[^&\\x20\\t",}}\\\\{BRK}])*'
API_LABEL = "api[-_]?key="
#: Each `api_key=` label a value holds is read apart (basecradle-router#516, F6): the value reads
#: on through it, by a repetition, to the last label's own value. In the gap reading each label's
#: value reads past the gap after its `=`, and each scheme word past the gap after it (#561).


def api_key(gap: str, blank: str) -> str:
    return (
        f"(?i)(?P<label>{API_LABEL}){gap}"
        f'(?:{KEPT}|(?:(?-i:{MARKERS})?(?:\\\\*")?(?:bearer{blank}+)*{API_RUN}*?(?:bearer{blank}+{API_RUN}*?)?\\\\*{API_LABEL}{gap})*'
        f"(?:{GLUED_MARKER}|{api_value(blank)}))"
    )


API_KEY = api_key("", BLANK)
API_KEY_GAP = api_key(f"{GAP}*", GAP)
#: The labels' gap with whitespace after a scheme word, as the block that first read the gap (#562)
#: read it: a third reading, so the box never takes less than that block did.
API_KEY_LABEL_GAP = api_key(f"{GAP}*", BLANK)
QUERY = (
    "(?i)(?P<label>[?&](?:api[-_]?key|access[-_]?token|token|key)=)"
    f"(?:{KEPT}|{GLUED_MARKER}|{API_VALUE})"
)

#: A credential-named JSON member (decided by `@origin` on basecradle-router#516, 2026-11-19): a
#: string value of a member named exactly one of these words, in any case, with `-` or `_` or
#: nothing between its words, never a bare `key` or `token`. The name stays, and the value is the
#: JSON string to its close, so the line stays JSON. The member's quotes are escaped at any depth,
#: as the block's other openers are, and its three quotes (the name's two and the value's opener) at
#: the same depth: unescaped, the value is read as JSON reads a string (a backslash escapes the next
#: character); escaped as JSON inside one to five JSON strings (a run of `2**d - 1` backslashes, 1
#: to 31), a quote behind a longer run is the value's own and one behind the opener's run or a
#: shorter one closes it; at any other run the value ends at its first quote. The close is never
#: taken, so it stays as deep as it came. (A value ending in a backslash, escaped once or deeper,
#: reads past its close to the next quote that run allows: more is taken, and the outermost string
#: still parses.)
MEMBER_NAME = (
    "(?i:api[-_]?key|access[-_]?token|refresh[-_]?token|client[-_]?secret|password|secret"
    "|authorization)"
)
MEMBER_DEPTHS = (1, 3, 7, 15, 31)
#: Its colon reads `blank` on either side of it: JSON's whitespace, and in the gap reading the gap.


def member(blank: str) -> str:
    return "|".join(
        [
            rf"""(?P<member_0>"{MEMBER_NAME}"{blank}*:{blank}*")(?:[^"\\]|\\[\s\S])*""",
            *(
                rf"""(?P<member_{n}>\\{{{n}}}"{MEMBER_NAME}\\{{{n}}}"{blank}*:{blank}*\\{{{n}}}")"""
                rf"""(?:[^"\\]|\\+[^"\\]|\\{{{n + 1},}}")*"""
                for n in MEMBER_DEPTHS
            ),
            rf"""(?P<member_any>\\+"{MEMBER_NAME}\\+"{blank}*:{blank}*\\+")(?:[^"\\]|\\+[^"\\])*""",
        ]
    )


MEMBER = member(BLANK)
MEMBER_GAP = member(GAP)
_MEMBER_HEADS = " ?? ".join(
    f"string(member.member_{n})" for n in ("0", *map(str, MEMBER_DEPTHS), "any")
)

#: The bearer header (`tests/bearer_rule.py`), with one fix (basecradle-router#516, F2): a piece
#: in a run of scheme words must close, so a token whose quote never closes runs to its line's end
#: rather than ending at a scheme word inside its own quote. The bare token is captured, so the
#: host's minimum (`scrub_bearer_least`) is read in the closure.
#: No piece but a double-quoted one crosses a bare `"`, which on a JSON or logfmt line is the
#: line's own, as the credential rule's pieces never do: a token's quote left open on a JSON line
#: runs to that line's next `"`, never past it. And a backslash goes into a piece only before
#: another character of it, never before the quote it escapes (`API_RUN`).
CLOSED_OPENERS = (
    r"""\\+["\x27]|\x24\x27(?:[^\x27\\"\n]|\\+[^\x27\\"\n])*\x27"""
    r"""|\x27(?:[^\x27\\"\n]|\\+[^\x27\\"\n])*\x27|"(?:[^"\\\n]|\\.)*\""""
    r"""|`(?:[^`\\"\n]|\\+[^`\\"\n])*`|\x24\((?:[^)\\"\n]|\\+[^)\\"\n])*\)"""
    r"""|\x24\{(?:[^}\\"\n]|\\+[^}\\"\n])*\}"""
)
OPEN_TAIL = (
    r"""(?:\x24\x27(?:[^\x27\\"\n]|\\+[^\x27\\"\n])*|\x27(?:[^\x27\\"\n]|\\+[^\x27\\"\n])*"""
    r"""|"(?:[^"\\\n]|\\.)*|`(?:[^`\\"\n]|\\+[^`\\"\n])*|\x24\((?:[^)\\"\n]|\\+[^)\\"\n])*"""
    r"""|\x24\{(?:[^}\\"\n]|\\+[^}\\"\n])*)"""
)
WORD_PIECE = f"""(?:{CLOSED_OPENERS}|\\\\+[^"\\\\\\s]|[^\\s\\x27"`\\\\])"""


def bearer(blank: str) -> str:
    """The header, each scheme word read `blank` after it."""
    scheme = bearer_rule.scheme(blank)
    return (
        f"{scheme}(?:(?:{bearer_rule.ESCAPED_PIECE}\\S*?|{WORD_PIECE}*?){scheme})*"
        f"(?:{bearer_rule.ESCAPED_PIECE}\\S*|(?:{CLOSED_OPENERS}){WORD_PIECE}*{OPEN_TAIL}?|{OPEN_TAIL}"
        f"|(?P<bare>[A-Za-z0-9._~+/\\-]+)=*)"
    )


BEARER = bearer(bearer_rule.BLANK)
BEARER_GAP = bearer(GAP)


#: The bearer header as the box read it before (`tests/bearer_rule.py`), a second reading of the
#: same rule: a word ending in a scheme word may hold a piece it leaves open, and what follows
#: a quoted piece is read as one run of non-blanks. Each reading takes what the other misses
#: (`BEARER` reads a token's quote to its close or its line's end, this one reads past a quote it
#: never pairs), and the union takes both, so the box never takes less of a bearer header than
#: either reading did alone.
def bearer_words(header: str) -> str:
    words = header.replace(r"[A-Za-z0-9._~+/\-]+=*)", r"(?P<bare>[A-Za-z0-9._~+/\-]+)=*)")
    assert words.endswith("(?P<bare>[A-Za-z0-9._~+/\\-]+)=*)"), words[-60:]
    return words


BEARER_WORDS = bearer_words(bearer_rule.HEADER)
BEARER_WORDS_GAP = bearer_words(bearer_rule.HEADER_GAP)

#: The prefix rules, rule for rule, in pattern order: the prefix as written, the name its run is
#: captured under, the run's class and its minimum (the box's; a subset of the harness's
#: `_PREFIXED`, whose `sk-` and `xai-` are 8 where these are 16). A narrow run never swallows the
#: head of a key glued after it (basecradle-router#423): it is two alternatives, the first a run as
#: short as it can be, a whole key by any rule's own shape, then the widest run.
WIDE = "[A-Za-z0-9_-]"
PREFIX_RULES = (
    ("github_pat_", "pat", "[A-Za-z0-9_]", 20),
    ("gh[a-z]_", "gh", "[A-Za-z0-9]", 20),
    ("bc_(?P<bc_kind>[a-z]{3})_", "bc", WIDE, 8),
    ("bc_uat_", "bc_uat", "[A-Za-z0-9]", 1),
    ("bc_isk_", "bc_isk", "[A-Za-z0-9]", 1),
    ("sk-", "sk", WIDE, 16),
    ("xai-", "xai", WIDE, 16),
    ("AIza", "aiza", WIDE, 20),
    ("hf_", "hf", "[A-Za-z0-9]", 20),
    ("r8_", "r8", "[A-Za-z0-9]", 20),
    ("ste-", "ste", "[A-Za-z0-9]", 20),
    ("abl_", "abl", "[A-Za-z0-9]", 20),
    ("pplx-", "pplx", "[A-Za-z0-9]", 20),
    ("tk_", "tk", WIDE, 20),
)
#: The GitHub and BaseCradle rules; every other is a vendor's, run where `scrub_vendor_keys` is.
FLEET_PREFIXES = ("pat", "gh", "bc", "bc_uat", "bc_isk")

GLUED_KEY = (
    "(?:"
    + "|".join(
        f"{prefix.replace('(?P<bc_kind>[a-z]{3})', '[a-z]{3}')}{run}{{{least}}}"
        for prefix, _, run, least in PREFIX_RULES
    )
    + ")"
)


def _at_least(least: int, *, lazy: bool = False) -> str:
    return ("+" if least == 1 else f"{{{least},}}") + ("?" if lazy else "")


def rule_pattern(prefix: str, name: str, run: str, least: int) -> str:
    """One prefix rule: a wide run as is, a narrow one ending where a glued key begins."""
    if run == WIDE:
        return f"{prefix}(?P<{name}>{run}{_at_least(least)})"
    glued = f"(?P<{name}_glued>{run}{_at_least(least, lazy=True)}{GLUED_KEY}{WIDE}*)"
    return f"{prefix}(?:{glued}|(?P<{name}>{run}{_at_least(least)}))"


PREFIX = "(?P<before>^|[\\s\\S])(?:" + "|".join(rule_pattern(*r) for r in PREFIX_RULES) + ")"

_NARROW = [name for _, name, run, _ in PREFIX_RULES if run != WIDE]
_RUNS = " ?? ".join(f"string(key.{name})" for _, name, _, _ in PREFIX_RULES)
_GLUES = " ?? ".join(f"string(key.{name}_glued)" for name in _NARROW)
_FLEET = " || ".join(
    f"key.{name} != null" + (f" || key.{name}_glued != null" if name in _NARROW else "")
    for name in FLEET_PREFIXES
)

# --- the markers ----------------------------------------------------------------------------------

#: What a merged span is written as, by the rule whose span starts it. These are the box's
#: vocabulary, unchanged, and the NOC counts several of them.
MARKS = {
    "c": "'<a value the box redacted>'",
    "h": "[REDACTED_HEARTBEAT_URL]",
    "x": "x-api-key: [REDACTED]",
    "a": "apiKey=[REDACTED]",
    "q": "[REDACTED]",
    "b": "Bearer [REDACTED]",
    "g": "[REDACTED_GH_TOKEN]",
    "i": "[REDACTED_BC_INTEGRATION_SECRET]",
    "u": "[REDACTED_BC_TOKEN]",
    "k": "[REDACTED_API_KEY]",
    "m": "'<a value the box redacted>'",
}

#: A member's value that is already a marker, the box's, the print site's or the harness's, is left
#: as it is: there is nothing in it to hide, and a line a scrub already wrote does not change.
MEMBER_KEPT = (
    """["'<a value the box redacted>'", "'<a redacted value>'", "[redacted]", "[REDACTED]"]"""
)

#: The whole message, when the pass cannot vouch for what it would ship.
FAILED = "[REDACTED_A_LINE_THE_BOX_COULD_NOT_SCRUB]"

#: How far a marker's second reading looks before it reads the rest of the line.
WINDOW = 4096

# --- the block ------------------------------------------------------------------------------------

_I = "      "  # the transform's own indent in the host file


def _add(indent: str, start: str, end: str, kind: str) -> str:
    """The statements that record one span, at `indent`: a start keeps its first rule and its
    longest end, and past `scrub_most` spans nothing more is recorded and the pass has failed. A
    start already recorded is never refused: two readings of one rule (#561) record one span
    there, which counts once."""
    lines = [
        f'span_key = slice("000000000" + to_string({start}), -9) ?? ""',
        "span_had = -1",
        "span_prior = get(scrub_spans, [span_key, 0]) ?? null",
        "if span_prior != null { span_had = to_int(span_prior) ?? -1 }",
        "if span_had < 0 && scrub_count >= scrub_most {",
        "    scrub_failed = true",
        f"}} else if span_had < 0 || {end} > span_had {{",
        f'    span_kind = "{kind}"',
        "    if span_had >= 0 { span_kind = string(get(scrub_spans, [span_key, 1]) ?? "
        f'"{kind}") ?? "{kind}" }}',
        f"    span_new, span_err = set(scrub_spans, [span_key], [{end}, span_kind])",
        "    if span_err == null { scrub_spans = span_new } else { scrub_failed = true }",
        "    if span_had < 0 { scrub_count = scrub_count + 1 }",
        "}",
    ]
    return "".join(f"{indent}{line}\n" for line in lines)


def _rule(
    pattern: str,
    var: str,
    body: str,
    *,
    flags: str = "",
    i: str = _I + "    ",
    text: str = "scrub_text",
    into: str = "",
    gives: str = '""',
) -> str:
    """One rule's statement at indent `i`: its matches in `text`, each starting where the last
    ended, read for the spans `body` records (written at `_R`, moved to `i`), then the running
    offset moved past the match. The closure returns `gives`, kept in `into` when one is named."""
    body = "".join(
        (i + "        " + line[len(_R) :] if line.startswith(_R) else line) + "\n"
        for line in body.splitlines()
    )
    return (
        f"{i}{into + ' = ' if into else ''}"
        f"replace_with({text}, r'{flags}{PRE}(?:{pattern})') -> |{var}| {{\n"
        f"{i}    if !scrub_failed {{\n"
        f'{i}        span_at = scrub_pos + length(string({var}.pre) ?? "")\n'
        f"{body}"
        f"{i}    }}\n"
        f"{i}    scrub_pos = scrub_pos + length({var}.string)\n"
        f"{i}    {gives}\n"
        f"{i}}}\n"
        f"{i}scrub_pos = 0\n"
    )


def _both(*readings_then_var_and_body: str, **kwargs: str) -> str:
    """A rule's readings, as it read before the gap and with it (#561), one statement each, each
    recording its own spans, so the union takes what any takes. The last two arguments are the
    closure's variable and body."""
    *readings, var, body = readings_then_var_and_body
    return "".join(_rule(reading, var, body, **kwargs) for reading in readings)


def _from_parity(indent: str, at: str, out: str, *, end: bool, reading: str = "") -> str:
    """Statements, at `indent`, that set `out` to the offset in the message of `at`, an offset in
    the parity pass's output (`scrub_parity`), where each value the parity pass took
    (`scrub_taken`, in order) stands as the box's marker: a start inside a marker maps to its
    value's start, an end inside one or at its end to its value's end. The run's matches come in
    order, so a marker every later offset lies past is passed once for all (`map_cursor`, with the
    shift before it, `map_base`), and only a compare is spent on it again. `reading` names the
    parity pass whose output it is: the first's (no suffix) or the gap reading's (`_gap`, #561),
    each with its own taken values and cursor."""
    size = len(MARKS["c"].encode())
    inside = f"{at} <= map_end" if end else f"{at} < map_end"
    before = f"{at} <= map_start" if end else f"{at} < map_start"
    lines = [
        "map_delta = map_base",
        "map_done = false",
        "for_each(scrub_taken) -> |map_index, taken| {",
        "    if !map_done && map_index >= map_cursor {",
    ]
    lines += [
        "        taken_from = to_int(get(taken, [0]) ?? 0) ?? 0",
        "        taken_to = to_int(get(taken, [1]) ?? 0) ?? 0",
        "        map_start = taken_from - map_delta",
        f"        map_end = map_start + {size}",
        f"        if {before} {{",
        f"            {out} = {at} + map_delta",
        "            map_done = true",
        f"        }} else if {inside} {{",
        f"            {out} = {'taken_to' if end else 'taken_from'}",
        "            map_done = true",
        "        } else {",
        f"            map_delta = map_delta + taken_to - taken_from - {size}",
        "            map_cursor = map_index + 1",
        "            map_base = map_delta",
        "        }",
        "    }",
        "}",
        f"if !map_done {{ {out} = {at} + map_delta }}",
    ]
    if reading:
        lines = [
            re.sub(r"\b(scrub_taken|map_cursor|map_base)\b", rf"\1{reading}", line)
            for line in lines
        ]
    return "".join(f"{indent}{line}\n" for line in lines)


_R = _I + "        "  # a closure's body


def _reread(indent: str, at: str, out: str) -> str:
    """Statements, at `indent`, that set `out` to where a value ends when the box's marker stands
    at `at` and what follows is read as the rest of its word: by the run and by the wide run, the
    farther of the two, or `at` itself when a marker would end a value there. The text is read in a
    window of 4 KiB, then 64 KiB, then the rest of the line, each only when the reading before
    reached its window's end, so a value costs about its own length to read again."""
    mark, size = MARKS["c"], len(MARKS["c"].encode())
    read = []
    readings = (
        ("reread_grown", VALUE_NARROW),
        ("reread_wide", VALUE),
        ("reread_gap", VALUE_NARROW_GAP),
        ("reread_wide_gap", VALUE_GAP),
    )
    for name, value in readings:
        read += [
            f'reread = parse_regex("{mark}" + reread_window, '
            f"r'(?x)\\A(?P<grown>{value})') ?? {{}}",
            f'{name} = length(string(reread.grown) ?? "") - {size}',
        ]
    read += [f"if {name} > reread_grown {{ reread_grown = {name} }}" for name, _ in readings[1:]]
    big = WINDOW * 16
    lines = [
        f"reread_at = {at}",
        f"{out} = reread_at",
        f'reread_window = slice(scrub_text, reread_at, reread_at + {WINDOW}) ?? ""',
        f'if reread_window != "" && !match(reread_window, r\'\\A[\\x20\\t",}}{BRK}]\') {{',
        *("    " + line for line in read),
        f"    if reread_grown >= length(reread_window) - 4 && reread_at + {WINDOW} < scrub_size {{",
        f'        reread_window = slice(scrub_text, reread_at, reread_at + {big}) ?? ""',
        *("        " + line for line in read),
        "        if reread_grown >= length(reread_window) - 4"
        f" && reread_at + {big} < scrub_size {{",
        '            reread_window = slice(scrub_text, reread_at) ?? ""',
        *("            " + line for line in read),
        "        }",
        "    }",
        f"    if reread_at + reread_grown > {out} {{ {out} = reread_at + reread_grown }}",
        "}",
    ]
    return "".join(f"{indent}{line}\n" for line in lines)


#: A merged span that holds a credential value reads on at its end, whichever rule's span starts it
#: (#561): a span another rule takes from before the name (an `api_key=` gap reading on into a
#: credential assignment) never stops the credential value reading on through what is glued after
#: the merged span, as it did when the credential span started it.
_EXTEND = (
    f"{_I}    if scrub_open >= 0 && scrub_open_holds_c {{\n"
    + _reread(_I + "        ", "scrub_open_end", "scrub_open_end")
    + f"{_I}    }}\n"
)

_MARK = (
    " else ".join(f'if scrub_open_kind == "{kind}" {{ "{mark}" }}' for kind, mark in MARKS.items())
    + f' else {{ "{FAILED}" }}'
)

_FLUSH = f"""\
{_I}    if scrub_open >= 0 {{
{_I}        scrub_kept = slice(scrub_text, scrub_done, scrub_open) ?? null
{_I}        if scrub_kept == null || scrub_open < scrub_done || scrub_open_end > scrub_size {{
{_I}            scrub_failed = true
{_I}        }} else {{
{_I}            scrub_parts = push(scrub_parts, scrub_kept)
{_I}            scrub_mark = {_MARK}
{_I}            scrub_parts = push(scrub_parts, scrub_mark)
{_I}        }}
{_I}        scrub_done = scrub_open_end
{_I}    }}
"""

START = "      # THE FLEET'S SCRUB, PASS 1 (basecradle-router#516). One block, the same bytes"
END = "      # END OF THE FLEET'S SCRUB, PASS 1.\n"

COMMENT = f"""\
{START}
      # on every host that runs it, between this line and its end; each host's own differences are
      # the parameter lines just above it, and nothing else in it varies. Generated by
      # tests/scrub_block.py, which states each rule, and held to it byte for byte by
      # tests/test_log_surface.py. Change it there, never here.
      #
      # EVERY RULE READS THE MESSAGE AS IT CAME, AND THE PASS TAKES THE UNION OF WHAT THEY TAKE,
      # as the harness's scrub does (basecradle-harness#929): each rule records the spans it would
      # take, the spans are merged where they overlap or touch, and the message is rebuilt once,
      # each merged span written as the marker of the rule whose span starts it (a credential
      # value's starts where its name ends, and the name stays). No rule reads another's output,
      # so no rule's value or marker is another rule's boundary (basecradle-router#510). A merged
      # span that holds a credential value and ends inside a word reads on, once more, as a value
      # read with the marker as its first piece (the harness's `_past_marker`), whichever rule's
      # span starts it (basecradle-router#561).
      #
      # FAILS CLOSED. Nothing here can raise an error, and a pass that cannot vouch for its output
      # (an offset that does not fit, a message longer than `scrub_longest`, more spans than
      # `scrub_most`) ships `{FAILED}` in place of the message, never the message as it came.
      #
      # A key is `slice` of a padded number and never `parse_int` without a base, which reads a
      # leading zero as octal; a missing span is told by `null`, never by `to_int`, which reads
      # `null` as 0.
"""


def _body(only: str | None = None) -> str:
    """The block's statements; with `only`, that one rule's alone (the differential's reading)."""
    credential_parity = _rule(
        PARITY.removeprefix("(?x)"),
        "segment",
        f'{_R}if segment.value != null && !includes(["\\"\'<a redacted value>\'\\"", '
        f'"\\"\'<a value the box redacted>\'\\""], segment.value) {{\n'
        f'{_R}    span_from = span_at + length(string(segment.head) ?? "")\n'
        f"{_R}    span_to = scrub_pos + length(segment.string)\n"
        f"{_R}    if scrub_count < scrub_most {{\n"
        f"{_R}        scrub_taken = push(scrub_taken, [span_from, span_to])\n"
        f'{_R}        parity_gives = (string(segment.head) ?? "") + "{MARKS["c"]}"\n'
        f"{_R}    }}\n" + _add(_R + "    ", "span_from", "span_to", "c") + f"{_R}}}\n",
        flags="(?x)",
        i=_I + "            ",
        into="scrub_parity",
        gives="parity_gives",
    ).replace(
        "    if !scrub_failed {",
        "    parity_gives = segment.string\n" + _I + "                if !scrub_failed {",
        1,
    )
    # The gap reading's parity pass (#561) writes an output of its own, as the first's does, so the
    # gap reading's run reads it too, its spans mapped back by its own taken values.
    credential_parity_gap = _rule(
        PARITY_GAP.removeprefix("(?x)"),
        "segment",
        f'{_R}if segment.value != null && !includes(["\\"\'<a redacted value>\'\\"", '
        f'"\\"\'<a value the box redacted>\'\\""], segment.word) {{\n'
        f'{_R}    span_from = span_at + length(string(segment.head) ?? "")\n'
        f"{_R}    span_to = scrub_pos + length(segment.string)\n"
        f"{_R}    if scrub_count < scrub_most {{\n"
        f"{_R}        scrub_taken_gap = push(scrub_taken_gap, [span_from, span_to])\n"
        f'{_R}        parity_gives = (string(segment.head) ?? "") + "{MARKS["c"]}"\n'
        f"{_R}    }}\n" + _add(_R + "    ", "span_from", "span_to", "c") + f"{_R}}}\n",
        flags="(?x)",
        i=_I + "            ",
        into="scrub_parity_gap",
        gives="parity_gives",
    ).replace(
        "    if !scrub_failed {",
        "    parity_gives = segment.string\n" + _I + "                if !scrub_failed {",
        1,
    )
    run_body = (
        f"{_R}if assignment.kept == null {{\n"
        f'{_R}    span_from = span_at + length(string(assignment.name) ?? "")\n'
        f"{_R}    span_to = scrub_pos + length(assignment.string)"
        " - length(string(assignment.quote_end) ?? "
        f'"") - length(string(assignment.record_break) ?? "")\n'
        + _add(_R + "    ", "span_from", "span_to", "c")
        + f"{_R}}}\n"
    )
    parity_body = (
        f"{_R}if assignment.kept == null {{\n"
        f'{_R}    parity_from = span_at + length(string(assignment.name) ?? "")\n'
        f"{_R}    parity_to = scrub_pos + length(assignment.string)"
        " - length(string(assignment.quote_end) ?? "
        f'"") - length(string(assignment.record_break) ?? "")\n'
        f"{_R}    span_from = parity_from\n"
        f"{_R}    span_to = parity_to\n"
        + _from_parity(_R + "    ", "parity_from", "span_from", end=False)
        + _from_parity(_R + "    ", "parity_to", "span_to", end=True)
        + _add(_R + "    ", "span_from", "span_to", "c")
        + f"{_R}}}\n"
    )
    parity_body_gap = parity_body
    for at, out, end in (("parity_from", "span_from", False), ("parity_to", "span_to", True)):
        first = _from_parity(_R + "    ", at, out, end=end)
        assert parity_body_gap.count(first) == 1, (at, "the gap run maps by its own taken values")
        parity_body_gap = parity_body_gap.replace(
            first, _from_parity(_R + "    ", at, out, end=end, reading="_gap")
        )
    credential_run = (
        f"{_I}        if length(scrub_taken) > 0 {{\n"
        + _rule(
            RUN.removeprefix("(?x)"),
            "assignment",
            parity_body,
            flags="(?x)",
            i=_I + "            ",
            text="scrub_parity",
        )
        + f"{_I}        }}\n"
        + f"{_I}        if length(scrub_taken_gap) > 0 {{\n"
        + _rule(
            RUN_GAP.removeprefix("(?x)"),
            "assignment",
            parity_body_gap,
            flags="(?x)",
            i=_I + "            ",
            text="scrub_parity_gap",
        )
        + f"{_I}        }}\n"
        + _rule(RUN.removeprefix("(?x)"), "assignment", run_body, flags="(?x)", i=_I + "        ")
        + _rule(
            RUN_GAP.removeprefix("(?x)"), "assignment", run_body, flags="(?x)", i=_I + "        "
        )
        + f"{_I}        if match(scrub_text, r'\\x24\\{{|=[,}}]') {{\n"
        + _rule(
            RUN_WIDE.removeprefix("(?x)"),
            "assignment",
            run_body,
            flags="(?x)",
            i=_I + "            ",
        )
        + f"{_I}        }}\n"
        + f"{_I}        if match(scrub_text, r'\\x24\\{{|=[,}}]') {{\n"
        + _rule(
            RUN_WIDE_GAP.removeprefix("(?x)"),
            "assignment",
            run_body,
            flags="(?x)",
            i=_I + "            ",
        )
        + f"{_I}        }}\n"
    )
    heartbeat_body = (
        f'{_R}url = string(hb.url) ?? ""\n'
        f"{_R}closing = parse_regex(url, "
        "r'\\A(?P<body>[\\s\\S]*?)(?P<quote>\"\\x1b\\[0m|\")\\z') ?? {}\n"
        f'{_R}quote = string(closing.quote) ?? ""\n'
        f'{_R}if quote == "\\"" && hb.after == null {{ quote = "" }}\n'
        f"{_R}span_to = span_at + length(url) - length(quote)\n"
        + _add(_R, "span_at", "span_to", "h")
    )
    heartbeat_to_quote_body = f'{_R}span_to = span_at + length(string(hb.url) ?? "")\n' + _add(
        _R, "span_at", "span_to", "h"
    )
    heartbeat = (
        f"{_I}    if scrub_heartbeat_to_quote {{\n"
        + _rule(HEARTBEAT_TO_QUOTE, "hb", heartbeat_to_quote_body, i=_I + "        ")
        + f"{_I}    }} else {{\n"
        + _rule(HEARTBEAT, "hb", heartbeat_body, i=_I + "        ")
        + f"{_I}    }}\n"
    )
    x_api_key = _both(
        X_API_KEY,
        X_API_KEY_GAP,
        "header",
        f"{_R}span_to = scrub_pos + length(header.string)\n" + _add(_R, "span_at", "span_to", "x"),
    )
    api_key = _both(
        API_KEY,
        API_KEY_GAP,
        API_KEY_LABEL_GAP,
        "assignment",
        f"{_R}if assignment.kept == null {{\n"
        f"{_R}    span_from = span_at\n"
        f'{_R}    span_label = span_at + length(string(assignment.label) ?? "")\n'
        f'{_R}    label_key = slice("000000000" + to_string(span_label), -9) ?? ""\n'
        f'{_R}    if (get(scrub_spans, [label_key, 1]) ?? "") == "c" {{ span_from = span_label }}\n'
        f"{_R}    span_to = scrub_pos + length(assignment.string)\n"
        + _add(_R + "    ", "span_from", "span_to", "a")
        + f"{_R}}}\n",
    )
    query = _rule(
        QUERY,
        "query",
        f"{_R}if query.kept == null {{\n"
        f'{_R}    span_from = span_at + length(string(query.label) ?? "")\n'
        f"{_R}    span_to = scrub_pos + length(query.string)\n"
        + _add(_R + "    ", "span_from", "span_to", "q")
        + f"{_R}}}\n",
    )
    bearer_body = (
        f'{_R}bare = string(header.bare) ?? ""\n'
        f'{_R}if bare == "" || length(bare) >= scrub_bearer_least {{\n'
        f"{_R}    span_to = scrub_pos + length(header.string)\n"
        + _add(_R + "    ", "span_at", "span_to", "b")
        + f"{_R}}}\n"
    )
    bearer = _both(BEARER, BEARER_GAP, "header", bearer_body) + _both(
        BEARER_WORDS, BEARER_WORDS_GAP, "header", bearer_body
    )
    prefix = _rule(
        PREFIX,
        "key",
        f'{_R}before = string(key.before) ?? ""\n'
        f'{_R}run = {_RUNS} ?? ""\n'
        f'{_R}glued = {_GLUES} ?? ""\n'
        f'{_R}kind = "k"\n'
        f"{_R}if key.pat != null || key.pat_glued != null"
        " || key.gh != null || key.gh_glued != null {\n"
        f'{_R}    kind = "g"\n'
        f"{_R}}} else if key.bc_isk != null || key.bc_isk_glued != null"
        ' || key.bc_kind == "isk" {\n'
        f'{_R}    kind = "i"\n'
        f"{_R}}} else if key.bc_uat != null || key.bc_uat_glued != null || key.bc != null {{\n"
        f'{_R}    kind = "u"\n'
        f"{_R}}}\n"
        f'{_R}word = glued == "" && run != ""'
        " && match(before, r'^[\\p{L}\\p{N}\\p{M}]') && !match(run, r'[0-9A-Z]')\n"
        f"{_R}if !word && (scrub_vendor_keys || {_FLEET}) {{\n"
        f"{_R}    span_from = span_at + length(before)\n"
        f"{_R}    span_to = scrub_pos + length(key.string)\n"
        + _add(_R + "    ", "span_from", "span_to", "kind").replace('"kind"', "kind")
        + f"{_R}}}\n",
        flags="(?x)",
    )
    member = _both(
        MEMBER,
        MEMBER_GAP,
        "member",
        f'{_R}span_from = span_at + length({_MEMBER_HEADS} ?? "")\n'
        f"{_R}span_to = scrub_pos + length(member.string)\n"
        f'{_R}member_value = slice(scrub_text, span_from, span_to) ?? ""\n'
        f"{_R}if span_to > span_from && !includes({MEMBER_KEPT}, member_value) {{\n"
        + _add(_R + "    ", "span_from", "span_to", "m")
        + f"{_R}}}\n",
    )
    rules = {
        "credential": (credential_parity + credential_parity_gap, credential_run),
        "heartbeat": heartbeat,
        "x-api-key": x_api_key,
        "api_key": api_key,
        "query": query,
        "bearer": bearer,
        "prefix": prefix,
        "member": member,
    }
    if only is not None:
        rules = {
            name: rule if name == only else ("", "") if name == "credential" else ""
            for name, rule in rules.items()
        }
    credential_parity, credential_run = rules.pop("credential")
    heartbeat, x_api_key, api_key, query, bearer, prefix, member = rules.values()
    credential = (
        f'{_I}    if contains(scrub_text, "=") {{\n'
        f'{_I}        if contains(scrub_text, "\\"") {{\n'
        f"{credential_parity}{_I}        }}\n"
        f"{credential_run}{_I}    }}\n"
        if credential_run
        else ""
    )
    return f"""\
{_I}scrub_text = replace(to_string(.message) ?? "", r'\\A', "")
{_I}scrub_size = length(scrub_text)
{_I}scrub_spans = {{}}
{_I}scrub_taken = []
{_I}scrub_parity = ""
{_I}map_cursor = 0
{_I}map_base = 0
{_I}scrub_taken_gap = []
{_I}scrub_parity_gap = ""
{_I}map_cursor_gap = 0
{_I}map_base_gap = 0
{_I}scrub_count = 0
{_I}scrub_pos = 0
{_I}scrub_failed = scrub_size > scrub_longest
{_I}if !scrub_failed && scrub_text != "" {{
{credential}{heartbeat}{x_api_key}{api_key}{query}{bearer}{prefix}{member}{_I}}}
{_I}scrub_parts = []
{_I}scrub_done = 0
{_I}scrub_open = -1
{_I}scrub_open_end = -1
{_I}scrub_open_kind = ""
{_I}scrub_open_holds_c = false
{_I}for_each(scrub_spans) -> |span_key, span| {{
{_I}    span_from = parse_int(span_key, base: 10) ?? -1
{_I}    span_to = to_int(get(span, [0]) ?? -1) ?? -1
{_I}    if span_from < 0 || span_to < span_from {{ scrub_failed = true }}
{_I}    if (string(get(span, [1]) ?? "") ?? "") == "c" {{
{_reread(_I + "        ", "span_to", "span_to")}{_I}    }}
{_I}    if scrub_open >= 0 && span_from > scrub_open_end {{
{_EXTEND.replace(_I + "    ", _I + "        ") * 2}{_I}    }}
{_I}    span_kind_here = string(get(span, [1]) ?? "") ?? ""
{_I}    if scrub_open >= 0 && span_from <= scrub_open_end {{
{_I}        if span_to > scrub_open_end {{ scrub_open_end = span_to }}
{_I}        if span_kind_here == "c" {{ scrub_open_holds_c = true }}
{_I}    }} else {{
{_FLUSH.replace(_I + "    ", _I + "        ")}{_I}        scrub_open = span_from
{_I}        scrub_open_end = span_to
{_I}        scrub_open_kind = span_kind_here
{_I}        scrub_open_holds_c = span_kind_here == "c"
{_I}    }}
{_I}}}
{_EXTEND}{_EXTEND}{_FLUSH}{_I}scrub_rest = slice(scrub_text, scrub_done) ?? null
{_I}if scrub_rest == null {{ scrub_failed = true }} else {{
{_I}    scrub_parts = push(scrub_parts, scrub_rest)
{_I}}}
{_I}msg = join(scrub_parts) ?? ""
{_I}if scrub_failed {{ msg = "{FAILED}" }}
"""


BLOCK = COMMENT + _body() + END
#: Each rule's statements alone, in the block's frame (the differential's `rules_alone`).
RULES = ("credential", "heartbeat", "x-api-key", "api_key", "query", "bearer", "prefix", "member")


def alone(rule: str) -> str:
    """The block with `rule`'s statements and no other rule's, its parameters before it."""
    assert rule in RULES, rule
    return PARAMETERS + COMMENT + _body(rule) + END


BLOCK_SHA256 = hashlib.sha256(BLOCK.encode()).hexdigest()

#: This repository's hosts' parameters, the lines just above the block.
PARAMETERS = """\
      # THE FLEET'S SCRUB'S PARAMETERS ON THIS HOST (basecradle-router#516): the only lines that
      # differ between the hosts that carry the block below. `scrub_bearer_least` is the shortest
      # bare bearer token taken (any length here); `scrub_vendor_keys` runs the vendors' prefix
      # rules beside the GitHub and BaseCradle ones; `scrub_heartbeat_to_quote` ends a heartbeat
      # URL at a `"`, so a JSON line whose path a caller chose (Caddy's access lines here) keeps
      # its fields, where `false` lets the run take the `"` and puts a kv value's closing quote
      # back; `scrub_longest` is the longest message read, in bytes, and `scrub_most` the most
      # spans one message may hold: past either the message ships as the block's failure marker.
      scrub_bearer_least = 1
      scrub_vendor_keys = true
      scrub_heartbeat_to_quote = true
      scrub_longest = 1048576
      scrub_most = 1024
"""

HOSTS = Path(__file__).resolve().parents[1] / "deploy" / "hosts"


def shipped(text: str) -> str:
    """The parameters and the block, as a host's file carries them."""
    start = text.index(PARAMETERS.splitlines()[0])
    return text[start : text.index(END) + len(END)]


if __name__ == "__main__":
    for path in sorted(HOSTS.glob("*/vector.yaml")):
        text = path.read_text()
        path.write_text(text.replace(shipped(text), PARAMETERS + BLOCK))
        print(f"wrote {path.relative_to(HOSTS.parents[1])}")
    print(f"block sha256 {BLOCK_SHA256}")

"""The bearer header in `ai_scrub` (basecradle-router#507), from its pieces.

The fleet's one definition is basecradle-harness `docs/telemetry.md` → Credential Scrubbing
(basecradle-harness#929): **a bearer header's token is taken whole, quoted or not, and a run of
scheme words goes with the token after it.**

1. The scheme word is `bearer` in any case, glued to anything before it, followed by whitespace.
2. The token is a run of `[A-Za-z0-9._~+/-]`, or the whole shell word a quote or substitution
   opens, or an escaped quote and the rest of its word.
3. While the word read as the token ends in `bearer`, the next word is the token in its place.

This module states the header once. The fleet's scrub (`tests/scrub_block.py`,
basecradle-router#516) reads it twice, as two readings of one rule whose union it takes: this
header (its bare token captured for a host's minimum) and a shell-word reading built from these
pieces, whose token is the whole shell word a quote opens and whose every middle word's pieces
close. Change a piece here and run `uv run python tests/scrub_block.py`, which writes the block
into every host's file, then run the suites.

The patterns are for Rust's regex under VRL, which is linear. Python's `re` backtracks, and the
scheme run's nested repetition is quadratic there: never read box text with them in Python.
A single quote is `\\x27` and a dollar sign `\\x24`, because a raw string cannot hold the one
and Vector interpolates the other across the whole file.
"""

from __future__ import annotations

#: The scheme word and the whitespace after it (rule 1). Unanchored on its left, so a word glued
#: to anything before it is one (`x.Bearer`, `a=Bearer`, `KBearer`, `abc-bearer`).
SCHEME = r"(?i:bearer)\s+"

#: The pieces of a word read as a token that ends in a scheme word (rule 3), as the fleet's
#: minimum reads them: closed quoted pieces and any other character but a blank or an opening
#: quote (`"q"bearer`, `'q'Bearer`, `$(x)Bearer`). A piece left open is no middle word: it is the
#: token, read by `TOKEN` to its line's end.
MINIMUM_PIECES = [r"\x27[^\x27\n]*\x27", r'"[^"\n]*"', r"`[^`\n]*`", r"""[^\s\x27"`]"""]

#: An escaped quote among a middle word's pieces (`x\"Bearer`), which the minimum leaves out. An
#: escaped quote is `"` or `'`, as `OPENED`'s is.
ESCAPED_PIECE = r"""\\+["\x27]"""

#: What opens a quoted token (rule 2): an escaped quote, a dollar-sign quote, a quote, a backquote
#: or a substitution, each to its close or its line's end when it never closes.
OPENED = (
    r"""(?:\\+["\x27]|\x24\x27(?:[^\x27\\\n]|\\.)*\x27?|\x27[^\x27\n]*\x27?|"(?:[^"\\\n]|\\.)*"?"""
    r"""|`[^`\n]*`?|\x24\([^)\n]*\)?|\x24\{[^}\n]*\}?)"""
)

#: The token (rule 2): a word that opens with `OPENED` is that piece and whatever is glued after
#: it; any other token is a run of token characters, of any length on the box (the source needs
#: eight, so `Bearer token missing` keeps its words there and only `missing` here). One piece, one
#: level deep, is the stated limit: no regular expression nests.
TOKEN = rf"""(?:{OPENED}\S*|[A-Za-z0-9._~+/\-]+=*)"""

#: A middle word (rule 3): any word the rule would read as the token that ends in a scheme word,
#: so the word after it is the token in its place. Either a quoted token's piece and the rest of
#: its word to the scheme word (`\"Bearer`, `'q'`Bearer`, `$("<TAB>)Bearer`, an unclosed piece
#: glued before it included), or closed pieces with an escaped quote among them (`"q"bearer`,
#: `x\"Bearer`). The minimum reads only the second, without the escaped quote, so under it
#: `Bearer 'q'`Bearer eyJ…` read `'q'`Bearer` as the token and left `eyJ…` to no rule.
MIDDLE_WORD = (
    rf"(?:{OPENED}\S*?|(?:{'|'.join([*MINIMUM_PIECES[:3], ESCAPED_PIECE, MINIMUM_PIECES[3]])})*)"
)

#: The scheme words after the first, each a middle word and its whitespace.
SCHEME_RUN = f"(?:{MIDDLE_WORD}{SCHEME})*"

#: A bearer header from its first scheme word to its token: the bearer rule's whole pattern.
HEADER = f"{SCHEME}{SCHEME_RUN}{TOKEN}"

"""The credential-named assignment rule in `ai_scrub` (basecradle-router#494), from its pieces.

VRL composes no pattern from parts, so every host's `vector.yaml` carries the rule's patterns
spelled out in full, each piece many times over. This module is where each piece is written once,
with what it reads; `tests/test_log_surface.py` holds every host's shipped block to `BLOCK`, byte
for byte, so a piece is never changed in one copy alone. To change the rule, change it here and
run this module from the repository root (`uv run python tests/credential_rule.py`), which writes
`BLOCK` into every host's file, then run the suites.

The value of a credential-named assignment is read as a SHELL WORD, as the print site's
`logfmt.redact_credentials` reads one, wherever a whole line allows: a run of pieces up to a blank,
a canary stop (`"`, `,`, `}`) or a record break. The two passes:

1. PARITY. Whether a `"` opens a piece of the value or closes the JSON string or logfmt value the
   assignment sits in is the count of quotes before it, as every fleet consumer reads a line. A
   name outside every quote whose word holds a double-quoted piece has that whole word as its
   value. Every other piece here must close, so that no piece left open can reach a later quote.
2. THE RUN, for every other value: the word, read with pieces that may stay open (each to its
   close or to a bare `"` or a record break), and every run of bearer scheme words with its token,
   a scheme word a tab glues to a value after lead words included (`LEAD_WORD`, #502).

No piece crosses a bare `"`: on a JSON or logfmt line it is the line's own.
"""

# ruff: noqa: E501 — `BLOCK` is the shipped VRL, line for line, and its lines are the file's.

from __future__ import annotations

from pathlib import Path

from basecradle_router.logfmt import (
    CREDENTIAL_TAIL,
    CREDENTIAL_WORDS,
    value_gap_class,
)

#: Every record break, as each class spells it (`logfmt._BREAK_CHARS`, pinned by a test).
BRK = r"\n\r\x00\x0b\x0c\x1c\x1d\x1e\x85\x{2028}\x{2029}"

#: What may stand between a separator and its value (`logfmt.VALUE_GAP_RANGES`, #561): a run of
#: whitespace or control characters, line breaks included. Every value here reads past it, after the
#: name's `=`, an inner name's `=` and a scheme word alike, and the span it is in takes it, so the
#: marker stands directly after the name.
GAP = value_gap_class(lambda point: f"\\x{{{point:x}}}")

#: The canary's credential name, byte for byte (`conftest.NOC_CREDENTIAL_ASSIGNMENT`).
NAME = f"[A-Z0-9_]*({'|'.join(CREDENTIAL_WORDS)})({CREDENTIAL_TAIL})?"

#: The print site's marker and the box's own, as a raw string spells them (`\x27` is `'`).
PRINT_MARKER = r"\x27<a\x20redacted\x20value>\x27"
BOX_MARKER = r"\x27<a\x20value\x20the\x20box\x20redacted>\x27"
MARKERS = f"(?:{PRINT_MARKER}|{BOX_MARKER})"

#: A raw double-quoted piece, to its close or the end: a journal record is one record, and its
#: quotes may span its lines. Inside it a backslash escapes any character, as JSON's escapes do.
DOUBLE_QUOTED = r'"(?:[^"\\]|\\[\s\S])*"?'

#: A double-quoted value closed on its line and followed by a word's end: one whatever the parity
#: before it, so a stray quote earlier on a line (an inch mark, a `'"'` in a traceback) never lets a
#: quoted value through. The close of a quoted value is never followed by one in a well-formed line.
CLOSED_QUOTED_VALUE = (
    r'"(?:[^"\\' + BRK + r"]|\\[^" + BRK + r'])*"(?P<quote_end>[\x20\t,}' + BRK + r"]|\z)"
)

#: One character outside every quote, or a backslash and the character after it, so `\"` is never
#: read as `\` and an opening quote. (The head's optional backslash before a name keeps
#: `\X_TOKEN=` read.)
OUTSIDE = r'(?:[^"\\]|\\[\s\S])'

#: Pass 1's pieces: each one closes.
CLOSED_PIECES = [
    r"\\[^" + BRK + "]",  # an escape, an escaped quote or space included
    r"\x24\x27(?:[^\x27\\\"" + BRK + r"]|\\[^" + BRK + r"])*\x27",  # dollar-sign quoting
    r"\x27(?:[^\x27\"\\" + BRK + r"]|\\[^\x27" + BRK + r"])*\\?\x27",  # single-quoted
    r"`(?:[^`\"\\" + BRK + r"]|\\[^" + BRK + r"])*`",  # backquoted
    r"\x24?\((?:[^()\"\\" + BRK + r"]|\\[^" + BRK + r"])*\)",  # a substitution, one level
    r"[^\x20\",}\\\x27`" + BRK + "]",  # any other character, a tab included
]

#: Pass 2's pieces: each quote to its close or to a bare `"` or a record break, a `$(` too, a bare
#: `(` only when it closes, a JSON-escaped `\"…\"` closed or not, and a lone backslash last.
OPEN_PIECES = [
    r'\\"(?:[^"\\' + BRK + r']|\\[^"' + BRK + r'])*(?:\\")?',  # JSON-escaped
    r"\\[^\"" + BRK + "]",  # an escape
    r"\x24\x27(?:[^\x27\\\"" + BRK + r"]|\\[^" + BRK + r"])*\x27?",  # dollar-sign quoting
    r"\x27(?:[^\x27\"\\" + BRK + r"]|\\[^\x27" + BRK + r"])*(?:\\?\x27)?",  # single-quoted
    r"`(?:[^`\"\\" + BRK + r"]|\\[^" + BRK + r"])*`?",  # backquoted
    r"\x24\((?:[^()\"\\" + BRK + r"]|\\[^" + BRK + r"])*\)?",  # a substitution
    r"\((?:[^()\"\\" + BRK + r"]|\\[^" + BRK + r"])*\)",  # a closed bare parenthesis
    r"[^\x20\",}\\\x27`" + BRK + "]",  # any other character, a tab included
    r"\\",  # a lone backslash
]

CLOSED_PIECE = "(?:" + "|".join(CLOSED_PIECES) + ")"
OPEN_PIECE = "(?:" + "|".join(OPEN_PIECES) + ")"

#: What the box's value reads a bearer header through when one is glued to it (#497): a value of
#: token characters, and the scheme word as HTTP spells it. The fleet's scheme word is any word
#: ending in `bearer` (basecradle-router#510), which the print site and the `jq` fallback read; any
#: other such word ends the box's value and leaves its token, a stated limit (deploy/README.md) until
#: the box's literal rules run as one alternation, where reading it cannot consume another rule's
#: label (basecradle-noc#1573).
SCHEME_GLUE = "A-Za-z0-9._~+/%:=-"
GLUED = f"[{SCHEME_GLUE}]*?Bearer"

#: A scheme word, alone in any case or glued.
SCHEME = f"(?:(?i:bearer)|{GLUED})"


#: What stands between a scheme word and its token in the first reading: blanks.
BLANKS = r"[\x20\t]"


def _schemes(blank: str) -> str:
    """A run of scheme words, each with the `blank` run after it, in any order, as the print site's
    `logfmt._value_end` reads them (#497)."""
    return rf"(?:{SCHEME}{blank}+)"


def _reached_schemes(blank: str) -> str:
    """A run of scheme words reached across tabs, as well as blanks (#502): each one after any prefix
    of closed pieces that ends at a tab (`x<TAB>Bearer`, `'q'<TAB>KBearer`, `(a)<TAB>Bearer`), a
    backslash right before the tab included, as the print site's `logfmt._value_end` never ends a
    value between a scheme word and its token. Every piece of the prefix closes, so none can stop
    inside a quote: one that could would let `'a<TAB>Bearer b'` read as a scheme word and open a
    quote that eats the line's fields. The box reads at least what the print site does here."""
    return rf"(?:(?:{CLOSED_PIECE}*?\\?\t)?\t*{SCHEME}{blank}+)"


SCHEMES = _schemes(BLANKS)
REACHED_SCHEMES = _reached_schemes(BLANKS)

#: The credential names a value holds outside every quote, each with its `=` (`'q'DB_PASSWORD=`):
#: the value goes on into each one's value, as the print site's `logfmt._value_end` reads it (#504's
#: review). The pieces before each name all close, so a name inside a quote is the quote's text. (A
#: `,` ends the box's value, so a name after one is an assignment of the box's own.)
INNER_NAMES = f"(?:{CLOSED_PIECE}*?{NAME}=)*"

#: A run of scheme words, each with the inner names its token holds before the next.
SCHEME_CHAIN = f"(?:{REACHED_SCHEMES}{INNER_NAMES})"

#: Pass 1's value: a word holding a double-quoted piece, a marker at its head included, and the
#: scheme words before it (`X_TOKEN=Bearer "…"`, `X_TOKEN=x<TAB>Bearer "…"`).
#: After its double-quoted piece the word goes on to a tab, and past it only by a scheme word and its
#: token (`X_TOKEN="a b"<TAB>Bearer tok`), so it never ends between the two.
_BARE = CLOSED_PIECES[-1]
assert _BARE.startswith(r"[^\x20"), _BARE  # any other character, a tab included
_TAIL = _BARE[len(r"[^\x20") :]
CLOSED_PIECE_BEFORE_A_TAB = "(?:" + "|".join([*CLOSED_PIECES[:-1], r"[^\x20\t" + _TAIL]) + ")"


def _parity_word(inner: str, schemes: str, chain: str) -> str:
    return (
        f"{inner}(?:{MARKERS}{schemes}*|{chain}*)"
        f"{CLOSED_PIECE}*{DOUBLE_QUOTED}(?:{CLOSED_PIECE_BEFORE_A_TAB}|{DOUBLE_QUOTED})*"
        f"(?:{chain}+(?:{CLOSED_PIECE}|{DOUBLE_QUOTED})*)?"
    )


PARITY_WORD = _parity_word(INNER_NAMES, SCHEMES, SCHEME_CHAIN)

# --- the gap reading (#561) -----------------------------------------------------------------------
#
# The same rule read with the gap after each separator: after a name's `=`, an inner name's `=` and
# a scheme word. It is a reading BESIDE the first, never in its place: the box's message pass takes
# the union of the two (`tests/scrub_block.py`), and the every-field block runs it after the first,
# on what the first left. Widened in place, a value read past a gap consumed a later name or label
# whose own match took more (leftmost-first keeps the first reading that matches, not the longest),
# and the box took less than it had: the NOC's differential on #561.

SCHEMES_GAP = _schemes(GAP)
REACHED_SCHEMES_GAP = _reached_schemes(GAP)

#: The closed pieces the run's open pieces read the same way: every closed piece but a bare `$` or
#: `(`, which may open the run's unclosed substitution, and an escaped `"`, which opens its
#: JSON-escaped quote. An inner name after these alone may have a gap after its `=`: the run's
#: reading with no inner name reads these pieces as they are and ends at the gap's blank, so
#: reading on past the gap only ever takes more.
_ESCAPE, *_QUOTED, _BARE_PIECE = CLOSED_PIECES
assert _ESCAPE == r"\\[^" + BRK + "]" and _BARE_PIECE.startswith(r"[^\x20"), (_ESCAPE, _BARE_PIECE)
PLAIN_PIECES = [r"\\[^\"" + BRK + "]", *_QUOTED, _BARE_PIECE.replace("`", r"`\x24\(", 1)]
PLAIN_PIECE = "(?:" + "|".join(PLAIN_PIECES) + ")"

#: The inner names, the gap after one's `=` read where only plain pieces stand before it, a reading
#: tried first so that it is the one kept where both match.
INNER_NAMES_GAP = f"(?:{PLAIN_PIECE}*?{NAME}={GAP}+|{CLOSED_PIECE}*?{NAME}=)*"
SCHEME_CHAIN_GAP = f"(?:{REACHED_SCHEMES_GAP}{INNER_NAMES_GAP})"
PARITY_WORD_GAP = _parity_word(INNER_NAMES_GAP, SCHEMES_GAP, SCHEME_CHAIN_GAP)

#: Where a kept marker ends: where the canary's run would.
MARKER_END = r'[\x20\t",}' + BRK + r"]|\z"

#: The values a credential-named field keeps as they are.
KEPT_FIELD_VALUES = """["", "'<a redacted value>'", "'<a value the box redacted>'"]"""

COMMENT = """      # CREDENTIAL-NAMED ASSIGNMENTS, IN EVERY FIELD, FOR EVERY WRITER (basecradle-router#494,
      # basecradle/basecradle#877). The value of any credential-named assignment becomes the box's
      # marker, `'<a value the box redacted>'`, and the name stays. "Names a credential" is the NOC's
      # log-store canary's definition, its `credential_assignment` rule, never a list of this box's
      # own: `[A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASSPHRASE)(_BASE)?=`, byte for byte (Rust's
      # regex, under VRL, is linear, so the canary's own spelling is safe here; @origin widened it
      # on 2026-11-17, basecradle-noc#1547: a bare word is a name, and `_BASE` the one tail).
      #
      # GENERATED. tests/credential_rule.py writes each piece of the two patterns below once, with
      # what it reads, and writes this block into every host's file; tests/test_log_surface.py holds
      # every host's block to it, byte for byte. Change the rule there, never here.
      #
      # A VALUE IS A SHELL WORD, read as the print site's `logfmt.redact_credentials` (the reference,
      # basecradle-router#491) reads one wherever a whole line allows: pieces up to a blank, a canary
      # stop (`"`, `,`, `}`) or a record break, each piece anywhere in the word. The print site reads
      # one value; the box reads a whole line, so no piece ever crosses a bare `"`, which on a JSON
      # or logfmt line is the line's own: a request's `'` never takes the rest of an access line.
      #   1. QUOTE PARITY. Whether a `"` opens a piece or closes the quoted value the assignment sits
      #      in (`a="X_TOKEN=" b=1`) is the count of quotes before it in the record, as every fleet
      #      consumer reads a line; a record is one record, so its quotes may span its lines. A name
      #      outside every quote whose word holds a double-quoted piece has the whole word as its
      #      value (`X_TOKEN="…"`, `X_TOKEN=abc"d e"`, `X_TOKEN=Y_TOKEN="…"`), bearer scheme words
      #      before it included (`X_TOKEN=Bearer "…"`). Every other piece here must close: one left
      #      open could reach a later `"` and eat a field.
      #   2. THE RUN, for every other value: a double-quoted value closed on its line before a word's
      #      end, whatever the parity (a stray quote earlier, an inch mark or a traceback's `'"'`,
      #      never lets one through); the word, with pieces that may stay open; and every run of
      #      bearer scheme words with the token after it (a `Bearer` glued to a value included, the
      #      words in any order), as the print site and the `api[-_]?key=` rule below take them: the
      #      scheme word alone is the canary's whole value. The canary's run crosses a tab, so a
      #      scheme word after one is reached too, after any prefix of pieces that all close
      #      (`x<TAB>Bearer`, `'q'<TAB>KBearer`, `(a)<TAB>Bearer`, basecradle-router#502): a value
      #      never ends between a scheme word and its token.
      #   Both passes read on into the value of a credential name the value holds outside every
      #   quote (`'q'DB_PASSWORD=<TAB>…`, `'q'DB_PASSWORD=Bearer …`), as the print site does.
      # A GAP BEFORE A VALUE IS THE VALUE'S (basecradle-router#561), in a second reading after the
      # first, on what the first left: a run of whitespace or control characters, line breaks
      # included, after a name's `=`, an inner name's `=` or a scheme word, is read past, and the
      # value after it is taken with it, so the marker stands directly after the name
      # (`A_TOKEN=<NEWLINE>value` ships as `A_TOKEN='<a value the box redacted>'`); a marker after
      # a gap is kept as its source wrote it. A word of prose after `NAME= ` is the accepted cost.
      # The second reading only ever writes a marker over what the first left, so it never takes
      # less than the first did; read in its place, a value past a gap consumed a later name whose
      # own value then shipped. An empty value (nothing but a gap to the record's end) is left as it
      # is, except before a record break, which the canary's run crosses: the marker stands between
      # the `=` and the break, and nothing is taken.
      #
      # THE TWO MARKERS PASS UNTOUCHED: the print site's `'<a redacted value>'` (the router's
      # renderer, and the harness's scrub) is a source doing its job, so the NOC's count of the box's
      # marker, per writer, is only what a source let through. A marker after a gap is kept with
      # its gap, as its source wrote it (#561): only the box's own marker is written directly
      # after the name. A marker is one where the canary's run
      # would end after it, where both redactors end every value: anything glued to it goes with it.
      # Each is one shell word with a space at its fourth character, so the canary reads `'<a`, under
      # its floor of eight. The bytes are a contract the NOC counts on (deploy/README.md → Part 5).
      #
      # EVERY FIELD: a writer chooses far more of the record than `MESSAGE` (`SYSLOG_IDENTIFIER`, the
      # `CODE_*` fields, any field it names, and by its binary and process name `_EXE` and `_COMM`),
      # so every string is read, a field sent more than once included, and a field whose own name is
      # a credential's has its value replaced unless empty or a marker.
      #
      # EVERY FIELD BUT THE MESSAGE, which the fleet's scrub above reads with this same rule among
      # its own (basecradle-router#516), and before the writer check, so a line marked `UNVERIFIED`
      # is redacted exactly as one that is not.
"""


def _parity(value: str) -> str:
    """Pass 1's pattern, its value read as `value`."""
    return (
        "(?x)\n"
        f"                      (?P<head>{OUTSIDE}*?\\\\?{NAME}=)\n"
        f"                      {value}\n"
        f"                    | {OUTSIDE}*(?:{DOUBLE_QUOTED})?"
    )


def _run(kept: str, head: str, inner: str, chain: str, *, record_break: bool = True) -> str:
    """Pass 2's pattern: a kept marker as `kept`, the value opening with `head`, its inner names and
    scheme words read as `inner` and `chain`, and with or without the record-break marker."""
    lines = [
        "(?x)",
        f"                  (?P<name>{NAME}=)",
        "                  (?:",
        f"                      (?P<kept>{kept})(?P<kept_end>{MARKER_END})",
        "                    | (?P<value>",
        f"                          {head}{inner}",
        "                          (?:",
        f"                              {CLOSED_QUOTED_VALUE}",
        f"                            | {chain}+{OPEN_PIECE}+",
        f"                            | {OPEN_PIECE}+",
        "                          )",
        *([f"                        | (?P<record_break>[{BRK}])"] if record_break else []),
        "                      )",
        "                  )",
    ]
    return "\n".join(lines)


_KEPT = f'{MARKERS}|"{MARKERS}"'

#: The rule's two passes as they read before the gap, which the box's message pass reads too.
PARITY = _parity(f"(?P<value>{PARITY_WORD})")
RUN = _run(_KEPT, "", INNER_NAMES, SCHEME_CHAIN)

#: The gap reading's two passes (#561): a marker kept after a gap as its source wrote it, so only
#: the box's own marker stands directly after a name; and a value past a gap taken with it, so the
#: marker the box writes stands directly after the name.
PARITY_GAP = _parity(f"(?P<value>{GAP}*(?P<word>{PARITY_WORD_GAP}))")
RUN_GAP = _run(f"{GAP}*(?:{_KEPT})", f"{GAP}*", INNER_NAMES_GAP, SCHEME_CHAIN_GAP)

#: The first reading's second pass in the every-field block, with no marker before a record break:
#: the gap reading after it writes that marker, or takes the value past the break, which a marker
#: the first reading wrote there would keep from it (VRL's regex has no look-ahead to read past one).
RUN_UNBROKEN = _run(_KEPT, "", INNER_NAMES, SCHEME_CHAIN, record_break=False)

BLOCK = (
    COMMENT
    + f"""      scrubbed = map_values(., recursive: true) -> |value| {{
          text = string(value) ?? ""
          if is_string(value) && contains(text, "=") {{
              # Only a field that holds a `"` has a parity to read: the rest skip the pass.
              parity = text
              if contains(text, "\\"") {{
                  parity = replace_with(text, r'{PARITY}') -> |segment| {{
                      if segment.value == null || includes(["\\"'<a redacted value>'\\"", "\\"'<a value the box redacted>'\\""], segment.value) {{
                          segment.string
                      }} else {{
                          (string(segment.head) ?? "") + "'<a value the box redacted>'"
                      }}
                  }}
              }}
              taken = replace_with(parity, r'{RUN_UNBROKEN}') -> |assignment| {{
                  # A value of gap characters alone (`X_TOKEN=<TAB> v`) is the gap reading's to take
                  # with what follows it: a marker over it here would be a marker that reading keeps.
                  if assignment.kept != null || match(string(assignment.value) ?? "", r'\A{GAP}+\z') {{
                      assignment.string
                  }} else {{
                      (string(assignment.name) ?? "") + "'<a value the box redacted>'" + (string(assignment.quote_end) ?? string(assignment.record_break) ?? "")
                  }}
              }}
              # The gap reading (#561), on what the first left: a value past a gap after a name's
              # `=`, an inner name's `=` or a scheme word goes with the gap.
              gapped = taken
              if contains(taken, "\\"") {{
                  gapped = replace_with(taken, r'{PARITY_GAP}') -> |segment| {{
                      if segment.value == null || includes(["\\"'<a redacted value>'\\"", "\\"'<a value the box redacted>'\\""], segment.word) {{
                          segment.string
                      }} else {{
                          (string(segment.head) ?? "") + "'<a value the box redacted>'"
                      }}
                  }}
              }}
              replace_with(gapped, r'{RUN_GAP}') -> |assignment| {{
                  if assignment.kept != null {{
                      assignment.string
                  }} else {{
                      (string(assignment.name) ?? "") + "'<a value the box redacted>'" + (string(assignment.quote_end) ?? string(assignment.record_break) ?? "")
                  }}
              }}
          }} else {{
              value
          }}
      }}
      . = scrubbed
      # A field whose own name is a credential's. One pattern over the record's names finds one,
      # so a record with none (nearly every record) never walks its fields a second time.
      if match(join(keys(.), "\\n"), r'{NAME}(?:\\n|\\z)') {{
          for_each(scrubbed) -> |field, value| {{
              if match(field, r'{NAME}\\z') {{
                  if is_array(value) {{
                      . = set!(., [field], map_values(array!(value)) -> |element| {{
                          if includes({KEPT_FIELD_VALUES}, element) {{ element }} else {{ "'<a value the box redacted>'" }}
                      }})
                  }} else if !includes({KEPT_FIELD_VALUES}, value) {{
                      . = set!(., [field], "'<a value the box redacted>'")
                  }}
              }}
          }}
      }}
"""
)

#: Where the block starts and what follows it, in every host's file.
START = "      # CREDENTIAL-NAMED ASSIGNMENTS, IN EVERY FIELD, FOR EVERY WRITER"
AFTER = "      # PRESENTATION — prefix the message"

HOSTS = Path(__file__).resolve().parents[1] / "deploy" / "hosts"


def shipped(text: str) -> str:
    """The block as a host's file carries it."""
    return text[text.index(START) : text.index(AFTER)]


if __name__ == "__main__":
    for path in sorted(HOSTS.glob("*/vector.yaml")):
        text = path.read_text()
        path.write_text(text.replace(shipped(text), BLOCK))
        print(f"wrote {path.relative_to(HOSTS.parents[1])}")

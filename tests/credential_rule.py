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
   close or to a bare `"` or a record break), and every bearer scheme word with its token.

No piece crosses a bare `"`: on a JSON or logfmt line it is the line's own.
"""

# ruff: noqa: E501 — `BLOCK` is the shipped VRL, line for line, and its lines are the file's.

from __future__ import annotations

from pathlib import Path

#: Every record break, as each class spells it (`logfmt._BREAK_CHARS`, pinned by a test).
BRK = r"\n\r\x00\x0b\x0c\x1c\x1d\x1e\x85\x{2028}\x{2029}"

#: The canary's credential name, byte for byte (`conftest.NOC_CREDENTIAL_ASSIGNMENT`).
NAME = "[A-Z][A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASSPHRASE)"

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

#: Pass 1's value: a word holding a double-quoted piece, a marker at its head included.
PARITY_WORD = f"{MARKERS}?{CLOSED_PIECE}*{DOUBLE_QUOTED}(?:{CLOSED_PIECE}|{DOUBLE_QUOTED})*"

#: What a bearer header may be glued to: a value of token characters, and the scheme word as HTTP
#: spells it, so a value merely ending in the letters (`abc-bearer`) is no header.
GLUED = r"[A-Za-z0-9._~+/%:=-]*?Bearer"

#: Where a kept marker ends: where the canary's run would.
MARKER_END = r'[\x20\t",}' + BRK + r"]|\z"

#: The values a credential-named field keeps as they are.
KEPT_FIELD_VALUES = """["", "'<a redacted value>'", "'<a value the box redacted>'"]"""

COMMENT = """      # CREDENTIAL-NAMED ASSIGNMENTS, IN EVERY FIELD, FOR EVERY WRITER (basecradle-router#494,
      # basecradle/basecradle#877). The value of any credential-named assignment becomes the box's
      # marker, `'<a value the box redacted>'`, and the name stays. "Names a credential" is the NOC's
      # log-store canary's definition, its `credential_assignment` rule, never a list of this box's
      # own: `[A-Z][A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASSPHRASE)=`, byte for byte (Rust's regex,
      # under VRL, is linear, so the canary's own spelling is safe here).
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
      #      value (`X_TOKEN="…"`, `X_TOKEN=abc"d e"`, `X_TOKEN=Y_TOKEN="…"`). Every other piece here
      #      must close: one left open could reach a later `"` and eat a field.
      #   2. THE RUN, for every other value: a double-quoted value closed on its line before a word's
      #      end, whatever the parity (a stray quote earlier, an inch mark or a traceback's `'"'`,
      #      never lets one through); the word, with pieces that may stay open; and every bearer
      #      scheme word with its token (a `Bearer` glued to a value included), as the
      #      `api[-_]?key=` rule below takes them: the scheme word alone is the canary's whole value.
      # An empty value is left as it is, except before a record break, which the canary's run
      # crosses: the marker stands between the `=` and the break, and nothing is taken.
      #
      # THE TWO MARKERS PASS UNTOUCHED: the print site's `'<a redacted value>'` (the router's
      # renderer, and the harness's scrub) is a source doing its job, so the NOC's count of the box's
      # marker, per writer, is only what a source let through. A marker is one where the canary's run
      # would end after it, where both redactors end every value: anything glued to it goes with it.
      # Each is one shell word with a space at its fourth character, so the canary reads `'<a`, under
      # its floor of eight. The bytes are a contract the NOC counts on (deploy/README.md → Part 5).
      #
      # EVERY FIELD: a writer chooses far more of the record than `MESSAGE` (`SYSLOG_IDENTIFIER`, the
      # `CODE_*` fields, any field it names, and by its binary and process name `_EXE` and `_COMM`),
      # so every string is read, a field sent more than once included, and a field whose own name is
      # a credential's has its value replaced unless empty or a marker.
      #
      # FIRST, before every token-shape rule, so each value is read as written and carries the one
      # marker the NOC counts (after them `X_API_KEY=…` shipped as `X_apiKey=[REDACTED]`), and before
      # the writer check, so a line marked `UNVERIFIED` is redacted exactly as one that is not.
"""

BLOCK = (
    COMMENT
    + f"""      scrubbed = map_values(., recursive: true) -> |value| {{
          text = string(value) ?? ""
          if is_string(value) && contains(text, "=") {{
              # Only a field that holds a `"` has a parity to read: the rest skip the pass.
              parity = text
              if contains(text, "\\"") {{
                  parity = replace_with(text, r'(?x)
                      (?P<head>{OUTSIDE}*?\\\\?{NAME}=)
                      (?P<value>{PARITY_WORD})
                    | {OUTSIDE}*(?:{DOUBLE_QUOTED})?') -> |segment| {{
                      if segment.value == null || includes(["\\"'<a redacted value>'\\"", "\\"'<a value the box redacted>'\\""], segment.value) {{
                          segment.string
                      }} else {{
                          (string(segment.head) ?? "") + "'<a value the box redacted>'"
                      }}
                  }}
              }}
              replace_with(parity, r'(?x)
                  (?P<name>{NAME}=)
                  (?:
                      (?P<kept>{MARKERS}|"{MARKERS}")(?P<kept_end>{MARKER_END})
                    | (?P<value>
                          {CLOSED_QUOTED_VALUE}
                        | (?:(?i:bearer)[\\x20\\t]+)+{OPEN_PIECE}+
                        | {GLUED}[\\x20\\t]+{OPEN_PIECE}+
                        | {OPEN_PIECE}+
                        | (?P<record_break>[{BRK}])
                      )
                  )') -> |assignment| {{
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
AFTER = "      # Redact secret-shaped patterns from the message"

HOSTS = Path(__file__).resolve().parents[1] / "deploy" / "hosts"


def shipped(text: str) -> str:
    """The block as a host's file carries it."""
    return text[text.index(START) : text.index(AFTER)]


if __name__ == "__main__":
    for path in sorted(HOSTS.glob("*/vector.yaml")):
        text = path.read_text()
        path.write_text(text.replace(shipped(text), BLOCK))
        print(f"wrote {path.relative_to(HOSTS.parents[1])}")

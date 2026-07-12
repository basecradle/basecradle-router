"""Guard: no short-circuiting consumer may sit downstream in a pipeline under ``pipefail``.

This bans, repo-wide, the shape that made ``deploy/smoke-test.sh`` reject a healthy
daemon and roll back a good deploy (#172)::

    journalctl -u basecradle-router --since "$since" | grep -qE "$pattern"

Under ``set -o pipefail``, ``grep -q`` exits the instant it matches. The producer is
still writing, so it takes SIGPIPE (141); ``pipefail`` promotes that 141 to the whole
pipeline's status; the ``if`` therefore reads FALSE **even though the match is there**.
It is deterministic and position-dependent — a match near the END of the stream passes
(the producer has finished writing) while an earlier one always fails — which is why it
read as a genuine security-gate failure rather than a bug in the gate.

A deploy gate that can fail by winning a race is the real defect, so the class is
banned rather than the one instance. The fix everywhere is the same: **capture the
producer's output, then match the captured string** — a here-string (``<<<``) or a
variable has no live producer to kill, so nothing can SIGPIPE.

The scanner below is quote-, comment-, and here-doc-aware because the naive grep for
this shape both MISSES the real bug (its pipeline wraps across two physical lines) and
FALSE-POSITIVES on ``deploy/bin/wake-runner`` (a multi-line single-quoted ``jq`` program
whose ``|`` are jq operators, not shell pipes). Offline by construction: it reads the
shipped scripts as text and never executes them.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Command words that stop reading before their producer stops writing. Downstream of a
# pipe under `pipefail`, each one can SIGPIPE-kill the stage feeding it and turn a
# successful match into a failed pipeline.
_GREPS = {"grep", "egrep", "fgrep", "rg", "zgrep"}
_QUIET_LONG = {"--quiet", "--silent", "--max-count"}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    stage: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: pipes into a short-circuiting consumer -> {self.stage}"


def _strip_heredocs(text: str) -> str:
    """Blank out here-doc BODIES; their contents are data, not shell syntax.

    A here-string (``<<<``) is not a here-doc and is left alone — it is precisely the
    safe replacement shape this guard exists to steer code toward.
    """
    out: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        match = re.search(r"<<-?\s*(?!<)(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", line)
        if not match:
            continue
        delimiter = match.group(2)
        while i < len(lines) and lines[i].strip() != delimiter:
            out.append("")  # body is data — blank it, but keep line numbers aligned
            i += 1
        if i < len(lines):
            out.append(lines[i])
            i += 1
    return "\n".join(out)


CODE, LITERAL, COMMENT = "c", "l", "#"


def _classify(text: str) -> list[str]:
    """Flag every character as shell CODE, a LITERAL (quoted/escaped), or a COMMENT.

    One state machine, used by everything below — so a ``|`` is judged a pipe by exactly
    one rule. It models the thing a naive quote-toggler gets wrong: **command
    substitution restarts the quoting context.** In ``"$(grep -E '^K=' "$F" | head)"``
    the quotes around ``$F`` open and close a *fresh* string inside ``$( )``; a toggler
    reads them as closing and reopening the outer one, concludes the ``| head`` is
    quoted, and waves the bug straight through.
    """
    flags: list[str] = []
    in_single = in_double = in_comment = False
    stack: list[tuple[bool, bool]] = []  # quoting context per `$( )` nesting level
    i = 0
    while i < len(text):
        char = text[i]
        if in_comment:
            if char == "\n":
                in_comment = False
                flags.append(CODE)  # the newline still terminates the command
            else:
                flags.append(COMMENT)
            i += 1
            continue
        if in_single:
            flags.append(LITERAL)
            if char == "'":
                in_single = False
            i += 1
            continue
        if char == "\\":  # the backslash and whatever follows are literal
            flags.append(LITERAL)
            if i + 1 < len(text):
                flags.append(LITERAL)  # incl. a newline: a line continuation
                i += 2
                continue
            i += 1
            continue
        if char == "$" and text[i : i + 2] == "$(":
            stack.append((in_single, in_double))
            in_single = in_double = False  # fresh quoting context inside `$( )`
            flags.extend([CODE, CODE])
            i += 2
            continue
        if char == ")" and stack and not in_double:
            in_single, in_double = stack.pop()
            flags.append(CODE)
            i += 1
            continue
        if char == "'":
            in_single = True
            flags.append(LITERAL)
            i += 1
            continue
        if char == '"':
            in_double = not in_double
            flags.append(LITERAL)
            i += 1
            continue
        if char == "#" and not in_double and (not flags or text[i - 1] in " \t\n;(|&"):
            in_comment = True
            flags.append(COMMENT)
            i += 1
            continue
        flags.append(LITERAL if in_double else CODE)
        i += 1
    return flags


def _logical_lines(text: str) -> list[tuple[int, list[tuple[str, str]]]]:
    """Split shell source into logical lines of ``(char, flag)`` pairs.

    A logical line is one command list: comments are dropped, backslash continuations
    and quoted newlines do not end it — so the wrapped ``journalctl |`` / ``grep -qE``
    pipeline that caused #172 is seen as the single pipeline it is.
    """
    flags = _classify(text)
    lines: list[tuple[int, list[tuple[str, str]]]] = []
    buf: list[tuple[str, str]] = []
    lineno = start = 1
    for char, flag in zip(text, flags, strict=True):
        if char == "\n":
            lineno += 1
            if flag == CODE and not _ends_open(buf):
                if any(c.strip() for c, _ in buf):
                    lines.append((start, buf))
                buf = []
                start = lineno
                continue
            buf.append((" ", flag))  # continuation: fold the newline into the line
            continue
        if flag == COMMENT:
            continue
        buf.append((char, flag))
    if any(c.strip() for c, _ in buf):
        lines.append((start, buf))
    return lines


def _ends_open(buf: list[tuple[str, str]]) -> bool:
    """Does this line end on a `|` or `&&` — i.e. is the command list still open?

    A pipeline may wrap after its `|`; the continuation is the same command.
    """
    code = "".join(char for char, flag in buf if flag == CODE).rstrip()
    return code.endswith(("|", "&&"))


def _split_pipeline(line: list[tuple[str, str]]) -> list[str] | None:
    """Split a logical line on real shell pipes; ``None`` if it is not a pipeline.

    ``||`` is a logical OR, not a pipe — which is why ``|| die "could not read
    registry"`` must not be read as piping into the ``read`` builtin.
    """
    stages: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(line):
        char, flag = line[i]
        if char == "|" and flag == CODE:
            following = line[i + 1][0] if i + 1 < len(line) else ""
            if following == "|":
                buf.append("||")
                i += 2
                continue
            stages.append("".join(buf))
            buf = []
            i += 2 if following == "&" else 1  # `|&` is `2>&1 |`
            continue
        buf.append(char)
        i += 1
    if not stages:
        return None
    stages.append("".join(buf))
    return stages


def _short_circuits(stage: str) -> bool:
    """Does this pipeline stage stop reading before its producer stops writing?"""
    tokens = stage.split()
    # Step past leading `VAR=value` assignments and any `(`/`{` from a subshell.
    while tokens and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]) or tokens[0] in "({"):
        tokens.pop(0)
    if not tokens:
        return False
    command = Path(tokens[0].strip("(){}")).name
    args = tokens[1:]

    if command in _GREPS:
        for arg in args:
            if arg in _QUIET_LONG or arg.startswith("--max-count="):
                return True
            # Bundled short flags: -q, -qE, -m1, -im 1 ...
            if re.fullmatch(r"-[A-Za-z]*[qm][A-Za-z0-9]*", arg):
                return True
        return False
    if command == "head":
        return True
    if command == "read":  # the builtin consumes one line, then the pipeline ends
        return True
    if command in {"awk", "gawk", "mawk"}:
        return bool(re.search(r"\bexit\b", stage))
    if command in {"sed", "gsed"}:
        # `sed -n '/x/{p;q}'` and friends quit early; a plain substitution does not.
        return bool(re.search(r"\bq\b|;q|\{q", stage))
    return False


def scan(path: Path) -> list[Violation]:
    """Report every pipeline in ``path`` that feeds a short-circuiting consumer.

    Only scripts that enable ``pipefail`` are reported: without it the pipeline takes
    the consumer's own (successful) status and the shape is harmless and idiomatic. It
    is the combination that is a trap.
    """
    text = path.read_text()
    if not re.search(r"set\s+-[a-z]*o\s+pipefail|set\s+-[a-zA-Z]*o\s+pipefail", text):
        return []

    try:
        rel = path.relative_to(REPO).as_posix()
    except ValueError:  # a synthetic sample under tmp_path, in this module's own tests
        rel = path.name

    violations: list[Violation] = []
    for lineno, line in _logical_lines(_strip_heredocs(text)):
        stages = _split_pipeline(line)
        if not stages:
            continue
        for stage in stages[1:]:  # stage 0 is the producer; it cannot SIGPIPE itself
            if _short_circuits(stage):
                violations.append(Violation(rel, lineno, stage.strip()))
    return violations


def shell_scripts() -> list[Path]:
    """Every shell script tracked by git — discovered, never hand-listed, so a new one
    is covered the day it lands rather than the day someone remembers this file."""
    tracked = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    found: list[Path] = []
    for name in filter(None, tracked):
        path = REPO / name
        if not path.is_file():
            continue
        if path.suffix == ".sh":
            found.append(path)
            continue
        try:
            first = path.read_text(errors="replace").split("\n", 1)[0]
        except OSError:  # pragma: no cover - unreadable file
            continue
        if re.match(r"#!.*\b(ba|da|z|k)?sh\b", first):
            found.append(path)
    return found


def test_shell_scripts_are_discovered() -> None:
    """The sweep below is only meaningful if it actually found the scripts."""
    names = {p.relative_to(REPO).as_posix() for p in shell_scripts()}
    assert "deploy/smoke-test.sh" in names
    assert "deploy/bin/wake-runner" in names  # no .sh suffix — found by shebang
    assert len(names) >= 5


@pytest.mark.parametrize("script", shell_scripts(), ids=lambda p: p.relative_to(REPO).as_posix())
def test_no_short_circuiting_consumer_in_a_pipeline(script: Path) -> None:
    """No script may pipe a producer into a consumer that stops reading early.

    Capture the output first and match the captured string (``<<<``) instead. See this
    module's docstring for why the shape silently inverts an assertion under pipefail.
    """
    violations = scan(script)
    assert not violations, "\n".join(
        ["SIGPIPE-under-pipefail trap (#172) — capture the output, then match it:"]
        + [f"  {v}" for v in violations]
    )


# --- the scanner's own tests: a guard that cannot detect the bug is not a guard ------


def _scan_text(text: str, tmp_path: Path) -> list[Violation]:
    script = tmp_path / "sample.sh"
    script.write_text(text)
    violations = scan(script)
    return [Violation(v.path, v.line, v.stage) for v in violations]


def test_scanner_catches_the_original_bug(tmp_path: Path) -> None:
    """The exact shape that rolled back the #170 deploy, wrapped across lines as it was."""
    found = _scan_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if journalctl -u basecradle-router --since "$since" --no-pager 2>/dev/null |\n'
        '\tgrep -qE "$pattern"; then\n'
        "\techo hit\n"
        "fi\n",
        tmp_path,
    )
    assert len(found) == 1
    assert found[0].line == 3
    assert "grep -qE" in found[0].stage


def test_scanner_catches_head_and_max_count(tmp_path: Path) -> None:
    found = _scan_text(
        "set -euo pipefail\n"
        'secret="$(grep -E \'^KEY=\' "$FILE" | head -n1 | cut -d= -f2-)"\n'
        "first=\"$(printf '%s' \"$list\" | tr ',' '\\n' | grep -m1 .)\"\n",
        tmp_path,
    )
    assert {v.line for v in found} == {2, 3}


def test_scanner_accepts_the_fixed_capture_then_match_shape(tmp_path: Path) -> None:
    """The remedy must pass: a here-string has no live producer to SIGPIPE."""
    assert not _scan_text(
        "set -euo pipefail\n"
        'journal="$(journalctl -u basecradle-router --no-pager 2>/dev/null || true)"\n'
        'if grep -qE "$pattern" <<<"$journal"; then echo hit; fi\n',
        tmp_path,
    )


def test_scanner_ignores_pipes_that_are_not_shell_pipes(tmp_path: Path) -> None:
    """Quoted `|` (jq programs), `||`, here-doc bodies, and comments are not pipelines."""
    assert not _scan_text(
        "set -euo pipefail\n"
        "mapfile -t rows < <(jq -r '\n"
        "  to_entries[]\n"
        '  | [.key, (.value.kind // "github")]\n'
        '  | @tsv\'  "$REGISTRY")\n'
        'cat "$f" || die "could not read registry"\n'
        "cat <<-JSON >/dev/null\n"
        '\t{"a": "x | head -n1"}\n'
        "JSON\n"
        "# a comment mentioning a pipe into grep -q must not trip the scanner\n",
        tmp_path,
    )


def test_scanner_allows_full_reading_consumers(tmp_path: Path) -> None:
    """`awk '{print $NF}'`, `cut`, `tr`, `sort` read to EOF — they are safe downstream."""
    assert not _scan_text(
        "set -euo pipefail\n"
        'sig="$(openssl dgst -sha256 -hmac "$secret" "$1" | awk \'{print $NF}\')"\n'
        "n=\"$(printf '%s' \"$x\" | tr ',' '\\n' | sort | wc -l)\"\n",
        tmp_path,
    )


def test_scanner_is_scoped_to_pipefail(tmp_path: Path) -> None:
    """Without pipefail the pipeline takes grep's own status — the shape is harmless."""
    assert not _scan_text("#!/usr/bin/env bash\nset -eu\njournalctl | grep -q x\n", tmp_path)

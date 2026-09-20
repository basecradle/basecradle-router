"""Guard: privileged code never follows a link an unprivileged account can plant.

**The class** (basecradle/basecradle#576, 2026-09-20). The NOC's root deploy wrapper
acted *as root* on paths inside directories an agent owns, testing them with ``[[ -d … ]]``
— which follows symlinks — and then chowning, chmodding, writing or deleting. An account
that can write its own home could swap such a path for a symlink and have root act on the
link's **target**. Twenty-one call sites; not exploited, and swept in basecradle-noc#773.

**Why a check is not enough, and why a fence is not either.** Measured on systemd 255,
``ReadWritePaths=-/home/%i/scratch`` binds a symlink's *target* read-write into the unit's
namespace, so the sandbox is no defence. And ``realpath`` + a prefix test is a
*resolution*: it follows the link, then judges where it landed, which leaves a TOCTOU
window one statement wide and makes the privileged side act on a path the unprivileged
owner chose. Only refusing the link — or not being root — closes it.

**What this repo's sweep found** (the enumeration is on the issue). Every place a
root-run or daemon-run path of ours touches a directory another account writes is a
*read* whose act happens after the privilege drop: ``deploy/bin/wake-runner`` resolves the
clone and the harness ``wake_bin`` as root, then ``cd``s and ``exec``s as the **agent**.
No root write, chown, chmod or delete under an agent-writable path exists. So this module
guards the shape rather than a live bug — the two pre-drop resolutions now go through one
component-wise gate, and the scanner below fails the build when new privileged code
reaches an agent-writable path any other way.

Four guards, each with a positive control so none can pass vacuously:

1. **the helper itself** — the shipped ``confined_path`` body, run in bash against a real
   sandbox, must refuse every link, ``..``, and non-canonical shape, and change nothing;
2. **the call sites** — the caller's ``--cwd`` and the registry's ``wake_bin`` (and
   anything they flow into) may reach a filesystem operation *only* through the helper,
   in **every** shipped shell script, not just the one that has the problem today;
3. **the literals** — ``/home/`` may appear in a shipped file only as an operand of the
   helper, or under ``/home/router`` (the daemon's own home, which is not an agent's);
4. **the CLI's one caller-supplied write** — ``claims --out-dir`` runs as the daemon's
   user, so it refuses a symlinked directory and opens every file ``O_NOFOLLOW``.

Offline by construction: the scanners read shipped text, and the behavioural tests run one
extracted bash function against ``tmp_path``. No model, agent, box, or network.
"""

from __future__ import annotations

import ast
import errno
import os
import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from basecradle_router.__main__ import ManifestWriteError, _write_manifests

REPO = Path(__file__).resolve().parents[1]
WAKE_RUNNER = REPO / "deploy" / "bin" / "wake-runner"

#: The one sanctioned gate. Named once, here, so a rename in the script fails loudly
#: in the call-site guard rather than silently disarming it.
HELPER = "confined_path"

#: The daemon's own home. Not an agent's, so naming it is not a finding — it is where
#: `basecradle-router.service` finds `uv`.
DAEMON_HOME = "/home/router"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to exercise the shipped helper"
)


# --------------------------------------------------------------------------- #
# Reading shell the way the shell reads it.                                     #
# --------------------------------------------------------------------------- #


def logical_lines(text: str) -> list[tuple[int, str]]:
    """Shell source as the commands it runs: ``(1-based line, code)``, one list each.

    One pass does the two things a naive scanner gets wrong, because they are the same
    thing — knowing what is quoted:

    * **What the shell would expand.** Single-quoted spans and comments are erased;
      double-quoted spans are kept. That is exactly the expansion rule (``'$cwd'`` is
      four characters, ``"$cwd"`` is a path), and it erases two regions for free: the
      embedded ``jq`` program, whose ``|`` and ``.`` are not shell, and
      ``wake-runner``'s ``AGENT_SCRIPT`` — one single-quoted string holding everything
      that runs **after** the privilege drop, which is not privileged code at all.
    * **Where one command ends.** A newline inside quotes, inside ``$( )``, or after
      ``\\``/``|``/``&&`` does not end it. Without that, the four-line ``jq`` lookup
      reads as two commands and its real file operand lands on neither.

    Command substitution restarts the quoting context — in ``"$(jq '…' "$F")"`` the
    inner quotes open and close fresh strings — so ``$( )`` nesting is tracked rather
    than toggled, the mistake that makes a toggler wave the inner program through.
    """
    lines: list[tuple[int, str]] = []
    buffer: list[str] = []
    lineno = start = 1
    in_single = in_double = in_comment = False
    stack: list[tuple[bool, bool]] = []
    previous = "\n"
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\n":
            lineno += 1
            in_comment = False
            code = "".join(buffer).rstrip()
            if in_single or in_double or stack or code.endswith(("|", "&&")):
                buffer.append(" ")
                index += 1
                previous = "\n"
                continue
            if code.strip():
                lines.append((start, " ".join(code.split())))
            buffer = []
            start = lineno
            index += 1
            previous = "\n"
            continue
        if in_comment:
            index += 1
            continue
        if char == "\\" and not in_single:
            # The backslash and whatever follows are literal — including a newline,
            # which is a line continuation, so the command is still open.
            if index + 1 < len(text) and text[index + 1] == "\n":
                lineno += 1
                buffer.append(" ")
                index += 2
                previous = " "
                continue
            buffer.append(text[index : index + 2])
            index += 2
            previous = char
            continue
        if in_single:
            if char == "'":
                in_single = False
            buffer.append(" ")
            index += 1
            previous = char
            continue
        if text[index : index + 2] == "$(":
            stack.append((in_single, in_double))
            in_single = in_double = False
            buffer.append("$(")
            index += 2
            previous = "("
            continue
        if char == ")" and stack and not in_double:
            in_single, in_double = stack.pop()
            buffer.append(")")
            index += 1
            previous = char
            continue
        if char == "'" and not in_double:
            in_single = True
            buffer.append(" ")
            index += 1
            previous = char
            continue
        if char == '"':
            in_double = not in_double
            buffer.append(char)
            index += 1
            previous = char
            continue
        if char == "#" and not in_double and previous in " \t\n;(|&":
            in_comment = True
            index += 1
            continue
        buffer.append(char)
        index += 1
        previous = char
    code = "".join(buffer).strip()
    if code:
        lines.append((start, " ".join(code.split())))
    return lines


# --------------------------------------------------------------------------- #
# The scanner: which lines act on a filesystem path.                            #
# --------------------------------------------------------------------------- #

#: Commands that follow or mutate a path. `exec` is deliberately absent: its operands
#: here are `runuser` and `systemd-cat`, pinned byte-for-byte by
#: `tests/test_wake_runner_journal.py`, and the argv they carry is the wake's own data.
#: `jq` is absent for the same reason in reverse — its only file operand is the
#: root-owned registry constant, pinned below, and its `--arg` values are data.
PATH_VERBS = frozenset(
    {
        "realpath",
        "readlink",
        "cd",
        "pushd",
        "rm",
        "cp",
        "mv",
        "ln",
        "install",
        "mkdir",
        "rmdir",
        "touch",
        "chown",
        "chmod",
        "chgrp",
        "truncate",
        "dd",
        "tee",
        "cat",
        "head",
        "tail",
        "stat",
        "sed",
        "source",
        ".",
    }
)

#: The `test`/`[[` operators that stat a path. `-n`/`-z` and the numeric comparisons are
#: string tests and belong nowhere near this set — including them would flag every
#: `[[ -n $cwd ]]` argument check in the script and train the guard to be ignored.
FILE_TESTS = frozenset(
    (
        "-e",
        "-f",
        "-d",
        "-r",
        "-w",
        "-x",
        "-s",
        "-L",
        "-h",
        "-p",
        "-S",
        "-b",
        "-c",
        "-g",
        "-u",
        "-k",
        "-O",
        "-G",
        "-N",
        "-nt",
        "-ot",
        "-ef",
    )
)

_ASSIGN = re.compile(r"(?:^|[;&|(]\s*|\blocal\s+|\bdeclare\s+|\bexport\s+)([A-Za-z_]\w*)\+?=")
_READ_TARGETS = re.compile(r"\bread\b((?:\s+-\w+)*)((?:\s+[A-Za-z_]\w*)+)")
#: A redirect INTO or OUT OF a file. `<<`/`<<<` (here-doc, here-string) and `>&`/`<&`
#: (fd duplication) are deliberately excluded: neither names a path.
_REDIRECT = re.compile(r"(?<![<>])\d*(?:>>?|<)(?![<>&])\s*([^\s;|&]*)")


def _names_in(fragment: str) -> set[str]:
    """Every ``$NAME`` / ``${NAME…}`` expanded in ``fragment``."""
    return set(re.findall(r"\$\{?([A-Za-z_]\w*)", fragment))


def _segments(line: str) -> list[str]:
    """Split one logical line into the commands it runs."""
    return [part for part in re.split(r"(?:\|\||&&|[;|])", line) if part.strip()]


def _command_names(token: str) -> set[str]:
    """The command word(s) this token could be — including one opened by ``$(``.

    ``real_cwd="$(realpath`` is a token whose command is ``realpath``; reading only the
    bare word would let every command substitution hide behind its assignment.
    """
    names = {Path(token.strip('"')).name}
    for match in re.finditer(r"\$\(\s*([^\s)]+)", token):
        names.add(Path(match.group(1).strip('"')).name)
    return names


def _command_words(segment: str) -> set[str]:
    """Every word this segment runs as a command — including one opened by ``$(``.

    Coarse on purpose: a word that *could* be the command counts, because the cost of
    including one extra is a redundant check and the cost of missing one is a blind spot.
    """
    tokens = segment.split()
    words: set[str] = set()
    first = 0
    while first < len(tokens) and (
        re.fullmatch(r"[A-Za-z_]\w*\+?=\S*", tokens[first]) or tokens[first] in "({"
    ):
        words |= _command_names(tokens[first])  # `x="$(cmd …)"` runs cmd
        first += 1
    if first < len(tokens):
        words |= _command_names(tokens[first])
    for token in tokens[first + 1 :]:
        if "$(" in token:
            words |= _command_names(token)
    return words


def path_operands(segment: str) -> list[str]:
    """The fragments of ``segment`` that name a filesystem path — and only those.

    Precision is the whole value here. A guard that flagged every line *mentioning* an
    untrusted variable would fire on ``id -u "$user"`` and on ``[[ -n $cwd ]]``, and a
    guard that fires on argument validation is one people learn to silence. So each
    source of a path operand is read in its own right:

    * a **redirect**'s target (never a here-string's body or an fd duplication);
    * the operand of a **file test**, and only inside a ``[[``/``[``/``test`` command —
      outside one, ``-u``, ``-r`` and ``-x`` are ordinary flags (``id -u``, ``read -r``,
      ``runuser -u``), not stat calls;
    * everything after a **path-following or path-mutating command**.
    """
    tokens = segment.split()
    operands: list[str] = []
    for match in _REDIRECT.finditer(segment):
        if match.group(1):
            operands.append(match.group(1))
    if "[[" in tokens or "[" in tokens or (tokens and tokens[0] == "test"):
        for index, token in enumerate(tokens):
            if token in FILE_TESTS and index + 1 < len(tokens):
                operands.append(tokens[index + 1])
    for index, token in enumerate(tokens):
        if _command_names(token) & PATH_VERBS:
            operands.extend(tokens[index + 1 :])
            break
    return operands


@dataclass(frozen=True)
class Violation:
    line: int
    text: str
    why: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.why}\n    {self.text}"


def _outside_helper(text: str) -> list[tuple[int, str]]:
    """Every logical line except the helper's own body — it IS the sanctioner.

    Part 1 proves the helper behaviourally, against a real sandbox. Scanning it here
    would only ask whether the gate goes through itself.
    """
    span = _helper_span(text)
    return [
        (lineno, line)
        for lineno, line in logical_lines(text)
        if not (span and span[0] <= lineno <= span[1])
    ]


def untrusted_path_names(text: str) -> set[str]:
    """Every variable carrying a value the caller or the registry chose, to a fixed point.

    Seeded from the two places an untrusted value enters ``wake-runner``: the argv loop's
    ``NAME="$2"`` and the registry lookup's ``read`` targets. Then propagated through
    assignments, because ``alias=$cwd`` must not launder anything.

    ``HELPER`` is what launders: an assignment whose right-hand side is a call to it
    yields a path we confined ourselves, so it — and only it — leaves the set. That is
    the whole point of having one gate rather than a habit.
    """
    lines = _outside_helper(text)
    names: set[str] = set()
    for _, line in lines:
        names.update(re.findall(r"([A-Za-z_]\w*)=(?:\"\$2\"|\$2)(?:\s|$)", line))
        for segment in _segments(line):
            # `read` only where it is the command — otherwise the prose in
            # `die "could not read registry $REGISTRY"` seeds a variable named
            # `registry` that exists nowhere, and a guard that tracks phantoms is one
            # nobody trusts the output of.
            if "read" not in _command_words(segment):
                continue
            for flags, targets in _READ_TARGETS.findall(segment):
                del flags
                names.update(targets.split())
    changed = True
    while changed:
        changed = False
        for _, line in lines:
            if HELPER in line:
                continue
            if not _names_in(line) & names:
                continue
            for assigned in _ASSIGN.findall(line):
                if assigned not in names:
                    names.add(assigned)
                    changed = True
    return names


def scan_call_sites(text: str) -> list[Violation]:
    """Every filesystem operation on an untrusted path that skipped the helper."""
    names = untrusted_path_names(text)
    violations: list[Violation] = []
    for lineno, line in _outside_helper(text):
        if HELPER in line:
            continue
        for segment in _segments(line):
            tainted = {
                name for operand in path_operands(segment) for name in _names_in(operand)
            } & names
            if not tainted:
                continue
            violations.append(
                Violation(lineno, line, f"acts on a path from {sorted(tainted)} without {HELPER}")
            )
            break
    return violations


def _helper_span(text: str) -> tuple[int, int] | None:
    """The 1-based line range of the ``confined_path`` marker block, if present."""
    lines = text.splitlines()
    opening = closing = None
    for index, line in enumerate(lines, start=1):
        if f"# >>> {HELPER} >>>" in line:
            opening = index
        elif f"# <<< {HELPER} <<<" in line:
            closing = index
    if opening is None or closing is None:
        return None
    return opening, closing


# --------------------------------------------------------------------------- #
# What this repo ships, discovered rather than hand-listed.                     #
# --------------------------------------------------------------------------- #


def shipped_files() -> list[Path]:
    """Everything this repo puts on the box or runs there — prose excluded."""
    tracked = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "deploy", "src"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return [
        REPO / name
        for name in filter(None, tracked)
        if (REPO / name).is_file() and not name.endswith(".md")
    ]


def test_shipped_files_are_discovered() -> None:
    names = {p.relative_to(REPO).as_posix() for p in shipped_files()}
    assert "deploy/bin/wake-runner" in names
    assert "deploy/systemd/basecradle-router.service" in names
    assert "src/basecradle_router/wake.py" in names
    assert not any(n.endswith(".md") for n in names)


#: A `/home/` that is not the daemon's own. Built from DAEMON_HOME so the exemption and
#: the constant can never drift apart.
_FOREIGN_HOME = rf"/home/(?!{re.escape(DAEMON_HOME.rsplit('/', 1)[1])}\b)"


def _is_shell(text: str) -> bool:
    return bool(re.match(r"#!.*\b(ba|da|z|k)?sh\b", text.split("\n", 1)[0]))


def _strip_line_comments(text: str) -> str:
    """For unit files and YAML: a ``#`` at the start of a line opens a comment."""
    return "\n".join("" if line.lstrip().startswith("#") else line for line in text.splitlines())


# --------------------------------------------------------------------------- #
# 1. The shipped helper, run against a real sandbox.                            #
# --------------------------------------------------------------------------- #


def _extract(marker: str) -> str:
    """Pull a real function body out of wake-runner, between its marker comments."""
    text = WAKE_RUNNER.read_text()
    match = re.search(
        rf"[ \t]*# >>> {marker} >>>\n(.*?)[ \t]*# <<< {marker} <<<\n", text, re.DOTALL
    )
    assert match, f"{marker} marker block not found in deploy/bin/wake-runner"
    return match.group(1)


def _confine(root: Path | str, path: Path | str) -> subprocess.CompletedProcess[str]:
    """Run the shipped ``confined_path`` over one (root, path) pair. Nothing else."""
    script = textwrap.dedent(
        """\
        set -euo pipefail
        {helper}
        confined_path "clone path" "$1" "$2"
        """
    ).format(helper=_extract(HELPER))
    return subprocess.run(
        ["bash", "-c", script, "bash", str(root), str(path)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A fabricated agent home: a real clone, and every link an agent might plant.

    Cast is fabricated per this repo's convention — Nova Digital (``nova``, AI).
    """
    root = tmp_path / "home" / "nova"
    (root / "repos" / "app").mkdir(parents=True)
    (root / "repos" / "app" / "README").write_text("clone\n", encoding="utf-8")
    (root / "venv" / "bin").mkdir(parents=True)
    wake = root / "venv" / "bin" / "wake"
    wake.write_text("#!/bin/sh\n", encoding="utf-8")
    wake.chmod(0o755)
    (root / "escape").symlink_to(tmp_path / "etc")
    (root / "inside").symlink_to("repos")
    (root / "dangling").symlink_to(root / "nothing-here")
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "shadow").write_text("root:*\n", encoding="utf-8")
    return root


def test_a_real_path_under_the_root_is_returned_verbatim(home: Path) -> None:
    # Verbatim, not resolved: the caller gets back the path it named, so nothing
    # downstream is ever handed a target some other account chose.
    clone = home / "repos" / "app"
    done = _confine(home, clone)
    assert done.returncode == 0, done.stderr
    assert done.stdout == str(clone)


def test_a_symlinked_final_component_is_refused(home: Path) -> None:
    done = _confine(home, home / "escape")
    assert done.returncode == 1
    assert "escape" in done.stderr and "symlink" in done.stderr


def test_a_symlinked_intermediate_component_is_refused_naming_that_component(
    home: Path,
) -> None:
    # The finding is the LINK, not the tail: an operator told "repos/app is a symlink"
    # goes and looks at the wrong entry.
    done = _confine(home, home / "inside" / "app")
    assert done.returncode == 1
    assert "component inside" in done.stderr


def test_a_link_that_points_back_inside_the_root_is_still_refused(home: Path) -> None:
    # The behaviour change from `realpath` + a prefix test, stated as a test: that check
    # ACCEPTED this, because the target lands inside the home. It is still a path the
    # agent can re-point between our look and anyone's use, so we refuse the link itself.
    done = _confine(home, home / "inside")
    assert done.returncode == 1
    assert "symlink" in done.stderr


def test_a_dangling_link_is_refused_as_a_link_not_as_missing(home: Path) -> None:
    # Saying "does not exist" would send an operator to create the very thing we are
    # refusing to follow.
    done = _confine(home, home / "dangling")
    assert done.returncode == 1
    assert "symlink" in done.stderr
    assert "does not exist" not in done.stderr


def test_the_refusal_never_names_the_links_target(home: Path) -> None:
    # The target is the other account's text. Printing it into root's journal is a
    # smaller version of trusting it, and it is never what an operator has to go fix.
    done = _confine(home, home / "escape")
    assert "/etc" not in done.stderr
    assert "shadow" not in done.stderr


@pytest.mark.parametrize(
    "tail",
    ["..", ".", "repos/../repos", "repos/./app"],
    ids=["parent", "dot", "parent-midway", "dot-midway"],
)
def test_dot_segments_are_refused_here_rather_than_delegated(home: Path, tail: str) -> None:
    # Refused by us, never handed to a resolver — delegating is what makes a check a
    # resolution again.
    done = _confine(home, f"{home}/{tail}")
    assert done.returncode == 1
    assert "segment" in done.stderr


@pytest.mark.parametrize(
    "path",
    ["repos/app/", "repos//app", "repos/app/."],
    ids=["trailing-slash", "double-slash", "trailing-dot"],
)
def test_a_non_canonical_path_is_refused(home: Path, path: str) -> None:
    done = _confine(home, f"{home}/{path}")
    assert done.returncode == 1


def test_a_relative_path_is_refused(home: Path) -> None:
    assert _confine(home, "repos/app").returncode == 1


def test_the_root_itself_is_not_under_the_root(home: Path) -> None:
    # `--cwd /home/nova` names the home, not a clone in it. There is nothing to confine.
    assert _confine(home, home).returncode == 1


def test_a_sibling_sharing_the_roots_prefix_is_refused(tmp_path: Path) -> None:
    # `/home/nova-staging` starts with `/home/nova`; a prefix test without the separator
    # would wave it through.
    (tmp_path / "nova").mkdir()
    (tmp_path / "nova-staging" / "repos").mkdir(parents=True)
    done = _confine(tmp_path / "nova", tmp_path / "nova-staging" / "repos")
    assert done.returncode == 1
    assert "is not under" in done.stderr


def test_a_missing_component_is_refused(home: Path) -> None:
    done = _confine(home, home / "repos" / "absent")
    assert done.returncode == 1
    assert "does not exist" in done.stderr


def test_a_file_used_as_a_directory_is_refused(home: Path) -> None:
    done = _confine(home, home / "repos" / "app" / "README" / "deeper")
    assert done.returncode == 1
    assert "is not a directory" in done.stderr


def test_a_symlinked_root_is_refused(tmp_path: Path) -> None:
    # The trusted root must be the one component the confined account cannot swap.
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "repos").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    done = _confine(tmp_path / "link", tmp_path / "link" / "repos")
    assert done.returncode == 1
    assert "not a real directory" in done.stderr


@pytest.mark.parametrize(
    "root_tail", ["/..", "/.", "/../nova", "/./nova", "/nova/", "//nova"], ids=lambda s: s
)
def test_a_non_canonical_root_is_refused(tmp_path: Path, root_tail: str) -> None:
    # The gate checks its own trusted root as strictly as the path: a dot segment there
    # would silently move the confinement somewhere else, and a gate that trusts its
    # caller's arguments is a gate with a door beside it.
    (tmp_path / "nova" / "repos").mkdir(parents=True)
    done = _confine(f"{tmp_path}{root_tail}", tmp_path / "nova" / "repos")
    assert done.returncode == 1
    assert "trusted root" in done.stderr


def test_a_missing_root_is_refused(tmp_path: Path) -> None:
    done = _confine(tmp_path / "absent", tmp_path / "absent" / "repos")
    assert done.returncode == 1
    assert "not a real directory" in done.stderr


def test_a_refusal_changes_nothing_on_disk(home: Path) -> None:
    before = sorted(p.relative_to(home).as_posix() for p in home.rglob("*"))
    for target in ("escape", "inside/app", "..", "repos/absent"):
        assert _confine(home, f"{home}/{target}").returncode == 1
    assert sorted(p.relative_to(home).as_posix() for p in home.rglob("*")) == before


# --------------------------------------------------------------------------- #
# 2. The call sites: the untrusted paths reach the filesystem only through it.   #
# --------------------------------------------------------------------------- #


def test_the_seeds_are_actually_found() -> None:
    """The call-site guard is only meaningful if it knows what it is tracking."""
    names = untrusted_path_names(WAKE_RUNNER.read_text())
    assert {"user", "cwd", "wake_bin"} <= names
    # And the helper's output is NOT in it — laundering is what the one gate buys.
    assert "real_cwd" not in names
    assert "launch_bin" not in names


def shipped_shell() -> list[Path]:
    """Every shell script this repo ships — discovered, never hand-listed.

    Hand-listing is how a recurrence guard stops recurring: the next root-run helper
    would be covered the day someone remembered it, which is the day after it mattered.
    """
    return [
        path
        for path in shipped_files()
        if path.suffix == ".sh" or _is_shell(path.read_text(errors="replace"))
    ]


def test_shipped_shell_is_discovered() -> None:
    names = {p.relative_to(REPO).as_posix() for p in shipped_shell()}
    assert "deploy/bin/wake-runner" in names
    assert "deploy/smoke-test.sh" in names  # root-run, so it is in scope too
    assert len(names) >= 5


@pytest.mark.parametrize("script", shipped_shell(), ids=lambda p: p.relative_to(REPO).as_posix())
def test_no_privileged_path_operation_skips_the_helper(script: Path) -> None:
    """The recurrence guard: new privileged code must go through the one gate.

    Fails when anything the caller chose — ``--cwd``, the registry's ``wake_bin``, and
    everything they flow into — reaches a file test, a path-following command, or a
    redirect without ``confined_path``. Run over every shipped script rather than the
    one that has the problem today, so the *next* root-run helper is covered on the day
    it lands.
    """
    violations = scan_call_sites(script.read_text())
    assert not violations, f"{script.name}: path operations that skipped the gate:\n" + "\n".join(
        str(v) for v in violations
    )


def test_the_call_site_guard_is_not_vacuous() -> None:
    """A positive control: the exact shape the sweep removed must still be caught."""
    sample = textwrap.dedent(
        """\
        #!/usr/bin/env bash
        cwd="$2"
        real_cwd="$(realpath -e -- "$cwd")"
        [[ -d $real_cwd ]] || exit 1
        """
    )
    violations = scan_call_sites(sample)
    assert violations, "the scanner missed a realpath+test on the caller's path"
    assert any("realpath" in v.text for v in violations)


def test_an_alias_cannot_launder_an_untrusted_path() -> None:
    """One hop of renaming must not slip past — taint propagates to a fixed point."""
    sample = textwrap.dedent(
        """\
        #!/usr/bin/env bash
        cwd="$2"
        alias_path="$cwd"
        [[ -x $alias_path ]] || exit 1
        """
    )
    assert scan_call_sites(sample), "renaming the variable laundered it"


def test_single_quoted_and_post_drop_code_is_not_scanned() -> None:
    """The agent's own script runs AFTER the drop, so it is not privileged code."""
    sample = textwrap.dedent(
        """\
        #!/usr/bin/env bash
        cwd="$2"
        AGENT_SCRIPT='
          cd "$1"
          [[ -r $HOME/.config/basecradle/agent.env ]] && echo ok
        '
        runuser -u nova -- /bin/bash -c "$AGENT_SCRIPT" wake-runner "$cwd"
        """
    )
    assert not scan_call_sites(sample)


def test_both_wake_paths_are_confined_to_the_agents_own_home() -> None:
    """The clone and the harness ``wake_bin`` each go through the gate, rooted at /home/<user>."""
    view = "\n".join(line for _, line in logical_lines(WAKE_RUNNER.read_text()))
    calls = re.findall(rf'{HELPER} "[^"]+" "(/home/\$\w+)" "\$(\w+)"', view)
    assert sorted(calls) == [("/home/$user", "cwd"), ("/home/$user", "wake_bin")], calls


def test_the_root_owned_wrappers_resolve_no_path_themselves() -> None:
    """No ``realpath``/``readlink`` in ``deploy/bin/`` — resolving is what we stopped doing.

    A future author reaching for one on an agent path has to reach for the gate instead.
    """
    offenders = {
        script.name: [
            lineno
            for lineno, line in logical_lines(script.read_text())
            if re.search(r"\b(realpath|readlink)\b", line)
        ]
        for script in sorted((REPO / "deploy" / "bin").iterdir())
        if script.is_file() and script.read_text(errors="replace").startswith("#!/usr/bin/env bash")
    }
    assert not {name: at for name, at in offenders.items() if at}, offenders


def test_the_registrys_reader_is_only_ever_given_the_registry() -> None:
    """``jq`` is outside ``PATH_VERBS``, so its file operand is pinned here instead."""
    for _, line in logical_lines(WAKE_RUNNER.read_text()):
        if not re.search(r"\bjq\b", line):
            continue
        assert '"$REGISTRY"' in line or "jq >/dev/null" in line, line


# --------------------------------------------------------------------------- #
# 3. The literals: /home/ appears only as the gate's root, or as the daemon's own.#
# --------------------------------------------------------------------------- #


def agent_home_literals(path: Path) -> list[Violation]:
    """Every ``/home/`` in shipped code that is neither the gate's root nor our own home.

    The literal is worth guarding in its own right, because it is how the class *starts*:
    an author who writes ``/home/$slug/...`` into a new unit, script, or module has
    reached into someone else's directory before any variable is involved for a taint
    scan to follow.
    """
    text = path.read_text(errors="replace")
    if path.suffix == ".py":
        return _python_home_literals(text)
    lines = (
        logical_lines(text)
        if _is_shell(text)
        else list(enumerate(_strip_line_comments(text).splitlines(), start=1))
    )
    violations = []
    for lineno, line in lines:
        if HELPER in line:
            continue  # the gate's own trusted root is the one sanctioned spelling
        if re.search(_FOREIGN_HOME, line):
            violations.append(Violation(lineno, line, "names an agent home outside the gate"))
    return violations


def _python_home_literals(source: str) -> list[Violation]:
    """``/home/`` in a Python **string the program uses**, never in one it only reads.

    Parsed rather than grepped, because this repo's modules explain themselves at length
    and a docstring that *mentions* ``/home/<agent>`` is describing the boundary, not
    crossing it. A guard that fired on prose would be silenced by the first person who
    wrote a good comment. A path the code actually carries is a different thing entirely,
    and ``ast`` tells them apart for free: comments are not nodes, and a docstring is a
    node we can name.
    """
    tree = ast.parse(source)
    prose: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            prose.add(id(first.value))
    return [
        Violation(node.lineno, node.value, "carries an agent home as a path literal")
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in prose
        and re.search(_FOREIGN_HOME, node.value)
    ]


@pytest.mark.parametrize("shipped", shipped_files(), ids=lambda p: p.relative_to(REPO).as_posix())
def test_no_shipped_file_names_an_agent_home_outside_the_gate(shipped: Path) -> None:
    violations = agent_home_literals(shipped)
    assert not violations, "\n".join(str(v) for v in violations)


def test_the_literal_guard_is_not_vacuous(tmp_path: Path) -> None:
    sample = tmp_path / "offender.sh"
    sample.write_text('#!/usr/bin/env bash\nchown root "/home/$user/scratch"\n', encoding="utf-8")
    assert agent_home_literals(sample)
    ours = tmp_path / "ours.sh"
    ours.write_text(
        "#!/usr/bin/env bash\nexec /home/router/.local/bin/uv run x\n", encoding="utf-8"
    )
    assert not agent_home_literals(ours)


def test_the_python_literal_guard_reads_code_and_not_prose(tmp_path: Path) -> None:
    module = tmp_path / "sample.py"
    module.write_text(
        '"""The wake enters /home/nova/repos as that agent."""\n'
        "SCRATCH = '/home/nova/scratch'\n"
        "UV = '/home/router/.local/bin/uv'\n",
        encoding="utf-8",
    )
    violations = _python_home_literals(module.read_text())
    assert [v.line for v in violations] == [2], violations


# --------------------------------------------------------------------------- #
# 4. The CLI's one caller-supplied write directory.                             #
# --------------------------------------------------------------------------- #

MANIFEST = {"subject": "box:ai.basecradle.com", "claims": []}


def test_manifests_are_written_normally(tmp_path: Path) -> None:
    out = tmp_path / "discovery"
    _write_manifests(str(out), [MANIFEST])
    assert list(out.iterdir()), "nothing was written"


def test_a_symlinked_out_dir_is_refused(tmp_path: Path) -> None:
    # The CLI runs as the DAEMON's user, which can write the evidence ledger. A link
    # here would have it clobber whatever that user can reach.
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = tmp_path / "discovery"
    link.symlink_to(real)
    with pytest.raises(ManifestWriteError) as caught:
        _write_manifests(str(link), [MANIFEST])
    assert "symlink" in str(caught.value)
    assert not list(real.iterdir()), "a refusal must write nothing"


def test_a_symlinked_manifest_file_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "discovery"
    out.mkdir()
    target = tmp_path / "evidence.json"
    target.write_text("{}\n", encoding="utf-8")
    from basecradle_router.claims import manifest_filename

    (out / manifest_filename(MANIFEST)).symlink_to(target)
    with pytest.raises(ManifestWriteError) as caught:
        _write_manifests(str(out), [MANIFEST])
    assert "symlink" in str(caught.value)
    assert target.read_text(encoding="utf-8") == "{}\n", "the link's target was written"


def test_o_nofollow_is_the_errno_this_platform_raises(tmp_path: Path) -> None:
    """Pin the errno mapping rather than trust it: Linux says ELOOP, some BSDs EMLINK."""
    target = tmp_path / "target"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError) as caught:
        os.open(str(link), os.O_WRONLY | os.O_NOFOLLOW)
    assert caught.value.errno in (errno.ELOOP, errno.EMLINK)

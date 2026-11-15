"""The argv recorder: run a shipped script with every program it names behind a shim
that writes down its own command line first.

A program's argv is readable by every account on the box (`ps`, `/proc/<pid>/cmdline`)
for as long as it runs, so a secret must never be one of its arguments. The deploy smoke
test broke that rule on every deploy until PR 465 (basecradle/basecradle#853). This is
that PR's recorder, lifted out of `tests/test_smoke_test_enabled_routes.py` so one guard,
`tests/test_no_secret_on_a_command_line.py`, can hold every shipped script to the rule
(basecradle-router#468).

How it sees a command line. Each program the script could run is replaced, on a PATH
directory the test controls, by a bash shim that writes `<name> <args…>` to a record
file of its own and then does whatever the test wants: `exec` the real program (a
passthrough, so the script behaves as it does on the box) or stand in for one that must
never really run here (`runuser`, `systemd-cat`, `curl`). A program the script reaches
by an ABSOLUTE path is not on PATH, so the driver points that path at a shim of its own
(`place`), and the guard keeps an inventory of every absolute path a driven script
names. A record is written with bash builtins only: a shim that ran a program to record
itself would run that program's shim.

What it sees through: a real binary that starts another by name (`env`, `xargs`) looks
it up on the same PATH, so the program it starts is recorded too. What it cannot see: a
value handed to a shell builtin or function, which is no command line at all, and a
program some real binary starts by an absolute path of its own.
"""

from __future__ import annotations

import base64
import functools
import os
import re
import urllib.parse
from collections.abc import Iterable
from pathlib import Path

#: Where a passthrough's real program is looked up: the directories every driver's PATH
#: carries after the shims, so a shim never makes reachable a program the script could not
#: already reach. Exact names from a listing, never `which`: on a case-insensitive
#: filesystem `which("Bash")` would find /bin/bash.
SYSTEM_DIRS = ("/bin", "/usr/bin")

#: Every shim runs under bash, so bash and sh are never shimmed.
NEVER_SHIMMED = frozenset({"bash", "sh"})


@functools.cache
def _system_programs() -> dict[str, str]:
    found: dict[str, str] = {}
    for directory in SYSTEM_DIRS:
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            path = f"{directory}/{name}"
            if name not in found and os.access(path, os.X_OK) and not os.path.isdir(path):
                found[name] = path
    return found


class ArgvRecorder:
    """One recording PATH directory and the one log directory every shim in it writes to."""

    def __init__(self, root: Path) -> None:
        self.bindir = root / "bin"
        self.bindir.mkdir(parents=True, exist_ok=True)
        self.log = root / "argv"
        self.log.mkdir(exist_ok=True)
        self._bodies: dict[str, str] = {}

    def fake(self, name: str, body: str) -> None:
        """Stand ``name`` in with ``body`` (bash), recorded like every other program."""
        self._bodies[name] = body

    def passthrough(self, *texts: str) -> None:
        """Shim every system program any of ``texts`` names, as the real one.

        Every lowercase word is a candidate, comments included: a word that names no
        program, or one the script never runs, costs a shim nobody calls. One the script
        does run is the real binary behind a recording, so it behaves as on the box.
        """
        system = _system_programs()
        words = {word for text in texts for word in re.findall(r"[a-z][a-z0-9_.-]*", text)}
        for word in sorted(words):
            if word in system and word not in self._bodies and word not in NEVER_SHIMMED:
                self._bodies[word] = f'exec "{system[word]}" "$@"\n'

    def has(self, name: str) -> bool:
        """Is ``name`` already shimmed on the recorder's PATH?"""
        return name in self._bodies

    def place(self, path: Path, body: str) -> None:
        """Write a recording shim at an absolute ``path`` the script runs by that path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write(path, path.name, body)

    def install(self) -> str:
        """Write every PATH shim; return the directory to put first on the script's PATH."""
        for name, body in self._bodies.items():
            self._write(self.bindir / name, name, body)
        return str(self.bindir)

    def _write(self, path: Path, name: str, body: str) -> None:
        # The name is written literally rather than read from $0, so a placed shim records
        # the name the driver gave it, whatever path it lives at. One file per record, never
        # one shared log: the members of a pipeline run at once, and a record several KB
        # long (a `bash -c` body) would not be one write(2), so appends could interleave.
        # noclobber makes the create exclusive. Only a clash (the name exists) draws another
        # name; any other failure to write the record stops the program, loudly, since a
        # command line that was not recorded must never read as one that carried nothing.
        record = (
            f'_rec="{self.log}/$BASHPID-$RANDOM$RANDOM"\n'
            'until (set -C; printf "%s" "' + name + ' $*" >"$_rec") 2>/dev/null; do\n'
            "\tif [[ ! -e $_rec ]]; then\n"
            '\t\techo "argv recorder: cannot write $_rec" >&2\n'
            "\t\texit 98\n"
            "\tfi\n"
            f'\t_rec="{self.log}/$BASHPID-$RANDOM$RANDOM"\n'
            "done\n"
            "unset _rec\n"
        )
        path.write_text("#!/usr/bin/env bash\n" + record + body)
        path.chmod(0o755)

    def lines(self) -> list[str]:
        """Every recorded command line, each whole (an argument may span lines)."""
        return recorded(self.log.parent)

    def ran(self, name: str) -> bool:
        """Did a program called ``name`` run at all?"""
        return any(line == name or line.startswith(f"{name} ") for line in self.lines())

    def leaks(self, secrets: Iterable[str]) -> list[str]:
        """Every recorded command line that carries any of ``secrets``, raw or encoded.

        An encoding is no protection (`xxd -p`, `base64` and percent-encoding reverse in
        one step), so each secret is looked for in every form a script would plausibly
        hand a program.
        """
        return leaks_in(self.lines(), secrets)


def recorded(root: Path) -> list[str]:
    """Every command line an :class:`ArgvRecorder` rooted at ``root`` recorded."""
    log = root / "argv"
    if not log.is_dir():
        return []
    return [path.read_text() for path in sorted(log.iterdir())]


def leaks_in(lines: Iterable[str], secrets: Iterable[str]) -> list[str]:
    """Every one of ``lines`` that carries any of ``secrets``, raw or encoded."""
    wanted = {form for secret in secrets if secret for form in _forms(secret)}
    return [line for line in lines if any(form in line for form in wanted)]


def _forms(secret: str) -> set[str]:
    """``secret`` as a script would plausibly hand it to a program.

    Base64 is looked for at all three byte alignments, so a secret behind a prefix (a Basic
    `user:<token>`) is still found: the characters that encode only the secret's bytes are
    the same whatever came before, once the first and last group are trimmed.
    """
    raw = secret.encode()
    forms = {secret, raw.hex(), raw.hex().upper(), urllib.parse.quote(secret, safe="")}
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        forms.add(encode(raw).decode())
        for shift in range(3):
            core = encode(b"\0" * shift + raw).decode()[4 if shift else 0 : -4]
            if len(core) >= 16:
                forms.add(core)
    return forms

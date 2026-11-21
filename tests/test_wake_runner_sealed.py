"""``harness_wake_bin`` — the gate a harness agent's ``wake_bin`` crosses (#535), run offline.

A harness agent's environment is moving out of its home, to
``/opt/basecradle-harness/venv/<os_user>/``, under two directories root owns, so the agent can
read and run it but never write, rename or replace it. ``wake-runner`` accepts a ``wake_bin``
there, or in the agent's home only while no sealed tree exists, and nowhere else.

These tests run the EXACT shipped functions: ``confined_path`` and ``harness_wake_bin`` are
extracted from ``deploy/bin/wake-runner`` between their marker comments and run in a clean
bash against a fabricated sandbox, with the sandbox standing in for ``/opt/basecradle-harness``
and ``/home/<user>``. Nothing is duplicated, so a regression in the real script fails here.
What only root can show (a parent root does not own, a tree root built for the agent's group)
is proven by ``tests/test_sealed_wake_root.py``, which runs the whole script as root.

The account is ``nobody``, which every Linux and macOS box has: it owns nothing in the
sandbox, which is what a sealed tree's agent is. Where a test needs the agent to own the tree,
the agent is the account running the suite. Fabricated cast otherwise (Nova Digital).
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

import proof_wheel

WAKE_RUNNER = Path(__file__).resolve().parents[1] / "deploy" / "bin" / "wake-runner"
AGENT = "nobody"
WAKE = "bin/basecradle-harness-wake"

pytestmark = [
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash runs the shipped gate"),
    pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership is under test"),
]


def _extract(marker: str) -> str:
    text = WAKE_RUNNER.read_text()
    match = re.search(
        rf"[ \t]*# >>> {marker} >>>\n(.*?)[ \t]*# <<< {marker} <<<\n", text, re.DOTALL
    )
    assert match, f"{marker} marker block not found in deploy/bin/wake-runner"
    return match.group(1)


@dataclass
class Box:
    """A fabricated box: ``base`` stands in for ``/opt/basecradle-harness``."""

    base: Path
    home_root: Path

    def tree(self, user: str = AGENT) -> Path:
        return self.base / "venv" / user

    def home(self, user: str = AGENT) -> Path:
        return self.home_root / user

    def sealed_bin(self, user: str = AGENT) -> Path:
        return self.tree(user) / WAKE

    def home_bin(self, user: str = AGENT) -> Path:
        return self.home(user) / "venv" / WAKE

    def gate(self, path: Path | str, user: str = AGENT) -> subprocess.CompletedProcess[str]:
        script = textwrap.dedent(
            """\
            set -euo pipefail
            {confined}
            {gate}
            harness_wake_bin "wake_bin" "$1" "$2" "$3" "$4"
            """
        ).format(confined=_extract("confined_path"), gate=_extract("harness_wake_bin"))
        return subprocess.run(
            ["bash", "-c", script, "bash", str(self.home(user)), str(self.base), user, str(path)],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin"},
            check=False,
        )


def _script(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _build_tree(box: Box, user: str = AGENT, body: str = "#!/bin/sh\nexit 0\n") -> None:
    """A minimal sealed tree: the wake script, and ``bin/python`` as a venv has it, a link."""
    _script(box.sealed_bin(user), body)
    (box.tree(user) / "bin" / "python").symlink_to("/bin/sh")
    for entry in (box.tree(user), box.tree(user) / "bin"):
        entry.chmod(0o755)


@pytest.fixture
def box(tmp_path: Path) -> Box:
    base = tmp_path / "opt" / "basecradle-harness"
    (base / "venv").mkdir(parents=True)
    for entry in (base, base / "venv"):
        entry.chmod(0o755)
    made = Box(base=base, home_root=tmp_path / "home")
    _script(made.home_bin())
    return made


def _me() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name


# --------------------------------------------------------------------------- #
# The two places it may be.                                                     #
# --------------------------------------------------------------------------- #


def test_a_sealed_wake_bin_that_passes_every_check_is_returned_verbatim(box: Box) -> None:
    _build_tree(box)
    done = box.gate(box.sealed_bin())
    assert done.returncode == 0, done.stderr
    assert done.stdout == str(box.sealed_bin())


def test_the_home_path_is_still_accepted_for_an_unsealed_agent(box: Box) -> None:
    done = box.gate(box.home_bin())
    assert done.returncode == 0, done.stderr
    assert done.stdout == str(box.home_bin())


def test_a_path_in_neither_place_is_refused(box: Box) -> None:
    done = box.gate("/usr/bin/env")
    assert done.returncode == 1
    assert "is under neither" in done.stderr


def test_the_home_path_is_still_walked_by_the_component_gate(box: Box) -> None:
    # Unsealed is today's rule, unchanged: a link in the home's venv is refused, not followed.
    shutil.move(box.home() / "venv", box.home() / "real-venv")
    (box.home() / "venv").symlink_to(box.home() / "real-venv")
    done = box.gate(box.home_bin())
    assert done.returncode == 1
    assert "symlink" in done.stderr


# --------------------------------------------------------------------------- #
# Sealed means sealed: no fall back to the home in either wrong order.          #
# --------------------------------------------------------------------------- #


def test_the_home_path_is_refused_once_the_sealed_tree_exists(box: Box) -> None:
    # Tree built, registry not flipped: nothing trusted follows the home path any more.
    _build_tree(box)
    done = box.gate(box.home_bin())
    assert done.returncode == 1
    assert f"{AGENT} is sealed" in done.stderr
    assert "point the registry at the sealed tree" in done.stderr


@pytest.mark.parametrize("shape", ["empty-directory", "file", "dangling-link"])
def test_any_entry_at_the_trees_name_seals_the_agent(box: Box, shape: str) -> None:
    # Its existence as ANYTHING is the statement: only root can create an entry there.
    tree = box.tree()
    if shape == "empty-directory":
        tree.mkdir()
    elif shape == "file":
        tree.write_text("", encoding="utf-8")
    else:
        tree.symlink_to(box.base / "nothing-here")
    done = box.gate(box.home_bin())
    assert done.returncode == 1
    assert "is sealed" in done.stderr


def test_a_sealed_registry_with_no_tree_is_refused_never_sent_home(box: Box) -> None:
    # Registry flipped, tree missing: a named refusal, and the home binary (present and
    # healthy here) is not what it falls back to.
    done = box.gate(box.sealed_bin())
    assert done.returncode == 1
    assert "is not a real directory" in done.stderr
    assert done.stdout == ""


# --------------------------------------------------------------------------- #
# The two root-owned parents.                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("which", ["base", "venv"])
def test_a_symlinked_parent_is_refused(box: Box, tmp_path: Path, which: str) -> None:
    _build_tree(box)
    parent = box.base if which == "base" else box.base / "venv"
    real = tmp_path / f"real-{which}"
    shutil.move(parent, real)
    parent.symlink_to(real)
    done = box.gate(box.sealed_bin())
    assert done.returncode == 1
    assert f"sealed parent {parent} is not a real directory" in done.stderr


@pytest.mark.parametrize(
    ("which", "mode"),
    [("base", 0o775), ("base", 0o757), ("venv", 0o775), ("venv", 0o757)],
    ids=["base-group", "base-other", "venv-group", "venv-other"],
)
def test_a_parent_writable_by_group_or_others_is_refused(box: Box, which: str, mode: int) -> None:
    _build_tree(box)
    parent = box.base if which == "base" else box.base / "venv"
    parent.chmod(mode)
    done = box.gate(box.sealed_bin())
    assert done.returncode == 1
    assert f"{parent} is writable by its group or by others" in done.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root owns /usr, so this cannot fail as root")
def test_a_parent_root_does_not_own_is_refused() -> None:
    # The gate's `-O` is the effective uid's, root's in the real script. Run unprivileged,
    # a root-owned directory is "not ours", which is the same test from the other side; the
    # root suite proves the real direction.
    done = Box(base=Path("/usr"), home_root=Path("/nonexistent")).gate("/usr/venv/nobody/x")
    assert done.returncode == 1
    assert "sealed parent /usr is not owned by root" in done.stderr


# --------------------------------------------------------------------------- #
# The agent's own tree, and no other.                                           #
# --------------------------------------------------------------------------- #


def test_another_users_tree_is_refused(box: Box) -> None:
    _build_tree(box)
    _build_tree(box, user="nova")
    done = box.gate(box.sealed_bin("nova"))
    assert done.returncode == 1
    assert f"is not under {box.tree()}" in done.stderr


def test_a_look_alike_sibling_tree_is_refused(box: Box) -> None:
    _build_tree(box)
    _build_tree(box, user=f"{AGENT}2")
    done = box.gate(box.sealed_bin(f"{AGENT}2"))
    assert done.returncode == 1
    assert "is not under" in done.stderr


def test_an_escape_with_dot_dot_is_refused(box: Box) -> None:
    _build_tree(box)
    _build_tree(box, user="nova")
    done = box.gate(f"{box.tree()}/../nova/{WAKE}")
    assert done.returncode == 1
    assert "has a .. segment" in done.stderr


def test_a_home_path_that_escapes_with_dot_dot_is_refused(box: Box) -> None:
    done = box.gate(f"{box.home()}/../nova/venv/{WAKE}")
    assert done.returncode == 1
    assert "segment" in done.stderr


@pytest.mark.parametrize("component", ["tree", "bin", "binary"])
def test_a_symlink_anywhere_in_the_tree_is_refused(
    box: Box, tmp_path: Path, component: str
) -> None:
    _build_tree(box)
    target = {"tree": box.tree(), "bin": box.tree() / "bin", "binary": box.sealed_bin()}[component]
    real = tmp_path / f"real-{component}"
    shutil.move(target, real)
    target.symlink_to(real)
    done = box.gate(box.sealed_bin())
    assert done.returncode == 1
    assert "symlink" in done.stderr or "is not a real directory" in done.stderr


def test_a_tree_the_agent_owns_is_refused(tmp_path: Path) -> None:
    # A misbuilt seal: the account running the suite owns the tree, and is the agent.
    me = _me()
    base = tmp_path / "opt" / "basecradle-harness"
    (base / "venv").mkdir(parents=True)
    made = Box(base=base, home_root=tmp_path / "home")
    _build_tree(made, user=me)
    done = made.gate(made.sealed_bin(me), user=me)
    assert done.returncode == 1
    assert f"is owned by {me}; a sealed tree is never the agent's own" in done.stderr


@pytest.mark.parametrize("component", ["tree", "bin", "binary"])
def test_an_entry_in_the_tree_writable_by_group_or_others_is_refused(
    box: Box, component: str
) -> None:
    _build_tree(box)
    target = {"tree": box.tree(), "bin": box.tree() / "bin", "binary": box.sealed_bin()}[component]
    target.chmod(0o775)
    done = box.gate(box.sealed_bin())
    assert done.returncode == 1
    assert f"{target} is writable by its group or by others" in done.stderr


# --------------------------------------------------------------------------- #
# Set-id programs, and the interpreter the `#!` line names.                     #
# --------------------------------------------------------------------------- #


def _set_bits(path: Path, bits: int) -> None:
    path.chmod(stat.S_IMODE(path.stat().st_mode) | bits)
    if not stat.S_IMODE(path.stat().st_mode) & bits:
        pytest.skip(f"this filesystem will not set {oct(bits)} on {path} for this account")


@pytest.mark.parametrize("bits", [stat.S_ISUID, stat.S_ISGID], ids=["setuid", "setgid"])
def test_a_set_id_binary_is_refused(box: Box, bits: int) -> None:
    _build_tree(box)
    _set_bits(box.sealed_bin(), bits)
    done = box.gate(box.sealed_bin())
    assert done.returncode == 1
    assert "binary" in done.stderr and "is setuid or setgid" in done.stderr


@pytest.mark.parametrize("bits", [stat.S_ISUID, stat.S_ISGID], ids=["setuid", "setgid"])
def test_a_set_id_interpreter_is_refused_through_its_link(box: Box, bits: int) -> None:
    # A venv's `bin/python` is a link; the set-id bit that matters is on the file it names.
    _build_tree(box)
    real = box.base.parent / "python3.12"
    _script(real)
    _set_bits(real, bits)
    (box.tree() / "bin" / "python").unlink()
    (box.tree() / "bin" / "python").symlink_to(real)
    box.sealed_bin().write_text(f"#!{box.tree()}/bin/python\nimport sys\n", encoding="utf-8")
    done = box.gate(box.sealed_bin())
    assert done.returncode == 1
    assert "interpreter" in done.stderr and "is setuid or setgid" in done.stderr


def test_a_directorys_setgid_bit_on_the_path_passes(box: Box) -> None:
    # How the tree hands the agent's group down. Expected, and not a finding.
    _build_tree(box)
    for directory in (box.tree(), box.tree() / "bin"):
        _set_bits(directory, stat.S_ISGID)
    done = box.gate(box.sealed_bin())
    assert done.returncode == 0, done.stderr


def test_an_interpreter_named_through_env_is_refused(box: Box) -> None:
    _build_tree(box, body="#!/usr/bin/env python3\nimport sys\n")
    done = box.gate(box.sealed_bin())
    assert done.returncode == 1
    assert "resolves it off the agent's own PATH" in done.stderr


def test_a_relative_interpreter_is_refused(box: Box) -> None:
    # The kernel would resolve it against the wake's working directory: the agent's clone.
    _build_tree(box, body="#!bin/python\nimport sys\n")
    done = box.gate(box.sealed_bin())
    assert done.returncode == 1
    assert "is not an absolute path" in done.stderr


def test_a_missing_interpreter_is_refused_by_name(box: Box) -> None:
    _build_tree(box, body=f"#!{box.tree()}/bin/python3.12\nimport sys\n")
    done = box.gate(box.sealed_bin())
    assert done.returncode == 1
    assert "does not exist" in done.stderr


def test_a_shebang_with_space_and_an_argument_is_read_as_its_interpreter(box: Box) -> None:
    _build_tree(box, body="#! /bin/sh -e\nexit 0\n")
    done = box.gate(box.sealed_bin())
    assert done.returncode == 0, done.stderr


def test_a_binary_with_no_shebang_needs_no_interpreter(box: Box) -> None:
    _build_tree(box, body="\x7fELF\x02\x01\x01\x00")
    done = box.gate(box.sealed_bin())
    assert done.returncode == 0, done.stderr


# --------------------------------------------------------------------------- #
# A refusal changes nothing.                                                    #
# --------------------------------------------------------------------------- #


def test_a_refusal_changes_nothing_on_disk(box: Box) -> None:
    _build_tree(box)
    before = sorted(
        (p.relative_to(box.base.parent).as_posix(), p.lstat().st_mode)
        for p in box.base.parent.rglob("*")
    )
    for path in (box.home_bin(), "/usr/bin/env", f"{box.tree()}/../x", box.sealed_bin("nova")):
        assert box.gate(path).returncode == 1
    after = sorted(
        (p.relative_to(box.base.parent).as_posix(), p.lstat().st_mode)
        for p in box.base.parent.rglob("*")
    )
    assert after == before


# --------------------------------------------------------------------------- #
# A REAL environment: uv's venv, uv's console script, uv's interpreter link.    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def real_tree(box: Box, tmp_path: Path) -> Path:
    """The agent's sealed tree, built by ``uv venv`` + ``uv pip install`` at its final name."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv builds the real environment")
    script = proof_wheel.build_venv(
        uv,
        sys.executable,
        box.tree(),
        proof_wheel.build_wheel(tmp_path),
        tmp_path / "uv-cache",
    )
    assert script == box.sealed_bin()
    return script


def test_a_real_venvs_console_script_passes_the_gate(box: Box, real_tree: Path) -> None:
    # The shape that matters, asserted before the gate is asked: `bin/python` IS a link, and
    # the script runs it, by its `#!` line or (for a path over 127 bytes, as a long temporary
    # directory can be) by uv's `#!/bin/sh` trampoline. The walk covers the script's own path,
    # which holds no link; the interpreter is judged by the file the link finally names.
    python = box.tree() / "bin" / "python"
    assert python.is_symlink()
    lines = real_tree.read_text().splitlines()
    assert lines[0] == f"#!{python}" or (lines[0] == "#!/bin/sh" and f"'{python}'" in lines[1])
    assert not real_tree.is_symlink()
    done = box.gate(real_tree)
    assert done.returncode == 0, done.stderr
    assert done.stdout == str(real_tree)


def test_a_real_venvs_interpreter_link_re_pointed_at_a_writable_file_is_refused(
    box: Box, real_tree: Path, tmp_path: Path
) -> None:
    writable = tmp_path / "python-anyone-can-write"
    _script(writable)
    writable.chmod(0o777)
    python = box.tree() / "bin" / "python"
    python.unlink()
    python.symlink_to(writable)
    done = box.gate(real_tree)
    assert done.returncode == 1
    assert "or writable by its group or by others" in done.stderr


def test_a_real_venvs_interpreter_link_dangling_is_refused(box: Box, real_tree: Path) -> None:
    python = box.tree() / "bin" / "python"
    python.unlink()
    python.symlink_to(box.base / "no-such-python")
    done = box.gate(real_tree)
    assert done.returncode == 1
    assert "does not exist" in done.stderr


def test_a_tree_with_no_interpreter_of_its_own_is_refused(box: Box) -> None:
    _build_tree(box)
    (box.tree() / "bin" / "python").unlink()
    done = box.gate(box.sealed_bin())
    assert done.returncode == 1
    assert "tree interpreter" in done.stderr and "does not exist" in done.stderr


def test_a_sh_trampoline_does_not_vouch_for_the_python_it_runs(box: Box, tmp_path: Path) -> None:
    # uv's form for a long path: `#!/bin/sh` passes on its own, so the tree's `bin/python`,
    # which the trampoline execs, is judged too.
    trampoline = f"#!/bin/sh\n'''exec' '{box.tree()}/bin/python' \"$0\" \"$@\"\n' '''\n"
    _build_tree(box, body=trampoline)
    writable = tmp_path / "python-anyone-can-write"
    _script(writable)
    writable.chmod(0o777)
    (box.tree() / "bin" / "python").unlink()
    (box.tree() / "bin" / "python").symlink_to(writable)
    done = box.gate(box.sealed_bin())
    assert done.returncode == 1
    assert "tree interpreter" in done.stderr
    assert "or writable by its group or by others" in done.stderr

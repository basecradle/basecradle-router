"""``deploy/bin/claude-stream-translate`` — what it will load, and how it reads (router#481).

It runs inside a builder's own environment, credentials loaded, so it loads exactly three source
files and only when every component of each path is owned by root alone. Here the owner is this
test's own account and the anchor its ``tmp_path``, which is the same rule with the trusted root
moved: the box's anchor is ``/`` and its owner uid 0, pinned at the end of this file.
"""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "claude-stream-translate"
SRC = ROOT / "src"
FIXTURES = Path(__file__).parent / "fixtures" / "claude_stream"
OWNER = os.getuid()


def _launcher():
    loader = importlib.machinery.SourceFileLoader("claude_stream_translate", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ct = _launcher()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of the three source files, laid out as the box lays them out, owned by us alone."""
    src = tmp_path / "src"
    package = src / ct.PACKAGE
    package.mkdir(parents=True)
    for name in ct.MODULES:
        shutil.copy(SRC / ct.PACKAGE / f"{name}.py", package / f"{name}.py")
        (package / f"{name}.py").chmod(0o644)
    for directory in (tmp_path, src, package):
        directory.chmod(0o755)
    return src


@pytest.fixture(autouse=True)
def _restore_modules():
    """``load`` registers a package in ``sys.modules``; put the real one back after each test."""
    saved = {k: v for k, v in sys.modules.items() if k.split(".")[0] == ct.PACKAGE}
    yield
    for key in [k for k in sys.modules if k.split(".")[0] == ct.PACKAGE]:
        del sys.modules[key]
    sys.modules.update(saved)


def load(src: Path):
    return ct.load(str(src), anchor=str(src.parent), owner=OWNER)


# --- what it loads ---------------------------------------------------------------------------


def test_it_loads_the_translator_from_the_three_verified_files(tree: Path) -> None:
    module = load(tree)
    assert module.__name__ == "basecradle_router.claude_stream"
    assert module.__file__ == f"{tree}/basecradle_router/claude_stream.py"
    assert sys.modules["basecradle_router"].__path__ == []
    assert hasattr(module, "Translator")


def test_the_modules_are_the_ones_the_repository_ships() -> None:
    for name in ct.MODULES:
        assert (SRC / ct.PACKAGE / f"{name}.py").is_file()


def test_the_three_modules_import_only_the_standard_library_and_each_other() -> None:
    # The package it builds has no search path, so any other import fails the load and the wake
    # loses its telemetry. This pins the surface before a deploy can find out.
    allowed = set(sys.stdlib_module_names) | {f"{ct.PACKAGE}.{name}" for name in ct.MODULES}
    for name in ct.MODULES:
        tree = ast.parse((SRC / ct.PACKAGE / f"{name}.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                found = [
                    f"{base}.{alias.name}" if base == ct.PACKAGE else base for alias in node.names
                ]
            else:
                continue
            for module in found:
                top = module.split(".")[0]
                assert module in allowed or (top != ct.PACKAGE and top in allowed), (name, module)


def test_a_fourth_module_cannot_be_imported_by_accident(tree: Path) -> None:
    target = tree / ct.PACKAGE / "claude_stream.py"
    target.write_text(target.read_text() + "\nimport basecradle_router.pipeline\n")
    with pytest.raises(ImportError):
        load(tree)


def test_a_symlinked_file_is_refused(tree: Path, tmp_path: Path) -> None:
    target = tree / ct.PACKAGE / "logfmt.py"
    moved = tmp_path / "elsewhere.py"
    target.rename(moved)
    target.symlink_to(moved)
    with pytest.raises(ct.Refused, match="cannot be opened without following a link"):
        load(tree)


def test_a_symlinked_directory_is_refused(tree: Path, tmp_path: Path) -> None:
    package = tree / ct.PACKAGE
    real = tmp_path / "real-package"
    package.rename(real)
    package.symlink_to(real)
    with pytest.raises(ct.Refused, match="is a symlink"):
        load(tree)


@pytest.mark.parametrize("mode", [0o664, 0o646])
def test_a_file_someone_else_could_write_is_refused(tree: Path, mode: int) -> None:
    (tree / ct.PACKAGE / "anthropic_rates.py").chmod(mode)
    with pytest.raises(ct.Refused, match="writable by its group or by others"):
        load(tree)


def test_a_directory_someone_else_could_write_is_refused(tree: Path) -> None:
    (tree / ct.PACKAGE).chmod(0o775)
    try:
        with pytest.raises(ct.Refused, match="writable by its group or by others"):
            load(tree)
    finally:
        (tree / ct.PACKAGE).chmod(0o755)


def test_a_file_owned_by_anyone_but_the_owner_is_refused(tree: Path) -> None:
    with pytest.raises(ct.Refused, match="is owned by uid"):
        ct.load(str(tree), anchor=str(tree.parent), owner=OWNER + 1)


@pytest.mark.parametrize("tail", ["../src/basecradle_router/logfmt.py", "./logfmt.py", ""])
def test_a_non_canonical_path_is_refused(tree: Path, tail: str) -> None:
    with pytest.raises(ct.Refused):
        ct.read_trusted(f"{tree}/{tail}", anchor=str(tree.parent), owner=OWNER)


def test_a_path_outside_the_anchor_is_refused(tree: Path) -> None:
    with pytest.raises(ct.Refused, match="is not under"):
        ct.read_trusted("/etc/hostname", anchor=str(tree.parent), owner=OWNER)


def test_a_directory_named_as_a_file_is_refused(tree: Path) -> None:
    with pytest.raises(ct.Refused, match="not a regular file"):
        ct.read_trusted(str(tree / ct.PACKAGE), anchor=str(tree.parent), owner=OWNER)


def test_the_box_constants_are_root_and_slash() -> None:
    # The one place this file's owner and anchor are not the test's own.
    assert ct.APP_SRC == "/opt/basecradle-router/app/src"
    assert ct.load.__kwdefaults__ == {"anchor": "/", "owner": 0}
    assert ct.read_trusted.__kwdefaults__ == {"anchor": "/", "owner": 0}
    assert SCRIPT.read_text().splitlines()[0] == "#!/usr/bin/python3 -I"


def test_unloadable_it_exits_70_and_says_why() -> None:
    if Path(ct.APP_SRC).exists():
        pytest.skip("this host carries a deployed router tree")
    done = subprocess.run(
        [sys.executable, "-I", str(SCRIPT)],
        input=(FIXTURES / "tools.jsonl").read_bytes(),
        capture_output=True,
        timeout=30,
    )
    assert done.returncode == ct.EXIT_UNLOADABLE
    assert done.stdout == b""
    assert b"not loading the translator" in done.stderr
    assert b"the reply is kept" in done.stderr


# --- how it reads ----------------------------------------------------------------------------


def _translate(tree: Path, read_fd: int, **kwargs) -> tuple[int, list[str]]:
    module = load(tree)
    read_out, write_out = os.pipe()
    try:
        code = ct.run(module, read_fd, write_out, received=[], environ={}, **kwargs)
    finally:
        os.close(write_out)
    with os.fdopen(read_out, "rb") as out:
        return code, out.read().decode().splitlines()


def test_it_translates_its_input_line_by_line(tree: Path) -> None:
    with open(FIXTURES / "tools.jsonl", "rb") as stream:
        code, lines = _translate(tree, stream.fileno())
    assert code == 0
    assert lines[0].startswith("INFO llm shadow provider=anthropic ")
    assert "Done: the work is finished." in lines


def test_a_line_of_megabytes_is_one_line(tree: Path, tmp_path: Path) -> None:
    # One large tool result is one JSON line, arriving in many reads.
    big = tmp_path / "big.jsonl"
    payload = {"type": "user", "message": {"content": "x" * 2_000_000}}
    big.write_text(json.dumps(payload) + "\n" + (FIXTURES / "tools.jsonl").read_text())
    with open(big, "rb") as stream:
        code, lines = _translate(tree, stream.fileno())
    assert code == 0 and "Done: the work is finished." in lines


def test_a_stream_whose_writer_is_gone_ends_once_the_pipe_is_quiet(tree: Path, monkeypatch) -> None:
    # A leftover process holding the write end must not keep the translator reading forever.
    monkeypatch.setattr(ct, "QUIET", 0.05)
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, (FIXTURES / "stopped.jsonl").read_bytes())
        started = time.monotonic()
        code, lines = _translate(tree, read_fd, writer_alive=lambda: False)
        assert time.monotonic() - started < 5
    finally:
        os.close(write_fd)  # held open throughout, as a leftover would hold it
        os.close(read_fd)
    assert code == 0
    assert lines[-1].startswith("INFO Claude Code's stream ended without a result")


def test_a_live_writer_keeps_it_reading_through_a_quiet_spell(tree: Path, monkeypatch) -> None:
    monkeypatch.setattr(ct, "QUIET", 0.05)
    read_fd, write_fd = os.pipe()
    asked = []

    def alive() -> bool:
        asked.append(1)
        if len(asked) == 3:
            os.write(write_fd, (FIXTURES / "tools.jsonl").read_bytes())
            os.close(write_fd)
        return True

    code, lines = _translate(tree, read_fd, writer_alive=alive)
    os.close(read_fd)
    assert code == 0 and len(asked) >= 3
    assert "Done: the work is finished." in lines


def test_pid_alive_answers_for_a_real_and_a_vanished_process() -> None:
    assert ct.pid_alive(os.getpid())()
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not ct.pid_alive(child.pid)()


def test_a_journal_that_refuses_writes_stops_the_writing_never_the_reading(tree: Path) -> None:
    module = load(tree)
    read_out, write_out = os.pipe()
    os.close(read_out)  # the journal is gone: every write is EPIPE
    with open(FIXTURES / "tools.jsonl", "rb") as stream:
        try:
            assert ct.run(module, stream.fileno(), write_out, received=[], environ={}) == 0
            assert stream.read() == b""  # read to the end, so the writer upstream never blocked
        finally:
            os.close(write_out)


#: A child that loads the translator from `tree` exactly as main() loads it from the box.
CHILD = """
import importlib.machinery, importlib.util, os, sys
loader = importlib.machinery.SourceFileLoader("ct", sys.argv[1])
spec = importlib.util.spec_from_loader("ct", loader)
ct = importlib.util.module_from_spec(spec)
loader.exec_module(ct)
received = ct.drain_through_signals()
module = ct.load(sys.argv[2], anchor=os.path.dirname(sys.argv[2]), owner=os.getuid())
print("loaded", flush=True)
sys.exit(ct.run(module, 0, 1, received=received, environ={}))
"""


def test_a_stops_sigterm_is_recorded_and_the_stream_is_read_to_its_end(tree: Path) -> None:
    lines = (FIXTURES / "tools.jsonl").read_bytes().splitlines(keepends=True)
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", CHILD, str(SCRIPT), str(tree)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert child.stdout.readline() == b"loaded\n"
    half = len(lines) // 2
    child.stdin.write(b"".join(lines[:half]))
    child.stdin.flush()
    time.sleep(0.2)
    child.send_signal(signal.SIGTERM)
    time.sleep(0.2)
    assert child.poll() is None, "a SIGTERM must not end the translator before its stream does"
    child.stdin.write(b"".join(lines[half:]))
    child.stdin.close()
    out = child.stdout.read().decode()
    assert child.wait(timeout=30) == 0, child.stderr.read()
    assert "Done: the work is finished." in out
    assert len([line for line in out.splitlines() if " llm shadow " in line]) == 4


def test_a_stream_a_stop_cut_short_says_so(tree: Path) -> None:
    lines = (FIXTURES / "stopped.jsonl").read_bytes()
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", CHILD, str(SCRIPT), str(tree)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert child.stdout.readline() == b"loaded\n"
    child.stdin.write(lines)
    child.stdin.flush()
    time.sleep(0.2)
    child.send_signal(signal.SIGTERM)
    child.stdin.close()
    out = child.stdout.read().decode()
    assert child.wait(timeout=30) == 0
    assert out.splitlines()[-1].startswith("INFO Claude Code's stream ended after a SIGTERM")


def test_a_journal_gone_at_exit_still_exits_zero(tree: Path) -> None:
    # Buffered writes would fail again in the interpreter's final flush and exit 120, which
    # wake-runner would read as a translator failure on every such wake.
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", CHILD, str(SCRIPT), str(tree)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert child.stdout.readline() == b"loaded\n"
    child.stdout.close()
    child.stdin.write((FIXTURES / "tools.jsonl").read_bytes())
    child.stdin.close()
    assert child.wait(timeout=30) == 0, child.stderr.read()


def test_its_one_argument_is_the_writers_pid() -> None:
    # Anything else, or nothing, reads to EOF: the watch is a bound, never a requirement.
    source = SCRIPT.read_text()
    assert "writer = int(argv[0]) if argv and argv[0].isdigit() and int(argv[0]) > 1 else None" in (
        source
    )
    assert "sys.exit(main(sys.argv[1:]))" in source

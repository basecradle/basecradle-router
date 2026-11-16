"""A builder's wake is a pipe: ``claude … | claude-stream-translate`` (basecradle-router#481).

wake-runner itself gates on root, a real agent account and the root-owned registry, so it is
not runnable offline (see ``test_wake_runner_journal.py``). This pins what it decides
structurally, against the shipped bytes, and runs the pipe itself (``run_stream``, extracted from
the shipped agent script between its markers) for real, against a stub ``claude``, the real
translator launcher, a crashing one, real ``jq`` and real ``cat``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WAKE_RUNNER = ROOT / "deploy" / "bin" / "wake-runner"
LAUNCHER = ROOT / "deploy" / "bin" / "claude-stream-translate"
FIXTURES = Path(__file__).parent / "fixtures" / "claude_stream"


def _script() -> str:
    return WAKE_RUNNER.read_text()


def _agent_script() -> str:
    match = re.search(r"^AGENT_SCRIPT='\n(.*?)\n'$", _script(), re.S | re.M)
    assert match, "AGENT_SCRIPT not found"
    return match.group(1)


def _run_stream() -> str:
    match = re.search(r"# >>> run_stream >>>\n(.*?)\n\t# <<< run_stream <<<", _agent_script(), re.S)
    assert match, "run_stream marker block not found in the agent script"
    return "\n".join(line.removeprefix("\t") for line in match.group(1).splitlines())


# --- what root decides ---------------------------------------------------------------------------


def test_the_translator_and_the_flags_are_constants_root_names() -> None:
    script = _script()
    assert "\nSTREAM_TRANSLATE=/opt/basecradle-router/bin/claude-stream-translate\n" in script
    assert (
        "\nSTREAM_FLAGS=(--output-format stream-json --verbose --include-partial-messages)\n"
        in script
    )


def test_a_builders_command_must_be_exactly_claude_p_trigger() -> None:
    # The caller's whole say is the trigger: before #481 anything after `claude` passed through.
    assert re.search(
        r'\[\[ \$# -eq 3 && "\$2" == -p \]\] \|\|\n\t\tdie "refusing: a builder\'s command must be'
        r" exactly 'claude -p <trigger>'",
        _script(),
    )


def test_a_trigger_that_is_itself_an_option_is_refused() -> None:
    assert re.search(
        r'\[\[ "\$3" != -\* \]\] \|\|\n\t\tdie "refusing: a builder\'s trigger may not begin with',
        _script(),
    )


def test_the_flags_are_appended_by_root_and_only_for_a_builders_real_wake() -> None:
    assert re.search(
        r'shift # drop argv\[1\].*\n\t# .*\n\t\[\[ -z \$stream_translate \]\] \|\| set -- "\$@"'
        r' "\$\{STREAM_FLAGS\[@\]\}"',
        _script(),
    )


@pytest.mark.parametrize("helper", ["python3", "jq", "cat"])
def test_every_stream_helper_is_resolved_off_the_trusted_path(helper: str) -> None:
    assert re.search(
        rf'stream_{"python" if helper == "python3" else helper}="\$\(PATH="\$TRUSTED_PATH"'
        rf" command -v {helper}\)\"",
        _script(),
    ), helper


def test_a_missing_translator_refuses_the_wake_rather_than_run_it_unlogged() -> None:
    # The same posture as systemd-cat: a wake that cannot be recorded fails, loudly.
    assert re.search(r"\[\[ -x \$STREAM_TRANSLATE \]\] \|\|\n\t+die ", _script())


def test_the_stream_helpers_ride_into_the_drop_as_inert_positionals() -> None:
    assert re.search(
        r'drop=\(runuser -u "\$user" -- /bin/bash -c "\$AGENT_SCRIPT" wake-runner "\$real_cwd"'
        r' "\$delivery"\n\t"\$stream_python" "\$stream_translate" "\$stream_jq" "\$stream_cat"'
        r' "\$launch_bin" "\$@"\)',
        _script(),
    )


def test_a_builders_claude_is_execed_with_its_stdout_into_the_translator() -> None:
    # runuser's child must be claude itself, as for every wake: a shell in between would die of
    # runuser's relayed TERM at once, and the router would read the wake as over while claude ran.
    assert re.search(
        r'\texec "\$@" > >\(\n\t\t"\$python" -I "\$translate" "\$\$" \|\| \{\n',
        _run_stream(),
    )


def test_every_wake_but_a_builders_still_execs_its_binary() -> None:
    assert re.search(
        r'if \[\[ -z \$stream_translate \]\]; then\n\t\texec "\$@"\n\tfi\n\trun_stream ',
        _agent_script(),
    )


def test_the_agent_script_parses_and_holds_no_single_quote() -> None:
    body = _agent_script()
    assert "'" not in body, "a single quote ends the single-quoted agent script early"
    subprocess.run(["bash", "-n"], input=body, text=True, check=True)


# --- the pipe, run for real ----------------------------------------------------------------------

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")

#: A stand-in `claude`: the fixture's stream, with `padding` lines before its final `result`
#: line, so a reader that dies early dies far from the reply; then the given exit status.
CLAUDE = """#!/usr/bin/env python3
import sys
lines = open(sys.argv[1]).read().splitlines(keepends=True)
sys.stdout.writelines(lines[:-1])
for n in range(int(sys.argv[3])):
    sys.stdout.write('{"type": "system", "subtype": "status", "status": "padding %d"}\\n' % n)
sys.stdout.write(lines[-1])
sys.stdout.flush()
sys.exit(int(sys.argv[2]))
"""

ECHO = """import sys
for line in sys.stdin:
    sys.stdout.write("T " + line)
"""

CRASH = """import sys
sys.stdin.readline()
sys.exit(1)
"""

#: A translator that dies before reading anything: what an interpreter that cannot start does.
DEAD = """import sys
sys.exit(1)
"""


def stream(
    tmp_path: Path,
    translator: Path,
    *,
    exit_code: int = 0,
    padding: int = 0,
    jq: str | None = None,
    fixture: str = "tools",
) -> subprocess.CompletedProcess[str]:
    claude = tmp_path / "claude"
    claude.write_text(CLAUDE)
    claude.chmod(0o755)
    program = f'set -euo pipefail\n{_run_stream()}\nrun_stream "$@"\n'
    return subprocess.run(
        [
            "bash",
            "-c",
            program,
            "wake-runner",
            sys.executable,
            str(translator),
            jq or shutil.which("jq") or "/nonexistent/jq",
            shutil.which("cat"),
            str(claude),
            str(FIXTURES / f"{fixture}.jsonl"),
            str(exit_code),
            str(padding),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _helper(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


def test_the_wakes_status_is_claudes_and_the_translator_reads_every_line(tmp_path: Path) -> None:
    done = stream(tmp_path, _helper(tmp_path, "echo.py", ECHO), exit_code=3)
    assert done.returncode == 3
    lines = (FIXTURES / "tools.jsonl").read_text().splitlines()
    assert done.stdout.splitlines() == [f"T {line}" for line in lines]


@needs_jq
def test_an_unloadable_translator_keeps_the_reply_and_the_wake(tmp_path: Path) -> None:
    if Path("/opt/basecradle-router/app/src").exists():
        pytest.skip("this host carries a deployed router tree")
    done = stream(tmp_path, LAUNCHER, exit_code=0)
    assert done.returncode == 0
    assert done.stdout == "Done: the work is finished.\n"
    assert "the stream translator exited 70; keeping the reply" in done.stderr
    assert "not loading the translator" in done.stderr


@needs_jq
def test_a_translator_that_crashes_mid_stream_costs_neither_the_wake_nor_the_reply(
    tmp_path: Path,
) -> None:
    # 20,000 lines after the crash: far past a pipe's buffer, so a claude whose reader had gone
    # would block or die of SIGPIPE (141), never exit with its own status.
    done = stream(tmp_path, _helper(tmp_path, "crash.py", CRASH), exit_code=0, padding=20_000)
    assert done.returncode == 0
    assert done.stdout == "Done: the work is finished.\n"
    assert "the stream translator exited 1" in done.stderr


def test_with_jq_gone_too_cat_drains_and_the_wake_still_ends_as_claude_did(
    tmp_path: Path,
) -> None:
    done = stream(
        tmp_path,
        _helper(tmp_path, "crash.py", CRASH),
        exit_code=5,
        padding=20_000,
        jq="/nonexistent/jq",
    )
    assert done.returncode == 5
    assert done.stdout == ""


@needs_jq
def test_the_fallback_reply_is_the_result_lines_text_and_nothing_else(tmp_path: Path) -> None:
    done = stream(tmp_path, _helper(tmp_path, "dead.py", DEAD), fixture="failed_model")
    result = json.loads((FIXTURES / "failed_model.jsonl").read_text().splitlines()[-1])
    assert done.stdout == result["result"] + "\n"

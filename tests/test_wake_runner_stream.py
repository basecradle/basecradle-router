"""A builder's wake is a pipe: ``claude … | claude-stream-translate`` (basecradle-router#481).

wake-runner itself gates on root, a real agent account and the root-owned registry, so it is
not runnable offline (see ``test_wake_runner_journal.py``). This pins what it decides
structurally, against the shipped bytes, and runs the pipe itself (``run_stream``, extracted from
the shipped agent script between its markers) for real, against a stub ``claude``, the real
translator launcher, a crashing one, real ``jq`` and real ``cat``.
"""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import sys
import time
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
    raw: bool = False,
) -> subprocess.CompletedProcess:
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
        text=not raw,  # raw: bytes as journald reads them, a `\r` never a line break
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
    assert done.stdout == "> Done: the work is finished.\n"
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
    assert done.stdout == "> Done: the work is finished.\n"
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
    assert done.stdout == "".join(f"> {line}\n" for line in result["result"].split("\n"))


@needs_jq
def test_the_fallback_reply_can_never_stand_at_the_heads_position(
    tmp_path: Path, noc_counted_head, forged_reply
) -> None:
    # The translator dead before reading: jq writes the reply, by the translator's own rule.
    forged = tmp_path / "forged.jsonl"
    forged.write_text(json.dumps({"type": "result", "result": forged_reply}) + "\n")
    done = stream(tmp_path, _helper(tmp_path, "dead.py", DEAD), fixture=str(forged)[:-6], raw=True)
    records = done.stdout.decode().split("\n")[:-1]  # journald's record breaks: newline and NUL
    records = [piece for record in records for piece in record.split("\x00")]
    assert records and all(record.startswith(">") for record in records)
    for record in records:
        assert len(record.encode()) <= 48 * 1024
        assert not noc_counted_head.match(record)
        assert not noc_counted_head.match(f"[basecradle-wake-basecradle-nova-ai] {record}")


@needs_jq
def test_the_fallback_quotes_by_exactly_the_translators_rule(tmp_path: Path) -> None:
    # One rule, two spellings: jq's output for a reply that holds every record break, a CRLF, an
    # empty line, a trailing break and a line two pieces long is `quoted()`'s, line for line.
    from basecradle_router.claude_stream import QUOTED_PIECE, quoted

    reply = (
        "a\r\nb\rc\x00d\x0be\x0cf\x1cg\x1dh\x1ei\x85j\u2028k\u2029l\n\nm"
        + "z" * (QUOTED_PIECE + 5)
        + "\n"
    )
    forged = tmp_path / "breaks.jsonl"
    forged.write_text(json.dumps({"type": "result", "result": reply}) + "\n")
    done = stream(tmp_path, _helper(tmp_path, "dead.py", DEAD), fixture=str(forged)[:-6], raw=True)
    assert done.stdout.decode() == "".join(line + "\n" for line in quoted(reply))


# --- the fallback redacts credential-named assignments (#491) ------------------------------------

#: A fabricated GitHub App installation token, and the mint line every builder command opens with.
_FAKE_TOKEN = "ghs_FABRICATEDnovaDigitalToken0000000000"
_MINT = (
    'GH_TOKEN="$(~/Documents/claude-workspace/shared/fleet-identity/gh-app-token'
    ' basecradle-nova-ai)" || exit 1'
)

#: Every common shape the fallback's one-level reading takes exactly as the translator's nesting
#: one does: the two spellings must agree here, line for line.
_AGREED = "\n".join(
    [
        f"Done. I ran {_MINT}; export GH_TOKEN",
        f"GH_TOKEN={_FAKE_TOKEN} gh api",
        "A_TOKEN='a b c' tail",
        'A_TOKEN="a \\" b" tail',
        "A_TOKEN=$(gh-app-token nova) tail",
        "A_TOKEN=`gh-app-token nova` tail",
        "A_TOKEN=a\\ b tail",
        "x=1 A_KEY=1 y=2 B_SECRET=2 C_PASSWORD_FILE=kept gh_token=kept API_KEY_ID=kept",
        # A bare word names a credential, and `_BASE` is the one tail (#504).
        "PASSWORD=gone KEY=gone SECRET_KEY_BASE=gone",
        "A_TOKEN=",
        "B_TOKEN=\tsecretsecret",
        "C_TOKEN='<a redacted value>' twice",
        "A_KEY=(sk1 sk2secretsecret) x",
        "GH_TOKEN=don't do that.",
        # A bearer value goes with its token (#497): any case, glued, doubled, in any order.
        "AUTH_TOKEN=Bearer eyJhbGciOiJIUzI1NiJ9.e30.fabricatedSig next=1",
        "X_API_KEY=KBearer eyJhbGciOiJIUzI1NiJ9 next=1",
        "A_TOKEN=Bearer Bearer eyJabc next=1 | B_TOKEN=bearer\tBEARER eyJabc next=2",
        "C_TOKEN=Bearer KBearer eyJabc next=1 | D_TOKEN=x.Bearer Bearer eyJabc next=2",
        'E_TOKEN=Bearer "a b" next=1 | F_TOKEN=abc-bearer next=2 | G_TOKEN=Bearer',
        "H_TOKEN=Bearer ",
        'I_TOKEN=Bearer\t"a b" next=1 | J_TOKEN=a\\ Bearer eyJabc next=2',
        # A scheme word a tab glues to a value goes with its token (#502): the issue's three lines,
        # several lead words, every lead piece, and a quoted value holding a tab, which is one word.
        "A_TOKEN=x\tBearer eyJhbGciOiJIUzI1NiJ9.e30.fabricatedSig next=1",
        "A_TOKEN=\tBearer eyJhbGciOiJIUzI1NiJ9.e30.fabricatedSig next=1",
        "A_TOKEN='q'\tKBearer eyJhbGciOiJIUzI1NiJ9.e30.fabricatedSig next=1",
        "B_TOKEN=x\tbearer\tBEARER eyJabc next=1 | C_TOKEN=a\\'b\t`c`\tBearer eyJabc next=2",
        "D_TOKEN='a\tBearer b' next=1 | E_TOKEN=a\t'b c' next=2 | F_KEY=abc\tdefghijk x",
        "G_TOKEN=x\t next=1 | H_TOKEN=x\t,y next=2 | I_TOKEN=\t next=3",
        'J_TOKEN=x\t"a b" next=1 | K_TOKEN=\t"a b" next=2',
        # A credential name inside a value takes its own value with it (#504's review), and a name
        # inside a quote is the quote's text.
        "AUTH_TOKEN=x,DB_PASSWORD=\thunter2hunter2 next=1",
        "A_TOKEN='a b'B_TOKEN=\tsecretsecret next=1 | C_TOKEN=x'q'D=x'q'E_TOKEN=Bearer eyJ next=2",
        "F_TOKEN=\t'q'G_PASSWORD=Bearer eyJabc next=1",
        "H_TOKEN=Bearer ;I_KEY=:KBearer :\tBearer eyJabc x",
        "J_TOKEN=x,K_TOKEN= next=1 | L_TOKEN=x,M_TOKEN=\t next=2 | N_TOKEN='O_TOKEN=x' next=3 o=1",
        "P_TOKEN='<a redacted value>',next=1 | Q_TOKEN='<a redacted value>' twice",
        # A backslash before a tab is its word's last character, so a scheme word after the tab is
        # the value's; and a scheme word's token is a word on its own record (the review of #502).
        "R_TOKEN=a\\\tBearer eyJabc next=1 | S_TOKEN=Bearer ,next=1 | T_TOKEN=Bearer \\\t}x",
        "U_TOKEN=(a)\tBearer eyJabc next=1 | V_TOKEN=f(x)\tKBearer eyJabc next=2",
        "",
        "The rest of the reply.",
    ]
)


def _fallback(tmp_path: Path, reply: str) -> list[str]:
    forged = tmp_path / "secret.jsonl"
    forged.write_text(json.dumps({"type": "result", "result": reply}) + "\n")
    done = stream(tmp_path, _helper(tmp_path, "dead.py", DEAD), fixture=str(forged)[:-6], raw=True)
    assert done.returncode == 0
    return done.stdout.decode().splitlines()


@needs_jq
def test_the_fallback_redacts_by_the_translators_rule(tmp_path: Path, canary) -> None:
    from basecradle_router.claude_stream import quoted

    out = _fallback(tmp_path, _AGREED)
    assert out == quoted(_AGREED)
    assert not any(_FAKE_TOKEN in line or canary.search(line) for line in out)
    assert "> Done. I ran GH_TOKEN='<a redacted value>' || exit 1; export GH_TOKEN" in out


@needs_jq
def test_the_fallback_never_leaves_a_line_the_canary_reads(tmp_path: Path, canary) -> None:
    # Where the one-level reading stops short of the translator's, what is left is still no value
    # the canary reads.
    reply = "\n".join(
        [
            'A_TOKEN="$(gh-app-token "a b" $(x y))" tail',
            "A_TOKEN=$'a\\'b c' tail",
            "A_KEY=abc\tdefghijk x",
            'A_TOKEN="never closed and the rest',
            "Z_TOKEN=" + "x" * 50,
        ]
    )
    out = _fallback(tmp_path, reply)
    assert out and not any(canary.search(line) for line in out)


@needs_jq
def test_the_fallback_reads_a_long_run_of_capitals_in_linear_time(tmp_path: Path) -> None:
    # The canary's own pattern took jq 30 s on 50,000 capitals, and the fallback reads the pipe
    # `claude` writes into: a stall there is a hung wake. `stream` times out at 60 s.
    out = _fallback(tmp_path, "A" * 200_000 + "=x " + "_" * 200_000 + "TOKEN=x")
    assert out[0].startswith("> AAAA")


#: The pieces of a fuzzed reply: credential names, every form of scheme word, tokens, blanks, and
#: lead words a tab glues to a scheme word (#502), a canary stop after one included. No piece the
#: fallback reads one level deep (a quote that never closes, a parenthesis): there its reading is
#: the degraded one, which is not this rule's.
_SCHEME_PIECES = [
    "A_TOKEN=", "MY_API_KEY=", "PASSWORD=", "Bearer ", "Bearer\t", "BEARER ", "bearer ",
    "KBearer ", "x.Bearer ", "abc-bearer ", "eyJabc", "abc", " ", "=", ".", ":", "next=1",
    "\t", "x\t", "\tBearer ", "'q'\t", "`a`\t", "\\\t", ",", "}",
    "DB_PASSWORD=", "x,", "'q'", "(a)\t",
]  # fmt: skip


@needs_jq
def test_the_fallback_reads_scheme_words_as_the_translator_does(tmp_path: Path) -> None:
    # Seeded fuzz, 600 lines: on every run of scheme words the two spellings agree (#497).
    from basecradle_router.claude_stream import quoted

    rng = random.Random(497)
    reply = "\n".join(
        "".join(rng.choice(_SCHEME_PIECES) for _ in range(rng.randint(1, 16))) + " end"
        for _ in range(600)
    )
    assert _fallback(tmp_path, reply) == quoted(reply)


@needs_jq
@pytest.mark.parametrize(
    ("run", "left"),
    [
        ("Bearer ", ""),
        ("Bearer\t", ""),
        ("xBearer", ""),
        ("A_TOKEN=Bearer", ""),
        ("xbearer", " x"),
        # Lead words before a scheme word a tab glues to a value (#502), and tabs alone.
        ("x\t", " x"),
        ("'q'\t", " x"),
        ("`a`\t", " x"),
        ("x\tBearer ", ""),
        ("\t", " x"),
        ("\tA_TOKEN=", " x"),
        # Credential names inside a value (#504's review): each one item, read once.
        ("x,A_TOKEN=", " x"),
        ("x'q'A_TOKEN=\t", " x"),
        ("x,A_TOKEN=Bearer ", ""),
    ],
)
def test_the_fallback_reads_a_long_run_of_scheme_words_in_linear_time(
    tmp_path: Path, run: str, left: str
) -> None:
    # A backtracking engine reads the scheme words before a value: none of these may stall it. A
    # run of scheme words takes the word after it; a value ending in `bearer` is no header.
    reply = "A_TOKEN=" + run * 40_000 + " x"
    started = time.monotonic()
    out = _fallback(tmp_path, reply)
    assert time.monotonic() - started < 15.0
    assert out == ["> A_TOKEN='<a redacted value>'" + left]
    from basecradle_router.claude_stream import quoted

    assert out == quoted(reply)


def test_the_fallback_is_its_generated_program_byte_for_byte() -> None:
    # The program is spelled three times over (regex, jq string, bash string), so it is generated
    # from the translator's own constants (`tests/fallback_rule.py`), never written by hand: a piece
    # is changed in one place, and the shipped line is held to it here.
    import fallback_rule

    assert fallback_rule.shipped(WAKE_RUNNER.read_text()) == fallback_rule.PROGRAM, (
        "rewrite it with `uv run python tests/fallback_rule.py`"
    )


def test_the_fallback_names_a_credential_by_the_translators_spelling() -> None:
    import fallback_rule
    from basecradle_router.logfmt import CREDENTIAL_NAME, LEAD_OPENERS, REDACTED_VALUE, SCHEME_GLUE

    script = _run_stream()
    assert f'\\"{CREDENTIAL_NAME}\\" as \\$n' in script
    marker = REDACTED_VALUE.replace("'", "\\\\u0027")
    assert f'\\"{marker}\\" as \\$r' in script
    # Every piece the value reads is the translator's (#497, #502, #504's review), one spelling.
    program = fallback_rule.PROGRAM_JQ
    assert f"[{SCHEME_GLUE}]*Bearer" in program
    assert fallback_rule.run_char("\t" + LEAD_OPENERS + "=") in fallback_rule.LEAD
    assert all(f"(?<={s})" in program and f"(?<!{s})" in program for s in fallback_rule.SPELLINGS)

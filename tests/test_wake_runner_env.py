"""The wake-runner's literal `agent.env` loader — exercised offline against bash.

`agent.env` is parsed as literal ``KEY=VALUE`` lines, never bash-``source``d, so a
secret containing a shell-special character ($, backtick, ``$( )``, spaces, quotes)
is data and never code. A bash-``source`` evaluated every value: a random password
with a ``$`` aborted the wake under ``set -u`` (a silent dead agent), and a value
like ``$(cmd)`` would have executed ``cmd`` as the agent (basecradle-router#109).

These tests run the EXACT shipped parser: the ``load_agent_env`` function is
extracted from ``deploy/bin/wake-runner`` between its marker comments and run in a
clean bash, so a regression in the real script fails here — nothing is duplicated.
No model, agent, or network is touched. Fabricated cast only: Nova Digital
(``nova``, AI). Tokens/passwords are correctly-shaped fakes.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

WAKE_RUNNER = Path(__file__).resolve().parents[1] / "deploy" / "bin" / "wake-runner"

# Skip rather than error where bash is unavailable; CI (ubuntu) and macOS have it.
pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to exercise the wake-runner parser"
)


def _extract_loader() -> str:
    """Pull the real ``load_agent_env`` body from wake-runner, between its markers."""
    text = WAKE_RUNNER.read_text()
    match = re.search(
        r"[ \t]*# >>> load_agent_env >>>\n(.*?)[ \t]*# <<< load_agent_env <<<\n",
        text,
        re.DOTALL,
    )
    assert match, "load_agent_env marker block not found in deploy/bin/wake-runner"
    return match.group(1)


def _load(env_text: str, tmp_path: Path) -> dict[str, str]:
    """Run the shipped parser over ``env_text`` and return every exported var.

    The loader is sourced into a clean bash with ``set -euo pipefail`` (the same
    strictness the real inner script runs under — this is what tripped on a ``$``),
    then the full environment is dumped NUL-delimited and parsed back.
    """
    env_file = tmp_path / "agent.env"
    env_file.write_text(env_text)
    script = textwrap.dedent(
        """\
        set -euo pipefail
        {loader}
        load_agent_env "$1"
        env -0
        """
    ).format(loader=_extract_loader())
    proc = subprocess.run(
        ["bash", "-c", script, "bash", str(env_file)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert proc.returncode == 0, f"loader exited {proc.returncode}: {proc.stderr}"
    out: dict[str, str] = {}
    for pair in proc.stdout.split("\0"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            out[key] = value
    return out


def test_value_with_a_dollar_loads_literally_and_the_agent_wakes(tmp_path: Path) -> None:
    # The live #109 failure: a BaseCradle password containing a literal `$` aborted
    # the source under `set -u` ("$<token>: unbound variable"). It must load verbatim.
    loaded = _load("BASECRADLE_PASSWORD=pa$$w0rd$X\n", tmp_path)
    assert loaded["BASECRADLE_PASSWORD"] == "pa$$w0rd$X"


def test_command_substitution_is_never_executed(tmp_path: Path) -> None:
    # The security footgun: a sourced `$(...)` would RUN as the agent. The literal
    # parser must keep it as inert data, and the marker file must never be created.
    canary = tmp_path / "PWNED"
    loaded = _load(f"X=$(touch {canary})\n", tmp_path)
    assert loaded["X"] == f"$(touch {canary})"
    assert not canary.exists(), "command substitution executed — value was evaluated as shell"


def test_backticks_are_never_executed(tmp_path: Path) -> None:
    loaded = _load("Y=`whoami`\n", tmp_path)
    assert loaded["Y"] == "`whoami`"


def test_spaces_in_a_value_are_preserved(tmp_path: Path) -> None:
    loaded = _load("MESSAGE=hello there world\n", tmp_path)
    assert loaded["MESSAGE"] == "hello there world"


def test_one_layer_of_double_quotes_is_stripped(tmp_path: Path) -> None:
    loaded = _load('TOKEN="quoted value"\n', tmp_path)
    assert loaded["TOKEN"] == "quoted value"


def test_one_layer_of_single_quotes_is_stripped(tmp_path: Path) -> None:
    # The interim workaround single-quoted every value; stripping one layer keeps
    # those existing files loading the same literal value the operator intended.
    loaded = _load("TOKEN='has$dollar'\n", tmp_path)
    assert loaded["TOKEN"] == "has$dollar"


def test_a_lone_quote_char_is_not_mistaken_for_a_wrapped_value(tmp_path: Path) -> None:
    # The quote-strip only fires on a matching PAIR (len >= 2, both ends): a value
    # that is a single quote char must survive verbatim, never underflow the slice.
    assert _load('A="\n', tmp_path)["A"] == '"'
    assert _load("B='\n", tmp_path)["B"] == "'"
    assert _load('C=""\n', tmp_path)["C"] == ""  # empty after stripping the pair


def test_only_the_first_equals_splits_the_pair(tmp_path: Path) -> None:
    loaded = _load("GH_APP_PEM_B64=abc=def==\n", tmp_path)
    assert loaded["GH_APP_PEM_B64"] == "abc=def=="


def test_comments_blanks_and_export_prefix(tmp_path: Path) -> None:
    loaded = _load(
        "# a comment\n\n  \n  export GH_APP_SLUG=nova-ai\n",
        tmp_path,
    )
    assert loaded["GH_APP_SLUG"] == "nova-ai"


def test_a_line_without_equals_is_ignored(tmp_path: Path) -> None:
    loaded = _load("NOT_AN_ASSIGNMENT\nGOOD=value\n", tmp_path)
    assert "NOT_AN_ASSIGNMENT" not in loaded
    assert loaded["GOOD"] == "value"


def test_plain_unquoted_values_are_unaffected(tmp_path: Path) -> None:
    # @jt-style files (no shell-special chars, no quotes) load exactly as before —
    # the change is structural, not behavioural, for the common case.
    loaded = _load(
        "ANTHROPIC_API_KEY=sk-ant-deadbeef\nGH_APP_ID=123456\n",
        tmp_path,
    )
    assert loaded["ANTHROPIC_API_KEY"] == "sk-ant-deadbeef"
    assert loaded["GH_APP_ID"] == "123456"

"""Both shell readers of `router.env` read it the way systemd hands it to the daemon (#419).

The daemon never parses `router.env`: systemd's `EnvironmentFile=` does, and the daemon
reads the environment that results. Two of this repo's scripts read the same file
themselves — `deploy/smoke-test.sh`, the gate that decides whether a deploy is healthy,
and `deploy/bin/router-admin`, the wrapper every NOC claim and probe runs through. A
reader that parses the file differently from systemd describes a daemon that does not
exist: the gate signs with a key the daemon is not using and rolls back a healthy
deploy, or the CLI emits claims about a config the daemon never loaded. Before #419 the
gate let the FIRST line of a duplicated key win and kept quotes, while `router-admin`
let the last win, stripped quotes, and honoured an `export ` prefix that systemd drops.

So both carry one parser, `systemd_env_file`, transcribed from systemd 255's
`src/basic/env-file.c`, and this file pins three things:

- **The two copies are one parser.** The marker blocks are identical but for each
  file's indentation, so a fix to one cannot leave the other behind.
- **Each reader agrees with systemd on every case below, fed the same file** — the
  smoke test through `env_value`, the function its every read goes through, and
  `router-admin` through the environment it actually exports to the CLI.
- **The expectations are systemd's, not ours.** Every expected mapping below was recorded
  by running its file through systemd's own `load_env_file` and `strv_env_clean` (the
  pair `EnvironmentFile=` runs), from `libsystemd-shared` in systemd
  255.4-1ubuntu8.18 — the build `ai.basecradle.com` runs — on 2026-11-07. A case added
  later must be recorded the same way, never written from a reading of the C.

No model, agent, network, or real `router.env` is touched; values are fabricated.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SMOKE_TEST = ROOT / "deploy" / "smoke-test.sh"
ROUTER_ADMIN = ROOT / "deploy" / "bin" / "router-admin"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")

# (case id, router.env bytes, the environment systemd 255 builds from them).
SYSTEMD_CASES: list[tuple[str, str, dict[str, str]]] = [
    ("bare", "K=v\n", {"K": "v"}),
    ("duplicate_last_wins", "K=first\nK=second\n", {"K": "second"}),
    ("duplicate_with_whitespace", "K=a\n  K = b\n", {"K": "b"}),
    ("double_quoted", 'K="a b"\n', {"K": "a b"}),
    ("single_quoted_literal", "K='$x \\n \"'\n", {"K": '$x \\n "'}),
    ("export_prefix_dropped", "export K=v\n", {}),
    ("export_then_bare", "export K=v\nK=w\n", {"K": "w"}),
    ("leading_whitespace", "  \tK=v\n", {"K": "v"}),
    ("whitespace_around_equals", "K = v\n", {"K": "v"}),
    ("trailing_whitespace_chomped", "K=v \t\n", {"K": "v"}),
    ("interior_whitespace_kept", "K=a  b\n", {"K": "a  b"}),
    ("hash_inside_value_kept", "K=v # not a comment\n", {"K": "v # not a comment"}),
    ("hash_after_closing_quote", 'K="a" # c\n', {"K": "a# c"}),
    ("hash_comment", "#K=v\n", {}),
    ("semicolon_comment", ";K=v\n", {}),
    ("indented_comment", "  # K=v\n", {}),
    ("quotes_concatenate", "K=\"a\" 'b'c\n", {"K": "abc"}),
    ("interior_quote_literal", 'K=a"b"\n', {"K": 'a"b"'}),
    ("double_quote_escapes", 'K="\\"\\\\\\`\\$\\n"\n', {"K": '"\\`$\\n'}),
    ("unquoted_backslash", "K=a\\b\n", {"K": "ab"}),
    ("escaped_trailing_space", "K=x\\ \n", {"K": "x "}),
    ("line_continuation", "K=a\\\nb\n", {"K": "ab"}),
    ("double_quote_continuation", 'K="a\\\nb"\n', {"K": "ab"}),
    ("multiline_double_quote", 'K="a\nb"\n', {"K": "a\nb"}),
    ("multiline_single_quote", "K='a\nb'\n", {"K": "a\nb"}),
    ("unterminated_quote_at_eof", 'K="abc', {"K": "abc"}),
    ("no_expansion", "K=$HOME$(id -u)`whoami`\n", {"K": "$HOME$(id -u)`whoami`"}),
    (
        "hostile_secret",
        "K=fake-secret-$(id -u)`whoami`$HOME'\"x\n",
        {"K": "fake-secret-$(id -u)`whoami`$HOME'\"x"},
    ),
    ("equals_in_value", "K=a=b==\n", {"K": "a=b=="}),
    ("digit_first_name_dropped", "1K=v\n", {}),
    ("dash_in_name_dropped", "K-X=v\n", {}),
    ("empty_value_present", "K=\n", {"K": ""}),
    ("empty_quoted_value", 'K=""\n', {"K": ""}),
    ("no_equals_ignored", "K\n", {}),
    ("no_trailing_newline", "K=v", {"K": "v"}),
    ("crlf_line_ending", "K=v\r\nL=w\r\n", {"K": "v", "L": "w"}),
    ("utf8_value", "K=café\n", {"K": "café"}),
    ("comment_backslash_not_continued", "# c \\\nK=v\n", {"K": "v"}),
    ("substring_name", "X_K=wrong\nK=right\n", {"X_K": "wrong", "K": "right"}),
    (
        "realistic",
        "# daemon env (fabricated values)\n"
        "BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET=fake-0123456789abcdef\n"
        "BASECRADLE_ROUTER_ENABLED_ROUTES=github,basecradle\n"
        "BASECRADLE_ROUTER_WAKE_BREAKER_MAX=12\n",
        {
            "BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET": "fake-0123456789abcdef",
            "BASECRADLE_ROUTER_ENABLED_ROUTES": "github,basecradle",
            "BASECRADLE_ROUTER_WAKE_BREAKER_MAX": "12",
        },
    ),
]

# Every name any case could set, so a reader that sets one systemd would not is caught,
# not only one that misses a name systemd sets.
CANDIDATE_NAMES = sorted({"K", "L", "X_K"} | {n for _, _, env in SYSTEMD_CASES for n in env})

PARAMS = [pytest.param(text, env, id=case_id) for case_id, text, env in SYSTEMD_CASES]


def _block(path: Path) -> str:
    match = re.search(
        r"[ \t]*# >>> systemd_env_file >>>\n(.*?)[ \t]*# <<< systemd_env_file <<<\n",
        path.read_text(),
        re.DOTALL,
    )
    assert match, f"systemd_env_file marker block not found in {path}"
    return match.group(1)


def _pairs(out: bytes) -> dict[str, str]:
    """Decode the harnesses' `name NUL value NUL` stream: a value may hold a newline."""
    parts = out.decode().split("\0")[:-1]
    return dict(zip(parts[::2], parts[1::2], strict=True))


def _smoke_test_reads(text: str, tmp_path: Path) -> dict[str, str]:
    """What the gate reads for every candidate name, through the shipped env_value."""
    env_file = tmp_path / "router.env"
    env_file.write_bytes(text.encode())
    names = " ".join(CANDIDATE_NAMES)
    parsers = re.search(
        r"[ \t]*# >>> env_parsers >>>\n(.*?)[ \t]*# <<< env_parsers <<<\n",
        SMOKE_TEST.read_text(),
        re.DOTALL,
    )
    assert parsers, "env_parsers marker block not found in deploy/smoke-test.sh"
    # An absent name prints a sentinel no case's value contains, so "absent" and "set to
    # the empty string" stay apart — for the enabled routes they are a default and a
    # boot refusal.
    script = (
        'set -euo pipefail\nROUTER_ENV_FILE="$1"\n'
        + parsers.group(1)
        + f"for name in {names}; do\n"
        + '\tvalue="$(env_value "$name" "<absent>")"\n'
        + '\t[[ "$value" == "<absent>" ]] || printf "%s\\0%s\\0" "$name" "$value"\n'
        + "done\n"
    )
    harness = tmp_path / "smoke.sh"
    harness.write_text(script)
    result = subprocess.run(["bash", str(harness), str(env_file)], capture_output=True, check=True)
    return _pairs(result.stdout)


def _router_admin_exports(text: str, tmp_path: Path) -> dict[str, str]:
    """What router-admin exports to the CLI, from the shipped script stopped before exec."""
    env_file = tmp_path / "router.env"
    env_file.write_bytes(text.encode())
    names = " ".join(CANDIDATE_NAMES)
    script = ROUTER_ADMIN.read_text().split("# Prefer the venv's python")[0]
    script += (
        f"for name in {names}; do\n"
        + '    if [ -n "${!name+set}" ]; then printf "%s\\0%s\\0" "$name" "${!name}"; fi\n'
        + "done\n"
    )
    harness = tmp_path / "admin.sh"
    harness.write_text(script)
    user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    result = subprocess.run(
        ["bash", str(harness)],
        capture_output=True,
        check=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "BASECRADLE_ROUTER_ENV_FILE": str(env_file),
            "BASECRADLE_ROUTER_USER": user,
            "HOME": str(tmp_path),
        },
    )
    return _pairs(result.stdout)


def test_both_readers_carry_one_parser() -> None:
    """The two copies differ only in each file's indentation, never in a byte of logic."""

    def dedented(path: Path) -> list[str]:
        return [line.lstrip(" \t") for line in _block(path).splitlines()]

    assert dedented(SMOKE_TEST) == dedented(ROUTER_ADMIN)


@pytest.mark.parametrize(("text", "expected"), PARAMS)
def test_the_smoke_test_reads_router_env_as_systemd_does(
    text: str, expected: dict[str, str], tmp_path: Path
) -> None:
    assert _smoke_test_reads(text, tmp_path) == expected


@pytest.mark.parametrize(("text", "expected"), PARAMS)
def test_router_admin_exports_router_env_as_systemd_does(
    text: str, expected: dict[str, str], tmp_path: Path
) -> None:
    assert _router_admin_exports(text, tmp_path) == expected


@pytest.mark.parametrize(("text", "expected"), PARAMS)
def test_the_smoke_test_and_router_admin_agree_on_one_file(
    text: str, expected: dict[str, str], tmp_path: Path
) -> None:
    """The parity #419 asked for, stated directly: one file, both readers, one answer."""
    gate, admin = tmp_path / "gate", tmp_path / "admin"
    gate.mkdir()
    admin.mkdir()
    assert _smoke_test_reads(text, gate) == _router_admin_exports(text, admin)

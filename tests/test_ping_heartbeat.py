"""`.github/scripts/ping-heartbeat.sh` — a heartbeat URL reaches curl on stdin, never argv.

A Better Stack heartbeat URL is the credential: whoever holds it can keep a monitor green.
Both scheduled workflows that report to one used to run `curl … "$HEARTBEAT_URL"`, which
put it on curl's command line (basecradle-router#468). These run the EXACT shipped script
against a fake curl that records its argv and its stdin, and once against the real curl on
loopback to prove the config line is one curl actually reads. No network is reached: the
real-curl case connects to 127.0.0.1 port 1, which refuses. The URLs are fakes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / ".github" / "scripts" / "ping-heartbeat.sh"
WORKFLOWS = REPO / ".github" / "workflows"
URL = "https://uptime.betterstack.com/api/v1/heartbeat/FakeHeartbeat0000000000"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")

FAKE_CURL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >"ARGV"
printf '%s' "${HEARTBEAT_URL-unset}" >"ENVIRON"
cat >"STDIN"
exit "${FAKE_CURL_EXIT:-0}"
"""


def _run(tmp_path: Path, url: str | None, curl_exit: int = 0) -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    curl = bindir / "curl"
    curl.write_text(
        FAKE_CURL.replace('"ARGV"', f'"{tmp_path / "argv"}"')
        .replace('"STDIN"', f'"{tmp_path / "stdin"}"')
        .replace('"ENVIRON"', f'"{tmp_path / "environ"}"')
    )
    curl.chmod(0o755)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_CURL_EXIT": str(curl_exit)}
    if url is not None:
        env["HEARTBEAT_URL"] = url
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)


def test_the_url_reaches_curl_as_a_config_on_stdin_and_never_on_its_argv(tmp_path: Path) -> None:
    result = _run(tmp_path, URL)

    assert result.returncode == 0, result.stderr
    argv = (tmp_path / "argv").read_text()
    assert URL not in argv and "heartbeat" not in argv
    # -q first, so no .curlrc joins the call (curl reads it only if -q is not argv[1]).
    assert argv == "-q -fsS --retry 3 --max-time 15 -o /dev/null --config -\n"
    assert (tmp_path / "stdin").read_text() == f'url = "{URL}"\n'
    # Nor does curl's own environment carry a copy: the script unsets it first.
    assert (tmp_path / "environ").read_text() == "unset"


def test_a_curl_failure_is_the_scripts_failure(tmp_path: Path) -> None:
    result = _run(tmp_path, URL, curl_exit=22)

    assert result.returncode == 22


def test_an_unset_url_fails_loudly_and_runs_nothing(tmp_path: Path) -> None:
    for url in (None, ""):
        result = _run(tmp_path, url)

        assert (result.returncode, "is not set" in result.stderr) == (64, True)
        assert not (tmp_path / "argv").exists()


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://uptime.betterstack.com/api/v1/heartbeat/Fake0000", "is not an https URL"),
        ("https://", "is not an https URL"),
        ('https://uptime.betterstack.com/api/v1/heartbeat/Fake"\noutput = "/tmp/x', "whitespace"),
        ("https://uptime.betterstack.com/api/v1/heartbeat/Fake0000\n", "whitespace"),
        ("https://uptime.betterstack.com/api/v1/heartbeat/Fake 0000", "whitespace"),
        ('https://uptime.betterstack.com/api/v1/heartbeat/Fake"0000', "double quote"),
        ("https://uptime.betterstack.com/api/v1/heartbeat/Fake\\0000", "backslash"),
    ],
    ids=["plain-http", "no-host", "config-injection", "trailing-newline", "space", "quote", "bs"],
)
def test_a_url_that_is_not_plain_https_is_refused_by_name_and_never_printed(
    url: str, reason: str, tmp_path: Path
) -> None:
    # A quote or a newline would let the value write a second curl option; refusing it is
    # what makes the one quoted config line safe without an escaping pass. The refusal
    # names the class, so a stored secret with a stray character is diagnosable unprinted.
    result = _run(tmp_path, url)

    assert result.returncode == 64
    assert reason in result.stderr
    assert "Fake" not in result.stderr
    assert not (tmp_path / "argv").exists()


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is required")
def test_the_real_curl_reads_the_url_from_the_config_line(tmp_path: Path) -> None:
    # Port 1 on loopback refuses at once: the failure names the address, which can only
    # have come from the config on stdin, since nothing else in the call carries one.
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HEARTBEAT_URL": "https://127.0.0.1:1/api/v1/heartbeat/FakeHeartbeat0000000000",
        },
        timeout=120,
    )

    assert result.returncode == 7, result.stderr  # CURLE_COULDNT_CONNECT
    assert re.search(r"127\.0\.0\.1.*\b1\b", result.stderr), result.stderr


def test_every_heartbeat_ping_in_the_workflows_goes_through_the_script() -> None:
    texts = {path.name: path.read_text() for path in sorted(WORKFLOWS.glob("*.yml"))}
    callers = [name for name, text in texts.items() if ".github/scripts/ping-heartbeat.sh" in text]

    assert callers == ["dependency-security-scan.yml", "interaction-limit-renewal.yml"]
    for name, text in texts.items():
        # The secret enters a step only as its HEARTBEAT_URL env entry, never expanded in a
        # run: line, and the step's own bash reads it only to test that it is set.
        for line in re.findall(r".*secrets\.[A-Z_]*HEARTBEAT_URL.*", text):
            assert re.fullmatch(r"\s+HEARTBEAT_URL: \$\{\{ secrets\.[A-Z_]+ \}\}", line), line
        uses = re.findall(r".*\$\{?HEARTBEAT_URL\b.*", text)
        assert all(re.search(r'test -n "\$HEARTBEAT_URL"', line) for line in uses), (name, uses)

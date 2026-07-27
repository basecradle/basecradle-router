"""The `router-admin` wrapper — the one path the NOC's converge and probes call.

It is what the emitted claim's ``prove.cmd`` names, so its two invariants are
load-bearing. **(1) It runs as the daemon's user**: root bypasses file permissions,
so a freeze probe run as root would pass on a box where the daemon itself is locked
out — the exact failure it exists to catch. **(2) It parses `router.env` literally,
never bash-``source``s it** (basecradle-router#109): that file holds the webhook
signing secret, and a `source` would evaluate every value — a secret containing
``$`` would be mangled and one containing ``$( )`` would EXECUTE as the daemon user.

These run the EXACT shipped script, so a regression in it fails here. No model,
agent, or network is touched; secrets are correctly-shaped fakes.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROUTER_ADMIN = Path(__file__).resolve().parents[1] / "deploy" / "bin" / "router-admin"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to exercise the wrapper"
)

# Every shell metacharacter that matters, in one correctly-shaped fake: command
# substitution in both spellings, a variable reference, and both quote characters.
HOSTILE_SECRET = "whsec_$(id -u)`whoami`$HOME'\"x"


def _run_wrapper(tmp_path, env_body: str, *args: str) -> subprocess.CompletedProcess:
    """Run the real wrapper with a fabricated env file, printing what it exported."""
    env_file = tmp_path / "router.env"
    env_file.write_text(env_body, encoding="utf-8")
    # Stop the wrapper before it execs python: we are testing the shell half, and the
    # CLI itself is covered in test_cli.py.
    script = ROUTER_ADMIN.read_text().split("# Prefer the venv's python")[0]
    script += '\nprintf "%s\\n" "$BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET"\n'
    harness = tmp_path / "harness.sh"
    harness.write_text(script, encoding="utf-8")
    return subprocess.run(
        ["bash", str(harness), *args],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "BASECRADLE_ROUTER_ENV_FILE": str(env_file),
            "BASECRADLE_ROUTER_USER": _current_user(),
            "HOME": str(tmp_path),
        },
    )


def _current_user() -> str:
    return subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()


def test_a_secret_full_of_shell_metacharacters_survives_verbatim(tmp_path) -> None:
    # THE #109 property: a credential is data, never code. `set -a; . router.env`
    # would have executed the substitutions and mangled the rest.
    result = _run_wrapper(tmp_path, f"BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET={HOSTILE_SECRET}\n")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == HOSTILE_SECRET


def test_the_wrapper_never_sources_the_env_file() -> None:
    # Belt and braces on the property above: no *executable* line may source the env
    # file, whatever a future edit is tempted to do. Comment lines are excluded — the
    # script documents the forbidden form in order to explain why it is forbidden.
    code = [
        line for line in ROUTER_ADMIN.read_text().splitlines() if not line.lstrip().startswith("#")
    ]

    assert not [line for line in code if "source " in line or '. "$ENV_FILE"' in line]


def test_one_layer_of_surrounding_quotes_is_stripped_as_systemd_does(tmp_path) -> None:
    # systemd's EnvironmentFile= strips one layer, so the CLI's view of the config has
    # to match the running daemon's exactly or the manifest describes a router that
    # does not exist.
    result = _run_wrapper(tmp_path, 'BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET="whsec_quoted"\n')

    assert result.stdout.strip() == "whsec_quoted"


def test_comments_and_non_assignments_are_skipped(tmp_path) -> None:
    result = _run_wrapper(
        tmp_path,
        "# a comment\n\nnot an assignment\n"
        "export BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET=whsec_exported\n",
    )

    assert result.stdout.strip() == "whsec_exported"


def test_an_unreadable_env_file_is_the_config_error_exit_code(tmp_path) -> None:
    # Exit 3 = "the router's own config would not load", which sends the NOC somewhere
    # different from a broken freeze surface (1) or an unprovable one (2).
    result = subprocess.run(
        ["bash", str(ROUTER_ADMIN), "selftest", "freeze"],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "BASECRADLE_ROUTER_ENV_FILE": str(tmp_path / "does-not-exist.env"),
            "BASECRADLE_ROUTER_USER": _current_user(),
            "HOME": str(tmp_path),
        },
    )

    assert result.returncode == 3
    assert "cannot read" in result.stderr


def test_a_non_root_caller_that_is_not_the_daemon_user_refuses_rather_than_pretending(
    tmp_path,
) -> None:
    # The privilege drop needs root. Running as some *other* unprivileged user cannot
    # reach the daemon's credentials, so the wrapper must refuse — a probe that
    # silently ran as the wrong user would report a readability it never proved.
    result = subprocess.run(
        ["bash", str(ROUTER_ADMIN), "selftest", "freeze"],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "BASECRADLE_ROUTER_ENV_FILE": str(tmp_path / "router.env"),
            "BASECRADLE_ROUTER_USER": "some-other-daemon-user",
            "HOME": str(tmp_path),
        },
    )

    assert result.returncode == 3
    assert "must run as" in result.stderr


def test_the_probe_command_the_claim_names_is_the_wrapper_that_ships() -> None:
    # The emitted claim's prove.cmd points at this file's deployed path; if the two
    # ever disagree the NOC schedules a probe that does not exist.
    from basecradle_router.config import DEFAULT_ADMIN_CMD

    assert DEFAULT_ADMIN_CMD.endswith("/deploy/bin/router-admin")
    assert ROUTER_ADMIN.exists()

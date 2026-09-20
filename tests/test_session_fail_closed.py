"""A session in this repo cannot reach @origin's stored GitHub login (#303).

A laptop builder posted under @origin's personal account because a tokenless ``gh``
falls back to whatever login the laptop has stored (``basecradle/basecradle#579``).
The instruction to mint per call was fixed there; this is the half that holds when
the instruction is forgotten — ``.claude/settings.json`` wires every Claude Code
session in this repo fail-closed, and this file is what stops that wiring from
being quietly dropped or weakened.

Offline and hermetic: reads one tracked JSON file and exercises the credential
helper it declares as a plain shell subprocess. No model, no agent, no network, no
real token — the ``GH_TOKEN`` used here is a correctly-shaped fake.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SETTINGS = REPO / ".claude" / "settings.json"

# The one URL the credential helper may ever answer for.
SCOPED_KEY = "credential.https://github.com.helper"
FAKE_TOKEN = "ghs_" + "0" * 36  # correctly-shaped fake; never a live credential


def _env() -> dict[str, str]:
    return json.loads(SETTINGS.read_text())["env"]


def _git_config_pairs() -> list[tuple[str, str]]:
    """The (key, value) pairs the session hands git, in declaration order."""
    env = _env()
    count = int(env["GIT_CONFIG_COUNT"])
    return [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(count)]


def _helper() -> str:
    return _git_config_pairs()[-1][1]


def _ask_helper(action: str, token: str | None) -> subprocess.CompletedProcess[str]:
    """Run the helper as git does: strip the leading ``!``, then ``sh -c '<rest> <action>'``."""
    helper = _helper()
    # git runs a helper through the shell only when its value starts with `!`.
    assert helper.startswith("!")
    env = {"PATH": "/usr/bin:/bin"}
    if token is not None:
        env["GH_TOKEN"] = token
    return subprocess.run(
        ["sh", "-c", f"{helper.removeprefix('!')} {action}"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_settings_are_tracked_so_a_fresh_checkout_is_covered() -> None:
    # The guard has to survive a clone, which is the whole reason it is not
    # .claude/settings.local.json (ignored) or per-clone .git/config (not cloned).
    tracked = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--error-unmatch", ".claude/settings.json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.returncode == 0, tracked.stderr


def test_gh_is_pointed_at_a_config_dir_that_can_hold_no_login() -> None:
    config_dir = Path(_env()["GH_CONFIG_DIR"])
    assert str(config_dir) == "/var/empty"
    assert not (config_dir / "hosts.yml").exists()
    if os.geteuid() == 0:
        return  # root bypasses file permissions, so a root run proves nothing
    # The property behind the path, and it differs by platform, so check the one
    # that actually applies. Present (root-owned, macOS): gh cannot write a hosts
    # file into it. Absent (Ubuntu 24.04, the fleet box): gh cannot create it,
    # which is a fact about /var — asserting on the missing path itself would pass
    # for any nonexistent path and prove nothing at all.
    if config_dir.exists():
        assert not os.access(config_dir, os.W_OK)
    else:
        assert not os.access(config_dir.parent, os.W_OK)


def test_the_credential_helper_list_resets_before_it_adds() -> None:
    pairs = _git_config_pairs()
    # Every entry is URL-scoped to github.com, so a submodule, a redirect or a
    # mistyped remote on another host is never offered the installation token.
    assert [key for key, _ in pairs] == [SCOPED_KEY] * len(pairs)
    # An empty value resets the helper list, dropping the system osxkeychain helper
    # and the global `gh auth git-credential` one — both of which answer as @origin.
    assert pairs[0][1] == ""
    assert len(pairs) >= 2


def test_a_tokenless_request_quits_instead_of_falling_through() -> None:
    result = _ask_helper("get", token=None)
    assert "quit=1" in result.stdout
    assert "password=" not in result.stdout
    # The message has to name the fix, or the error reads as a broken remote.
    assert "GH_TOKEN" in result.stderr


@pytest.mark.parametrize("token", ["", None])
def test_an_empty_token_is_treated_the_same_as_a_missing_one(token: str | None) -> None:
    assert "quit=1" in _ask_helper("get", token=token).stdout


def test_a_minted_token_is_served_as_the_bot() -> None:
    result = _ask_helper("get", token=FAKE_TOKEN)
    assert "username=x-access-token" in result.stdout
    assert f"password={FAKE_TOKEN}" in result.stdout
    assert "quit=1" not in result.stdout


@pytest.mark.parametrize("action", ["store", "erase"])
def test_only_get_is_answered(action: str) -> None:
    # Nothing is ever stored: the token lives in the caller's environment and dies
    # with it, so `store` must not hand it to a keychain to persist.
    result = _ask_helper(action, token=FAKE_TOKEN)
    assert result.stdout.strip() == ""


# The helper a laptop really inherits is an UNSCOPED `credential.helper` in system or
# global config — `osxkeychain`, or `gh auth git-credential` — answering as @origin.
# Standing in for it: a decoy that hands back a marker username, in an isolated global
# config file. Nothing here reads or writes a real credential store.
_STORED_LOGIN_DECOY = "!printf 'username=drawkkwast\\npassword=STORED-LOGIN-MARKER\\n'"


def _credential_fill(
    tmp_path: Path, *, session_env: bool, token: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Ask git for a github.com credential, with the decoy inherited from global config."""
    global_config = tmp_path / "gitconfig"
    # Written through git so the value's quoting is git's problem, not this test's.
    subprocess.run(
        ["git", "config", "--file", str(global_config), "credential.helper", _STORED_LOGIN_DECOY],
        check=True,
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",  # isolate from this machine's real config entirely
        "GIT_CONFIG_GLOBAL": str(global_config),
        "GIT_TERMINAL_PROMPT": "0",
    }
    if session_env:
        env.update(_env())
    if token is not None:
        env["GH_TOKEN"] = token
    return subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_without_the_session_env_the_stored_login_answers(tmp_path: Path) -> None:
    # The control: this is #579. An inherited unscoped helper answers for github.com,
    # and without it the three tests below would pass for the wrong reason.
    result = _credential_fill(tmp_path, session_env=False)
    assert "username=drawkkwast" in result.stdout


def test_the_session_env_clears_an_inherited_unscoped_helper(tmp_path: Path) -> None:
    # The mechanic the guard rests on: a *URL-scoped* empty value resets the helper
    # list for that URL, including helpers inherited unscoped from system/global.
    result = _credential_fill(tmp_path, session_env=True)
    assert result.returncode != 0
    assert "drawkkwast" not in result.stdout
    assert "STORED-LOGIN-MARKER" not in result.stdout
    assert "told us to quit" in result.stderr


def test_with_a_minted_token_the_bot_answers_instead_of_the_stored_login(
    tmp_path: Path,
) -> None:
    result = _credential_fill(tmp_path, session_env=True, token=FAKE_TOKEN)
    assert result.returncode == 0, result.stderr
    assert "username=x-access-token" in result.stdout
    assert "drawkkwast" not in result.stdout


def test_git_resolves_the_session_helper_last_overriding_a_stored_one(tmp_path: Path) -> None:
    # The ordering underneath that: whatever a machine has configured for github.com,
    # the session's entries are applied after it — after system, global AND local.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "--local", "--add", SCOPED_KEY, "!echo username=x"],
        check=True,
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **_env()}
    resolved = subprocess.run(
        ["git", "-C", str(tmp_path), "config", "--get-all", SCOPED_KEY],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.splitlines()
    assert resolved[0] == "!echo username=x"  # the stored one is still on the list…
    reset = resolved.index("")  # …until the session's empty value resets it,
    assert resolved[reset + 1 :] == [_helper()]  # leaving only the fail-closed helper


def test_origin_is_an_https_remote_so_the_helper_is_consulted_at_all() -> None:
    # The guard binds `https://github.com` only: an ssh:// remote, or a global
    # `url."git@github.com:".insteadOf`, never consults a credential helper and would
    # push under @origin's SSH key instead. `--get-url` reports the URL git will
    # actually use, rewrites applied, so this pins the precondition the claim rests on.
    url = subprocess.run(
        ["git", "-C", str(REPO), "ls-remote", "--get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert url.startswith("https://github.com/"), url


def test_a_local_settings_override_declares_no_environment() -> None:
    # .claude/settings.local.json outranks the tracked settings.json and is gitignored,
    # so an `env` block there could restore the @origin fallback on one machine while
    # every test above — which reads only the tracked file — stayed green. That is the
    # green-while-absent shape this repo exists to refuse.
    local = REPO / ".claude" / "settings.local.json"
    if not local.exists():
        return
    assert "env" not in json.loads(local.read_text())


def test_the_live_session_environment_matches_the_tracked_settings() -> None:
    # And the direct check, when there is a session to check: every variable the
    # tracked file declares that is actually set must still hold the declared value.
    declared = _env()
    live = {key: os.environ[key] for key in declared if key in os.environ}
    if not live:
        pytest.skip("no session applied these — nothing to compare (a plain CI run)")
    assert live == {key: declared[key] for key in live}

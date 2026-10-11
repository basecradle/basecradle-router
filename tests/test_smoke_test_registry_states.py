"""The live smoke test's basecradle cases on each registry state a booted daemon can be in.

`deploy/smoke-test.sh` reads the agent registry to pick the recipient its basecradle
cases sign for. It used to treat "no agent holds a per-recipient key and the shared
fallback is retired" as a state the daemon cannot boot in, and died there. That is true
only while a harness agent is registered: `load_recipient_keyring` refuses a retired
fallback by naming the unprovisioned agents, so with none registered it has no one to
name and the daemon boots. Every new router host is in exactly that state on its first
deploy, so its smoke gate died on a healthy daemon (#338, found on
`ubuntu-1.landfill.click`, basecradle-noc#1047).

The WHOLE shipped script is run here against a fake box — `curl` answers the way a daemon
in the given configuration answers, `journalctl` holds the decision lines, and case 9's
namespace probe reports `hidden` — so the outcome is read off the gate's own exit status
and the requests it actually sent, not off a variable it happens to set. Three states:

* **no harness agent, fallback retired** (a new host): the gate passes, skips cases 4 and
  5 saying why, and still asserts case 6's 401;
* **an agent with its own key, fallback retired** (`ai`): cases 4, 5 and 6 all run,
  unchanged;
* **an agent registered with no key, fallback retired**: still a death — the daemon
  really cannot boot there, so a gate that passed would be asserting a box that does not
  exist.

No network, journal, model, or agent is touched. Fabricated cast: Nova Digital (`nova`,
AI). Secrets are correctly-shaped fakes; the uuid is a well-formed UUIDv7.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from basecradle_router.routes.basecradle import RECIPIENT_SECRET_PREFIX, SHARED_FALLBACK_VAR

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
SMOKE_TEST = DEPLOY / "smoke-test.sh"
HOST = "ubuntu-1.landfill.click"

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("bash", "openssl", "jq")),
    reason="bash, openssl and jq are required to run the shipped smoke test",
)

NOVA_UUID = "01980a3c-6f2e-7c41-9d3a-5b8e2f1c4a07"
NOVA_KEY = "bc_isk_fakenovasmokeintegrationkey00001"
GITHUB_SECRET = "ghs-fake-0000000000000000000000000000"
BC_SECRET = "bc-fake-00000000000000000000000000000"

# The delivery ids the script sends cases 4/5 and case 6 under — copied, not imported:
# a rename on one side should fail here, not silently re-point the test.
CASE_45_DELIVERY = "smoke-basecradle-00000000-0000-0000-0000-000000000000"
CASE_6_DELIVERY = "smoke-basecradle-fallback-00000000-0000-0000-0000-000000000000"
ZERO_SIG = "sha256=" + "0" * 64

#: A github builder is not a basecradle recipient: a host carrying only builders has no
#: harness agent for the keyring to refuse, exactly like an empty registry.
BUILDER = {
    "basecradle/basecradle-router": {
        "os_user": "basecradle-router-ai",
        "clone_path": "/home/basecradle-router-ai/repos/basecradle-router",
        "bot_slug": "basecradle-router-ai",
    }
}
NOVA = {
    "nova": {
        "kind": "harness",
        "os_user": "nova",
        "clone_path": "/home/nova/harness",
        "recipient_uuid": NOVA_UUID,
        "wake_bin": "/home/nova/venv/bin/basecradle-harness-wake",
    }
}

#: Every line the gate's journal assertions look for, so a pass or fail below is the
#: control flow's doing, never the journal's. The regexes themselves are pinned against
#: the route's real rendering in tests/test_smoke_test_assertions.py.
JOURNAL = "\n".join(
    [
        "event=delivery_decision source=github event_type=issues decision=woke "
        "recipient=basecradle/__router-smoke-test-do-not-register__ "
        "delivery=smoke-00000000-0000-0000-0000-000000000000",
        f"event=verify_key source=basecradle key_path=recipient delivery={CASE_45_DELIVERY}",
        "event=delivery_decision source=basecradle event_type=timeline.locked "
        f"decision=ignored delivery={CASE_45_DELIVERY}",
    ]
)

#: A daemon with the fallback retired. github: a zero digest is 401, the untrusted actor
#: 400, anything else 200. basecradle: a zero digest is 401, and so is case 6 — the
#: unregistered recipient has no key once the fallback is retired — while a correctly
#: signed delivery for a keyed agent is 200. probe: the public path is denied, loopback
#: rejects the unsigned delivery. Every request is logged as `<url> <delivery> <status>`.
FAKE_CURL = textwrap.dedent(
    """\
    url="" sig="" delivery="" body=""
    while (($#)); do
      case "$1" in
        -H) case "$2" in
              X-Hub-Signature-256:*|X-BaseCradle-Signature:*) sig="${2#*: }" ;;
              X-GitHub-Delivery:*|X-BaseCradle-Delivery:*) delivery="${2#*: }" ;;
            esac; shift ;;
        --data-binary) body="$2"; shift ;;
        http*) url="$1" ;;
      esac
      shift
    done
    case "$url" in
      */webhooks/github)
        if [[ "$sig" == "ZERO" ]]; then code=401
        elif grep -q router-smoke-untrusted-actor "${body#@}"; then code=400
        else code=200; fi ;;
      */webhooks/basecradle)
        if [[ "$sig" == "ZERO" || "$delivery" == "CASE6" ]]; then code=401
        else code=200; fi ;;
      https://*/webhooks/probe) code=404 ;;
      *) code=401 ;;
    esac
    printf '%s %s %s\\n' "$url" "$delivery" "$code" >>"LOG"
    printf '%s' "$code"
    """
)


def _fake_bin(tmp_path: Path, log: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    journal = tmp_path / "journal.txt"
    journal.write_text(JOURNAL + "\n")
    curl = (
        FAKE_CURL.replace('"ZERO"', f'"{ZERO_SIG}"')
        .replace('"CASE6"', f'"{CASE_6_DELIVERY}"')
        .replace('"LOG"', f'"{log}"')
    )
    fakes = {
        "curl": curl,
        "journalctl": f'cat "{journal}"\n',
        # Case 9: a running unit under an unprivileged User=, whose namespace hides PID 1.
        "systemctl": textwrap.dedent(
            """\
            case "$*" in
              *MainPID*) printf 4242 ;;
              *User*) printf basecradle-router ;;
            esac
            """
        ),
        "nsenter": "echo hidden\n",
        "sleep": "exit 0\n",
    }
    for name, body in fakes.items():
        fake = bindir / name
        fake.write_text("#!/usr/bin/env bash\n" + body)
        fake.chmod(0o755)
    return bindir


def _run(
    tmp_path: Path, *, registry: dict[str, object], keys: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], list[tuple[str, str, str]]]:
    agents = tmp_path / "agents.json"
    agents.write_text(json.dumps(registry))
    env_file = tmp_path / "router.env"
    env_file.write_text(
        f"BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET={GITHUB_SECRET}\n"
        "BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS=nova\n"
        f"BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET={BC_SECRET}\n"
        f"{SHARED_FALLBACK_VAR}=0\n"
        f"BASECRADLE_ROUTER_AGENTS={agents}\n"
        + "".join(f"{RECIPIENT_SECRET_PREFIX}{suffix}={key}\n" for suffix, key in keys.items())
    )
    log = tmp_path / "requests.log"
    log.touch()
    bindir = _fake_bin(tmp_path, log)
    result = subprocess.run(
        ["bash", str(SMOKE_TEST), HOST],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "ROUTER_ENV_FILE": str(env_file),
        },
    )
    requests = [tuple(line.split(" ")) for line in log.read_text().splitlines()]
    return result, requests  # type: ignore[return-value]


def _basecradle(requests: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    return [(d, code) for url, d, code in requests if url.endswith("/webhooks/basecradle")]


@pytest.mark.parametrize("registry", [{}, BUILDER], ids=["empty", "builders-only"])
def test_a_host_with_no_harness_agent_and_the_fallback_retired_passes(
    registry: dict[str, object], tmp_path: Path
) -> None:
    result, requests = _run(tmp_path, registry=registry, keys={})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LIVE SMOKE TEST PASSED" in result.stdout
    assert "could not have booted" not in result.stderr
    assert "skipping cases 4 and 5 (no key exists to verify under)" in result.stdout
    # The route probe and case 6 — and nothing under the case 4/5 delivery id.
    assert _basecradle(requests) == [(CASE_6_DELIVERY, "401"), (CASE_6_DELIVERY, "401")]
    assert "PASS  basecradle retired shared fallback refuses an unknown recipient" in (
        result.stdout
    )
    assert "basecradle verified on key_path" not in result.stdout


def test_a_host_whose_agent_holds_its_own_key_runs_every_case_unchanged(tmp_path: Path) -> None:
    """`ai`'s shape: the no-agents branch must not have taken cases 4 and 5 from it."""
    result, requests = _run(tmp_path, registry=NOVA, keys={"NOVA": NOVA_KEY})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "basecradle recipient under test: nova (its own per-recipient key)" in result.stdout
    assert _basecradle(requests) == [
        (CASE_6_DELIVERY, "401"),  # the route probe
        (CASE_45_DELIVERY, "401"),  # case 4, forged digest
        (CASE_45_DELIVERY, "200"),  # case 5, valid digest
        (CASE_6_DELIVERY, "401"),  # case 6, retired fallback
    ]
    assert "PASS  basecradle verified on key_path=recipient" in result.stdout


def test_a_registered_agent_with_no_key_and_the_fallback_retired_still_dies(
    tmp_path: Path,
) -> None:
    """The daemon refuses to boot here, so the gate must refuse to call it healthy."""
    result, requests = _run(tmp_path, registry=NOVA, keys={})
    assert result.returncode == 1
    assert "a harness agent is registered but none holds a per-recipient signing key" in (
        result.stderr
    )
    assert CASE_45_DELIVERY not in {d for _, d, _ in requests}

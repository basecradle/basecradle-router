"""The live smoke test's github cases run exactly where the daemon enables github (#417).

`deploy/smoke-test.sh` used to demand the github route's secret and trusted actors and
assert cases 1-3 on every box. `red.basecradle.com` is the first router host whose daemon
does not enable github at all (harness-only, `BASECRADLE_ROUTER_ENABLED_ROUTES=basecradle`),
so its `deploy-router` smoke gate could never pass.

Cases 1-3 now gate on the daemon's own enabled-routes list, never on the secret's absence
alone — a github box that lost its secret must still die rather than quietly skip the
trust-gate proof. A github-less box skips them and asserts instead that the running
daemon answers an unsigned github delivery 404, so a router.env the gate reads one way
and systemd another fails loudly rather than skipping the proof it owes.

The WHOLE shipped script runs against a fake box — `curl` answers the way a daemon serving
the given routes answers, `journalctl` holds the decision lines, case 9's namespace probe
reports `hidden` — and every assertion reads the gate's exit status and the requests it
actually sent. The parse itself is pinned to the daemon's `load_config` in
tests/test_smoke_test_assertions.py.

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
from caddy_edge import edge_headers

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
SMOKE_TEST = DEPLOY / "smoke-test.sh"
HOST = "red.basecradle.com"

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("bash", "openssl", "jq")),
    reason="bash, openssl and jq are required to run the shipped smoke test",
)

NOVA_UUID = "01980a3c-6f2e-7c41-9d3a-5b8e2f1c4a07"
NOVA_KEY = "bc_isk_fakenovasmokeintegrationkey00001"
GITHUB_SECRET = "ghs-fake-0000000000000000000000000000"
REGISTRY = {
    "nova": {
        "kind": "harness",
        "os_user": "nova",
        "clone_path": "/home/nova/harness",
        "recipient_uuid": NOVA_UUID,
        "wake_bin": "/home/nova/venv/bin/basecradle-harness-wake",
    }
}

GITHUB_DELIVERY = "smoke-00000000-0000-0000-0000-000000000000"
CASE_45_DELIVERY = "smoke-basecradle-00000000-0000-0000-0000-000000000000"
CASE_6_DELIVERY = "smoke-basecradle-fallback-00000000-0000-0000-0000-000000000000"
ZERO_SIG = "sha256=" + "0" * 64
GITHUB_LOCAL = "http://127.0.0.1:8000/webhooks/github"

GITHUB_JOURNAL = [
    "event=delivery_decision source=github event_type=issues decision=woke "
    f"recipient=basecradle/__router-smoke-test-do-not-register__ delivery={GITHUB_DELIVERY}",
]
BASECRADLE_JOURNAL = [
    f"event=verify_key source=basecradle key_path=recipient delivery={CASE_45_DELIVERY}",
    "event=delivery_decision source=basecradle event_type=timeline.locked "
    f"decision=ignored delivery={CASE_45_DELIVERY}",
]

#: A daemon with the basecradle fallback retired, serving github only when GITHUB_SERVED
#: is 1 — otherwise `/webhooks/github` is an unknown route, the daemon's 404. github: a
#: zero digest is 401, the untrusted actor 400, anything else 200. basecradle: a zero
#: digest and case 6 are 401, a correctly signed keyed delivery 200. probe: the public
#: path is denied, loopback rejects. Every request is logged as `<url> <delivery> <status>`.
FAKE_CURL = textwrap.dedent(
    """\
    url="" sig="" delivery="" body="" dump="" proto="HTTP/1.1"
    while (($#)); do
      case "$1" in
        -D) dump="$2"; shift ;;
        --http2) proto="HTTP/2" ;;
        -H) case "$2" in
              X-Hub-Signature-256:*|X-BaseCradle-Signature:*) sig="${2#*: }" ;;
              X-GitHub-Delivery:*|X-BaseCradle-Delivery:*) delivery="${2#*: }" ;;
            esac; shift ;;
        --data-binary) body="$2"; shift ;;
        http*) url="$1" ;;
      esac
      shift
    done
    # Case 10: an edge that sends exactly the headers the host's Caddyfile sets (#437).
    if [[ "$url" == */up ]]; then
      { printf '%s 200 \\r\\n' "$proto"; cat "EDGE"; printf '\\r\\n'; } >"$dump"
      exit 0
    fi
    case "$url" in
      */webhooks/github)
        if [[ "GITHUB_SERVED" != 1 ]]; then code=404
        elif [[ "$sig" == "ZERO" ]]; then code=401
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


def _edge_headers(tmp_path: Path) -> Path:
    """The header block a correct edge sends on HOST's /up: each `header` its Caddyfile sets."""
    lines = edge_headers((DEPLOY / "hosts" / HOST / "Caddyfile").read_text(), "/up")
    path = tmp_path / "edge-headers.txt"
    path.write_text("".join(f"{name}: {value}\r\n" for name, value in lines))
    return path


def _run(
    tmp_path: Path, *, env_lines: list[str], github_served: bool
) -> tuple[subprocess.CompletedProcess[str], list[tuple[str, str, str]]]:
    agents = tmp_path / "agents.json"
    agents.write_text(json.dumps(REGISTRY))
    env_file = tmp_path / "router.env"
    env_file.write_text(
        "\n".join(
            [
                *env_lines,
                f"{SHARED_FALLBACK_VAR}=0",
                f"BASECRADLE_ROUTER_AGENTS={agents}",
                f"{RECIPIENT_SECRET_PREFIX}NOVA={NOVA_KEY}",
            ]
        )
        + "\n"
    )
    log = tmp_path / "requests.log"
    log.touch()
    journal = tmp_path / "journal.txt"
    # The github lines are in the journal only where a daemon serving github wrote them,
    # so a github-less run that still asserted them would fail here.
    journal.write_text("\n".join((GITHUB_JOURNAL if github_served else []) + BASECRADLE_JOURNAL))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fakes = {
        "curl": FAKE_CURL.replace('"ZERO"', f'"{ZERO_SIG}"')
        .replace('"CASE6"', f'"{CASE_6_DELIVERY}"')
        .replace('"LOG"', f'"{log}"')
        .replace('"EDGE"', f'"{_edge_headers(tmp_path)}"')
        .replace('"GITHUB_SERVED"', '"1"' if github_served else '"0"'),
        "journalctl": f'cat "{journal}"\n',
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
    result = subprocess.run(
        ["bash", str(SMOKE_TEST), HOST],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "ROUTER_ENV_FILE": str(env_file),
            "CADDYFILE": str(DEPLOY / "hosts" / HOST / "Caddyfile"),
        },
    )
    requests = [tuple(line.split(" ")) for line in log.read_text().splitlines()]
    return result, requests  # type: ignore[return-value]


def _route(requests: list[tuple[str, str, str]], route: str) -> list[str]:
    return [code for url, _, code in requests if url.endswith(f"/webhooks/{route}")]


#: The basecradle requests a keyed box with the fallback retired and no shared value
#: answers, in order: the route probe, case 4, case 5 (case 6 has nothing to refuse).
BASECRADLE_CODES = ["401", "401", "200"]

GITHUB_VARS = [
    f"BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET={GITHUB_SECRET}",
    "BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS=nova",
]


@pytest.mark.parametrize(
    "routes", [None, "github,basecradle", " basecradle , github "], ids=["default", "listed", "ws"]
)
def test_a_github_box_runs_cases_1_to_3_unchanged(routes: str | None, tmp_path: Path) -> None:
    """Absent, the variable is the daemon's default — github — exactly as before #417."""
    lines = GITHUB_VARS + ([] if routes is None else [f"BASECRADLE_ROUTER_ENABLED_ROUTES={routes}"])
    result, requests = _run(tmp_path, env_lines=lines, github_served=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _route(requests, "github") == ["401", "400", "200"]
    assert "PASS  delivery id threaded onto the decision line (#170)" in result.stdout
    assert "github route not enabled" not in result.stdout
    assert "enforces the #52 trust gate" in result.stdout
    assert _route(requests, "basecradle") == BASECRADLE_CODES
    assert _route(requests, "probe") == ["404"]


@pytest.mark.parametrize("missing", GITHUB_VARS, ids=["secret", "trusted-actors"])
@pytest.mark.parametrize("routes", [None, "github,basecradle"], ids=["default", "listed"])
def test_a_github_box_missing_its_github_config_still_dies(
    routes: str | None, missing: str, tmp_path: Path
) -> None:
    """The daemon demands both here, so the gate must die — never skip to a pass."""
    lines = [line for line in GITHUB_VARS if line != missing]
    if routes is not None:
        lines.append(f"BASECRADLE_ROUTER_ENABLED_ROUTES={routes}")
    result, requests = _run(tmp_path, env_lines=lines, github_served=True)
    assert result.returncode == 1
    assert f"{missing.split('=')[0]} not set" in result.stderr
    assert requests == []


@pytest.mark.parametrize(
    "github_lines",
    [[], GITHUB_VARS, ["BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS="]],
    ids=["none", "leftover", "leftover-empty"],
)
def test_a_github_less_box_skips_cases_1_to_3_and_passes(
    github_lines: list[str], tmp_path: Path
) -> None:
    """`red`'s shape: the basecradle and probe cases run; the github vars are never read."""
    lines = [*github_lines, "BASECRADLE_ROUTER_ENABLED_ROUTES=basecradle"]
    result, requests = _run(tmp_path, env_lines=lines, github_served=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "github route not enabled; skipping cases 1-3" in result.stdout
    # One unsigned delivery, and nothing signed: the skip proven live, no case 1-3 sent —
    # on loopback, so the 404 is the daemon's and never Caddy's.
    assert _route(requests, "github") == ["404"]
    assert [url for url, _, _ in requests if url.endswith("/webhooks/github")] == [GITHUB_LOCAL]
    assert f"Smoke-testing the live daemon on {HOST} at https://{HOST}/webhooks\n" in (
        result.stdout.replace("\x1b[0m", "")
    )
    assert "PASS  github route not served by the running daemon" in result.stdout
    assert "Trusted actor under test" not in result.stdout
    assert "github decision line emitted" not in result.stdout
    assert "delivery id threaded onto the decision line" not in result.stdout
    assert _route(requests, "basecradle") == BASECRADLE_CODES
    assert "PASS  basecradle verified on key_path=recipient" in result.stdout
    assert _route(requests, "probe") == ["404"]
    assert "PASS  other accounts' argv hidden" in result.stdout
    # Case 10 ran, not skipped: the floor, then each of red's headers over both protocols.
    assert result.stdout.count("  PASS  edge header ") == 6 + 6 * 2
    assert "FAIL" not in result.stdout
    assert "github is not enabled on this daemon" in result.stdout
    assert "enforces the #52 trust gate" not in result.stdout


def test_a_github_less_router_env_on_a_daemon_that_serves_github_fails(tmp_path: Path) -> None:
    """The gate and the daemon disagree about the box: that is a failure, never a skip."""
    lines = [*GITHUB_VARS, "BASECRADLE_ROUTER_ENABLED_ROUTES=basecradle"]
    result, requests = _run(tmp_path, env_lines=lines, github_served=True)
    assert result.returncode == 1
    assert "FAIL  github route not served by the running daemon: HTTP 401" in result.stdout
    assert [(url, code) for url, _, code in requests if url.endswith("/webhooks/github")] == [
        (GITHUB_LOCAL, "401")
    ]
    assert "LIVE SMOKE TEST FAILED" in result.stdout


@pytest.mark.parametrize("raw", ["", " , "], ids=["empty", "blank-list"])
def test_an_enabled_routes_list_naming_no_route_dies(raw: str, tmp_path: Path) -> None:
    """The daemon refuses to boot on it, so the gate must not describe it as either box."""
    lines = [*GITHUB_VARS, f"BASECRADLE_ROUTER_ENABLED_ROUTES={raw}"]
    result, requests = _run(tmp_path, env_lines=lines, github_served=True)
    assert result.returncode == 1
    assert "lists no routes" in result.stderr
    assert requests == []

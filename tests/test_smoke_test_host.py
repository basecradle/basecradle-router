"""The live smoke test targets the router host it is given, and only that host (#334).

`deploy/smoke-test.sh` used to hardcode `https://ai.basecradle.com/webhooks/github` and
derive its other public URLs from it, and the NOC runs it with no URL. On a second router
host the gate would therefore have passed against ai's endpoint while the box it had just
deployed went untested. It now takes the host's `<fqdn>` — the token the NOC's per-host
ops already take — and derives every URL it sends to from it.

Two layers pin that:

* the WHOLE shipped script is run against a fake `curl` that records each URL it is asked
  to POST, once per host in `deploy/hosts/`, so "targets the host it is given" is read off
  the requests the gate actually makes rather than off a variable it happens to set;
* the shipped `smoke_host` body — extracted between its markers — is run over the shapes
  it must refuse, so a typo, a path, or a host this tree does not carry dies before a
  single request is sent.

The no-argument case is the expand half of an expand-then-contract roll: the installed
NOC wrapper calls the script bare, so until it passes the host the gate keeps targeting
ai and says so on stderr. That is pinned too, so the fallback cannot quietly become
something other than ai — or silent.

No network, journal, or real secret is touched. Secrets are correctly-shaped fakes; the
trusted actor is the fabricated `nova`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from urllib.parse import urlsplit

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
SMOKE_TEST = DEPLOY / "smoke-test.sh"
HOSTS = sorted(p.name for p in (DEPLOY / "hosts").iterdir() if p.is_dir())
FALLBACK = "ai.basecradle.com"
LOOPBACK = "http://127.0.0.1:8000/webhooks/probe"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("openssl") is None,
    reason="bash and openssl are required to run the shipped smoke test",
)

#: Every route's secret set, so every public URL the gate can send to is exercised.
ROUTER_ENV = textwrap.dedent(
    """\
    BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET=ghs-fake-0000000000000000000000000000
    BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS=nova
    BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET=bc-fake-00000000000000000000000000000
    BASECRADLE_ROUTER_PROBE_WEBHOOK_SECRET=probe-fake-000000000000000000000000000
    """
)


def _fake_bin(tmp_path: Path, log: Path) -> Path:
    """`curl` records its URL and answers 404; the rest make the run fast and inert.

    404 is the one answer that keeps every self-gated case skipped, so the run sends
    exactly the requests the gate always sends and never reaches the registry read.
    `systemctl` answers nothing, so case 9 reports a FAIL instead of entering a namespace.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = f'for a in "$@"; do case "$a" in http*) printf \'%s\\n\' "$a" >>"{log}";; esac; done\n'
    fakes = {
        "curl": record + "printf 404\n",
        "journalctl": "exit 0\n",
        "systemctl": "exit 0\n",
        "sleep": "exit 0\n",
    }
    for name, body in fakes.items():
        fake = bindir / name
        fake.write_text("#!/usr/bin/env bash\n" + body)
        fake.chmod(0o755)
    return bindir


def _run_smoke(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    log = tmp_path / "urls.log"
    log.touch()
    env_file = tmp_path / "router.env"
    env_file.write_text(ROUTER_ENV)
    bindir = _fake_bin(tmp_path, log)
    result = subprocess.run(
        ["bash", str(SMOKE_TEST), *args],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "ROUTER_ENV_FILE": str(env_file),
        },
    )
    return result, log.read_text().splitlines()


def _public(urls: list[str]) -> list[str]:
    return [u for u in urls if u != LOOPBACK]


@pytest.mark.parametrize("host", HOSTS)
def test_every_public_request_goes_to_the_host_it_was_given(host: str, tmp_path: Path) -> None:
    result, urls = _run_smoke(tmp_path, host)
    public = _public(urls)
    # The github cases, the basecradle route probe, and the public probe deny — every
    # route's public URL, each derived from <fqdn>.
    assert {urlsplit(u).path for u in public} == {
        "/webhooks/github",
        "/webhooks/basecradle",
        "/webhooks/probe",
    }, result.stderr
    assert {urlsplit(u).netloc for u in public} == {host}
    assert all(u.startswith(f"https://{host}/webhooks/") for u in public)
    assert LOOPBACK in urls, "the loopback probe case must still run, and stay on loopback"
    assert f"Smoke-testing the live daemon on {host} at https://{host}/webhooks/github" in (
        result.stdout
    )


def test_a_second_host_never_reaches_ai(tmp_path: Path) -> None:
    others = [h for h in HOSTS if h != FALLBACK]
    assert others, "this pin needs a router host other than ai in deploy/hosts/"
    for host in others:
        run_dir = tmp_path / host
        run_dir.mkdir()
        result, urls = _run_smoke(run_dir, host)
        assert urls, result.stderr
        assert not [u for u in urls if FALLBACK in u]
        assert "transitional default" not in result.stderr


def test_with_no_host_it_targets_ai_and_says_so(tmp_path: Path) -> None:
    result, urls = _run_smoke(tmp_path)
    assert {urlsplit(u).netloc for u in _public(urls)} == {FALLBACK}
    assert f"no <fqdn> given — targeting {FALLBACK}, the transitional default (#334)" in (
        result.stderr
    )


@pytest.mark.parametrize(
    "args",
    [
        ("example.com",),  # well-formed, but not a router host this tree carries
        ("AI.BASECRADLE.COM",),
        ("ai.basecradle.com/../x",),
        ("https://ai.basecradle.com",),
        (FALLBACK, "extra"),
    ],
)
def test_a_refused_host_sends_nothing(args: tuple[str, ...], tmp_path: Path) -> None:
    result, urls = _run_smoke(tmp_path, *args)
    assert result.returncode != 0
    assert urls == []


# --- the shipped smoke_host body ------------------------------------------------------


def _smoke_host_body() -> str:
    match = re.search(
        r"# >>> smoke_host >>>\n(.*?)# <<< smoke_host <<<\n", SMOKE_TEST.read_text(), re.DOTALL
    )
    assert match, "smoke_host marker block not found in deploy/smoke-test.sh"
    return match.group(1)


def _smoke_host(fqdn: str, hosts_dir: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "harness.sh"
    script.write_text(
        'set -euo pipefail\nHOSTS_DIR="$2"\n' + _smoke_host_body() + 'smoke_host "$1"\n'
    )
    return subprocess.run(
        ["bash", str(script), fqdn, str(hosts_dir)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )


def _hosts_dir(tmp_path: Path, *hosts: str) -> Path:
    hosts_dir = tmp_path / "hosts"
    hosts_dir.mkdir()
    for host in hosts:
        (hosts_dir / host).mkdir()
    return hosts_dir


@pytest.mark.parametrize(
    "fqdn",
    ["ubuntu-1.landfill.click", "a.b.example.com", "x1.example.io", f"{'a' * 63}.example.com"],
)
def test_smoke_host_accepts_a_router_host_this_tree_carries(fqdn: str, tmp_path: Path) -> None:
    result = _smoke_host(fqdn, _hosts_dir(tmp_path, fqdn), tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == fqdn
    assert result.stderr == ""


@pytest.mark.parametrize(
    "fqdn",
    [
        "localhost",  # one label
        "Ubuntu-1.landfill.click",  # upper case
        "-a.example.com",  # label starts with a hyphen
        "a-.example.com",  # label ends with a hyphen
        "a..example.com",  # empty label
        "a.example.123",  # last label starts with a digit
        "a.example.com.",  # trailing dot
        "a/b.example.com",
        "a.example.com:443",
        "user@a.example.com",
        "a.example.com\nb.example.com",
        f"{'a' * 64}.example.com",  # a label over 63 bytes
        ".".join(["a" * 63] * 4) + ".com",  # a name over 253 bytes
    ],
)
def test_smoke_host_refuses_a_malformed_name(fqdn: str, tmp_path: Path) -> None:
    hosts_dir = tmp_path / "hosts"
    hosts_dir.mkdir()
    result = _smoke_host(fqdn, hosts_dir, tmp_path)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "invalid <fqdn>" in result.stderr


def test_smoke_host_refuses_a_host_this_tree_does_not_carry(tmp_path: Path) -> None:
    result = _smoke_host("b.example.com", _hosts_dir(tmp_path, "a.example.com"), tmp_path)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "no deploy/hosts/b.example.com/" in result.stderr


def test_smoke_host_refuses_a_symlinked_host_directory(tmp_path: Path) -> None:
    hosts_dir = _hosts_dir(tmp_path, "a.example.com")
    (hosts_dir / "b.example.com").symlink_to(hosts_dir / "a.example.com")
    result = _smoke_host("b.example.com", hosts_dir, tmp_path)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "symlink" in result.stderr


def test_smoke_host_refuses_a_symlinked_hosts_directory(tmp_path: Path) -> None:
    real = _hosts_dir(tmp_path, "a.example.com")
    linked = tmp_path / "linked-hosts"
    linked.symlink_to(real)
    result = _smoke_host("a.example.com", linked, tmp_path)
    assert result.returncode == 1
    assert "symlink" in result.stderr


def test_smoke_host_falls_back_to_ai_loudly_and_only_while_ai_is_carried(tmp_path: Path) -> None:
    result = _smoke_host("", _hosts_dir(tmp_path, FALLBACK), tmp_path)
    assert result.returncode == 0
    assert result.stdout == FALLBACK
    assert "transitional default (#334)" in result.stderr
    # The fallback goes through the same binding as a named host: it is never a bypass.
    other = tmp_path / "other"
    other.mkdir()
    result = _smoke_host("", _hosts_dir(other, "ubuntu-1.landfill.click"), other)
    assert result.returncode == 1
    assert result.stdout == ""

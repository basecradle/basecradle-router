"""Per-host deploy config, pinned against the shipped bytes (#326).

The router runs on more than one box, and each box installs its own copy of two files
from the deployed tree: ``deploy/hosts/<fqdn>/Caddyfile`` and
``deploy/hosts/<fqdn>/vector.yaml``. The NOC installs those bytes as they are, with no
rendering step, so the only thing that can keep the copies honest is this file. It
holds three things:

* every host carries exactly the two files, and no host is missing from the table below;
* each host's files are the reference host's with only that host's own values swapped
  in, so a scrub rule or a proxy directive can never land on one box and not another;
* hosts on different root domains never share a Better Stack source (@origin,
  2026-09-21: a User on one source never writes to another).

No network, Caddy, or Vector binary is involved. This reads files off disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
HOSTS_DIR = DEPLOY / "hosts"

#: The host every other host's files are derived from. It is the production router box.
REFERENCE = "ai.basecradle.com"

#: Each host's Better Stack ingest host: the one value its vector.yaml differs by. A new
#: host directory with no entry here fails the suite, so a copy of ai's files can never
#: ship ai's sink to another box by omission.
INGEST_HOSTS = {
    "ai.basecradle.com": "s2531770.eu-fsn-3.betterstackdata.com",
    "ubuntu-1.landfill.click": "s2780873.eu-central-1a.betterstackdata.com",
}

#: The files every host directory carries, and nothing else.
HOST_FILES = {"Caddyfile", "vector.yaml"}


def _read(host: str, name: str) -> str:
    return (HOSTS_DIR / host / name).read_text()


def _root_domain(fqdn: str) -> str:
    # The last two labels. Exact for every domain the fleet uses (.com, .click); a
    # host under a multi-label public suffix such as .co.uk would need the real list.
    return ".".join(fqdn.split(".")[-2:])


def test_every_host_directory_is_declared_and_complete() -> None:
    hosts = {p.name for p in HOSTS_DIR.iterdir() if p.is_dir()}
    assert hosts == set(INGEST_HOSTS), "deploy/hosts/ and INGEST_HOSTS must name the same hosts"
    for host in hosts:
        files = {p.name for p in (HOSTS_DIR / host).iterdir()}
        assert files == HOST_FILES, f"{host} carries {sorted(files)}, not {sorted(HOST_FILES)}"


@pytest.mark.parametrize("host", sorted(INGEST_HOSTS))
def test_a_hosts_vector_config_differs_from_the_reference_only_by_its_sink(host: str) -> None:
    reference = _read(REFERENCE, "vector.yaml")
    ours = _read(host, "vector.yaml")
    assert ours == reference.replace(INGEST_HOSTS[REFERENCE], INGEST_HOSTS[host])


@pytest.mark.parametrize("host", sorted(INGEST_HOSTS))
def test_a_hosts_logs_and_metrics_both_ship_to_its_own_source(host: str) -> None:
    sinks = [line.strip() for line in _read(host, "vector.yaml").splitlines() if "uri:" in line]
    ingest = INGEST_HOSTS[host]
    assert sinks == [f'uri: "https://{ingest}/"', f'uri: "https://{ingest}/metrics"']


def test_hosts_on_different_root_domains_never_share_a_source() -> None:
    for a in INGEST_HOSTS:
        for b in INGEST_HOSTS:
            if _root_domain(a) != _root_domain(b):
                assert INGEST_HOSTS[a] != INGEST_HOSTS[b], f"{a} and {b} share a source"


@pytest.mark.parametrize("host", sorted(INGEST_HOSTS))
def test_a_hosts_caddyfile_differs_from_the_reference_only_by_its_site_name(host: str) -> None:
    reference = _read(REFERENCE, "Caddyfile")
    site = f"\n{REFERENCE} {{\n"
    assert reference.count(site) == 1, "the reference Caddyfile must carry exactly one site block"
    assert _read(host, "Caddyfile") == reference.replace(site, f"\n{host} {{\n")


def test_the_legacy_paths_still_carry_the_reference_hosts_bytes() -> None:
    # The NOC's fleet-deploy-runner on ai.basecradle.com reads deploy/vector.yaml and
    # deploy/caddy/Caddyfile from the deployed tree by fixed path. Until it reads
    # deploy/hosts/<fqdn>/ instead (basecradle-noc#892), those two paths must stay
    # byte-identical to ai's own copy, or the next deploy tick installs bytes nobody
    # reviewed for that box. Delete both paths, and this test, once nothing reads them.
    assert (DEPLOY / "vector.yaml").read_text() == _read(REFERENCE, "vector.yaml")
    assert (DEPLOY / "caddy" / "Caddyfile").read_text() == _read(REFERENCE, "Caddyfile")

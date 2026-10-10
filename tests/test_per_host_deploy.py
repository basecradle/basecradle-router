"""Per-host deploy config, pinned against the shipped bytes (#326).

The router runs on more than one box, and each box installs its own copy of three files
from the deployed tree: ``deploy/hosts/<fqdn>/Caddyfile``,
``deploy/hosts/<fqdn>/vector.yaml`` and ``deploy/hosts/<fqdn>/basecradle-router-reboot.timer``
(#334). The NOC installs those bytes as they are, with no rendering step, so the only
thing that can keep the copies honest is this file. It holds four things:

* every host carries exactly the three files, and no host is missing from the tables below;
* each host's files are the reference host's with only that host's own values swapped
  in, so a scrub rule, a proxy directive or a timer setting can never land on one box and
  not another;
* hosts on different root domains never share a Better Stack source (@origin,
  2026-09-21: a User on one source never writes to another);
* every host reboots in its own slot of the fleet's reboot stagger registry
  (basecradle/basecradle → provision-server §2.3), no two hosts share a window, and no
  reboot timer is shared — a host with no copy of its own has no window, never ai's.

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

#: Each host's reboot window: the one value its reboot timer differs by, and its slot in
#: the fleet's reboot stagger registry (basecradle/basecradle → provision-server §2.3). The
#: capital assigns the slot; this table only mirrors it, so a slot moves here when the
#: registry moves. ai's is 5 AM Central, DST-tracking; ubuntu-1's is the interim 12:00 UTC.
REBOOT_WINDOWS = {
    "ai.basecradle.com": "*-*-* 05:00:00 America/Chicago",
    "ubuntu-1.landfill.click": "*-*-* 12:00:00 UTC",
}

REBOOT_TIMER = "basecradle-router-reboot.timer"

#: The files every host directory carries, and nothing else.
HOST_FILES = {"Caddyfile", "vector.yaml", REBOOT_TIMER}


def _read(host: str, name: str) -> str:
    return (HOSTS_DIR / host / name).read_text()


def _root_domain(fqdn: str) -> str:
    # The last two labels. Exact for every domain the fleet uses (.com, .click); a
    # host under a multi-label public suffix such as .co.uk would need the real list.
    return ".".join(fqdn.split(".")[-2:])


def test_every_host_directory_is_declared_and_complete() -> None:
    hosts = {p.name for p in HOSTS_DIR.iterdir() if p.is_dir()}
    assert hosts == set(INGEST_HOSTS), "deploy/hosts/ and INGEST_HOSTS must name the same hosts"
    assert hosts == set(REBOOT_WINDOWS), "deploy/hosts/ and REBOOT_WINDOWS must name the same hosts"
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


def _on_calendar(host: str) -> list[str]:
    return [
        line.removeprefix("OnCalendar=")
        for line in _read(host, REBOOT_TIMER).splitlines()
        if line.startswith("OnCalendar=")
    ]


@pytest.mark.parametrize("host", sorted(REBOOT_WINDOWS))
def test_a_hosts_reboot_timer_differs_from_the_reference_only_by_its_window(host: str) -> None:
    reference = _read(REFERENCE, REBOOT_TIMER)
    line = f"\nOnCalendar={REBOOT_WINDOWS[REFERENCE]}\n"
    assert reference.count(line) == 1, "the reference timer must carry exactly one window"
    assert _read(host, REBOOT_TIMER) == reference.replace(
        line, f"\nOnCalendar={REBOOT_WINDOWS[host]}\n"
    )


@pytest.mark.parametrize("host", sorted(REBOOT_WINDOWS))
def test_a_host_reboots_only_in_its_own_registry_slot(host: str) -> None:
    assert _on_calendar(host) == [REBOOT_WINDOWS[host]]


def test_ai_keeps_its_window() -> None:
    # The registry's ai row is 5 AM Central plus jitter; moving it is the capital's call.
    assert _on_calendar("ai.basecradle.com") == ["*-*-* 05:00:00 America/Chicago"]


def test_no_two_hosts_share_a_reboot_window() -> None:
    windows = list(REBOOT_WINDOWS.values())
    assert len(set(windows)) == len(windows), "two router hosts would reboot in one window"


def test_the_reboot_timer_never_catches_up_off_window() -> None:
    timer = _read(REFERENCE, REBOOT_TIMER).splitlines()
    assert "Persistent=false" in timer
    assert "RandomizedDelaySec=15min" in timer


def test_no_reboot_window_is_shared_by_every_host() -> None:
    # The NOC installs every deploy/systemd/*.timer on every router box. A reboot timer
    # there would give a host with no window of its own another box's window.
    assert not (DEPLOY / "systemd" / REBOOT_TIMER).exists()

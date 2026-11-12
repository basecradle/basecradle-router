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

The vector.yaml rule has one declared, narrow exception (#442): an **owner destination**, a
second telemetry store a host's owner chose, carried beside Better Stack on that host alone.
The rule it exists to make unbreakable is that an owner's store receives the owner's box and
nothing else, so only a host outside ``basecradle.com`` may declare one, and a host with no
entry carries no extra sink of any kind.

No network, Caddy, or Vector binary is involved. This reads files off disk.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
HOSTS_DIR = DEPLOY / "hosts"

#: The host every other host's files are derived from. It is the production router box.
REFERENCE = "ai.basecradle.com"

#: Each host's Better Stack ingest host: the one value its vector.yaml differs by. A new
#: host directory with no entry here fails the suite, so a copy of ai's files can never
#: ship ai's sink to another box by omission. Hosts on one root domain share that domain's
#: source (@origin: a red box is just another host in its root domain's source), so red's
#: value is ai's on purpose and its vector.yaml is byte-identical to ai's.
INGEST_HOSTS = {
    "ai.basecradle.com": "s2531770.eu-fsn-3.betterstackdata.com",
    "red.basecradle.com": "s2531770.eu-fsn-3.betterstackdata.com",
    "ubuntu-1.landfill.click": "s2780873.eu-central-1a.betterstackdata.com",
}

#: Each host's reboot window: the one value its reboot timer differs by, and its slot in
#: the fleet's reboot stagger registry (basecradle/basecradle → provision-server §2.3). The
#: capital assigns the slot; this table only mirrors it, so a slot moves here when the
#: registry moves. ai's is 5 AM Central, DST-tracking; red's is 07:00 UTC; ubuntu-1's is the
#: interim 12:00 UTC.
REBOOT_WINDOWS = {
    "ai.basecradle.com": "*-*-* 05:00:00 America/Chicago",
    "red.basecradle.com": "*-*-* 07:00:00 UTC",
    "ubuntu-1.landfill.click": "*-*-* 12:00:00 UTC",
}

#: Our own root domain. No host on it may declare an owner destination: its telemetry is ours,
#: and an owner's store receives the owner's box and nothing else (#442).
OUR_ROOT_DOMAIN = "basecradle.com"

#: Each owner destination, keyed by host: the exact bytes its vector.yaml carries beyond the
#: reference's, set in just before the health exporter. ubuntu-1's owner pays for the box, and by
#: founder order (2026-11-11) its scrubbed logs and host metrics also ship to the owner's own Axiom
#: account (#442), every line measured on Vector 0.61.0 by the capital (or, for ``dt``, here):
#:
#: * ``when_full: "drop_newest"`` is the whole safety: with the default (``block``) a hanging Axiom
#:   froze the Better Stack sink, and with ``drop_newest`` Better Stack ran at full rate through a
#:   hang and a refused token. The disk buffer lets a short outage lose nothing.
#: * logs take ``ai_scrub``, never a raw source: the scrub is the boundary for anything leaving the
#:   box, and this is a second store we cannot purge.
#: * the ``url`` names the event's own time field, so Axiom dates an event by when it happened and a
#:   batch replayed after an outage is dated correctly; a url with a path is used as-is, query
#:   included. That field is ``dt`` for a log, because ``ai_scrub`` ends ``.dt = del(.timestamp)``
#:   (Better Stack's field), and ``timestamp`` for a metric: measured on 0.61.0, a scrubbed log
#:   reaches the sink with ``dt`` and no ``timestamp``, so naming ``timestamp`` dated every log line
#:   by its arrival.
#: * ``request.timeout_secs: 15`` keeps a hanging destination from holding Vector's graceful
#:   shutdown for its full 60 seconds on every restart.
#: * the token comes from the environment, as Better Stack's does, never from the file.
OWNER_DESTINATIONS = {
    "ubuntu-1.landfill.click": """\
  # THE OWNER'S OWN COPY (basecradle-router#442), ON THIS HOST ALONE. By founder order (2026-11-11)
  # the scrubbed logs and host metrics this host ships to Better Stack also go to its owner's own
  # Axiom account, which receives the owner's box and nothing else: tests/test_per_host_deploy.py
  # declares these two sinks for this host and refuses them on any other. Axiom is a best-effort
  # copy, and `when_full: "drop_newest"` is the whole safety of it: with the default (block) a
  # hanging Axiom froze the Better Stack sink. Logs take `ai_scrub`, never a raw source, and are
  # dated by `dt`, the field the scrub moves an event's time to; metrics keep `timestamp`. These two
  # token values and Better Stack's two are this file's only dollar signs.
  axiom_logs:
    type: "axiom"
    inputs: ["ai_scrub"]
    token: "${AXIOM_INGEST_TOKEN}"
    dataset: "fleet-servers-logs"
    url: "https://us-east-1.aws.edge.axiom.co/v1/ingest/fleet-servers-logs?timestamp-field=dt"
    request:
      timeout_secs: 15
    buffer:
      type: "disk"
      max_size: 268435488
      when_full: "drop_newest"

  axiom_metrics:
    type: "axiom"
    inputs: ["ai_host_metrics"]
    token: "${AXIOM_INGEST_TOKEN}"
    dataset: "fleet-servers-metrics"
    url: "https://us-east-1.aws.edge.axiom.co/v1/ingest/fleet-servers-metrics?timestamp-field=timestamp"
    request:
      timeout_secs: 15
    buffer:
      type: "disk"
      max_size: 268435488
      when_full: "drop_newest"

""",
}

#: Where an owner destination is set in: just before the health exporter's comment, so it sits
#: beside the Better Stack sinks.
OWNER_ANCHOR = "  # Vector's health counters, exposed"

#: The sinks the reference carries. A host with no owner destination carries exactly these.
REFERENCE_SINKS = ["better_stack_logs", "better_stack_metrics", "ai_vector_health_exporter"]

#: The inputs a sink leaving the box may take, each to the field that holds its event's own time:
#: the scrubbed logs, whose time the scrub moves to ``dt``, or the host's gauges, which carry no
#: message to scrub and keep ``timestamp``. Every other source is raw.
SHIPPABLE_INPUTS = {"ai_scrub": "dt", "ai_host_metrics": "timestamp"}

#: The scrub's last statement, which is why a scrubbed log's time is ``dt``.
SCRUB_TIME_MOVE = "      .dt = del(.timestamp)\n"

#: A sink's ``url``, as an owner destination declares it.
OWNER_URL = re.compile(
    r'^    url: "https://[a-z0-9.-]+/v1/ingest/(?P<dataset>[\w-]+)\?timestamp-field=(?P<field>\w+)"$',
    re.MULTILINE,
)

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


def _sinks(text: str) -> list[str]:
    return re.findall(r"^  (\w+):$", text[text.index("\nsinks:\n") :], re.MULTILINE)


def _components(block: str) -> dict[str, str]:
    """Each component of a block, by id, to the next component or the block's end, with every
    comment line taken out, so a comment can never stand in for, or hide, a setting."""
    settings = "".join(
        line for line in block.splitlines(keepends=True) if not line.lstrip().startswith("#")
    )
    parts = re.split(r"^  (\w+):\n", settings, flags=re.MULTILINE)
    assert not parts[0].strip(), f"a setting outside any component: {parts[0]!r}"
    return dict(zip(parts[1::2], parts[2::2], strict=True))


@pytest.mark.parametrize("host", sorted(INGEST_HOSTS))
def test_a_hosts_vector_config_differs_from_the_reference_only_by_its_sink(host: str) -> None:
    reference = _read(REFERENCE, "vector.yaml")
    assert reference.count(OWNER_ANCHOR) == 1, "the reference must carry the anchor exactly once"
    expected = reference.replace(INGEST_HOSTS[REFERENCE], INGEST_HOSTS[host]).replace(
        OWNER_ANCHOR, OWNER_DESTINATIONS.get(host, "") + OWNER_ANCHOR
    )
    assert _read(host, "vector.yaml") == expected


def test_every_owner_destination_is_a_declared_host() -> None:
    assert set(OWNER_DESTINATIONS) <= set(INGEST_HOSTS)


@pytest.mark.parametrize("host", sorted(OWNER_DESTINATIONS))
def test_only_a_host_outside_our_domain_may_declare_an_owner_destination(host: str) -> None:
    assert _root_domain(host) != OUR_ROOT_DOMAIN, (
        f"{host} is one of our own hosts. An owner destination ships a box's telemetry to the "
        "store of the User who owns that box, and that store must receive the owner's box and "
        f"nothing else, so no {OUR_ROOT_DOMAIN} host may carry one."
    )


@pytest.mark.parametrize("host", sorted(set(INGEST_HOSTS) - set(OWNER_DESTINATIONS)))
def test_a_host_with_no_owner_destination_carries_no_extra_sink(host: str) -> None:
    # The byte check above already holds this while the reference is clean; this holds the
    # reference itself, so an Axiom sink added to it (and so to every host) still fails.
    text = _read(host, "vector.yaml")
    assert _sinks(text) == REFERENCE_SINKS
    assert "AXIOM" not in text.upper()


@pytest.mark.parametrize("host", sorted(OWNER_DESTINATIONS))
def test_an_owner_destination_never_holds_better_stack_back_nor_ships_raw(host: str) -> None:
    components = _components(OWNER_DESTINATIONS[host])
    assert components, f"{host}'s owner destination declares no sink"
    for component_id, body in components.items():
        # Every sink in it: a hanging or refusing destination drops its own newest events and
        # never blocks the shared pipeline Better Stack is fed from.
        assert '      when_full: "drop_newest"\n' in body, f"{component_id} may block"
        assert "when_full" not in body.replace('      when_full: "drop_newest"\n', "", 1)
        # Logs only after the scrub: never a raw source.
        inputs = re.findall(r'^    inputs: \["(\w+)"\]$', body, re.MULTILINE)
        assert body.count("inputs:") == 1 and len(inputs) == 1, f"{component_id}: {body!r}"
        assert inputs[0] in SHIPPABLE_INPUTS, f"{component_id} ships the raw {inputs[0]!r}"
        # One token, from the environment, never from the file, and no other key that could
        # carry a secret.
        assert re.findall(r'^    token: "\$\{[A-Z_]+\}"$', body, re.MULTILINE), component_id
        assert body.count("token:") == 1 and "$" not in body.replace("${", "", 1), component_id
        assert not re.search(r"(?i)key|secret|password|auth", body), component_id
        # Its dataset is the one the url ingests into (a url with a path is used as-is, so the
        # `dataset:` line alone would be silently ignored), and it dates each event by the field
        # its input actually carries the event's time in.
        url = OWNER_URL.search(body)
        assert url and body.count("url:") == 1, f"{component_id}: {body!r}"
        assert f'    dataset: "{url["dataset"]}"\n' in body, component_id
        assert url["field"] == SHIPPABLE_INPUTS[inputs[0]], (
            f"{component_id} takes {inputs[0]}, whose event time is in "
            f"{SHIPPABLE_INPUTS[inputs[0]]!r}, not {url['field']!r}: Axiom would date it by arrival"
        )


@pytest.mark.parametrize("host", sorted(INGEST_HOSTS))
def test_a_scrubbed_logs_time_is_dt(host: str) -> None:
    # SHIPPABLE_INPUTS dates a scrubbed log by `dt` because the scrub moves the time there.
    assert _read(host, "vector.yaml").count(SCRUB_TIME_MOVE) == 1


@pytest.mark.parametrize("host", sorted(INGEST_HOSTS))
def test_the_only_dollar_signs_in_a_hosts_vector_config_are_its_sink_tokens(host: str) -> None:
    # With interpolation on, as every box's unit runs it, Vector interpolates environment
    # variables across the WHOLE file before it parses the YAML — comments and the VRL source
    # included — so a dollar sign anywhere else is never literal: a `$1` back-reference in a
    # replacement, or a regex end anchor, becomes an environment lookup and can fail the NOC's
    # pre-install validation, refusing the apply. The two Better Stack tokens, plus each token an
    # owner destination declares, are the only ones.
    extra = [line.strip() for line in OWNER_DESTINATIONS.get(host, "").splitlines() if "$" in line]
    carriers = [line.strip() for line in _read(host, "vector.yaml").splitlines() if "$" in line]
    assert sorted(carriers) == sorted(['token: "${BETTERSTACK_AI_SOURCE_TOKEN}"'] * 2 + extra)
    assert all(line.startswith("token: ") for line in extra), extra


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

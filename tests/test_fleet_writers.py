"""``deploy/bin/fleet-writers`` — the writer map, built offline (#484).

The script is loaded as a module and driven over a fabricated registry and account database: no
real account is read and nothing is written outside ``tmp_path``. What `ai_scrub` does with the map
is proven by ``tests/vector/ai_writers.yaml`` (run by ``tests/test_vector_unit.py`` on a map this
same script builds); this suite pins what the map holds, how it is written, and that the script,
its unit, the daemon's unit and every host's ``vector.yaml`` agree with each other.

Cast: Nova Digital (``nova``, uid 1001) is a harness agent, ``basecradle-nova-ai`` (uid 1002) a
builder; the daemon runs as ``router`` (uid 999); John Doe (``john``, uid 1000) is a human's login,
in no registry.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from fleet_writers_box import ACCOUNTS, REGISTRY, SCRIPT, fw

REPO = Path(__file__).resolve().parents[1]
SYSTEMD = REPO / "deploy" / "systemd"
WRITER_UNIT = SYSTEMD / "basecradle-router-writers.service"
DAEMON_UNIT = SYSTEMD / "basecradle-router.service"
RECOVERY_UNIT = SYSTEMD / "basecradle-router-recovery.service"
HOSTS_DIR = REPO / "deploy" / "hosts"


def _rows(registry=REGISTRY, accounts=ACCOUNTS):
    return fw.rows(registry, accounts.get)


# --- what the map holds -------------------------------------------------------------------------


def test_every_legitimate_writer_is_listed_and_nothing_else() -> None:
    found, left_out = _rows()
    assert left_out == []
    assert found == [
        ("basecradle-log-grammar", 1001),
        ("basecradle-log-grammar", 1002),
        ("basecradle-router", 999),
        ("basecradle-router-stop-wake", 0),
        ("basecradle-wake-basecradle-nova-ai", 0),
        ("basecradle-wake-basecradle-nova-ai", 1002),
        ("basecradle-wake-nova", 0),
        ("basecradle-wake-nova", 1001),
        ("fleet-deploy-runner", 0),
        ("verify-recovery.sh", 999),
    ]


def test_a_login_account_is_never_a_writer_of_anything() -> None:
    found, _ = _rows()
    assert all(uid != ACCOUNTS["john"] for _, uid in found)
    assert all(identifier != "basecradle-wake-john" for identifier, _ in found)


def test_root_writes_only_on_a_wake_stream_and_under_its_own_two() -> None:
    found, _ = _rows()
    assert {identifier for identifier, uid in found if uid == 0} == {
        "basecradle-wake-nova",
        "basecradle-wake-basecradle-nova-ai",
        "basecradle-router-stop-wake",
        "fleet-deploy-runner",
    }


def test_root_is_the_only_writer_of_its_own_two_whatever_the_registry_holds() -> None:
    # The operator's stop and the NOC's deploy runner write only as root (basecradle-noc#1518's
    # look-back), so no agent, the router or a login is ever listed under either, and an empty
    # registry lists root under both all the same.
    for registry in (REGISTRY, {}):
        found, _ = _rows(registry=registry)
        for identifier in ("basecradle-router-stop-wake", "fleet-deploy-runner"):
            assert fw.FIXED_WRITERS[identifier] == fw.ROOT_ACCOUNT
            assert [uid for name, uid in found if name == identifier] == [0]


def test_one_agent_is_never_listed_under_another_agents_wake() -> None:
    found, _ = _rows()
    assert ("basecradle-wake-nova", 1002) not in found
    assert ("basecradle-wake-basecradle-nova-ai", 1001) not in found


def test_the_map_follows_a_moved_uid() -> None:
    # A re-provision moves a UID (the router's own already has, 999 -> 995): the map is read off
    # the account database each run, never carried over.
    found, _ = _rows(accounts={**ACCOUNTS, "router": 995, "nova": 1011})
    assert ("basecradle-router", 995) in found and ("basecradle-router", 999) not in found
    assert ("basecradle-wake-nova", 1011) in found and ("basecradle-wake-nova", 1001) not in found


def test_two_entries_sharing_an_account_give_one_row_each() -> None:
    # `get`-style lookups refuse a duplicate match; the map never holds one.
    found, _ = _rows(registry={**REGISTRY, "nova-again": {"os_user": "nova"}})
    assert len(found) == len(set(found)) == 10


@pytest.mark.parametrize(
    ("entry", "why"),
    [
        ({"os_user": "ghost"}, "no account named 'ghost'"),
        ({"os_user": "nova,1000"}, "is not an account name"),
        ({"os_user": "Nova"}, "is not an account name"),
        ({"os_user": "nova\n"}, "is not an account name"),
        ({"os_user": ""}, "is not an account name"),
        ({"os_user": 1001}, "is not an account name"),
        ({}, "is not an account name"),
        ("nova", "is not an account name"),
    ],
)
def test_an_entry_that_names_no_account_is_left_out_and_named(entry, why) -> None:
    found, left_out = _rows(registry={**REGISTRY, "broken": entry})
    assert found == _rows()[0], "the rest of the map is still written"
    assert len(left_out) == 1 and left_out[0].startswith("broken: ") and why in left_out[0]


def test_a_missing_router_account_writes_nothing() -> None:
    accounts = {name: uid for name, uid in ACCOUNTS.items() if name != "router"}
    with pytest.raises(fw.MapError, match="'router' does not exist"):
        _rows(accounts=accounts)


def test_the_rendered_map_is_the_tables_csv() -> None:
    found, _ = _rows()
    text = fw.render(found)
    assert text.startswith("identifier,uid\n") and text.endswith("\n")
    assert text.splitlines()[1:] == [f"{identifier},{uid}" for identifier, uid in found]


# --- reading the registry -----------------------------------------------------------------------


def test_the_registry_is_read_as_a_json_object(tmp_path: Path) -> None:
    registry = tmp_path / "agents.json"
    registry.write_text('{"nova": {"os_user": "nova"}}')
    assert fw.read_registry(registry) == {"nova": {"os_user": "nova"}}


@pytest.mark.parametrize("body", ["[]", "{", ""])
def test_a_malformed_registry_writes_nothing(tmp_path: Path, body: str) -> None:
    registry = tmp_path / "agents.json"
    registry.write_text(body)
    with pytest.raises(fw.MapError):
        fw.read_registry(registry)


def test_a_missing_registry_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(fw.MapError):
        fw.read_registry(tmp_path / "agents.json")


# --- writing the file ---------------------------------------------------------------------------


def test_the_map_is_written_whole_with_its_mode_and_no_temp_left(tmp_path: Path) -> None:
    dest, temp = tmp_path / "fleet-writers.csv", tmp_path / ".fleet-writers.csv.tmp"
    text = "identifier,uid\nbasecradle-router,999\n"
    assert fw.install(text, dest, temp, os.getuid(), os.getgid()) is True
    assert dest.read_text() == "identifier,uid\nbasecradle-router,999\n"
    assert dest.stat().st_mode & 0o777 == 0o640
    assert not temp.exists()


def test_an_unchanged_map_is_not_rewritten(tmp_path: Path) -> None:
    dest, temp = tmp_path / "fleet-writers.csv", tmp_path / ".fleet-writers.csv.tmp"
    fw.install("identifier,uid\n", dest, temp, os.getuid(), os.getgid())
    before = dest.stat().st_ino
    assert fw.install("identifier,uid\n", dest, temp, os.getuid(), os.getgid()) is False
    assert dest.stat().st_ino == before


def test_a_temp_left_by_a_killed_run_is_replaced_and_removed(tmp_path: Path) -> None:
    dest, temp = tmp_path / "fleet-writers.csv", tmp_path / ".fleet-writers.csv.tmp"
    temp.write_text("half a map")
    assert fw.install("identifier,uid\n", dest, temp, os.getuid(), os.getgid()) is True
    assert dest.read_text() == "identifier,uid\n" and not temp.exists()


def test_a_symlink_in_the_temp_slot_is_never_written_through(tmp_path: Path) -> None:
    dest, temp = tmp_path / "fleet-writers.csv", tmp_path / ".fleet-writers.csv.tmp"
    target = tmp_path / "elsewhere"
    target.write_text("untouched")
    temp.symlink_to(target)
    fw.install("identifier,uid\n", dest, temp, os.getuid(), os.getgid())
    assert target.read_text() == "untouched"
    assert dest.read_text() == "identifier,uid\n" and not temp.is_symlink()


def test_a_failed_write_removes_its_temp_and_leaves_the_map(tmp_path: Path, monkeypatch) -> None:
    dest, temp = tmp_path / "fleet-writers.csv", tmp_path / ".fleet-writers.csv.tmp"
    dest.write_text("identifier,uid\nbasecradle-router,999\n")

    def refuse(src, dst):
        raise OSError("refused")

    monkeypatch.setattr(fw.os, "rename", refuse)
    with pytest.raises(OSError):
        fw.install("identifier,uid\n", dest, temp, os.getuid(), os.getgid())
    assert dest.read_text() == "identifier,uid\nbasecradle-router,999\n"
    assert not temp.exists()


# --- the run --------------------------------------------------------------------------------------


@pytest.fixture
def box(tmp_path: Path, monkeypatch):
    """The script's paths, the account database and systemctl, all fabricated."""
    registry = tmp_path / "agents.json"
    registry.write_text('{"nova": {"os_user": "nova"}}')
    dest = tmp_path / "fleet-writers.csv"
    monkeypatch.setattr(fw, "REGISTRY", registry)
    monkeypatch.setattr(fw, "MAP", dest)
    monkeypatch.setattr(fw, "TEMP", tmp_path / ".fleet-writers.csv.tmp")
    monkeypatch.setattr(fw, "MAP_OWNER", os.getuid())
    monkeypatch.setattr(fw, "account_uid", ACCOUNTS.get)
    monkeypatch.setattr(fw.grp, "getgrnam", lambda name: type("G", (), {"gr_gid": os.getgid()}))
    reloads: list[str] = []
    monkeypatch.setattr(fw, "reload_vector", lambda: reloads.append("reload") or True)
    return registry, dest, reloads


def test_a_run_that_changed_the_map_queues_one_reload(box, capsys) -> None:
    _, dest, reloads = box
    assert fw.main() == 0
    assert reloads == ["reload"] and dest.read_text().count("\n") == 8
    assert "Vector reload queued" in capsys.readouterr().out


def test_a_run_that_changed_nothing_reloads_nothing(box) -> None:
    _, _, reloads = box
    fw.main()
    assert fw.main() == 0
    assert reloads == ["reload"]


def test_a_run_with_an_entry_left_out_writes_the_rest_and_fails(box, capsys) -> None:
    registry, dest, _ = box
    registry.write_text('{"nova": {"os_user": "nova"}, "ghost": {"os_user": "ghost"}}')
    assert fw.main() == 1
    assert "basecradle-wake-nova,1001" in dest.read_text()
    assert "left out ghost: no account named 'ghost'" in capsys.readouterr().out


def test_a_run_on_a_broken_registry_leaves_the_map_in_place(box, capsys) -> None:
    registry, dest, reloads = box
    dest.write_text("identifier,uid\nbasecradle-router,999\n")
    registry.write_text("{")
    assert fw.main() == 1
    assert dest.read_text() == "identifier,uid\nbasecradle-router,999\n" and reloads == []
    assert "the map in place is unchanged" in capsys.readouterr().out


def test_a_failed_reload_fails_the_run(box, monkeypatch) -> None:
    monkeypatch.setattr(fw, "reload_vector", lambda: False)
    assert fw.main() == 1


def test_the_reload_never_waits_on_the_unit_it_runs_in(monkeypatch) -> None:
    # This unit is ordered before vector.service, so a reload job it waited for would wait on it.
    seen: list[list[str]] = []

    def run(argv, **_):
        seen.append(argv)
        return type("R", (), {"returncode": 0})

    monkeypatch.setattr(fw.subprocess, "run", run)
    assert fw.reload_vector() is True
    assert seen == [["systemctl", "--no-block", "try-reload-or-restart", "vector.service"]]


# --- the script, its units and the configs agree --------------------------------------------------


def _directive(unit: Path, key: str) -> list[str]:
    return re.findall(rf"^{key}=(.*)$", unit.read_text(), re.MULTILINE)


def test_the_router_account_is_the_one_the_units_run_as() -> None:
    assert _directive(DAEMON_UNIT, "User") == [fw.ROUTER_ACCOUNT]
    assert _directive(RECOVERY_UNIT, "User") == [fw.ROUTER_ACCOUNT]


def test_the_daemons_identifier_is_one_the_map_vouches_for() -> None:
    assert _directive(DAEMON_UNIT, "SyslogIdentifier") == ["basecradle-router"]
    assert fw.FIXED_WRITERS["basecradle-router"] == fw.ROUTER_ACCOUNT
    assert fw.FIXED_WRITERS["verify-recovery.sh"] == fw.ROUTER_ACCOUNT
    # verify-recovery.sh logs under its script's name, the unit's ExecStart.
    assert _directive(RECOVERY_UNIT, "ExecStart")[0].endswith("/verify-recovery.sh")


def test_the_wake_identifier_is_the_one_wake_runner_writes() -> None:
    wake_runner = (REPO / "deploy" / "bin" / "wake-runner").read_text()
    assert f'journal_tag="{fw.WAKE_PREFIX}$user"' in wake_runner


def test_the_stop_identifier_is_the_one_stop_wake_writes_and_only_root_reaches_it() -> None:
    stop_wake = (REPO / "deploy" / "bin" / "stop-wake").read_text()
    assert '\nIDENT = "basecradle-router-stop-wake"\n' in stop_wake
    assert fw.FIXED_WRITERS["basecradle-router-stop-wake"] == fw.ROOT_ACCOUNT
    # It refuses any account but root before it opens the journal, so root is its only writer.
    refusal = stop_wake.index("if os.geteuid() != 0:")
    assert refusal < stop_wake.index("syslog.openlog(IDENT")
    assert stop_wake.count("syslog.openlog(") == 1


def test_the_grammar_probe_identifier_is_one_the_map_vouches_for() -> None:
    # The NOC's probe writes it as each agent (basecradle-noc#1518); this pins the spelling.
    assert fw.LOG_GRAMMAR == "basecradle-log-grammar"


def test_the_daemon_runs_the_writer_first_and_never_waits_on_its_success() -> None:
    assert "basecradle-router-writers.service" in _directive(DAEMON_UNIT, "Wants")
    assert "basecradle-router-writers.service" in _directive(DAEMON_UNIT, "After")
    for strong in ("Requires", "BindsTo", "Requisite", "PartOf"):
        assert not any("writers" in v for v in _directive(DAEMON_UNIT, strong)), strong


def test_the_writer_runs_as_root_from_the_root_owned_helpers_and_before_vector() -> None:
    assert _directive(WRITER_UNIT, "ExecStart") == ["/opt/basecradle-router/bin/fleet-writers"]
    assert _directive(WRITER_UNIT, "User") == []
    assert _directive(WRITER_UNIT, "Type") == ["oneshot"]
    assert _directive(WRITER_UNIT, "Before") == ["basecradle-router.service vector.service"]
    assert _directive(WRITER_UNIT, "ReadWritePaths") == [str(fw.MAP.parent)]
    # Nothing enables it: the daemon's unit pulls it in, so it carries no [Install].
    assert "[Install]" not in WRITER_UNIT.read_text()


def test_the_writer_logs_under_a_name_no_column_trusts() -> None:
    (identifier,) = _directive(WRITER_UNIT, "SyslogIdentifier")
    assert not identifier.startswith(fw.WAKE_PREFIX)
    assert identifier not in (fw.LOG_GRAMMAR, *fw.FIXED_WRITERS)


def test_the_script_is_executable_and_runs_isolated() -> None:
    assert SCRIPT.stat().st_mode & 0o111
    assert SCRIPT.read_text().splitlines()[0] == "#!/usr/bin/python3 -I"


@pytest.mark.parametrize("host", sorted(p.name for p in HOSTS_DIR.iterdir() if p.is_dir()))
def test_every_host_vets_exactly_the_identifiers_the_map_vouches_for(host: str) -> None:
    text = (HOSTS_DIR / host / "vector.yaml").read_text()
    assert text.count(f'      path: "{fw.MAP}"\n') == 1
    (check,) = re.findall(
        r"^      if starts_with\(ident, \"(.+?)\"\) \|\| includes\((\[.*?\]), ident\) \{$",
        text,
        re.MULTILINE,
    )
    prefix, exact = check
    assert prefix == fw.WAKE_PREFIX
    assert sorted(re.findall(r'"([^"]+)"', exact)) == sorted((fw.LOG_GRAMMAR, *fw.FIXED_WRITERS))


def _bullet(path: Path, lead: str) -> str:
    """The one markdown bullet in `path` that starts with `lead`, joined onto one line."""
    text = path.read_text()
    start = text.index(lead)
    end = text.find("\n- ", start)
    return " ".join(text[start : end if end != -1 else None].split())


@pytest.mark.parametrize(
    ("doc", "lead"),
    [
        ("CLAUDE.md", "- **Every line under a fleet identifier is bound to its writer**"),
        ("deploy/README.md", "- **The fleet identifiers**"),
    ],
)
def test_the_prose_names_exactly_the_identifiers_the_map_vouches_for(doc: str, lead: str) -> None:
    # The prose is where a session learns which names are bound; a name added in code and not
    # here is a writer nobody knows to list.
    bullet = _bullet(REPO / doc, lead)
    named = set(re.findall(r"`([a-z][a-z0-9.-]*)`", bullet)) - {"fleet-writers"}
    named = {name for name in named if not name.startswith("basecradle-wake")}
    assert named == {fw.LOG_GRAMMAR, *fw.FIXED_WRITERS}, bullet
    assert f"`{fw.WAKE_PREFIX}" in bullet


# --- the review's fail directions (#484) ----------------------------------------------------------


@pytest.mark.parametrize("os_user", ["root", "router", "systemd-network"])
def test_an_account_below_wake_runners_floor_is_never_a_writer(os_user: str) -> None:
    # wake-runner refuses to wake any account under MIN_UID, so none is a real agent writer, and
    # vouching for one would let root or the daemon write the grammar probe unmarked.
    accounts = {**ACCOUNTS, "root": 0, "systemd-network": 998}
    found, left_out = fw.rows({**REGISTRY, "system": {"os_user": os_user}}, accounts.get)
    assert found == _rows()[0]
    assert len(left_out) == 1 and "below the agent floor 1000" in left_out[0]


def test_the_floor_and_the_registry_are_wake_runners_own() -> None:
    wake_runner = (REPO / "deploy" / "bin" / "wake-runner").read_text()
    assert f"\nMIN_UID={fw.MIN_UID}\n" in wake_runner
    assert f"\nREGISTRY={fw.REGISTRY}\n" in wake_runner


def test_a_current_map_with_a_widened_mode_is_rewritten(tmp_path: Path) -> None:
    dest, temp = tmp_path / "fleet-writers.csv", tmp_path / ".fleet-writers.csv.tmp"
    dest.write_text("identifier,uid\n")
    dest.chmod(0o666)
    assert fw.install("identifier,uid\n", dest, temp, os.getuid(), os.getgid()) is True
    assert stat.S_IMODE(dest.stat().st_mode) == 0o640


def test_a_current_map_under_another_group_is_rewritten(tmp_path: Path) -> None:
    dest, temp = tmp_path / "fleet-writers.csv", tmp_path / ".fleet-writers.csv.tmp"
    fw.install("identifier,uid\n", dest, temp, os.getuid(), os.getgid())
    assert fw._current("identifier,uid\n", dest, os.getuid(), os.getgid())
    assert not fw._current("identifier,uid\n", dest, os.getuid(), os.getgid() + 1)
    assert not fw._current("identifier,uid\n", dest, os.getuid() + 1, os.getgid())


def test_a_symlink_at_the_map_is_never_current(tmp_path: Path) -> None:
    dest, temp = tmp_path / "fleet-writers.csv", tmp_path / ".fleet-writers.csv.tmp"
    real = tmp_path / "real.csv"
    fw.install("identifier,uid\n", real, temp, os.getuid(), os.getgid())
    dest.symlink_to(real)
    assert fw.install("identifier,uid\n", dest, temp, os.getuid(), os.getgid()) is True
    assert not dest.is_symlink()


def test_a_broken_registry_on_a_box_with_no_map_writes_one_that_loads(box, capsys) -> None:
    # No map would keep Vector from starting at all; a header-only one loads and vouches for no
    # one, so every fleet line is marked and the box keeps shipping.
    registry, dest, reloads = box
    registry.write_text("{")
    assert fw.main() == 1
    assert dest.read_text() == fw.HEADER and reloads == []
    assert "wrote an empty map" in capsys.readouterr().out


def test_vector_pulls_the_writer_in_before_it_starts_and_never_waits_on_its_success() -> None:
    dropin = SYSTEMD / "vector.service.d" / "30-fleet-writers.conf"
    assert _directive(dropin, "Wants") == ["basecradle-router-writers.service"]
    assert _directive(dropin, "After") == ["basecradle-router-writers.service"]
    for strong in ("Requires", "BindsTo", "Requisite", "PartOf"):
        assert _directive(dropin, strong) == [], strong


def test_the_writer_keeps_one_capability() -> None:
    assert _directive(WRITER_UNIT, "CapabilityBoundingSet") == ["CAP_CHOWN"]
    assert _directive(WRITER_UNIT, "AmbientCapabilities") == [""]

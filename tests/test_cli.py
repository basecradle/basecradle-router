"""The admin CLI the NOC actually invokes, driven offline end to end.

``python -m basecradle_router`` is the whole interface between this component and
the claims-vs-evidence ledger (basecradle/basecradle#460): the emitter the converge
runs, the probe the Layer-3 scheduler exercises, and the evidence dump an operator
reads mid-incident. What is pinned here is the contract the NOC schedules against —
the exit codes, the JSON shapes, and the promise that the CLI reads and never
writes the daemon's state. No network, model, or live agent.
Test cast: Nova Digital (``nova``, AI).
"""

import json

import pytest

from basecradle_router.__main__ import EXIT_CONFIG_ERROR, main

NOVA_ENTRY = {
    "os_user": "nova",
    "clone_path": "/home/nova/basecradle-python",
    "bot_slug": "basecradle-python-ai",
}
SECRET = "whsec_" + "0" * 32  # correctly-shaped fake


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A fabricated box: an agent registry, an empty lock dir, an evidence path."""
    registry = tmp_path / "agents.json"
    registry.write_text(json.dumps({"basecradle/basecradle-python": NOVA_ENTRY}), encoding="utf-8")
    locks = tmp_path / "wake-locks"
    locks.mkdir()
    evidence = tmp_path / "evidence.json"

    for name, value in {
        "BASECRADLE_ROUTER_AGENTS": str(registry),
        "BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET": SECRET,
        "BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS": "drawkkwast",
        "BASECRADLE_ROUTER_WAKE_LOCK_DIR": str(locks),
        "BASECRADLE_ROUTER_EVIDENCE_FILE": str(evidence),
    }.items():
        monkeypatch.setenv(name, value)
    return type("Box", (), {"locks": locks, "evidence": evidence, "registry": registry})


def _json_out(capsys) -> object:
    return json.loads(capsys.readouterr().out)


# --- claims -----------------------------------------------------------------


def test_claims_emits_contract_v1_manifests(box, capsys) -> None:
    assert main(["claims", "--host", "ai.basecradle.com"]) == 0

    manifests = _json_out(capsys)
    assert [m["subject"] for m in manifests] == ["box:ai.basecradle.com", "agent:nova"]
    assert all(m["contract"] == 1 for m in manifests)


def test_claims_out_dir_writes_one_strict_manifest_per_subject(box, tmp_path, capsys) -> None:
    out = tmp_path / "claims.d"

    assert main(["claims", "--host", "ai.basecradle.com", "--out-dir", str(out)]) == 0

    written = sorted(p.name for p in out.iterdir())
    assert written == [
        "basecradle-router.agent-nova.json",
        "basecradle-router.box-ai.basecradle.com.json",
    ]
    # Each file is a single-subject manifest — the strict per-file contract shape.
    one = json.loads((out / "basecradle-router.agent-nova.json").read_text(encoding="utf-8"))
    assert one["subject"] == "agent:nova"
    assert one["contract"] == 1


def test_claims_reads_the_evidence_the_daemon_wrote(box, capsys) -> None:
    # The emitter runs in a different process from the daemon — the file is the only
    # channel between them, and this is the property that makes it work.
    from basecradle_router.evidence import EvidenceStore

    EvidenceStore(str(box.evidence)).record_wake_ok("nova", "delivery-1")

    main(["claims", "--host", "ai.basecradle.com"])

    agent = _json_out(capsys)[1]
    assert "delivery=delivery-1" in agent["claims"][0]["evidence"]


# --- selftest freeze --------------------------------------------------------


def test_selftest_freeze_exits_zero_on_a_readable_surface(box, capsys) -> None:
    assert main(["selftest", "freeze"]) == 0
    assert "freeze-readability: ok" in capsys.readouterr().out


def test_selftest_freeze_exits_one_on_a_malformed_lock(box, capsys) -> None:
    (box.locks / "nova.lock").write_text("{ not json", encoding="utf-8")

    assert main(["selftest", "freeze"]) == 1


def test_selftest_freeze_exits_two_when_it_cannot_prove(box, monkeypatch) -> None:
    # Exit 2 is deliberately distinct from 1: a fresh box whose wake-lock directory
    # the NOC has not created yet must not look like a broken freeze surface.
    monkeypatch.setenv("BASECRADLE_ROUTER_WAKE_LOCK_DIR", str(box.locks / "not-created-yet"))

    assert main(["selftest", "freeze"]) == 2


def test_selftest_freeze_json_is_the_shape_the_noc_parses(box, capsys) -> None:
    assert main(["selftest", "freeze", "--json"]) == 0

    payload = _json_out(capsys)
    assert payload["probe"] == "freeze-readability"
    assert payload["status"] == "ok"
    assert payload["lock_dir"] == str(box.locks)
    assert payload["ran_as"]


def test_the_cli_never_writes_the_daemons_evidence_file(box) -> None:
    # The trap this forecloses: a probe run as root would replace the state file and
    # leave it root-owned, locking the *daemon* out of writing its own evidence — an
    # instrument that breaks the thing it instruments. The daemon is the sole writer.
    assert main(["selftest", "freeze"]) == 0
    assert main(["claims"]) == 0

    assert not box.evidence.exists()


# --- evidence ---------------------------------------------------------------


def test_evidence_dumps_the_document(box, capsys) -> None:
    from basecradle_router.evidence import EvidenceStore

    EvidenceStore(str(box.evidence)).record_delivery_accepted("github")

    assert main(["evidence"]) == 0
    assert _json_out(capsys)["delivery_sinks"]["github"]["accepted"] == 1


def test_evidence_on_a_box_that_has_produced_none_is_empty_not_an_error(box, capsys) -> None:
    assert main(["evidence"]) == 0
    assert _json_out(capsys)["agent_wakes"] == {}


# --- the config-error exit code --------------------------------------------


def test_a_config_the_daemon_could_not_boot_on_exits_three(box, monkeypatch, capsys) -> None:
    # A distinct code, because "the router's own config is broken" sends the NOC
    # somewhere different from "the surface this probe examines is broken".
    monkeypatch.delenv("BASECRADLE_ROUTER_AGENTS")

    assert main(["claims"]) == EXIT_CONFIG_ERROR
    assert "configuration error" in capsys.readouterr().err


def test_a_missing_trusted_actor_list_also_exits_three(box, monkeypatch) -> None:
    # The emitter builds the same registry the daemon runs, so a config the daemon
    # would refuse to start on can never yield a manifest describing a live router.
    monkeypatch.delenv("BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS")

    assert main(["claims"]) == EXIT_CONFIG_ERROR

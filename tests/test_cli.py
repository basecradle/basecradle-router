"""The admin CLI the NOC actually invokes, driven offline end to end.

``python -m basecradle_router`` is the whole interface between this component and
the claims-vs-evidence ledger (basecradle/basecradle#460): the emitter the converge
runs, the probe the Layer-3 scheduler exercises, and the evidence dump an operator
reads mid-incident. What is pinned here is the contract the NOC schedules against —
the exit codes, the JSON shapes, and the promise that the CLI reads and never
writes the daemon's state. No network, model, or live agent.
Test cast: Nova Digital (``nova``, AI).
"""

import io
import json

import pytest

from basecradle_router.__main__ import EXIT_CONFIG_ERROR, main
from basecradle_router.probe import Injection

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
    # The filenames are the contract's, and they are load-bearing: run-claim-probe
    # resolves $CLAIMS_DIR/<component>@<os_user>.json before it will run anything, so a
    # file spelled any other way is a claim that can never be proven.
    out = tmp_path / "claims.d"

    assert main(["claims", "--host", "ai.basecradle.com", "--out-dir", str(out)]) == 0

    written = sorted(p.name for p in out.iterdir())
    assert written == ["basecradle-router.json", "basecradle-router@nova.json"]
    # Each file is a single-subject manifest — the strict per-file contract shape.
    one = json.loads((out / "basecradle-router@nova.json").read_text(encoding="utf-8"))
    assert one["subject"] == "agent:nova"
    assert one["contract"] == 1
    # The box manifest carries the host in its body, never in its name.
    box_manifest = json.loads((out / "basecradle-router.json").read_text(encoding="utf-8"))
    assert box_manifest["subject"] == "box:ai.basecradle.com"


def test_the_array_on_stdout_survives_alongside_the_per_subject_files(box, capsys) -> None:
    # Two surfaces, not two spellings: stdout is provision-claims's per-subject
    # surface, the directory is the census's. Ratifying the files did not retire the
    # array, so dropping it would break the other consumer.
    assert main(["claims", "--host", "ai.basecradle.com"]) == 0

    manifests = _json_out(capsys)
    assert isinstance(manifests, list)
    assert [m["subject"] for m in manifests] == ["box:ai.basecradle.com", "agent:nova"]


def test_claims_reads_the_evidence_the_daemon_wrote(box, capsys) -> None:
    # The emitter runs in a different process from the daemon — the file is the only
    # channel between them, and this is the property that makes it work.
    from basecradle_router.evidence import EvidenceStore

    EvidenceStore(str(box.evidence)).record_wake_ok(
        "nova", "delivery-1", route="github", synthetic=False
    )

    main(["claims", "--host", "ai.basecradle.com"])

    agent = _json_out(capsys)[1]
    claims = {c["claim"]: c for c in agent["claims"]}
    assert "delivery=delivery-1" in claims["wake-edge:webhook-route"]["evidence"]
    assert "route=github" in claims["wake-edge:webhook-route"]["evidence"]
    # The per-recipient row reads the same wake through its own (agent, route) record.
    assert "delivery=delivery-1" in claims["wake-edge:webhook-route:github"]["evidence"]


# --- selftest freeze --------------------------------------------------------


def test_selftest_freeze_exits_zero_on_a_readable_surface(box, capsys) -> None:
    assert main(["selftest", "freeze"]) == 0
    assert "freeze-readability: ok" in capsys.readouterr().out


def test_selftest_freeze_exits_one_on_a_malformed_lock(box, capsys) -> None:
    # 1 is FAIL in the ledger's vocabulary: *we asked; the answer is no.*
    (box.locks / "nova.lock").write_text("{ not json", encoding="utf-8")

    assert main(["selftest", "freeze"]) == 1


def test_selftest_freeze_exits_seventy_five_when_it_cannot_prove(box, monkeypatch) -> None:
    # 75 (EX_TEMPFAIL) is the contract's one inconclusive sentinel — *we never got an
    # answer* — and it is still red, still immediate. It stays distinct from 1 because a
    # fresh box whose wake-lock directory the NOC has not created yet must not look
    # identical to a box whose freeze surface is genuinely unreadable.
    monkeypatch.setenv("BASECRADLE_ROUTER_WAKE_LOCK_DIR", str(box.locks / "not-created-yet"))

    assert main(["selftest", "freeze"]) == 75


def test_a_non_proven_verdict_states_itself_on_stderr(box, monkeypatch, capsys) -> None:
    # A config error shares exit 75, so stderr is the only place the two are told
    # apart — and it is the stream the NOC forwards on any non-proven verdict.
    monkeypatch.setenv("BASECRADLE_ROUTER_WAKE_LOCK_DIR", str(box.locks / "not-created-yet"))

    main(["selftest", "freeze"])

    err = capsys.readouterr().err
    assert "freeze-readability: degraded" in err
    assert "not-created-yet" in err  # names the surface, as a failing check must


def test_a_proven_verdict_says_nothing_on_stderr(box, capsys) -> None:
    # The corollary: stderr is the non-proven channel. A green probe that wrote there
    # would train the NOC to ignore the stream that carries the diagnosis.
    assert main(["selftest", "freeze"]) == 0
    assert capsys.readouterr().err == ""


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


# --- probe wake -------------------------------------------------------------


def _arm_probe_route(monkeypatch) -> None:
    monkeypatch.setenv("BASECRADLE_ROUTER_ENABLED_ROUTES", "github,probe")
    monkeypatch.setenv("BASECRADLE_ROUTER_PROBE_WEBHOOK_SECRET", "whsec_" + "1" * 32)


def _marker(nonce: str = "0" * 32) -> str:
    # Shape only — the CLI carries markers and never verifies them, so a correctly-shaped
    # fake is exactly what the router's own layer sees.
    return f"BCNOC1 {nonce} {'a' * 64}"


def test_probe_wake_reads_its_marker_from_stdin_never_argv(box, monkeypatch, capsys) -> None:
    # The value is minted by the NOC with the recipient's own probe secret. argv is
    # visible in `ps` to every account on the box, so the NOC's own discipline for
    # mint-probe-secret carries here: stdin, and no --marker flag exists to tempt anyone.
    _arm_probe_route(monkeypatch)
    posted: list[tuple[str, bytes, dict]] = []

    def fake_post(url, body, headers):
        posted.append((url, body, headers))
        return Injection(status=202, stages=(("resolve", "ok"),))

    monkeypatch.setattr("basecradle_router.probe._post_over_http", fake_post)
    monkeypatch.setattr("sys.stdin", io.StringIO(_marker() + "\n"))

    # No wake is ever recorded, so this is the honest "we never got an answer".
    assert main(["probe", "wake", "--agent", "nova", "--timeout", "0"]) == 75

    (url, body, headers) = posted[0]
    assert url.endswith("/webhooks/probe")
    assert json.loads(body) == {"harness_key": "nova", "marker": _marker()}
    assert headers["X-BaseCradle-Probe-Signature"].startswith("sha256=")
    assert "no outcome recorded" in capsys.readouterr().err


def test_probe_wake_reports_proven_only_from_the_daemons_own_record(
    box, monkeypatch, capsys
) -> None:
    # Requirement 5, at the CLI boundary: the probe cannot write the evidence, so it
    # cannot manufacture its own pass. Here the "daemon" records the wake while the
    # injection is in flight, exactly as the real one does.
    from basecradle_router.evidence import EvidenceStore

    _arm_probe_route(monkeypatch)
    store = EvidenceStore(str(box.evidence))

    def fake_post(url, body, headers):
        store.record_wake_ok(
            "nova", headers["X-BaseCradle-Probe-Delivery"], route="probe", synthetic=True
        )
        return Injection(status=202, stages=(("resolve", "ok"),))

    monkeypatch.setattr("basecradle_router.probe._post_over_http", fake_post)
    monkeypatch.setattr("sys.stdin", io.StringIO(_marker()))

    assert main(["probe", "wake", "--agent", "nova", "--json"]) == 0

    result = _json_out(capsys)
    assert result["status"] == "proven"
    assert result["before"]["agent_last_ok_at"] is None
    assert result["after"]["agent_last_ok_at"] is not None
    assert result["after"]["agent_last_ok_synthetic"] is True


def test_probe_wake_with_an_unusable_marker_is_unprovable_not_a_verdict(
    box, monkeypatch, capsys
) -> None:
    # Not an answer about the wake edge — the probe could not be attempted at all — so
    # it lands in the inconclusive bucket with the cause on stderr, and nothing is posted.
    _arm_probe_route(monkeypatch)
    monkeypatch.setattr(
        "basecradle_router.probe._post_over_http",
        lambda *_a: pytest.fail("nothing may be posted for an unusable marker"),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("not-a-marker"))

    assert main(["probe", "wake", "--agent", "nova"]) == 75
    assert "well-formed BCNOC1" in capsys.readouterr().err


def test_probe_wake_needs_the_route_armed_on_this_box(box, monkeypatch, capsys) -> None:
    # The probe route's secret is demanded like every other route's. Without it the
    # daemon would not boot on this config either, so the CLI must not pretend otherwise.
    monkeypatch.setenv("BASECRADLE_ROUTER_ENABLED_ROUTES", "github,probe")
    monkeypatch.setattr("sys.stdin", io.StringIO(_marker()))

    assert main(["probe", "wake", "--agent", "nova"]) == EXIT_CONFIG_ERROR
    assert "BASECRADLE_ROUTER_PROBE_WEBHOOK_SECRET" in capsys.readouterr().err


# --- the config-error exit code --------------------------------------------


def test_a_config_the_daemon_could_not_boot_on_is_unprovable_not_a_pass(
    box, monkeypatch, capsys
) -> None:
    # It shares the inconclusive sentinel with an unprovable probe, because from the
    # ledger's side it is the same state: we never got an answer. What must never
    # happen is a 0 — a config the CLI cannot load is a config the daemon cannot boot
    # on, and that is a finding, not a pass. The two are told apart on stderr.
    monkeypatch.delenv("BASECRADLE_ROUTER_AGENTS")

    assert main(["claims"]) == 75
    assert EXIT_CONFIG_ERROR == 75
    assert "configuration error" in capsys.readouterr().err


def test_a_missing_trusted_actor_list_is_also_unprovable(box, monkeypatch) -> None:
    # The emitter builds the same registry the daemon runs, so a config the daemon
    # would refuse to start on can never yield a manifest describing a live router.
    monkeypatch.delenv("BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS")

    assert main(["claims"]) == EXIT_CONFIG_ERROR

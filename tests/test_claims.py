"""The Claims Manifest emitter, built offline from a fabricated registry.

**This file carries regression instance 4** of the green-while-absent acceptance set
(basecradle/basecradle#460): *the parked-builder wake gap — a builder parked cleanly
with nothing in existence that would ever re-wake it.* The demonstration the program
asks for is
:func:`test_regression_instance_4_a_subject_with_no_wake_edge_is_visible_in_the_claim`.

Everything else here pins the Contract v1 shape the NOC's ledger reads
(basecradle-noc#406) and the emitter's one architectural promise: it names no event
source, so adding a route never edits it. No network, model, or live agent.
Test cast: Nova Digital (``nova``, AI) and JT (``jt``, a harness persona).
"""

from types import MappingProxyType

from basecradle_router.claims import (
    COMPONENT,
    CONTRACT_VERSION,
    build_manifests,
    manifest_filename,
)
from basecradle_router.config import Config
from basecradle_router.evidence import EvidenceStore
from basecradle_router.models import Agent, WakeKind
from basecradle_router.routes import RouteRegistry
from basecradle_router.routes.basecradle import BasecradleRoute
from basecradle_router.routes.github import GithubRoute
from basecradle_router.wakelock import WakeLockGuard

NOVA = Agent(
    key="basecradle/basecradle-python",
    os_user="nova",
    clone_path="/home/nova/basecradle-python",
    bot_slug="basecradle-python-ai",
)
JT = Agent(
    key="jt",
    os_user="jt",
    clone_path="/home/jt/harness",
    wake_kind=WakeKind.HARNESS,
    recipient_uuid="019e916c-7f45-700e-afc0-f45557b237b7",
    wake_bin="/home/jt/venv/bin/basecradle-harness-wake",
)
SECRET = "whsec_" + "0" * 32  # correctly-shaped fake
DELIVERY = "0192f3a4-5b6c-7d8e-9f01-00000000000a"
EVIDENCE_PATH = "/var/lib/basecradle-router/evidence.json"


def _config(agents=(NOVA,), routes=("github",)) -> Config:
    return Config(
        agents=MappingProxyType({a.key: a for a in agents}),
        enabled_routes=frozenset(routes),
        webhook_secrets=MappingProxyType({r: SECRET for r in routes}),
        recipient_index=MappingProxyType({a.recipient_uuid: a for a in agents if a.recipient_uuid}),
    )


def _registry(routes=("github",)) -> RouteRegistry:
    registry = RouteRegistry()
    if "github" in routes:
        registry.register(GithubRoute(["drawkkwast"]))
    if "basecradle" in routes:
        registry.register(BasecradleRoute())
    return registry


def _build(tmp_path, *, config=None, routes=("github",), evidence=None):
    config = config or _config(routes=routes)
    return build_manifests(
        config,
        _registry(routes),
        (evidence or EvidenceStore(None)).snapshot(),
        WakeLockGuard(lock_dir=str(tmp_path)),
        host="ai.basecradle.com",
        evidence_path=EVIDENCE_PATH,
    )


def _subject(manifests, subject: str) -> dict:
    return next(m for m in manifests if m["subject"] == subject)


def _claim(manifest: dict, claim: str) -> dict:
    return next(c for c in manifest["claims"] if c["claim"] == claim)


# --- regression instance 4: the parked builder with no wake edge ------------


def test_regression_instance_4_a_subject_with_no_wake_edge_is_visible_in_the_claim(
    tmp_path,
) -> None:
    """A registered agent no enabled route can reach emits a claim that says so.

    JT is a harness persona: the router resolves it by ``recipient_uuid``, which only
    the ``basecradle`` route produces. With that route disabled — the fleet's actual
    state today — JT is registered, healthy, and **unreachable**: no webhook can
    deliver to it, nothing is queued for it, and it has never been woken. Nothing
    else in the router says so, and every dashboard stays green.

    The claim makes it one machine-readable row: ``edge_count: 0`` (nothing will
    wake it) plus ``evidence: null`` (nothing ever has). That pair is precisely the
    NOC-side detection the handoff asked this to enable — *"agent X is idle AND no
    edge exists that will ever re-wake it"* — and neither half is sufficient alone:
    an agent woken last week may have lost its only edge since, and a freshly-armed
    edge has no history yet.
    """
    manifests = _build(tmp_path, config=_config(agents=(NOVA, JT)), routes=("github",))

    claim = _claim(_subject(manifests, "agent:jt"), "wake-edge:webhook-route")

    assert claim["detail"]["edges"] == []
    assert claim["detail"]["edge_count"] == 0
    assert claim["evidence"] is None  # never-proven
    # And the contrast that makes it a finding rather than a fleet-wide false alarm:
    # the agent the enabled route CAN reach is not flagged.
    reachable = _claim(_subject(manifests, "agent:nova"), "wake-edge:webhook-route")
    assert reachable["detail"]["edge_count"] == 1


def test_enabling_the_route_arms_the_edge_that_was_missing(tmp_path) -> None:
    # The other direction of the same detection: the gap closes when the route that
    # can reach the agent is actually enabled, so the claim tracks configuration
    # rather than restating the registry.
    manifests = _build(
        tmp_path,
        config=_config(agents=(NOVA, JT), routes=("github", "basecradle")),
        routes=("github", "basecradle"),
    )

    claim = _claim(_subject(manifests, "agent:jt"), "wake-edge:webhook-route")

    assert claim["detail"]["edges"] == [
        {"kind": "webhook-route", "source": "basecradle", "resolves_by": "recipient_uuid"}
    ]


def test_a_registered_route_that_is_not_enabled_arms_nothing(tmp_path) -> None:
    # Registered-but-disabled is the subtler half: the route object exists, so a
    # naive "is there a route for this kind" check would call the edge armed.
    config = _config(agents=(NOVA,), routes=("github",))
    manifests = build_manifests(
        config,
        _registry(("github", "basecradle")),  # both registered...
        EvidenceStore(None).snapshot(),
        WakeLockGuard(lock_dir=str(tmp_path)),
        host="ai.basecradle.com",
    )

    edges = _claim(_subject(manifests, "agent:nova"), "wake-edge:webhook-route")["detail"]["edges"]
    assert [e["source"] for e in edges] == ["github"]  # ...only the enabled one counts


def test_a_queued_wake_is_reported_as_the_transient_edge_it_is(tmp_path) -> None:
    evidence = EvidenceStore(None)
    evidence.record_queue_depth("nova", 3)

    manifests = _build(tmp_path, evidence=evidence)

    edges = _claim(_subject(manifests, "agent:nova"), "wake-edge:webhook-route")["detail"]["edges"]
    assert {"kind": "queued-wake", "pending": 3} in edges


def test_a_successful_wake_becomes_the_claims_evidence(tmp_path) -> None:
    evidence = EvidenceStore(None)
    evidence.record_wake_ok("nova", DELIVERY)

    claim = _claim(
        _subject(_build(tmp_path, evidence=evidence), "agent:nova"), "wake-edge:webhook-route"
    )

    assert "stage=wake outcome=ok" in claim["evidence"]
    assert DELIVERY in claim["evidence"]
    assert claim["prove"] == {
        "kind": "evidence",
        "source": f"{EVIDENCE_PATH}#agent_wakes.nova.last_ok_at",
    }


def test_the_agents_current_freeze_state_rides_on_the_wake_edge_claim(tmp_path) -> None:
    # Reported as detail, not as a claim of its own: a held lock suspends the edge, it
    # does not remove it, and a row that flipped to never-proven on every converge
    # would be worse than useless.
    (tmp_path / "nova.lock").write_text(
        '{"agent": "nova", "reason": "converge", "expires_at": "2999-01-01T00:00:00+00:00"}',
        encoding="utf-8",
    )

    detail = _claim(_subject(_build(tmp_path), "agent:nova"), "wake-edge:webhook-route")["detail"]

    assert detail["wake_lock"]["state"] == "held"
    assert detail["wake_lock"]["would_wake"] is False
    assert detail["wake_lock"]["path"].endswith("nova.lock")
    assert detail["edge_count"] == 1  # suspended, not absent


# --- the box's own surfaces -------------------------------------------------


def test_the_freeze_claim_is_proven_by_running_the_probe(tmp_path) -> None:
    # The only claim whose proof is a command: nothing in the router's history
    # demonstrates the wake-lock directory is readable *now*.
    claim = _claim(_subject(_build(tmp_path), "box:ai.basecradle.com"), "freeze-surface:readable")

    assert claim["prove"]["kind"] == "probe"
    assert claim["prove"]["cmd"].endswith("selftest freeze --json")
    assert claim["ttl_hours"] == 24
    assert claim["detail"]["lock_dir"] == str(tmp_path)
    assert claim["detail"]["exit_codes"] == {"ok": 0, "failed": 1, "degraded": 2}


def test_the_last_selftest_is_the_freeze_claims_evidence(tmp_path) -> None:
    evidence = EvidenceStore(None)
    evidence.record_freeze_selftest("ok", "4 check(s) passed")

    claim = _claim(
        _subject(_build(tmp_path, evidence=evidence), "box:ai.basecradle.com"),
        "freeze-surface:readable",
    )

    assert "status=ok" in claim["evidence"]
    assert claim["detail"]["last_selftest"]["detail"] == "4 check(s) passed"


def test_a_sink_with_rejections_and_no_accepts_is_never_proven(tmp_path) -> None:
    # Instance 5, as the ledger sees it: armed on paper, every delivery rejected on a
    # secret mismatch. The counters are what separate this from a sink nobody used.
    evidence = EvidenceStore(None)
    for _ in range(417):
        evidence.record_delivery_rejected("github", "X-Hub-Signature-256 does not match")

    claim = _claim(
        _subject(_build(tmp_path, evidence=evidence), "box:ai.basecradle.com"),
        "delivery-sink:github",
    )

    assert claim["evidence"] is None
    assert claim["detail"]["rejected"] == 417
    assert claim["detail"]["accepted"] == 0
    assert "does not match" in claim["detail"]["last_reject_reason"]


def test_an_accepted_delivery_proves_the_sink(tmp_path) -> None:
    evidence = EvidenceStore(None)
    evidence.record_delivery_accepted("github")
    evidence.record_delivery_decision("github", woke=True)

    claim = _claim(
        _subject(_build(tmp_path, evidence=evidence), "box:ai.basecradle.com"),
        "delivery-sink:github",
    )

    assert "1 delivery(s) verified" in claim["evidence"]
    assert claim["detail"]["woke"] == 1


def test_one_sink_claim_per_enabled_route(tmp_path) -> None:
    manifests = _build(
        tmp_path,
        config=_config(agents=(NOVA, JT), routes=("github", "basecradle")),
        routes=("github", "basecradle"),
    )

    box = _subject(manifests, "box:ai.basecradle.com")
    assert [c["claim"] for c in box["claims"]] == [
        "freeze-surface:readable",
        "delivery-sink:basecradle",
        "delivery-sink:github",
    ]


# --- the contract's shape ---------------------------------------------------


def test_every_manifest_carries_the_pinned_contract_envelope(tmp_path) -> None:
    for manifest in _build(tmp_path, config=_config(agents=(NOVA, JT))):
        assert manifest["contract"] == CONTRACT_VERSION
        assert manifest["component"] == COMPONENT
        assert manifest["subject"].split(":")[0] in ("box", "agent")
        assert manifest["claims"]


def test_every_claim_carries_the_pinned_contract_keys(tmp_path) -> None:
    pinned = {"claim", "class", "prove", "evidence", "ttl_hours"}
    for manifest in _build(tmp_path, config=_config(agents=(NOVA, JT))):
        for claim in manifest["claims"]:
            assert pinned <= set(claim)
            assert claim["class"] in ("active", "rare", "dependency")
            assert set(claim["prove"]) in ({"kind", "cmd"}, {"kind", "source"})
            # The one deliberate, additive extension — see the module docstring.
            assert set(claim) - pinned == {"detail"}


def test_the_box_subject_comes_first_and_agents_are_ordered(tmp_path) -> None:
    # A manifest built twice from unchanged inputs must be byte-identical, or a
    # ledger diff shows churn that is not change.
    subjects = [m["subject"] for m in _build(tmp_path, config=_config(agents=(JT, NOVA)))]

    assert subjects == ["box:ai.basecradle.com", "agent:jt", "agent:nova"]


def test_two_registry_entries_for_one_harness_instance_are_one_subject(tmp_path) -> None:
    # The subject is the harness instance (the OS user), not the registry key — the
    # same key the lock, the breaker and the journal identifier all use.
    twin = Agent(
        key="basecradle/basecradle-python-sdk",
        os_user="nova",
        clone_path="/home/nova/basecradle-python",
        bot_slug="basecradle-python-ai",
    )

    subjects = [m["subject"] for m in _build(tmp_path, config=_config(agents=(NOVA, twin)))]

    assert subjects.count("agent:nova") == 1


def test_disabled_persistence_is_stated_in_the_evidence_pointer(tmp_path) -> None:
    # A box whose evidence is memory-only cannot prove anything across a restart —
    # itself worth surfacing rather than emitting a path that does not exist.
    manifests = build_manifests(
        _config(),
        _registry(),
        EvidenceStore(None).snapshot(),
        WakeLockGuard(lock_dir=str(tmp_path)),
        host="ai.basecradle.com",
        evidence_path=None,
    )

    source = _claim(_subject(manifests, "agent:nova"), "wake-edge:webhook-route")["prove"]["source"]
    assert source.startswith("(in-memory evidence, not persisted)")


def test_the_per_subject_filename_is_filesystem_safe(tmp_path) -> None:
    manifests = _build(tmp_path)

    assert manifest_filename(manifests[0]) == "basecradle-router.box-ai.basecradle.com.json"
    assert manifest_filename(manifests[1]) == "basecradle-router.agent-nova.json"


def test_the_probe_command_follows_a_non_standard_deploy_tree(tmp_path) -> None:
    # The NOC schedules whatever prove.cmd says, so an overridden wrapper path has to
    # reach the manifest — otherwise the probe it runs is not the one we described.
    manifests = build_manifests(
        _config(),
        _registry(),
        EvidenceStore(None).snapshot(),
        WakeLockGuard(lock_dir=str(tmp_path)),
        host="ai.basecradle.com",
        admin_cmd="/srv/router/admin",
    )

    claim = _claim(_subject(manifests, "box:ai.basecradle.com"), "freeze-surface:readable")
    assert claim["prove"]["cmd"] == "/srv/router/admin selftest freeze --json"

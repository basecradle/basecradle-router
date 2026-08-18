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

**The load-bearing test in this file is
:func:`test_every_declared_evidence_pointer_resolves_from_its_own_detail`.** A claim
whose pointer the NOC cannot land on is not *wrong* — it is refused, loudly and by
name, and then never armed, which is a capability that stays unproven while looking
like it is being watched. That is the same silence this whole program attacks, one
level up, and it is a shape nothing else here would catch: every other assertion in
this file passes just as happily with an unarmable pointer (basecradle-noc#417).
"""

import re
from types import MappingProxyType

import pytest

from basecradle_router.app import build_registry
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


def _resolve(claim: dict) -> object:
    """Run the NOC's shipped evidence-pointer rule against one claim, and return its value.

    A deliberate re-statement of ``EvidenceResolver._pointer`` (basecradle-noc#409),
    kept to four lines because that is all the rule is: **the pointer's last segment
    is the field, and the claim's ``detail`` is the object it lives in.** The NOC owns
    the rule; this is the emitter's test oracle for it, and it earns its keep because
    the alternative is discovering an unarmable pointer the way we discovered the last
    one — from a monitor on another box, a week later.

    Raises exactly where the resolver would refuse, so the assertion message names the
    same thing the NOC's ledger row would.
    """
    source = claim["prove"]["source"]
    field = source.partition("#")[2].rsplit(".", 1)[-1].strip()
    detail = claim.get("detail") or {}
    assert field, f"pointer {source!r} names no field after '#'"
    assert field in detail, (
        f"UNPROVABLE: {claim['claim']}'s detail does not carry {field!r}, which its pointer "
        f"{source!r} names (it has: {sorted(detail)})"
    )
    value = detail[field]
    assert value is None or isinstance(value, str), (
        f"UNPROVABLE: {claim['claim']}'s pointer resolved {field!r} to a "
        f"{type(value).__name__}, not a timestamp"
    )
    return value


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
        {
            "kind": "webhook-route",
            "source": "basecradle",
            "resolves_by": "recipient_uuid",
            # Armed, and honest that being armed proves nothing yet — in all four
            # outcomes, so "never exercised" and "exercised and refused every time"
            # can never read alike (basecradle-router#208), and a collapsed duplicate
            # never reads as a rejection (basecradle-router#218).
            "ok": 0,
            "failed": 0,
            "refused": 0,
            "deduped": 0,
            "last_ok_at": None,
            "last_ok_delivery": None,
            "last_failed_at": None,
            "last_failed_reason": None,
            "last_refused_at": None,
            "last_refused_reason": None,
            "last_deduped_at": None,
        }
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
    evidence.record_wake_ok("nova", DELIVERY, route="github", synthetic=False)

    claim = _claim(
        _subject(_build(tmp_path, evidence=evidence), "agent:nova"), "wake-edge:webhook-route"
    )

    assert "stage=wake outcome=ok" in claim["evidence"]
    assert DELIVERY in claim["evidence"]
    assert "route=github" in claim["evidence"]
    assert claim["prove"] == {
        "kind": "evidence",
        "source": f"{EVIDENCE_PATH}#agent_wakes.nova.last_ok_at",
    }
    # And the pointer lands: `last_ok_at` sits at the top of this claim's own detail,
    # which is the only reason the NOC can arm the row at all.
    assert _resolve(claim) == claim["detail"]["last_ok_at"]
    assert _resolve(claim) is not None


def test_a_collapsed_duplicate_is_published_apart_from_a_refusal(tmp_path) -> None:
    """A healthy route must never publish as a rejected one (basecradle-router#218).

    Measured live on both builders' ``wake-edge:webhook-route:github`` rows:
    ``ok=4 failed=0 refused=2``, the newest refusal 2.6 ms after the newest success and
    both of them idempotent dedups. A consumer reading *the newest recorded attempt was
    refused* saw a route in trouble that had rejected nothing.

    **The counter is the classification.** The NOC cannot tell a benign collapse from a
    real refusal by reading our ``reason`` strings — that would be a second spelling of
    this repo's contract living in its repo, which its own rulings forbid
    (basecradle-noc#344/#366). So the split has to be visible in the published fields,
    at both granularities, and ``last_refused_at`` must not move for a dedup.
    """
    evidence = EvidenceStore(None)
    evidence.record_wake_ok("nova", DELIVERY, route="github", synthetic=False)
    evidence.record_wake_deduped("nova", route="github", synthetic=False)

    subject = _subject(_build(tmp_path, evidence=evidence), "agent:nova")
    agent_row = _claim(subject, "wake-edge:webhook-route")["detail"]
    route_row = _claim(subject, "wake-edge:webhook-route:github")["detail"]

    for row in (agent_row, route_row):
        assert (row["ok"], row["failed"], row["refused"], row["deduped"]) == (1, 0, 0, 1)
        assert row["last_refused_at"] is None
        assert row["last_deduped_at"] is not None
    # Provenance rides the agent-wide dedup trio like every other outcome's, so a reader
    # never has to know which route names are the fleet's own probes.
    assert (agent_row["last_deduped_route"], agent_row["last_deduped_synthetic"]) == (
        "github",
        False,
    )
    # And the row the ledger actually arms still resolves to the success, which is the
    # whole outcome being bought: nothing about a dedup disturbs the proof.
    assert _resolve(_claim(subject, "wake-edge:webhook-route:github")) is not None


def test_regression_instance_5_per_recipient_an_armed_edge_can_be_unproven(tmp_path) -> None:
    """Instance 5 read per *recipient*: both coarser projections read green here.

    Nova is dual-wired — a builder that also holds a platform account, so both enabled
    routes can reach it — and both routes' sinks have verified a delivery. So the
    **per-route** projection is green for `basecradle` (its secret demonstrably
    matches) and the **per-agent** projection is green for nova (something woke it).
    What is actually true is narrower: `basecradle` has never woken *anyone*, and
    nothing has ever woken JT.

    Only the per-(agent, route) proof draws that line, which is why the NOC declined to
    arm its per-recipient rows on either substitution (basecradle-noc#408). It is read
    twice here — off the agent-wide claim's ``edges`` (the operator's one-row view) and
    off the per-route claim the ledger actually arms — because only the second is a row
    the NOC can prove, and the two must never disagree about the same wake.
    """
    nova_dual = Agent(
        key=NOVA.key,
        os_user=NOVA.os_user,
        clone_path=NOVA.clone_path,
        bot_slug=NOVA.bot_slug,
        recipient_uuid="019e916c-7f45-700e-afc0-f45557b23800",
    )
    evidence = EvidenceStore(None)
    evidence.record_delivery_accepted("github")
    evidence.record_delivery_accepted("basecradle")  # the sink is armed and verified...
    evidence.record_wake_ok(
        "nova", DELIVERY, route="github", synthetic=False
    )  # ...but never woke a soul

    manifests = _build(
        tmp_path,
        config=_config(agents=(nova_dual, JT), routes=("github", "basecradle")),
        routes=("github", "basecradle"),
        evidence=evidence,
    )

    def edge(subject: str, source: str) -> dict:
        edges = _claim(_subject(manifests, subject), "wake-edge:webhook-route")["detail"]["edges"]
        return next(e for e in edges if e.get("source") == source)

    def row(subject: str, source: str) -> dict:
        return _claim(_subject(manifests, subject), f"wake-edge:webhook-route:{source}")

    assert edge("agent:nova", "github")["last_ok_delivery"] == DELIVERY
    # Same agent, verified sink, the *other* route: armed, and never proven. The
    # per-agent projection would have greened this off the github wake above.
    assert edge("agent:nova", "basecradle")["last_ok_at"] is None
    # Same route, a different recipient: also never proven. The per-route projection
    # would have greened this off the basecradle accept above.
    assert edge("agent:jt", "basecradle")["last_ok_at"] is None

    # The same three readings off the rows the ledger arms. `null` here is not a miss:
    # the NOC reads a resolved null as FAIL — "the emitter publishes this field and
    # nothing has ever landed in it" — which is exactly instance 5's answer.
    assert _resolve(row("agent:nova", "github")) is not None
    assert _resolve(row("agent:nova", "basecradle")) is None
    assert _resolve(row("agent:jt", "basecradle")) is None
    assert row("agent:nova", "basecradle")["evidence"] is None
    assert row("agent:nova", "github")["evidence"].endswith("route=github")


def test_one_armable_row_per_armed_recipient_route_pair(tmp_path) -> None:
    """The per-recipient roster the NOC's hand-written rows retire in favour of.

    basecradle-noc#417 step 3 replaces eight transcribed ``basecradle-platform@*``
    delivery-sink rows with the router's own, on the reasoning that the router is the
    authority on its own delivery and a second spelling of a fact can later disagree
    with the first. That only works if the roster is complete and enumerable: one row
    per armed ``(agent, route)`` pair, with the pair's identity readable off the row
    rather than parsed back out of the claim id.
    """
    manifests = _build(
        tmp_path,
        config=_config(agents=(NOVA, JT), routes=("github", "basecradle")),
        routes=("github", "basecradle"),
    )

    assert [c["claim"] for c in _subject(manifests, "agent:nova")["claims"]] == [
        "wake-edge:webhook-route",
        "wake-edge:webhook-route:github",  # nova is resolvable by repo, not by uuid
    ]
    jt_row = _claim(_subject(manifests, "agent:jt"), "wake-edge:webhook-route:basecradle")
    assert jt_row["detail"]["harness_key"] == "jt"
    assert jt_row["detail"]["route"] == "basecradle"
    assert jt_row["detail"]["resolves_by"] == "recipient_uuid"


def test_the_per_route_wake_record_survives_a_route_being_disarmed(tmp_path) -> None:
    # `edges` only carries routes armed *now*, so a route that used to wake the agent
    # and has since been disabled would vanish without a trace. The raw per-route
    # record keeps it — an agent whose only proof came from a route nobody enables any
    # more is exactly the parked-builder shape, not a proven edge.
    evidence = EvidenceStore(None)
    evidence.record_wake_ok("nova", DELIVERY, route="basecradle", synthetic=False)

    manifest = _subject(_build(tmp_path, evidence=evidence, routes=("github",)), "agent:nova")
    detail = _claim(manifest, "wake-edge:webhook-route")["detail"]

    assert [e["source"] for e in detail["edges"]] == ["github"]
    assert detail["edges"][0]["last_ok_at"] is None
    assert detail["by_route"]["basecradle"]["ok"] == 1
    assert detail["last_ok_route"] == "basecradle"
    # And no per-route row for it: a claim states a capability the router has *now*,
    # and a route the box no longer enables cannot wake anyone. Asserting otherwise
    # would be the paper-armed integration this program exists to catch, one level up.
    assert [c["claim"] for c in manifest["claims"]] == [
        "wake-edge:webhook-route",
        "wake-edge:webhook-route:github",
    ]


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
    # The contract's codes, pinned literally: 0 PASS, 1 FAIL, 75 (EX_TEMPFAIL) the one
    # inconclusive sentinel — which a config error shares, because both mean "no
    # answer" and the contract has no third non-zero tier (basecradle-noc#408).
    assert claim["detail"]["exit_codes"] == {
        "ok": 0,
        "failed": 1,
        "degraded": 75,
        "config_error": 75,
    }


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
        "log-grammar:breaker_tripped",
        "delivery-sink:basecradle",
        "delivery-sink:github",
    ]


# --- the contract's shape ---------------------------------------------------


def _every_shape(tmp_path) -> list[dict]:
    """Manifests exercising every claim this emitter can emit, on one fabricated box.

    Both routes enabled and both agents registered, so every claim family appears at
    once — and with evidence deliberately lopsided (github has woken nova, basecradle
    has verified a delivery but woken nobody), so both the proven and the never-proven
    resolution paths are exercised in the same pass.
    """
    nova_dual = Agent(
        key=NOVA.key,
        os_user=NOVA.os_user,
        clone_path=NOVA.clone_path,
        bot_slug=NOVA.bot_slug,
        recipient_uuid="019e916c-7f45-700e-afc0-f45557b23800",
    )
    evidence = EvidenceStore(None)
    evidence.record_wake_ok("nova", DELIVERY, route="github", synthetic=False)
    evidence.record_delivery_accepted("github")
    evidence.record_delivery_accepted("basecradle")
    evidence.record_delivery_rejected("basecradle", "signature does not match")
    evidence.record_freeze_selftest("ok", "4 check(s) passed")
    evidence.record_queue_depth("jt", 2)
    return _build(
        tmp_path,
        config=_config(agents=(nova_dual, JT), routes=("github", "basecradle")),
        routes=("github", "basecradle"),
        evidence=evidence,
    )


def test_every_declared_evidence_pointer_resolves_from_its_own_detail(tmp_path) -> None:
    """**The pointer the NOC cannot land on is the failure this test exists for.**

    An ``evidence`` claim is proven by resolving ``<path>#<dotted.field>`` — and the NOC
    resolves it from the claim's own ``detail``, not from the file: it has no shell on
    this box, no wrapper op reads ``/var/lib``, so the census that carries the manifest
    is also the transport for its evidence. The rule it shipped is one line: *the
    pointer's last segment is the field, and ``detail`` is the object it lives in*
    (basecradle-noc#409).

    A pointer that misses is refused with a named reason and reads ``unprovable``. That
    is honest and loud — and it is still a capability nobody is watching, because an
    unprovable row is never armed. The wake-edge claim shipped exactly that way in
    ``f00836d``: its timestamp sat one level down at ``detail.wakes.last_ok_at``, every
    other test in this file passed, and the miss was found by a monitor on another box
    (basecradle-noc#417, finding 2). So the rule is re-run here over *every* claim, and
    a new claim cannot be added without satisfying it.
    """
    claims = [c for m in _every_shape(tmp_path) for c in m["claims"]]
    pointered = [c for c in claims if c["prove"]["kind"] == "evidence"]

    for claim in pointered:
        _resolve(claim)  # raises where the NOC's resolver would refuse

    # Coverage, so a build that quietly stopped emitting a family cannot pass by
    # vacuous truth: every evidence claim this emitter has, in one pass.
    assert {c["claim"] for c in pointered} == {
        "delivery-sink:github",
        "delivery-sink:basecradle",
        "wake-edge:webhook-route",
        "wake-edge:webhook-route:github",
        "wake-edge:webhook-route:basecradle",
    }
    # ...and both resolution outcomes, so the null path is exercised rather than assumed.
    resolved = [_resolve(c) for c in pointered]
    assert any(v is not None for v in resolved) and any(v is None for v in resolved)


def test_every_claim_id_matches_the_contracts_grammar(tmp_path) -> None:
    # A claim id crosses to the box as a wrapper TOKEN, so the contract bounds its
    # charset to what can never be anything but inert data — no slash, no '..', no shell
    # metacharacter (basecradle-noc#406). The per-route ids interpolate a route name, so
    # this is the check that keeps a future route from minting an id the NOC refuses.
    grammar = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")

    for manifest in _every_shape(tmp_path):
        for claim in manifest["claims"]:
            assert grammar.match(claim["claim"]), claim["claim"]
            assert ".." not in claim["claim"]
            assert len(claim["claim"]) <= 120

    # And at the source — the registry the *daemon* builds, not this file's fixture —
    # so a route added years from now is caught where it is added rather than only if
    # someone remembers to extend the fixture above. Two claim ids interpolate a route's
    # own name, which makes `Route.name` part of the claims contract.
    daemon_registry = build_registry(
        _config(routes=("github", "basecradle")),
        {"BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS": "drawkkwast"},
    )
    for route in daemon_registry.routes():
        assert grammar.match(route.name), route.name


def test_one_id_means_one_claim_within_a_subject(tmp_path) -> None:
    # The contract refuses a manifest that declares an id twice — "or the file says one
    # thing and its reader does another". The per-route rows share a prefix with the
    # agent-wide row, so this is the collision worth pinning rather than assuming.
    for manifest in _every_shape(tmp_path):
        ids = [c["claim"] for c in manifest["claims"]]
        assert len(ids) == len(set(ids))


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


def test_the_per_route_rows_and_edges_are_ordered_by_route(tmp_path) -> None:
    # Same byte-identical requirement, one level down — and it must not rest on the
    # order somebody happened to call `register` in, which is a detail of the
    # composition root. It doesn't: the registry enumerates name-ordered, and both
    # views of the armed set are built straight off that one enumeration, so they can
    # never read in different orders.
    nova = _subject(_every_shape(tmp_path), "agent:nova")

    assert [c["claim"] for c in nova["claims"]] == [
        "wake-edge:webhook-route",
        "wake-edge:webhook-route:basecradle",
        "wake-edge:webhook-route:github",
    ]
    agent_wide = _claim(nova, "wake-edge:webhook-route")["detail"]
    assert [e["source"] for e in agent_wide["edges"]] == ["basecradle", "github"]
    # Registered github-first, enumerated basecradle-first: the order is the registry's
    # rule, not the caller's.
    assert [r.name for r in _registry(("github", "basecradle")).routes()] == [
        "basecradle",
        "github",
    ]


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


def test_the_per_subject_filename_is_the_one_the_contract_pins(tmp_path) -> None:
    """The names are a constraint, not taste — the NOC's probe resolves them literally.

    ``run-claim-probe`` looks for ``$CLAIMS_DIR/<component>@<os_user>.json`` before it
    will run anything, so a file spelled any other way is a claim that can never be
    proven. The box manifest carries no host in its name: one box, one box-manifest
    per component, and a second spelling of a fact the body already states is a thing
    that can later disagree with it (basecradle-noc#408, ruling 1).
    """
    manifests = _build(tmp_path)

    assert manifest_filename(manifests[0]) == "basecradle-router.json"
    assert manifest_filename(manifests[1]) == "basecradle-router@nova.json"


def test_an_unrecognised_subject_raises_rather_than_colliding(tmp_path) -> None:
    # The tempting fallback — the bare component name — is the box manifest's own
    # filename, so a subject kind this function does not know would silently overwrite
    # the box's claims with something else's. Loud beats a lost manifest.
    with pytest.raises(ValueError, match="no contract filename"):
        manifest_filename({"subject": "cluster:fleet"})


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

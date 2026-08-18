"""The Claims Manifest emitter — what the router claims, and what proves it.

The router is the fleet's wake edge. Every agent it serves is reachable *because
the router says so*, and until now that was an assumption nobody could check: an
agent could sit registered, healthy, and permanently unreachable, and every
dashboard would stay green. That is the shape the green-while-absent program
attacks (basecradle/basecradle#460) — fleet observability catches failures that
*happen*, and none of these do.

This module turns the router's assumptions into **claims with evidence**, in the
capital-pinned **Claims Manifest Contract v1** the NOC's claims-vs-evidence ledger
consumes (basecradle-noc#406). The NOC's converge runs the emitter and writes its
output under ``/etc/basecradle/claims.d/``; the ledger then diffs each claim's
last demonstrable success against its cadence class and reports **green / stale /
never-proven**. The router emits; the NOC judges. We never grade our own homework.

**The three claims, and the incident each one closes.**

- ``wake-edge:webhook-route`` — one per registered agent, subject ``agent:<slug>``.
  Its ``detail.edges`` lists every path by which the router could wake that agent
  *right now* (an armed webhook route, a queued or in-flight wake), and its
  evidence is the last ``stage=wake outcome=ok``. **``edge_count: 0`` with
  ``evidence: null`` is a parked builder with nothing in existence that will ever
  re-wake it** — incident instance 4, in one machine-readable row.
- ``wake-edge:webhook-route:<route>`` — one per **armed** webhook-route edge, same
  ``agent:<slug>`` subject. The agent-wide row above says *something* reached the
  agent; this one says whether **this route** did, which is the granularity instance
  5 is actually asked at: an armed edge whose ``last_ok_at`` is ``null`` while that
  route's sink counts hundreds of rejections is a broken integration for *this*
  recipient, and no agent-wide or route-wide scalar can say so (basecradle-noc#408).
  These are the per-recipient rows the NOC's eight hand-written
  ``basecradle-platform@*`` delivery-sink rows retire in favour of
  (basecradle-noc#417, step 3) — so there is one row per armed ``(agent, route)``
  pair, and a route that is registered but not enabled gets none.
- ``wake-edge:synthetic:<route>`` — one per **armed synthetic** route, same
  ``agent:<slug>`` subject. The two rows above are proven only by something that
  *happened*, and for a deliberately quiet agent nothing ever does — an
  ``evidence``-kind claim cannot exercise itself, so its only remedy was a social one
  (*"go message @pinky"*), which shared law now forbids outright: **a monitor never
  depends on a consent or trust surface**. This row is the lever that replaces it — the
  router firing a signed test delivery at its own real verify→wake path, on-box, with
  no platform account and no relationship with anyone (`basecradle-noc#421`,
  basecradle-router#208). It is proven by the *same* ``last_ok_at`` a real wake writes,
  never by the probe process's own exit code, and it is deliberately **not** counted as
  a wake edge: a probe is the fleet reaching an agent on purpose, not the world being
  able to.
- ``freeze-surface:readable`` — subject ``box:<host>``. Proven by *running* the
  freeze self-test (:mod:`basecradle_router.selftest`), because readability is not
  a fact you can look up; it is a fact you have to demonstrate with the daemon's
  own credentials. Incident instance 2 — the control that existed but could not be
  read when it mattered.
- ``delivery-sink:<route>`` — one per enabled route, subject ``box:<host>``.
  Accept/reject counters plus the last accepted delivery, which is what separates
  ``accepted=0 rejected=417`` (a mismatched secret — armed on paper, every delivery
  silently rejected: incident instance 5) from ``accepted=0 rejected=0`` (a sink
  nobody has used).

**Every pointer this emitter declares must resolve, and the shape is what makes it
resolve.** An ``evidence``-kind claim's ``prove.source`` is a ``<path>#<dotted.field>``
pointer, and the NOC resolves it **from the claim's own ``detail``, never from the
file** — it has no shell on this box and no wrapper op reads ``/var/lib``, so the
census (which returns each manifest's whole parsed body) is the transport. The rule
the NOC shipped is one line: **the pointer's last segment is the field, and ``detail``
is the object it lives in** (basecradle-noc#409). Anything it cannot land on is refused
with a named reason and reads *unprovable* — loud, never green, but also never armed.

So the emitter's obligation is structural: **each claim's ``detail`` is this emitter's
projection of the exact sub-object its own pointer walks into**, flat, with the
descriptive keys hung beside it rather than around it. ``agent_wakes.<key>.last_ok_at``
is paired with a ``detail`` carrying ``last_ok_at`` at its top;
``agent_wakes.<key>.by_route.<route>.last_ok_at`` gets its own claim whose ``detail`` is
that per-route sub-object. Nesting the timestamp one level down — which is what the
first cut of this emitter did — costs nothing at emit time and silently makes the claim
unarmable (basecradle-noc#417, finding 2). :func:`_pointer` takes the container and the
field separately for that reason, and
``tests/test_claims.py::test_every_declared_evidence_pointer_resolves_from_its_own_detail``
re-runs the NOC's rule over every emitted claim so the pairing cannot drift.

**Cadence classes.** All of them are ``rare``: the normal state of a wake edge, a
freeze surface, and a webhook sink is *silence*, so cadence monitoring cannot save
us and only a TTL on the age of proof will. Wake edges and delivery sinks get a
generous 7-day TTL — long enough that an ordinarily quiet week never cries wolf,
short enough that "nothing has woken this agent all month" surfaces while it still
means something. The freeze surface gets 24 h because proving it is a stat and a
parse, so there is no reason to trust yesterday's answer.

**One extension to Contract v1, now ratified.** Each claim carries an extra
``detail`` object beside the pinned
``claim``/``class``/``prove``/``evidence``/``ttl_hours`` keys. Without it the emitter
could report *whether* a wake edge had ever fired but not *whether one exists* — and
the parked-builder detection needs both. It is purely additive, so a consumer reading
only the pinned keys is unaffected. The NOC owns the contract, so it was raised
upward rather than assumed, and ratified as an **optional additive key**: parsed,
validated as an object and nothing more, round-tripped, and deliberately never
persisted to the ledger — it is for the reader and the operator
(basecradle-noc#408, ruling 2).

**Two subjects' worth of manifests, two surfaces.** The array on stdout is the
lossless form; ``--out-dir`` writes the same manifests as one strict single-subject
file each, named as the contract pins it (see :func:`manifest_filename`). Both are
ratified and both are kept: stdout is what ``provision-claims`` reads per subject,
the directory is what the census walks (ruling 1).

**No source knowledge lives here.** Whether an agent is reachable is computed by
pairing each enabled route's
:attr:`~basecradle_router.routes.base.Route.recipient_kind` with
:meth:`~basecradle_router.config.Config.resolvable_by` — so adding an event source
means implementing one route module, and never editing this file.
"""

from __future__ import annotations

import re
import socket

from basecradle_router.config import DEFAULT_ADMIN_CMD, Config
from basecradle_router.evidence import (
    AgentWakeEvidence,
    DeliverySinkEvidence,
    EvidenceDocument,
    RouteWakeEvidence,
)
from basecradle_router.log_grammar import IDENTIFIER as LOG_GRAMMAR_IDENTIFIER
from basecradle_router.log_grammar import LINE_CLASS as LOG_GRAMMAR_LINE_CLASS
from basecradle_router.log_grammar import manifest_detail as log_grammar_detail
from basecradle_router.models import Agent, WakeKind
from basecradle_router.routes import Route, RouteRegistry
from basecradle_router.selftest import EXIT_CODES as FREEZE_EXIT_CODES
from basecradle_router.selftest import EXIT_UNPROVABLE
from basecradle_router.wakelock import WakeLockGuard

#: The manifest schema version this emitter writes — the capital-pinned Contract v1.
CONTRACT_VERSION = 1

#: The emitting component, as it appears in every manifest and in the NOC's
#: ``/etc/basecradle/claims.d/<component>.json`` discovery path. It is the *repo*
#: name (the software), not the builder AI's — the daemon is what makes the claims.
COMPONENT = "basecradle-router"

#: Age-of-proof thresholds, in hours. A week for the edges and sinks — a quiet week
#: is normal, a quiet month is a finding. A day for the freeze surface, which is
#: cheap enough to re-prove that stale evidence is never worth accepting.
WAKE_EDGE_TTL_HOURS = 168
DELIVERY_SINK_TTL_HOURS = 168
FREEZE_TTL_HOURS = 24

#: The log-grammar claim's age-of-proof threshold, and **the one TTL here that is
#: load-bearing for another instrument**. The NOC's extraction guard judges the router's
#: line class on a one-hour window, so the guard reads *deaf* — on a perfectly healthy
#: fleet — in any hour this probe did not fire. One hour is what keeps it due on
#: essentially every 30-minute exerciser pass (a ``rare`` claim is due within the 45-minute
#: refresh margin of expiry, so worst-case proof age is 60 − 45 + 30 = 45 min). Raising it
#: would not merely age the row; it would page. **The number is the NOC's to set** from its
#: own constants (capital ruling, basecradle-noc#509 §7) — this is the value it implies
#: today, and the reasoning is written down here so a later change is made knowingly.
LOG_GRAMMAR_TTL_HOURS = 1

_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def build_manifests(
    config: Config,
    registry: RouteRegistry,
    evidence: EvidenceDocument,
    guard: WakeLockGuard,
    *,
    host: str | None = None,
    evidence_path: str | None = None,
    admin_cmd: str = DEFAULT_ADMIN_CMD,
) -> list[dict]:
    """Every Contract v1 manifest this box's router emits — box first, then agents.

    One manifest per **subject**, because the ledger keeps one row per claimed
    capability *per subject* and the router serves many: a ``box:<host>`` manifest
    for the surfaces the daemon owns as a whole, and an ``agent:<slug>`` manifest
    per registered agent. Agents are emitted in ``harness_key`` order so a manifest
    built twice from unchanged inputs is byte-identical and a ledger diff shows only
    real change.
    """
    host = host or socket.gethostname()
    agents = sorted(_unique_agents(config), key=lambda a: a.harness_key)
    manifests = [
        _box_manifest(config, registry, evidence, guard, host, evidence_path, admin_cmd),
    ]
    manifests += [
        _agent_manifest(agent, config, registry, evidence, guard, evidence_path) for agent in agents
    ]
    return manifests


def _unique_agents(config: Config) -> list[Agent]:
    """The registered agents, de-duplicated by harness instance.

    Keyed by ``harness_key`` because that — not the registry key — is the agent's
    one harness instance, and the whole vocabulary of a wake (the lock, the breaker,
    the journal identifier, and now the claim subject) is keyed on it. Two registry
    entries pointing at one OS user are one subject, not two.
    """
    by_instance: dict[str, Agent] = {}
    for agent in config.agents.values():
        by_instance.setdefault(agent.harness_key, agent)
    return list(by_instance.values())


# --- the box's own surfaces -------------------------------------------------


def _box_manifest(
    config: Config,
    registry: RouteRegistry,
    evidence: EvidenceDocument,
    guard: WakeLockGuard,
    host: str,
    evidence_path: str | None,
    admin_cmd: str,
) -> dict:
    synthetic = _synthetic_route_names(registry)
    claims = [_freeze_claim(evidence, guard, admin_cmd), _log_grammar_claim(admin_cmd)]
    claims += [
        _delivery_sink_claim(
            route,
            evidence.delivery_sinks.get(route),
            evidence_path,
            synthetic=route in synthetic,
        )
        for route in sorted(config.enabled_routes)
    ]
    return _manifest(f"box:{host}", claims)


def _synthetic_route_names(registry: RouteRegistry) -> frozenset[str]:
    """The registered routes that carry manufactured traffic, read off the registry.

    Asked of the routes rather than spelled here, for the same reason
    ``recipient_kind`` is: a hard-coded ``{"probe"}`` would make adding a second
    synthetic source edit this file, and the core/routes split is the property the
    whole daemon is shaped around. A route the registry does not hold is not synthetic
    *as far as the emitter is concerned* — which is safe, because everything the
    emitter tags this way is drawn from ``enabled_routes``, and an enabled route is by
    construction registered (:func:`~basecradle_router.app.build_registry`).
    """
    return frozenset(route.name for route in registry.routes() if route.synthetic)


def _freeze_claim(evidence: EvidenceDocument, guard: WakeLockGuard, admin_cmd: str) -> dict:
    """The freeze surface is readable — a ``probe`` claim, because it must be *run*.

    The only claim here whose ``prove`` is a command rather than a pointer: a wake
    edge is proven by a wake that already happened, but nothing in the router's
    history proves the wake-lock directory is readable *now*. The evidence field
    still carries the last self-test (boot's, or the NOC's own previous run) so the
    ledger has an age-of-proof between exercises.
    """
    last = evidence.freeze_selftest
    proven = last.status is not None and last.at is not None
    return {
        "claim": "freeze-surface:readable",
        "class": "rare",
        "prove": {"kind": "probe", "cmd": f"{admin_cmd} selftest freeze --json"},
        "evidence": f"freeze self-test status={last.status} at {last.at}" if proven else None,
        "ttl_hours": FREEZE_TTL_HOURS,
        "detail": {
            "lock_dir": guard.lock_dir,
            "last_selftest": {
                "status": last.status,
                "at": last.at,
                "detail": last.detail,
            },
            # Read from the probe itself rather than restated, so the manifest cannot
            # describe a contract the probe does not implement. ``config_error`` shares
            # the unprovable sentinel by contract — one state, *no answer* — and is
            # named here anyway because it is a different finding, told apart on stderr.
            "exit_codes": {**FREEZE_EXIT_CODES, "config_error": EXIT_UNPROVABLE},
        },
    }


def _log_grammar_claim(admin_cmd: str) -> dict:
    """The breaker-trip line is still the line the fleet's alarm matches on.

    A ``probe`` claim, and it has to be: the NOC's ``breaker_tripped`` column is a
    **needle** — every clause in its pattern is a whole line that exists only when a
    breaker trips — so nothing arrives on a healthy fleet for its extraction guard to
    watch, and a rename would take the *Circuit Breaker Tripped* alarm silently to zero
    (basecradle-noc#509, basecradle-router#232). Observation cannot close that; only an
    exercise can, so the router fires one.

    ``class: rare`` is the class by its own definition — silence is the normal state and
    the proof is a forced exercise. ``evidence`` names the journal rather than a
    resolvable pointer: this is where the last success is recorded, and it is deliberately
    **not** a ``<path>#<field>`` pointer, because a ``probe`` claim is proven by running,
    never by re-reading (re-reading a pointer is the one thing that cannot move a needle).

    The ``detail`` is read from :mod:`basecradle_router.log_grammar` rather than restated
    here — see :func:`~basecradle_router.log_grammar.manifest_detail`.
    """
    return {
        "claim": f"log-grammar:{LOG_GRAMMAR_LINE_CLASS}",
        "class": "rare",
        "prove": {
            "kind": "probe",
            "cmd": f"{admin_cmd} probe log-grammar {LOG_GRAMMAR_LINE_CLASS} --json",
        },
        "evidence": f"journal:{LOG_GRAMMAR_IDENTIFIER}",
        "ttl_hours": LOG_GRAMMAR_TTL_HOURS,
        "detail": log_grammar_detail(),
    }


def _delivery_sink_claim(
    route: str,
    sink: DeliverySinkEvidence | None,
    evidence_path: str | None,
    *,
    synthetic: bool,
) -> dict:
    """This route's webhook sink has accepted a real delivery — instance 5's claim.

    ``accepted`` counts signature verification passing, not wakes: that is the
    event which proves the shared secret on this box matches the one at the source.
    A sink with rejections and no accepts is a mismatched secret; one with neither
    has simply never been used, and the ledger must be able to tell them apart.

    ``detail.synthetic`` says whether this sink carries manufactured traffic. The
    probe route has a sink of its own — it is injected as a genuine signed delivery,
    so it verifies and counts like any other — and the flag is what stops the ledger
    from reading the fleet probing itself as evidence that an *external* integration
    is armed. It is a distinct and useful row all the same: an injection point whose
    accepts stop while its rejections climb is a rotated probe secret, told apart
    from a dead prober exactly as any other sink is.
    """
    sink = sink or DeliverySinkEvidence()
    return {
        "claim": f"delivery-sink:{route}",
        "class": "rare",
        "prove": {
            "kind": "evidence",
            "source": _pointer(evidence_path, f"delivery_sinks.{route}", "last_accepted_at"),
        },
        "evidence": (
            f"{sink.accepted} delivery(s) verified, last at {sink.last_accepted_at}"
            + (" (synthetic — the router's own probe injection point)" if synthetic else "")
            if sink.last_accepted_at
            else None
        ),
        "ttl_hours": DELIVERY_SINK_TTL_HOURS,
        "detail": {"route": route, "synthetic": synthetic, **_sink_scalars(sink)},
    }


def _sink_scalars(sink: DeliverySinkEvidence) -> dict:
    """``delivery_sinks.<route>`` projected flat — the object the pointer walks into.

    Written out field by field rather than ``asdict``-ed so the manifest's shape is a
    decision made here and not a shadow of a dataclass's field order: this is a wire
    contract a monitor on another box parses, and it must change only when someone
    means to change it.
    """
    return {
        "accepted": sink.accepted,
        "rejected": sink.rejected,
        "woke": sink.woke,
        "ignored": sink.ignored,
        "last_accepted_at": sink.last_accepted_at,
        "last_rejected_at": sink.last_rejected_at,
        "last_reject_reason": sink.last_reject_reason,
    }


# --- one agent's wake edges -------------------------------------------------


def _agent_manifest(
    agent: Agent,
    config: Config,
    registry: RouteRegistry,
    evidence: EvidenceDocument,
    guard: WakeLockGuard,
    evidence_path: str | None,
) -> dict:
    """This agent's claims: the agent-wide wake edge, then one row per armed route.

    Two granularities, because they answer different questions and each greens the
    other's blind spot. The agent-wide row is instance 4's — *is there any way to
    reach this agent at all, and did anything ever?* The per-route rows are instance
    5's, per recipient — *can **this** source reach **this** agent?* A single wake by
    a healthy route satisfies the first while the second stays honestly unproven,
    which is the whole reason the NOC declined to arm its per-recipient rows on the
    agent-wide scalars (basecradle-noc#408).

    One row per armed edge, in the order :func:`_edges` already puts them — route name,
    the same rule as the box's sinks — so the two views of the same set never read in
    different orders.

    Then, last, one row per armed **synthetic** exerciser (:func:`_synthetic_claim`).
    Those are deliberately not part of the two views above: a probe is not a way the
    world reaches this agent, it is the fleet reaching it on purpose, and counting it
    as an edge would green the parked builder the first row exists to expose.
    """
    wake = evidence.agent_wakes.get(agent.harness_key) or AgentWakeEvidence()
    edges = _edges(agent, config, registry, wake)
    claims = [_wake_edge_claim(agent, wake, edges, guard, evidence_path)]
    claims += [
        _route_wake_edge_claim(agent, wake, edge, evidence_path)
        for edge in edges
        if edge["kind"] == "webhook-route"
    ]
    claims += [
        _synthetic_claim(agent, wake, route, evidence_path)
        for route in _armed_routes(agent, config, registry, synthetic=True)
    ]
    return _manifest(f"agent:{agent.harness_key}", claims)


def _wake_edge_claim(
    agent: Agent,
    wake: AgentWakeEvidence,
    edges: list[dict],
    guard: WakeLockGuard,
    evidence_path: str | None,
) -> dict:
    """Something can wake this agent, and here is the last time something did.

    The claim the parked-builder gap (instance 4) needs, and it needs *both* halves:
    ``detail.edges`` is what could wake the agent from now on, ``evidence`` is what
    demonstrably did. Either alone lies — an agent woken last week may have lost its
    only edge since, and a freshly-armed edge has no history yet.

    ``detail`` carries the projection of ``agent_wakes.<harness_key>`` **flat**, which
    is what makes the pointer resolvable at all (see the module docstring): the
    descriptive keys sit *beside* the evidence fields, never wrapped around them.
    """
    return {
        "claim": "wake-edge:webhook-route",
        "class": "rare",
        "prove": {
            "kind": "evidence",
            "source": _pointer(evidence_path, f"agent_wakes.{agent.harness_key}", "last_ok_at"),
        },
        "evidence": (
            f"stage=wake outcome=ok at {wake.last_ok_at} delivery={wake.last_ok_delivery}"
            + (f" route={wake.last_ok_route}" if wake.last_ok_route else "")
            if wake.last_ok_at
            else None
        ),
        "ttl_hours": WAKE_EDGE_TTL_HOURS,
        "detail": {
            "registry_key": agent.key,
            "harness_key": agent.harness_key,
            "os_user": agent.os_user,
            "edges": edges,
            "edge_count": len(edges),
            "wake_lock": _wake_lock_detail(agent, guard),
            **_wake_scalars(wake),
        },
    }


def _route_wake_edge_claim(
    agent: Agent, wake: AgentWakeEvidence, edge: dict, evidence_path: str | None
) -> dict:
    """One armed ``(agent, route)`` edge — the per-recipient row instance 5 is asked at.

    Emitted only for routes that are armed **now**, because a claim states a capability
    the router currently has: a route the box no longer enables cannot wake anyone, and
    a row asserting otherwise would be the paper-armed integration this program exists
    to catch, one level up. The historical record of a since-disabled route is not lost
    — it stays in the agent-wide claim's ``detail.by_route``.

    A never-fired armed edge resolves to ``null``, which the NOC reads as FAIL, not as
    a miss: *the emitter publishes this field and nothing has ever landed in it*. That
    is the correct and intended reading — never-proven is a state the ledger exists to
    show, and an armed edge that has never once delivered is exactly what an integration
    armed on paper looks like from here.
    """
    route = edge["source"]
    proof = wake.by_route.get(route)
    return {
        "claim": f"wake-edge:webhook-route:{route}",
        "class": "rare",
        "prove": {
            "kind": "evidence",
            "source": _pointer(
                evidence_path, f"agent_wakes.{agent.harness_key}.by_route.{route}", "last_ok_at"
            ),
        },
        "evidence": (
            f"stage=wake outcome=ok at {proof.last_ok_at} delivery={proof.last_ok_delivery} "
            f"route={route}"
            if proof and proof.last_ok_at
            else None
        ),
        "ttl_hours": WAKE_EDGE_TTL_HOURS,
        "detail": {
            "harness_key": agent.harness_key,
            "route": route,
            "resolves_by": edge["resolves_by"],
            **_route_scalars(proof),
        },
    }


def _wake_scalars(wake: AgentWakeEvidence) -> dict:
    """``agent_wakes.<harness_key>`` projected flat — the object the pointer walks into.

    ``refused`` stays apart from ``failed`` for the reason the evidence store keeps them
    apart: a refusal is the converge lock or the breaker working, and an agent whose whole
    history is refusals is *gated*, not unreachable. ``deduped`` stays apart from
    ``refused`` for a sharper reason still (basecradle-router#218): a collapsed duplicate
    is only reachable *through* a successful wake, so publishing it as a refusal made the
    newest recorded attempt on a demonstrably healthy route read as a rejection. Both
    counters ship, so a consumer classifies by the field it reads and never by parsing our
    reason strings.

    ``by_route`` is the complete per-``(agent, route)`` record, including routes that are
    no longer armed — ``edges`` and the per-route claims carry only the armed ones, and a
    route that used to work and has since been disabled is precisely the parked-builder
    shape, worth keeping visible.

    Each ``last_*`` trio carries its own ``route`` and ``synthetic`` flag, so this row
    answers *what kind of traffic last proved this edge* without the reader having to
    know which route names are the fleet's own probes. A ``last_ok_synthetic: true``
    beside ``edge_count: 0`` is not a contradiction — it is the exact reading a parked
    builder should produce: the terminus answers, and nothing in the world will ever
    address it (basecradle-router#208).
    """
    return {
        "ok": wake.ok,
        "failed": wake.failed,
        "refused": wake.refused,
        "deduped": wake.deduped,
        "queued": wake.queued,
        "last_ok_at": wake.last_ok_at,
        "last_ok_delivery": wake.last_ok_delivery,
        "last_ok_route": wake.last_ok_route,
        "last_ok_synthetic": wake.last_ok_synthetic,
        "last_failed_at": wake.last_failed_at,
        "last_failed_reason": wake.last_failed_reason,
        "last_failed_route": wake.last_failed_route,
        "last_failed_synthetic": wake.last_failed_synthetic,
        "last_refused_at": wake.last_refused_at,
        "last_refused_reason": wake.last_refused_reason,
        "last_refused_route": wake.last_refused_route,
        "last_refused_synthetic": wake.last_refused_synthetic,
        "last_deduped_at": wake.last_deduped_at,
        "last_deduped_route": wake.last_deduped_route,
        "last_deduped_synthetic": wake.last_deduped_synthetic,
        "by_route": {route: _route_scalars(p) for route, p in sorted(wake.by_route.items())},
    }


def _route_scalars(proof: RouteWakeEvidence | None) -> dict:
    """``agent_wakes.<harness_key>.by_route.<route>`` projected flat — ``None`` when absent.

    Emitted as explicit nulls rather than omitted keys so an armed-but-never-proven edge
    and an armed-and-proven one have the same shape: a consumer reads a value instead of
    testing for a key's absence, and the NOC's resolver distinguishes *the field is not
    there* (unprovable — refused) from *the field is null* (FAIL — never demonstrated),
    which are very different findings.

    One function, used by the per-route claim's ``detail``, the synthetic claim's, and
    the agent-wide claim's ``edges``/``by_route`` views, so the several places this row
    surfaces cannot disagree about the same wake.

    It carries all four outcomes, not only the successes: once one route is the fleet's
    own probe, a row that reported only ``ok`` would leave "this edge has never been
    exercised" and "every exercise of it was refused because the agent has no probe
    secret armed" looking identical — the same *never tried* vs. *tried and it did not
    work* confusion the per-route granularity was introduced to end.

    **This is the row the dedup misclassification was measured on** (basecradle-router#218).
    It is where a per-recipient claim points, so a consumer asking *what did this route
    last do for this agent?* reads it and nothing else — and with a collapsed duplicate
    filed under ``refused``, ``ok=4 failed=0 refused=2`` with the refusal 2.6 ms after the
    success was a route that had rejected nothing at all. ``deduped``/``last_deduped_at``
    carry it now, so ``last_refused_at`` moves only for a wake a gate actually stopped.
    """
    return {
        "ok": proof.ok if proof else 0,
        "failed": proof.failed if proof else 0,
        "refused": proof.refused if proof else 0,
        "deduped": proof.deduped if proof else 0,
        "last_ok_at": proof.last_ok_at if proof else None,
        "last_ok_delivery": proof.last_ok_delivery if proof else None,
        "last_failed_at": proof.last_failed_at if proof else None,
        "last_failed_reason": proof.last_failed_reason if proof else None,
        "last_refused_at": proof.last_refused_at if proof else None,
        "last_refused_reason": proof.last_refused_reason if proof else None,
        "last_deduped_at": proof.last_deduped_at if proof else None,
    }


def _armed_routes(
    agent: Agent, config: Config, registry: RouteRegistry, *, synthetic: bool
) -> tuple[Route, ...]:
    """The enabled, registered routes that can actually reach ``agent`` right now.

    Three conditions that each fail independently — the route is registered, its
    source is enabled on this box, and the agent is resolvable by the
    ``recipient_kind`` it delivers by — which is why the check is structural rather
    than "the agent is in the registry".

    ``synthetic`` selects *which* population is being asked about, and the split is the
    whole point: the real routes are the agent's production wake edges, and the
    synthetic ones are levers the fleet can pull at that same terminus. Mixing them
    would let a probe count as an edge, which would green a parked builder — the exact
    reading (basecradle/basecradle#460, instance 4) the wake-edge claim exists to make
    impossible. Name-ordered, like every other enumeration here, so a manifest built
    twice from unchanged inputs is byte-identical.
    """
    resolvable = config.resolvable_by(agent)
    return tuple(
        route
        for route in registry.routes()
        if route.synthetic is synthetic
        and route.name in config.enabled_routes
        and route.recipient_kind in resolvable
    )


def _synthetic_claim(
    agent: Agent, wake: AgentWakeEvidence, route: Route, evidence_path: str | None
) -> dict:
    """The router's own probe can wake this agent — the lever an ``evidence`` claim lacks.

    Every other claim here is proven by something that *happened*: a delivery arrived, a
    wake fired. For a deliberately quiet agent nothing ever does, and exercising an
    ``evidence``-kind claim only re-reads the pointer — it cannot cause a wake. So a
    healthy edge and a dead one are indistinguishable from outside, and the only
    remedies left are social ones ("go message @pinky") — which is what shared law now
    forbids: *a monitor never depends on a consent or trust surface*
    (``constitution.md`` → Operational Baselines). This claim is the replacement lever
    (`basecradle-noc#421`, basecradle-router#208).

    **The probe's own verdict is deliberately not what proves it.** ``prove.kind`` is
    ``evidence``, not ``probe``, and the pointer is the same ``last_ok_at`` a real wake
    writes — so this row goes green only when the router itself recorded a successful
    wake, which is the one fact the router can honestly state and the probe process
    cannot fake by exiting zero.

    ``detail.proves`` and ``detail.stops_before`` state the boundary plainly, because
    the NOC decides what its ledger accepts as proof for which claim and must decide
    knowing it: the probe traverses everything up to and including the privilege drop
    into the agent's own context and the match of that agent's own probe secret, and
    stops one step short of ``exec``-ing the model — the single step the fleet's
    zero-token-at-rest constraint forbids. What it does not prove is that the model
    binary *runs*; a wake that fails there is a failure that *happens*, which ordinary
    telemetry already catches (``failed`` climbs, with the reason).
    """
    proof = wake.by_route.get(route.name)
    return {
        "claim": f"wake-edge:synthetic:{route.name}",
        "class": "rare",
        "prove": {
            "kind": "evidence",
            "source": _pointer(
                evidence_path,
                f"agent_wakes.{agent.harness_key}.by_route.{route.name}",
                "last_ok_at",
            ),
        },
        "evidence": (
            f"synthetic wake acked at {proof.last_ok_at} delivery={proof.last_ok_delivery} "
            f"route={route.name}"
            if proof and proof.last_ok_at
            else None
        ),
        "ttl_hours": WAKE_EDGE_TTL_HOURS,
        "detail": {
            "harness_key": agent.harness_key,
            "route": route.name,
            "resolves_by": route.recipient_kind,
            "synthetic": True,
            "proves": list(_PROBE_PROVES),
            "stops_before": f"exec {_would_exec(agent)}",
            **_route_scalars(proof),
        },
    }


#: The stages a synthetic wake demonstrably traverses, in order — the honest inventory
#: behind ``wake-edge:synthetic:<route>``. Every one of them is a real step of a real
#: delivery, not a probe-only path: the probe is injected at the daemon's own front door
#: precisely so that none of this is simulated.
_PROBE_PROVES = (
    "http_accept",
    "signature_verify",
    "normalize",
    "resolve",
    "agent_lock",
    "delivery_dedup",
    "noc_wake_lock",
    "wake_rate_breaker",
    "sudo_wake_runner",
    "registry_pin",
    "privilege_drop",
    "agent_env_loaded",
    "clone_cwd",
    "probe_secret_match",
)


def _would_exec(agent: Agent) -> str:
    """The binary a *real* wake would exec for this agent — what the probe stops before.

    Named from the same registry field the wake-runner pins the launch against, so the
    manifest cannot describe a boundary the wrapper does not actually draw.
    """
    return agent.wake_bin if agent.wake_kind is WakeKind.HARNESS else "claude"


def _edges(
    agent: Agent, config: Config, registry: RouteRegistry, wake: AgentWakeEvidence
) -> list[dict]:
    """Every path by which this agent could be woken from now on, and what each proved.

    A **webhook-route** edge exists for each registered, enabled route the agent is
    resolvable by (:func:`_armed_routes`) that carries **real** traffic. A
    **queued-wake** edge exists while the scheduler holds pending work for the agent:
    transient, but an edge, and the one that says a currently-silent agent is about to
    run.

    A **synthetic** route is deliberately not an edge. Nothing in the world will wake an
    agent through the router's own probe; only the fleet will, on purpose, to ask
    whether the terminus answers. Counting it would put ``edge_count: 1`` on a builder
    that no event can reach and quietly retire the parked-builder finding — the
    instrument defeating itself. Its proof is emitted as its own claim instead
    (:func:`_synthetic_claim`).

    Each webhook-route edge carries **its own** last successful wake, not the agent's,
    and is also emitted as a claim of its own (:func:`_route_wake_edge_claim`) so the
    ledger can arm it per recipient. The list is the human-readable half — one row that
    says what could wake this agent and what each path has proven — in the route-name
    order :meth:`~basecradle_router.routes.registry.RouteRegistry.routes` already
    enumerates in, so a manifest built twice from unchanged inputs is byte-identical
    without that resting on the order somebody happened to call ``register`` in. The
    transient queued-wake edge goes last: it is not a route and has no name to sort by.

    An empty list is the other finding: nothing in existence will wake this agent.
    """
    edges = [
        {
            "kind": "webhook-route",
            "source": route.name,
            "resolves_by": route.recipient_kind,
            **_route_scalars(wake.by_route.get(route.name)),
        }
        for route in _armed_routes(agent, config, registry, synthetic=False)
    ]
    if wake.queued > 0:
        edges.append({"kind": "queued-wake", "pending": wake.queued})
    return edges


def _wake_lock_detail(agent: Agent, guard: WakeLockGuard) -> dict:
    """The agent's freeze state right now — read, not assumed.

    Reported on the wake-edge claim rather than as a claim of its own: a held lock
    does not remove the edge, it suspends it, and a ledger row that flipped to
    never-proven every time the NOC converged an agent would be worse than useless.
    Uses :meth:`~basecradle_router.wakelock.WakeLockGuard.inspect` so reading the
    state emits no ``event=wake_refused`` line for a wake nobody attempted.
    """
    decision = guard.inspect(agent.harness_key)
    return {
        "state": decision.state.value,
        "path": guard.path_for(agent.harness_key),
        "expires_at": decision.expires_at or None,
        "lock_reason": decision.lock_reason or None,
        "would_wake": decision.should_wake,
        "detail": decision.reason or None,
    }


# --- shared shape -----------------------------------------------------------


def _manifest(subject: str, claims: list[dict]) -> dict:
    return {
        "contract": CONTRACT_VERSION,
        "subject": subject,
        "component": COMPONENT,
        "claims": claims,
    }


def _pointer(evidence_path: str | None, container: str, field: str) -> str:
    """A ``<file>#<dotted.container>.<field>`` pointer into the evidence document.

    A path plus the exact field, so the ledger reads one value rather than
    re-deriving it — and so a human reading the manifest during an incident can
    ``jq`` straight to the thing the claim says proves it. Falls back to the
    in-memory marker when persistence is disabled, which is itself worth surfacing:
    a box whose evidence is memory-only cannot prove anything across a restart.

    ``container`` and ``field`` are separate parameters rather than one dotted string
    because the NOC resolves the pointer's **last segment** against the claim's own
    ``detail`` (basecradle-noc#409). Splitting them at the call site puts the field the
    caller must also place in ``detail`` in its own argument, where it is hard to write
    a pointer whose tail nothing publishes — the exact miss that made the first cut of
    the wake-edge claim unarmable (basecradle-noc#417, finding 2).
    """
    field_path = f"{container}.{field}"
    if evidence_path is None:
        return f"(in-memory evidence, not persisted)#{field_path}"
    return f"{evidence_path}#{field_path}"


def manifest_filename(manifest: dict) -> str:
    """The per-subject filename for ``--out-dir``, exactly as the contract pins it.

    ``basecradle-router.json`` for the box subject — the host is deliberately *not*
    in the name: one box gets one box-manifest per component, and a second spelling
    of a fact the body already carries is a thing that can later disagree with it.
    Agent subjects get ``basecradle-router@<slug>.json``, where ``<slug>`` is the
    agent's OS user.

    This is a **constraint, not taste** (basecradle-noc#408, ruling 1): the NOC's
    ``run-claim-probe`` resolves ``$CLAIMS_DIR/<component>@<os_user>.json`` before it
    will run anything, so a file spelled any other way is a claim that can never be
    proven. Non-filename characters in a slug collapse to ``-`` so the name stays
    safe on any filesystem; an unrecognised subject kind raises rather than falling
    back, because the plausible fallback (the bare component name) would silently
    overwrite the box manifest.
    """
    subject = manifest.get("subject", "")
    kind, _, name = subject.partition(":")
    if kind == "agent" and name:
        return f"{COMPONENT}@{_UNSAFE_IN_FILENAME.sub('-', name)}.json"
    if kind == "box" and name:
        return f"{COMPONENT}.json"
    raise ValueError(f"no contract filename for claim subject {subject!r}")


__all__ = [
    "COMPONENT",
    "CONTRACT_VERSION",
    "build_manifests",
    "manifest_filename",
]

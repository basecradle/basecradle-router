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
  re-wake it** — incident instance 4, in one machine-readable row. Each webhook-route
  edge also carries **its own** last successful wake, so instance 5 can be read per
  *recipient*: an armed edge that has never fired while that route's sink counts
  hundreds of rejections is a broken integration for *this* agent, which the
  agent-wide scalars cannot say (basecradle-noc#408).
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

**Cadence classes.** All three are ``rare``: the normal state of a wake edge, a
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
from basecradle_router.evidence import AgentWakeEvidence, DeliverySinkEvidence, EvidenceDocument
from basecradle_router.models import Agent
from basecradle_router.routes import RouteRegistry
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
        _box_manifest(config, evidence, guard, host, evidence_path, admin_cmd),
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
    evidence: EvidenceDocument,
    guard: WakeLockGuard,
    host: str,
    evidence_path: str | None,
    admin_cmd: str,
) -> dict:
    claims = [_freeze_claim(evidence, guard, admin_cmd)]
    claims += [
        _delivery_sink_claim(route, evidence.delivery_sinks.get(route), evidence_path)
        for route in sorted(config.enabled_routes)
    ]
    return _manifest(f"box:{host}", claims)


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


def _delivery_sink_claim(
    route: str, sink: DeliverySinkEvidence | None, evidence_path: str | None
) -> dict:
    """This route's webhook sink has accepted a real delivery — instance 5's claim.

    ``accepted`` counts signature verification passing, not wakes: that is the
    event which proves the shared secret on this box matches the one at the source.
    A sink with rejections and no accepts is a mismatched secret; one with neither
    has simply never been used, and the ledger must be able to tell them apart.
    """
    sink = sink or DeliverySinkEvidence()
    return {
        "claim": f"delivery-sink:{route}",
        "class": "rare",
        "prove": {
            "kind": "evidence",
            "source": _pointer(evidence_path, f"delivery_sinks.{route}.last_accepted_at"),
        },
        "evidence": (
            f"{sink.accepted} delivery(s) verified, last at {sink.last_accepted_at}"
            if sink.last_accepted_at
            else None
        ),
        "ttl_hours": DELIVERY_SINK_TTL_HOURS,
        "detail": {
            "route": route,
            "accepted": sink.accepted,
            "rejected": sink.rejected,
            "woke": sink.woke,
            "ignored": sink.ignored,
            "last_accepted_at": sink.last_accepted_at,
            "last_rejected_at": sink.last_rejected_at,
            "last_reject_reason": sink.last_reject_reason,
        },
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
    return _manifest(
        f"agent:{agent.harness_key}",
        [_wake_edge_claim(agent, config, registry, evidence, guard, evidence_path)],
    )


def _wake_edge_claim(
    agent: Agent,
    config: Config,
    registry: RouteRegistry,
    evidence: EvidenceDocument,
    guard: WakeLockGuard,
    evidence_path: str | None,
) -> dict:
    """Something can wake this agent, and here is the last time something did.

    The claim the parked-builder gap (instance 4) needs, and it needs *both* halves:
    ``detail.edges`` is what could wake the agent from now on, ``evidence`` is what
    demonstrably did. Either alone lies — an agent woken last week may have lost its
    only edge since, and a freshly-armed edge has no history yet.
    """
    wake = evidence.agent_wakes.get(agent.harness_key) or AgentWakeEvidence()
    edges = _edges(agent, config, registry, wake)
    return {
        "claim": "wake-edge:webhook-route",
        "class": "rare",
        "prove": {
            "kind": "evidence",
            "source": _pointer(evidence_path, f"agent_wakes.{agent.harness_key}.last_ok_at"),
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
            "wakes": {
                "ok": wake.ok,
                "failed": wake.failed,
                "refused": wake.refused,
                "last_ok_at": wake.last_ok_at,
                "last_ok_delivery": wake.last_ok_delivery,
                "last_ok_route": wake.last_ok_route,
                "last_failed_at": wake.last_failed_at,
                "last_failed_reason": wake.last_failed_reason,
                "last_refused_at": wake.last_refused_at,
                "last_refused_reason": wake.last_refused_reason,
                # The complete per-(agent, route) record, including routes that are no
                # longer armed — ``edges`` only carries the ones that are, and a route
                # that used to work and has since been disabled is worth being able to
                # see.
                "by_route": {
                    route: {
                        "ok": proof.ok,
                        "last_ok_at": proof.last_ok_at,
                        "last_ok_delivery": proof.last_ok_delivery,
                    }
                    for route, proof in sorted(wake.by_route.items())
                },
            },
        },
    }


def _edges(
    agent: Agent, config: Config, registry: RouteRegistry, wake: AgentWakeEvidence
) -> list[dict]:
    """Every path by which this agent could be woken from now on, and what each proved.

    A **webhook-route** edge exists for each registered route whose
    ``recipient_kind`` the agent is actually resolvable by *and* whose source is
    enabled — three conditions that can each fail independently, which is why the
    check is structural rather than "the agent is in the registry". A **queued-wake**
    edge exists while the scheduler holds pending work for the agent: transient, but
    an edge, and the one that says a currently-silent agent is about to run.

    Each webhook-route edge carries **its own** last successful wake, not the agent's.
    That is the per-recipient arming gate the ledger's delivery-sink rows are asked
    at: an edge that is armed but whose ``last_ok_at`` is ``null`` while that route's
    sink counts hundreds of rejections is instance 5, for *this* agent, in one row —
    a distinction the agent-wide scalars cannot draw, because a wake delivered by a
    healthy route would green the broken one beside it.

    An empty list is the other finding: nothing in existence will wake this agent.
    """
    resolvable = config.resolvable_by(agent)
    edges = [
        {
            "kind": "webhook-route",
            "source": route.name,
            "resolves_by": route.recipient_kind,
            **_route_proof(wake, route.name),
        }
        for route in registry.routes()
        if route.name in config.enabled_routes and route.recipient_kind in resolvable
    ]
    if wake.queued > 0:
        edges.append({"kind": "queued-wake", "pending": wake.queued})
    return edges


def _route_proof(wake: AgentWakeEvidence, route: str) -> dict:
    """This (agent, route) pair's last successful wake — ``None`` when it has none.

    Emitted as explicit nulls rather than omitted keys so an armed-but-never-proven
    edge and an armed-and-proven one have the same shape, and a consumer reads a
    value instead of testing for a key's absence.
    """
    proof = wake.by_route.get(route)
    return {
        "last_ok_at": proof.last_ok_at if proof else None,
        "last_ok_delivery": proof.last_ok_delivery if proof else None,
    }


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


def _pointer(evidence_path: str | None, field_path: str) -> str:
    """A ``<file>#<dotted.field>`` pointer into the evidence document.

    A path plus the exact field, so the ledger reads one value rather than
    re-deriving it — and so a human reading the manifest during an incident can
    ``jq`` straight to the thing the claim says proves it. Falls back to the
    in-memory marker when persistence is disabled, which is itself worth surfacing:
    a box whose evidence is memory-only cannot prove anything across a restart.
    """
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

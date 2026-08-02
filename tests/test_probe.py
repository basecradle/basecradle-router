"""The router-side synthetic wake — proving the wake edge without a trust surface.

basecradle-router#208, from `basecradle-noc#421`. The NOC's retired probe posted a
signed marker into each agent's own BaseCradle timeline, which required mutual trust
with every persona — a consent surface a monitor is now forbidden to depend on
(``constitution.md`` → Operational Baselines). The router owns the router→agent wake
edge, so the router proves it: a signed test delivery fired at its own real
verify→wake path, on-box, with no platform account and no relationship with anyone.

What these tests pin, in the order the requirements were handed down:

1. **It exercises the real path.** A probe is verified, normalized, resolved, locked,
   deduped, freeze-checked and breaker-checked exactly as production traffic is.
2. **Token-free, or it does not fire.** No code path can build a model command for a
   synthetic event, and an agent with no probe secret armed is refused — never woken.
3. **A synthetic is distinguishable from real traffic** in every counter it touches.
4. **One rule, every agent** — builders and harness personas, no special case.
5. **The probe's own PASS is not the proof**: only the *daemon's* record of a wake
   carrying this run's delivery id counts.

Everything is fabricated and offline: the cast is John Doe (``john``) and Nova Digital
(``nova``), plus the harness persona ``jt``. No network, no model, no live agent. The
one real subprocess is ``deploy/bin/probe-ack``, which by construction can reach
neither — it imports ``hmac`` and prints a line.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import subprocess
import sys
import uuid
from pathlib import Path
from types import MappingProxyType

import pytest

from basecradle_router.claims import build_manifests
from basecradle_router.config import Config, ConfigError, load_config, load_self_url
from basecradle_router.evidence import EvidenceStore, read_evidence
from basecradle_router.marker import SCHEME, is_marker, nonce_of
from basecradle_router.models import Agent, Event, EventKind, Recipient, WakeKind
from basecradle_router.pipeline import Outcome, Pipeline, PipelineResult, Stage
from basecradle_router.probe import (
    BROKEN,
    PROVEN,
    UNPROVABLE,
    UNPROVABLE_MARKER,
    Injection,
    ProbeError,
    WakeProbe,
)
from basecradle_router.routes import RouteRegistry, SignatureError
from basecradle_router.routes.base import InboundRequest, PayloadError
from basecradle_router.routes.basecradle import BasecradleRoute
from basecradle_router.routes.github import GithubRoute
from basecradle_router.routes.probe import DELIVERY_HEADER, SIGNATURE_HEADER, ProbeRoute
from basecradle_router.selftest import EXIT_UNPROVABLE
from basecradle_router.server import WebhookServer
from basecradle_router.wake import HomeServerWaker, SubprocessWaker, WakeError, WakeResult
from basecradle_router.wakelock import WakeLockGuard

PROBE_ACK = Path(__file__).resolve().parents[1] / "deploy" / "bin" / "probe-ack"
WAKE_RUNNER = Path(__file__).resolve().parents[1] / "deploy" / "bin" / "wake-runner"

PROBE_SECRET = "whsec_" + "1" * 32  # the ROUTE's secret: authorises injection
NOVA_AGENT_SECRET = "noc-probe-secret-for-nova-fabricated"  # the AGENT's own secret
JT_AGENT_SECRET = "noc-probe-secret-for-jt-fabricated"

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
    recipient_uuid="0192f3a4-5b6c-7d8e-9f01-aaaaaaaaaaaa",
    wake_bin="/home/jt/.venv/bin/basecradle-harness-wake",
)


def mint(nonce: str, secret: str) -> str:
    """A correctly-signed marker — the verifying mirror of basecradle-noc's ``marker.mint``.

    Re-implemented here rather than imported for the same reason the harness and the
    wrapper re-implement it: repo sovereignty forbids reaching across, and the halves are
    pinned to agree byte-for-byte. A test that imported the sender's own minting could
    not catch the two drifting apart.
    """
    sig = hmac.new(secret.encode(), f"{SCHEME} {nonce}".encode(), hashlib.sha256).hexdigest()
    return f"{SCHEME} {nonce} {sig}"


def a_marker(secret: str = NOVA_AGENT_SECRET) -> str:
    return mint(uuid.uuid4().hex, secret)


def config(*, agents=(NOVA, JT), routes=("github", "basecradle", "probe")) -> Config:
    by_key = {a.key: a for a in agents}
    return Config(
        agents=MappingProxyType(by_key),
        enabled_routes=frozenset(routes),
        webhook_secrets=MappingProxyType({r: PROBE_SECRET for r in routes}),
        recipient_index=MappingProxyType({a.recipient_uuid: a for a in agents if a.recipient_uuid}),
        harness_index=MappingProxyType({a.harness_key: a for a in agents}),
    )


def registry(routes=("github", "basecradle", "probe")) -> RouteRegistry:
    reg = RouteRegistry()
    if "github" in routes:
        reg.register(GithubRoute(frozenset({"john"})))
    if "basecradle" in routes:
        reg.register(BasecradleRoute())
    if "probe" in routes:
        reg.register(ProbeRoute())
    return reg


def probe_request(
    harness_key: str, marker: str, *, delivery: str = "probe-abc", secret=PROBE_SECRET
):
    body = json.dumps(
        {"harness_key": harness_key, "marker": marker}, separators=(",", ":"), sort_keys=True
    ).encode()
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return InboundRequest(
        headers={
            SIGNATURE_HEADER: f"sha256={digest}",
            DELIVERY_HEADER: delivery,
            "X-BaseCradle-Probe-Event": "probe.wake",
        },
        body=body,
    )


# --- 1. the core vocabulary: what makes an event synthetic -------------------


def test_only_a_probe_event_is_synthetic() -> None:
    # The flag is derived from the event's own kind, never set beside it, so nothing can
    # declare itself real while carrying a probe (or the reverse).
    def event(kind: EventKind) -> Event:
        return Event(
            source="x",
            kind=kind,
            recipient=Recipient(by="harness_key", value="nova"),
            wake_arg="arg",
            delivery_id="d1",
        )

    assert event(EventKind.SYNTHETIC_PROBE).synthetic is True
    assert event(EventKind.HANDOFF).synthetic is False
    assert event(EventKind.PLATFORM_EVENT).synthetic is False


def test_a_routes_declared_provenance_matches_the_events_it_normalizes() -> None:
    # The weld between the two halves of the same fact: the registry-level declaration
    # (`Route.synthetic`, which the claims emitter reads to keep probes out of the edge
    # count) and the event-level one (`Event.synthetic`, which the evidence store
    # records). They are separate mechanisms, so this pins that they agree.
    marker = a_marker()
    probe_event = ProbeRoute().normalize(probe_request("nova", marker))
    assert ProbeRoute.synthetic is True
    assert probe_event.synthetic is True

    assert GithubRoute.synthetic is False
    assert BasecradleRoute.synthetic is False


def test_a_route_that_declares_no_provenance_is_rejected_by_the_registry() -> None:
    # There is deliberately no default. A source that forgot to say whether it carries
    # real traffic would otherwise be counted as real by omission, which is the exact
    # direction the mistake must not fall.
    class Forgetful:
        name = "forgetful"
        recipient_kind = "repo"

        def verify(self, request, secret) -> None: ...

        def normalize(self, request):
            return None

    with pytest.raises(TypeError):
        RouteRegistry().register(Forgetful())


# --- 2. the route: a probe is a real, signed, verified delivery --------------


def test_the_probe_route_verifies_like_every_other_source() -> None:
    route = ProbeRoute()
    marker = a_marker()
    route.verify(probe_request("nova", marker), PROBE_SECRET)  # the real HMAC boundary

    with pytest.raises(SignatureError):
        route.verify(probe_request("nova", marker, secret="the-wrong-secret"), PROBE_SECRET)


def test_a_verified_probe_normalizes_to_a_synthetic_event_addressed_by_harness_key() -> None:
    marker = a_marker()
    event = ProbeRoute().normalize(probe_request("nova", marker, delivery="probe-42"))

    assert event.source == "probe"
    assert event.kind is EventKind.SYNTHETIC_PROBE
    # Addressed by the OS-user slug — the identity every agent has, builder or persona.
    assert event.recipient == Recipient(by="harness_key", value="nova")
    assert event.wake_arg == marker  # the marker IS the wake argument
    assert event.delivery_id == "probe-42"
    assert event.synthetic is True


def test_a_malformed_marker_is_refused_at_the_router_not_carried() -> None:
    # The marker becomes an argv element crossing sudo, so its shape is a security
    # boundary — the first of three independent checks (route, waker, wrapper).
    for bad in (
        "BCNOC1 nonce",  # no hmac
        "BCNOC1 nonce " + "0" * 63,  # short hmac
        "BCNOC1 nonce " + "g" * 64,  # not hex
        "prefix BCNOC1 nonce " + "0" * 64,  # anchored: no surrounding context
        "BCNOC1 nonce " + "0" * 64 + " ; rm -rf /",
        "BCNOC2 nonce " + "0" * 64,  # a different scheme version
    ):
        with pytest.raises(PayloadError):
            ProbeRoute().normalize(probe_request("nova", bad))


def test_a_probe_is_never_a_silent_ignore() -> None:
    # Every other route has an ignore path because its source's catalogue is wider than
    # the router's interest. This route's traffic is manufactured by the fleet for one
    # purpose, so anything else is a defect — and a monitor that fails to monitor and
    # says nothing is the shape the whole program exists to kill.
    marker = a_marker()
    request = probe_request("nova", marker)
    unknown = InboundRequest(
        headers={**dict(request.headers), "X-BaseCradle-Probe-Event": "probe.something-else"},
        body=request.body,
    )
    with pytest.raises(PayloadError, match="unknown probe event"):
        ProbeRoute().normalize(unknown)

    missing_delivery = InboundRequest(
        headers={k: v for k, v in request.headers.items() if k != DELIVERY_HEADER},
        body=request.body,
    )
    with pytest.raises(PayloadError, match=DELIVERY_HEADER):
        ProbeRoute().normalize(missing_delivery)


def test_every_registered_agent_is_reachable_by_the_probe_builder_and_persona_alike() -> None:
    # Requirement 4: one rule, every agent — including @jt, whose grandfathered trust
    # edge the founder's ruling explicitly declined to preserve.
    cfg = config()
    assert "harness_key" in cfg.resolvable_by(NOVA)
    assert "harness_key" in cfg.resolvable_by(JT)
    assert cfg.agent_for_recipient(Recipient(by="harness_key", value="nova")) is NOVA
    assert cfg.agent_for_recipient(Recipient(by="harness_key", value="jt")) is JT

    with pytest.raises(ConfigError, match="no agent registered for harness_key"):
        cfg.agent_for_recipient(Recipient(by="harness_key", value="nobody"))


# --- 3. token-free by construction, not by promise --------------------------


def test_no_wake_command_exists_for_a_synthetic_event() -> None:
    # `claude -p "<marker>"` IS a model call — the marker would be read as a prompt. So
    # there is no argv the command builder could return that would not start a session,
    # and it returns none. The impossibility is structural, not remembered.
    from basecradle_router.wake import wake_command

    event = ProbeRoute().normalize(probe_request("nova", a_marker()))
    with pytest.raises(WakeError, match="must never launch the agent's model"):
        wake_command(NOVA, event)


def test_the_bare_subprocess_waker_cannot_carry_a_probe_at_all() -> None:
    # The only waker that may carry a probe is the one that goes through the wrapper.
    # The v0/offline waker would have run `claude -p <marker>` directly, so it refuses.
    event = ProbeRoute().normalize(probe_request("nova", a_marker()))
    with pytest.raises(WakeError):
        SubprocessWaker(runner=lambda _i: WakeResult(0)).wake(NOVA, event)


def test_the_home_server_waker_assembles_probe_mode_never_a_command() -> None:
    marker = a_marker()
    event = ProbeRoute().normalize(probe_request("nova", marker, delivery="probe-7"))

    argv = HomeServerWaker(wrapper="/opt/wr").invocation_for(NOVA, event).argv

    assert argv == (
        "sudo",
        "/opt/wr",
        "--user",
        "nova",
        "--cwd",
        "/home/nova/basecradle-python",
        "--delivery",
        "probe-7",
        "--probe",
        marker,
    )
    # No `--`, and therefore no command: the two modes are disjoint in the argv itself.
    assert "--" not in argv
    assert "claude" not in argv


def test_a_real_wake_is_untouched_by_probe_mode_existing() -> None:
    event = Event(
        source="github",
        kind=EventKind.HANDOFF,
        recipient=Recipient(by="repo", value=NOVA.key),
        wake_arg="Cross-repo handoff: work https://example.invalid/1",
        delivery_id="0192f3a4-5b6c-7d8e-9f01-23456789abcd",
    )
    argv = HomeServerWaker(wrapper="/opt/wr").invocation_for(NOVA, event).argv
    assert argv[-4:] == ("--", "claude", "-p", event.wake_arg)
    assert "--probe" not in argv


def test_the_waker_refuses_a_probe_whose_marker_is_malformed() -> None:
    # Defence in depth: the route already refused this, so reaching here means the
    # router's own edge check did not hold. Refuse rather than carry it across sudo.
    event = Event(
        source="probe",
        kind=EventKind.SYNTHETIC_PROBE,
        recipient=Recipient(by="harness_key", value="nova"),
        wake_arg="not-a-marker",
        delivery_id="probe-9",
    )
    with pytest.raises(WakeError, match="malformed BCNOC1 marker"):
        HomeServerWaker(wrapper="/opt/wr").invocation_for(NOVA, event)


# --- 4. the verifier: the ack happens as the agent, or not at all ------------


def run_probe_ack(marker: str, *, secret: str | None, would_exec: str = "/usr/bin/claude"):
    env = {"PATH": "/usr/bin:/bin", "USER": "nova"}
    if secret is not None:
        env["NOC_PROBE_SECRET"] = secret
    return subprocess.run(
        [sys.executable, str(PROBE_ACK), marker, would_exec],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_valid_marker_is_acked_token_free() -> None:
    nonce = uuid.uuid4().hex
    result = run_probe_ack(mint(nonce, NOVA_AGENT_SECRET), secret=NOVA_AGENT_SECRET)

    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == f"BCNOC1-ACK {nonce}"
    # The ack names the boundary it stopped at, so the verdict is self-describing.
    assert "a real wake would have exec'd /usr/bin/claude" in result.stdout


def test_an_unarmed_agent_refuses_and_nothing_is_launched() -> None:
    # The requirement, stated exactly: "An agent without its probe secret armed must be
    # a refusal, never a fallback that wakes the model." 75 is the contract's
    # inconclusive sentinel — we never got an answer — not a claim the edge is dead.
    result = run_probe_ack(a_marker(), secret=None)

    assert result.returncode == EXIT_UNPROVABLE
    assert "no NOC_PROBE_SECRET armed" in result.stderr
    assert "Refusing rather than falling through to a real wake" in result.stderr
    assert "BCNOC1-ACK" not in result.stdout


def test_an_empty_probe_secret_is_the_same_refusal_as_an_absent_one() -> None:
    # An empty value is a half-finished arming, not an arming. It must not verify
    # anything, and it must not be the one input that slips past the gate.
    result = run_probe_ack(a_marker(), secret="")
    assert result.returncode == EXIT_UNPROVABLE
    assert "no NOC_PROBE_SECRET armed" in result.stderr


def test_a_forged_marker_is_proven_broken_never_acked() -> None:
    # Signed, not a bare sentinel: only a holder of this agent's own secret can mint one.
    forged = f"{SCHEME} {uuid.uuid4().hex} {'0' * 64}"
    result = run_probe_ack(forged, secret=NOVA_AGENT_SECRET)

    assert result.returncode == 1
    assert "does not verify" in result.stderr
    assert "BCNOC1-ACK" not in result.stdout


def test_one_agents_marker_does_not_verify_against_anothers_secret() -> None:
    # The per-agent secret is what stops any secret-holder minting a marker that any
    # other agent's harness would ack — the forgery risk the NOC caught in #424, which
    # carries over to this layer unchanged.
    result = run_probe_ack(a_marker(JT_AGENT_SECRET), secret=NOVA_AGENT_SECRET)
    assert result.returncode == 1


def test_the_verifier_refuses_a_marker_it_cannot_parse() -> None:
    assert run_probe_ack("garbage", secret=NOVA_AGENT_SECRET).returncode == 1


# --- 5. the wrapper's probe mode, pinned structurally -----------------------


def wake_runner_source() -> str:
    return WAKE_RUNNER.read_text()


def test_the_wrapper_refuses_a_probe_carrying_a_command() -> None:
    # Asking for a wake and a probe at once has no safe reading: acking would drop a real
    # trigger on the floor, and running the command would burn a token for a monitoring
    # cycle. The modes are mutually exclusive, and the refusal is explicit.
    assert 'die "refusing: --probe takes no command after' in wake_runner_source()


def test_the_wrapper_pins_the_marker_to_the_same_inert_shape_the_router_did() -> None:
    assert r"^BCNOC1\ [A-Za-z0-9_-]{1,128}\ [0-9a-f]{64}$" in wake_runner_source()


def test_probe_mode_still_resolves_the_binary_a_real_wake_would_have_run() -> None:
    # Resolving is most of what there is to prove on this side of the boundary: that the
    # binary is present, executable, and exactly where the ROOT-OWNED registry pins it.
    # Only the exec is skipped.
    source = wake_runner_source()
    assert 'would_exec="$launch_bin"' in source
    assert 'set -- "$PROBE_ACK" "$probe" "$would_exec"' in source


def test_the_verifiers_interpreter_is_resolved_as_root_off_the_trusted_path() -> None:
    # The verifier is exec-ed AFTER agent.env loads, so honouring its shebang would
    # resolve `python3` through whatever PATH that agent's env file sets. A root-owned
    # verifier must not depend on the account it is verifying for its own interpreter.
    source = wake_runner_source()
    assert 'probe_python="$(PATH="$TRUSTED_PATH" command -v python3)"' in source
    assert 'launch_bin="$probe_python"' in source


def test_the_probe_verifier_must_be_executable_or_the_probe_is_unprovable() -> None:
    # A missing verifier is "we never got an answer", not "the wake edge is broken" —
    # and certainly not a reason to fall through to a real wake. The reason carries the
    # contract token both sides agree on, so the router keeps it out of the broken bucket.
    source = wake_runner_source()
    assert 'refuse "$EXIT_UNPROVABLE"' in source
    assert f'"{UNPROVABLE_MARKER} probe verifier is missing or not executable' in source


def test_both_far_side_refusals_carry_the_token_the_router_reads_back() -> None:
    # The contract between the two root-owned files we ship and the router that reads
    # their recorded reason. A refusal whose wording drifted off this token would be
    # reported as a broken wake edge — a page for a configuration step.
    assert UNPROVABLE_MARKER in PROBE_ACK.read_text()
    assert UNPROVABLE_MARKER in wake_runner_source()


def test_the_verifier_path_is_a_constant_the_caller_cannot_choose() -> None:
    # It runs AS the agent (so it can read that agent's secret) but must not be WRITABLE
    # by the agent, or the account under test could rewrite its own verifier to always
    # ack. A monitor whose subject can forge its result is not a monitor.
    #
    # And the path must not be caller-choosable: it is exec-ed after agent.env has
    # loaded, so a caller who could point it anywhere could run a script of its choosing
    # inside the agent's environment and read the very secrets this privilege boundary
    # exists to keep the router away from. sudoers `env_reset` already makes that
    # unreachable; a constant makes it impossible.
    source = wake_runner_source()
    assert "PROBE_ACK=/opt/basecradle-router/bin/probe-ack" in source
    assert "BASECRADLE_ROUTER_PROBE_ACK" not in source


def test_the_verifier_never_imports_anything_that_could_reach_a_model_or_the_network() -> None:
    # Token-free as a property of the program, not a promise about it. Pinned as bytes so
    # a later edit that reaches for a client library fails here rather than on the box.
    imports = {
        line.split()[1]
        for line in PROBE_ACK.read_text().splitlines()
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__")
    }
    assert imports <= {"hmac", "os", "re", "sys", "hashlib"}


# --- 6. the pipeline: a probe travels the real path -------------------------


class FakeBox:
    """A fabricated home server: the real pipeline, with sudo/runuser stood in for.

    The wake boundary is mocked exactly where the existing suite mocks it — ``sudo`` and
    ``runuser`` gate on EUID 0 and a real UID≥1000 login user, so they are not
    offline-testable. Everything on either side of them is real: the argv the router
    assembles, and the shipped ``probe-ack`` verifier the wrapper would exec after the
    drop, run here as a subprocess with the agent's own fabricated secret.
    """

    def __init__(self, path: Path, secrets: dict[str, str], *, routes=("github", "probe")):
        self.evidence_path = str(path / "evidence.json")
        self.secrets = secrets
        self.waker = HomeServerWaker(wrapper="/opt/wr", runner=self._run)
        self.pipeline = Pipeline(
            registry=registry(routes),
            config=config(routes=routes),
            waker=self.waker,
            evidence=EvidenceStore(self.evidence_path),
            wake_lock=WakeLockGuard(lock_dir=str(path / "wake-locks")),
            sleep=lambda _d: None,
        )
        self.server = WebhookServer(self.pipeline)
        self.attempts: list[tuple[str, ...]] = []

    def _run(self, invocation):
        self.attempts.append(invocation.argv)
        argv = invocation.argv
        if "--probe" not in argv:  # a real wake: never actually run one here
            return WakeResult(exit_code=0, stdout="opened PR")
        user = argv[argv.index("--user") + 1]
        marker = argv[argv.index("--probe") + 1]
        completed = run_probe_ack(marker, secret=self.secrets.get(user))
        if completed.returncode != 0:
            raise WakeError(
                f"wake of {user} exited {completed.returncode}: {completed.stderr.strip()}",
                exit_code=completed.returncode,
            )
        return WakeResult(exit_code=0, stdout=completed.stdout)

    def post(self, url: str, body: bytes, headers: dict) -> Injection:
        """Drive the real ASGI app in-process — the injector's transport seam."""
        path = "/" + url.split("//", 1)[-1].split("/", 1)[1]

        async def drive():
            scope = {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": [
                    (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
                ],
            }
            incoming = [{"type": "http.request", "body": body, "more_body": False}]
            sent: list[dict] = []

            async def receive():
                return incoming.pop(0)

            async def send(message):
                sent.append(message)

            await self.server(scope, receive, send)
            await self.server.drain()  # the ack is sent first; let the wake finish
            status = next(m["status"] for m in sent if m["type"] == "http.response.start")
            raw = next(m["body"] for m in sent if m["type"] == "http.response.body")
            return status, raw.decode()

        status, raw = asyncio.run(drive())
        parsed = json.loads(raw)
        return Injection(
            status=status,
            stages=tuple((s, o) for s, o in parsed.get("stages", [])),
            body=raw,
        )

    def probe(self, **kwargs) -> WakeProbe:
        return WakeProbe(
            secret=PROBE_SECRET,
            evidence_path=self.evidence_path,
            self_url="http://127.0.0.1:8000",
            post=self.post,
            read=lambda: read_evidence(self.evidence_path),
            sleep=lambda _d: None,
            **kwargs,
        )

    def evidence(self):
        return read_evidence(self.evidence_path)


@pytest.fixture
def box(tmp_path):
    return FakeBox(tmp_path, {"nova": NOVA_AGENT_SECRET, "jt": JT_AGENT_SECRET})


def test_the_probe_proves_the_edge_end_to_end_and_last_ok_at_moves(box) -> None:
    """The capstone, and the definition of done: fire → the real path runs → proof moves.

    Before: this agent has never been woken, so the ledger's pointer is ``null`` — the
    parked-builder reading. After one probe: the *daemon* has recorded a successful wake,
    which is the fact only the router can honestly state.
    """
    before = box.evidence().agent_wakes.get("nova")
    assert before is None  # never proven

    result = box.probe().run("nova", a_marker(NOVA_AGENT_SECRET))

    assert result.status == PROVEN
    assert result.exit_code == 0
    assert result.before.agent_last_ok_at is None
    assert result.after.agent_last_ok_at is not None

    after = box.evidence().agent_wakes["nova"]
    assert after.ok == 1
    assert after.last_ok_at is not None
    assert after.last_ok_delivery == result.delivery_id
    assert after.last_ok_route == "probe"
    assert after.last_ok_synthetic is True


def test_the_probe_traverses_every_stage_a_genuine_delivery_traverses(box) -> None:
    # Requirement 1: "A synthetic that bypasses verification proves nothing about the
    # edge that matters." The stage record is the router's own account of the trip.
    request = probe_request("nova", a_marker(NOVA_AGENT_SECRET), delivery="probe-real-path")
    result = box.pipeline.handle("probe", request)

    assert result.stages == [
        (Stage.ROUTE, Outcome.OK),
        (Stage.VERIFY, Outcome.OK),
        (Stage.NORMALIZE, Outcome.OK),
        (Stage.RESOLVE, Outcome.OK),
        (Stage.LOCK, Outcome.OK),
        (Stage.WAKE, Outcome.OK),
    ]


def test_a_probe_signed_with_the_wrong_secret_never_reaches_an_agent(box) -> None:
    request = probe_request("nova", a_marker(NOVA_AGENT_SECRET), secret="not-the-route-secret")
    result = box.pipeline.handle("probe", request)

    assert result.stages[-1] == (Stage.VERIFY, Outcome.REJECTED)
    assert box.attempts == []
    assert box.evidence().delivery_sinks["probe"].rejected == 1


def test_the_synthetic_is_distinguishable_from_real_traffic_everywhere_it_lands(box) -> None:
    """Requirement 3, checked in each counter a delivery touches.

    A probe must never masquerade as an accepted production delivery. It cannot: the
    route tag separates the sinks and the per-route proof, and the provenance flag says
    plainly which kind of traffic last proved the edge.
    """
    box.probe().run("nova", a_marker(NOVA_AGENT_SECRET))
    document = box.evidence()

    # Its own sink — the github sink is untouched, so no production integration is
    # greened by the fleet probing itself.
    assert document.delivery_sinks["probe"].accepted == 1
    assert "github" not in document.delivery_sinks

    # Its own per-(agent, route) row — the github row is absent, so the per-recipient
    # question ("can THIS source reach THIS agent?") still reads honestly unproven.
    wake = document.agent_wakes["nova"]
    assert set(wake.by_route) == {"probe"}
    assert wake.by_route["probe"].ok == 1

    # And the agent-wide scalars name the provenance, so a reader never has to know
    # which route names happen to be the fleet's own.
    assert (wake.last_ok_route, wake.last_ok_synthetic) == ("probe", True)


def test_a_real_wake_is_recorded_as_real_beside_a_synthetic_one(box) -> None:
    # The other direction of the same property: after both, each row says which it was.
    box.probe().run("nova", a_marker(NOVA_AGENT_SECRET))
    box.pipeline.evidence.record_wake_ok("nova", "gh-1", route="github", synthetic=False)

    wake = box.evidence().agent_wakes["nova"]
    assert wake.last_ok_route == "github"
    assert wake.last_ok_synthetic is False
    assert wake.by_route["probe"].ok == 1  # the synthetic proof is not overwritten
    assert wake.by_route["github"].ok == 1


def test_a_probe_at_an_unarmed_agent_refuses_and_never_wakes_the_model(tmp_path) -> None:
    """The refusal path, end to end. No secret armed → the wake fails at the verifier.

    The load-bearing assertion is the last one: the argv that reached the boundary was a
    probe, so the model binary was never in it. There is no path from an unarmed agent to
    a real wake — which is what keeps a monitoring cycle from becoming a standing token
    burn at rest.
    """
    unarmed = FakeBox(tmp_path, {})  # nobody has a probe secret

    result = unarmed.probe().run("nova", a_marker(NOVA_AGENT_SECRET))

    # Unprovable, not broken: an agent nobody has armed is a configuration state, not a
    # dead wake edge, and paging for the two alike is how a monitor teaches you to skim.
    assert result.status == UNPROVABLE
    assert result.exit_code == EXIT_UNPROVABLE
    assert "no NOC_PROBE_SECRET armed" in result.detail
    assert unarmed.evidence().agent_wakes["nova"].last_ok_at is None  # nothing proven

    failure = unarmed.evidence().agent_wakes["nova"]
    assert failure.failed == 1
    assert failure.last_failed_route == "probe"
    assert failure.last_failed_synthetic is True
    assert failure.by_route["probe"].failed == 1

    assert len(unarmed.attempts) == 1  # measured once, not retried
    assert "--probe" in unarmed.attempts[0]
    assert "claude" not in unarmed.attempts[0]


def test_a_drifted_probe_secret_is_broken_not_merely_unprovable(tmp_path) -> None:
    # The other side of the same split. The agent IS armed — with a different value than
    # the NOC minted against. That is a definite negative finding about this seam (the
    # exact drift `mint-probe-secret` writes both copies to prevent), so it must not be
    # softened into "we could not ask".
    drifted = FakeBox(tmp_path, {"nova": "a-different-secret-than-the-noc-holds"})

    result = drifted.probe().run("nova", a_marker(NOVA_AGENT_SECRET))

    assert result.status == BROKEN
    assert result.exit_code == 1
    assert "does not verify" in result.detail
    assert drifted.evidence().agent_wakes["nova"].last_ok_at is None


def test_a_missing_verifier_on_the_box_is_unprovable_and_names_the_install(tmp_path) -> None:
    # The wrapper's own refusal, before the drop. Same bucket as an un-armed agent —
    # nothing was asked — and the message carries the exact command that fixes it.
    box = FakeBox(tmp_path, {"nova": NOVA_AGENT_SECRET})

    def missing_verifier(invocation):
        box.attempts.append(invocation.argv)
        raise WakeError(
            "wake of basecradle/basecradle-python exited 75: wake-runner: could not prove: "
            "probe verifier is missing or not executable: /opt/basecradle-router/bin/probe-ack",
            exit_code=EXIT_UNPROVABLE,
        )

    box.pipeline = Pipeline(
        registry=box.pipeline.registry,
        config=box.pipeline.config,
        waker=HomeServerWaker(wrapper="/opt/wr", runner=missing_verifier),
        evidence=box.pipeline.evidence,
        wake_lock=box.pipeline.wake_lock,
        sleep=lambda _d: None,
    )
    box.server = WebhookServer(box.pipeline)

    result = box.probe().run("nova", a_marker(NOVA_AGENT_SECRET))

    assert result.status == UNPROVABLE
    assert "probe verifier is missing" in result.detail


def test_a_probe_is_measured_once_never_retried(tmp_path) -> None:
    # A probe IS the measurement. Retrying would report the best of N rather than the
    # state of the system — and an unarmed agent refuses identically every time.
    unarmed = FakeBox(tmp_path, {})
    unarmed.probe().run("nova", a_marker(NOVA_AGENT_SECRET))
    assert len(unarmed.attempts) == 1

    # A real wake keeps its retries: they exist so a flaky transport does not cost a
    # real unit of work, which is a different question entirely.
    calls = []

    def flaky(_invocation):
        calls.append(1)
        raise WakeError("transient")

    pipeline = Pipeline(
        registry=registry(("github", "probe")),
        config=config(routes=("github", "probe")),
        waker=HomeServerWaker(wrapper="/opt/wr", runner=flaky),
        sleep=lambda _d: None,
    )
    event = Event(
        source="github",
        kind=EventKind.HANDOFF,
        recipient=Recipient(by="repo", value=NOVA.key),
        wake_arg="trigger",
        delivery_id="gh-2",
    )
    pipeline.execute(NOVA, event, PipelineResult())
    assert len(calls) == 3


def test_a_frozen_agent_refuses_the_probe_which_reads_unprovable_not_broken(box, tmp_path) -> None:
    """The freeze interlock applies to probes too, and that is the point.

    A probe fired while the NOC is converging an agent is refused — the interlock
    working. The honest reading is *we never got an answer*, never *the edge is dead*,
    and the refused-vs-failed split the evidence store already draws is what carries it.
    """
    locks = tmp_path / "wake-locks"
    locks.mkdir(exist_ok=True)
    (locks / "nova.lock").write_text(
        json.dumps({"expires_at": "2099-01-01T00:00:00+00:00", "reason": "converge"})
    )

    result = box.probe().run("nova", a_marker(NOVA_AGENT_SECRET))

    assert result.status == UNPROVABLE
    assert result.exit_code == EXIT_UNPROVABLE
    wake = box.evidence().agent_wakes["nova"]
    assert wake.refused == 1
    assert wake.last_refused_route == "probe"
    assert wake.last_refused_synthetic is True
    assert wake.last_ok_at is None
    assert box.attempts == []  # nothing was launched


def test_the_probe_reaches_a_harness_persona_by_exactly_the_same_route(box) -> None:
    # Requirement 4: @jt migrates to this mechanism, with no special case. The only
    # difference is which binary the wrapper would have exec'd.
    result = box.probe().run("jt", a_marker(JT_AGENT_SECRET))

    assert result.status == PROVEN
    assert box.evidence().agent_wakes["jt"].last_ok_synthetic is True
    argv = box.attempts[0]
    assert argv[:6] == ("sudo", "/opt/wr", "--user", "jt", "--cwd", "/home/jt/harness")
    assert "--probe" in argv


# --- 7. the injector's verdict is the daemon's record, never its own ---------


def test_the_probe_reports_proven_only_for_its_own_delivery(tmp_path) -> None:
    """Requirement 5: the probe's own PASS is not the proof.

    A wake for the same agent landing in the same window must not be accepted as this
    probe's proof — matching on a moved timestamp would do exactly that, so success is
    matched on the delivery id this run minted and nothing else.
    """
    box = FakeBox(tmp_path, {"nova": NOVA_AGENT_SECRET})
    # Somebody else's successful wake, recorded on the same route, just before ours.
    box.pipeline.evidence.record_wake_ok("nova", "someone-elses", route="probe", synthetic=True)

    probe = box.probe(timeout=0.0)
    # Injection that goes nowhere: the daemon "accepts" but no wake is ever recorded.
    result = WakeProbe(
        secret=PROBE_SECRET,
        evidence_path=box.evidence_path,
        self_url="http://127.0.0.1:8000",
        post=lambda *_a: Injection(status=202, stages=(("resolve", "ok"),)),
        read=probe.read,
        sleep=lambda _d: None,
        timeout=0.0,
    ).run("nova", a_marker(NOVA_AGENT_SECRET))

    assert result.status == UNPROVABLE
    assert "no outcome recorded" in result.detail


def test_the_probe_cannot_write_the_evidence_it_reads(box) -> None:
    # The daemon is the evidence document's sole writer. A probe that could record its
    # own proof would be grading its own homework — and, run under the wrong identity,
    # would take the state file's ownership away from the daemon.
    box.probe().run("nova", a_marker(NOVA_AGENT_SECRET))
    owner_before = Path(box.evidence_path).stat().st_mtime
    read_evidence(box.evidence_path)
    assert Path(box.evidence_path).stat().st_mtime == owner_before


def test_a_daemon_that_does_not_serve_the_route_is_unprovable_not_broken() -> None:
    # Nothing was asked, so nothing can be concluded — and the message names the fix.
    result = WakeProbe(
        secret=PROBE_SECRET,
        evidence_path=None,
        self_url="http://127.0.0.1:8000",
        post=lambda *_a: Injection(status=404),
        read=lambda: read_evidence(None),
        sleep=lambda _d: None,
    ).run("nova", a_marker())

    assert result.status == UNPROVABLE
    assert "BASECRADLE_ROUTER_ENABLED_ROUTES" in result.detail


def test_a_rejected_signature_at_injection_is_a_definite_negative() -> None:
    result = WakeProbe(
        secret=PROBE_SECRET,
        evidence_path=None,
        self_url="http://127.0.0.1:8000",
        post=lambda *_a: Injection(status=401),
        read=lambda: read_evidence(None),
        sleep=lambda _d: None,
    ).run("nova", a_marker())

    assert result.status == BROKEN
    assert result.exit_code == 1
    assert "not the one the daemon holds" in result.detail


def test_an_unresolvable_recipient_is_a_definite_negative(box) -> None:
    result = box.probe().run("nobody", a_marker())

    assert result.status == BROKEN
    assert box.attempts == []


def test_a_probe_cannot_be_attempted_with_an_unusable_marker() -> None:
    # Not a verdict about the wake edge — the probe could not be *attempted* — so it is
    # raised rather than reported, and the CLI turns it into the inconclusive sentinel.
    probe = WakeProbe(
        secret=PROBE_SECRET,
        evidence_path=None,
        self_url="http://127.0.0.1:8000",
        post=lambda *_a: pytest.fail("nothing may be posted for an unusable marker"),
        read=lambda: read_evidence(None),
    )
    with pytest.raises(ProbeError, match="well-formed BCNOC1"):
        probe.run("nova", "BCNOC1 nonce")


def test_the_body_the_probe_signs_is_the_body_the_route_verifies() -> None:
    # The round trip, against the real route: a signature over anything but these exact
    # bytes is a rejected delivery. Pinned because the two halves are written apart.
    probe = WakeProbe(secret=PROBE_SECRET, evidence_path=None, self_url="http://x")
    marker = a_marker()
    body = probe.body_for("nova", marker)
    headers = probe.headers_for(body, "probe-round-trip")

    request = InboundRequest(headers=headers, body=body)
    ProbeRoute().verify(request, PROBE_SECRET)
    event = ProbeRoute().normalize(request)
    assert (event.recipient.value, event.wake_arg) == ("nova", marker)


# --- 8. the claims: a probe is a lever, never an edge ------------------------


def manifests(box, *, routes=("github", "probe"), agents=(NOVA, JT), host="ai.basecradle.com"):
    return build_manifests(
        config(agents=agents, routes=routes),
        registry(routes),
        box.evidence(),
        WakeLockGuard(lock_dir="/nonexistent"),
        host=host,
        evidence_path=box.evidence_path,
    )


def subject(all_manifests, name):
    return next(m for m in all_manifests if m["subject"] == name)


def claim(manifest, claim_id):
    return next((c for c in manifest["claims"] if c["claim"] == claim_id), None)


def test_a_probe_is_not_a_wake_edge_and_never_inflates_the_edge_count(box) -> None:
    """The instrument must not defeat itself.

    Nothing in the world will wake an agent through the router's own probe. Counting it
    as an edge would put ``edge_count: 1`` on a builder no event can reach and quietly
    retire the parked-builder finding — instance 4, greened by the very lever built to
    exercise it.
    """
    box.probe().run("jt", a_marker(JT_AGENT_SECRET))
    edge = claim(subject(manifests(box), "agent:jt"), "wake-edge:webhook-route")

    # jt is a persona: with only github+probe enabled, it has NO production edge at all.
    assert edge["detail"]["edges"] == []
    assert edge["detail"]["edge_count"] == 0
    # ...and yet the terminus demonstrably answers. That pair IS the honest reading.
    assert edge["detail"]["last_ok_at"] is not None
    assert edge["detail"]["last_ok_synthetic"] is True


def test_the_synthetic_claim_is_emitted_per_armed_agent_and_points_at_its_own_row(box) -> None:
    box.probe().run("nova", a_marker(NOVA_AGENT_SECRET))
    synthetic = claim(subject(manifests(box), "agent:nova"), "wake-edge:synthetic:probe")

    assert synthetic is not None
    assert synthetic["class"] == "rare"
    # `evidence`, never `probe`: the row goes green because the DAEMON recorded a wake,
    # not because some process exited zero.
    assert synthetic["prove"]["kind"] == "evidence"
    assert synthetic["prove"]["source"].endswith("#agent_wakes.nova.by_route.probe.last_ok_at")
    assert synthetic["detail"]["synthetic"] is True
    assert synthetic["detail"]["last_ok_at"] is not None
    # And it states the boundary plainly, so the NOC judges knowing it.
    assert synthetic["detail"]["stops_before"] == "exec claude"
    assert "probe_secret_match" in synthetic["detail"]["proves"]


def test_the_synthetic_claim_names_the_binary_a_persona_would_have_run(box) -> None:
    synthetic = claim(subject(manifests(box), "agent:jt"), "wake-edge:synthetic:probe")
    assert synthetic["detail"]["stops_before"] == f"exec {JT.wake_bin}"


def test_a_box_that_has_not_enabled_the_probe_advertises_no_lever(box) -> None:
    # A claim states a capability the router currently has. A deployment without the
    # probe route enabled has no lever, and must not say it does.
    without = manifests(box, routes=("github",))
    assert claim(subject(without, "agent:nova"), "wake-edge:synthetic:probe") is None
    assert claim(subject(without, "box:ai.basecradle.com"), "delivery-sink:probe") is None


def test_the_probes_delivery_sink_is_marked_synthetic(box) -> None:
    box.probe().run("nova", a_marker(NOVA_AGENT_SECRET))
    box_manifest = subject(manifests(box), "box:ai.basecradle.com")

    assert claim(box_manifest, "delivery-sink:probe")["detail"]["synthetic"] is True
    assert claim(box_manifest, "delivery-sink:github")["detail"]["synthetic"] is False


def test_every_declared_pointer_including_the_new_claim_resolves_from_its_own_detail(box) -> None:
    """Re-run the NOC's resolver rule over every emitted claim (basecradle-noc#409).

    The rule is one line: the pointer's last segment is the field, and ``detail`` is the
    object it lives in. A pointer that misses is refused by name and reads *unprovable* —
    which is the same silence, one level up. The sibling test in ``test_claims.py`` pins
    this for the pre-existing claims; this one pins that the synthetic claim joined them.
    """
    for manifest in manifests(box):
        for entry in manifest["claims"]:
            if entry["prove"]["kind"] != "evidence":
                continue
            field = entry["prove"]["source"].rsplit(".", 1)[-1]
            assert field in entry["detail"], (
                f"{entry['claim']} points at {field!r}, which its own detail does not publish"
            )


# --- 9. configuration -------------------------------------------------------


def test_the_probe_route_needs_its_own_secret_like_every_other_route(tmp_path) -> None:
    # Same gate as every source: enabled plus a secret, or the daemon does not boot. A
    # probe is a genuine signed delivery, not a back door into the middle of the path.
    registry_file = tmp_path / "agents.json"
    registry_file.write_text(
        json.dumps(
            {
                NOVA.key: {
                    "os_user": "nova",
                    "clone_path": "/home/nova/basecradle-python",
                    "bot_slug": "basecradle-python-ai",
                }
            }
        )
    )
    env = {
        "BASECRADLE_ROUTER_AGENTS": str(registry_file),
        "BASECRADLE_ROUTER_ENABLED_ROUTES": "github,probe",
        "BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET": "s",
    }
    with pytest.raises(ConfigError, match="BASECRADLE_ROUTER_PROBE_WEBHOOK_SECRET"):
        load_config(env)

    loaded = load_config({**env, "BASECRADLE_ROUTER_PROBE_WEBHOOK_SECRET": PROBE_SECRET})
    assert loaded.webhook_secret("probe") == PROBE_SECRET
    # And every registered agent is addressable by its harness key, from the registry.
    assert loaded.harness_index["nova"].key == NOVA.key


def test_the_injector_targets_the_daemons_own_loopback_listener_by_default() -> None:
    # Loopback, not the public hostname: a probe routed through Caddy would prove the
    # front end's liveness and quietly stop proving the daemon's.
    assert load_self_url({}) == "http://127.0.0.1:8000"
    assert load_self_url({"BASECRADLE_ROUTER_SELF_URL": "http://127.0.0.1:9000/"}) == (
        "http://127.0.0.1:9000"
    )


def test_the_marker_helpers_agree_with_the_wire_format() -> None:
    nonce = uuid.uuid4().hex
    marker = mint(nonce, NOVA_AGENT_SECRET)
    assert is_marker(marker)
    assert nonce_of(marker) == nonce
    assert nonce_of("not a marker") is None

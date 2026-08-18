"""The router's admin CLI — what the NOC runs to prove the daemon's claims.

    python -m basecradle_router claims            # the Contract v1 claims manifests
    python -m basecradle_router selftest freeze   # prove the freeze surface is readable
    python -m basecradle_router probe wake …      # prove one agent's wake edge, by using it
    python -m basecradle_router probe log-grammar    # prove the alarm's needle line is written
    python -m basecradle_router evidence          # dump the raw evidence document

Four subcommands, one purpose: everything the claims-vs-evidence ledger
(basecradle/basecradle#460, basecradle-noc#406) needs from this component, reached
without the running daemon. That matters — the emitter and the probe run from the
NOC's converge, in a *different process* from the daemon, which is why the daemon
writes its evidence to a file (:mod:`basecradle_router.evidence`) instead of
keeping it in memory where only it could see it.

**Run it as the daemon's user.** ``selftest freeze`` proves the wake-lock surface
is readable *with the credentials the daemon actually has*; root bypasses file
permissions, so a root run proves nothing and the probe says so (a ``degraded``
verdict with ``ran_as_root``). The ``deploy/bin/router-admin`` wrapper does the
privilege drop and sources the daemon's env file, so the NOC schedules one stable
path rather than reconstructing any of it.

**The CLI never writes the evidence document — the daemon is its sole writer.**
The temptation is obvious (record the probe's verdict as the freeze claim's
evidence) and the trap is worse: the evidence file is replaced atomically, so a
probe accidentally run as root would leave the state file root-owned and lock the
*daemon* out of writing its own evidence — an instrument that breaks the thing it
instruments. The probe reports through its exit code and its JSON, and the NOC's
ledger records what it ran; the ``evidence`` field in the emitted claim carries the
daemon's own last self-test, which is exactly what a *pointer* should say.

That rule is what gives ``probe wake`` its integrity too, and it is worth stating in
its own right: the synthetic wake reports ``proven`` **only** because the *daemon*
recorded a successful wake carrying the delivery id this run minted. The probe
process cannot write that record, so it cannot manufacture its own pass — which is
the property the design was asked for (*"the probe's own PASS is not the proof;
``last_ok_at`` moving is"* — `basecradle-noc#421`).

**Exit codes** are the probe contract, and the contract owner pins three readings
(basecradle-noc#408, ruling 4): ``0`` **proven** — the only thing that counts as
evidence; ``75`` **unprovable** — we never got an answer; any other non-zero
(``1`` here) **proven broken** — we asked, and the answer is no. See
:data:`~basecradle_router.selftest.EXIT_UNPROVABLE`.

A configuration error the CLI could not load shares the ``75`` code, because it is
the same state: no answer. It is still a finding in its own right — a config the
CLI cannot load is a config the daemon cannot boot on — so the *distinction* rides
on **stderr**, which the NOC forwards on any non-proven verdict. There is
deliberately no quieter "could not run" tier: a softened inconclusive is the
silent-death shape this program exists to kill.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from basecradle_router.app import build_registry
from basecradle_router.claims import build_manifests, manifest_filename
from basecradle_router.config import (
    ConfigError,
    load_admin_cmd,
    load_config,
    load_evidence_path,
    load_self_url,
    load_wake_lock_dir,
)
from basecradle_router.evidence import read_evidence
from basecradle_router.log_grammar import (
    LINE_CLASS,
    LogGrammarError,
    LogGrammarProbe,
)
from basecradle_router.probe import DEFAULT_TIMEOUT, ProbeError, WakeProbe
from basecradle_router.routes.probe import ProbeRoute
from basecradle_router.selftest import EXIT_UNPROVABLE, OK, run_freeze_selftest
from basecradle_router.wakelock import WakeLockGuard

#: A configuration the CLI cannot load. It carries the contract's *unprovable*
#: sentinel rather than a code of its own: from the ledger's side this is the same
#: state as a probe that could not read its surface — **we never got an answer** —
#: and the contract has no third non-zero tier to put it in. It is still a distinct
#: *finding* ("the router's own config is broken" sends the NOC somewhere different
#: from "the surface this probe examines is broken"), so the distinction is carried
#: on **stderr**, where the NOC already forwards a bounded tail on any non-proven
#: verdict.
EXIT_CONFIG_ERROR = EXIT_UNPROVABLE


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="basecradle_router",
        description="Admin CLI for the BaseCradle fleet router daemon.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    claims = sub.add_parser(
        "claims",
        help="emit this box's Claims Manifest Contract v1 manifests as JSON",
        description=(
            "Print a JSON array of Contract v1 manifests — one per subject: the box, "
            "then one per registered agent. With --out-dir, write each manifest to its "
            "own single-subject file instead (the strict per-file contract shape). The "
            "two are different surfaces, not two spellings: stdout is what "
            "provision-claims reads, the directory is what the census reads."
        ),
    )
    claims.add_argument(
        "--out-dir",
        metavar="DIR",
        help=(
            "write one single-subject manifest file per subject into DIR, named as the "
            "contract pins it: <component>.json for the box, <component>@<slug>.json "
            "per agent"
        ),
    )
    claims.add_argument("--host", help="override the box hostname used in the box subject")
    claims.set_defaults(handler=_cmd_claims)

    selftest = sub.add_parser(
        "selftest", help="run a liveness probe for one of the daemon's control surfaces"
    )
    probes = selftest.add_subparsers(dest="probe", required=True)
    freeze = probes.add_parser(
        "freeze",
        help="prove the NOC wake-lock (freeze) surface is readable and honoured now",
        description=(
            "Read the wake-lock directory the way the daemon does and report whether the "
            "freeze control is readable, parseable, and would be honoured. Exit 0 proven, "
            "1 proven broken, 75 could not be proven (the cause is on stderr). Run as "
            "the daemon's user."
        ),
    )
    freeze.add_argument("--json", action="store_true", help="print the full result as JSON")
    freeze.set_defaults(handler=_cmd_selftest_freeze)

    probe = sub.add_parser(
        "probe", help="exercise a control surface by using it, and report what moved"
    )
    kinds = probe.add_subparsers(dest="kind", required=True)
    wake = kinds.add_parser(
        "wake",
        help="prove one agent's wake edge by firing a signed synthetic delivery at it",
        description=(
            "Fire a signed test delivery at this daemon's own real verify->wake path for "
            "one agent, then report whether the DAEMON recorded a successful wake for it. "
            "The BCNOC1 marker is read from stdin (never argv) and is minted by the NOC "
            "with that agent's own probe secret — the router holds no agent's secret and "
            "verifies nothing. Exit 0 proven, 1 proven broken, 75 could not be proven."
        ),
    )
    wake.add_argument(
        "--agent",
        required=True,
        metavar="SLUG",
        help="the recipient's harness_key (its OS-user slug — the agent's universal identity)",
    )
    wake.add_argument("--json", action="store_true", help="print the full result as JSON")
    wake.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=(
            "how long to wait for the daemon to record an outcome "
            f"(default {DEFAULT_TIMEOUT:.0f}s; a probe queued behind a live session waits)"
        ),
    )
    wake.set_defaults(handler=_cmd_probe_wake)

    grammar = kinds.add_parser(
        "log-grammar",
        help="prove the alarm's needle line is still the line this daemon writes",
        description=(
            "Drive a real wake-rate breaker past its threshold so the daemon's own trip "
            "statement renders the line, and write it to the daemon's journald identifier "
            "stamped source=probe at INFO. Proves RENDERED, never landed or extracted — "
            "the daemon is a system user that cannot read the journal back, so the NOC's "
            "witness on that identifier owns those halves, off the live stream. "
            "Exit 0 proven, 1 proven broken, 75 could not be proven."
        ),
    )
    grammar.add_argument(
        "line_class",
        nargs="?",
        default=LINE_CLASS,
        metavar="LINE_CLASS",
        help=(
            f"the NOC column whose grammar to prove (default {LINE_CLASS}; it is also the "
            "claim id's suffix, so a second line class is a new claim row rather than a flag)"
        ),
    )
    grammar.add_argument("--json", action="store_true", help="print the full result as JSON")
    grammar.set_defaults(handler=_cmd_probe_log_grammar)

    evidence = sub.add_parser(
        "evidence",
        help="print the evidence document the daemon writes (delivery + wake proof)",
    )
    evidence.set_defaults(handler=_cmd_evidence)

    return parser


def _cmd_claims(args: argparse.Namespace) -> int:
    config = load_config()
    evidence_path = load_evidence_path()
    manifests = build_manifests(
        config,
        build_registry(config),
        read_evidence(evidence_path),
        WakeLockGuard(lock_dir=load_wake_lock_dir()),
        host=args.host,
        evidence_path=evidence_path,
        admin_cmd=load_admin_cmd(),
    )
    if args.out_dir:
        _write_manifests(args.out_dir, manifests)
        return 0
    print(json.dumps(manifests, indent=2))
    return 0


def _write_manifests(out_dir: str, manifests: list[dict]) -> None:
    """Write one strict single-subject manifest file per subject into ``out_dir``.

    The array on stdout and this directory are two **surfaces**, not two spellings of
    one: stdout is what ``provision-claims`` reads per subject, the directory is what
    the census walks. The filenames are the contract's, not ours — see
    :func:`~basecradle_router.claims.manifest_filename`. Nothing here is atomic on
    purpose: the NOC's converge owns the real discovery directory and its own write
    discipline — this is the convenience path for an operator or a converge step that
    just wants the files.
    """
    os.makedirs(out_dir, exist_ok=True)
    for manifest in manifests:
        path = os.path.join(out_dir, manifest_filename(manifest))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
            handle.write("\n")
        print(path)


def _cmd_selftest_freeze(args: argparse.Namespace) -> int:
    config = load_config()
    result = run_freeze_selftest(
        WakeLockGuard(lock_dir=load_wake_lock_dir()),
        {agent.harness_key for agent in config.agents.values()},
    )
    if args.json:
        print(json.dumps(result.to_json(), indent=2))
    else:
        print(f"freeze-readability: {result.status} (lock_dir={result.lock_dir})")
        print(f"  ran as: {result.ran_as}")
        for check in result.checks:
            print(f"  [{check.status}] {check.target}: {check.state} — {check.detail}")
    if result.status != OK:
        # The verdict on stderr, not only stdout. ``degraded`` and a config error share
        # exit 75 by contract, so stderr is where the two are told apart — and it is the
        # stream the NOC forwards on any non-proven verdict, whether or not --json was
        # asked for. One line, naming the surface and the cause.
        print(
            f"freeze-readability: {result.status} "
            f"(lock_dir={result.lock_dir} ran_as={result.ran_as}): {result.summary()}",
            file=sys.stderr,
        )
    return result.exit_code


def _cmd_probe_wake(args: argparse.Namespace) -> int:
    """Fire one synthetic wake at ``--agent`` and report what the daemon's evidence said.

    The marker is read from **stdin**, never argv: it is minted by the NOC with the
    recipient's own probe secret, and a value on the command line would be visible in
    ``ps`` to every account on the box. (A marker is not itself a secret — it is an HMAC
    over a one-time nonce — but the discipline is the NOC's own for
    ``mint-probe-secret``, and one exception is how a rule stops being a rule.)

    A :class:`~basecradle_router.probe.ProbeError` is *not* a verdict about the wake
    edge — it means the probe could not be attempted (an unusable marker, a daemon that
    would not answer) — so it reports the contract's inconclusive sentinel with the cause
    on stderr, exactly as a configuration error does.
    """
    config = load_config()
    marker = sys.stdin.read().strip()
    probe = WakeProbe(
        secret=config.webhook_secret(ProbeRoute.name),
        evidence_path=load_evidence_path(),
        self_url=load_self_url(),
        timeout=args.timeout,
    )
    try:
        result = probe.run(args.agent, marker)
    except ProbeError as exc:
        print(f"synthetic-wake: unprovable: {exc}", file=sys.stderr)
        return EXIT_UNPROVABLE

    if args.json:
        print(json.dumps(result.to_json(), indent=2))
    else:
        print(f"synthetic-wake: {result.status} (agent={result.harness_key} route={result.route})")
        print(f"  delivery: {result.delivery_id} nonce={result.nonce}")
        print(f"  injected: HTTP {result.injection.status} ({result.injection.terminal()})")
        print(f"  before:   agent last_ok_at={result.before.agent_last_ok_at}")
        print(f"  after:    agent last_ok_at={result.after.agent_last_ok_at}")
        print(f"  {result.detail}")
    if not result.proven:
        # The verdict on stderr as well, for the same reason `selftest freeze` does it:
        # `broken` and `unprovable` are what the NOC forwards, and it must be able to
        # read the cause without having asked for --json.
        print(f"synthetic-wake: {result.status}: {result.summary()}", file=sys.stderr)
    return result.exit_code


def _cmd_probe_log_grammar(args: argparse.Namespace) -> int:
    """Emit one synthetic breaker trip and report whether it carries the declared grammar.

    It needs **no configuration at all**, which is the point rather than an omission: the
    bytes under proof come from the breaker's own trip statement and the journal is
    addressed by the daemon's identifier, so there is nothing here for a stale
    ``router.env`` to make this probe answer about the wrong thing.

    A :class:`~basecradle_router.log_grammar.LogGrammarError` is not a verdict about the
    grammar — the journal tooling did not answer — so it reports the contract's
    inconclusive sentinel with the cause on stderr, exactly as a configuration error does.
    """
    if args.line_class != LINE_CLASS:
        # Named, never silently substituted: a probe that quietly proved a different line
        # than the one asked for would report a healthy grammar about the wrong column.
        print(
            f"log-grammar: unprovable: this component states no grammar for "
            f"{args.line_class!r} (it declares {LINE_CLASS!r})",
            file=sys.stderr,
        )
        return EXIT_UNPROVABLE

    try:
        result = LogGrammarProbe().run()
    except LogGrammarError as exc:
        print(f"log-grammar: unprovable: {exc}", file=sys.stderr)
        return EXIT_UNPROVABLE

    if args.json:
        print(json.dumps(result.to_json(), indent=2))
    else:
        print(f"log-grammar: {result.status} ({result.line_class} -> {result.identifier})")
        print(f"  grammar:  {result.grammar}")
        print(f"  rendered: {result.rendered}")
        print(f"  {result.detail}")
    if not result.proven:
        # The verdict on stderr too, for the reason its siblings do it: `broken` and
        # `unprovable` are what the NOC forwards, and it must be able to read the cause
        # without having asked for --json.
        print(f"log-grammar: {result.status}: {result.summary()}", file=sys.stderr)
    return result.exit_code


def _cmd_evidence(args: argparse.Namespace) -> int:
    document = read_evidence(load_evidence_path())
    print(json.dumps(document.to_json(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

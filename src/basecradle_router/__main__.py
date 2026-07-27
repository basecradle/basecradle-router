"""The router's admin CLI — what the NOC runs to prove the daemon's claims.

    python -m basecradle_router claims            # the Contract v1 claims manifests
    python -m basecradle_router selftest freeze   # prove the freeze surface is readable
    python -m basecradle_router evidence          # dump the raw evidence document

Three subcommands, one purpose: everything the claims-vs-evidence ledger
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

**Exit codes** are the probe contract: ``0`` proven, ``1`` proven broken, ``2``
could not be proven (see :data:`~basecradle_router.selftest.EXIT_CODES`), and
``3`` for a configuration error that stopped the command running at all — which is
itself a finding, since a config the CLI cannot load is a config the daemon cannot
boot on.
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
    load_wake_lock_dir,
)
from basecradle_router.evidence import read_evidence
from basecradle_router.selftest import run_freeze_selftest
from basecradle_router.wakelock import WakeLockGuard

#: A configuration the CLI cannot load — distinct from every probe verdict, because
#: "the router's own config is broken" is a different finding from "the surface this
#: probe examines is broken", and a ledger that conflated them would send the NOC
#: looking at the wrong thing.
EXIT_CONFIG_ERROR = 3


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
            "own single-subject file instead (the strict per-file contract shape)."
        ),
    )
    claims.add_argument(
        "--out-dir",
        metavar="DIR",
        help="write one single-subject manifest file per subject into DIR",
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
            "1 proven broken, 2 could not be proven. Run as the daemon's user."
        ),
    )
    freeze.add_argument("--json", action="store_true", help="print the full result as JSON")
    freeze.set_defaults(handler=_cmd_selftest_freeze)

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

    The array on stdout is the lossless form (many subjects, one component); this is
    the form that drops straight into a ``claims.d``-style directory, one file per
    ledger subject. Nothing here is atomic on purpose: the NOC's converge owns the
    real discovery directory and its own write discipline — this is the convenience
    path for an operator or a converge step that just wants the files.
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
    return result.exit_code


def _cmd_evidence(args: argparse.Namespace) -> int:
    document = read_evidence(load_evidence_path())
    print(json.dumps(document.to_json(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

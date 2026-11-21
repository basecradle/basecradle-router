"""A fabricated box for the sealed-wake proof (#535), built inside a private mount namespace.

Run as root by ``tests/test_sealed_wake_root.py``, always under
``unshare --mount --propagation private``, never directly:

    unshare --mount --propagation private -- python3 -I sealed_wake_box.py '<spec json>'

It lays an overlay over ``/etc``, ``/opt``, ``/home`` and ``/var`` (where an account's
creation would otherwise leave its log records), whose upper layers sit on a tmpfs
mounted at the spec's scratch directory, so every account, registry, tree and file it makes
exists only in this namespace and is gone when the process exits. The real filesystem is read,
never written: ``wake-runner`` keeps its constant paths (``/etc/basecradle-router/agents.json``,
``/opt/basecradle-harness``), which is the point of running the real script.

The box it builds is the box the issue describes:

* ``nova`` and ``jt``, two harness agents (UID >= 1000), each with a clone, an ``agent.env``
  holding a probe secret, and the home venv an unsealed agent wakes from;
* ``/opt/basecradle-harness`` and its ``venv``, ``root:root 0755``;
* a sealed tree per agent at ``venv/<user>``, a REAL environment built the way the NOC builds
  one: ``uv venv`` at its final name, then ``uv pip install`` of a wheel declaring the
  ``basecradle-harness-wake`` console script (``tests/proof_wheel.py``), so ``bin/python`` is
  uv's link to the system interpreter and the script's ``#!`` is uv's own. It is then owned by
  that agent's updater account, group the agent, with its top two directories ``2750`` (the
  setgid bit is how the group is handed down);
* the router's root-owned ``bin/`` with the real ``wake-runner``, ``probe-ack`` and
  ``set-wake-bin``;
* the registry, ``root:router 0640``, with nova's ``wake_bin`` sealed or in its home as the
  spec says, jt sealed, and one builder entry.

Then it runs the spec's root-run mutations (the hostile shapes), then its runs, and prints one
JSON object: each run's exit code and output, the registry before and after, its owner and
mode, whether the setter's staging slot is left, and what the woken binary wrote, if anything.

Fabricated cast only: Nova Digital (``nova``) and ``jt``. The secret is a correctly shaped fake.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Run with `python3 -I`, which keeps this directory off sys.path; the one sibling it needs is
# named here explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import proof_wheel  # noqa: E402

TRUSTED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
REGISTRY = Path("/etc/basecradle-router/agents.json")
STAGING = Path("/etc/basecradle-router/.agents.json.set-wake-bin.tmp")
BASE = Path("/opt/basecradle-harness")
ROUTER_BIN = Path("/opt/basecradle-router/bin")
AGENTS = ("nova", "jt")
SECRET = "bcnoc1-fake-probe-secret-0000000000000000000000000000000000000000"
NONCE = "0192f3a4-5b6c-7d8e-9f01-0000000000ab"
TIMELINE = "0192f3a4-5b6c-7d8e-9f01-0000000000cd"
RECIPIENTS = {
    "nova": "0192f3a4-5b6c-7d8e-9f01-000000000001",
    "jt": "0192f3a4-5b6c-7d8e-9f01-000000000002",
}

WAKE_SCRIPT = """\
#!{interpreter}
import os, pwd, sys
with open(os.path.join(os.getcwd(), "woke"), "w") as out:
    out.write("\\n".join([pwd.getpwuid(os.getuid()).pw_name, *sys.argv]) + "\\n")
"""


def sh(*argv: str) -> None:
    subprocess.run(argv, check=True, env={"PATH": TRUSTED_PATH})


def sealed_bin(user: str) -> str:
    return f"{BASE}/venv/{user}/bin/basecradle-harness-wake"


def home_bin(user: str) -> str:
    return f"/home/{user}/venv/bin/basecradle-harness-wake"


def marker() -> str:
    signature = hmac.new(SECRET.encode(), f"BCNOC1 {NONCE}".encode(), hashlib.sha256)
    return f"BCNOC1 {NONCE} {signature.hexdigest()}"


def overlay(scratch: Path, target: str) -> None:
    name = target.strip("/")
    upper, work = scratch / name / "upper", scratch / name / "work"
    upper.mkdir(parents=True)
    work.mkdir(parents=True)
    sh(
        "mount", "-t", "overlay", "overlay",
        "-o", f"lowerdir={target},upperdir={upper},workdir={work}", target,
    )  # fmt: skip


def install(source: Path, dest: Path, mode: int, owner: str = "root", group: str = "root") -> None:
    sh("install", "-o", owner, "-g", group, "-m", f"{mode:o}", str(source), str(dest))


def build_agent(user: str, sealed: bool, spec: dict, scratch: Path) -> None:
    sh("useradd", "--create-home", "--no-log-init", "--user-group", "--shell", "/bin/bash", user)
    sh(
        "useradd",
        "--system",
        "--no-create-home",
        "--no-log-init",
        "--user-group",
        f"harness-up-{user}",
    )
    home = Path(f"/home/{user}")
    clone = home / "harness"
    config = home / ".config" / "basecradle"
    for directory in (clone, config, home / "venv" / "bin"):
        directory.mkdir(parents=True, exist_ok=True)
    (config / "agent.env").write_text(f"NOC_PROBE_SECRET={SECRET}\n", encoding="utf-8")
    Path(home_bin(user)).write_text(WAKE_SCRIPT.format(interpreter="/usr/bin/python3"))
    sh("chown", "-R", f"{user}:{user}", str(home))
    sh("chmod", "0700", str(home), str(clone))
    sh("chmod", "0600", str(config / "agent.env"))
    sh("chmod", "0755", home_bin(user))
    if not sealed:
        return
    tree = BASE / "venv" / user
    wheels = scratch / "wheels"
    wheels.mkdir(exist_ok=True)
    script = proof_wheel.build_venv(
        spec["uv"], spec["python"], tree, proof_wheel.build_wheel(wheels), scratch / "uv-cache"
    )
    assert str(script) == sealed_bin(user), script
    sh("chown", "-R", "--no-dereference", f"harness-up-{user}:{user}", str(tree))
    sh("chmod", "2750", str(tree), str(tree / "bin"))


def registry(nova_sealed: bool) -> dict:
    entries: dict = {
        "basecradle/basecradle-ruby": {
            "os_user": "jt",
            "clone_path": "/home/jt/repos/basecradle-ruby",
            "bot_slug": "basecradle-ruby-ai",
        }
    }
    for user in AGENTS:
        entries[user] = {
            "kind": "harness",
            "os_user": user,
            "clone_path": f"/home/{user}/harness",
            "recipient_uuid": RECIPIENTS[user],
            "wake_bin": sealed_bin(user) if (user != "nova" or nova_sealed) else home_bin(user),
        }
    return entries


def run(argv: list[str], as_user: str | None, scratch: Path) -> dict:
    argv = [part.replace("{marker}", marker()) for part in argv]
    if as_user:
        argv = ["runuser", "-u", as_user, "--", *argv]
    env = {"PATH": TRUSTED_PATH, "SCRATCH": str(scratch)}
    done = subprocess.run(argv, capture_output=True, text=True, env=env)
    return {"rc": done.returncode, "stdout": done.stdout, "stderr": done.stderr}


def main(spec: dict) -> dict:
    repo = Path(spec["repo"])
    scratch = Path(spec["scratch"])
    sh("mount", "-t", "tmpfs", "-o", "mode=0700", "tmpfs", str(scratch))
    for target in ("/etc", "/opt", "/home", "/var"):
        overlay(scratch, target)

    # The daemon's account, as on the box: the gate refuses a tree it owns.
    sh("useradd", "--system", "--no-create-home", "--no-log-init", "--user-group", "router")
    for directory in (BASE, BASE / "venv", ROUTER_BIN, REGISTRY.parent):
        directory.mkdir(parents=True, exist_ok=True)
        sh("chown", "root:root", str(directory))
        sh("chmod", "0755", str(directory))
    for program in ("wake-runner", "probe-ack", "set-wake-bin"):
        install(repo / "deploy" / "bin" / program, ROUTER_BIN / program, 0o755)
    for user in AGENTS:
        build_agent(user, sealed=(user != "nova" or spec["nova_tree"]), spec=spec, scratch=scratch)
    REGISTRY.write_text(json.dumps(registry(spec["nova_sealed"]), indent=2) + "\n")
    sh("chown", "root:router", str(REGISTRY))
    sh("chmod", "0640", str(REGISTRY))

    # Whether a real wake can attach to the journal here (a container has none): asked of the
    # same systemd-cat wake-runner resolves, off the same trusted PATH.
    systemd_cat = shutil.which("systemd-cat", path=TRUSTED_PATH)
    journal = systemd_cat is not None and (
        subprocess.run(
            [systemd_cat, "--identifier=sealed-wake-proof", "true"],
            capture_output=True,
            env={"PATH": TRUSTED_PATH},
        ).returncode
        == 0
    )

    # A mutation's own scratch files go in $SCRATCH, on the namespace's tmpfs, never in /tmp.
    for mutation in spec.get("mutations", []):
        subprocess.run(
            ["bash", "-euo", "pipefail", "-c", mutation],
            check=True,
            env={"PATH": TRUSTED_PATH, "SCRATCH": str(scratch)},
        )

    before = REGISTRY.read_text()
    runs = [run(entry["argv"], entry.get("as"), scratch) for entry in spec["runs"]]
    after = REGISTRY.read_text()
    status = REGISTRY.stat()
    woke = Path("/home/nova/harness/woke")
    return {
        "journal": journal,
        "runs": runs,
        "registry_before": before,
        "registry_after": after,
        "registry_owner": [status.st_uid, status.st_gid, oct(status.st_mode & 0o7777)],
        "router_gid": int(
            subprocess.run(
                ["getent", "group", "router"], capture_output=True, text=True, check=True
            ).stdout.split(":")[2]
        ),
        "staging_left": STAGING.exists() or STAGING.is_symlink(),
        "woke": woke.read_text() if woke.exists() else None,
        "exists": {path: Path(path).exists() for path in spec.get("check_paths", [])},
    }


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("sealed_wake_box: run as root, inside `unshare --mount --propagation private`")
    print(json.dumps(main(json.loads(sys.argv[1]))))

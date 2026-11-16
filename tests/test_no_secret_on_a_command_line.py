"""No secret on a command line, in any script this repository ships (basecradle-router#468).

A program's argv is readable by every account on the box (`ps`, `/proc/<pid>/cmdline`) for
as long as it runs; its environment and its stdin are not. The deploy smoke test signed
every delivery with `openssl dgst -hmac <key>` until basecradle/basecradle#853 found it,
and a security finding is a sample of a class, so this is the one guard that holds the
whole class shut:

1. **The universe is discovered, never listed.** Every tracked shell script, and every
   tracked Python file that can start a program at all (it imports ``subprocess`` or
   ``pty``, or reaches an ``os.exec*``/``os.spawn*``/``os.system``/``os.popen``), is a
   place a command line can come from. Each must be classified below, so a new one fails
   here the day it lands, before anyone has had to remember this file.
2. **A script that handles a secret is run, whole, under the argv recorder**
   (``tests/argv_recorder.py``), with every secret it can read planted as a fake, and no
   recorded command line may carry one. Each run also proves it reached the program that
   consumes the secret, holding the secret, so a run that went nowhere cannot pass.
3. **A script that handles no secret must not name one.** A tripwire over its code (its
   comments and docstrings excluded) refuses the vocabulary of a secret source, so a script
   that starts reading one is reclassified, and driven, rather than waved through.
4. **Every workflow holds a secret only in ``env:`` or an action's input.** A ``run:``
   body that expands one in place, or reads a secret-carrying variable for anything but
   ``test -n``, fails. A GitHub-hosted runner is single-tenant, so this is the rule held
   without exception rather than a cross-account read (see #468).
5. **No systemd unit expands a value into an ``Exec*=`` line,** where systemd would put it
   on the daemon's argv for its whole life, and the session hooks name no secret.
6. **The guard is mutation-checked, permanently.** For every driven script, a planted
   secret-on-a-command-line of the classic shapes (`env VAR=value`, a header, an argument)
   must be caught, so a recorder that stopped seeing would fail here rather than go green.

Fabricated cast and correctly-shaped fakes only; nothing reaches the network, a model, an
agent, or root.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

import test_smoke_test_enabled_routes as smoke
from argv_recorder import ArgvRecorder
from test_shell_pipeline_safety import shell_scripts

REPO = Path(__file__).resolve().parents[1]

#: Only the driven tests run the scripts; the classification, the tripwire and the workflow
#: rule are static and run everywhere, so a box without jq never skips the whole guard.
needs_shell = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("bash", "jq")),
    reason="bash and jq are required to run the shipped scripts",
)

NOVA_UUID = "01980a3c-6f2e-7c41-9d3a-5b8e2f1c4a07"


@dataclass
class Run:
    """One driven run: what it recorded, the secrets in play, and its own proof of life."""

    recorder: ArgvRecorder
    secrets: tuple[str, ...]
    #: Asserts the run reached the program that consumes the secret, holding it.
    reached: Callable[[], None]


Driver = Callable[[str, Path], Run]


def _bash(script: Path, *args: str, env: dict[str, str], stdin: str | None = None):
    return subprocess.run(
        ["bash", str(script), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source)
    path.chmod(0o755)
    return path


# --- deploy/smoke-test.sh: every route's secret, as the deploy gate runs ------------------


def _drive_smoke_test(armed: bool) -> Driver:
    """Armed, every route's secret and the probe route's mark key are in play; unarmed (no
    probe secret), the gate takes its unmarked path, which signs and sends on its own."""

    def drive(source: str, tmp_path: Path) -> Run:
        return _smoke_run(source, tmp_path, armed)

    return drive


def _smoke_run(source: str, tmp_path: Path, armed: bool) -> Run:
    recorder = ArgvRecorder(tmp_path)
    # The gate finds a host's files beside itself (deploy/hosts/<fqdn>/) and refuses to
    # read them through a symlink, so its copy runs beside a copy of the tree's own.
    deploy = tmp_path / "deploy"
    shutil.copytree(REPO / "deploy" / "hosts", deploy / "hosts")
    script = _write(deploy, "smoke-test.sh", source)
    journal = smoke.GITHUB_JOURNAL + smoke.BASECRADLE_JOURNAL
    result, requests = smoke._run(
        tmp_path,
        env_lines=smoke.ARMED if armed else smoke.GITHUB_VARS,
        github_served=True,
        journal_lines=smoke._tagged(journal) if armed else journal,
        script=script,
        recorder=recorder,
    )

    def reached() -> None:
        assert result.returncode == 0, result.stdout + result.stderr
        assert smoke._route(requests, "github") == ["401", "400", "200"]
        assert smoke._route(requests, "basecradle") == smoke.BASECRADLE_CODES
        assert recorder.ran("python3"), "the signer never ran"
        assert not recorder.ran("openssl"), "the signer went back to openssl"
        assert recorder.ran("jq"), "the recorder must reach jq too"
        assert ("synthetic mark: armed" in result.stdout) is armed, result.stdout

    secrets = (smoke.GITHUB_SECRET, smoke.NOVA_KEY, *((smoke.PROBE_SECRET,) if armed else ()))
    return Run(recorder, secrets, reached)


# --- deploy/bin/router-admin: all of router.env, handed to the CLI -------------------------

ROUTER_ENV_SECRETS = {
    "BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET": "ghs-fake-admin-0000000000000000000000",
    "BASECRADLE_ROUTER_PROBE_WEBHOOK_SECRET": "probe-fake-admin-00000000000000000000",
    "BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET": "bc_isk_fakeadminsharedkey000000000001",
    "BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET_NOVA": "bc_isk_fakeadminnovakey00000000000001",
}


def _drive_router_admin(branch: str) -> Driver:
    """The wrapper ends in one of two execs: the venv's python where the deploy left it,
    else `uv run` at its default path. Each is driven, each a placed shim."""

    def drive(source: str, tmp_path: Path) -> Run:
        return _router_admin_run(source, tmp_path, branch)

    return drive


def _router_admin_run(source: str, tmp_path: Path, branch: str) -> Run:
    recorder = ArgvRecorder(tmp_path)
    script = _write(tmp_path, "router-admin", source)
    env_file = tmp_path / "router.env"
    env_file.write_text("".join(f"{name}={value}\n" for name, value in ROUTER_ENV_SECRETS.items()))
    env_file.chmod(0o600)
    app = tmp_path / "app"
    seen = tmp_path / "seen"
    # Both are exec'd by absolute path, so each is a placed shim, not a PATH one.
    launcher = app / ".venv" / "bin" / "python" if branch == "venv" else tmp_path / ".local/bin/uv"
    recorder.place(
        launcher,
        f'printf "%s" "$BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET" >"{seen}"\ncat >/dev/null\n',
    )
    recorder.passthrough(source)
    bindir = recorder.install()
    user = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    result = _bash(
        script,
        "probe",
        "wake",
        "--agent",
        "nova",
        "--json",
        stdin="BCNOC1 fake-nonce " + "0" * 64 + "\n",
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "BASECRADLE_ROUTER_APP_DIR": str(app),
            "BASECRADLE_ROUTER_ENV_FILE": str(env_file),
            "BASECRADLE_ROUTER_USER": user,
        },
    )

    def reached() -> None:
        assert result.returncode == 0, result.stderr
        assert recorder.ran(launcher.name), "the CLI never ran"
        assert seen.read_text() == ROUTER_ENV_SECRETS["BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET"]

    return Run(recorder, tuple(ROUTER_ENV_SECRETS.values()), reached)


# --- deploy/bin/wake-runner: the agent's own agent.env, loaded after the drop -------------

AGENT_ENV_SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-fakenovawakekey000000000000000000000000",
    "NOC_PROBE_SECRET": "noc-probe-fake-nova-0000000000000000000000",
    "BASECRADLE_TOKEN": "bc_uat_fakenovatoken0000000000000000000000",
    "GH_APP_PEM_B64": "LS0tLS1CRUdJTiBGQUtFIFBSSVZBVEUgS0VZLS0tLS0K",
}
PROBE_NONCE = "fake-nonce-0001"
PROBE_MARKER = (
    f"BCNOC1 {PROBE_NONCE} "
    + hmac.new(
        AGENT_ENV_SECRETS["NOC_PROBE_SECRET"].encode(),
        f"BCNOC1 {PROBE_NONCE}".encode(),
        hashlib.sha256,
    ).hexdigest()
)

#: The four constants that pin wake-runner to the box, and its root check. Each is swapped
#: for the test's own, and each must occur exactly as written, so a wake-runner that moved
#: one fails here loudly rather than running against the box's real path.
WAKE_RUNNER_BOX = {
    '[[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root (via the sudoers rule)"\n': (
        ': "the recorder runs unprivileged, and runuser below is a shim"\n'
    ),
    "REGISTRY=/etc/basecradle-router/agents.json\n": "REGISTRY={registry}\n",
    "TRUSTED_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n": (
        "TRUSTED_PATH={bindir}\n"
    ),
    "PROBE_ACK=/opt/basecradle-router/bin/probe-ack\n": "PROBE_ACK={probe_ack}\n",
    "STREAM_TRANSLATE=/opt/basecradle-router/bin/claude-stream-translate\n": (
        "STREAM_TRANSLATE={stream_translate}\n"
    ),
}


def _wake_runner_on(source: str, **values: str) -> str:
    for old, new in WAKE_RUNNER_BOX.items():
        assert source.count(old) == 1, f"wake-runner no longer carries {old!r}"
        source = source.replace(old, new.format(**values))
    homes = source.count('"/home/$user"')
    assert homes == 2, f"expected the clone and wake_bin gates under /home/$user, got {homes}"
    return source.replace('"/home/$user"', f'"{values["homes"]}/$user"')


def _drive_wake_runner(mode: str) -> Driver:
    def drive(source: str, tmp_path: Path) -> Run:
        recorder = ArgvRecorder(tmp_path)
        homes = (tmp_path / "home").resolve()
        home = homes / "nova"
        clone = home / "clone"
        clone.mkdir(parents=True)
        wake_bin = home / "venv" / "bin" / "nova-wake"
        env_dir = home / ".config" / "basecradle"
        env_dir.mkdir(parents=True)
        (env_dir / "agent.env").write_text(
            "".join(f"{name}={value}\n" for name, value in AGENT_ENV_SECRETS.items())
        )
        registry = tmp_path / "agents.json"
        kind = "harness" if mode == "harness" else "github"
        entry = {
            "kind": kind,
            "os_user": "nova",
            "clone_path": str(clone),
            "wake_bin": str(wake_bin),
        }
        registry.write_text(json.dumps({"nova": entry}))
        seen = tmp_path / "seen"
        consumer = f'printf "%s" "$ANTHROPIC_API_KEY" >"{seen}"\n'
        recorder.fake("claude", consumer)
        recorder.place(wake_bin, consumer)
        recorder.fake("python3", f'exec "{sys.executable}" "$@"\n')
        # nova is not a real account here: `id` answers for it, and runuser stands in for
        # the drop, giving the agent bash nova's HOME as the real one does.
        recorder.fake(
            "id",
            'if [ "$1" = -u ] && [ "$2" = nova ]; then echo 1000; exit 0; fi\n'
            'exec /usr/bin/id "$@"\n',
        )
        recorder.fake(
            "runuser",
            '[ "$1" = -u ] && [ "$3" = -- ] || exit 97\n'
            f'export HOME="{homes}/$2" USER="$2" LOGNAME="$2"\nshift 3\nexec "$@"\n',
        )
        recorder.fake("systemd-cat", 'while [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n')
        recorder.passthrough(source)
        bindir = recorder.install()
        script = _write(
            tmp_path,
            "wake-runner",
            _wake_runner_on(
                source,
                registry=str(registry),
                bindir=bindir,
                probe_ack=str(REPO / "deploy" / "bin" / "probe-ack"),
                stream_translate=str(REPO / "deploy" / "bin" / "claude-stream-translate"),
                homes=str(homes),
            ),
        )
        tail = {
            "builder": ["--", "claude", "-p", "Cross-repo handoff: work https://example.com/1"],
            "harness": ["--", str(wake_bin), "--timeline", NOVA_UUID],
            "probe": ["--probe", PROBE_MARKER],
        }[mode]
        result = _bash(
            script,
            "--user",
            "nova",
            "--cwd",
            str(clone),
            "--delivery",
            "fake-delivery-1",
            *tail,
            env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        )

        def reached() -> None:
            assert result.returncode == 0, result.stdout + result.stderr
            assert recorder.ran("runuser"), "the privilege drop never ran"
            if mode == "probe":
                # The verifier read the agent's own NOC_PROBE_SECRET: only that acks.
                assert f"BCNOC1-ACK {PROBE_NONCE}" in result.stdout, result.stdout
            else:
                assert seen.read_text() == AGENT_ENV_SECRETS["ANTHROPIC_API_KEY"]

        return Run(recorder, tuple(AGENT_ENV_SECRETS.values()), reached)

    return drive


# --- .github/scripts/ping-heartbeat.sh: the heartbeat URL --------------------------------

HEARTBEAT_URL = "https://uptime.betterstack.com/api/v1/heartbeat/FakeHeartbeatGuard00000"


def _drive_ping_heartbeat(source: str, tmp_path: Path) -> Run:
    recorder = ArgvRecorder(tmp_path)
    script = _write(tmp_path, "ping-heartbeat.sh", source)
    config = tmp_path / "config"
    recorder.fake("curl", f'cat >"{config}"\n')
    recorder.passthrough(source)
    bindir = recorder.install()
    result = _bash(script, env={"PATH": f"{bindir}:/usr/bin:/bin", "HEARTBEAT_URL": HEARTBEAT_URL})

    def reached() -> None:
        assert result.returncode == 0, result.stderr
        assert HEARTBEAT_URL in config.read_text(), "curl never received the URL"

    return Run(recorder, (HEARTBEAT_URL,), reached)


# --- .github/scripts/public_repository_lockdown.py: both of the audit's tokens ------------

ADMIN_TOKEN = (
    "github_pat_11FAKEADMIN00000000000_" + "fakeFleetAdminTokenForTheLockdownGuard" + "0" * 21
)
ISSUES_TOKEN = "ghs_fakeIssuesToken000000000000000000000"


def _lockdown_seam(tree: ast.Module) -> None:
    """Every program the audit starts goes through ``GitHub._api``: driving that one seam
    is driving every command line the script can produce.

    Held from both sides. The script's only way to start a program is ``subprocess.run``,
    and it appears only as the injectable ``run`` default, never called. And the injected
    runner is called exactly once, in ``GitHub._api``, wherever a call could hide (a
    module-level function, a nested or ``async`` one).
    """
    defaults = {
        id(default)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
        for default in (*node.args.defaults, *node.args.kw_defaults)
        if default is not None
    }
    for reference, node in _exec_references(tree):
        allowed = reference == "import subprocess" or (
            reference == "subprocess.run" and id(node) in defaults
        )
        assert allowed, f"the audit can start a program outside GitHub._api: {reference}"
    calls: list[str] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                inner = f"{scope}.{child.name}" if scope else child.name
            if isinstance(child, ast.Call):
                func = child.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in ("run", "_run"):
                    calls.append(scope)
            visit(child, inner)

    visit(tree, "")
    assert calls == ["GitHub._api"], calls


def _drive_lockdown(source: str, tmp_path: Path) -> Run:
    _lockdown_seam(ast.parse(source))
    recorder = ArgvRecorder(tmp_path)
    module_path = tmp_path / "public_repository_lockdown.py"
    module_path.write_text(source)
    spec = importlib.util.spec_from_file_location(f"lockdown_{tmp_path.name}", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    tokens_seen = tmp_path / "tokens"
    recorder.fake(
        "gh",
        f'printf "%s\\n" "$GH_TOKEN" >>"{tokens_seen}"\n'
        # Only a call that hands gh a body reads stdin: a GET inherits the test's own.
        'case " $* " in *" --input - "*) cat >/dev/null ;; esac\n'
        'echo "{}"\n',
    )
    bindir = recorder.install()
    github = module.GitHub(ADMIN_TOKEN, ISSUES_TOKEN)
    with mock.patch.dict(os.environ, {"PATH": f"{bindir}:/usr/bin:/bin"}):
        github._api(ADMIN_TOKEN, "/orgs/basecradle/repos", paginate=True)
        github._api(ISSUES_TOKEN, "/repos/basecradle/x/issues", method="POST", payload={"a": 1})

    def reached() -> None:
        assert recorder.ran("gh"), "gh never ran"
        assert tokens_seen.read_text().split() == [ADMIN_TOKEN, ISSUES_TOKEN]

    return Run(recorder, (ADMIN_TOKEN, ISSUES_TOKEN), reached)


# --- the classification ------------------------------------------------------------------

#: Every shipped script that handles a secret and can start a program, and how it is run.
#: A script with more than one way to reach its secret gets one driver per way.
DRIVEN: dict[str, dict[str, Driver]] = {
    "deploy/smoke-test.sh": {
        "armed": _drive_smoke_test(armed=True),
        "unarmed": _drive_smoke_test(armed=False),
    },
    "deploy/bin/router-admin": {branch: _drive_router_admin(branch) for branch in ("venv", "uv")},
    "deploy/bin/wake-runner": {
        mode: _drive_wake_runner(mode) for mode in ("builder", "harness", "probe")
    },
    ".github/scripts/ping-heartbeat.sh": {"ping": _drive_ping_heartbeat},
    ".github/scripts/public_repository_lockdown.py": {"api": _drive_lockdown},
}

#: Every shipped script that can start a program and handles no secret, with the reason.
#: The tripwire holds each to it.
NO_SECRET: dict[str, str] = {
    ".claude/self-exit.sh": "laptop session lifecycle: signals this session's own claude",
    ".claude/session-guard.sh": "laptop leftovers check: Docker, ports, tabs",
    ".github/scripts/commit-trailers.sh": "CI lint over commit messages",
    ".github/scripts/injection-guard.sh": "CI check for contamination artifacts by filename",
    ".github/scripts/lint-python-scripts.sh": "CI ruff over the extensionless Python helpers",
    ".github/scripts/lint-shell.sh": "CI shellcheck",
    ".github/scripts/lint-workflows.sh": "CI actionlint",
    ".github/scripts/dependency_security_scan.py": (
        "runs osv-scanner over uv.lock with no credential; the workflow's gh steps hold the"
        " App token, not this script"
    ),
    "deploy/drift-check.sh": "git ls-remote of the public repository, with no credential",
    "deploy/bin/reboot-if-required.sh": "stops the router and reboots; reads no credential",
    "deploy/bin/fleet-writers": (
        "reads the registry (no secrets) and the account database, and runs systemctl with a"
        " constant argv; reads no credential"
    ),
    "deploy/verify-recovery.sh": "systemctl and the loopback /up; reads no credential",
    "src/basecradle_router/wake.py": (
        "builds `sudo wake-runner …` from the registry and the event, with an empty"
        " environment; no credential is in its scope, and test_wake.py pins the argv and env"
    ),
    "src/basecradle_router/log_grammar.py": (
        "systemd-cat with a constant argv; the rendered line rides stdin"
    ),
}

#: The vocabulary of a secret source: a variable, a file, or a /proc read that holds one.
SECRET_SOURCE = re.compile(
    r"(?i)secret|token|passw|api[_-]?key|private[_-]?key|signing[_-]?key|heartbeat"
    r"|credential|router\.env|agent\.env|betterstack\.env|/environ\b"
    r"|\bpem\b|_pem|netrc|bearer|authorization|\.ssh/|id_rsa|id_ed25519"
)

_EXEC_ATTRS = re.compile(r"(system|popen|exec\w*|spawn\w*|posix_spawn\w*|fork\w*)\Z")
_EXEC_MODULES = ("subprocess", "pty")


def _exec_references(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """Every way ``tree`` reaches something that starts a program, as (what, where).

    Imports of ``subprocess`` or ``pty`` under any alias, an ``os`` or ``asyncio`` exec
    function imported by name or reached through any alias, an event loop's
    ``subprocess_exec``/``subprocess_shell``, and a module imported by its string name.
    """
    found: list[tuple[str, ast.AST]] = []
    aliases = {"os": "os", "asyncio": "asyncio", "subprocess": "subprocess"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                aliases[alias.asname or root] = root
                if root in _EXEC_MODULES:
                    found.append((f"import {root}", node))
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            for alias in node.names:
                if root in _EXEC_MODULES:
                    found.append((f"from {root} import {alias.name}", node))
                elif root == "os" and _EXEC_ATTRS.match(alias.name):
                    found.append((f"from os import {alias.name}", node))
                elif root == "asyncio" and alias.name.startswith("create_subprocess"):
                    found.append((f"from asyncio import {alias.name}", node))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("import_module", "__import__") and any(
                isinstance(arg, ast.Constant) and str(arg.value).split(".")[0] in _EXEC_MODULES
                for arg in node.args
            ):
                found.append((f"{name}(...)", node))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr in ("subprocess_exec", "subprocess_shell"):
            found.append((f"<loop>.{node.attr}", node))
        if node.attr.startswith("create_subprocess"):
            found.append((f"<...>.{node.attr}", node))
        if isinstance(node.value, ast.Name) and node.value.id in aliases:
            module = aliases[node.value.id]
            if (
                module == "subprocess"
                or (module == "os" and _EXEC_ATTRS.match(node.attr))
                or (module == "asyncio" and node.attr.startswith("create_subprocess"))
            ):
                found.append((f"{module}.{node.attr}", node))
    return found


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, text=True, check=True
    ).stdout
    return [name for name in out.split("\0") if name and (REPO / name).is_file()]


def _is_python(name: str) -> bool:
    if name.endswith(".py"):
        return True
    with open(REPO / name, errors="replace") as handle:
        return bool(re.match(r"#!.*\bpython", handle.readline()))


def _can_start_a_program(tree: ast.Module) -> bool:
    return bool(_exec_references(tree))


def shipped_programs() -> set[str]:
    """Every tracked file that can put something on a command line. Tests are not shipped."""
    shells = {str(path.relative_to(REPO)) for path in shell_scripts()}
    pythons = {
        name
        for name in _tracked()
        if not name.startswith("tests/")
        and _is_python(name)
        and _can_start_a_program(ast.parse((REPO / name).read_text()))
    }
    return {name for name in shells | pythons if not name.startswith("tests/")}


def _code(name: str) -> list[str]:
    """The script's code with its comments and docstrings left out: what it DOES."""
    text = (REPO / name).read_text()
    if not _is_python(name):
        return [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    tree = ast.parse(text)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    parts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            parts.append(node.id)
        elif isinstance(node, ast.Attribute):
            parts.append(node.attr)
        elif isinstance(node, ast.arg):
            parts.append(node.arg)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            parts.append(node.name)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            parts.append(node.value)
    return parts


def test_every_script_that_can_start_a_program_is_classified() -> None:
    found = shipped_programs()
    assert "deploy/bin/wake-runner" in found and "src/basecradle_router/wake.py" in found
    assert not set(DRIVEN) & set(NO_SECRET), "a script is classified twice"
    unclassified = sorted(found - set(DRIVEN) - set(NO_SECRET))
    assert not unclassified, (
        f"{unclassified}: classify each in tests/test_no_secret_on_a_command_line.py — drive"
        " it under the recorder if it handles a secret, else say why it holds none"
    )
    stale = sorted((set(DRIVEN) | set(NO_SECRET)) - found)
    assert not stale, f"{stale} is classified but no longer ships or starts a program"


@pytest.mark.parametrize("name", sorted(NO_SECRET))
def test_a_script_classed_as_holding_no_secret_names_none(name: str) -> None:
    named = sorted({m.group(0) for part in _code(name) for m in SECRET_SOURCE.finditer(part)})
    assert not named, (
        f"{name} names a secret source ({named}): it handles a secret now, so drive it under"
        " the recorder in DRIVEN instead of NO_SECRET"
    )


DRIVES = [(name, label) for name, drivers in DRIVEN.items() for label in drivers]


@needs_shell
@pytest.mark.parametrize(("name", "label"), DRIVES, ids=[f"{n}:{lb}" for n, lb in DRIVES])
def test_no_secret_reaches_a_command_line(name: str, label: str, tmp_path: Path) -> None:
    run = DRIVEN[name][label]((REPO / name).read_text(), tmp_path)

    run.reached()
    assert run.recorder.lines(), "the recorder saw no program at all"
    assert run.recorder.leaks(run.secrets) == []


#: One planted leak per drive, in the shapes #468 names: `env VAR=value`, an argument, a
#: header. Each must be caught, or the recorder has stopped seeing what it claims to.
PLANTED: dict[tuple[str, str], tuple[str, str]] = {
    **{
        ("deploy/smoke-test.sh", state): (
            'sign() { hmac_sha256 "$1" "$secret"; }\n',
            'sign() { grep -c "$secret" /dev/null >/dev/null || true; '
            'hmac_sha256 "$1" "$secret"; }\n',
        )
        for state in ("armed", "unarmed")
    },
    ("deploy/bin/router-admin", "venv"): (
        'exec "$APP_DIR/.venv/bin/python" -m basecradle_router "$@"\n',
        'exec "$APP_DIR/.venv/bin/python" -m basecradle_router "$@"'
        ' --secret "$BASECRADLE_ROUTER_PROBE_WEBHOOK_SECRET"\n',
    ),
    ("deploy/bin/router-admin", "uv"): (
        'run --project "$APP_DIR" python -m basecradle_router "$@"\n',
        'run --env "SECRET=$BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET" --project "$APP_DIR"'
        ' python -m basecradle_router "$@"\n',
    ),
    **{
        ("deploy/bin/wake-runner", mode): (
            '\texec "$@"\n',
            '\texec env "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" "$@"\n',
        )
        for mode in ("harness", "probe")
    },
    # A builder's wake takes the stream's `exec` (#481): the plant goes on that command.
    ("deploy/bin/wake-runner", "builder"): (
        '\t\texec "$@" > >(\n',
        '\t\texec env "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" "$@" > >(\n',
    ),
    (".github/scripts/ping-heartbeat.sh", "ping"): (
        '--config - <<<"url = \\"$url\\""\n',
        '-H "X-Heartbeat: $url" --config - <<<"url = \\"$url\\""\n',
    ),
    (".github/scripts/public_repository_lockdown.py", "api"): (
        "        command.append(path)\n",
        '        command += [path, "-H", f"Authorization: token {token}"]\n',
    ),
}


def test_every_drive_has_a_planted_leak() -> None:
    assert sorted(PLANTED) == sorted(DRIVES)


@needs_shell
@pytest.mark.parametrize(("name", "label"), DRIVES, ids=[f"{n}:{lb}" for n, lb in DRIVES])
def test_the_guard_catches_a_planted_leak(name: str, label: str, tmp_path: Path) -> None:
    old, new = PLANTED[(name, label)]
    source = (REPO / name).read_text()
    assert source.count(old) == 1, f"the planting site moved in {name}: {old!r}"
    run = DRIVEN[name][label](source.replace(old, new), tmp_path)

    assert run.recorder.leaks(run.secrets), f"a secret planted on a command line in {name} passed"


# --- workflows ---------------------------------------------------------------------------

WORKFLOWS = REPO / ".github" / "workflows"

#: An expression that evaluates to a secret.
#: An expression that reads a secret, in any form: `secrets.X`, `secrets['X']`, a default
#: (`secrets.X || ''`), `toJSON(secrets)`, the job token, a step's minted token.
SECRET_EXPR = re.compile(
    r"\$\{\{[^}]*?(?:\bsecrets\b|\bgithub\.token\b|\.outputs\.token\b)[^}]*\}\}"
)
_KEY = re.compile(r"^(?P<indent>\s*)(?P<dash>- )?(?P<key>[\w-]+):(?P<rest>.*)$")
#: A block scalar's indicator (`|`, `>-`, `|2`), with or without a trailing comment.
_BLOCK = re.compile(r"[|>][0-9+-]*\s*(#.*)?")
#: The places GitHub hands a secret over as environment, never as script text.
_HANDOVER = ("env", "with", "secrets")


def workflow_leaks(text: str) -> list[str]:
    """Every way ``text`` (one workflow) could put a secret on a command line.

    A secret may enter a step only as a whole ``KEY: ${{ <secret> }}`` entry of an ``env:``
    map or an action's inputs (``with:``, or a reusable workflow's ``secrets:``): GitHub
    hands both over as environment, never argv. Any OTHER line that carries a secret
    expression is a leak, whatever it is: a ``run:`` body, a block scalar, a plain
    scalar's continuation line, a flow map. The rule is stated as an allow-list so a shape
    this parser does not recognise is refused rather than waved through. And a variable
    that carries one may be expanded in a ``run:`` body only to test that it is set: every
    other use hands the value to a program, which is how `curl "$HEARTBEAT_URL"` put the
    heartbeat URL on curl's argv.
    """
    leaks: list[str] = []
    carriers: set[str] = set()
    stack: list[tuple[int, str]] = []
    block: int | None = None  # the indent of the key whose block scalar we are inside
    for number, line in enumerate(text.splitlines(), 1):
        indent = len(line) - len(line.lstrip())
        inside = block is not None and (not line.strip() or indent > block)
        match = None if inside else _KEY.match(line)
        if not inside:
            block = None
        allowed = False
        if match is not None and not line.lstrip().startswith("#"):
            depth = len(match["indent"]) + (2 if match["dash"] else 0)
            while stack and stack[-1][0] >= depth:
                stack.pop()
            parent = stack[-1][1] if stack else ""
            stack.append((depth, match["key"]))
            rest = match["rest"].strip()
            if _BLOCK.fullmatch(rest):
                block = depth
            if parent in _HANDOVER and re.fullmatch(SECRET_EXPR.pattern, rest):
                allowed = True
                if parent == "env":
                    carriers.add(match["key"])
        if SECRET_EXPR.search(line) and not allowed:
            leaks.append(f"{number}: a secret outside an env:/with: entry: {line.strip()}")
    for name in sorted(carriers):
        for number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue  # a comment is no command line
            rest = re.sub(rf'(?:test|\[) -n "\${name}"', "", line)
            if re.search(rf"\$\{{?{name}\b|\benv\.{name}\b", rest):
                leaks.append(f"{number}: {name} carries a secret and is expanded: {line.strip()}")
    return leaks


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.y*ml")), ids=lambda p: p.name)
def test_no_workflow_puts_a_secret_on_a_command_line(path: Path) -> None:
    assert workflow_leaks(path.read_text()) == []


def test_the_workflow_rule_sees_every_secret_the_workflows_carry() -> None:
    # The rule passes trivially if it never recognised a carrier; these are the ones the
    # workflows really hold, so a parser that went blind fails here.
    text = "\n".join(path.read_text() for path in sorted(WORKFLOWS.glob("*.y*ml")))
    for carrier in ("HEARTBEAT_URL", "ADMIN_TOKEN", "ISSUES_TOKEN", "GH_TOKEN"):
        assert re.search(rf"^\s+{carrier}: \$\{{\{{", text, re.M), carrier
    planted = text.replace('test -n "$ADMIN_TOKEN"', 'curl -H "Authorization: $ADMIN_TOKEN"')
    assert any("ADMIN_TOKEN carries a secret" in leak for leak in workflow_leaks(planted))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "            .github/scripts/ping-heartbeat.sh\n",
            '            curl -fsS -o /dev/null "$HEARTBEAT_URL"\n',
        ),
        (
            "            .github/scripts/ping-heartbeat.sh\n",
            '            curl -fsS -o /dev/null "${{ secrets.DEPENDENCY_SCAN_HEARTBEAT_URL }}"\n',
        ),
        (
            "            .github/scripts/ping-heartbeat.sh\n",
            '            curl -fsS -o /dev/null "${{ env.HEARTBEAT_URL }}"\n',
        ),
        (
            "          GH_TOKEN: ${{ steps.app-token.outputs.token }}\n        run: gh issue list",
            "          REPO: basecradle\n        run: GH_TOKEN=${{ steps.app-token.outputs.token }}"
            " gh issue list",
        ),
    ],
    ids=["expanded-variable", "secret-expression", "env-expression", "inline-assignment"],
)
def test_the_workflow_rule_catches_a_planted_leak(old: str, new: str) -> None:
    text = (WORKFLOWS / "dependency-security-scan.yml").read_text()
    assert text.count(old) >= 1, old
    assert workflow_leaks(text.replace(old, new, 1))


STEP = "jobs:\n  ping:\n    steps:\n      - name: Ping\n"


@pytest.mark.parametrize(
    "body",
    [
        '        run: | # ping it\n          curl "${{ secrets.HB }}"\n',
        '        run: |2\n          curl "${{ secrets.HB }}"\n',
        '        run: curl -fsS\n          "${{ secrets.HB }}"\n',
        '        env: { HB: "${{ secrets.HB }}" }\n        run: true\n',
        "        with:\n          args: --token ${{ secrets.HB }}\n",
    ],
    ids=["commented-indicator", "indented-indicator", "plain-continuation", "flow-map", "partial"],
)
def test_the_workflow_rule_refuses_a_shape_it_does_not_recognise(body: str) -> None:
    # The allow-list is what makes these fail: none is a whole `KEY: ${{ secret }}` entry
    # of an env: or with: map, so each is refused without the parser having to know it.
    assert workflow_leaks(STEP + body)


def test_the_workflow_rule_allows_exactly_a_handed_over_secret() -> None:
    body = (
        "        env:\n          HB: ${{ secrets.HB }}\n"
        "        with:\n          private-key: ${{ secrets.KEY }}\n"
        '        run: |\n          test -n "$HB" || exit 1\n'
        "          .github/scripts/ping-heartbeat.sh\n"
    )
    assert workflow_leaks(STEP + body) == []


#: Every literal system program path a driven shell script names in its code, and why the
#: recorder still sees its command line. The recorder sees a program only through PATH or a
#: placed shim, so a new one must be accounted for here. A program held in a variable is the
#: driver's to place: `$smoke_python` (SMOKE_TRUSTED_PATH), wake-runner's `$launch_bin`,
#: `$systemd_cat` and probe mode's python3 (TRUSTED_PATH), router-admin's venv python and
#: `uv` (both placed, both driven).
ABSOLUTE_PROGRAMS: dict[str, dict[str, str]] = {
    "deploy/smoke-test.sh": {
        "/bin/sh": "runs under runuser, a recorded shim: its argv is runuser's"
    },
    "deploy/bin/wake-runner": {
        "/bin/bash": "runs under runuser, a recorded shim: its argv is runuser's",
    },
    "deploy/bin/router-admin": {},
    ".github/scripts/ping-heartbeat.sh": {},
}

_ABSOLUTE = re.compile(r"(?<![\w./$-])/(?:usr/)?(?:local/)?s?bin/[\w.+-]+")


def test_every_absolute_program_a_driven_script_runs_is_accounted_for() -> None:
    shells = {name for name in DRIVEN if not _is_python(name)}
    assert sorted(ABSOLUTE_PROGRAMS) == sorted(shells)
    for name in sorted(shells):
        named = {m.group(0) for line in _code(name) for m in _ABSOLUTE.finditer(line)}
        assert named == set(ABSOLUTE_PROGRAMS[name]), (name, sorted(named))


@pytest.mark.parametrize(
    "snippet",
    [
        "import subprocess",
        "import subprocess as sp",
        "from subprocess import run",
        "import pty",
        "from os import system",
        "from os import execv",
        "import os as _os\n_os.execv('/bin/true', ['true'])",
        "import os\nos.posix_spawn('/bin/true', ['true'], {})",
        "from asyncio import create_subprocess_exec",
        "import asyncio\nasyncio.create_subprocess_shell('true')",
        "loop.subprocess_exec(factory, 'true')",
        "import importlib\nimportlib.import_module('subprocess')",
        "__import__('pty')",
    ],
)
def test_every_way_python_starts_a_program_is_discovered(snippet: str) -> None:
    assert _can_start_a_program(ast.parse(snippet)), snippet


def test_a_file_that_cannot_start_a_program_is_not_discovered() -> None:
    assert not _can_start_a_program(ast.parse("import os\nos.environ.get('X')\nos.path.join"))


@pytest.mark.parametrize(
    "planted",
    [
        "\n\ndef leak(run=subprocess.run):\n    run(['gh', 'auth', 'login'])\n",
        "\n\nasync def leak(self):\n    self._run(['gh'])\n",
        "\n\ndef leak():\n    subprocess.check_output(['gh'])\n",
        "\n\nimport os\n\n\ndef leak():\n    os.system('gh')\n",
    ],
    ids=["free-function", "async-method", "check-output", "os-system"],
)
def test_the_lockdown_seam_refuses_a_second_way_to_start_a_program(planted: str) -> None:
    source = (REPO / ".github" / "scripts" / "public_repository_lockdown.py").read_text()
    with pytest.raises(AssertionError):
        _lockdown_seam(ast.parse(source + planted))


@pytest.mark.parametrize(
    "line",
    [
        "        run: curl \"${{ secrets.HB || '' }}\"\n",
        "        run: curl \"${{ secrets['HB'] }}\"\n",
        "        run: echo '${{ toJSON(secrets) }}'\n",
        '        run: gh api /x -H "Authorization: token ${{ github.token }}"\n',
    ],
    ids=["default", "index", "to-json", "job-token"],
)
def test_the_workflow_rule_sees_a_secret_in_any_expression(line: str) -> None:
    assert workflow_leaks(STEP + line)


# --- the other places a command line is written: units and session hooks ------------------


def test_no_unit_expands_a_value_into_a_command_line() -> None:
    """systemd expands `$VAR`/`${VAR}` from the unit's environment (router.env included)
    into an `Exec*=` line, and the result is the process's argv for its whole life. So no
    shipped unit's command line expands anything; a value it needs rides the environment."""
    units = [name for name in _tracked() if name.startswith("deploy/") and "/systemd/" in name]
    assert any(name.endswith("basecradle-router.service") for name in units), units
    for name in units:
        lines = (REPO / name).read_text().splitlines()
        exec_lines: list[str] = []
        for index, line in enumerate(lines):
            if re.match(r"\s*Exec\w*=", line):
                exec_lines.append(line)
                while line.rstrip().endswith("\\") and index + 1 < len(lines):
                    index += 1
                    line = lines[index]
                    exec_lines.append(line)
        assert not [line for line in exec_lines if "$" in line], name


def test_the_session_hooks_handle_no_secret() -> None:
    """The one other command line this repository writes: the Claude Code hooks in
    .claude/settings.json, run on the laptop. They take a session baseline and name no
    secret; one that did would be a script to drive, not a hook to wave through."""
    settings = json.loads((REPO / ".claude" / "settings.json").read_text())
    commands = [
        hook["command"]
        for entries in settings.get("hooks", {}).values()
        for entry in entries
        for hook in entry.get("hooks", [])
        if hook.get("type") == "command"
    ]
    assert commands, "the session-guard baseline hook is expected"
    for command in commands:
        assert not SECRET_SOURCE.search(command), command

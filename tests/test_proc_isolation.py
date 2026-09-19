"""A wake cannot read another account's argv — pinned offline (#271, basecradle#545).

The box mounts /proc with ``hidepid=invisible`` so no account can read another's argv
(basecradle-noc#694). That control never reached an agent session: the router unit's
sandbox gives it a private mount namespace with a fresh procfs instance, a procfs
instance carries its own mount options, and every wake is a child of the unit. The
unit now mounts that instance with ``ProtectProc=invisible``.

Two halves are pinned here, and both can be lost to a one-line edit that nothing else
would notice:

- **The unit files.** The router unit declares ``ProtectProc=invisible``, and every unit
  this repo ships that turns on systemd's mount-namespace sandbox says what its /proc is.
  Which directive implies a fresh procfs instance is a systemd detail
  (``ProtectKernelTunables`` and ``ProtectControlGroups`` do today), so the rule is stated
  in terms that do not depend on it.
- **The live gate** — ``deploy/smoke-test.sh`` case 9, which looks inside the running
  unit's namespace on every deploy. Its EXACT shipped bodies are run here against a fake
  ``systemctl``/``nsenter`` and fabricated /proc trees. The gate must pass only on
  ``hidden``, and fail on everything else, including the verdicts that would read as a
  pass if the gate were careless (no verdict, a probe that never reached /proc).

No model, agent, network, or privileged call is touched.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SYSTEMD = _ROOT / "deploy" / "systemd"
ROUTER_UNIT = _SYSTEMD / "basecradle-router.service"
SMOKE_TEST = _ROOT / "deploy" / "smoke-test.sh"

# The directives that give a unit its own mount namespace (systemd.exec(5)). A unit
# that sets any of them gets a /proc of systemd's making rather than the host's, so it
# must say which one.
_MOUNT_NAMESPACING = frozenset(
    {
        "RootDirectory", "RootImage", "MountAPIVFS", "PrivateMounts",
        "ProtectSystem", "ProtectHome", "PrivateTmp", "PrivateDevices",
        "ProtectKernelTunables", "ProtectKernelModules", "ProtectKernelLogs",
        "ProtectControlGroups", "ProtectHostname",
        "ReadWritePaths", "ReadOnlyPaths", "InaccessiblePaths", "ExecPaths", "NoExecPaths",
        "TemporaryFileSystem", "BindPaths", "BindReadOnlyPaths", "ProcSubset",
    }
)  # fmt: skip
_OFF = frozenset({"", "no", "false", "0", "off"})

# A pid no kernel can hand out (pid_max tops out at 2^22), so /proc/<pid> never exists
# on the machine running these tests and the diagnosis path reads `unknown`.
FAKE_PID = "99999999"


def _directives(unit: Path) -> dict[str, str]:
    """Every `Key=value` in a unit file, last one wins (comments ignored)."""
    found: dict[str, str] = {}
    for line in unit.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", ";")):
            continue
        m = re.match(r"([A-Za-z]+)\s*=\s*(.*)$", stripped)
        if m:
            found[m.group(1)] = m.group(2).strip()
    return found


def _shipped_units() -> list[Path]:
    return sorted(_SYSTEMD.glob("*.service")) + sorted(_SYSTEMD.glob("*.service.d/*.conf"))


def _sandboxed(unit: Path) -> bool:
    return any(
        key in _MOUNT_NAMESPACING and value.lower() not in _OFF
        for key, value in _directives(unit).items()
    )


def test_the_router_unit_hides_other_accounts_processes() -> None:
    assert _directives(ROUTER_UNIT).get("ProtectProc") == "invisible", (
        "basecradle-router.service must set ProtectProc=invisible, or every wake can read "
        "every other account's argv from /proc/<pid>/cmdline (#271)"
    )


def test_every_sandboxed_unit_declares_its_proc() -> None:
    """A private mount namespace never falls back to a plain /proc by omission."""
    sandboxed = [unit for unit in _shipped_units() if _sandboxed(unit)]
    # Non-vacuity: the router unit is sandboxed today, so a parser that stopped seeing
    # directives would fail here rather than pass every unit by finding none.
    assert ROUTER_UNIT in sandboxed
    missing = [
        unit.relative_to(_ROOT).as_posix()
        for unit in sandboxed
        if _directives(unit).get("ProtectProc") != "invisible"
    ]
    assert not missing, (
        f"these units run in a private mount namespace without ProtectProc=invisible, so "
        f"the host's hidepid does not reach them: {missing}"
    )


# --- the live gate, smoke-test.sh case 9 --------------------------------------------

_needs_bash = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to exercise the smoke-test bodies"
)


def _extract(marker: str) -> str:
    """Pull a real body out of the shipped smoke test, between its markers."""
    match = re.search(
        rf"[ \t]*# >>> {marker} >>>\n(.*?)[ \t]*# <<< {marker} <<<\n",
        SMOKE_TEST.read_text(),
        re.DOTALL,
    )
    assert match, f"{marker} marker block not found in deploy/smoke-test.sh"
    return match.group(1)


def _probe() -> str:
    """The exact probe script the gate hands the principal inside the namespace."""
    match = re.search(r"FOREIGN_ARGV_PROBE='(.*?)'", _extract("foreign_argv_probe"), re.DOTALL)
    assert match, "FOREIGN_ARGV_PROBE is not a single-quoted literal in deploy/smoke-test.sh"
    return match.group(1)


def _fakes(tmp_path: Path) -> Path:
    """A `systemctl` that answers from the environment and an `nsenter` that records."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    systemctl = bindir / "systemctl"
    systemctl.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            case "$*" in
            *--property=MainPID*) printf '%s\\n' "$FAKE_MAINPID" ;;
            *--property=User*) printf '%s\\n' "$FAKE_USER" ;;
            *) exit 1 ;;
            esac
            """
        )
    )
    nsenter = bindir / "nsenter"
    nsenter.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\0' "$@" >"$FAKE_NSENTER_ARGV"
            [[ -n $FAKE_STDERR ]] && printf '%s\\n' "$FAKE_STDERR" >&2
            printf '%b' "$FAKE_STDOUT"
            exit "$FAKE_NSENTER_RC"
            """
        )
    )
    for fake in (systemctl, nsenter):
        fake.chmod(0o755)
    return bindir


def _gate(
    tmp_path: Path,
    *,
    stdout: str = "hidden\n",
    stderr: str = "",
    nsenter_rc: int = 0,
    mainpid: str = FAKE_PID,
    user: str = "router",
) -> tuple[subprocess.CompletedProcess[str], list[str] | None]:
    """Run the shipped case 9 once; return its result and nsenter's argv (None if unused)."""
    bindir = _fakes(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir()
    argv_file = tmp_path / "nsenter.argv"
    script = tmp_path / "gate.sh"
    preamble = textwrap.dedent(
        f"""\
        set -euo pipefail
        rc=0
        workdir="{workdir}"
        green() {{ printf 'PASS %s\\n' "$*"; }}
        red() {{ printf 'FAIL %s\\n' "$*"; }}
        """
    )
    coda = 'assert_foreign_argv_hidden "case 9" basecradle-router\nexit $rc\n'
    script.write_text(preamble + _extract("foreign_argv_probe") + _extract("proc_isolation") + coda)
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "FAKE_MAINPID": mainpid,
            "FAKE_USER": user,
            "FAKE_STDOUT": stdout,
            "FAKE_STDERR": stderr,
            "FAKE_NSENTER_RC": str(nsenter_rc),
            "FAKE_NSENTER_ARGV": str(argv_file),
        },
    )
    argv = argv_file.read_text().split("\0")[:-1] if argv_file.exists() else None
    return result, argv


@_needs_bash
def test_the_gate_passes_only_when_pid_1_is_invisible(tmp_path: Path) -> None:
    result, argv = _gate(tmp_path, stdout="hidden\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout
    # It looked inside the RUNNING unit's mount namespace, as the unit's own User=, with
    # the same runuser drop a real wake takes, running the shipped probe verbatim.
    assert argv == [
        "--target", FAKE_PID, "--mount", "--",
        "runuser", "-u", "router", "--", "/bin/sh", "-c", _probe(),
    ]  # fmt: skip


@_needs_bash
@pytest.mark.parametrize(
    ("verdict", "says"),
    [
        ("readable", "can read PID 1's argv"),
        ("listed", "can see PID 1"),
    ],
)
def test_the_gate_fails_when_the_namespace_exposes_pid_1(
    verdict: str, says: str, tmp_path: Path
) -> None:
    result, _ = _gate(tmp_path, stdout=f"{verdict}\n")
    assert result.returncode == 1
    assert f"case 9: router {says} inside basecradle-router's namespace" in result.stdout
    # ...and names what it found mounted there, so the failure says what to go fix.
    assert "that namespace's /proc is mounted unknown;" in result.stdout
    assert "ProtectProc=invisible" in result.stdout


@_needs_bash
@pytest.mark.parametrize(
    "stdout",
    [
        "no-proc\n",  # the principal could not read its own entry: nothing was proven
        "",  # no verdict at all
        "runuser: warning\nhidden\n",  # a verdict is exact, never merely contained
        "HIDDEN\n",
    ],
)
def test_the_gate_never_reads_anything_else_as_a_pass(stdout: str, tmp_path: Path) -> None:
    result, _ = _gate(tmp_path, stdout=stdout)
    assert result.returncode == 1, result.stdout
    assert "could not look inside basecradle-router's namespace as router" in result.stdout


@_needs_bash
def test_the_gate_fails_naming_why_it_could_not_enter(tmp_path: Path) -> None:
    reason = "nsenter: reassociate to namespace 'ns/mnt' failed: Operation not permitted"
    result, _ = _gate(tmp_path, stdout="", stderr=reason, nsenter_rc=1)
    assert result.returncode == 1
    assert reason in result.stdout


@_needs_bash
@pytest.mark.parametrize("mainpid", ["0", "", "not-a-pid"])
def test_the_gate_fails_without_a_running_unit_to_enter(mainpid: str, tmp_path: Path) -> None:
    result, argv = _gate(tmp_path, mainpid=mainpid)
    assert result.returncode == 1
    assert "has no main process to look inside" in result.stdout
    assert argv is None  # it never tried to enter anything


@_needs_bash
@pytest.mark.parametrize("user", ["", "root"])
def test_the_gate_fails_for_a_unit_hidepid_cannot_restrict(user: str, tmp_path: Path) -> None:
    """A root unit would pass the probe vacuously: hidepid never restricts root."""
    result, argv = _gate(tmp_path, user=user)
    assert result.returncode == 1
    assert "runs as root, which hidepid never restricts" in result.stdout
    assert argv is None


def test_case_9_is_asserted_unconditionally() -> None:
    """Called at the top level, never inside a self-gating branch (#271)."""
    calls = re.findall(r"^\s*assert_foreign_argv_hidden\s", SMOKE_TEST.read_text(), re.MULTILINE)
    top_level = re.findall(r"^assert_foreign_argv_hidden\s", SMOKE_TEST.read_text(), re.MULTILINE)
    assert len(calls) == len(top_level) == 1


# --- the probe itself ----------------------------------------------------------------


def _run_probe(proc: str) -> str:
    """Run the shipped probe with its /proc paths pointed at `proc`."""
    script = _probe().replace("/proc/", f"{proc}/")
    return subprocess.run(
        ["/bin/sh", "-c", script], capture_output=True, text=True, check=True
    ).stdout.strip()


def _proc_tree(tmp_path: Path, *, self_entry: bool, pid_1: str | None) -> Path:
    """A fabricated /proc: `self/cmdline`, and PID 1 absent, readable, or unreadable."""
    proc = tmp_path / "proc"
    proc.mkdir()
    if self_entry:
        (proc / "self").mkdir()
        (proc / "self" / "cmdline").write_bytes(b"sh\0")
    if pid_1 is not None:
        (proc / "1").mkdir()
        cmdline = proc / "1" / "cmdline"
        cmdline.write_bytes(b"/sbin/init\0")
        if pid_1 == "unreadable":
            cmdline.chmod(0)
    return proc


@pytest.mark.parametrize(
    ("self_entry", "pid_1", "verdict"),
    [
        (True, None, "hidden"),
        (True, "readable", "readable"),
        (True, "unreadable", "listed"),
        (False, None, "no-proc"),
        (False, "readable", "no-proc"),  # the control comes first, whatever PID 1 shows
    ],
)
def test_the_probe_names_each_view_it_can_find(
    self_entry: bool, pid_1: str | None, verdict: str, tmp_path: Path
) -> None:
    if pid_1 == "unreadable" and os.geteuid() == 0:
        pytest.skip("root reads a mode-0 file, so the unreadable fixture cannot be built")
    proc = _proc_tree(tmp_path, self_entry=self_entry, pid_1=pid_1)
    assert _run_probe(str(proc)) == verdict


def test_the_probe_agrees_with_this_machines_real_proc() -> None:
    """The shipped probe, unmodified, against the real /proc of whatever runs the tests.

    Linux CI has no hidepid, so this is where `readable` is proven on a real procfs rather
    than a fabricated tree; without a /proc at all (macOS) it must say `no-proc`.
    """
    if not os.path.exists("/proc/self/cmdline"):
        expected = "no-proc"
    elif os.access("/proc/1/cmdline", os.R_OK):
        expected = "readable"
    elif os.path.exists("/proc/1"):
        expected = "listed"
    else:
        expected = "hidden"
    assert _run_probe("/proc") == expected


# --- the diagnosis -------------------------------------------------------------------

HOST_PROC = (
    "25 22 0:22 / /proc rw,nosuid,nodev,noexec,relatime shared:13 - proc proc "
    "rw,hidepid=invisible,gid=985"
)
NAMESPACE_PROC = "900 22 0:61 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw"


@_needs_bash
@pytest.mark.parametrize(
    ("lines", "options"),
    [
        ([HOST_PROC], "rw,hidepid=invisible,gid=985"),
        # A read-only bind of /proc/sys shares the fs type but is not the /proc mount.
        (
            [
                NAMESPACE_PROC,
                "901 900 0:61 /sys /proc/sys ro,nosuid,nodev,noexec,relatime - proc proc rw",
            ],
            "rw",
        ),
        ([HOST_PROC, NAMESPACE_PROC], "rw"),  # stacked: the one listed last is on top
        (["30 22 0:40 / /proc rw,relatime - tmpfs tmpfs rw"], "unknown"),  # not a procfs
        (["22 1 259:1 / / rw,relatime shared:1 - ext4 /dev/root rw,discard"], "unknown"),
    ],
)
def test_the_diagnosis_reads_the_proc_mount_options(
    lines: list[str], options: str, tmp_path: Path
) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("\n".join(lines) + "\n")
    script = "set -euo pipefail\n" + _extract("proc_isolation") + 'proc_mount_options "$1"\n'
    result = subprocess.run(
        ["bash", "-c", script, "gate", str(mountinfo)], capture_output=True, text=True, check=True
    )
    assert result.stdout == options


@_needs_bash
def test_the_diagnosis_is_unknown_when_it_cannot_read(tmp_path: Path) -> None:
    script = "set -euo pipefail\n" + _extract("proc_isolation") + 'proc_mount_options "$1"\n'
    result = subprocess.run(
        ["bash", "-c", script, "gate", str(tmp_path / "absent")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "unknown"

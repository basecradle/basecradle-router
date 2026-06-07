"""The OS-update reboot orchestration (issue #66), tested at the boundary.

``deploy/reboot-if-required.sh`` and ``deploy/verify-recovery.sh`` are ops bash,
so they are exercised the way the rest of the suite mocks at the edge: the real
``systemctl``/``curl``/reboot are replaced with recording stubs on ``PATH``, and
the scripts run under that fake environment. No service is touched, nothing
reboots, no network is hit — only the orchestration *logic* is asserted.

Stub behaviour is driven by env vars the stubs read at run time:
  * ``systemctl`` — logs every call; ``is-active --quiet <svc>`` exits non-zero
    for any svc listed in ``INACTIVE_SERVICES``; ``stop`` exits 1 if ``STOP_FAILS=1``;
    ``reboot`` records a ``REBOOT`` line.
  * ``curl`` — logs the call, prints ``CURL_BODY``, exits ``CURL_EXIT``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REBOOT_SCRIPT = REPO_ROOT / "deploy" / "reboot-if-required.sh"
RECOVERY_SCRIPT = REPO_ROOT / "deploy" / "verify-recovery.sh"

# The exact green liveness body the app serves and the recovery script asserts.
GREEN_BODY = '<!DOCTYPE html><html><body style="background-color: green"></body></html>'

_SYSTEMCTL_STUB = """#!/usr/bin/env bash
echo "systemctl $*" >>"$STUB_LOG"
case "$1" in
  is-active)
    svc="${@: -1}"
    for bad in $INACTIVE_SERVICES; do
      [[ "$svc" == "$bad" ]] && exit 3
    done
    exit 0 ;;
  stop)
    [[ "${STOP_FAILS:-0}" == "1" ]] && exit 1
    exit 0 ;;
  reboot)
    echo "REBOOT" >>"$STUB_LOG"
    exit 0 ;;
  *) exit 0 ;;
esac
"""

_CURL_STUB = """#!/usr/bin/env bash
echo "curl $*" >>"$STUB_LOG"
[[ "${CURL_EXIT:-0}" != "0" ]] && exit "${CURL_EXIT}"
printf '%s' "${CURL_BODY:-}"
exit 0
"""


def _make_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)


def _run(script: Path, tmp_path, extra_env: dict[str, str]):
    """Run a script with stub systemctl+curl on PATH; return (CompletedProcess, log)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _make_stub(bin_dir, "systemctl", _SYSTEMCTL_STUB)
    _make_stub(bin_dir, "curl", _CURL_STUB)
    log = tmp_path / "calls.log"
    log.write_text("")

    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "INACTIVE_SERVICES": "",
        **extra_env,
    }
    proc = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=30
    )
    return proc, log.read_text()


# --- reboot-if-required.sh -------------------------------------------------


def test_no_reboot_required_is_a_noop(tmp_path) -> None:
    # The flag file is absent → nothing drains, nothing reboots, clean exit.
    proc, calls = _run(
        REBOOT_SCRIPT,
        tmp_path,
        {
            "SKIP_ROOT_CHECK": "1",
            "REBOOT_REQUIRED_FILE": str(tmp_path / "nope"),
        },
    )
    assert proc.returncode == 0
    assert "REBOOT" not in calls
    assert "systemctl stop" not in calls


def test_reboot_required_drains_then_reboots(tmp_path) -> None:
    # The flag file is present → drain the router first, THEN reboot, in that order.
    flag = tmp_path / "reboot-required"
    flag.write_text("")
    proc, calls = _run(
        REBOOT_SCRIPT,
        tmp_path,
        {
            "SKIP_ROOT_CHECK": "1",
            "REBOOT_REQUIRED_FILE": str(flag),
            "ROUTER_SERVICE": "basecradle-router",
        },
    )
    assert proc.returncode == 0
    assert "systemctl stop basecradle-router" in calls
    assert "REBOOT" in calls
    # Drain must precede the reboot — never sever a wake we could have let finish.
    assert calls.index("systemctl stop") < calls.index("REBOOT")


def test_reboot_proceeds_even_if_drain_fails(tmp_path) -> None:
    # A stuck unit must not pin an unpatched kernel forever: a stop failure is
    # logged but the reboot still happens.
    flag = tmp_path / "reboot-required"
    flag.write_text("")
    proc, calls = _run(
        REBOOT_SCRIPT,
        tmp_path,
        {
            "SKIP_ROOT_CHECK": "1",
            "REBOOT_REQUIRED_FILE": str(flag),
            "STOP_FAILS": "1",
        },
    )
    assert proc.returncode == 0
    assert "systemctl stop" in calls
    assert "REBOOT" in calls


# --- verify-recovery.sh ----------------------------------------------------


def _recovery_env(**overrides) -> dict[str, str]:
    env = {
        "SERVICES": "caddy basecradle-router",
        "UP_URL": "http://127.0.0.1:8000/up",
        "RETRIES": "2",
        "RETRY_INTERVAL": "0",  # do not actually sleep in tests
        "CURL_BODY": GREEN_BODY,
    }
    env.update(overrides)
    return env


def test_recovery_passes_when_services_active_and_up_green(tmp_path) -> None:
    proc, calls = _run(RECOVERY_SCRIPT, tmp_path, _recovery_env())
    assert proc.returncode == 0, proc.stderr
    assert "systemctl is-active --quiet caddy" in calls
    assert "systemctl is-active --quiet basecradle-router" in calls


def test_recovery_fails_when_a_service_is_inactive(tmp_path) -> None:
    proc, _ = _run(
        RECOVERY_SCRIPT,
        tmp_path,
        _recovery_env(INACTIVE_SERVICES="basecradle-router"),
    )
    assert proc.returncode == 1


def test_recovery_fails_when_up_body_is_wrong(tmp_path) -> None:
    # Services up, but /up serves the wrong bytes (a wedged or stale app).
    proc, _ = _run(RECOVERY_SCRIPT, tmp_path, _recovery_env(CURL_BODY="not green"))
    assert proc.returncode == 1


def test_recovery_fails_when_up_is_unreachable(tmp_path) -> None:
    # curl itself fails (connection refused → exit 7): the app never came back.
    proc, _ = _run(RECOVERY_SCRIPT, tmp_path, _recovery_env(CURL_EXIT="7"))
    assert proc.returncode == 1

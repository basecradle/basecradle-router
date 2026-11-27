"""The OS-update reboot orchestration (issue #66), tested at the boundary.

``deploy/bin/reboot-if-required.sh`` and ``deploy/verify-recovery.sh`` are ops bash,
so they are exercised the way the rest of the suite mocks at the edge: the real
``systemctl``/``curl``/reboot are replaced with recording stubs on ``PATH``, and
the scripts run under that fake environment. No service is touched, nothing
reboots, no network is hit — only the orchestration *logic* is asserted.

Stub behaviour is driven by env vars the stubs read at run time:
  * ``systemctl`` — logs every call; ``is-active --quiet <svc>`` exits non-zero
    for any svc listed in ``INACTIVE_SERVICES``; ``show … -p ActiveState`` prints
    ``ROUTER_STATE`` (default ``active``) and ``show … -p SubState`` prints
    ``ROUTER_SUBSTATE`` (default ``running``); ``stop`` exits 1 if ``STOP_FAILS=1``;
    ``reboot`` records a ``REBOOT`` line.
  * ``curl`` — logs the call, prints ``CURL_BODY``, exits ``CURL_EXIT``.
  * ``router-admin`` (the reboot script's ``ROUTER_ADMIN``) — logs ``pending -> <its
    reading> as <user>``, prints the file ``PENDING_FILE``, writes ``PENDING_STDERR``
    to stderr and exits ``PENDING_EXIT``; so the router's pending wakes are a file a
    test rewrites.
  * ``setpriv`` — logs the drop it was asked for, then runs its command as is.
  * ``sleep`` — logs ``sleep`` and returns at once; when ``AFTER_SLEEP_<n>`` names a
    file, the ``n``-th sleep copies it over ``PENDING_FILE`` (a wake ending mid-wait).
  * ``timeout`` — runs its command (the bound is the box's business, not the test's).
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REBOOT_SCRIPT = REPO_ROOT / "deploy" / "bin" / "reboot-if-required.sh"
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
  show)
    if [[ "$*" == *SubState* ]]; then
      printf '%s\\n' "${ROUTER_SUBSTATE-running}"
    else
      printf '%s\\n' "${ROUTER_STATE-active}"
    fi
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


_ROUTER_ADMIN_STUB = """#!/usr/bin/env bash
[[ "$*" == pending ]] || { echo "router-admin stub: unexpected $*" >&2; exit 64; }
reading="$(cat "$PENDING_FILE" 2>/dev/null || true)"
echo "pending -> ${reading} as ${STUB_USER:-root}" >>"$STUB_LOG"
printf '%s\\n' "$reading"
[[ -z "${PENDING_STDERR:-}" ]] || printf '%s\\n' "$PENDING_STDERR" >&2
exit "${PENDING_EXIT:-0}"
"""

_SETPRIV_STUB = """#!/usr/bin/env bash
drop=()
while [[ "$1" != -- ]]; do drop+=("$1"); shift; done
shift
echo "setpriv ${drop[*]}" >>"$STUB_LOG"
for flag in "${drop[@]}"; do [[ "$flag" == --reuid=* ]] && export STUB_USER="${flag#--reuid=}"; done
exec "$@"
"""

_SLEEP_STUB = """#!/usr/bin/env bash
echo "sleep $*" >>"$STUB_LOG"
n=$(grep -c '^sleep' "$STUB_LOG")
next="AFTER_SLEEP_${n}"
if [[ -n "${!next:-}" ]]; then cp "${!next}" "$PENDING_FILE"; fi
exit 0
"""

_TIMEOUT_STUB = """#!/usr/bin/env bash
while [[ "$1" == -* ]]; do shift; done
shift
exec "$@"
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
    _make_stub(bin_dir, "router-admin", _ROUTER_ADMIN_STUB)
    _make_stub(bin_dir, "sleep", _SLEEP_STUB)
    _make_stub(bin_dir, "timeout", _TIMEOUT_STUB)
    _make_stub(bin_dir, "setpriv", _SETPRIV_STUB)
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


# --- bin/reboot-if-required.sh -------------------------------------------------

IDLE = "0"
BUSY = "1 nova=1"
DAY = 24 * 3600


def _reboot(tmp_path, pending: str = IDLE, flag: bool = True, **overrides: str):
    """Run the reboot script with a reboot staged (unless ``flag`` is False).

    The router's pending wakes read ``pending``; the wait is four readings long (at 0,
    5, 10 and 15 s: a 15 s bound, 5 s between readings, the stub's sleeps taking no
    time). Its temporary files go to ``tmp_path/tmp``, so a test can see them gone.
    """
    required = tmp_path / "reboot-required"
    if flag:
        required.write_text("*** System restart required ***\n")
    pending_file = tmp_path / "pending"
    pending_file.write_text(pending)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    env = {
        "SKIP_ROOT_CHECK": "1",
        "TMPDIR": str(tmp_path / "tmp"),
        "REBOOT_REQUIRED_FILE": str(required),
        "REBOOT_PKGS_FILE": str(tmp_path / "pkgs"),
        "ROUTER_SERVICE": "basecradle-router",
        "ROUTER_ADMIN": str(tmp_path / "bin" / "router-admin"),
        "PENDING_FILE": str(pending_file),
        "REBOOT_IDLE_WAIT_S": "15",
        "REBOOT_IDLE_POLL_S": "5",
        "REBOOT_STATE_DIR": str(tmp_path / "state"),
        **overrides,
    }
    return _run(REBOOT_SCRIPT, tmp_path, env)


def _record(tmp_path) -> Path:
    return tmp_path / "state" / "deferrals"


def _deferred(tmp_path, ago_s: int) -> str:
    """Write a deferral record whose first deferral was ``ago_s`` seconds ago."""
    at = int(time.time()) - ago_s
    content = f"{at} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(at))}\n"
    _record(tmp_path).parent.mkdir(mode=0o700)
    _record(tmp_path).write_text(content)
    return content


def _after_sleep(tmp_path, n: int, reading: str) -> dict[str, str]:
    """The env that makes the ``n``-th wait end with the router reading ``reading``."""
    later = tmp_path / f"pending-after-{n}"
    later.write_text(reading)
    return {f"AFTER_SLEEP_{n}": str(later)}


def _line(proc, event: str) -> str:
    return next(ln for ln in proc.stdout.splitlines() if f"event={event}" in ln)


def test_no_reboot_required_is_a_noop(tmp_path) -> None:
    # The flag file is absent → nothing is read, nothing drains, nothing reboots.
    proc, calls = _reboot(tmp_path, flag=False)
    assert proc.returncode == 0, proc.stderr
    assert "REBOOT" not in calls
    assert "systemctl stop" not in calls
    assert "pending" not in calls


def test_no_reboot_required_removes_the_deferral_record_and_its_directory(tmp_path) -> None:
    # Whatever creates, cleans up: no reboot pending means no deferral pending either.
    _deferred(tmp_path, 60)
    proc, _ = _reboot(tmp_path, flag=False)
    assert proc.returncode == 0, proc.stderr
    assert not _record(tmp_path).parent.exists()


def test_the_cleanup_never_touches_what_is_not_a_real_directory(tmp_path) -> None:
    # A link at the record's path is left alone, and so is everything behind it.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "deferrals").write_text("keep me\n")
    (tmp_path / "state").symlink_to(elsewhere)
    proc, _ = _reboot(tmp_path, flag=False)
    assert proc.returncode == 0, proc.stderr
    assert (elsewhere / "deferrals").read_text() == "keep me\n"
    assert (tmp_path / "state").is_symlink()


def test_an_idle_router_is_drained_then_rebooted(tmp_path) -> None:
    proc, calls = _reboot(tmp_path, pending=IDLE)
    assert proc.returncode == 0, proc.stderr
    assert "pending -> 0" in calls
    assert "systemctl stop basecradle-router" in calls
    assert "REBOOT" in calls
    # Read, then drain, then reboot — never sever a wake we could have let finish.
    assert calls.index("pending ->") < calls.index("systemctl stop") < calls.index("REBOOT")
    assert "sleep" not in calls
    assert not _record(tmp_path).exists()


def test_the_reading_runs_as_the_daemons_own_user(tmp_path) -> None:
    # setpriv execs, so the timeout signals the reader itself, and root reads nothing.
    proc, calls = _reboot(tmp_path, pending=IDLE)
    assert proc.returncode == 0, proc.stderr
    assert "setpriv --reuid=router --regid=router --init-groups --reset-env" in calls
    assert "pending -> 0 as router" in calls


def test_a_wake_in_flight_keeps_the_port_open_until_it_has_ended(tmp_path) -> None:
    # The DoD's test (#557): the stop that closes the port comes only after the reading
    # that says the wake is over — every reading before it said busy, and no stop ran.
    proc, calls = _reboot(tmp_path, pending=BUSY, **_after_sleep(tmp_path, 2, IDLE))
    assert proc.returncode == 0, proc.stderr
    readings = re.findall(r"^pending -> (.*) as router$", calls, re.MULTILINE)
    assert readings == [BUSY, BUSY, IDLE]
    before_stop = calls[: calls.index("systemctl stop")]
    assert before_stop.count("pending -> ") == 3
    assert before_stop.rindex("pending -> 0 ") > before_stop.rindex(f"pending -> {BUSY} ")
    assert "REBOOT" in calls
    assert "event=reboot_deferred" not in proc.stdout


def test_a_router_busy_for_the_whole_wait_defers_to_the_next_slot(tmp_path) -> None:
    proc, calls = _reboot(tmp_path, pending=BUSY)
    assert proc.returncode == 0, proc.stderr
    assert "systemctl stop" not in calls
    assert "REBOOT" not in calls
    assert calls.count("pending -> ") == 4  # at 0, 5, 10 and 15 s: the bound is inclusive
    line = _line(proc, "reboot_deferred")
    assert line.startswith("reboot-if-required: event=reboot_deferred deferred_since=")
    assert "deferred_for_s=0 max_defer_s=129600:" in line
    assert "1 wake(s) queued or in flight after 15s (nova=1)" in line
    at, since = _record(tmp_path).read_text().split()
    assert abs(int(at) - time.time()) < 60
    assert since == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(at)))
    assert f"deferred_since={since}" in line


def test_a_later_deferral_keeps_the_first_deferrals_time(tmp_path) -> None:
    # The next day's slot: still inside the bound, and the clock is not restarted.
    content = _deferred(tmp_path, DAY)
    proc, calls = _reboot(tmp_path, pending=BUSY)
    assert proc.returncode == 0, proc.stderr
    assert "REBOOT" not in calls
    assert _record(tmp_path).read_text() == content
    line = _line(proc, "reboot_deferred")
    assert f"deferred_since={content.split()[1]} " in line
    assert re.search(r"deferred_for_s=86[34]\d\d ", line)


def test_runs_by_hand_cannot_spend_the_bound(tmp_path) -> None:
    # The bound is a time, never a count: any number of runs inside it all defer.
    _deferred(tmp_path, 60)
    for _ in range(3):
        proc, calls = _reboot(tmp_path, pending=BUSY)
        assert proc.returncode == 0, proc.stderr
        assert "REBOOT" not in calls


def test_a_reboot_deferred_for_the_longest_allowed_runs_whatever_is_running(tmp_path) -> None:
    # The bound: a kernel update never waits forever. It still waited the full wait first.
    content = _deferred(tmp_path, 2 * DAY)
    proc, calls = _reboot(tmp_path, pending=BUSY)
    assert proc.returncode == 0, proc.stderr
    assert calls.count("pending -> ") == 4
    assert calls.index("pending ->") < calls.index("systemctl stop") < calls.index("REBOOT")
    line = _line(proc, "reboot_overdue")
    assert f"deferred since {content.split()[1]} (" in line
    assert "the longest allowed (129600s)" in line
    assert "(nova=1)" in line


def test_the_bound_is_the_configured_time(tmp_path) -> None:
    _deferred(tmp_path, 120)
    proc, calls = _reboot(tmp_path, pending=BUSY, REBOOT_MAX_DEFER_S="60")
    assert proc.returncode == 0, proc.stderr
    assert "REBOOT" in calls


def test_a_zero_bound_disables_deferral(tmp_path) -> None:
    proc, calls = _reboot(tmp_path, pending=BUSY, REBOOT_MAX_DEFER_S="0")
    assert proc.returncode == 0, proc.stderr
    assert "REBOOT" in calls
    assert "deferral is disabled (REBOOT_MAX_DEFER_S=0)" in _line(proc, "reboot_overdue")
    assert not _record(tmp_path).exists()


def test_a_record_dated_in_the_future_takes_no_deferral(tmp_path) -> None:
    # A clock set back must not stretch the bound by however far it went back.
    _deferred(tmp_path, -DAY)
    proc, calls = _reboot(tmp_path, pending=BUSY)
    assert proc.returncode == 0, proc.stderr
    assert "REBOOT" in calls
    assert "dated in the future" in _line(proc, "reboot_overdue")


@pytest.mark.parametrize(
    ("reading", "exit_code", "why"),
    [
        ("", "75", "router-admin pending exited 75"),
        ("nonsense", "0", "printed no reading this script recognises"),
        ("", "0", "printed no reading this script recognises"),
        ("-1 nova=-1", "0", "printed no reading this script recognises"),
        ("0\n0", "0", "printed no reading this script recognises"),
    ],
    ids=["exit-75", "garbled", "empty", "negative", "two-lines"],
)
def test_a_reading_that_cannot_tell_is_never_idle(tmp_path, reading, exit_code, why) -> None:
    proc, calls = _reboot(tmp_path, pending=reading, PENDING_EXIT=exit_code)
    assert proc.returncode == 0, proc.stderr
    assert "systemctl stop" not in calls
    assert "REBOOT" not in calls
    line = _line(proc, "reboot_deferred")
    assert "could not be read after 15s" in line and why in line


def test_a_failed_readings_cause_is_its_last_stderr_line(tmp_path) -> None:
    proc, _ = _reboot(
        tmp_path,
        pending="",
        PENDING_EXIT="75",
        PENDING_STDERR="noise\npending: unprovable: no evidence document at /x",
    )
    line = _line(proc, "reboot_deferred")
    assert (
        "router-admin pending exited 75: pending: unprovable: no evidence document at /x)" in line
    )
    assert "noise" not in line


def test_stderr_from_a_reading_that_succeeded_does_not_spoil_it(tmp_path) -> None:
    # uv's chatter, a Python warning: stdout alone is the reading.
    proc, calls = _reboot(tmp_path, pending=IDLE, PENDING_STDERR="Resolved 42 packages")
    assert proc.returncode == 0, proc.stderr
    assert "REBOOT" in calls


def test_a_reading_that_could_not_tell_is_read_again(tmp_path) -> None:
    # An unreadable moment is not the router's state for the whole slot.
    proc, calls = _reboot(tmp_path, pending="garbled", **_after_sleep(tmp_path, 1, IDLE))
    assert proc.returncode == 0, proc.stderr
    assert "REBOOT" in calls
    assert "event=reboot_" not in proc.stdout


@pytest.mark.parametrize(
    ("state", "substate"),
    [
        ("inactive", "dead"),
        ("failed", "failed"),
        ("activating", "auto-restart"),
        ("activating", "auto-restart-queued"),
    ],
)
def test_a_router_with_nothing_running_has_nothing_to_wait_for(tmp_path, state, substate):
    # A crash-looping daemon is waiting to start again: no wake can be in flight.
    proc, calls = _reboot(tmp_path, pending=BUSY, ROUTER_STATE=state, ROUTER_SUBSTATE=substate)
    assert proc.returncode == 0, proc.stderr
    assert "pending" not in calls
    assert "REBOOT" in calls


@pytest.mark.parametrize(
    ("state", "substate"),
    [("activating", "start"), ("deactivating", "stop-sigterm"), ("", "")],
)
def test_a_start_or_stop_under_way_or_an_unreadable_state_is_not_idle(tmp_path, state, substate):
    proc, calls = _reboot(tmp_path, pending=IDLE, ROUTER_STATE=state, ROUTER_SUBSTATE=substate)
    assert proc.returncode == 0, proc.stderr
    assert "REBOOT" not in calls
    assert "event=reboot_deferred" in proc.stdout


@pytest.mark.parametrize(
    "content", ["", "garbage\n", "1\n", "x 2026-11-27T11:02:32Z\n", "1795000000 yesterday\n"]
)
def test_a_record_that_cannot_be_vouched_for_takes_no_deferral(tmp_path, content) -> None:
    _record(tmp_path).parent.mkdir(mode=0o700)
    _record(tmp_path).write_text(content)
    proc, calls = _reboot(tmp_path, pending=BUSY)
    assert proc.returncode == 0, proc.stderr
    assert "REBOOT" in calls
    assert "cannot be read or kept" in _line(proc, "reboot_overdue")


def test_a_linked_state_directory_takes_no_deferral_and_writes_nothing(tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "state").symlink_to(elsewhere)
    proc, calls = _reboot(tmp_path, pending=BUSY)
    assert proc.returncode == 0, proc.stderr
    assert "REBOOT" in calls
    assert list(elsewhere.iterdir()) == []


def test_a_linked_record_takes_no_deferral_and_is_not_followed(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_text("1795000000 2026-11-18T09:46:40Z\n")
    _record(tmp_path).parent.mkdir(mode=0o700)
    _record(tmp_path).symlink_to(target)
    proc, calls = _reboot(tmp_path, pending=BUSY)
    assert proc.returncode == 0, proc.stderr
    assert "REBOOT" in calls
    assert target.read_text() == "1795000000 2026-11-18T09:46:40Z\n"


@pytest.mark.parametrize("pending", [IDLE, BUSY], ids=["rebooted", "deferred"])
def test_the_readings_temporary_file_is_removed_on_every_way_out(tmp_path, pending) -> None:
    proc, _ = _reboot(tmp_path, pending=pending)
    assert proc.returncode == 0, proc.stderr
    assert list((tmp_path / "tmp").iterdir()) == []


def test_the_deferral_line_is_a_journal_warning_and_plain_at_a_terminal(tmp_path) -> None:
    # systemd reads a leading <4> on a journal stream as WARNING; a terminal gets none.
    journal_dir, terminal_dir = tmp_path / "journal", tmp_path / "terminal"
    journal_dir.mkdir()
    terminal_dir.mkdir()
    journal, _ = _reboot(journal_dir, pending=BUSY, JOURNAL_STREAM="8:12345")
    assert "\n<4>reboot-if-required: event=reboot_deferred " in "\n" + journal.stdout
    terminal, _ = _reboot(terminal_dir, pending=BUSY)
    assert "event=reboot_deferred" in terminal.stdout
    assert "<4>" not in terminal.stdout


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("REBOOT_IDLE_WAIT_S", "soon"),
        ("REBOOT_IDLE_POLL_S", "0"),
        ("REBOOT_READ_TIMEOUT_S", "0"),
        ("REBOOT_MAX_DEFER_S", "-1"),
    ],
)
def test_a_setting_it_cannot_honour_is_refused_before_anything_happens(
    tmp_path, name, value
) -> None:
    proc, calls = _reboot(tmp_path, pending=IDLE, **{name: value})
    assert proc.returncode == 1
    assert name in proc.stderr
    assert "systemctl" not in calls


def test_reboot_proceeds_even_if_drain_fails(tmp_path) -> None:
    # A stuck unit must not pin an unpatched kernel forever: a stop failure is
    # logged but the reboot still happens.
    proc, calls = _reboot(tmp_path, pending=IDLE, STOP_FAILS="1")
    assert proc.returncode == 0
    assert "systemctl stop" in calls
    assert "REBOOT" in calls


def test_the_idle_reader_is_the_root_owned_router_admin() -> None:
    # Root runs nothing out of the mirrored app tree (#297): the default reader is the
    # deploy/bin copy installed root-owned, and it runs as the daemon's user.
    text = REBOOT_SCRIPT.read_text()
    assert 'ROUTER_ADMIN="${ROUTER_ADMIN:-/opt/basecradle-router/bin/router-admin}"' in text
    assert 'ROUTER_USER="${ROUTER_USER:-router}"' in text
    assert (REPO_ROOT / "deploy" / "bin" / "router-admin").is_file()


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

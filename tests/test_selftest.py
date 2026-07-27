"""The freeze-readability self-test, driven offline against a real temp lock dir.

**This file carries regression instance 2** of the green-while-absent acceptance set
(basecradle/basecradle#460): *the unreadable freeze — the control existed but could
not be read when it mattered.* The two demonstrations the program asks for are
:func:`test_regression_instance_2_an_unreadable_lock_fails_loudly_naming_the_file`
and :func:`test_regression_instance_2_a_malformed_lock_fails_loudly_naming_the_file`.

Every check runs against a throwaay ``tmp_path`` directory — **never** the live
``/run/basecradle-noc/wake-locks``, which is why the lock directory is
configurable at all. No network, model, or live agent.
Test cast: Nova Digital (``nova``, AI) and John Doe (``john``, human).
"""

import json
import os
from datetime import datetime, timezone

import pytest

from basecradle_router.selftest import (
    DEGRADED,
    FAILED,
    OK,
    log_freeze_selftest,
    run_freeze_selftest,
)
from basecradle_router.wakelock import WakeLockGuard

NOVA = "nova"
JOHN = "john"
FIXED_NOW = "2026-07-27T12:00:00+00:00"


class _Clock:
    def __init__(self, moment: str = FIXED_NOW) -> None:
        self.now = datetime.fromisoformat(moment)

    def __call__(self) -> datetime:
        return self.now


def _guard(tmp_path) -> WakeLockGuard:
    return WakeLockGuard(lock_dir=str(tmp_path), now=_Clock())


def _write_lock(tmp_path, slug: str, *, expires_at: str = "2026-07-27T12:05:00+00:00") -> None:
    payload = {
        "agent": slug,
        "reason": "converge → 0.41.0",
        "acquired_at": "2026-07-27T11:59:00+00:00",
        "expires_at": expires_at,
    }
    (tmp_path / f"{slug}.lock").write_text(json.dumps(payload), encoding="utf-8")


def _run(tmp_path, slugs=(NOVA,)):
    return run_freeze_selftest(_guard(tmp_path), slugs, now=lambda: datetime.now(timezone.utc))


def _check_for(result, target: str):
    return next(check for check in result.checks if check.target.endswith(target))


# --- regression instance 2: the unreadable freeze ---------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the permissions under test")
def test_regression_instance_2_an_unreadable_lock_fails_loudly_naming_the_file(
    tmp_path, caplog
) -> None:
    """A lock the daemon cannot read must fail the probe, naming the exact file.

    This is the 2026-06-28 ``root:router 640`` class applied to the freeze surface.
    The wake-lock guard's own fail-direction on an unreadable lock is to **wake
    anyway** — correct, because wedging every wake on a permissions typo would be
    far worse — so the interlock silently stops working and nothing else in the
    system ever says so. The self-test is the thing that says so.
    """
    _write_lock(tmp_path, NOVA)
    (tmp_path / f"{NOVA}.lock").chmod(0o000)

    with caplog.at_level("ERROR", logger="basecradle_router.selftest"):
        result = _run(tmp_path)
        log_freeze_selftest(result)

    assert result.status == FAILED
    assert result.exit_code == 1
    check = _check_for(result, f"{NOVA}.lock")
    assert check.state == "unreadable"
    assert "failing open" in check.detail
    # Loud, and it names the file an operator has to go fix — the property the
    # original outage's diagnosis lacked.
    assert "event=freeze_selftest status=failed" in caplog.text
    assert f"{NOVA}.lock" in caplog.text


def test_regression_instance_2_a_malformed_lock_fails_loudly_naming_the_file(
    tmp_path, caplog
) -> None:
    """A malformed lock must fail the probe too — it refuses *every* wake, silently.

    The other half of the same class, and the more insidious one: ``Present =
    locked`` means an unparseable lock refuses each wake rather than failing open,
    so the agent goes permanently unreachable while the daemon looks healthy.
    """
    (tmp_path / f"{NOVA}.lock").write_text("{ not json at all", encoding="utf-8")

    with caplog.at_level("ERROR", logger="basecradle_router.selftest"):
        result = _run(tmp_path)
        log_freeze_selftest(result)

    assert result.status == FAILED
    check = _check_for(result, f"{NOVA}.lock")
    assert check.state == "unparseable"
    assert "every wake for this agent is being refused" in check.detail
    assert f"{NOVA}.lock" in caplog.text


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the permissions under test")
def test_an_unreadable_lock_directory_fails_the_probe(tmp_path) -> None:
    # The directory-level shape of the same fault: 0000 leaves it neither listable
    # nor searchable, so every agent's lock is unreachable at once.
    locks = tmp_path / "wake-locks"
    locks.mkdir()
    locks.chmod(0o000)
    try:
        result = _run(locks)
    finally:
        locks.chmod(0o755)  # so tmp_path teardown can remove it

    assert result.status == FAILED
    assert _check_for(result, "wake-locks").state == "dir_unreadable"


def test_a_stray_malformed_lock_is_caught_even_for_an_unregistered_agent(tmp_path) -> None:
    # Perms and contract faults do not stay confined to the agents we happen to have
    # registered, so every lock file present is inspected, not just the known ones.
    (tmp_path / "someone-else.lock").write_text("{}", encoding="utf-8")

    result = _run(tmp_path, slugs=(NOVA,))

    assert result.status == FAILED
    assert _check_for(result, "someone-else.lock").state == "unparseable"


# --- the healthy and the merely-unprovable ---------------------------------


def test_a_clean_surface_passes_and_reports_every_agent(tmp_path) -> None:
    result = _run(tmp_path, slugs=(NOVA, JOHN))

    assert result.status == OK
    assert result.exit_code == 0
    assert _check_for(result, f"{NOVA}.lock").state == "absent"
    assert _check_for(result, f"{JOHN}.lock").state == "absent"


def test_a_live_freeze_is_the_control_working_not_a_fault(tmp_path) -> None:
    # A held lock is the interlock doing its job. A probe that called it a failure
    # would go red on every converge and be waved through when it finally mattered.
    _write_lock(tmp_path, NOVA, expires_at="2999-01-01T00:00:00+00:00")

    result = _run(tmp_path)

    assert result.status == OK
    check = _check_for(result, f"{NOVA}.lock")
    assert check.state == "held"
    assert "converge → 0.41.0" in check.detail


def test_a_stale_lock_is_degraded_not_failed(tmp_path) -> None:
    # The NOC died mid-converge: wakes proceed (correctly), but a wedged converge
    # should be seen. Not a failure — nothing is broken for the agent.
    _write_lock(tmp_path, NOVA, expires_at="2020-01-01T00:00:00+00:00")

    result = _run(tmp_path)

    assert result.status == DEGRADED
    assert result.exit_code == 2
    assert _check_for(result, f"{NOVA}.lock").state == "stale"


def test_an_absent_lock_directory_is_degraded_and_names_the_configured_path(tmp_path) -> None:
    # Not a failure — a lock written there later would still be honoured. Not a pass
    # either: a router configured for the WRONG directory looks exactly like this, so
    # the verdict must carry the path for the NOC to compare against its own.
    missing = tmp_path / "not-created-yet"

    result = _run(missing)

    assert result.status == DEGRADED
    assert result.lock_dir == str(missing)
    assert _check_for(result, "not-created-yet").state == "dir_absent"


def test_a_lock_path_that_is_not_a_directory_fails(tmp_path) -> None:
    not_a_dir = tmp_path / "wake-locks"
    not_a_dir.write_text("", encoding="utf-8")

    assert _run(not_a_dir).status == FAILED


# --- the probe reports what it can actually prove ---------------------------


def test_the_probe_records_the_user_it_ran_as(tmp_path) -> None:
    # Who ran the probe changes what it proves, so the result says.
    result = _run(tmp_path)

    assert result.ran_as
    assert _check_for(result, "process").state in ("ran_as_user", "ran_as_root")


def test_running_as_root_cannot_prove_the_daemons_access(tmp_path, monkeypatch) -> None:
    # Root bypasses file permissions, so a root run would pass on a box where the
    # daemon itself is locked out — the exact failure this probe exists to catch.
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    result = _run(tmp_path)

    assert result.status == DEGRADED
    check = _check_for(result, "process")
    assert check.state == "ran_as_root"
    assert "re-run as the daemon's user" in check.detail


def test_reading_the_surface_emits_no_wake_refused_lines(tmp_path, caplog) -> None:
    # The probe inspects every agent's lock; if it went through the wake path's
    # `check` it would log a refusal for every wake nobody attempted, and Live Tail
    # would show an interlock storm during a routine converge.
    _write_lock(tmp_path, NOVA, expires_at="2999-01-01T00:00:00+00:00")

    with caplog.at_level("DEBUG", logger="basecradle_router.wakelock"):
        _run(tmp_path)

    assert "event=wake_refused" not in caplog.text


def test_a_healthy_run_still_says_so_at_info(tmp_path, caplog) -> None:
    # An absent line is itself a finding (the check did not run), so the all-clear is
    # stated positively rather than inferred from silence.
    with caplog.at_level("INFO", logger="basecradle_router.selftest"):
        log_freeze_selftest(_run(tmp_path))

    assert "event=freeze_selftest status=ok" in caplog.text


def test_the_json_shape_is_what_the_noc_schedules_against(tmp_path) -> None:
    payload = _run(tmp_path).to_json()

    assert payload["probe"] == "freeze-readability"
    assert payload["status"] == OK
    assert payload["lock_dir"] == str(tmp_path)
    assert {"target", "status", "state", "detail"} == set(payload["checks"][0])

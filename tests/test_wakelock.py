"""The NOC wake-lock guard, driven offline with a real temp dir and a fake clock.

The guard reads ``/run/basecradle-noc/wake-locks/<slug>.lock`` (here a tmp dir) and
decides whether a wake may fire: a live lock refuses, an expired lock wakes (stale),
a malformed lock refuses, an unreadable lock fails open. The clock is injected so
``now`` is deterministic and never depends on wall time. Counterpart to
basecradle-noc#38. No network, model, or live agent.
Test cast: Nova Digital (``nova``, AI) is the agent whose wake is gated.
"""

import json
import logging

import pytest

from basecradle_router.wakelock import (
    DEFAULT_LOCK_DIR,
    WakeLockGuard,
    WakeLockState,
    _parse_iso8601,
)

NOVA = "nova"  # the agent's harness_key (its OS-user slug)


class _Clock:
    """A hand-set wall clock: returns a fixed UTC ``datetime`` until told to move."""

    def __init__(self, moment: str = "2026-06-26T12:00:00+00:00") -> None:
        self.now = _parse_iso8601(moment)

    def __call__(self):
        return self.now

    def set(self, moment: str) -> None:
        self.now = _parse_iso8601(moment)


def _guard(tmp_path, clock: _Clock | None = None) -> WakeLockGuard:
    return WakeLockGuard(lock_dir=str(tmp_path), now=clock or _Clock())


def _write_lock(tmp_path, slug: str, **fields) -> None:
    payload = {
        "agent": slug,
        "reason": "converge → 0.39.0",
        "acquired_at": "2026-06-26T11:59:00+00:00",
        "expires_at": "2026-06-26T12:05:00+00:00",
        **fields,
    }
    (tmp_path / f"{slug}.lock").write_text(json.dumps(payload), encoding="utf-8")


# --- the pinned contract ---------------------------------------------------


def test_default_lock_dir_is_the_pinned_path() -> None:
    # The capital pinned this exact path; the NOC writes there. Guard the contract.
    assert DEFAULT_LOCK_DIR == "/run/basecradle-noc/wake-locks"


# --- absent: the common case wakes silently --------------------------------


def test_absent_lock_wakes(tmp_path) -> None:
    decision = _guard(tmp_path).check(NOVA)
    assert decision.state is WakeLockState.ABSENT
    assert decision.should_wake is True


def test_absent_lock_emits_no_log(tmp_path, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="basecradle_router.wakelock"):
        _guard(tmp_path).check(NOVA)
    assert caplog.records == []  # the happy path is silent


# --- held: a live lock refuses ---------------------------------------------


def test_live_lock_refuses_the_wake(tmp_path) -> None:
    clock = _Clock("2026-06-26T12:00:00+00:00")  # before expires_at (12:05)
    _write_lock(tmp_path, NOVA)
    decision = _guard(tmp_path, clock).check(NOVA)
    assert decision.state is WakeLockState.HELD
    assert decision.should_wake is False


def test_held_lock_logs_the_required_shape(tmp_path, caplog) -> None:
    clock = _Clock("2026-06-26T12:00:00+00:00")
    _write_lock(tmp_path, NOVA)
    with caplog.at_level(logging.WARNING, logger="basecradle_router.wakelock"):
        _guard(tmp_path, clock).check(NOVA)
    line = caplog.text
    # The exact contract triplet must be present so Live Tail can filter on it.
    assert "event=wake_refused" in line
    assert "reason=wake_lock_held" in line
    assert f"agent={NOVA}" in line


def test_a_lock_for_another_agent_does_not_gate_this_one(tmp_path) -> None:
    clock = _Clock("2026-06-26T12:00:00+00:00")
    _write_lock(tmp_path, "someone-else")
    decision = _guard(tmp_path, clock).check(NOVA)
    assert decision.state is WakeLockState.ABSENT
    assert decision.should_wake is True


# --- stale: an expired lock wakes but is flagged ---------------------------


def test_expired_lock_is_stale_and_wakes(tmp_path) -> None:
    clock = _Clock("2026-06-26T12:10:00+00:00")  # past expires_at (12:05)
    _write_lock(tmp_path, NOVA)
    decision = _guard(tmp_path, clock).check(NOVA)
    assert decision.state is WakeLockState.STALE
    assert decision.should_wake is True


def test_stale_lock_logs_the_required_shape(tmp_path, caplog) -> None:
    clock = _Clock("2026-06-26T12:10:00+00:00")
    _write_lock(tmp_path, NOVA)
    with caplog.at_level(logging.WARNING, logger="basecradle_router.wakelock"):
        _guard(tmp_path, clock).check(NOVA)
    assert "event=wake_lock_stale" in caplog.text
    assert f"agent={NOVA}" in caplog.text


def test_exactly_at_expiry_is_stale_not_held(tmp_path) -> None:
    # now == expires_at: the contract says now >= expires_at is stale (wake).
    clock = _Clock("2026-06-26T12:05:00+00:00")
    _write_lock(tmp_path, NOVA)
    assert _guard(tmp_path, clock).check(NOVA).state is WakeLockState.STALE


# --- unparseable: present but malformed -> refuse (Present = locked) -------


def test_malformed_json_refuses(tmp_path, caplog) -> None:
    (tmp_path / f"{NOVA}.lock").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="basecradle_router.wakelock"):
        decision = _guard(tmp_path).check(NOVA)
    assert decision.state is WakeLockState.UNPARSEABLE
    assert decision.should_wake is False
    assert "reason=wake_lock_unparseable" in caplog.text


def test_missing_expires_at_refuses(tmp_path) -> None:
    (tmp_path / f"{NOVA}.lock").write_text(json.dumps({"agent": NOVA}), encoding="utf-8")
    decision = _guard(tmp_path).check(NOVA)
    assert decision.state is WakeLockState.UNPARSEABLE
    assert decision.should_wake is False


def test_non_object_lock_refuses(tmp_path) -> None:
    (tmp_path / f"{NOVA}.lock").write_text(json.dumps(["nope"]), encoding="utf-8")
    assert _guard(tmp_path).check(NOVA).state is WakeLockState.UNPARSEABLE


def test_unparseable_expires_at_refuses(tmp_path) -> None:
    _write_lock(tmp_path, NOVA, expires_at="not-a-timestamp")
    assert _guard(tmp_path).check(NOVA).state is WakeLockState.UNPARSEABLE


# --- unreadable: a deployment fault -> fail open (wake) + loud error --------


def test_permission_error_fails_open(tmp_path, caplog, monkeypatch) -> None:
    _write_lock(tmp_path, NOVA)

    def _boom(*_a, **_k):
        raise PermissionError("EACCES")

    monkeypatch.setattr("builtins.open", _boom)
    with caplog.at_level(logging.ERROR, logger="basecradle_router.wakelock"):
        decision = _guard(tmp_path).check(NOVA)
    assert decision.state is WakeLockState.UNREADABLE
    assert decision.should_wake is True  # fail open: never wedge the whole fleet
    assert "event=wake_lock_unreadable" in caplog.text


@pytest.mark.parametrize("bad", ["../escape", "a/b", ".", "..", ""])
def test_unsafe_slug_fails_open(tmp_path, bad) -> None:
    # A path-separator slug must never escape the lock dir; fail open, loudly.
    assert _guard(tmp_path).check(bad).state is WakeLockState.UNREADABLE


def test_embedded_nul_slug_does_not_raise(tmp_path) -> None:
    # A NUL byte slips past the basename guard (NUL is no path separator) but makes
    # open() raise ValueError; check() must absorb it (UNREADABLE), never propagate.
    assert _guard(tmp_path).check("nova\x00evil").state is WakeLockState.UNREADABLE


# --- timestamp tolerance ----------------------------------------------------


def test_z_suffix_is_parsed(tmp_path) -> None:
    # The NOC may write a trailing Z; fromisoformat on 3.10 can't, so we tolerate it.
    clock = _Clock("2026-06-26T12:00:00+00:00")
    _write_lock(tmp_path, NOVA, expires_at="2026-06-26T12:05:00Z")
    assert _guard(tmp_path, clock).check(NOVA).state is WakeLockState.HELD


def test_naive_timestamp_assumed_utc(tmp_path) -> None:
    clock = _Clock("2026-06-26T12:00:00+00:00")
    _write_lock(tmp_path, NOVA, expires_at="2026-06-26T12:05:00")
    assert _guard(tmp_path, clock).check(NOVA).state is WakeLockState.HELD


def test_parse_iso8601_rejects_non_strings() -> None:
    assert _parse_iso8601(None) is None
    assert _parse_iso8601(123) is None
    assert _parse_iso8601("") is None

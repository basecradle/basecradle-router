"""Per-agent locks and bounded retry — deterministic, no flaky sleeps.

Contention is proven with non-blocking acquires (not timing); the one real
two-thread test sequences with an Event, never a sleep. Retry uses an injected
clock so nothing actually waits.

The lock key is an agent's harness-instance identity (its OS-user slug), not a
repo — every input source for one agent serializes on the same key.
"""

import threading

import pytest

from basecradle_router.concurrency import (
    AgentLocks,
    RetryExhausted,
    TransientError,
    with_retry,
)

AGENT_A = "nova"
AGENT_B = "basecradle-ruby-ai"


# --- AgentLocks ------------------------------------------------------------


def test_different_agents_do_not_contend() -> None:
    locks = AgentLocks()
    assert locks.acquire(AGENT_A) is True
    # A different agent is free while AGENT_A is held.
    assert locks.acquire(AGENT_B, blocking=False) is True
    locks.release(AGENT_A)
    locks.release(AGENT_B)


def test_same_agent_contends_then_frees() -> None:
    locks = AgentLocks()
    assert locks.acquire(AGENT_A) is True
    # A second acquire for the same agent cannot proceed while it is held.
    assert locks.acquire(AGENT_A, blocking=False) is False
    locks.release(AGENT_A)
    # Once released, the agent is acquirable again.
    assert locks.acquire(AGENT_A, blocking=False) is True
    locks.release(AGENT_A)


def test_guard_serializes_and_releases() -> None:
    locks = AgentLocks()
    with locks.guard(AGENT_A):
        assert locks.acquire(AGENT_A, blocking=False) is False
    # Guard released on normal exit.
    assert locks.acquire(AGENT_A, blocking=False) is True
    locks.release(AGENT_A)


def test_guard_releases_on_error() -> None:
    locks = AgentLocks()
    with pytest.raises(RuntimeError), locks.guard(AGENT_A):
        raise RuntimeError("boom")
    # The lock must be released even though the guarded block raised.
    assert locks.acquire(AGENT_A, blocking=False) is True
    locks.release(AGENT_A)


def test_different_agent_proceeds_while_another_is_held() -> None:
    # A real two-thread proof that AGENT_B runs to completion while AGENT_A is
    # held by another thread. Sequenced with Events — no sleeps, no flakes.
    locks = AgentLocks()
    a_held = threading.Event()
    let_a_go = threading.Event()
    b_done = threading.Event()

    def hold_a() -> None:
        with locks.guard(AGENT_A):
            a_held.set()
            let_a_go.wait(timeout=5)

    def run_b() -> None:
        with locks.guard(AGENT_B):
            b_done.set()

    holder = threading.Thread(target=hold_a)
    runner = threading.Thread(target=run_b)
    holder.start()
    assert a_held.wait(timeout=5)  # AGENT_A is now held by `holder`

    runner.start()
    # AGENT_B completes despite AGENT_A still being held — concurrency proven.
    assert b_done.wait(timeout=5)
    runner.join(timeout=5)

    let_a_go.set()
    holder.join(timeout=5)
    assert not holder.is_alive()


# --- with_retry ------------------------------------------------------------


class _Recorder:
    """A no-op sleep that records the backoff delays it was asked to wait."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def test_retry_returns_immediately_on_success() -> None:
    sleep = _Recorder()
    calls = []

    def op() -> str:
        calls.append(1)
        return "woke"

    assert with_retry(op, attempts=3, sleep=sleep) == "woke"
    assert len(calls) == 1
    assert sleep.delays == []  # never slept


def test_retry_succeeds_after_transient_failures() -> None:
    sleep = _Recorder()
    attempts_seen = []

    def op() -> str:
        attempts_seen.append(1)
        if len(attempts_seen) < 3:
            raise TransientError("transient blip")
        return "woke"

    assert with_retry(op, attempts=3, base_delay=0.5, sleep=sleep) == "woke"
    assert len(attempts_seen) == 3
    # Slept between the two failed attempts, with exponential backoff.
    assert sleep.delays == [0.5, 1.0]


def test_retry_gives_up_after_the_bound() -> None:
    sleep = _Recorder()
    calls = []

    def op() -> str:
        calls.append(1)
        raise TransientError("always failing")

    with pytest.raises(RetryExhausted) as excinfo:
        with_retry(op, attempts=3, base_delay=0.5, sleep=sleep)

    assert excinfo.value.attempts == 3
    assert isinstance(excinfo.value.__cause__, TransientError)
    assert len(calls) == 3  # tried exactly the bound
    assert sleep.delays == [0.5, 1.0]  # slept between tries, not after the last


def test_retry_does_not_catch_non_transient_errors() -> None:
    sleep = _Recorder()
    calls = []

    def op() -> str:
        calls.append(1)
        raise ValueError("permanent")

    with pytest.raises(ValueError, match="permanent"):
        with_retry(op, attempts=3, sleep=sleep)

    assert len(calls) == 1  # no retry on a non-transient error
    assert sleep.delays == []


def test_retry_rejects_non_positive_attempts() -> None:
    with pytest.raises(ValueError, match="attempts must be >= 1"):
        with_retry(lambda: None, attempts=0)

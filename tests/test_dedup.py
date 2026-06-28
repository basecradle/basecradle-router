"""The delivery deduper unit, driven offline with a fake clock (#133).

A ``seen``/``mark`` cache that collapses a duplicate webhook delivery into one
wake. Pinned here: the recently-woke semantics, TTL expiry (eager and swept),
the disable switch, and thread-safety under contention. No clock ever really
ticks — the monotonic clock is injected so the tests are deterministic.
"""

import threading

import pytest

from basecradle_router.dedup import _GC_INTERVAL, DeliveryDeduper


def test_unseen_key_is_not_seen() -> None:
    assert DeliveryDeduper(ttl=60.0).seen("github:abc") is False


def test_a_marked_key_is_seen_within_the_ttl() -> None:
    clock = [0.0]
    deduper = DeliveryDeduper(ttl=60.0, clock=lambda: clock[0])
    deduper.mark("github:abc")
    assert deduper.seen("github:abc") is True
    clock[0] = 59.9
    assert deduper.seen("github:abc") is True


def test_a_key_expires_at_the_ttl_boundary() -> None:
    clock = [0.0]
    deduper = DeliveryDeduper(ttl=60.0, clock=lambda: clock[0])
    deduper.mark("github:abc")
    clock[0] = 60.0  # the boundary is exclusive: >= ttl is expired
    assert deduper.seen("github:abc") is False


def test_distinct_keys_are_independent() -> None:
    deduper = DeliveryDeduper(ttl=60.0)
    deduper.mark("github:abc")
    assert deduper.seen("github:abc") is True
    assert deduper.seen("github:def") is False


def test_remarking_refreshes_the_window() -> None:
    clock = [0.0]
    deduper = DeliveryDeduper(ttl=60.0, clock=lambda: clock[0])
    deduper.mark("github:abc")
    clock[0] = 50.0
    deduper.mark("github:abc")  # refreshes the mark time
    clock[0] = 100.0  # 50s since the refresh — still within the TTL
    assert deduper.seen("github:abc") is True


def test_zero_ttl_disables_dedup() -> None:
    deduper = DeliveryDeduper(ttl=0.0)
    deduper.mark("github:abc")
    assert deduper.seen("github:abc") is False  # never remembers anything


def test_negative_ttl_is_rejected() -> None:
    with pytest.raises(ValueError, match="ttl must be >= 0"):
        DeliveryDeduper(ttl=-1.0)


def test_expired_entry_is_reclaimed_eagerly_on_seen() -> None:
    clock = [0.0]
    deduper = DeliveryDeduper(ttl=10.0, clock=lambda: clock[0])
    deduper.mark("github:abc")
    clock[0] = 11.0
    assert deduper.seen("github:abc") is False
    # The eager reclaim dropped it from the backing store, not just reported miss.
    assert "github:abc" not in deduper._seen


def test_gc_sweeps_expired_entries_to_stay_bounded() -> None:
    clock = [0.0]
    deduper = DeliveryDeduper(ttl=10.0, clock=lambda: clock[0])
    deduper.mark("github:old")
    clock[0] = 100.0  # well past the TTL; the old entry is now stale
    # Drive marks up to the GC interval so a sweep fires; use fresh keys so they
    # are not themselves stale.
    for i in range(_GC_INTERVAL):
        deduper.mark(f"github:fresh-{i}")
    assert "github:old" not in deduper._seen  # swept
    assert len(deduper._seen) == _GC_INTERVAL  # only the fresh keys remain


def test_thread_safe_under_concurrent_marks() -> None:
    # Many threads marking distinct keys must not corrupt the dict or lose entries.
    deduper = DeliveryDeduper(ttl=600.0)
    keys = [f"github:k{i}" for i in range(200)]

    def worker(start: int) -> None:
        for i in range(start, len(keys), 4):
            deduper.mark(keys[i])

    threads = [threading.Thread(target=worker, args=(s,)) for s in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(deduper.seen(k) for k in keys)

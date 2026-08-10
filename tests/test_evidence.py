"""The evidence store, driven offline against a real temp file.

What the router has demonstrably done — deliveries verified, agents woken — kept
where a *different* process (the NOC's claims emitter) can read it. The properties
pinned here are the ones the claims-vs-evidence ledger relies on
(basecradle/basecradle#460): counters survive a restart, the four wake outcomes stay
told apart — a success, a broken wake, a gate's refusal, and an idempotent collapse
that only a success can produce (basecradle-router#218) — and a store that cannot
write degrades quietly instead of taking a wake down with it. No network, model, or
live agent.
Test cast: Nova Digital (``nova``, AI) and John Doe (``john``, human).
"""

import json
import os
import stat
from datetime import datetime

from basecradle_router import evidence as evidence_module
from basecradle_router.evidence import (
    EVIDENCE_VERSION,
    EvidenceDocument,
    EvidenceStore,
    read_evidence,
)

NOVA = "nova"
JOHN = "john"


class _Clock:
    """A hand-set UTC clock, so every recorded timestamp is deterministic."""

    def __init__(self, moment: str = "2026-07-27T12:00:00+00:00") -> None:
        self.now = datetime.fromisoformat(moment)

    def __call__(self) -> datetime:
        return self.now

    def set(self, moment: str) -> None:
        self.now = datetime.fromisoformat(moment)


def _store(tmp_path, clock: _Clock | None = None) -> EvidenceStore:
    return EvidenceStore(str(tmp_path / "evidence.json"), now=clock or _Clock())


# --- the delivery sink: instance 5 (armed on paper, never accepted) ----------


def test_a_verified_delivery_is_the_sinks_proof(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_delivery_accepted("github")

    sink = store.snapshot().delivery_sinks["github"]
    assert sink.accepted == 1
    assert sink.last_accepted_at == "2026-07-27T12:00:00+00:00"


def test_rejections_and_accepts_are_counted_apart(tmp_path) -> None:
    # THE instance-5 distinction: a sink with rejections and no accepts is a
    # mismatched secret; one with neither has simply never been used. A ledger that
    # could not tell them apart is what let "armed on paper" look healthy.
    store = _store(tmp_path)
    for _ in range(3):
        store.record_delivery_rejected("github", "X-Hub-Signature-256 does not match")

    sink = store.snapshot().delivery_sinks["github"]
    assert (sink.accepted, sink.rejected) == (0, 3)
    assert sink.last_accepted_at is None  # never proven
    assert "does not match" in sink.last_reject_reason


def test_the_route_decision_splits_an_accepted_delivery(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_delivery_accepted("github")
    store.record_delivery_decision("github", woke=True)
    store.record_delivery_accepted("github")
    store.record_delivery_decision("github", woke=False)

    sink = store.snapshot().delivery_sinks["github"]
    assert (sink.accepted, sink.woke, sink.ignored) == (2, 1, 1)


def test_a_reject_reason_cannot_grow_the_document_without_bound(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_delivery_rejected("github", "x" * 5000)

    assert len(store.snapshot().delivery_sinks["github"].last_reject_reason) <= 200


# --- the wake edge: instance 4 (parked with nothing to re-wake it) -----------


def test_a_successful_wake_records_the_ledgers_evidence_pointer(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_wake_ok(
        NOVA, "0192f3a4-5b6c-7d8e-9f01-00000000000a", route="github", synthetic=False
    )

    wake = store.snapshot().agent_wakes[NOVA]
    assert wake.ok == 1
    assert wake.last_ok_at == "2026-07-27T12:00:00+00:00"
    # The delivery id is the join key back to both halves of that wake in journald.
    assert wake.last_ok_delivery == "0192f3a4-5b6c-7d8e-9f01-00000000000a"
    assert wake.last_ok_route == "github"


def test_the_wake_proof_is_kept_per_route_not_only_per_agent(tmp_path) -> None:
    """Instance 5's per-recipient half: which route proved it, not merely that one did.

    Two routes wired to the same agent; only one has ever woken it. The agent-wide
    scalars cannot express that — they describe whichever wake was most recent — so a
    ledger row reading them would call the never-used route proven the moment the
    working one fired. That is the substitution the NOC declined (basecradle-noc#408).
    """
    store = _store(tmp_path)
    store.record_wake_ok(NOVA, "delivery-1", route="github", synthetic=False)
    store.record_wake_ok(NOVA, "delivery-2", route="github", synthetic=False)

    wake = store.snapshot().agent_wakes[NOVA]
    assert wake.by_route["github"].ok == 2
    assert wake.by_route["github"].last_ok_delivery == "delivery-2"
    # Absent, not a zero row: "never tried" must never read as "tried and it worked".
    assert "basecradle" not in wake.by_route


def test_a_second_routes_first_wake_does_not_disturb_the_firsts_proof(tmp_path) -> None:
    # Each pair's age-of-proof is its own. A wake over one route must not refresh
    # another's timestamp, or a route that quietly stopped delivering would stay green
    # forever behind its healthy sibling.
    store = EvidenceStore(str(tmp_path / "evidence.json"), now=_Clock())
    store.record_wake_ok(NOVA, "delivery-1", route="github", synthetic=False)

    later = EvidenceStore(str(tmp_path / "evidence.json"), now=_Clock("2026-07-28T09:00:00+00:00"))
    later.record_wake_ok(NOVA, "delivery-2", route="basecradle", synthetic=False)

    wake = later.snapshot().agent_wakes[NOVA]
    assert wake.by_route["github"].last_ok_at == "2026-07-27T12:00:00+00:00"
    assert wake.by_route["basecradle"].last_ok_at == "2026-07-28T09:00:00+00:00"
    # The scalars describe the most recent wake, whatever route delivered it.
    assert (wake.ok, wake.last_ok_route) == (2, "basecradle")


def test_the_per_route_record_round_trips_through_the_document(tmp_path) -> None:
    # It is read by a different process than the one that wrote it, so the nested map
    # has to survive the file — and come back as objects, not the raw dicts on disk.
    path = str(tmp_path / "evidence.json")
    EvidenceStore(path, now=_Clock()).record_wake_ok(
        NOVA, "delivery-1", route="github", synthetic=False
    )

    reread = read_evidence(path).agent_wakes[NOVA].by_route["github"]

    assert (reread.ok, reread.last_ok_delivery) == (1, "delivery-1")


def test_a_hand_mangled_per_route_map_is_dropped_not_fatal(tmp_path) -> None:
    # Same tolerance as every other field: an unusable value costs an under-counted
    # ledger row that the next real wake corrects, never a daemon that will not boot.
    path = tmp_path / "evidence.json"
    path.write_text(
        json.dumps(
            {
                "version": EVIDENCE_VERSION,
                "agent_wakes": {NOVA: {"ok": 3, "by_route": "not a map"}},
            }
        ),
        encoding="utf-8",
    )

    wake = read_evidence(str(path)).agent_wakes[NOVA]
    assert (wake.ok, wake.by_route) == (3, {})


def test_a_refused_wake_is_never_confused_with_a_failed_one(tmp_path) -> None:
    # Opposite meanings: a refusal is the NOC's converge lock or the breaker working as
    # designed; a failure is the wake path broken. An agent whose history is all
    # refusals is suppressed, not unreachable — and the ledger must not cry wolf over a
    # converge.
    store = _store(tmp_path)
    store.record_wake_refused(
        NOVA, "wake_lock_held until 2026-07-27T12:05:00+00:00", route="github", synthetic=False
    )
    store.record_wake_failed(NOVA, "claude exited 1", route="github", synthetic=False)

    wake = store.snapshot().agent_wakes[NOVA]
    assert (wake.ok, wake.refused, wake.failed) == (0, 1, 1)
    assert wake.last_refused_reason.startswith("wake_lock_held")
    assert wake.last_failed_reason == "claude exited 1"


def test_a_dedup_is_never_confused_with_a_refusal(tmp_path) -> None:
    """The third meaning, and the one only a *success* can produce (#218).

    A refusal says a wake that should have run did not; a dedup says a wake that must
    not run did not — and the cache is marked only after a wake has succeeded, so this
    outcome is reachable exclusively downstream of an ``ok``. Sharing one counter made
    the newest recorded attempt on a healthy route read as a rejection.
    """
    store = _store(tmp_path)
    store.record_wake_ok(NOVA, "delivery-1", route="github", synthetic=False)
    store.record_wake_deduped(NOVA, route="github", synthetic=False)

    wake = read_evidence(str(tmp_path / "evidence.json")).agent_wakes[NOVA]
    assert (wake.ok, wake.failed, wake.refused, wake.deduped) == (1, 0, 0, 1)
    # The fields a consumer reads to decide "what did this route last do": the dedup
    # moved its own timestamp and left the refusal slot untouched.
    assert wake.last_refused_at is None and wake.last_refused_reason is None
    assert wake.last_deduped_at == wake.last_ok_at
    assert (wake.last_deduped_route, wake.last_deduped_synthetic) == ("github", False)
    assert wake.by_route["github"].deduped == 1
    assert wake.by_route["github"].last_deduped_at == wake.last_deduped_at


def test_a_synthetic_dedup_never_reads_as_a_production_one(tmp_path) -> None:
    # Provenance is recorded at write time beside *every* outcome, the dedup included —
    # deriving it later from the route name is what a since-disabled route answers
    # wrongly (basecradle-router#208).
    store = _store(tmp_path)
    store.record_wake_deduped(NOVA, route="probe", synthetic=True)

    wake = store.snapshot().agent_wakes[NOVA]
    assert (wake.last_deduped_route, wake.last_deduped_synthetic) == ("probe", True)
    assert set(wake.by_route) == {"probe"}


def test_a_legacy_duplicate_delivery_refusal_is_reclassified_on_load(tmp_path) -> None:
    """The rows already on disk carry the misreading, so the fix has to reach them.

    This document is durable on purpose, and ``last_refused_*`` moves only when a
    genuine refusal happens — so code that merely stops writing a dedup there would
    leave the live rows saying "newest attempt: rejected" indefinitely. Shaped like the
    rows measured live: ``ok=4 refused=2``, the last refusal a dedup.
    """
    path = tmp_path / "evidence.json"
    path.write_text(
        json.dumps(
            {
                "version": EVIDENCE_VERSION,
                "agent_wakes": {
                    NOVA: {
                        "ok": 4,
                        "failed": 0,
                        "refused": 2,
                        "last_ok_at": "2026-08-02T03:42:17+00:00",
                        "last_refused_at": "2026-08-02T03:42:17.002600+00:00",
                        "last_refused_reason": "duplicate_delivery",
                        "last_refused_route": "github",
                        "last_refused_synthetic": False,
                        "by_route": {
                            "github": {
                                "ok": 4,
                                "refused": 2,
                                "last_ok_at": "2026-08-02T03:42:17+00:00",
                                "last_refused_at": "2026-08-02T03:42:17.002600+00:00",
                                "last_refused_reason": "duplicate_delivery",
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    wake = read_evidence(str(path)).agent_wakes[NOVA]

    # The one event we can positively identify moves; `refused + deduped` is conserved,
    # because nothing ever recorded the mix of the rest.
    assert (wake.ok, wake.refused, wake.deduped) == (4, 1, 1)
    assert wake.last_deduped_at == "2026-08-02T03:42:17.002600+00:00"
    assert (wake.last_deduped_route, wake.last_deduped_synthetic) == ("github", False)
    # The point of the whole exercise: the newest recorded attempt is the success again.
    assert wake.last_refused_at is None and wake.last_refused_reason is None
    assert wake.last_refused_route is None and wake.last_refused_synthetic is None

    per_route = wake.by_route["github"]
    assert (per_route.ok, per_route.refused, per_route.deduped) == (4, 1, 1)
    assert per_route.last_refused_at is None and per_route.last_refused_reason is None
    assert per_route.last_deduped_at == "2026-08-02T03:42:17.002600+00:00"


def test_reclassifying_a_legacy_dedup_is_idempotent(tmp_path) -> None:
    # It must survive load → write → load: a migration that moved a count on every boot
    # would inflate `deduped` forever. Safe by construction — nothing writes
    # `duplicate_delivery` into a refusal reason any more — and pinned so it stays so.
    path = tmp_path / "evidence.json"
    path.write_text(
        json.dumps(
            {
                "version": EVIDENCE_VERSION,
                "agent_wakes": {
                    NOVA: {
                        "refused": 1,
                        "last_refused_at": "2026-08-02T03:42:17+00:00",
                        "last_refused_reason": "duplicate_delivery",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    # A round trip through the daemon's own writer, then a fresh read.
    EvidenceStore(str(path)).record_queue_depth(JOHN, 1)
    wake = read_evidence(str(path)).agent_wakes[NOVA]

    assert (wake.refused, wake.deduped) == (0, 1)
    assert wake.last_deduped_at == "2026-08-02T03:42:17+00:00"


def test_a_genuine_refusal_on_disk_is_left_exactly_as_it_was(tmp_path) -> None:
    # The migration is keyed on an exact reason the pipeline had exactly one writer for,
    # so a converge freeze or a tripped breaker must pass through untouched — silently
    # reclassifying a real refusal would be the mirror image of the bug being fixed.
    path = tmp_path / "evidence.json"
    path.write_text(
        json.dumps(
            {
                "version": EVIDENCE_VERSION,
                "agent_wakes": {
                    NOVA: {
                        "refused": 1,
                        "last_refused_at": "2026-08-02T03:42:17+00:00",
                        "last_refused_reason": "wake_lock_held until 2026-08-02T04:00:00+00:00",
                        "last_refused_route": "github",
                        "last_refused_synthetic": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    wake = read_evidence(str(path)).agent_wakes[NOVA]

    assert (wake.refused, wake.deduped) == (1, 0)
    assert wake.last_refused_at == "2026-08-02T03:42:17+00:00"
    assert wake.last_refused_route == "github"
    assert wake.last_deduped_at is None


def test_an_agent_never_woken_has_no_evidence_at_all(tmp_path) -> None:
    # The never-proven state the parked-builder detection reads. Silence in the
    # document is the finding, so it must be genuinely absent, not a zeroed row.
    store = _store(tmp_path)
    store.record_wake_ok(JOHN, "delivery-1", route="github", synthetic=False)

    assert NOVA not in store.snapshot().agent_wakes


def test_queue_depth_tracks_the_transient_wake_edge(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_queue_depth(NOVA, 2)
    assert store.snapshot().agent_wakes[NOVA].queued == 2

    store.record_queue_depth(NOVA, 0)
    assert store.snapshot().agent_wakes[NOVA].queued == 0


def test_an_unchanged_queue_depth_writes_nothing(tmp_path, monkeypatch) -> None:
    # The scheduler reports on every enqueue and completion; re-writing the whole
    # document for a value that did not move would be pure IO on the wake path.
    writes: list[str] = []
    real = evidence_module._atomic_write
    monkeypatch.setattr(
        evidence_module,
        "_atomic_write",
        lambda path, text: (writes.append(path), real(path, text))[1],
    )
    store = _store(tmp_path)

    store.record_queue_depth(NOVA, 1)
    store.record_queue_depth(NOVA, 1)

    assert len(writes) == 1


# --- durability: the ledger's age-of-proof has to span a restart ------------


def test_evidence_survives_the_daemon_restarting(tmp_path) -> None:
    # The whole reason this lives in /var/lib and not /run: a deploy must not make
    # every proven capability read as never-proven.
    path = str(tmp_path / "evidence.json")
    first = EvidenceStore(path, now=_Clock())
    first.record_wake_ok(NOVA, "delivery-1", route="github", synthetic=False)
    first.record_delivery_accepted("github")

    revived = EvidenceStore(path, now=_Clock("2026-07-28T09:00:00+00:00"))
    revived.record_wake_ok(NOVA, "delivery-2", route="github", synthetic=False)

    wake = revived.snapshot().agent_wakes[NOVA]
    assert wake.ok == 2  # counted on, not reset
    assert wake.last_ok_delivery == "delivery-2"
    assert revived.snapshot().delivery_sinks["github"].accepted == 1


def test_the_document_on_disk_is_the_documented_shape(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_wake_ok(NOVA, "delivery-1", route="github", synthetic=False)

    written = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert written["version"] == EVIDENCE_VERSION
    assert written["agent_wakes"][NOVA]["last_ok_delivery"] == "delivery-1"
    assert written["updated_at"] == "2026-07-27T12:00:00+00:00"


def test_the_document_is_world_readable_for_the_nocs_converge(tmp_path) -> None:
    # The NOC's converge reads this without a privilege grant, and it holds no
    # secrets — so 0644, not mkstemp's default 0600.
    store = _store(tmp_path)
    store.record_wake_ok(NOVA, "delivery-1", route="github", synthetic=False)

    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o644


def test_a_flush_leaves_no_temp_files_behind(tmp_path) -> None:
    store = _store(tmp_path)
    for index in range(5):
        store.record_wake_ok(NOVA, f"delivery-{index}", route="github", synthetic=False)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["evidence.json"]


# --- degradation: the instrument must never break the thing it instruments ---


def test_an_unwritable_store_degrades_to_memory_and_warns_once(tmp_path, caplog) -> None:
    # A wake matters more than its evidence. An unwritable state dir must cost one
    # warning, not a line per delivery and never a failed wake.
    store = EvidenceStore(str(tmp_path / "no-such-dir" / "evidence.json"), now=_Clock())

    with caplog.at_level("WARNING", logger="basecradle_router.evidence"):
        for index in range(4):
            store.record_wake_ok(NOVA, f"delivery-{index}", route="github", synthetic=False)

    assert store.snapshot().agent_wakes[NOVA].ok == 4  # still recorded in memory
    assert caplog.text.count("event=evidence_write_failed") == 1


def test_a_corrupt_document_is_treated_as_no_evidence(tmp_path, caplog) -> None:
    # The safe direction: report a capability as unproven until it is proven again,
    # rather than trust a document we could not parse.
    path = tmp_path / "evidence.json"
    path.write_text("{ this is not json", encoding="utf-8")

    with caplog.at_level("WARNING", logger="basecradle_router.evidence"):
        document = read_evidence(str(path))

    assert document.agent_wakes == {}
    assert "event=evidence_unreadable" in caplog.text


def test_a_document_from_an_unknown_version_is_ignored(tmp_path) -> None:
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps({"version": 99, "agent_wakes": {NOVA: {"ok": 7}}}), encoding="utf-8")

    assert read_evidence(str(path)).agent_wakes == {}


def test_unknown_fields_in_a_document_are_dropped_not_fatal(tmp_path) -> None:
    # A hand-edited or newer document must never stop the daemon booting.
    path = tmp_path / "evidence.json"
    path.write_text(
        json.dumps(
            {
                "version": EVIDENCE_VERSION,
                "agent_wakes": {NOVA: {"ok": 3, "invented_by_a_future_build": True}},
            }
        ),
        encoding="utf-8",
    )

    assert read_evidence(str(path)).agent_wakes[NOVA].ok == 3


def test_reading_an_absent_document_is_not_an_error(tmp_path) -> None:
    assert read_evidence(str(tmp_path / "never-written.json")) == EvidenceDocument()


def test_an_in_memory_store_never_touches_the_filesystem(tmp_path, monkeypatch) -> None:
    # The offline default: a bare Pipeline must be constructible in a test without
    # writing anywhere, so nothing in the suite can reach the box's state dir.
    def explode(*args, **kwargs):
        raise AssertionError("an in-memory store must not write")

    monkeypatch.setattr("basecradle_router.evidence._atomic_write", explode)
    store = EvidenceStore(None, now=_Clock())

    store.record_wake_ok(NOVA, "delivery-1", route="github", synthetic=False)

    assert store.snapshot().agent_wakes[NOVA].ok == 1


def test_read_evidence_with_persistence_disabled_is_an_empty_document() -> None:
    assert read_evidence(None) == EvidenceDocument()

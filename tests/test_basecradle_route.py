"""The basecradle route's signature verification + normalization.

Offline only: a fabricated secret and a hand-computed signature, no network. The
verify boundary is the shared HMAC implementation (also exercised via github), so
these tests focus on this route's header names and its normalize contract.

Fabricated platform event: a new message lands on a timeline the fleet harness
persona @jt (``jt``, an AI user) views; the platform signs and POSTs it. @jt's
fabricated user uuid is a well-formed UUIDv7.
"""

import hashlib
import hmac
import json

import pytest

from basecradle_router.models import EventKind, Recipient
from basecradle_router.routes import (
    BasecradleRoute,
    InboundRequest,
    PayloadError,
    Route,
    SignatureError,
)
from basecradle_router.routes.basecradle import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
)

SECRET = "s3cret-fake-integration-secret"
JT_UUID = "019e916c-7f45-700e-afc0-f45557b237b7"  # @jt's BaseCradle user uuid
TIMELINE_UUID = "0192aaaa-bbbb-7ccc-8ddd-eeeeffff0000"
DELIVERY = "0192f3a4-5b6c-7d8e-9f01-23456789abcd"  # the event_id / delivery id


def _sign(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _payload(
    *,
    recipient_uuid: str | None = JT_UUID,
    timeline_uuid: str | None = TIMELINE_UUID,
    event: str = "message.created",
) -> dict:
    payload: dict = {
        "event": event,
        "event_id": DELIVERY,
        "occurred_at": "2026-06-09T00:00:00Z",
        "actor_uuid": None,
        "resource": {"type": "message", "uuid": TIMELINE_UUID, "url": "https://x/m/1"},
    }
    if recipient_uuid is not None:
        payload["recipient_uuid"] = recipient_uuid
    if timeline_uuid is not None:
        payload["timeline_uuid"] = timeline_uuid
    return payload


def _request(
    payload: dict | None = None,
    *,
    raw_body: bytes | None = None,
    event: str | None = "message.created",
    delivery: str | None = DELIVERY,
    signature: str | None = "auto",
) -> InboundRequest:
    body = raw_body if raw_body is not None else json.dumps(payload or _payload()).encode("utf-8")
    headers: dict[str, str] = {}
    if event is not None:
        headers[EVENT_HEADER] = event
    if delivery is not None:
        headers[DELIVERY_HEADER] = delivery
    if signature == "auto":
        headers[SIGNATURE_HEADER] = _sign(body)
    elif signature is not None:
        headers[SIGNATURE_HEADER] = signature
    return InboundRequest(headers=headers, body=body)


# --- contract + verify -----------------------------------------------------


def test_basecradle_route_satisfies_the_protocol() -> None:
    assert isinstance(BasecradleRoute(), Route)
    assert BasecradleRoute().name == "basecradle"


def test_verify_accepts_a_correct_signature() -> None:
    body = json.dumps(_payload()).encode("utf-8")
    BasecradleRoute().verify(_request(raw_body=body, signature=_sign(body)), SECRET)


def test_verify_accepts_regardless_of_header_case() -> None:
    body = b'{"event":"message.created"}'
    req = InboundRequest(headers={"x-basecradle-signature": _sign(body)}, body=body)
    BasecradleRoute().verify(req, SECRET)


def test_verify_rejects_a_tampered_body() -> None:
    body = json.dumps(_payload()).encode("utf-8")
    tampered = InboundRequest(headers={SIGNATURE_HEADER: _sign(body)}, body=body + b" ")
    with pytest.raises(SignatureError, match="does not match"):
        BasecradleRoute().verify(tampered, SECRET)


def test_verify_rejects_a_missing_header() -> None:
    body = b"{}"
    with pytest.raises(SignatureError, match="missing"):
        BasecradleRoute().verify(InboundRequest(headers={}, body=body), SECRET)


def test_verify_rejects_a_malformed_header() -> None:
    body = b"{}"
    bare = _sign(body).removeprefix("sha256=")
    req = InboundRequest(headers={SIGNATURE_HEADER: bare}, body=body)
    with pytest.raises(SignatureError, match="malformed"):
        BasecradleRoute().verify(req, SECRET)


# --- normalize -------------------------------------------------------------


def test_normalize_message_created_round_trips() -> None:
    event = BasecradleRoute().normalize(_request())
    assert event is not None
    assert event.source == "basecradle"
    assert event.kind is EventKind.PLATFORM_EVENT
    # Resolved by the recipient's BaseCradle user uuid, not a repo.
    assert event.recipient == Recipient(by="recipient_uuid", value=JT_UUID)
    # The wake hands the harness the timeline to process.
    assert event.wake_arg == TIMELINE_UUID
    assert event.delivery_id == DELIVERY
    # No GitHub-style issue to report on — the harness replies on the timeline itself.
    assert event.origin is None


def test_normalize_ignores_non_message_event() -> None:
    # A non-message delivery is a clean ignore (None), not an error — the harness
    # wake is for new messages; other delivery types need no wake.
    assert BasecradleRoute().normalize(_request(event="reaction.created")) is None


def test_normalize_ignores_event_with_no_event_header() -> None:
    assert BasecradleRoute().normalize(_request(event=None)) is None


def test_normalize_rejects_malformed_json() -> None:
    with pytest.raises(PayloadError, match="not valid JSON"):
        BasecradleRoute().normalize(_request(raw_body=b"{not json"))


def test_normalize_rejects_non_object_body() -> None:
    with pytest.raises(PayloadError, match="must be a JSON object"):
        BasecradleRoute().normalize(_request(raw_body=b"[1, 2, 3]"))


def test_normalize_rejects_missing_delivery_header() -> None:
    with pytest.raises(PayloadError, match=DELIVERY_HEADER):
        BasecradleRoute().normalize(_request(delivery=None))


def test_normalize_rejects_missing_recipient_uuid() -> None:
    with pytest.raises(PayloadError, match="recipient_uuid"):
        BasecradleRoute().normalize(_request(_payload(recipient_uuid=None)))


def test_normalize_rejects_missing_timeline_uuid() -> None:
    with pytest.raises(PayloadError, match="timeline_uuid"):
        BasecradleRoute().normalize(_request(_payload(timeline_uuid=None)))

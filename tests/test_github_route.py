"""The github route's signature verification — the security boundary.

Offline only: a fabricated secret and a hand-computed signature, no network.
Test cast: John Doe (``john``, human) / Nova Digital (``nova``, AI).
"""

import hashlib
import hmac
import json

import pytest

from basecradle_router.models import EventKind
from basecradle_router.routes import (
    GithubRoute,
    InboundRequest,
    PayloadError,
    Route,
    SignatureError,
)
from basecradle_router.routes.github import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
)

SECRET = "s3cret-fake-webhook-signing-key"
BODY = b'{"action":"opened","issue":{"number":42}}'


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _request(body: bytes = BODY, signature: str | None = None) -> InboundRequest:
    headers = {} if signature is None else {SIGNATURE_HEADER: signature}
    return InboundRequest(headers=headers, body=body)


def test_github_route_satisfies_the_protocol() -> None:
    assert isinstance(GithubRoute(), Route)
    assert GithubRoute().name == "github"


def test_verify_accepts_a_correct_signature() -> None:
    GithubRoute().verify(_request(signature=_sign(BODY, SECRET)), SECRET)


def test_verify_accepts_regardless_of_header_case() -> None:
    req = InboundRequest(headers={"x-hub-signature-256": _sign(BODY, SECRET)}, body=BODY)
    GithubRoute().verify(req, SECRET)


def test_verify_rejects_a_tampered_body() -> None:
    signature = _sign(BODY, SECRET)
    tampered = _request(body=BODY + b" ", signature=signature)
    with pytest.raises(SignatureError, match="does not match"):
        GithubRoute().verify(tampered, SECRET)


def test_verify_rejects_the_wrong_secret() -> None:
    signature = _sign(BODY, "a-different-secret")
    with pytest.raises(SignatureError, match="does not match"):
        GithubRoute().verify(_request(signature=signature), SECRET)


def test_verify_rejects_a_missing_header() -> None:
    with pytest.raises(SignatureError, match="missing"):
        GithubRoute().verify(_request(signature=None), SECRET)


def test_verify_rejects_a_malformed_header() -> None:
    # A bare hexdigest with no 'sha256=' prefix is malformed.
    bare = _sign(BODY, SECRET).removeprefix("sha256=")
    with pytest.raises(SignatureError, match="malformed"):
        GithubRoute().verify(_request(signature=bare), SECRET)


def test_verify_rejects_the_wrong_algorithm_prefix() -> None:
    sha1ish = "sha1=" + _sign(BODY, SECRET).removeprefix("sha256=")
    with pytest.raises(SignatureError, match="malformed"):
        GithubRoute().verify(_request(signature=sha1ish), SECRET)


def test_verify_rejects_an_empty_signature_header() -> None:
    with pytest.raises(SignatureError, match="malformed"):
        GithubRoute().verify(_request(signature=""), SECRET)


def test_verify_binds_the_signature_to_the_exact_body() -> None:
    # A signature valid for one body must not validate a different body.
    other_body = b'{"action":"closed"}'
    signature = _sign(BODY, SECRET)
    with pytest.raises(SignatureError):
        GithubRoute().verify(_request(body=other_body, signature=signature), SECRET)


def test_verify_rejects_a_non_ascii_signature_without_crashing() -> None:
    # A header digest with non-ASCII bytes must reject as a SignatureError, not
    # leak a TypeError from hmac.compare_digest's str/ASCII restriction.
    with pytest.raises(SignatureError, match="does not match"):
        GithubRoute().verify(_request(signature="sha256=café"), SECRET)


# --- normalize -------------------------------------------------------------
#
# Fabricated handoff: John Doe (human) files an issue on basecradle-python for
# Nova Digital's repo agent to work. Payloads are hand-built GitHub `issues`
# webhooks; no network.

DELIVERY = "0192f3a4-5b6c-7d8e-9f01-23456789abcd"  # well-formed UUIDv7
TARGET_REPO = "basecradle/basecradle-python"
ISSUE_NUMBER = 42
ISSUE_URL = f"https://github.com/{TARGET_REPO}/issues/{ISSUE_NUMBER}"


def _issues_payload(
    action: str = "opened",
    labels: tuple[str, ...] = ("handoff",),
    added_label: str = "handoff",
    repo: str = TARGET_REPO,
    number: int = ISSUE_NUMBER,
) -> dict:
    payload: dict = {
        "action": action,
        "issue": {
            "number": number,
            "title": "Mirror the wire-shape change",
            "html_url": f"https://github.com/{repo}/issues/{number}",
            "labels": [{"name": name} for name in labels],
        },
        "repository": {"full_name": repo},
    }
    if action == "labeled":
        payload["label"] = {"name": added_label}
    return payload


def _issues_request(
    payload: dict | None = None,
    *,
    raw_body: bytes | None = None,
    event: str | None = "issues",
    delivery: str | None = DELIVERY,
) -> InboundRequest:
    headers: dict[str, str] = {}
    if event is not None:
        headers[EVENT_HEADER] = event
    if delivery is not None:
        headers[DELIVERY_HEADER] = delivery
    body = raw_body if raw_body is not None else json.dumps(payload or {}).encode("utf-8")
    return InboundRequest(headers=headers, body=body)


def test_normalize_opened_handoff_round_trips() -> None:
    event = GithubRoute().normalize(_issues_request(_issues_payload(action="opened")))
    assert event is not None
    assert event.source == "github"
    assert event.kind is EventKind.HANDOFF
    assert event.target_repo == TARGET_REPO
    assert event.origin.repo == TARGET_REPO
    assert event.origin.number == ISSUE_NUMBER
    assert event.origin.url == ISSUE_URL
    assert event.origin.title == "Mirror the wire-shape change"
    assert event.trigger == f"Cross-repo handoff: work {ISSUE_URL}"
    assert event.delivery_id == DELIVERY


def test_normalize_labeled_handoff_round_trips() -> None:
    # The handoff label is the one just added.
    payload = _issues_payload(action="labeled", labels=(), added_label="handoff")
    event = GithubRoute().normalize(_issues_request(payload))
    assert event is not None
    assert event.kind is EventKind.HANDOFF


def test_normalize_ignores_opened_without_handoff_label() -> None:
    payload = _issues_payload(action="opened", labels=("bug", "enhancement"))
    assert GithubRoute().normalize(_issues_request(payload)) is None


def test_normalize_ignores_unrelated_label_on_already_handoff_issue() -> None:
    # A 'labeled' event adding a non-handoff label must not re-trigger, even if
    # the issue already carries the handoff label.
    payload = _issues_payload(action="labeled", labels=("handoff",), added_label="bug")
    assert GithubRoute().normalize(_issues_request(payload)) is None


def test_normalize_ignores_non_issues_event() -> None:
    assert GithubRoute().normalize(_issues_request(_issues_payload(), event="push")) is None


def test_normalize_ignores_ping_event() -> None:
    req = _issues_request(raw_body=b'{"zen": "Non-blocking is better."}', event="ping")
    assert GithubRoute().normalize(req) is None


def test_normalize_ignores_non_actionable_action() -> None:
    payload = _issues_payload(action="closed")
    assert GithubRoute().normalize(_issues_request(payload)) is None


def test_normalize_rejects_malformed_json() -> None:
    with pytest.raises(PayloadError, match="not valid JSON"):
        GithubRoute().normalize(_issues_request(raw_body=b"{not json"))


def test_normalize_rejects_non_object_body() -> None:
    with pytest.raises(PayloadError, match="must be a JSON object"):
        GithubRoute().normalize(_issues_request(raw_body=b"[1, 2, 3]"))


def test_normalize_rejects_missing_issue_object() -> None:
    with pytest.raises(PayloadError, match="'issue' object"):
        GithubRoute().normalize(_issues_request(raw_body=b'{"action": "opened"}'))


def test_normalize_rejects_handoff_missing_repository() -> None:
    payload = _issues_payload(action="opened")
    del payload["repository"]
    with pytest.raises(PayloadError, match="'repository' object"):
        GithubRoute().normalize(_issues_request(payload))


def test_normalize_rejects_non_integer_issue_number() -> None:
    payload = _issues_payload(action="opened")
    payload["issue"]["number"] = "42"
    with pytest.raises(PayloadError, match="issue.number"):
        GithubRoute().normalize(_issues_request(payload))


def test_normalize_rejects_missing_delivery_header() -> None:
    payload = _issues_payload(action="opened")
    with pytest.raises(PayloadError, match=DELIVERY_HEADER):
        GithubRoute().normalize(_issues_request(payload, delivery=None))

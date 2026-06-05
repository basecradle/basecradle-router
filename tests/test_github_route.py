"""The github route's signature verification — the security boundary.

Offline only: a fabricated secret and a hand-computed signature, no network.
Test cast: John Doe (``john``, human) / Nova Digital (``nova``, AI).
"""

import hashlib
import hmac

import pytest

from basecradle_router.routes import (
    GithubRoute,
    InboundRequest,
    Route,
    SignatureError,
)
from basecradle_router.routes.github import SIGNATURE_HEADER

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


def test_normalize_is_not_yet_implemented() -> None:
    with pytest.raises(NotImplementedError):
        GithubRoute().normalize(_request(signature=_sign(BODY, SECRET)))

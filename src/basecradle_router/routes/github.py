"""The ``github`` route — v0's only event source.

GitHub signs each webhook with HMAC-SHA256 over the raw request body, keyed by
the per-route shared secret, and delivers the digest in the
``X-Hub-Signature-256`` header as ``sha256=<hexdigest>``. This module's
:meth:`GithubRoute.verify` is the security boundary: nothing unsigned,
malformed, or tampered gets past it into the core pipeline.

Payload normalization (handoff issue → :class:`~basecradle_router.models.Event`)
is the github route's other half, built separately; until then
:meth:`GithubRoute.normalize` is intentionally unimplemented.
"""

from __future__ import annotations

import hmac
from hashlib import sha256

from basecradle_router.models import Event
from basecradle_router.routes.base import (
    InboundRequest,
    SignatureError,
)

SIGNATURE_HEADER = "X-Hub-Signature-256"
_ALGORITHM_PREFIX = "sha256="


class GithubRoute:
    """The GitHub webhook route. ``name`` is the source key the registry uses."""

    name = "github"

    def verify(self, request: InboundRequest, secret: str) -> None:
        """Raise :class:`SignatureError` unless the request carries a valid signature.

        Valid means: a present ``X-Hub-Signature-256`` header of the form
        ``sha256=<hexdigest>`` whose digest equals the HMAC-SHA256 of the raw
        body under ``secret``. The comparison is constant-time.
        """
        provided = request.header(SIGNATURE_HEADER)
        if provided is None:
            raise SignatureError(f"missing {SIGNATURE_HEADER} header")
        if not provided.startswith(_ALGORITHM_PREFIX):
            raise SignatureError(
                f"malformed {SIGNATURE_HEADER}: expected '{_ALGORITHM_PREFIX}<hexdigest>'"
            )

        expected = (
            _ALGORITHM_PREFIX + hmac.new(secret.encode("utf-8"), request.body, sha256).hexdigest()
        )

        # Compare as bytes: hmac.compare_digest raises TypeError on a str with
        # non-ASCII chars, and the header value is attacker-controlled.
        if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("ascii")):
            raise SignatureError(f"{SIGNATURE_HEADER} does not match the request body")

    def normalize(self, request: InboundRequest) -> Event | None:
        """Normalize a GitHub webhook into an :class:`Event` — built separately."""
        raise NotImplementedError("github payload normalization is not yet implemented")

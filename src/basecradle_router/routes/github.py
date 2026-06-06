"""The ``github`` route — v0's only event source.

GitHub signs each webhook with HMAC-SHA256 over the raw request body, keyed by
the per-route shared secret, and delivers the digest in the
``X-Hub-Signature-256`` header as ``sha256=<hexdigest>``. This module's
:meth:`GithubRoute.verify` is the security boundary: nothing unsigned,
malformed, or tampered gets past it into the core pipeline.

:meth:`GithubRoute.normalize` is the other half: it turns a verified ``issues``
webhook into a core :class:`~basecradle_router.models.Event`, gating on the
``handoff`` label so only a handoff issue wakes an agent — everything else is a
well-formed *ignore*, not an error. As defense-in-depth it *also* gates on the
webhook ``sender``: a handoff only wakes an agent if a **trusted fleet actor**
applied the label. Because this check runs *after* :meth:`verify`, the ``sender``
field is GitHub-attested and not attacker-spoofable.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Iterable
from hashlib import sha256
from typing import Any

from basecradle_router.models import Event, EventKind, IssueRef
from basecradle_router.routes.base import (
    InboundRequest,
    PayloadError,
    SignatureError,
    UntrustedSenderError,
)

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"
DELIVERY_HEADER = "X-GitHub-Delivery"

_ALGORITHM_PREFIX = "sha256="
HANDOFF_LABEL = "handoff"
_ACTIONABLE_ACTIONS = frozenset({"opened", "labeled"})


class GithubRoute:
    """The GitHub webhook route. ``name`` is the source key the registry uses.

    ``trusted_actors`` is the allow-list of GitHub logins permitted to trigger a
    wake — the fleet's org members and App bots. A handoff whose ``sender`` is not
    on it is rejected (defense-in-depth behind GitHub's own permission model).
    GitHub logins are case-insensitive, so the list is matched case-insensitively.
    """

    name = "github"

    def __init__(self, trusted_actors: Iterable[str]) -> None:
        self._trusted_actors = frozenset(actor.lower() for actor in trusted_actors)

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
        """Turn a verified ``issues`` webhook into an :class:`Event`, or ignore it.

        Returns ``None`` (a well-formed ignore) for any webhook that is not a
        handoff: a non-``issues`` event, a non-``opened``/``labeled`` action, or
        an issue without the ``handoff`` label. Raises :class:`PayloadError` only
        when an ``issues`` payload is structurally malformed, and
        :class:`UntrustedSenderError` when a genuine handoff was triggered by an
        actor who is not a trusted fleet member.
        """
        if request.header(EVENT_HEADER) != "issues":
            return None

        data = _parse(request.body)
        action = data.get("action")
        if action not in _ACTIONABLE_ACTIONS:
            return None

        issue = data.get("issue")
        if not isinstance(issue, dict):
            raise PayloadError("issues event is missing an 'issue' object")

        if not _is_handoff(action, issue, data):
            return None

        # Defense-in-depth: the label says "handoff", but only a trusted fleet
        # actor may actually trigger a wake. This runs after verify(), so the
        # sender is GitHub-attested. An untrusted (or unidentifiable) sender is a
        # rejection, not a wake — fail closed.
        self._require_trusted_sender(data)

        repository = data.get("repository")
        if not isinstance(repository, dict):
            raise PayloadError("issues event is missing a 'repository' object")

        delivery_id = request.header(DELIVERY_HEADER)
        if not delivery_id:
            raise PayloadError(f"missing {DELIVERY_HEADER} header")

        try:
            origin = IssueRef(
                repo=_text(repository, "full_name", "repository.full_name"),
                number=_int(issue, "number", "issue.number"),
                url=_text(issue, "html_url", "issue.html_url"),
                title=_text(issue, "title", "issue.title"),
            )
            return Event(
                source=self.name,
                kind=EventKind.HANDOFF,
                target_repo=origin.repo,
                origin=origin,
                trigger=f"Cross-repo handoff: work {origin.url}",
                delivery_id=delivery_id,
            )
        except ValueError as exc:
            raise PayloadError(f"malformed issues payload: {exc}") from exc

    def _require_trusted_sender(self, data: dict[str, Any]) -> None:
        """Raise :class:`UntrustedSenderError` unless the webhook's actor is trusted.

        The ``sender`` is the actor GitHub attributes the delivery to — who opened
        the issue or applied the label. We can't identify the actor from a missing
        or malformed ``sender``, so that fails closed (an untrusted sender), the
        same as a known-but-not-allowed login.
        """
        sender = data.get("sender")
        login = sender.get("login") if isinstance(sender, dict) else None
        if not isinstance(login, str) or not login:
            raise UntrustedSenderError("handoff webhook has no identifiable sender")
        if login.lower() not in self._trusted_actors:
            raise UntrustedSenderError(
                f"handoff label applied by untrusted actor {login!r}; not a fleet member"
            )


def _parse(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise PayloadError(f"body is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PayloadError("webhook body must be a JSON object")
    return data


def _is_handoff(action: str, issue: dict[str, Any], data: dict[str, Any]) -> bool:
    """Whether this event makes the issue a handoff *now*.

    ``opened``: the issue carries the ``handoff`` label. ``labeled``: the label
    just added is ``handoff`` — so an unrelated label added to an
    already-handoff issue does not re-trigger a wake.
    """
    if action == "labeled":
        added = data.get("label")
        return isinstance(added, dict) and added.get("name") == HANDOFF_LABEL

    labels = issue.get("labels")
    if not isinstance(labels, list):
        return False
    return any(isinstance(label, dict) and label.get("name") == HANDOFF_LABEL for label in labels)


def _text(obj: dict[str, Any], key: str, label: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise PayloadError(f"{label} must be a string")
    return value


def _int(obj: dict[str, Any], key: str, label: str) -> int:
    value = obj.get(key)
    # bool is an int subclass; a JSON true/false is not a valid issue number.
    if not isinstance(value, int) or isinstance(value, bool):
        raise PayloadError(f"{label} must be an integer")
    return value

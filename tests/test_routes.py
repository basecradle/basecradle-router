"""The route contract and registry, pinned by a fake route — no real source.

Test cast: John Doe (``john``, human) / Nova Digital (``nova``, AI).
"""

import pytest

from basecradle_router.models import Event, EventKind, IssueRef, Recipient
from basecradle_router.routes import (
    InboundRequest,
    PayloadError,
    Route,
    RouteRegistry,
    SignatureError,
    UnknownRouteError,
)

DELIVERY_ID = "0192f3a4-5b6c-7d8e-9f01-23456789abcd"  # well-formed UUIDv7
SECRET = "s3cret-fake"


class FakeRoute:
    """A minimal conforming route: a fixed-header signature and a handoff marker."""

    name = "fake"
    # Part of the Route protocol: the Recipient.by tag this source resolves by, so
    # the wake-edge claims emitter can ask the registry which agents a route can
    # actually reach without ever naming a source itself.
    recipient_kind = "repo"

    def verify(self, request: InboundRequest, secret: str) -> None:
        if request.header("X-Fake-Signature") != secret:
            raise SignatureError("fake signature mismatch")

    def normalize(self, request: InboundRequest) -> Event | None:
        if request.header("X-Fake-Event") != "handoff":
            return None
        if not request.body:
            raise PayloadError("empty body")
        return Event(
            source=self.name,
            kind=EventKind.HANDOFF,
            recipient=Recipient(by="repo", value="basecradle/basecradle-python"),
            wake_arg="Cross-repo handoff: work https://github.com/basecradle/basecradle-python/issues/42",
            delivery_id=DELIVERY_ID,
            origin=IssueRef(
                repo="basecradle/basecradle-python",
                number=42,
                url="https://github.com/basecradle/basecradle-python/issues/42",
                title="Mirror the wire-shape change",
            ),
        )


def _request(**headers: str) -> InboundRequest:
    return InboundRequest(headers=headers, body=b'{"ok": true}')


def test_fake_route_satisfies_the_protocol() -> None:
    assert isinstance(FakeRoute(), Route)


def test_inbound_request_header_is_case_insensitive() -> None:
    req = InboundRequest(headers={"X-Fake-Event": "handoff"}, body=b"")
    assert req.header("x-fake-event") == "handoff"
    assert req.header("X-FAKE-EVENT") == "handoff"
    assert req.header("missing") is None


def test_verify_passes_and_rejects() -> None:
    route = FakeRoute()
    route.verify(_request(**{"X-Fake-Signature": SECRET}), SECRET)
    with pytest.raises(SignatureError):
        route.verify(_request(**{"X-Fake-Signature": "wrong"}), SECRET)
    with pytest.raises(SignatureError):
        route.verify(_request(), SECRET)  # unsigned


def test_normalize_round_trips_a_handoff() -> None:
    event = FakeRoute().normalize(_request(**{"X-Fake-Event": "handoff"}))
    assert event is not None
    assert event.kind is EventKind.HANDOFF
    assert event.recipient == Recipient(by="repo", value="basecradle/basecradle-python")


def test_normalize_ignores_non_handoff() -> None:
    assert FakeRoute().normalize(_request(**{"X-Fake-Event": "push"})) is None


def test_normalize_raises_on_malformed() -> None:
    bad = InboundRequest(headers={"X-Fake-Event": "handoff"}, body=b"")
    with pytest.raises(PayloadError):
        FakeRoute().normalize(bad)


def test_registry_register_and_get() -> None:
    registry = RouteRegistry()
    route = FakeRoute()
    registry.register(route)
    assert registry.get("fake") is route
    assert "fake" in registry
    assert registry.names() == frozenset({"fake"})


def test_registry_rejects_duplicate() -> None:
    registry = RouteRegistry()
    registry.register(FakeRoute())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(FakeRoute())


def test_registry_rejects_nameless_route() -> None:
    class Nameless:
        name = ""

    with pytest.raises(ValueError, match="non-empty name"):
        RouteRegistry().register(Nameless())  # type: ignore[arg-type]


def test_registry_rejects_nonconforming_route() -> None:
    class HalfRoute:
        name = "half"  # has a name but no verify/normalize

    with pytest.raises(TypeError, match="does not implement"):
        RouteRegistry().register(HalfRoute())  # type: ignore[arg-type]


def test_registry_unknown_source_fails_with_known_list() -> None:
    registry = RouteRegistry()
    registry.register(FakeRoute())
    with pytest.raises(UnknownRouteError, match="known routes: fake"):
        registry.get("github")

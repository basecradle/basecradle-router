"""The source → route registry.

The core consults this to find the route for an incoming event's source. It
holds no source-specific knowledge — that is the whole point of the routes/core
split.
"""

from __future__ import annotations

from basecradle_router.routes.base import Route, RouteError


class UnknownRouteError(RouteError):
    """No route is registered for the requested source."""


class RouteRegistry:
    """A name → :class:`Route` map. One instance is built at startup."""

    def __init__(self) -> None:
        self._routes: dict[str, Route] = {}

    def register(self, route: Route) -> None:
        name = getattr(route, "name", "")
        if not name:
            raise ValueError("route must declare a non-empty name")
        if not isinstance(route, Route):
            raise TypeError(f"route {name!r} does not implement verify/normalize")
        if name in self._routes:
            raise ValueError(f"route {name!r} is already registered")
        self._routes[name] = route

    def get(self, name: str) -> Route:
        try:
            return self._routes[name]
        except KeyError:
            known = ", ".join(sorted(self._routes)) or "(none)"
            raise UnknownRouteError(
                f"no route registered for source {name!r}; known routes: {known}"
            ) from None

    def names(self) -> frozenset[str]:
        return frozenset(self._routes)

    def __contains__(self, name: object) -> bool:
        return name in self._routes

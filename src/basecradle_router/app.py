"""The composition root: build the runnable ASGI app from the environment.

This is what production runs:

    uvicorn --factory basecradle_router.app:create_app

It wires the real collaborators — config from the environment, the ``github``
route, the home-server waker — into a :class:`~basecradle_router.server.WebhookServer`.
It is a *factory*, not a module-level ``app``, so importing the package never
reads config or touches the network; the daemon builds itself only when started.

**The router never merges.** Auto-merge of a captain's own green PR is performed
by GitHub native auto-merge — during its wake the agent opens its PR and enables
``gh pr merge --auto --squash`` under its own bot identity, so the platform merges
when CI goes green. The router holds no GitHub credential by design, so there is
no merge stage to wire here (see issue #38 for the decision).
"""

from __future__ import annotations

from collections.abc import Mapping

from basecradle_router.config import load_config, load_github_trusted_actors
from basecradle_router.pipeline import Pipeline
from basecradle_router.routes import RouteRegistry
from basecradle_router.routes.basecradle import BasecradleRoute
from basecradle_router.routes.github import GithubRoute
from basecradle_router.server import WebhookServer
from basecradle_router.wake import HomeServerWaker, Waker


def create_app(
    env: Mapping[str, str] | None = None, *, waker: Waker | None = None
) -> WebhookServer:
    """Build the daemon's ASGI app from ``env`` (defaults to the process environment).

    Raises :class:`~basecradle_router.config.ConfigError` (naming the missing
    variable) if required configuration is absent. ``waker`` is injectable so tests
    never reach the real wake boundary; production uses the default
    :class:`~basecradle_router.wake.HomeServerWaker`.
    """
    config = load_config(env)
    registry = RouteRegistry()
    # Register a route only when its source is enabled, so each route's own
    # required config (the github trust allow-list) is demanded only when in use.
    if "github" in config.enabled_routes:
        registry.register(GithubRoute(load_github_trusted_actors(env)))
    if "basecradle" in config.enabled_routes:
        registry.register(BasecradleRoute())
    pipeline = Pipeline(
        registry=registry,
        config=config,
        waker=waker or HomeServerWaker(),
    )
    return WebhookServer(pipeline)

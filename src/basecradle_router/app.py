"""The composition root: build the runnable ASGI app from the environment.

This is what production runs:

    uvicorn --factory basecradle_router.app:create_app

It wires the real collaborators — config from the environment, the ``github``
route, the home-server waker — into a :class:`~basecradle_router.server.WebhookServer`.
It is a *factory*, not a module-level ``app``, so importing the package never
reads config or touches the network; the daemon builds itself only when started.

The merge stage is intentionally a **no-op for now**: the default ``pr_provider``
returns no PR, so the router wakes agents and they open their own PRs — auto-merge
automation (the live ``pr_provider`` + a real merger) is deferred (#38). Until then
the merger is never invoked, so a placeholder that fails loudly stands in for it.
"""

from __future__ import annotations

from collections.abc import Mapping

from basecradle_router.config import load_config
from basecradle_router.merge_policy import MergePolicy, PullRequest
from basecradle_router.pipeline import Pipeline
from basecradle_router.routes import RouteRegistry
from basecradle_router.routes.github import GithubRoute
from basecradle_router.server import WebhookServer
from basecradle_router.wake import HomeServerWaker, Waker


class _UnwiredMerger:
    """Stands in until live merge automation (#38). Never called today — the default
    ``pr_provider`` yields no PR, so the merge stage short-circuits before reaching a
    merger. Fails loudly if a ``pr_provider`` is ever wired without a real merger."""

    def merge(self, pr: PullRequest) -> None:
        raise NotImplementedError("live merge automation is not wired yet — see issue #38")


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
    registry.register(GithubRoute())
    pipeline = Pipeline(
        registry=registry,
        config=config,
        waker=waker or HomeServerWaker(),
        merge_policy=MergePolicy(_UnwiredMerger()),
    )
    return WebhookServer(pipeline)

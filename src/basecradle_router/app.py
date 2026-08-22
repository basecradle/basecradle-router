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

from collections.abc import Callable, Mapping

from basecradle_router.breaker import WakeRateBreaker
from basecradle_router.config import (
    Config,
    load_breaker_config,
    load_config,
    load_dedup_ttl,
    load_evidence_path,
    load_github_trusted_actors,
    load_wake_lanes,
    load_wake_lock_dir,
)
from basecradle_router.dedup import DeliveryDeduper
from basecradle_router.evidence import EvidenceStore
from basecradle_router.pipeline import Pipeline
from basecradle_router.routes import RouteRegistry
from basecradle_router.routes.basecradle import BasecradleRoute, load_recipient_keyring
from basecradle_router.routes.github import GithubRoute
from basecradle_router.routes.probe import ProbeRoute
from basecradle_router.server import WebhookServer
from basecradle_router.wake import HomeServerWaker, Waker
from basecradle_router.wakelock import WakeLockGuard


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
    pipeline = Pipeline(
        registry=build_registry(config, env),
        config=config,
        waker=waker or HomeServerWaker(),
        breaker=WakeRateBreaker(load_breaker_config(env)),
        deduper=DeliveryDeduper(load_dedup_ttl(env)),
        wake_lock=WakeLockGuard(lock_dir=load_wake_lock_dir(env)),
        evidence=EvidenceStore(load_evidence_path(env)),
    )
    return WebhookServer(pipeline, lanes=load_wake_lanes(env))


def build_registry(config: Config, env: Mapping[str, str] | None = None) -> RouteRegistry:
    """The enabled routes, wired with their own source-specific config.

    Split out of :func:`create_app` because the claims emitter needs the same
    registry the daemon runs — it asks each route which recipient kind it delivers
    by, to decide whether an agent's wake edge is actually armed. Building it the
    one way keeps the emitter honest: if the daemon could not start with this
    config, neither can the emitter, so the manifest can never describe a router
    that does not exist.

    A route is registered only when its source is enabled, so each route's own
    required config — the github trust allow-list, the basecradle per-recipient
    keyring — is demanded only when in use.
    """
    registry = RouteRegistry()
    if "github" in config.enabled_routes:
        registry.register(
            GithubRoute(
                load_github_trusted_actors(env),
                bot_login_for_repo=_bot_login_for_repo(config),
            )
        )
    if "basecradle" in config.enabled_routes:
        # The keyring is built here, from the same registry the daemon resolves with, so
        # a per-recipient key can only ever be provisioned for an agent that exists
        # (basecradle/basecradle#497). It is loaded eagerly and loudly: a mistyped slug
        # or a retired fallback with an unprovisioned persona stops the daemon at boot
        # rather than surfacing as one persona's deliveries quietly failing to verify.
        registry.register(BasecradleRoute(load_recipient_keyring(config.recipient_index, env)))
    if ProbeRoute.name in config.enabled_routes:
        # The router's own synthetic wake (basecradle-router#208). Registered exactly
        # like a real source and behind the same enable-plus-secret gate, because that
        # is the point: a probe is a genuine signed delivery through the genuine path,
        # not a back door into the middle of it. A box that has not enabled it simply
        # has no lever — the emitter then publishes no synthetic claim, so nothing
        # advertises a capability this deployment does not have.
        registry.register(ProbeRoute())
    return registry


def _bot_login_for_repo(config: Config) -> Callable[[str], str | None]:
    """A resolver from a repo to its captain agent's GitHub bot login, or ``None``.

    The github route's self-comment guard (basecradle-router#129) needs to know a
    repo's own bot identity to suppress an own-comment re-wake loop. The agent
    registry is the authority: a builder's ``bot_slug`` (e.g. ``basecradle-ruby-ai``)
    yields the GitHub App login ``<bot_slug>[bot]``. An unregistered repo (or a
    non-builder without a ``bot_slug``) resolves to ``None`` — the route then
    cannot mistake any sender for that repo's bot, which is safe because an
    unregistered repo wakes no agent at resolve anyway.
    """

    def resolve(repo: str) -> str | None:
        agent = config.agents.get(repo)
        if agent is None or not agent.bot_slug:
            return None
        return f"{agent.bot_slug}[bot]"

    return resolve

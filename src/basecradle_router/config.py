"""Router configuration, loaded from the environment.

No secrets live in the repo. The non-secret agent registry (which repo maps to
which OS user, clone, and bot identity) is a JSON file whose path is given by an
env var; the webhook secrets are read straight from the environment.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from basecradle_router.breaker import BreakerConfig
from basecradle_router.models import Agent, Recipient, WakeKind, _require_repo
from basecradle_router.wake import WAKE_BACKSTOP_MARGIN

_EMPTY: Mapping[str, Agent] = MappingProxyType({})

ENV_PREFIX = "BASECRADLE_ROUTER_"
_AGENTS_VAR = f"{ENV_PREFIX}AGENTS"
_ENABLED_ROUTES_VAR = f"{ENV_PREFIX}ENABLED_ROUTES"
_DEFAULT_ENABLED_ROUTES = ("github",)
_GITHUB_TRUSTED_ACTORS_VAR = f"{ENV_PREFIX}GITHUB_TRUSTED_ACTORS"

_BREAKER_PREFIX = f"{ENV_PREFIX}WAKE_BREAKER_"
_BREAKER_MAX_VAR = f"{_BREAKER_PREFIX}MAX"
_BREAKER_WINDOW_VAR = f"{_BREAKER_PREFIX}WINDOW"
_BREAKER_COOLDOWN_VAR = f"{_BREAKER_PREFIX}COOLDOWN"
_BREAKER_STREAM_MAX_VAR = f"{_BREAKER_PREFIX}STREAM_MAX"

_DEDUP_TTL_VAR = f"{ENV_PREFIX}DEDUP_TTL"

_WAKE_TIMEOUT_VAR = f"{ENV_PREFIX}WAKE_TIMEOUT"
# Mirror of the systemd unit's TimeoutStopSec (deploy/systemd/basecradle-router.service).
# A wake's total wall-clock — its bound plus the waker's subprocess backstop margin —
# must stay under this, so a hung wake is reaped before systemd SIGKILLs the whole
# control-group mid-drain (the #135 failure, in reverse). Kept in sync with the unit.
_STOP_DEADLINE_SECONDS = 120.0


class ConfigError(Exception):
    """Configuration is missing or malformed. The message names the fix."""


@dataclass(frozen=True, slots=True)
class Config:
    """Everything the daemon needs that isn't code.

    ``agents`` maps an agent's stable ``key`` (a ``owner/name`` repo for a github
    builder, a bare slug for a harness persona) to the agent to wake;
    ``recipient_index`` maps a harness persona's BaseCradle user uuid to the same
    agent, so a basecradle event resolves by ``recipient_uuid`` while github
    resolves by repo; ``webhook_secrets`` maps a route name to its signing secret;
    ``enabled_routes`` is the set of routes the daemon will accept events for.
    """

    agents: Mapping[str, Agent]
    enabled_routes: frozenset[str]
    webhook_secrets: Mapping[str, str]
    recipient_index: Mapping[str, Agent] = field(default_factory=lambda: _EMPTY)

    def agent_for(self, key: str) -> Agent:
        try:
            return self.agents[key]
        except KeyError:
            raise ConfigError(
                f"no agent registered for {key!r}; add it to the registry at {_AGENTS_VAR}"
            ) from None

    def agent_for_recipient(self, recipient: Recipient) -> Agent:
        """Resolve a normalized event's :class:`Recipient` to its agent.

        Dispatches on the recipient's source-set tag without naming any source:
        ``"repo"`` is a direct key lookup (a github builder's key *is* its repo);
        ``"recipient_uuid"`` consults the harness-persona index. An unknown tag or
        an unregistered value is a loud :class:`ConfigError` (a registry gap),
        never a silent miss.
        """
        if recipient.by == "repo":
            return self.agent_for(recipient.value)
        if recipient.by == "recipient_uuid":
            try:
                return self.recipient_index[recipient.value]
            except KeyError:
                raise ConfigError(
                    f"no agent registered for recipient_uuid {recipient.value!r}; "
                    f"add it to the registry at {_AGENTS_VAR}"
                ) from None
        raise ConfigError(f"unknown recipient kind {recipient.by!r}")

    def webhook_secret(self, route: str) -> str:
        try:
            return self.webhook_secrets[route]
        except KeyError:
            raise ConfigError(
                f"no webhook secret for route {route!r}; set {_route_secret_var(route)}"
            ) from None


def _route_secret_var(route: str) -> str:
    return f"{ENV_PREFIX}{route.upper()}_WEBHOOK_SECRET"


def _split_csv(raw: str) -> frozenset[str]:
    """Parse a comma-separated env value into stripped, non-empty items."""
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _load_agents(path: str) -> tuple[dict[str, Agent], dict[str, Agent]]:
    """Parse the registry into ``(agents-by-key, agents-by-recipient_uuid)``.

    An entry's ``kind`` selects how it is read: absent or ``"github"`` is a
    builder (keyed by its ``owner/name`` repo, woken via ``claude``); ``"harness"``
    is a non-builder persona (keyed by its bare slug, woken via its ``wake_bin``,
    resolved for basecradle events by its ``recipient_uuid``). Existing github
    entries — which carry no ``kind`` — therefore load unchanged.
    """
    try:
        raw = json.loads(_read(path))
    except FileNotFoundError:
        raise ConfigError(f"agent registry file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"agent registry at {path} is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"agent registry at {path} must be a JSON object of key -> fields")

    agents: dict[str, Agent] = {}
    by_recipient: dict[str, Agent] = {}
    for key, fields in raw.items():
        if not isinstance(fields, dict):
            raise ConfigError(f"agent entry for {key!r} must be a JSON object")
        kind = fields.get("kind", "github")
        if kind == "github":
            agent = _build_github_agent(key, fields)
        elif kind == "harness":
            agent = _build_harness_agent(key, fields)
            # The recipient_uuid index must be a bijection: two entries claiming the
            # same uuid would silently misroute one persona's events to the other.
            # Fail loudly, matching the rest of load_config's posture (no silent miss).
            if agent.recipient_uuid in by_recipient:
                raise ConfigError(
                    f"agent entry for {key!r} reuses recipient_uuid "
                    f"{agent.recipient_uuid!r}, already claimed by "
                    f"{by_recipient[agent.recipient_uuid].key!r}"
                )
            by_recipient[agent.recipient_uuid] = agent
        else:
            raise ConfigError(
                f"agent entry for {key!r} has unknown kind {kind!r} "
                "(expected 'github' or 'harness')"
            )
        agents[key] = agent
    return agents, by_recipient


def _build_github_agent(key: str, fields: dict) -> Agent:
    # A github builder is keyed by the repo it captains; preserve the owner/name
    # shape check the legacy registry relied on (the same rule models.py enforces
    # on a repo, so the two key-spaces stay disjoint — see _build_harness_agent).
    try:
        _require_repo(key, "github agent key")
    except ValueError as exc:
        raise ConfigError(f"agent entry for {key!r} is invalid: {exc}") from None
    try:
        return Agent(
            key=key,
            os_user=fields["os_user"],
            clone_path=fields["clone_path"],
            wake_kind=WakeKind.CLAUDE,
            bot_slug=fields["bot_slug"],
        )
    except KeyError as exc:
        raise ConfigError(f"agent entry for {key!r} is missing {exc.args[0]!r}") from None
    except ValueError as exc:
        raise ConfigError(f"agent entry for {key!r} is invalid: {exc}") from None


def _build_harness_agent(key: str, fields: dict) -> Agent:
    # A harness persona is keyed by a bare slug, never an ``owner/name`` repo —
    # which keeps it out of the github key-space, so a ``Recipient(by="repo")``
    # lookup can never resolve to a harness agent and feed it a builder's trigger.
    if "/" in key:
        raise ConfigError(
            f"agent entry for {key!r} is invalid: a harness agent key must be a "
            "bare slug (no '/'), not an 'owner/name' repo"
        )
    try:
        return Agent(
            key=key,
            os_user=fields["os_user"],
            clone_path=fields["clone_path"],
            wake_kind=WakeKind.HARNESS,
            recipient_uuid=fields["recipient_uuid"],
            wake_bin=fields["wake_bin"],
        )
    except KeyError as exc:
        raise ConfigError(f"agent entry for {key!r} is missing {exc.args[0]!r}") from None
    except ValueError as exc:
        raise ConfigError(f"agent entry for {key!r} is invalid: {exc}") from None


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a :class:`Config` from ``env`` (defaults to ``os.environ``).

    Raises :class:`ConfigError`, naming the missing/invalid variable, if any
    required configuration is absent — required means: the agent registry, and
    a webhook secret for every enabled route.
    """

    env = os.environ if env is None else env

    agents_path = env.get(_AGENTS_VAR)
    if not agents_path:
        raise ConfigError(f"{_AGENTS_VAR} is required (path to the agent registry JSON)")
    agents, by_recipient = _load_agents(agents_path)

    raw_routes = env.get(_ENABLED_ROUTES_VAR)
    if raw_routes is None:
        enabled = frozenset(_DEFAULT_ENABLED_ROUTES)
    else:
        enabled = _split_csv(raw_routes)
        if not enabled:
            raise ConfigError(f"{_ENABLED_ROUTES_VAR} is set but lists no routes")

    secrets: dict[str, str] = {}
    for route in enabled:
        secret = env.get(_route_secret_var(route))
        if not secret:
            raise ConfigError(
                f"route {route!r} is enabled but {_route_secret_var(route)} is not set"
            )
        secrets[route] = secret

    return Config(
        agents=MappingProxyType(agents),
        enabled_routes=enabled,
        webhook_secrets=MappingProxyType(secrets),
        recipient_index=MappingProxyType(by_recipient),
    )


def _int_env(env: Mapping[str, str], var: str, default: int) -> int:
    raw = env.get(var)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ConfigError(f"{var} must be an integer, got {raw!r}") from None


def _float_env(env: Mapping[str, str], var: str, default: float) -> float:
    raw = env.get(var)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        raise ConfigError(f"{var} must be a number, got {raw!r}") from None


def load_breaker_config(env: Mapping[str, str] | None = None) -> BreakerConfig:
    """Build the wake-rate breaker's :class:`BreakerConfig` from ``env``.

    All four knobs are optional with generous safe defaults (so a deployment that
    sets none still gets the runaway backstop): ``BASECRADLE_ROUTER_WAKE_BREAKER_MAX``
    (per-agent wakes per window, default 20), ``_WINDOW`` (rolling window seconds,
    default 60), ``_COOLDOWN`` (halt seconds after a trip, default 60), and
    ``_STREAM_MAX`` (per-(agent, stream) wakes per window, default 15; ``0`` disables
    the per-stream scope). An unparseable or out-of-range value is a loud
    :class:`ConfigError` naming the variable, never a silent fallback to the default.
    """
    env = os.environ if env is None else env
    try:
        return BreakerConfig(
            max_wakes=_int_env(env, _BREAKER_MAX_VAR, 20),
            window=_float_env(env, _BREAKER_WINDOW_VAR, 60.0),
            cooldown=_float_env(env, _BREAKER_COOLDOWN_VAR, 60.0),
            stream_max_wakes=_int_env(env, _BREAKER_STREAM_MAX_VAR, 15),
        )
    except ValueError as exc:
        raise ConfigError(f"invalid wake breaker configuration: {exc}") from exc


def load_dedup_ttl(env: Mapping[str, str] | None = None) -> float:
    """The delivery-dedup TTL in seconds, from ``BASECRADLE_ROUTER_DEDUP_TTL``.

    Optional with a generous default (600 s): the "recently-woke delivery" cache
    that collapses a duplicate webhook delivery into a single wake
    (basecradle-router#133). ``0`` disables dedup; a negative value is a loud
    :class:`ConfigError` naming the variable, never a silent fallback. See
    :mod:`basecradle_router.dedup`.
    """
    env = os.environ if env is None else env
    ttl = _float_env(env, _DEDUP_TTL_VAR, 600.0)
    if ttl < 0:
        raise ConfigError(f"{_DEDUP_TTL_VAR} must be >= 0 (0 disables dedup), got {ttl!r}")
    return ttl


def load_wake_timeout(env: Mapping[str, str] | None = None) -> float | None:
    """The per-wake wall-clock bound in seconds, from ``BASECRADLE_ROUTER_WAKE_TIMEOUT``.

    A wake runs *inside* the per-agent lock and is awaited by ``drain()`` on
    shutdown, so a hung ``claude`` that runs unbounded pins a worker thread and the
    agent lock forever — saturating the shared thread pool and stalling wakes for
    every agent, and hanging a deploy/reboot drain until systemd SIGKILLs the whole
    control-group mid-task (basecradle-router#135). Every wake therefore carries a
    finite bound, set here at the composition root rather than left to a library
    default of ``None``.

    Optional with a generous default (90 s): well under the unit's
    ``TimeoutStopSec`` so a hang is reaped before the drain deadline, yet generous
    for a real multi-minute agent task (the wake-runner's OS-level ``timeout(1)``
    reaps the whole ``runuser→bash→claude`` tree, and the subprocess timeout is the
    Python-level backstop). ``0`` disables the bound (unbounded — a hang can then
    pin a thread; opt in only deliberately).

    Every other input fails loudly with a :class:`ConfigError` naming the variable,
    never a silent fallback: a negative or non-finite (``inf``/``nan``) value, or a
    value so large that the wake's total wall-clock (the bound plus the
    :data:`~basecradle_router.wake.WAKE_BACKSTOP_MARGIN` subprocess backstop) would
    not finish before the unit's ``TimeoutStopSec`` — which would re-open the very
    drain-severance #135 closes, just via misconfiguration. To allow a longer wake,
    raise ``TimeoutStopSec`` in the systemd unit (and ``_STOP_DEADLINE_SECONDS``).
    """
    env = os.environ if env is None else env
    secs = _float_env(env, _WAKE_TIMEOUT_VAR, 90.0)
    if not math.isfinite(secs):
        raise ConfigError(f"{_WAKE_TIMEOUT_VAR} must be a finite number, got {secs!r}")
    if secs < 0:
        raise ConfigError(f"{_WAKE_TIMEOUT_VAR} must be >= 0 (0 disables the bound), got {secs!r}")
    if secs == 0:
        return None
    ceiling = _STOP_DEADLINE_SECONDS - WAKE_BACKSTOP_MARGIN
    if secs > ceiling:
        raise ConfigError(
            f"{_WAKE_TIMEOUT_VAR} must be <= {ceiling:g} (so the bound plus the "
            f"{WAKE_BACKSTOP_MARGIN:g}s backstop stays under the unit's "
            f"{_STOP_DEADLINE_SECONDS:g}s TimeoutStopSec); raise TimeoutStopSec to go higher, "
            f"got {secs!r}"
        )
    return secs


def load_github_trusted_actors(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """The github route's allow-list of actors permitted to trigger a wake.

    Read from ``BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS`` as a comma-separated
    list of GitHub logins (the fleet's org members and App bots, e.g.
    ``drawkkwast,basecradle-router-ai[bot]``). It is **required** and must be
    non-empty: this is a security control, so a deployment that forgets it fails
    loudly at startup rather than silently disabling the gate. Source-specific
    config, so it lives on the github route (via the composition root), not on the
    source-agnostic core :class:`Config`.
    """
    env = os.environ if env is None else env
    raw = env.get(_GITHUB_TRUSTED_ACTORS_VAR)
    if not raw:
        raise ConfigError(
            f"{_GITHUB_TRUSTED_ACTORS_VAR} is required "
            "(comma-separated GitHub logins of trusted fleet actors)"
        )
    actors = _split_csv(raw)
    if not actors:
        raise ConfigError(f"{_GITHUB_TRUSTED_ACTORS_VAR} is set but lists no actors")
    return actors

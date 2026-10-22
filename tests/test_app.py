"""The composition root: ``create_app`` builds a runnable, wired daemon.

Drives the app it returns end to end (signed webhook → woken agent) with the wake
boundary mocked, so the wiring is proven without a real model, agent, or network.
Test cast: Nova Digital (``nova``, AI) captains basecradle-python.
"""

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

from basecradle_router.app import build_registry, create_app
from basecradle_router.config import ConfigError, load_config
from basecradle_router.models import Agent, Event, WakeKind
from basecradle_router.routes.basecradle import RECIPIENT_SECRET_PREFIX
from basecradle_router.secret import Secret
from basecradle_router.wake import HomeServerWaker, WakeResult

SECRET = "fake-github-webhook-secret-" + "0" * 32
NOVA = Agent(
    key="basecradle/basecradle-python",
    os_user="nova",
    clone_path="/home/nova/basecradle-python",
    bot_slug="basecradle-python-ai",
)


class _RecordingWaker:
    def __init__(self) -> None:
        self.calls: list[tuple[Agent, Event]] = []

    def wake(self, agent: Agent, event: Event) -> WakeResult:
        self.calls.append((agent, event))
        return WakeResult(exit_code=0, stdout="opened PR")


def _registry_file(tmp_path) -> str:
    path = tmp_path / "agents.json"
    path.write_text(
        json.dumps(
            {
                NOVA.key: {
                    "os_user": NOVA.os_user,
                    "clone_path": NOVA.clone_path,
                    "bot_slug": NOVA.bot_slug,
                }
            }
        )
    )
    return str(path)


HANDOFF_SENDER = "john"  # John Doe, a trusted human org member, files the handoff


def _env(tmp_path) -> dict[str, str]:
    return {
        "BASECRADLE_ROUTER_AGENTS": _registry_file(tmp_path),
        "BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET": SECRET,
        "BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS": HANDOFF_SENDER,
    }


def _signed_body() -> tuple[bytes, dict[str, str]]:
    payload = {
        "action": "opened",
        "issue": {
            "number": 42,
            "title": "Mirror the wire-shape change",
            "html_url": f"https://github.com/{NOVA.key}/issues/42",
            "labels": [{"name": "handoff"}],
        },
        "repository": {"full_name": NOVA.key},
        "sender": {"login": HANDOFF_SENDER, "type": "User"},
    }
    body = json.dumps(payload).encode("utf-8")
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": "0192f3a4-5b6c-7d8e-9f01-23456789abcd",
        "X-Hub-Signature-256": f"sha256={digest}",
        "Content-Type": "application/json",
    }
    return body, headers


def _post(server, path: str, body: bytes, headers: dict[str, str]):
    async def _drive():
        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [
                (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
            ],
        }
        incoming = [{"type": "http.request", "body": body, "more_body": False}]
        sent: list[dict] = []

        async def receive():
            return incoming.pop(0)

        async def send(message):
            sent.append(message)

        await server(scope, receive, send)
        await server.drain()  # let the backgrounded wake finish before asserting
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    return asyncio.run(_drive())


def test_create_app_wires_a_working_daemon_end_to_end(tmp_path) -> None:
    waker = _RecordingWaker()
    server = create_app(_env(tmp_path), waker=waker)
    body, headers = _signed_body()

    status = _post(server, "/webhooks/github", body, headers)

    assert status == 202  # fast-acked, processing in the background
    assert len(waker.calls) == 1
    agent, _ = waker.calls[0]
    assert agent == NOVA  # config → resolve → wake wired together


def test_create_app_enables_the_github_route_and_verifies_signatures(tmp_path) -> None:
    server = create_app(_env(tmp_path), waker=_RecordingWaker())
    body, headers = _signed_body()
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64  # wrong digest
    assert _post(server, "/webhooks/github", body, headers) == 401


def test_create_app_defaults_to_the_home_server_waker(tmp_path) -> None:
    server = create_app(_env(tmp_path))
    assert isinstance(server.pipeline.waker, HomeServerWaker)


def test_create_app_raises_config_error_when_config_is_missing() -> None:
    # No agent registry path set → a ConfigError naming the missing variable, not a crash.
    with pytest.raises(ConfigError, match="BASECRADLE_ROUTER_AGENTS"):
        create_app({})


def test_create_app_requires_the_trusted_actors_allow_list(tmp_path) -> None:
    # The github route's trust gate is a security control: a deployment missing it
    # fails loudly at startup rather than silently disabling the check.
    env = _env(tmp_path)
    del env["BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS"]
    with pytest.raises(ConfigError, match="GITHUB_TRUSTED_ACTORS is required"):
        create_app(env, waker=_RecordingWaker())


# --- a captain's bot on the allow-list refuses to boot (#369) -----------------


def test_a_captain_bot_on_the_allow_list_refuses_to_boot(tmp_path) -> None:
    # Nova's own bot listed would let any captain's comment wake another: the daemon
    # refuses to start, as with an absent or empty list.
    env = _env(tmp_path)
    env["BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS"] = f"{HANDOFF_SENDER},{NOVA.bot_slug}[bot]"
    with pytest.raises(ConfigError, match="lists a captain's bot"):
        create_app(env, waker=_RecordingWaker())


def test_the_claims_emitter_refuses_the_same_allow_list(tmp_path) -> None:
    # The emitter builds the daemon's registry, so it cannot describe a router that
    # would refuse to start.
    env = _env(tmp_path)
    env["BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS"] = f"{HANDOFF_SENDER},{NOVA.bot_slug}[bot]"
    with pytest.raises(ConfigError, match="lists a captain's bot"):
        build_registry(load_config(env), env)


def test_the_capital_bot_boots_even_when_the_capital_is_registered(tmp_path) -> None:
    # The capital bot is the one bot the list must hold. A box that also registers the
    # capital as an agent (its bot is then a registered bot like any captain's) still
    # boots with it listed: the refusal is about the list, not the registry.
    env = _env(tmp_path)
    registry = tmp_path / "capital-agents.json"  # its own file: `_env` owns agents.json
    registry.write_text(
        json.dumps(
            {
                "basecradle/basecradle": {
                    "os_user": "basecradle-ai",
                    "clone_path": "/home/basecradle-ai/basecradle",
                    "bot_slug": "basecradle-ai",
                }
            }
        )
    )
    env["BASECRADLE_ROUTER_AGENTS"] = str(registry)
    env["BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS"] = f"{HANDOFF_SENDER},basecradle-ai[bot]"
    assert "github" in create_app(env, waker=_RecordingWaker()).pipeline.registry


# --- issue_comment re-wake is wired, self-comment guard included (#129) ------

NOVA_BOT = f"{NOVA.bot_slug}[bot]"  # basecradle-python-ai[bot] — Nova's own bot


def _signed_comment(sender: str) -> tuple[bytes, dict[str, str]]:
    payload = {
        "action": "created",
        "issue": {
            "number": 42,
            "title": "Mirror the wire-shape change",
            "html_url": f"https://github.com/{NOVA.key}/issues/42",
            "labels": [{"name": "handoff"}],
        },
        "comment": {"body": "a follow-up"},
        "repository": {"full_name": NOVA.key},
        "sender": {"login": sender, "type": "User"},
    }
    body = json.dumps(payload).encode("utf-8")
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "X-GitHub-Event": "issue_comment",
        "X-GitHub-Delivery": "0192f3a4-5b6c-7d8e-9f01-23456789abcd",
        "X-Hub-Signature-256": f"sha256={digest}",
        "Content-Type": "application/json",
    }
    return body, headers


def test_create_app_rewakes_on_a_peer_comment(tmp_path) -> None:
    # A trusted human's reply on the handoff issue re-wakes Nova end-to-end.
    waker = _RecordingWaker()
    server = create_app(_env(tmp_path), waker=waker)
    body, headers = _signed_comment(sender=HANDOFF_SENDER)

    assert _post(server, "/webhooks/github", body, headers) == 202
    assert len(waker.calls) == 1
    assert waker.calls[0][0] == NOVA


def test_create_app_wires_the_self_comment_guard(tmp_path) -> None:
    # The wiring proof: a comment authored by Nova's OWN bot must not re-wake Nova
    # (the infinite-loop guard). create_app must resolve the repo's bot from the
    # registry, or the guard is inert — so this fails if the wiring regresses. The
    # allow-list here is just the human filer (NOT Nova's bot): because the guard
    # runs ahead of the trust gate, it is the GUARD — an IGNORED 200 — that stops
    # the wake, proving the two controls are decoupled.
    waker = _RecordingWaker()
    server = create_app(_env(tmp_path), waker=waker)
    body, headers = _signed_comment(sender=NOVA_BOT)

    assert _post(server, "/webhooks/github", body, headers) == 200  # accepted, ignored
    assert waker.calls == []  # no wake — the self-comment guard held


# --- a captain's own merge re-wakes it, the author gate wired from the registry (#365)


def _signed(event: str, delivery: str, payload: dict) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode("utf-8")
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": f"sha256={digest}",
        "Content-Type": "application/json",
    }
    return body, headers


def _signed_merge(author: str) -> tuple[bytes, dict[str, str]]:
    # Merged after the handoff's own wake started, so no coalesce can claim it was read.
    merged_at = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return _signed(
        "pull_request",
        "0192f3a4-5b6c-7d8e-9f01-23456789abce",
        {
            "action": "closed",
            "pull_request": {
                "number": 57,
                "html_url": f"https://github.com/{NOVA.key}/pull/57",
                "title": "Mirror the wire-shape change",
                "body": "Ships #42.",
                "merged": True,
                "merged_at": merged_at,
                "user": {"login": author, "type": "Bot"},
            },
            "repository": {"full_name": NOVA.key},
            "sender": {"login": author, "type": "Bot"},
        },
    )


def _merge_server(tmp_path, waker: _RecordingWaker):
    # The allow-list stays the box's shape — no captain bot on it (#367): Nova's own
    # merge must be admitted by the registry's bot_slug, through the composition root.
    env = _env(tmp_path)
    server = create_app(env, waker=waker)
    opened = {
        "action": "opened",
        "issue": {
            "number": 42,
            "title": "Mirror the wire-shape change",
            "html_url": f"https://github.com/{NOVA.key}/issues/42",
            "labels": [{"name": "handoff"}],
            "state": "open",
            "created_at": "2026-09-19T03:14:07Z",
            "updated_at": "2026-09-19T03:14:07Z",
        },
        "repository": {"full_name": NOVA.key},
        "sender": {"login": HANDOFF_SENDER, "type": "User"},
    }
    body, headers = _signed("issues", "0192f3a4-5b6c-7d8e-9f01-23456789abcd", opened)
    assert _post(server, "/webhooks/github", body, headers) == 202
    return server


def test_create_app_rewakes_a_captain_on_its_own_merge(tmp_path) -> None:
    waker = _RecordingWaker()
    server = _merge_server(tmp_path, waker)
    body, headers = _signed_merge(author=NOVA_BOT)

    assert _post(server, "/webhooks/github", body, headers) == 202
    assert len(waker.calls) == 2  # the handoff, then the merge
    agent, event = waker.calls[1]
    assert agent == NOVA
    assert event.origin.url == f"https://github.com/{NOVA.key}/issues/42"
    assert "pull request #57" in event.wake_arg


def test_create_app_keeps_a_dependabot_merge_silent(tmp_path) -> None:
    waker = _RecordingWaker()
    server = _merge_server(tmp_path, waker)
    body, headers = _signed_merge(author="dependabot[bot]")

    assert _post(server, "/webhooks/github", body, headers) == 200  # accepted, ignored
    assert len(waker.calls) == 1  # the handoff alone


# --- the startup warm-up refills the handoff ledger before any delivery (#370) --
#
# A deploy restarts the daemon, and the ledger the merge path reads is memory. The
# reader is injected (no test reaches GitHub); the lifespan is driven as uvicorn drives
# it, with its global logging set-up stubbed so nothing leaks into other tests.


def _start(server) -> list[str]:
    """Drive the ASGI lifespan startup; return what it sent, in order.

    As under uvicorn, the lifespan then waits on ``receive`` until shutdown; the test has
    no shutdown to send (it would close the wake pool the next delivery needs), so it
    cancels that wait once the startup is answered.
    """

    async def _drive() -> list[str]:
        sent: list[str] = []
        started = asyncio.Event()
        messages = [{"type": "lifespan.startup"}]

        async def receive():
            if messages:
                return messages.pop(0)
            await asyncio.Event().wait()  # no shutdown ever comes

        async def send(message):
            sent.append(message["type"])
            started.set()

        lifespan = asyncio.create_task(server({"type": "lifespan"}, receive, send))
        await started.wait()
        lifespan.cancel()
        return sent

    return asyncio.run(_drive())


def _open_issue_42() -> dict:
    return {
        "number": 42,
        "title": "Mirror the wire-shape change",
        "html_url": f"https://github.com/{NOVA.key}/issues/42",
        "labels": [{"name": "handoff"}],
        "state": "open",
        "created_at": "2026-09-19T03:14:07Z",
        "updated_at": "2026-09-19T03:14:07Z",
    }


def test_a_merge_right_after_a_restart_wakes_on_the_issue_the_startup_warmed(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("basecradle_router.server.configure_logging", lambda: None)
    order: list[str] = []

    def reader(repo: str, timeout: float) -> list[dict]:
        order.append(f"read {repo}")
        return [_open_issue_42()]

    waker = _RecordingWaker()
    server = create_app(_env(tmp_path), waker=waker, read_open_handoffs=reader)
    order.extend(_start(server))
    # Read before the startup completes, so before uvicorn binds and any delivery lands.
    assert order == [f"read {NOVA.key}", "lifespan.startup.complete"]

    body, headers = _signed_merge(author=NOVA_BOT)  # no delivery taught this daemon #42
    assert _post(server, "/webhooks/github", body, headers) == 202
    ((agent, event),) = waker.calls
    assert agent == NOVA
    assert event.origin.url == f"https://github.com/{NOVA.key}/issues/42"


def test_a_warm_up_that_cannot_reach_github_never_stops_the_start(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("basecradle_router.server.configure_logging", lambda: None)

    def reader(repo: str, timeout: float) -> list[dict]:
        raise OSError("network is unreachable")

    server = create_app(_env(tmp_path), waker=_RecordingWaker(), read_open_handoffs=reader)
    assert _start(server) == ["lifespan.startup.complete"]


def test_the_warm_up_reads_only_the_repos_a_github_builder_captains(tmp_path) -> None:
    # A harness agent is keyed by its slug, not a repo: there is nothing to read for it.
    asked: list[str] = []
    server = create_app(
        _env_with_harness_agent(tmp_path),
        waker=_RecordingWaker(),
        read_open_handoffs=lambda repo, timeout: asked.append(repo) or [],
    )
    server.warm_routes()
    assert asked == [NOVA.key]


# --- the basecradle route's per-recipient keyring is wired here (#497) --------
#
# Fabricated cast: @orion, a harness agent registered alongside Nova's builder entry.

ORION = Agent(
    key="orion",
    os_user="orion",
    clone_path="/home/orion/harness",
    wake_kind=WakeKind.HARNESS,
    recipient_uuid="01a161c5-8102-71e0-82f0-2d53fa00c27b",
    wake_bin="/home/orion/venv/bin/basecradle-harness-wake",
)
ORION_KEY = "bc_isk_fakeorionintegrationsigningkey01"


def _env_with_harness_agent(tmp_path) -> dict[str, str]:
    path = tmp_path / "agents-with-harness-agent.json"
    path.write_text(
        json.dumps(
            {
                NOVA.key: {
                    "os_user": NOVA.os_user,
                    "clone_path": NOVA.clone_path,
                    "bot_slug": NOVA.bot_slug,
                },
                ORION.key: {
                    "kind": "harness",
                    "os_user": ORION.os_user,
                    "clone_path": ORION.clone_path,
                    "recipient_uuid": ORION.recipient_uuid,
                    "wake_bin": ORION.wake_bin,
                },
            }
        )
    )
    return {
        "BASECRADLE_ROUTER_AGENTS": str(path),
        "BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET": SECRET,
        "BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS": HANDOFF_SENDER,
        "BASECRADLE_ROUTER_ENABLED_ROUTES": "github,basecradle",
        "BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET": SECRET,
    }


def test_create_app_wires_the_per_recipient_keyring(tmp_path) -> None:
    # The wiring proof: a key provisioned in the environment must reach the route, or
    # the whole feature is inert on the box while every unit test still passes.
    env = _env_with_harness_agent(tmp_path) | {f"{RECIPIENT_SECRET_PREFIX}ORION": ORION_KEY}
    route = build_registry(load_config(env), env).get("basecradle")

    assert dict(route.keyring.by_recipient) == {ORION.recipient_uuid: Secret(ORION_KEY)}
    assert route.keyring.shared_fallback is True


def test_a_misprovisioned_key_stops_the_daemon_at_boot(tmp_path) -> None:
    # Loud at boot, not as one agent's deliveries quietly failing to verify. The
    # daemon refusing to start is what the NOC's converge and the box's alarms see.
    env = _env_with_harness_agent(tmp_path) | {f"{RECIPIENT_SECRET_PREFIX}ORIONN": ORION_KEY}
    with pytest.raises(ConfigError, match="names no registered agent"):
        create_app(env, waker=_RecordingWaker())


def test_the_keyring_is_not_built_when_the_basecradle_route_is_disabled(tmp_path) -> None:
    # Each route's own config is demanded only when that route is in use — so a box
    # running github only is never asked for keys it has no route to verify with.
    env = _env(tmp_path) | {f"{RECIPIENT_SECRET_PREFIX}ORIONN": ORION_KEY}
    create_app(env, waker=_RecordingWaker())


# --- the route-wide secret is demanded at boot, and only where it can verify (#337) --

SHARED_FALLBACK = "BASECRADLE_ROUTER_BASECRADLE_SHARED_SECRET_FALLBACK"
SHARED_SECRET = "BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET"
UNREGISTERED_UUID = "01a161c5-ddcf-7e4b-95b3-9366453193b0"


def _signed_basecradle(recipient_uuid: str, key: str) -> tuple[bytes, dict[str, str]]:
    # `timeline.locked` is outside the actionable set, so a delivery that verifies is a
    # logged ignore (200) and one that does not is a 401 — the status is the verify.
    body = json.dumps(
        {
            "event": "timeline.locked",
            "event_id": "01a161c5-fcbe-7528-a672-4053f306856c",
            "occurred_at": "2026-01-01T00:00:00Z",
            "actor_uuid": None,
            "recipient_uuid": recipient_uuid,
            "timeline_uuid": "01a161c6-1bad-74a3-8dc1-ce01f48ee0c5",
            "resource": {"type": "timeline", "uuid": "01a161c6-1bad-74a3-8dc1-ce01f48ee0c5"},
        }
    ).encode("utf-8")
    digest = hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    return body, {
        "X-BaseCradle-Event": "timeline.locked",
        "X-BaseCradle-Delivery": "01a161c5-fcbe-7528-a672-4053f306856c",
        "X-BaseCradle-Signature": f"sha256={digest}",
        "Content-Type": "application/json",
    }


def _retired_env_without_the_shared_secret(tmp_path) -> dict[str, str]:
    """A new host's router.env: per-recipient keys, the fallback off, no shared value."""
    env = _env_with_harness_agent(tmp_path) | {
        f"{RECIPIENT_SECRET_PREFIX}ORION": ORION_KEY,
        SHARED_FALLBACK: "0",
    }
    del env[SHARED_SECRET]
    return env


def test_an_enabled_route_without_its_secret_refuses_to_boot(tmp_path) -> None:
    env = _env(tmp_path)
    del env["BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET"]
    with pytest.raises(ConfigError, match="GITHUB_WEBHOOK_SECRET is not set"):
        create_app(env, waker=_RecordingWaker())


def test_an_enabled_name_with_no_route_behind_it_is_still_demanded_a_secret(tmp_path) -> None:
    # No route exists to vouch that it needs none, so the answer never fails open.
    env = _env(tmp_path) | {"BASECRADLE_ROUTER_ENABLED_ROUTES": "github,nosuch"}
    with pytest.raises(ConfigError, match="BASECRADLE_ROUTER_NOSUCH_WEBHOOK_SECRET is not set"):
        create_app(env, waker=_RecordingWaker())


@pytest.mark.parametrize("value", [None, ""], ids=["absent", "empty"])
def test_an_armed_fallback_still_requires_the_shared_secret(tmp_path, value) -> None:
    # Armed, the shared value IS the fallback: a box without one could not verify any
    # recipient that holds no key of its own, so it does not boot. Empty is absent —
    # never an empty HMAC key.
    env = _env_with_harness_agent(tmp_path)
    if value is None:
        del env[SHARED_SECRET]
    else:
        env[SHARED_SECRET] = value
    with pytest.raises(ConfigError, match=f"{SHARED_SECRET} is not set"):
        create_app(env, waker=_RecordingWaker())


def test_a_retired_fallback_boots_without_the_shared_secret(tmp_path) -> None:
    # The #337 shape: per-recipient keys only, fallback off — the daemon boots, a keyed
    # recipient verifies, and the boot line still states the retirement.
    env = _retired_env_without_the_shared_secret(tmp_path)
    route = build_registry(load_config(env), env).get("basecradle")
    assert route.requires_route_secret is False
    assert "shared_fallback=false" in route.boot_summary()

    server = create_app(env, waker=_RecordingWaker())
    assert _post(
        server, "/webhooks/basecradle", *_signed_basecradle(ORION.recipient_uuid, ORION_KEY)
    ) == (200)
    # A recipient with no key of its own is refused — there is nothing to fall back to.
    assert (
        _post(server, "/webhooks/basecradle", *_signed_basecradle(UNREGISTERED_UUID, ORION_KEY))
        == 401
    )


def test_a_retired_fallback_boots_unchanged_with_the_shared_secret_still_set(tmp_path) -> None:
    # `ai`'s router.env still carries the shared value: it boots exactly as before, and
    # the value is never consulted — signing an unregistered recipient with it is refused.
    env = _env_with_harness_agent(tmp_path) | {
        f"{RECIPIENT_SECRET_PREFIX}ORION": ORION_KEY,
        SHARED_FALLBACK: "0",
    }
    server = create_app(env, waker=_RecordingWaker())
    assert _post(
        server, "/webhooks/basecradle", *_signed_basecradle(ORION.recipient_uuid, ORION_KEY)
    ) == (200)
    assert (
        _post(server, "/webhooks/basecradle", *_signed_basecradle(UNREGISTERED_UUID, SECRET)) == 401
    )

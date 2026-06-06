"""The composition root: ``create_app`` builds a runnable, wired daemon.

Drives the app it returns end to end (signed webhook → woken agent) with the wake
boundary mocked, so the wiring is proven without a real model, agent, or network.
Test cast: Nova Digital (``nova``, AI) captains basecradle-python.
"""

import asyncio
import hashlib
import hmac
import json

import pytest

from basecradle_router.app import create_app
from basecradle_router.config import ConfigError
from basecradle_router.models import Agent, Event
from basecradle_router.wake import HomeServerWaker, WakeResult

SECRET = "whsec_" + "0" * 32
NOVA = Agent(
    repo="basecradle/basecradle-python",
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
                NOVA.repo: {
                    "os_user": NOVA.os_user,
                    "clone_path": NOVA.clone_path,
                    "bot_slug": NOVA.bot_slug,
                }
            }
        )
    )
    return str(path)


def _env(tmp_path) -> dict[str, str]:
    return {
        "BASECRADLE_ROUTER_AGENTS": _registry_file(tmp_path),
        "BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET": SECRET,
    }


def _signed_body() -> tuple[bytes, dict[str, str]]:
    payload = {
        "action": "opened",
        "issue": {
            "number": 42,
            "title": "Mirror the wire-shape change",
            "html_url": f"https://github.com/{NOVA.repo}/issues/42",
            "labels": [{"name": "handoff"}],
        },
        "repository": {"full_name": NOVA.repo},
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

"""Config loads from a fabricated env + agent registry, and fails loudly.

Test cast: Nova Digital (``nova``, AI). Secrets are correctly-shaped fakes.
"""

import json

import pytest

from basecradle_router.config import ConfigError, load_config

REGISTRY = {
    "basecradle/basecradle-python": {
        "os_user": "nova",
        "clone_path": "/home/nova/basecradle-python",
        "bot_slug": "basecradle-python-ai",
    }
}
FAKE_SECRET = "whsec_" + "0" * 32  # correctly-shaped fake


def _write_registry(tmp_path, registry=REGISTRY, name="agents.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(registry), encoding="utf-8")
    return str(path)


def _env(tmp_path, **overrides) -> dict[str, str]:
    env = {
        "BASECRADLE_ROUTER_AGENTS": _write_registry(tmp_path),
        "BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET": FAKE_SECRET,
    }
    env.update(overrides)
    return env


def test_loads_with_defaults(tmp_path) -> None:
    config = load_config(_env(tmp_path))
    assert config.enabled_routes == frozenset({"github"})
    assert config.webhook_secret("github") == FAKE_SECRET
    agent = config.agent_for("basecradle/basecradle-python")
    assert agent.os_user == "nova"
    assert agent.bot_slug == "basecradle-python-ai"


def test_missing_agents_var_fails_loudly(tmp_path) -> None:
    env = _env(tmp_path)
    del env["BASECRADLE_ROUTER_AGENTS"]
    with pytest.raises(ConfigError, match="BASECRADLE_ROUTER_AGENTS is required"):
        load_config(env)


def test_enabled_route_without_secret_fails(tmp_path) -> None:
    env = _env(tmp_path)
    del env["BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET"]
    with pytest.raises(ConfigError, match="GITHUB_WEBHOOK_SECRET is not set"):
        load_config(env)


def test_explicit_enabled_routes_require_each_secret(tmp_path) -> None:
    env = _env(tmp_path, BASECRADLE_ROUTER_ENABLED_ROUTES="github, basecradle")
    with pytest.raises(ConfigError, match="BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET"):
        load_config(env)


def test_empty_enabled_routes_list_fails(tmp_path) -> None:
    env = _env(tmp_path, BASECRADLE_ROUTER_ENABLED_ROUTES="  ,  ")
    with pytest.raises(ConfigError, match="lists no routes"):
        load_config(env)


def test_malformed_registry_json_fails(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(_env(tmp_path, BASECRADLE_ROUTER_AGENTS=str(path)))


def test_registry_entry_missing_field_fails(tmp_path) -> None:
    bad = {"basecradle/x": {"os_user": "nova", "clone_path": "/c"}}  # no bot_slug
    bad_path = _write_registry(tmp_path, bad, name="bad.json")
    with pytest.raises(ConfigError, match="missing 'bot_slug'"):
        load_config(_env(tmp_path, BASECRADLE_ROUTER_AGENTS=bad_path))


def test_missing_path_reports_clean_error(tmp_path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(_env(tmp_path, BASECRADLE_ROUTER_AGENTS=str(tmp_path / "nope.json")))


def test_agent_for_unknown_repo_fails(tmp_path) -> None:
    config = load_config(_env(tmp_path))
    with pytest.raises(ConfigError, match="no agent registered"):
        config.agent_for("basecradle/unknown")


def test_config_maps_are_read_only(tmp_path) -> None:
    config = load_config(_env(tmp_path))
    with pytest.raises(TypeError):
        config.agents["basecradle/x"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        config.webhook_secrets["github"] = "x"  # type: ignore[index]

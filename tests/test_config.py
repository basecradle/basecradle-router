"""Config loads from a fabricated env + agent registry, and fails loudly.

Test cast: Nova Digital (``nova``, AI). Secrets are correctly-shaped fakes.
"""

import json

import pytest

from basecradle_router.config import (
    ConfigError,
    load_breaker_config,
    load_config,
    load_dedup_ttl,
    load_github_trusted_actors,
    load_wake_timeout,
)

_BREAKER_MAX_VAR = "BASECRADLE_ROUTER_WAKE_BREAKER_MAX"
_BREAKER_WINDOW_VAR = "BASECRADLE_ROUTER_WAKE_BREAKER_WINDOW"
_BREAKER_COOLDOWN_VAR = "BASECRADLE_ROUTER_WAKE_BREAKER_COOLDOWN"
_BREAKER_STREAM_MAX_VAR = "BASECRADLE_ROUTER_WAKE_BREAKER_STREAM_MAX"
_DEDUP_TTL_VAR = "BASECRADLE_ROUTER_DEDUP_TTL"
_WAKE_TIMEOUT_VAR = "BASECRADLE_ROUTER_WAKE_TIMEOUT"

REGISTRY = {
    "basecradle/basecradle-python": {
        "os_user": "nova",
        "clone_path": "/home/nova/basecradle-python",
        "bot_slug": "basecradle-python-ai",
    }
}
JT_UUID = "019e916c-7f45-700e-afc0-f45557b237b7"
HARNESS_ENTRY = {
    "os_user": "jt",
    "clone_path": "/home/jt/harness",
    "kind": "harness",
    "recipient_uuid": JT_UUID,
    "wake_bin": "/home/jt/venv/bin/basecradle-harness-wake",
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


# --- harness (non-repo) agent entries --------------------------------------


def test_harness_entry_parses_and_indexes_by_recipient_uuid(tmp_path) -> None:
    from basecradle_router.models import Recipient, WakeKind

    registry = {**REGISTRY, "jt": HARNESS_ENTRY}
    config = load_config(
        _env(tmp_path, BASECRADLE_ROUTER_AGENTS=_write_registry(tmp_path, registry, name="r.json"))
    )
    # Keyed by its bare slug, with the harness wake shape and no bot_slug.
    jt = config.agent_for("jt")
    assert jt.wake_kind is WakeKind.HARNESS
    assert jt.wake_bin == "/home/jt/venv/bin/basecradle-harness-wake"
    assert jt.bot_slug is None
    # A basecradle event resolves to it by recipient_uuid; github builders are untouched.
    assert config.agent_for_recipient(Recipient(by="recipient_uuid", value=JT_UUID)) is jt
    assert config.agent_for("basecradle/basecradle-python").wake_kind is WakeKind.CLAUDE


def test_harness_entry_missing_field_fails(tmp_path) -> None:
    bad = {"jt": {k: v for k, v in HARNESS_ENTRY.items() if k != "wake_bin"}}
    bad_path = _write_registry(tmp_path, bad, name="bad.json")
    with pytest.raises(ConfigError, match="missing 'wake_bin'"):
        load_config(_env(tmp_path, BASECRADLE_ROUTER_AGENTS=bad_path))


def test_unknown_kind_fails(tmp_path) -> None:
    bad = {"jt": {**HARNESS_ENTRY, "kind": "robot"}}
    bad_path = _write_registry(tmp_path, bad, name="bad.json")
    with pytest.raises(ConfigError, match="unknown kind 'robot'"):
        load_config(_env(tmp_path, BASECRADLE_ROUTER_AGENTS=bad_path))


def test_duplicate_recipient_uuid_fails_loudly(tmp_path) -> None:
    # Two personas claiming the same uuid would silently misroute one's events to
    # the other; the recipient index is a bijection, so this must fail at load.
    second = {**HARNESS_ENTRY, "os_user": "kim", "clone_path": "/home/kim/harness"}
    bad = {"jt": HARNESS_ENTRY, "kim": second}
    bad_path = _write_registry(tmp_path, bad, name="bad.json")
    with pytest.raises(ConfigError, match="reuses recipient_uuid"):
        load_config(_env(tmp_path, BASECRADLE_ROUTER_AGENTS=bad_path))


def test_harness_key_shaped_like_a_repo_fails(tmp_path) -> None:
    # A harness key must be a bare slug — never owner/name — so it stays out of the
    # github key-space and a Recipient(by="repo") lookup can never hit it.
    bad = {"basecradle/jt": HARNESS_ENTRY}
    bad_path = _write_registry(tmp_path, bad, name="bad.json")
    with pytest.raises(ConfigError, match="bare slug"):
        load_config(_env(tmp_path, BASECRADLE_ROUTER_AGENTS=bad_path))


def test_github_entry_with_non_repo_key_fails(tmp_path) -> None:
    # A builder entry's key must be owner/name — the legacy shape check is preserved.
    bad = {"not-a-repo": {"os_user": "x", "clone_path": "/c", "bot_slug": "b"}}
    bad_path = _write_registry(tmp_path, bad, name="bad.json")
    with pytest.raises(ConfigError, match="owner/name"):
        load_config(_env(tmp_path, BASECRADLE_ROUTER_AGENTS=bad_path))


def test_config_maps_are_read_only(tmp_path) -> None:
    config = load_config(_env(tmp_path))
    with pytest.raises(TypeError):
        config.agents["basecradle/x"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        config.webhook_secrets["github"] = "x"  # type: ignore[index]


# --- github trusted-actor allow-list ---------------------------------------

_ACTORS_VAR = "BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS"


def test_trusted_actors_parse_a_comma_separated_list() -> None:
    env = {_ACTORS_VAR: "john, basecradle-python-ai[bot] ,nova"}
    assert load_github_trusted_actors(env) == frozenset(
        {"john", "basecradle-python-ai[bot]", "nova"}
    )


def test_trusted_actors_ignore_blanks_and_whitespace() -> None:
    assert load_github_trusted_actors({_ACTORS_VAR: "john, ,, nova ,"}) == frozenset(
        {"john", "nova"}
    )


def test_trusted_actors_required_when_unset() -> None:
    with pytest.raises(ConfigError, match=f"{_ACTORS_VAR} is required"):
        load_github_trusted_actors({})


def test_trusted_actors_empty_list_fails() -> None:
    with pytest.raises(ConfigError, match="lists no actors"):
        load_github_trusted_actors({_ACTORS_VAR: " , , "})


# --- wake-rate breaker config ----------------------------------------------


def test_breaker_config_defaults_when_unset() -> None:
    cfg = load_breaker_config({})
    assert (cfg.max_wakes, cfg.window, cfg.cooldown, cfg.stream_max_wakes) == (
        20,
        60.0,
        60.0,
        15,
    )


def test_breaker_config_reads_each_knob_from_env() -> None:
    cfg = load_breaker_config(
        {
            _BREAKER_MAX_VAR: "30",
            _BREAKER_WINDOW_VAR: "90",
            _BREAKER_COOLDOWN_VAR: "120",
            _BREAKER_STREAM_MAX_VAR: "0",
        }
    )
    assert (cfg.max_wakes, cfg.window, cfg.cooldown, cfg.stream_max_wakes) == (
        30,
        90.0,
        120.0,
        0,
    )


def test_breaker_config_non_numeric_value_fails_loudly() -> None:
    with pytest.raises(ConfigError, match=f"{_BREAKER_MAX_VAR} must be an integer"):
        load_breaker_config({_BREAKER_MAX_VAR: "lots"})


def test_breaker_config_out_of_range_value_fails_loudly() -> None:
    with pytest.raises(ConfigError, match="invalid wake breaker configuration"):
        load_breaker_config({_BREAKER_MAX_VAR: "0"})


def test_dedup_ttl_defaults_when_unset() -> None:
    assert load_dedup_ttl({}) == 600.0


def test_dedup_ttl_reads_from_env_and_zero_disables() -> None:
    assert load_dedup_ttl({_DEDUP_TTL_VAR: "120"}) == 120.0
    assert load_dedup_ttl({_DEDUP_TTL_VAR: "0"}) == 0.0  # the disable switch


def test_dedup_ttl_non_numeric_value_fails_loudly() -> None:
    with pytest.raises(ConfigError, match=f"{_DEDUP_TTL_VAR} must be a number"):
        load_dedup_ttl({_DEDUP_TTL_VAR: "forever"})


def test_dedup_ttl_negative_value_fails_loudly() -> None:
    with pytest.raises(ConfigError, match=f"{_DEDUP_TTL_VAR} must be >= 0"):
        load_dedup_ttl({_DEDUP_TTL_VAR: "-5"})


def test_wake_timeout_defaults_to_a_finite_bound() -> None:
    # The whole point of #135: the default is finite, never None — a wake left
    # unbounded pins the agent lock and a worker thread forever.
    assert load_wake_timeout({}) == 90.0


def test_wake_timeout_reads_from_env_and_zero_disables() -> None:
    assert load_wake_timeout({_WAKE_TIMEOUT_VAR: "80"}) == 80.0
    assert load_wake_timeout({_WAKE_TIMEOUT_VAR: "0"}) is None  # the explicit opt-out


def test_wake_timeout_non_numeric_value_fails_loudly() -> None:
    with pytest.raises(ConfigError, match=f"{_WAKE_TIMEOUT_VAR} must be a number"):
        load_wake_timeout({_WAKE_TIMEOUT_VAR: "forever"})


def test_wake_timeout_negative_value_fails_loudly() -> None:
    with pytest.raises(ConfigError, match=f"{_WAKE_TIMEOUT_VAR} must be >= 0"):
        load_wake_timeout({_WAKE_TIMEOUT_VAR: "-1"})


def test_wake_timeout_non_finite_value_fails_loudly() -> None:
    # `inf`/`nan` pass float() and survive a `< 0` check, then crash at format time
    # and silently wedge EVERY wake — reject them loudly at load instead.
    for bad in ("inf", "nan"):
        with pytest.raises(ConfigError, match=f"{_WAKE_TIMEOUT_VAR} must be a finite number"):
            load_wake_timeout({_WAKE_TIMEOUT_VAR: bad})


def test_wake_timeout_above_the_stop_deadline_ceiling_fails_loudly() -> None:
    # The bound + the 20 s backstop must finish before the unit's 120 s TimeoutStopSec,
    # or a hung wake's drain is severed by systemd's SIGKILL — the #135 failure, via
    # misconfiguration. The enforced ceiling is 100 s.
    assert load_wake_timeout({_WAKE_TIMEOUT_VAR: "100"}) == 100.0  # the ceiling itself is allowed
    with pytest.raises(ConfigError, match="raise TimeoutStopSec"):
        load_wake_timeout({_WAKE_TIMEOUT_VAR: "115"})

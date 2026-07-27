"""Config loads from a fabricated env + agent registry, and fails loudly.

Test cast: Nova Digital (``nova``, AI). Secrets are correctly-shaped fakes.
"""

import json

import pytest

from basecradle_router.config import (
    DEFAULT_ADMIN_CMD,
    DEFAULT_WAKE_LANES,
    ConfigError,
    load_admin_cmd,
    load_breaker_config,
    load_config,
    load_dedup_ttl,
    load_evidence_path,
    load_github_trusted_actors,
    load_wake_lanes,
    load_wake_lock_dir,
)
from basecradle_router.evidence import DEFAULT_EVIDENCE_FILE
from basecradle_router.wakelock import DEFAULT_LOCK_DIR

_BREAKER_MAX_VAR = "BASECRADLE_ROUTER_WAKE_BREAKER_MAX"
_BREAKER_WINDOW_VAR = "BASECRADLE_ROUTER_WAKE_BREAKER_WINDOW"
_BREAKER_COOLDOWN_VAR = "BASECRADLE_ROUTER_WAKE_BREAKER_COOLDOWN"
_BREAKER_STREAM_MAX_VAR = "BASECRADLE_ROUTER_WAKE_BREAKER_STREAM_MAX"
_DEDUP_TTL_VAR = "BASECRADLE_ROUTER_DEDUP_TTL"
_WAKE_LANES_VAR = "BASECRADLE_ROUTER_WAKE_LANES"
_WAKE_LOCK_DIR_VAR = "BASECRADLE_ROUTER_WAKE_LOCK_DIR"
_EVIDENCE_FILE_VAR = "BASECRADLE_ROUTER_EVIDENCE_FILE"
_ADMIN_CMD_VAR = "BASECRADLE_ROUTER_ADMIN_CMD"

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


def test_wake_lanes_defaults_when_unset() -> None:
    # A fixed, box-independent default — deliberately NOT derived from cpu_count
    # (that implicit sizing was half of basecradle-router#182's starvation).
    assert load_wake_lanes({}) == DEFAULT_WAKE_LANES


def test_wake_lanes_reads_from_env() -> None:
    assert load_wake_lanes({_WAKE_LANES_VAR: "16"}) == 16


def test_wake_lanes_non_integer_value_fails_loudly() -> None:
    with pytest.raises(ConfigError, match=f"{_WAKE_LANES_VAR} must be an integer"):
        load_wake_lanes({_WAKE_LANES_VAR: "plenty"})


def test_wake_lanes_below_one_fails_loudly() -> None:
    # 0 lanes would dispatch nothing — a loud misconfiguration, never a silent no-op.
    with pytest.raises(ConfigError, match=f"{_WAKE_LANES_VAR} must be >= 1"):
        load_wake_lanes({_WAKE_LANES_VAR: "0"})


def test_wake_lock_dir_defaults_to_the_capital_pinned_path() -> None:
    assert load_wake_lock_dir({}) == DEFAULT_LOCK_DIR


def test_wake_lock_dir_reads_from_env() -> None:
    # Configurable for two reasons: the freeze self-test must be demonstrable against
    # a throwaway directory rather than the live locks, and — the load-bearing one — a
    # router reading a DIFFERENT directory than the NOC writes is the exact "the
    # control existed but was never read" failure (basecradle/basecradle#460).
    assert (
        load_wake_lock_dir({_WAKE_LOCK_DIR_VAR: "/run/test/wake-locks"}) == "/run/test/wake-locks"
    )


def test_a_blank_wake_lock_dir_falls_back_to_the_pinned_path() -> None:
    assert load_wake_lock_dir({_WAKE_LOCK_DIR_VAR: "   "}) == DEFAULT_LOCK_DIR


def test_evidence_path_defaults_to_the_state_dir() -> None:
    assert load_evidence_path({}) == DEFAULT_EVIDENCE_FILE


def test_evidence_path_reads_from_env() -> None:
    assert load_evidence_path({_EVIDENCE_FILE_VAR: "/tmp/e.json"}) == "/tmp/e.json"


@pytest.mark.parametrize("value", ["none", "NONE", "", "  "])
def test_evidence_persistence_can_be_disabled_explicitly(value) -> None:
    # The escape hatch for a laptop or throwaway run — never the fleet box, where a
    # ledger that reset on restart would report every proven capability as
    # never-proven after a deploy.
    assert load_evidence_path({_EVIDENCE_FILE_VAR: value}) is None


def test_admin_cmd_defaults_to_the_deploy_wrapper() -> None:
    # The path an emitted claim's prove.cmd names, so the NOC schedules one stable
    # entry point rather than reconstructing the privilege drop itself.
    assert load_admin_cmd({}) == DEFAULT_ADMIN_CMD


def test_admin_cmd_reads_from_env() -> None:
    assert load_admin_cmd({_ADMIN_CMD_VAR: "/srv/router/admin"}) == "/srv/router/admin"

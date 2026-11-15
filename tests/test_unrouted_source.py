"""A request that names no route logs ``source=unrouted``, never its own segment (#462).

``source=`` is the key the fleet filters its synthetic traffic on, and ``deploy/README.md``
says its value set is closed: the enabled route names, and ``unrouted``. Until #462 the
route-rejection line wrote the caller's own path segment there, so ``POST
/webhooks/probes`` logged ``source=probes`` and ``POST /webhooks/x%20source=probe`` logged
``source="x source=probe"``, and a substring filter on ``source=probe`` hid both. The
segment now rides under ``requested=``, bounded as before, beside a fixed token no caller
controls and no route may take.

Every line is read off the daemon's own logger, never a re-rendering.

Cast: John Doe (``john``, human) and Nova Digital (``nova``, AI). Every secret is fabricated.
"""

from __future__ import annotations

import logging
import re
from types import MappingProxyType

import pytest

from basecradle_router.config import Config
from basecradle_router.models import Agent
from basecradle_router.pipeline import Outcome, Pipeline, Stage
from basecradle_router.routes import (
    UNROUTED,
    BasecradleRoute,
    GithubRoute,
    ProbeRoute,
    RouteRegistry,
)
from basecradle_router.secret import Secret

SECRET = "fake-webhook-secret-" + "0" * 32
NOVA = Agent(
    key="basecradle/basecradle-python",
    os_user="nova",
    clone_path="/home/nova/basecradle-python",
    bot_slug="basecradle-python-ai",
)

#: The NOC's `source` column's extraction (`basecradle-noc` `observability/ai-box.json`,
#: `columns[].name == "source"`), copied on 2026-11-15: the first ` source=` outside every
#: quoted value. The NOC owns it and nothing here tracks a change to it; it is here to
#: show the line's shape against the reader that existed when the shape was decided.
NOC_SOURCE_COLUMN = re.compile(r'^(?:[^"]*"[^"]*")*?[^"]*? source=([A-Za-z0-9_-]+)')

#: Every escape `paint` can put on a line, so the assertions read the line's text.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _pipeline() -> Pipeline:
    registry = RouteRegistry()
    registry.register(GithubRoute(frozenset({"john"})))
    registry.register(BasecradleRoute())
    registry.register(ProbeRoute())
    config = Config(
        agents=MappingProxyType({NOVA.key: NOVA}),
        enabled_routes=registry.names(),
        webhook_secrets=MappingProxyType({name: Secret(SECRET) for name in registry.names()}),
    )
    return Pipeline(registry=registry, config=config, waker=None)


ROUTES = sorted(_pipeline().registry.names())

#: Names no route is registered under, built to collide with one: a route's name with a
#: suffix (what the edge's mask does not cover), a forged tag with spaces and `=` (what the
#: decoded ASGI path delivers), the token itself, and one long enough to be cut.
UNREGISTERED = [
    "probes",
    "probe.php",
    "probe-admin",
    "Probe",
    "x source=probe",
    "source=probe",
    "x=probe",
    "a b c",
    'say "source=probe"',
    UNROUTED,
    "probe" * 40,
    *(f"{name}s" for name in ROUTES),
    *(f"x source={name}" for name in ROUTES),
    *(f"source={name} y" for name in ROUTES),
]


def _lines(caplog, stage: str = "") -> list[str]:
    return [_ANSI.sub("", r.getMessage()) for r in caplog.records if stage in r.getMessage()]


def _assert_unrouted(line: str, name: str) -> None:
    """``line`` names ``name`` under ``requested=`` and the fixed token under ``source=``."""
    assert NOC_SOURCE_COLUMN.match(line).group(1) == UNROUTED, line
    # Outside requested='s value (and every other quoted value) the line carries one
    # `source=`, the token, and no route's tag as a substring.
    unquoted = re.sub(r'"[^"]*"', '""', line)
    assert re.findall(r"(?:^| )source=(\S+)", unquoted) == [UNROUTED], line
    for route in ROUTES:
        assert f"source={route}" not in unquoted, (route, line)
    assert " requested=" in line, line
    if len(name) <= 64 and " " not in name and "=" not in name and '"' not in name:
        assert f" requested={name} " in line, line


@pytest.mark.parametrize("name", UNREGISTERED)
def test_a_route_rejection_logs_the_fixed_token_and_the_callers_name_apart(
    name: str, caplog
) -> None:
    pipeline = _pipeline()
    with caplog.at_level(logging.INFO, logger="basecradle_router"):
        admission = pipeline.admit(name)
    assert admission.route is None
    assert admission.result.stages == [(Stage.ROUTE, Outcome.REJECTED)]
    (line,) = _lines(caplog, "stage=route")
    assert line.startswith(f"stage=route outcome=rejected source={UNROUTED} requested="), line
    _assert_unrouted(line, name)


def _broken_get(pipeline: Pipeline, monkeypatch) -> None:
    def boom(_name: str):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(pipeline.registry, "get", boom)


def test_a_route_stage_bug_on_an_unregistered_name_logs_the_fixed_token(
    caplog, monkeypatch
) -> None:
    # The last-resort branch: the registry raised, and the name is still only the caller's.
    pipeline = _pipeline()
    _broken_get(pipeline, monkeypatch)
    with caplog.at_level(logging.INFO, logger="basecradle_router"):
        admission = pipeline.admit("x source=probe")
    assert admission.result.stages == [(Stage.ROUTE, Outcome.FAILED)]
    (line,) = _lines(caplog, "stage=route")
    _assert_unrouted(line, "x source=probe")


@pytest.mark.parametrize("name", ROUTES)
def test_a_route_stage_bug_on_a_registered_route_still_names_it(
    name: str, caplog, monkeypatch
) -> None:
    # A bug on real traffic must read as that route, never as internet noise.
    pipeline = _pipeline()
    _broken_get(pipeline, monkeypatch)
    with caplog.at_level(logging.INFO, logger="basecradle_router"):
        admission = pipeline.admit(name)
    assert admission.result.stages == [(Stage.ROUTE, Outcome.FAILED)]
    (line,) = _lines(caplog, "stage=route")
    assert NOC_SOURCE_COLUMN.match(line).group(1) == name, line
    assert "requested=" not in line, line


@pytest.mark.parametrize("name", ROUTES)
def test_a_registered_route_still_logs_its_own_name(name: str, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="basecradle_router"):
        _pipeline().admit(name)
    (line,) = _lines(caplog, "stage=route")
    assert NOC_SOURCE_COLUMN.match(line).group(1) == name, line
    assert "requested=" not in line, line


@pytest.mark.parametrize("name", ["probes", "x source=probe"])
def test_a_refused_mark_on_an_unrouted_request_logs_the_fixed_token(name: str, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="basecradle_router"):
        assert _pipeline().mark_origin(name, "BCSYN1 i-am-the-fleet-honest") is None
    (line,) = _lines(caplog, "event=synthetic_mark_refused")
    assert "reason=malformed" in line, line
    _assert_unrouted(line, name)


def test_a_refused_mark_on_a_route_logs_the_routes_name(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="basecradle_router"):
        _pipeline().mark_origin("github", "BCSYN1 i-am-the-fleet-honest")
    (line,) = _lines(caplog, "event=synthetic_mark_refused")
    assert NOC_SOURCE_COLUMN.match(line).group(1) == "github", line
    assert "requested=" not in line, line


@pytest.mark.parametrize("name", ["probes", *ROUTES])
def test_a_mark_check_bug_names_the_source_as_every_other_line_does(
    name: str, caplog, monkeypatch
) -> None:
    # The mark check's last-resort branch reads the source through the same helper.
    pipeline = _pipeline()

    def boom(*_args):
        raise RuntimeError("marks exploded")

    monkeypatch.setattr(type(pipeline.marks), "verify", boom)
    with caplog.at_level(logging.INFO, logger="basecradle_router"):
        assert pipeline.mark_origin(name, "BCSYN1 anything") is None
    (line,) = _lines(caplog, "event=synthetic_mark_refused")
    assert "reason=error" in line, line
    if name in ROUTES:
        assert NOC_SOURCE_COLUMN.match(line).group(1) == name, line
        assert "requested=" not in line, line
    else:
        _assert_unrouted(line, name)


def test_a_path_with_no_segment_logs_the_token_and_no_requested(caplog) -> None:
    # `POST /webhooks/` reaches the mark check with an empty segment; an empty value is
    # dropped like any other, so the line carries the token and no `requested=`.
    with caplog.at_level(logging.INFO, logger="basecradle_router"):
        _pipeline().mark_origin("", "BCSYN1 i-am-the-fleet-honest")
    (line,) = _lines(caplog, "event=synthetic_mark_refused")
    assert NOC_SOURCE_COLUMN.match(line).group(1) == UNROUTED, line
    assert "requested=" not in line, line


def test_no_route_can_take_the_token() -> None:
    class Impostor(ProbeRoute):
        name = UNROUTED

    with pytest.raises(ValueError, match="reserved"):
        RouteRegistry().register(Impostor())

"""Suite-wide guards.

Tests pin invariants and never hit the live network (CLAUDE.md → Conventions). The one
outbound HTTP read the daemon makes — the github route's startup warm-up (#370) — goes
through ``urllib.request.urlopen``, looked up when called, so replacing it here fails any
test that would reach GitHub, loudly, instead of letting it pass on a live answer.
"""

import urllib.request

import pytest


def _no_network(*args, **kwargs):
    raise AssertionError(
        "a test reached urllib.request.urlopen: inject the github route's reader instead"
    )


@pytest.fixture(autouse=True)
def _forbid_live_http(monkeypatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", _no_network)

"""Smoke test: the package imports and exposes a version string.

Pins the scaffold so CI has something real to run. Offline by construction.
"""

import basecradle_router


def test_version_is_a_nonempty_string() -> None:
    assert isinstance(basecradle_router.__version__, str)
    assert basecradle_router.__version__

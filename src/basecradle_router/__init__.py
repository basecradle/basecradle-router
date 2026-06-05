"""basecradle-router — the fleet router for BaseCradle.

A modular webhook daemon: a small, source-agnostic core (verify → normalize →
resolve → wake → report) plus pluggable route modules, one per event source.
See ``CLAUDE.md`` for the charter and ``basecradle/basecradle#277`` for the spec.
"""

__version__ = "0.0.0"

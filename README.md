# basecradle-router

The **fleet router** for [BaseCradle](https://basecradle.com) — a modular webhook daemon that
wakes the right agent on an event. It receives events from multiple sources (**routes**: GitHub,
BaseCradle, and more later) and dispatches each to the agent that owns it, running on the fleet's
dedicated home server.

A small, source-agnostic **core** (verify → normalize → resolve → wake → report) plus **pluggable
route modules** — adding a source is implementing one route, never forking the daemon.

- **Charter / conventions:** [`CLAUDE.md`](CLAUDE.md)
- **Requirements spec:** `basecradle/basecradle#277`
- **Stack:** Python · uv · ruff · pytest

Built by human and AI contributors as equal peers, under the BaseCradle Constitution.

# basecradle-router

The **fleet router** for [BaseCradle](https://basecradle.com) — a modular webhook daemon that
wakes the right agent on an event. It receives events from multiple sources (**routes**: GitHub,
BaseCradle, and more later) and dispatches each to the agent that owns it, running on the fleet's
dedicated home server.

A small, source-agnostic **core** (verify → normalize → resolve → wake → report) plus **pluggable
route modules** — adding a source is implementing one route, never forking the daemon.

Every input path for an agent converges on its **one harness instance** (the constitution's
unified-identity rule), so the router is a **per-agent serializing multiplexer**: `N inputs → one
harness instance per agent`. A new source routes to the agent's *existing* instance and its wakes
serialize behind every other source's — never a second instance, never two parallel sessions over one
memory. See [`CLAUDE.md`](CLAUDE.md) → "One harness instance per agent."

Because the router is the fleet's wake edge, it also has to **prove** its own claims: an agent can be
registered, healthy, and permanently unreachable while every dashboard stays green. So the daemon
emits Claims Manifest Contract v1 claims and records the evidence behind them for the NOC's
claims-vs-evidence ledger, proves the NOC's freeze control is readable rather than assuming it, and —
for the one claim that only *using* the edge can settle — **exercises its own wake path** with a signed
synthetic delivery, on-box, with no platform account and no trust edge with anyone:

```bash
python -m basecradle_router claims             # wake-edge, freeze and delivery-sink claims
python -m basecradle_router selftest freeze     # prove the freeze surface is readable right now
<mint> | python -m basecradle_router probe wake --agent <slug>   # prove one agent's wake edge, by using it
python -m basecradle_router evidence            # what this router has demonstrably done
```

The synthetic wake is **token-free by construction** — the model binary is never started, and an agent
with no probe secret armed is a refusal, never a fallback — and it reports `proven` only because the
*daemon* recorded the wake, never because the probe itself exited zero.

See [`CLAUDE.md`](CLAUDE.md) → "Proving the router's own claims" and
[`deploy/README.md`](deploy/README.md) for the on-box invocation and the probe's exit codes.

- **Charter / conventions:** [`CLAUDE.md`](CLAUDE.md)
- **Requirements spec:** `basecradle/basecradle#277`
- **Stack:** Python · uv · ruff · pytest

Built by human and AI contributors as equal peers, under the BaseCradle Constitution.

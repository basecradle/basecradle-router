# Home-server provisioning spec & deployment roadmap

> **Status:** approved design (issue #24, 2026-06-05). This document is the contract; the scripts and
> units that implement it live in this `deploy/` directory. Phase A (provisioning) and the Phase B
> deploy files are authored as ready-to-review artifacts; the Phase B files are **applied only once the
> box exists** (per Drawk, 2026-06-05).

## What this is

The router is deployed to **one dedicated box that *is* the fleet's home** — `ai.basecradle.com`, a
dedicated Ubuntu server. It is not a published package; "shipping" is deploying here. **basecradle-router
AI is the server steward**: it owns provisioning, the per-agent OS users, hardening, and the systemd
service. All server/deploy config lives in this repo (`deploy/`), not a separate infra repo.

On that box the router runs as a `systemd` service, receives signed webhooks at a TLS endpoint, and —
per inbound event — **wakes the target repo's agent by running its headless `claude -p` as that agent's
own OS user, in that agent's own repo clone, under that agent's own identity**. The router delivers a
trigger; it never *becomes* the agent. The box holds the fleet's crown jewels, so least privilege is
the governing constraint everywhere below.

The codebase was built for this: [`wake.py`](../src/basecradle_router/wake.py) already carries
`run_as_user` on every invocation and exposes injectable `runner` + `env_provider` seams;
[`models.py`](../src/basecradle_router/models.py)'s `Agent` already carries `os_user` / `clone_path` /
`bot_slug`. The home-server work *consumes* these seams — it does not reshape the core.

---

## Part 1 — Provisioning spec

### The box
| Property | Value | Why |
|---|---|---|
| Provider | AWS **Lightsail** (resizable to EC2 later) | Standing decision; cheapest always-on Ubuntu, painless resize-up path. |
| OS | Ubuntu **24.04 LTS** | Current LTS, supported to 2029. |
| Size (start) | **4 GB RAM / 2 vCPU / 80 GB SSD** (~$24/mo) | Each concurrent wake is a `claude -p` Node process (~hundreds of MB) plus a git worktree; the daemon itself is tiny. 4 GB is fleet-ready headroom for several concurrent wakes. |
| Resize trigger | Sustained memory pressure as the 3rd–4th agent onboards | Resizing is the whole point of starting on Lightsail. |
| Inbound firewall | **22** (SSH, key-only), **80** (ACME redirect), **443** (webhooks). Everything else denied. | Minimal attack surface on the box that holds the credentials. |
| Static IP | Attached | DNS target for `ai.basecradle.com`. |

### OS-user layout — the security spine
- **One unprivileged OS user per agent, named by the agent's fleet identity slug:**
  `basecradle-ruby-ai`, `basecradle-python-ai`, `basecradle-harness-ai`, … (`basecradle-<repo>-ai`).
  This is the **same slug** as the agent's GitHub bot (`basecradle-ruby-ai[bot]`) and its BaseCradle
  platform handle — agent / bot / handle / OS-user all align under one identity; there is no separate
  naming scheme. Home mode `700`; each holds only its own credentials, unreadable by siblings.
  **basecradle AI (the capital) gets no OS user yet** — it stays on the founder's laptop + subscription
  for the foreseeable future and is not migrated to the server now.
- **One unprivileged `router` service user** runs the daemon. It does **not** run as root and
  **cannot read any agent's secrets**. This is deliberately a *minimal, non-agent* service account, kept
  separate from the fleet-slug agent users (steward's call, per #26): the daemon is the dispatcher, never
  a woken agent, so it carries no fleet identity — just the least privilege it needs to run and escalate.
- **Privilege drop for a wake:** the `router` user escalates *only* through a **root-owned wrapper**,
  `/opt/basecradle-router/bin/wake-runner`, invoked via a locked `sudoers` rule that grants `router`
  exactly that one command and nothing else. The wrapper validates the requested agent against the
  registry, then `exec`s `runuser -u basecradle-<repo>-ai -- claude -p "<trigger>"` in that agent's
  clone. **The wrapper is the privilege boundary** — deliberately not argv-matching in `sudoers` (which
  is brittle and bypassable). This keeps the long-running webhook daemon fully unprivileged.
- **The steward** (basecradle-router AI) runs on the box inherently as the daemon's operator; it needs
  no separate wake-user.

### Filesystem & credential layout
```
/opt/basecradle-router/            # ROOT-owned tree (router cannot write it)
  app/                             # the daemon: checked-out repo + uv venv, owned by `router`
  bin/wake-runner                  # root-owned (root:root, 0755) privilege-drop wrapper
/etc/basecradle-router/
  router.env                       # daemon config (owner router, 0600) — see below
  agents.json                      # the registry (BASECRADLE_ROUTER_AGENTS); root-owned, router
                                   #   read-only (0640) — it is the wake-runner's trusted allowlist
                                   #   so the daemon must not be able to write it; NO secrets
/home/basecradle-ruby-ai/          # mode 700, owned by basecradle-ruby-ai (the fleet slug)
  repos/basecradle-ruby/           # the agent's own clone (cwd of its wake)
  .config/basecradle/agent.env     # 0600 — the agent's secrets (see below)
```

**`router.env`** holds only the daemon's own non-agent config:
```
BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET=<the GitHub App webhook signing secret>
BASECRADLE_ROUTER_AGENTS=/etc/basecradle-router/agents.json
# BASECRADLE_ROUTER_ENABLED_ROUTES defaults to "github"
```

**`agent.env`** (per agent, sourced by the wrapper *as that user*) holds that agent's secrets — its
GitHub App key / token-minting helper, its `ANTHROPIC_API_KEY`, and later its BaseCradle token. This is
the live implementation of `wake.py`'s `env_provider` seam: the env is resolved by the wrapper running
as the agent, so the unprivileged `router` daemon never touches an agent secret.

**No secret lives in this repo, ever** (constitution §Security and Responsibility). Secrets are placed
on the box out-of-band (a founder gate), `chmod 600`, never in git.

### Installed software
- **System-wide (root):** `git`, `gh` CLI (official apt repo), **Node.js LTS + Claude Code**
  (`npm install -g`, so every agent user gets `claude`), **Caddy** (official apt repo). All by
  `deploy/bootstrap.sh`.
- **The `router` user:** **`uv`** (only the daemon needs it).
- **Per-agent (in each agent's home):** that repo's language toolchain via its own version manager
  (asdf/mise) — e.g. Ruby for `basecradle-ruby-ai` — matching how that repo actually builds.
- **The daemon's own Python:** `uv`-managed venv under `/opt/basecradle-router/app`.

### Ingress — the TLS webhook endpoint (`ai.basecradle.com`)
- **Caddy** terminates TLS with automatic Let's Encrypt certificates + renewal and reverse-proxies to
  the local ASGI app. One-block Caddyfile; only 80/443 exposed.
- The ASGI app ([`server.py`](../src/basecradle_router/server.py)) is served by **`uvicorn`** — the
  router's first runtime dependency (added in #37, run via the entrypoint
  `basecradle_router.app:create_app`). It is the minimal omakase ASGI server.
  > **INVARIANT — a single uvicorn worker. Never `--workers N`.** The threaded model's per-repo
  > `threading.Lock` ([`concurrency.py`](../src/basecradle_router/concurrency.py)) serializes same-repo
  > wakes only *within one process*. Multiple worker processes would each hold independent locks and
  > could double-wake the same repo's clone. Pinned in the systemd unit (`deploy/systemd/`).
- Webhook URL: `https://ai.basecradle.com/webhooks/github`.

---

## Part 2 — Deployment roadmap

Sequenced; *(founder)* marks a human gate, *(steward)* marks basecradle-router AI's work. Phase B
files are **authored ahead as ready-to-review artifacts** (per Drawk, 2026-06-05) but **applied only
once the box exists**.

### Phase A — Provisioning
- **A1** *(founder)* Provision the Lightsail Ubuntu 24.04 box (4 GB/2 vCPU/80 GB), attach a static IP,
  open firewall 22/80/443.
- **A2** *(founder)* DNS: `ai.basecradle.com` **A →** static IP.
- **A3** *(steward — authorable now)* `deploy/bootstrap.sh`: an **idempotent bash** setup script —
  create the `router` + per-agent users, install the system toolchains, lay down the directories with
  their modes/owners, install Caddy. Bash over Ansible: one box, "convention over configuration";
  structured so it could become Ansible *if* the fleet ever goes multi-host (out of scope now).

### Phase B — Daemon deploy *(authored ahead as ready-to-review files, per Drawk 2026-06-05; applied only once the box exists)*
- **B1** ✅ **Authored (#36).** systemd unit `deploy/systemd/basecradle-router.service`: runs `uvicorn` (**single worker** —
  the per-repo lock is per-process) as `router`, `EnvironmentFile=/etc/basecradle-router/router.env`,
  `Restart=on-failure`, `TimeoutStopSec` to let the app drain in-flight wakes on shutdown, bound to
  localhost (Caddy fronts it).
  > **Sandboxing is constrained by the in-process wake.** The wake (`sudo`→`runuser`→`claude`) runs as a
  > *child* of this service and inherits its namespace, so the strong filesystem directives **cannot**
  > be used: `NoNewPrivileges=yes` would block the `sudo`→wrapper escalation; `ProtectHome=yes` would
  > hide the agent's clone under `/home`; `ProtectSystem=strict` would make `/home` read-only; and
  > `MemoryDenyWriteExecute=yes` would kill Node's JIT. The real isolation is the per-OS-user
  > separation + the wake-runner boundary (#28), not namespace sandboxing of the router. The unit
  > applies only the wake-compatible directives (`ProtectSystem=full`, `PrivateTmp`, the kernel/cgroup
  > protections). Stronger per-wake isolation later: launch each wake in its own `systemd-run --scope`.
- **B2** ✅ **Authored (#35).** the root-owned `wake-runner` wrapper (`deploy/bin/wake-runner`) + the `sudoers` rule
  (`deploy/sudoers/basecradle-router`). The wrapper's runtime contract:
  `sudo /opt/basecradle-router/bin/wake-runner --user <os_user> --cwd <clone_path> -- claude -p "<trigger>"`.
  It enforces the boundary — root-only, target a registered agent login user (UID ≥ 1000, in the registry
  with that exact clone), `claude`-only — then `runuser`s to the agent, which sources its own `agent.env`
  and execs the wake. **Install root-owned, in the root-owned `bin/`:**
  `install -o root -g root -m 0755 deploy/bin/wake-runner /opt/basecradle-router/bin/` and
  `install -o root -g root -m 0440 deploy/sudoers/basecradle-router /etc/sudoers.d/basecradle-router`
  (validate with `visudo -cf`).
- **B3** ✅ **Done (#40).** `HomeServerWaker` in `wake.py` assembles the wrapper argv
  (`--user`/`--cwd`/`--`); env is empty (the wrapper sources the agent's `agent.env` after the drop).
- **B4** ✅ **Done (#30).** Fast-ack in `server.py`: `accept` runs inline → `202`, `execute` (the wake)
  runs as a tracked background task drained on shutdown.
- **B5** Caddyfile (`deploy/caddy/Caddyfile`): TLS via Let's Encrypt for `ai.basecradle.com`,
  reverse-proxy to the local uvicorn (`127.0.0.1:8000`). Install to `/etc/caddy/Caddyfile`, then
  `caddy validate` + `systemctl reload caddy`.

> **Also shipped (not in the original B-list):** the ASGI **entrypoint** `basecradle_router.app:create_app`
> + the `uvicorn` dependency (#37) — the composition root the systemd unit's `ExecStart` runs — and a
> note that the merge stage is a no-op until **live merge automation (#38)**: the router wakes agents,
> who open their own PRs.

### Phase C — Go-live (ruby-first canary)
The low-stakes canary that proves the per-OS-user + Claude-Code-on-server + router-wake loop end-to-end.
- **C1** *(founder)* Create a per-agent **Anthropic API key** for basecradle-ruby AI; place ruby's
  `agent.env` on the box.
- **C2** *(steward)* Create `basecradle-ruby-ai`, clone `basecradle-ruby` into `/home/basecradle-ruby-ai/repos/basecradle-ruby`, register it in `agents.json`.
- **C3** *(founder)* Enable the **GitHub App webhook** → `https://ai.basecradle.com/webhooks/github`,
  signing secret matching `router.env`.
- **C4** **Canary run:** file a trivial `handoff` issue on `basecradle-ruby` and confirm the **live**
  loop — webhook → verify → wake ruby *as* `basecradle-ruby-ai` → PR → report. The first un-mocked end-to-end run.
- **C5** On a green canary, onboard the remaining worker agents (python, harness, …) by repeating
  C1–C2. **basecradle AI last**, and only once the system is proven stable — and per the standing
  decision it stays on the laptop for the foreseeable future, so it is effectively not migrated this
  phase.

### Phase D — Hardening (ongoing steward duty)
SSH hardening + `fail2ban`, `unattended-upgrades`, retention for the pipeline's structured stage log,
backup of `agents.json` with a documented rebuild, and liveness alerting on the systemd service.

---

## Founder gates (in order)
The human actions this phase needs. Surfaced here so they are never a surprise; the steward builds
right up to each gate and pauses only at it.

1. **Provision** Lightsail Ubuntu 24.04, **4 GB / 2 vCPU / 80 GB**, static IP, firewall **22/80/443**.
2. **DNS:** `ai.basecradle.com` **A →** `<static IP>`.
3. **Anthropic API key:** one per agent — start with **basecradle-ruby AI**.
4. **GitHub App webhook** → `https://ai.basecradle.com/webhooks/github`, signing secret matched to
   `router.env`.

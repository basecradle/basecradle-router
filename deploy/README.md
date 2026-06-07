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
BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS=<comma-separated GitHub logins, e.g. drawkkwast,basecradle-router-ai[bot]>
# BASECRADLE_ROUTER_ENABLED_ROUTES defaults to "github"
```

`BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS` is the github route's **trust gate** (defense-in-depth): a
handoff only wakes an agent if the webhook `sender` — the actor who applied the `handoff` label — is on
this allow-list of fleet actors (org members + fleet App bots). It is **required and non-empty**; the
daemon refuses to start without it, so the check can never be silently off. List human org members by
their GitHub login and each fleet captain's bot as `<slug>[bot]`. Matched case-insensitively.

**`agent.env`** (per agent, sourced by the wrapper *as that user*) holds that agent's secrets — its
`ANTHROPIC_API_KEY` and its GitHub App credentials, and later its BaseCradle token. This is the live
implementation of `wake.py`'s `env_provider` seam: the env is resolved by the wrapper running as the
agent, so the unprivileged `router` daemon never touches an agent secret. The GitHub App credentials are:
```
ANTHROPIC_API_KEY=sk-ant-...
GH_APP_SLUG=basecradle-ruby-ai
GH_APP_ID=<github app id>
GH_APP_BOT_USER_ID=<bot user id, for the commit-author email>
GH_APP_PEM_B64=<base64 of the App private-key PEM>
```
The agent mints its own short-lived `<slug>[bot]` tokens from these with **`deploy/bin/gh-app-token`**
(installed root-owned at `/usr/local/bin/gh-app-token`): `gh-app-token --token` / `--author` / `--remote`.
It reads the creds from the environment (each box agent holds only its own) and signs the JWT via the
`openssl` CLI, so it needs no extra runtime — the same shape as the laptop fleet helper. Each agent's
Claude Code is defaulted (`~/.claude/settings.json`, by `bootstrap.sh`) to **Opus 4.8 High**
(`model` + `effortLevel`) and **`permissions.defaultMode = bypassPermissions`** — a wake is headless
(no human to approve prompts), so the agent must act autonomously; the security boundary is the
per-OS-user isolation + the wake-runner wrapper, not Claude's interactive prompts.

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
- **Liveness:** `GET https://ai.basecradle.com/up` → `200` + the verbatim Rails health
  body (`<!DOCTYPE html><html><body style="background-color: green"></body></html>`). It is the
  fleet-uniform liveness path (constitution → Operational Baselines), served **from the app**, so a
  green `/up` proves uvicorn itself is up — not merely that Caddy/the host replied. One path,
  checked the same way as the Rails platform's `basecradle.com/up`. There is deliberately no
  competing `/healthz`.

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
  (validate with `visudo -cf`). **After this first install, `deploy/deploy.sh` reinstalls the
  wrapper from the deployed tree on every deploy**, so the live wrapper can never silently drift from
  `main` (it is code on the launch path, but lives root-owned outside the router-owned `app/` tree the
  app-rsync mirrors). The `sudoers` rule is *not* auto-rewritten — it changes rarely and a bad rule is
  dangerous, so it stays this documented manual step.
- **B3** ✅ **Done (#40).** `HomeServerWaker` in `wake.py` assembles the wrapper argv
  (`--user`/`--cwd`/`--`); env is empty (the wrapper sources the agent's `agent.env` after the drop).
- **B4** ✅ **Done (#30).** Fast-ack in `server.py`: `accept` runs inline → `202`, `execute` (the wake)
  runs as a tracked background task drained on shutdown.
- **B5** Caddyfile (`deploy/caddy/Caddyfile`): TLS via Let's Encrypt for `ai.basecradle.com`,
  reverse-proxy to the local uvicorn (`127.0.0.1:8000`). Install to `/etc/caddy/Caddyfile`, then
  `caddy validate` + `systemctl reload caddy`.

> **Also shipped (not in the original B-list):** the ASGI **entrypoint** `basecradle_router.app:create_app`
> + the `uvicorn` dependency (#37) — the composition root the systemd unit's `ExecStart` runs.
>
> **The pipeline ends at the wake — the router never merges (#38, decided).** Auto-merge of a captain's
> own green PR (Earned Autonomy) is done by **GitHub native auto-merge**: during its wake the agent opens
> its PR and runs `gh pr merge --auto --squash` under its own bot identity, so the platform merges when
> required checks pass. A router-side merger was rejected — it would have meant a standing merge-capable
> token on the crown-jewels box, contradicting "the router holds no secret." Per-repo prerequisite:
> branch protection with required status checks, **Allow auto-merge**, and **Automatically delete head
> branches** all enabled (part of repo bootstrap).

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
SSH hardening + `fail2ban`, `unattended-upgrades` (the install half is on; the **reboot half** is the
clean-reboot mechanism in Part 4), retention for the pipeline's structured stage log, backup of
`agents.json` with a documented rebuild, and liveness alerting on the systemd service.

---

## Part 3 — Shipping: the deploy loop (Definition of Done)

**`merged` ≠ `done`.** The artifact is a running service; a merge to `main` changes nothing on the box
until the code is rsynced there and the daemon restarts. Issue #54 was the proof: #50/#52/#53 sat merged
but unrun for a day while the live daemon served pre-#52 code, because "done" silently meant "merged" and
nothing redeployed. The Definition of Done is therefore the full loop, codified in `CLAUDE.md`
("Deploying — Definition of Done") and implemented here:

> **tested (offline) → deployed to the box → smoke-tested LIVE → confirmed.**

### One command: `deploy/deploy.sh`
Run from a trusted local checkout. It *is* the loop, so a deploy can never silently half-finish:

1. **Test (offline gate)** — refuses to proceed unless `ruff` + `pytest` pass locally **and** `HEAD ==
   origin/main` with a clean tree (so you can only ship merged, current code; `FORCE=1` overrides for an
   emergency).
2. **Deploy** — rsyncs the checkout to staging on the box, `sudo rsync`s into `/opt/basecradle-router/app`,
   `chown`s to `router`, `uv sync`s, reinstalls the root-owned `wake-runner` wrapper, **installs + enables
   the managed systemd units** from the deployed tree (the daemon, drift `.{service,timer}`, recovery
   `.service`, reboot `.{service,timer}` — `daemon-reload`, arm the timers, enable the recovery gate;
   issue #71, so a merged unit can't be merged-but-not-installed), **stamps the deployed commit SHA** to
   `/etc/basecradle-router/deployed-sha`, and restarts the service (asserting it comes back active).
3. **Smoke (live)** — runs `deploy/smoke-test.sh` against the real endpoint, then asserts the
   fleet-uniform liveness route `GET /up` is green over the public TLS path (Caddy → uvicorn); either
   failure aborts the deploy loudly. *This deploy is not done unless both are green.*
4. **Confirm** — prints the deployed SHA, the live trusted-actor list, and a drift check that must read
   "in sync".

```bash
# from the laptop checkout, on a clean main:
ROUTER_SSH_KEY=<path to the Lightsail key> deploy/deploy.sh
```
Config via env: `ROUTER_HOST` (default `ubuntu@ai.basecradle.com` — the public DNS name; **no infra IP
lives in this repo**), `ROUTER_SSH_KEY`, `SMOKE_URL`, `FORCE=1`.

> **Why rsync-from-laptop, not a token on the box?** The box holds the fleet's crown jewels, so it carries
> no GitHub credential — code arrives by rsync from a trusted checkout, never by the box pulling. The daemon
> just *runs* the code; it never pushes or pulls.

### The live smoke test: `deploy/smoke-test.sh`
Proves the **running** daemon enforces the boundary — not that code merged, but that the bytes serving
traffic right now behave. Three synthetic, GitHub-shaped, HMAC-signed webhooks at the live endpoint:

| Case | Webhook | Asserted | Proves |
|---|---|---|---|
| 1 | bad signature | **401** | the HMAC verify boundary holds |
| 2 | valid sig, **untrusted** sender | **400** | the #52 trusted-actor gate **rejects** strangers |
| 3 | valid sig, **trusted** sender, **unregistered** repo | **200** | the gate **admits** the fleet — and no agent is woken (resolve finds none) |

Case 3 targets a repo that is never in the registry, so it exercises the whole accept path past the gate
**without waking any real agent** — safe to run against production at any time. It reads the signing secret
and the trusted-actor list from `router.env` (root-readable only), so it runs on the box (`deploy.sh`
invokes it over SSH; or `sudo deploy/smoke-test.sh` by hand). The same three status-code outcomes are pinned
offline in `tests/test_server_e2e.py`, so the smoke test can't bit-rot against the route logic unnoticed.

### Drift can never be silent: `deploy/drift-check.sh` + the timer
`drift-check.sh` compares the stamped deployed SHA against the live tip of `origin/main`, fetched
tokenlessly with `git ls-remote` (the repo is public, so the box needs no credential to ask "what is main
now?"). It exits non-zero and prints loudly on drift (or a missing stamp). `deploy.sh` runs it as the final
confirm step, and `deploy/systemd/basecradle-router-drift.{service,timer}` run it **hourly** as the `router`
user — so a merge that never reached the box surfaces in `systemctl --failed` and the journal, instead of
going unnoticed for a day. It only reads; it never auto-deploys.

### Why not fully-automated CD (GitHub Actions → prod)?
Deliberately not done. Auto-deploy-on-merge from a GitHub-hosted runner needs either an SSH key to the
crown-jewels box stored in CI secrets, or the box's SSH opened to GitHub's shared runner ranges; a
self-hosted runner means GitHub's runner agent executing workflow code *on* the box that holds every agent's
credentials. Both regress the box's governing constraint ("least privilege everywhere"). The chosen model —
a self-verifying **one-command deploy** from a trusted local checkout, plus a **drift alarm** that makes
the merge≠deploy gap loud — closes the silent-drift root cause (#54) without that security trade. Revisit
only if the manual deploy step ever becomes the bottleneck; the natural next step would be a pull-based,
human-initiated deploy on the box, not unattended push from CI.

---

## Part 4 — OS updates & reboots (clean, observable reboots)

**The gap this closes (issue #66).** `unattended-upgrades` installs security/kernel patches but **does
not reboot**, so a kernel fix can sit installed-but-inactive — "applied in name only" — until someone
remembers to reboot. The fleet home box was found exactly like that (running an unpatched kernel with a
newer one already on disk). This service now owns a reboot mechanism that is clean and observable, and
**automatic reboots are turned on** (founder decision, issue #66: "done" means auto-reboot is
operational — that is the whole point of building this). What makes an unattended reboot safe is the
post-boot recovery gate below: the box reboots cleanly, then proves it came back or alarms loudly.

Two halves, mirroring the deploy loop's "do it, then verify it" shape:

- **Perform the reboot cleanly — [`deploy/reboot-if-required.sh`](reboot-if-required.sh).** A no-op
  unless `/var/run/reboot-required` exists (the flag apt drops when an installed package needs a
  reboot). When it does: it **drains** the router first — `systemctl stop basecradle-router`, which
  blocks on the unit's lifespan drain (bounded by `TimeoutStopSec`) so in-flight wakes finish rather
  than being severed — then performs a controlled `systemctl reboot`. A drain failure is logged but does
  **not** abort the reboot (a stuck unit must never pin an unpatched kernel forever).
- **Verify recovery after boot — [`deploy/verify-recovery.sh`](verify-recovery.sh).** Asserts the
  services are active **and** `/up` is green (polling, since `After=` only orders start, not health),
  and **exits nonzero + loud** if the box did not come back. It checks `/up` on the **local** app
  (`127.0.0.1:8000`) so a green result proves uvicorn itself recovered, independent of Caddy/DNS/TLS
  (the `caddy` service check covers the front end). Run by the
  `basecradle-router-recovery.service` oneshot unit at every boot, a failure lands in `systemctl
  --failed` and the journal — the **same alarm convention as the drift check** — instead of the box
  silently serving nothing.

### systemd units (`deploy/systemd/`)
> Since issue #71, **`deploy/deploy.sh` installs + enables these from the deployed tree on every run** —
> `daemon-reload`, arm the timers, enable the recovery gate. The `enable` commands below are the explicit
> contract / first-time-by-hand fallback; you don't run them per deploy.

| Unit | Role | Enable? |
|---|---|---|
| `basecradle-router-recovery.service` | post-boot health gate (services + `/up`) | **Enable** (`systemctl enable basecradle-router-recovery.service`). Read-only, observational; verifies recovery after *every* reboot — manual or automatic. |
| `basecradle-router-reboot.service` | the clean-reboot oneshot (drives `reboot-if-required.sh`) | `static` (timer-driven). Install (`daemon-reload`). |
| `basecradle-router-reboot.timer` | schedules the reboot check in a low-traffic window | **Enable** (`systemctl enable --now basecradle-router-reboot.timer`) — this is what turns automatic reboots ON. |

### The reboot policy (decided: automatic)
**Automatic reboots are on.** The `basecradle-router-reboot.timer` fires daily at **5:00 AM US Central**
(`OnCalendar=*-*-* 05:00:00 America/Chicago` — the timezone is named in the spec so it stays 5 AM Central
across DST; the box's systemd 255 supports this, vs. a fixed UTC value that would drift an hour;
`RandomizedDelaySec=15min`). `reboot-if-required.sh` no-ops unless a reboot is actually pending, so an
actual reboot happens only on the days an OS update staged one. This is the founder's call (issue #66): the deferral was only until the mechanism existed and
was verified working — now it is, so the box reboots itself to take security/kernel patches, and the
recovery gate confirms it came back (or alarms). It pairs with the Phase D hardening duty
(`unattended-upgrades` — the install half; this is the reboot half). The manual fallback still works
(`systemctl start basecradle-router-reboot.service`, or `reboot-if-required.sh` by hand) for an
out-of-window reboot.

---

## Founder gates (in order)
The human actions this phase needs. Surfaced here so they are never a surprise; the steward builds
right up to each gate and pauses only at it.

1. **Provision** Lightsail Ubuntu 24.04, **4 GB / 2 vCPU / 80 GB**, static IP, firewall **22/80/443**.
2. **DNS:** `ai.basecradle.com` **A →** `<static IP>`.
3. **Anthropic API key:** one per agent — start with **basecradle-ruby AI**.
4. **GitHub App webhook** → `https://ai.basecradle.com/webhooks/github`, signing secret matched to
   `router.env`.

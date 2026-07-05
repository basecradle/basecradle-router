# Router daemon — deploy & ops runbook

> **Status:** approved design (issue #24, 2026-06-05). This document is the contract for how the router
> **daemon** is deployed and operated on its box; the scripts and units that implement it live in this
> `deploy/` directory.
>
> **Scope — daemon only.** This is the **router daemon's** runbook: its config, security boundary,
> ingress, deploy loop, and ops. **Onboarding/provisioning a builder agent** — creating its OS user,
> cloning its repo, seeding `~/.claude`, placing its `agent.env` and per-agent token minter — is the
> **NOC's** job (the composable builder leaf, [basecradle-noc#53](https://github.com/basecradle/basecradle-noc/issues/53)),
> and the onboarding roster lives at [basecradle-noc#91](https://github.com/basecradle/basecradle-noc/issues/91).
> The router daemon *wakes* agents; it does not onboard them, and nothing onboarding-related lives in this
> repo.

## What this is

The router daemon runs on **one dedicated box that *is* the fleet's home** — `ai.basecradle.com`, a
dedicated Ubuntu server. It is not a published package; the daemon reaches the box by deploy, not by
release.

> **Who does what (capital PR basecradle#363, issue #122).** **basecradle-router AI builds and
> maintains the router daemon's code and all the version-controlled server/deploy config in this repo
> (`deploy/`) — it never deploys.** The **capital owns and operates `ai.basecradle.com`** (the **NOC**
> once its fleet-ops ships): it provisions the box, installs the daemon, and runs every command in this
> doc that touches the box. The router-AI is a **tenant** on the box, not its operator. Throughout this
> doc, work marked *(router-AI)* is config the agent authors in this repo; anything that installs, runs,
> or hardens on the box is the **capital/NOC's**, even where older wording below still reads as if one
> actor did both.

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

## Part 1 — The daemon on the box

### The security boundary — OS-user isolation + the wake-runner

The router wakes each agent **as that agent's own unprivileged OS user, in that agent's own repo clone**.
The daemon enforces least privilege around that wake:

- **One unprivileged `router` service user** runs the daemon. It does **not** run as root and
  **cannot read any agent's secrets**. This is deliberately a *minimal, non-agent* service account, kept
  separate from the fleet-slug agent users (router-AI's design call, per #26): the daemon is the dispatcher, never
  a woken agent, so it carries no fleet identity — just the least privilege it needs to run and escalate.
- **Privilege drop for a wake:** the `router` user escalates *only* through a **root-owned wrapper**,
  `/opt/basecradle-router/bin/wake-runner`, invoked via a locked `sudoers` rule that grants `router`
  exactly that one command and nothing else. The wrapper validates the requested agent against the
  registry, then `exec`s `systemd-cat --identifier=basecradle-wake-<os_user> -- runuser -u
  basecradle-<repo>-ai -- claude -p "<trigger>"` in that agent's clone — the `systemd-cat` wrap routes
  the wake's stdout+stderr into journald under a per-agent identifier (see *Reading a Wake's Ledger*
  below) while `exec`-ing straight through to `runuser`, so the privilege drop and exit-code
  propagation are unchanged. **The wrapper is the privilege boundary** — deliberately not argv-matching
  in `sudoers` (which is brittle and bypassable). This keeps the long-running webhook daemon fully
  unprivileged.
- **The box's operator** (the capital today; the NOC once its fleet-ops ships) acts on the box over its
  own administrative SSH, not as a fleet wake-user — it installs and operates the daemon. basecradle-router
  AI has **no** operator presence on the box: it authors this config in the repo and never logs in to deploy.

The agent OS users themselves — created, credentialed, and seeded by the NOC's onboarding (noc#91) — are
the daemon's *wake targets*, resolved from the registry below. Provisioning them is not the daemon's job.

### Daemon filesystem & config layout
```
/opt/basecradle-router/            # ROOT-owned tree (router cannot write it)
  app/                             # the daemon: checked-out repo + uv venv, owned by `router`
  bin/wake-runner                  # root-owned (root:root, 0755) privilege-drop wrapper
/etc/basecradle-router/
  router.env                       # daemon config (owner router, 0600) — see below
  agents.json                      # the registry (BASECRADLE_ROUTER_AGENTS); root-owned, router
                                   #   read-only (0640) — it is the wake-runner's trusted allowlist
                                   #   so the daemon must not be able to write it; NO secrets
```

The daemon's own Python is a **`uv`-managed venv** under `/opt/basecradle-router/app`, `uv sync`ed by the
deploy (the NOC's `deploy-router` op) on each run. The daemon's only system dependency on the box is the privilege-drop
chain (`sudo` → `wake-runner` → `systemd-cat` → `runuser` → the agent's `claude`); everything an agent needs to *run* is
part of that agent's own onboarding, not the daemon's.

#### Reading a Wake's Ledger

`wake-runner` routes each wake's stdout+stderr into journald under the identifier
`basecradle-wake-<os_user>`, so one persona's wake output — including the harness's per-step ledger
(`step N/M: tools=… (…s)`, `wake used X/N steps`) and its install/config warnings — is greppable on
its own:

```bash
journalctl -t basecradle-wake-basecradle-glm-ai -n 100 --no-pager   # one agent's recent wakes
journalctl -t basecradle-wake-basecradle-glm-ai -f                  # follow a wake live
```

The identifier is the agent's OS-user slug (its universal identity), so it is stable and needs no
per-agent list. Grep by this `-t` identifier, **not** by `_UID`: `systemd-cat` opens the journal stream
as root (before the privilege drop), so the entries are stamped `_UID=0` even though the wake runs as
the agent — the identifier is the per-agent axis, the uid is not. Before this seam the router captured
the wake's output and dropped it, so none of it reached journald at all (basecradle-router#168).

Two properties worth knowing. **(1)** A failed wake's error detail now lives here, not in the router's
own log — the router's `capture_output` sees EOF once the wake's fds point at journald, so its
`WakeError` degrades to `(no output)`; the reason (`claude` stderr, an auth failure, etc.) is under this
identifier at the same timestamp. **(2)** These entries ride the normal journald→telemetry path and are
covered by `vector.yaml`'s `ai_scrub` redaction like every other log (Part 5) — they are **not** dropped
(that rule targets the `sudo` identifier, not this one), and the harness never prints secrets, so no
extra scrubbing layer is added. The wake does gain one dependency: `systemd-cat` aborts (and the wake
fails, retryably) if it cannot open the journal stream — negligible on a healthy box, where the journal
socket survives a journald restart.

**`router.env`** holds only the daemon's own non-agent config:
```
BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET=<the GitHub App webhook signing secret>
BASECRADLE_ROUTER_AGENTS=/etc/basecradle-router/agents.json
BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS=<comma-separated GitHub logins, e.g. drawkkwast,basecradle-router-ai[bot]>
# BASECRADLE_ROUTER_ENABLED_ROUTES defaults to "github"; set to "github,basecradle"
#   to also accept BaseCradle platform events (then the basecradle secret is required):
# BASECRADLE_ROUTER_ENABLED_ROUTES=github,basecradle
# BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET=<the agent's BaseCradle integration_secret>
#
# Wake-rate circuit breaker (the runaway-loop backstop, issue #110) — all optional,
# generous defaults; only a genuine runaway should ever trip it:
# BASECRADLE_ROUTER_WAKE_BREAKER_MAX=20          # per-agent wakes per window
# BASECRADLE_ROUTER_WAKE_BREAKER_WINDOW=60       # rolling window, seconds
# BASECRADLE_ROUTER_WAKE_BREAKER_COOLDOWN=60     # halt seconds after a trip (auto-reset)
# BASECRADLE_ROUTER_WAKE_BREAKER_STREAM_MAX=15   # per-(agent, timeline/issue) wakes per window; 0 disables
#
# Delivery dedup (collapse a duplicate webhook delivery into one wake, issue #133) — optional,
# generous default; 0 disables:
# BASECRADLE_ROUTER_DEDUP_TTL=600                # seconds a woken delivery id is remembered; 0 disables
```

`BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS` is the github route's **trust gate** (defense-in-depth): a
wake only fires if the webhook `sender` — the actor who applied the `handoff` label, or who left the
comment — is on this allow-list of fleet actors (org members + fleet App bots). It is **required and
non-empty**; the daemon refuses to start without it, so the check can never be silently off. List human
org members by their GitHub login and each fleet captain's bot as `<slug>[bot]`. Matched
case-insensitively.

**The github route consumes two webhook events** (subscribe each agent's App to both — *Issues* **and**
*Issue comment*):

- **`issues`** (action `opened`/`labeled`) — a handoff issue filed or labeled `handoff`: the initial wake.
- **`issue_comment`** (action `created`) — a **reply on a handoff issue re-wakes its agent** (issue #129).
  Polling only covers an agent while it is awake and looping on an open issue; once it sleeps, a new
  comment reaches no one, so a reply to a sleeping agent would be lost. Re-waking on the comment closes
  that hole — pointing the agent back at the issue to re-read the full thread. Gates narrow it to "a fleet
  peer replied to an in-flight handoff": the comment must be on a real **issue** (a comment on a *pull
  request* — which GitHub also delivers as `issue_comment` — is not a handoff reply and is ignored) that
  carries the `handoff` label, from a trusted actor (same gate as above), and the agent's **own** comment
  never re-wakes it (the infinite-loop guard, run ahead of the trust gate — the router resolves the repo's
  captain bot from `agents.json` and suppresses it). A comment storm on one issue is capped by the
  breaker's per-`(agent, issue)` scope below.

The **basecradle route** (issue #87) accepts signed BaseCradle platform events at
`POST /webhooks/basecradle`. Enable it by adding `basecradle` to
`BASECRADLE_ROUTER_ENABLED_ROUTES` and setting `BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET` to the
recipient agent's `integration_secret` (the platform signs each delivery with it, HMAC-SHA256 over the
raw body in `X-BaseCradle-Signature`, exactly like GitHub). The platform decides what to deliver to whom,
so a valid signature *is* the trust — there is no extra actor allow-list here. A `message.created`
delivery resolves to the agent by its BaseCradle user uuid (`recipient_uuid`) and wakes that agent's
harness for the event's `timeline_uuid`.

The **wake-rate circuit breaker** (issue #110) is the router's cross-agent runaway backstop. The router
is the single chokepoint for every wake, so it alone can catch a runaway loop the per-agent harness layer
can't (a harness that crashes before it can self-track, a multi-agent ping-pong, a novel loop from a
drop-in `tools/` or MCP server). It tracks wakes per **agent** (and per **(agent, timeline/issue)**) in a
rolling window; over a generous sanity cap it **trips** — stops dispatching that scope's wakes, logs a
visible refusal, and escalates with a loud `CIRCUIT BREAKER TRIPPED` `ERROR` the NOC can detect — then
**auto-resets** once the cooldown elapses and the window clears (a transient burst self-heals). The four
`router.env` knobs above tune it; defaults (20/60 s per agent, 15/60 s per stream, 60 s cooldown) are set
so legitimate multi-peer activity never trips it. Refusing a wake never loses data — the platform's
cursor-paginated read API is the source of truth and push is best-effort — so a tripped agent simply
pauses its push until the loop is understood. The breaker is defense-in-depth with, and independent of,
the harness's own per-timeline self-breaker (basecradle-harness#138): no shared protocol, each trips on
its own view.

**Delivery dedup** (issue #133) collapses a duplicate webhook delivery into a single wake. One logical
event can reach the router as more than one delivery — e.g. two fleet GitHub Apps installed on a repo,
both subscribed to the same event, each POSTing to the router — and GitHub stamps every such delivery of
*one* event with the **same** `X-GitHub-Delivery` GUID. The per-agent lock serialises them (no collision),
but without dedup each delivery wakes the agent independently, so N subscribed Apps cost N full agent
sessions for one event. The router keeps a short-TTL "recently-**woke** delivery" cache keyed on
`source:delivery_id`: checked inside the per-agent lock, ahead of the wake-lock and breaker, and marked
**only after a wake actually succeeds** — so a duplicate serialised behind the original observes the mark
and is collapsed to a visible `dedup`/`IGNORED`, while a *failed* original leaves the duplicate free to
retry. Keying on the delivery GUID (not event content) can never over-collapse a *distinct* event — the
dangerous direction — at worst it no-ops. `BASECRADLE_ROUTER_DEDUP_TTL` (default 600 s; `0` disables)
tunes it.

The **NOC wake-lock interlock** (issue #120, counterpart to basecradle-noc#38) keeps the router from
waking an agent while the NOC is converging (upgrading) its harness — a wake landing on a half-installed
venv is the failure to avoid. The NOC writes a root-owned lock at
`/run/basecradle-noc/wake-locks/<slug>.lock` (on the `/run` tmpfs, so a reboot clears stale locks) for
the duration of a converge; before each wake the router reads the agent's lock and, if it is **present
and unexpired**, *refuses* the wake — logging `event=wake_refused reason=wake_lock_held agent=<slug>`
(visible in BetterStack Live Tail via the `AI` source). No data is lost: the message stays on the
platform's read API and the agent processes it on its next wake once the lock clears. An **expired** lock
(the NOC died mid-converge) is treated as stale — the router wakes but logs `event=wake_lock_stale` so
the wedged converge is visible. There is **nothing to configure** (the lock path is the only contract,
pinned by the capital) and it is harmless with no locks present — no lock file, never refuses. The lock
content is non-secret (slug + converge reason + timestamps), so the NOC publishes it world-readable
(root-owned `0644` in a `0755` dir) and the unprivileged `router` daemon reads it directly; a perms fault
fails *open* (the router wakes) with a loud `event=wake_lock_unreadable` `ERROR` rather than wedge the
fleet. Independent of the harness's own self-breaker and of the wake-rate breaker — defense-in-depth,
each layer on its own view.

#### The registry (`agents.json`)

A JSON object of **agent key → fields** — the daemon's wake-resolution table, maintained on the box by
the operator (the daemon reads it; it never writes it). The key is the agent's stable slug: a GitHub
builder's key is the `owner/name` repo it captains; a harness persona's key is its bare slug (e.g. `jt`).
An entry's `kind` selects how it is read (absent ⇒ `github`); existing github entries are unchanged.

```jsonc
{
  // A GitHub builder (kind defaults to "github"): woken via `claude -p "<trigger>"`,
  // resolved by the repo a handoff issue is filed on.
  "basecradle/basecradle-ruby": {
    "os_user": "basecradle-ruby-ai",
    "clone_path": "/home/basecradle-ruby-ai/repos/basecradle-ruby",
    "bot_slug": "basecradle-ruby-ai"
  },
  // A harness persona: woken via its OWN wake CLI in its venv, resolved by its
  // BaseCradle user uuid. No bot_slug; carries kind/recipient_uuid/wake_bin.
  "jt": {
    "kind": "harness",
    "os_user": "jt",
    "clone_path": "/home/jt/harness",
    "recipient_uuid": "019e916c-7f45-700e-afc0-f45557b237b7",
    "wake_bin": "/home/jt/venv/bin/basecradle-harness-wake"
  }
}
```

The `wake-runner` reads the same registry: for a harness entry it launches **only** the pinned
`wake_bin` (which must resolve inside that agent's `/home/<user>`), never a caller-supplied path — the
same registry-is-the-only-authority rule that confines builders to the system `claude`.

Each agent's own secrets live in a per-agent `agent.env` that the **wrapper** loads *as that user* after
the privilege drop — the live implementation of `wake.py`'s `env_provider` seam, so the unprivileged
`router` daemon never touches an agent secret. The file is parsed as **literal `KEY=VALUE` lines, never
bash-`source`d** (#109): the value after the first `=` is taken verbatim (one layer of surrounding quotes
stripped), so a secret containing `$`, a backtick, `$( )`, spaces, or quotes is loaded as data and never
evaluated as shell. *Placing* that `agent.env` (its contents, the agent's API key and GitHub App
credentials, its token minter) is the NOC's onboarding job (noc#91), not the daemon's.

**No secret lives in this repo, ever** (constitution §Security and Responsibility). Secrets are placed
on the box out-of-band, `chmod 600`, never in git.

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

## Part 2 — The daemon's deploy components

The daemon's on-box artifacts, each **authored in this repo by the router-AI** and **installed/run on the
box by the deployer** (the **NOC** — the fleet's sole deployer). The router-AI never deploys —
anything that installs, runs, or hardens on the box is the NOC's. (The first install of the
root-owned wrapper + `sudoers` rule is a one-time on-box step; thereafter the NOC's `deploy-router` op keeps
the wrapper and the managed units in lockstep with `main` on every deploy.)

- **The systemd unit** `deploy/systemd/basecradle-router.service`: runs `uvicorn` (**single worker** —
  the per-repo lock is per-process) as `router`, `EnvironmentFile=/etc/basecradle-router/router.env`,
  `Restart=on-failure`, `TimeoutStopSec` to let the app drain in-flight wakes on shutdown, bound to
  localhost (Caddy fronts it).
  > **Sandboxing is constrained by the in-process wake.** The wake (`sudo`→`runuser`→`claude`) runs as a
  > *child* of this service and inherits its namespace, so the strong filesystem directives **cannot**
  > be used: `NoNewPrivileges=yes` would block the `sudo`→wrapper escalation; `ProtectHome=yes` would
  > hide the agent's clone under `/home`; `ProtectSystem=strict` would make `/home` read-only; and
  > `MemoryDenyWriteExecute=yes` would kill Node's JIT. The real isolation is the per-OS-user
  > separation + the wake-runner boundary, not namespace sandboxing of the router. The unit
  > applies only the wake-compatible directives (`ProtectSystem=full`, `PrivateTmp`, the kernel/cgroup
  > protections). Stronger per-wake isolation later: launch each wake in its own `systemd-run --scope`.
- **The root-owned `wake-runner` wrapper** (`deploy/bin/wake-runner`) + the `sudoers` rule
  (`deploy/sudoers/basecradle-router`). The wrapper's runtime contract:
  `sudo /opt/basecradle-router/bin/wake-runner --user <os_user> --cwd <clone_path> -- <wake command>`,
  where the wake command is `claude -p "<trigger>"` for a builder or `<wake_bin> --timeline "<uuid>"`
  for a harness persona (#87) — the binary decided by the registry, never the caller's argv. The
  `sudoers` rule is command-generic (it grants only the wrapper), so adding the harness kind needs no
  sudoers change: the registry entry for the new agent is what authorizes its wake.
  It enforces the boundary — root-only, target a registered agent login user (UID ≥ 1000, in the registry
  with that exact clone), `claude`-only — then `runuser`s to the agent, which loads its own `agent.env`
  (literal `KEY=VALUE`, never sourced — #109) and execs the wake. **Install root-owned, in the root-owned `bin/`:**
  `install -o root -g root -m 0755 deploy/bin/wake-runner /opt/basecradle-router/bin/` and
  `install -o root -g root -m 0440 deploy/sudoers/basecradle-router /etc/sudoers.d/basecradle-router`
  (validate with `visudo -cf`). **After this first install, the NOC's `deploy-router` op reinstalls the
  wrapper from the deployed tree on every deploy**, so the live wrapper can never silently drift from
  `main` (it is code on the launch path, but lives root-owned outside the router-owned `app/` tree the
  app-mirror covers). The `sudoers` rule is *not* auto-rewritten — it changes rarely and a bad rule is
  dangerous, so it stays this documented manual step.
- **`HomeServerWaker`** in `wake.py` assembles the wrapper argv (`--user`/`--cwd`/`--`); env is empty
  (the wrapper loads the agent's `agent.env` after the drop).
- **Fast-ack** in `server.py`: `accept` runs inline → `202`, `execute` (the wake) runs as a tracked
  background task drained on shutdown.
- **The Caddyfile** (`deploy/caddy/Caddyfile`): TLS via Let's Encrypt for `ai.basecradle.com`,
  reverse-proxy to the local uvicorn (`127.0.0.1:8000`). Install to `/etc/caddy/Caddyfile`, then
  `caddy validate` + `systemctl reload caddy`.
- **The ASGI entrypoint** `basecradle_router.app:create_app` + the `uvicorn` dependency (#37) — the
  composition root the systemd unit's `ExecStart` runs.

> **The pipeline ends at the wake — the router never merges (#38, decided).** Auto-merge of a captain's
> own green PR (Earned Autonomy) is done by **GitHub native auto-merge**: during its wake the agent opens
> its PR and runs `gh pr merge --auto --squash` under its own bot identity, so the platform merges when
> required checks pass. A router-side merger was rejected — it would have meant a standing merge-capable
> token on the crown-jewels box, contradicting "the router holds no secret." Per-repo prerequisite:
> branch protection with required status checks, **Allow auto-merge**, and **Automatically delete head
> branches** all enabled.

### Hardening (ongoing; capital/NOC operates, router-AI authors the config)
SSH hardening + `fail2ban`, `unattended-upgrades` (the install half is on; the **reboot half** is the
clean-reboot mechanism in Part 4), retention for the pipeline's structured stage log, backup of
`agents.json` with a documented rebuild, and liveness alerting on the systemd service. The router-AI
authors and maintains this hardening config in the repo; the box's operator (capital/NOC) applies and
runs it on the box.

---

## Part 3 — Shipping: the deploy loop (the deployer's Definition of Done)

> **The router-AI never runs this loop.** Deploying is the **NOC's** job — the fleet's sole deployer
> (CLAUDE.md → "Building vs. Deploying — the router-AI never deploys", issue #122; constitution → "One
> deployer for the fleet's machines: the NOC"). This section is the **deployer's** runbook; the router-AI's
> only stake in it is keeping this config correct, green, and merged, and keeping the on-box **contract**
> below stable so the NOC's op stays in lockstep.

**For the deployer, `merged` ≠ `done`.** The artifact is a running service; a merge to `main` changes
nothing on the box until the code lands there and the daemon restarts. Issue #54 was the proof:
#50/#52/#53 sat merged but unrun for a day while the live daemon served pre-#52 code, because "done"
silently meant "merged" and nothing redeployed. The deployer's Definition of Done is therefore the full
loop, mirrored in `CLAUDE.md` ("Building vs. Deploying"):

> **tested (offline) → deployed to the box → smoke-tested LIVE → confirmed.**

### The deploy mechanism: the NOC's `deploy-router` op (basecradle-noc#134)
Run by the NOC (the fleet's sole deployer), **not** basecradle-router AI:

```bash
basecradle-noc deploy-router <sha>
```

The box **PULLS** the merged commit **anonymously from the public `basecradle-router` repo** by SHA (so the
crown-jewels box carries **no GitHub credential** — a public repo needs no read token, strictly better than
a scoped one) and runs the same Definition-of-Done loop **on-box**, plus a **rollback** to the prior good
SHA on any failure. github.com TLS authenticates the source; the content-addressed SHA verifies the bytes;
the driver's offline gate confirms that SHA is the tip of branch-protected, CI-gated `main`. This replaced
the old capital-run `deploy/deploy.sh` rsync-from-laptop (basecradle#395). The op's steps mirror what
deploy.sh did on-box — mirror into `/opt/basecradle-router/app` (protecting `.venv`) + `chown router`,
`uv sync`, reinstall `wake-runner` + the systemd unit files, stamp the SHA, `daemon-reload` + restart +
settle + `is-active`, then the live smoke test — and the NOC's driver adds the out-of-band `GET /up` check
over the public TLS path (a broken `/up` after an on-box success is a FAIL).

#### The on-box contract the op consumes — the router repo owns these (confirmed, basecradle#395)
The router owns the **contract** (the *what* — stable paths, names, and artifacts); the NOC owns the deploy
**mechanism** (the *how* — pulling, installing, restarting on the box). The `deploy-router` op reads these
from the deployed tree / box, and they are this repo's to keep stable:

| Contract | What the op does with it |
|---|---|
| `/opt/basecradle-router/app` | the router-owned daemon tree the op mirrors into (protecting its `.venv`), then `chown router:router` + `uv sync` as `router`. |
| `deploy/bin/wake-runner` | reinstalled root-owned (`root:root`, `0755`) to `/opt/basecradle-router/bin/wake-runner` on every deploy. |
| `deploy/systemd/*.service` + `*.timer` | **all** unit files installed (globbed `0644`), so a newly-added unit is never missed. Enable **policy stays the router's**: the op arms every `*.timer` (`enable --now`) and keeps `basecradle-router.service` enabled, but **never enables `*.service` generically** — it cannot tell `recovery.service` (must be enabled) from `reboot.service` (must stay timer-triggered, though it carries `[Install]`). |
| `/etc/basecradle-router/deployed-sha` | the SHA stamp, written world-readable (`0644`) — the drift source `drift-check.sh` reads. |
| `deploy/smoke-test.sh` | run as root post-restart as the live smoke gate; a smoke failure rolls the deploy back. |

> **`recovery.service` enable is a provisioning concern, not a routine-deploy one.** The old deploy.sh
> re-`enable`d `basecradle-router-recovery.service` on every run (belt-and-suspenders). The NOC op does not,
> because it cannot distinguish a service that must be enabled from one that must stay timer-triggered — both
> carry `[Install]`. This is harmless: an `enable` symlink **persists across a file reinstall**, so the
> recovery gate enabled once at provisioning stays enabled. Adding a **new** non-timer service that must be
> enabled directly (or a new root-owned file outside `app/` beyond `wake-runner`) is therefore a
> **provisioning change**, not a routine deploy — out of the op's routine band by design. New **timers** and
> new **unit files** are handled automatically (the timer arm-loop and the unit glob).

> **Why the op globs units rather than a router-owned `deploy/apply.sh`?** Considered and declined
> (basecradle#395). The unit glob already drift-proofs the common evolution (adding a unit), so an
> `apply.sh` would protect only the rare provisioning-class changes above — for which it would add a
> cross-repo NOC re-point PR and put a privileged install sequence in a repo whose agent has no on-box
> presence. The contract-vs-mechanism split is the cleaner line: stable artifacts here, install logic in
> the NOC's op.

### `deploy/deploy.sh` is RETIRED (interim emergency fallback only)
The old one-command rsync-from-laptop loop `deploy/deploy.sh` is **retired**, superseded by the op above. It
refuses to run by default — for everyone, NOC and capital included — directing the deployer to
`basecradle-noc deploy-router <sha>`. Its rsync body survives **only** as an emergency fallback for the
transition window **before** the `deploy-router` wrapper is installed on the box; a deployer who genuinely
needs it in that gap must opt in explicitly with `ROUTER_INTERIM_RSYNC_DEPLOY=1` (and still `DEPLOYER=noc`).
Once the NOC path is live on the box, it is never used again, and this fallback can be deleted outright.

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
and the trusted-actor list from `router.env` (root-readable only), so it runs on the box (the `deploy-router`
op runs it as root post-restart; or `sudo deploy/smoke-test.sh` by hand). It defaults `SMOKE_URL` to the
live endpoint, so it needs no argument. The same three status-code outcomes are pinned offline in
`tests/test_server_e2e.py`, so the smoke test can't bit-rot against the route logic unnoticed.

### Drift can never be silent: `deploy/drift-check.sh` + the timer
`drift-check.sh` compares the stamped deployed SHA against the live tip of `origin/main`, fetched
tokenlessly with `git ls-remote` (the repo is public, so the box needs no credential to ask "what is main
now?"). It exits non-zero and prints loudly on drift (or a missing stamp). The `deploy-router` op runs it
as its final confirm step, and `deploy/systemd/basecradle-router-drift.{service,timer}` run it **hourly** as
the `router` user — so a merge that never reached the box surfaces in `systemctl --failed` and the journal,
instead of going unnoticed for a day. It only reads; it never auto-deploys.

### Why the box pulls, not a token on the box and not push-CD
The box holds the fleet's crown jewels, so it carries **no GitHub credential** — and the deploy is a **box
pull**, not a push. Auto-deploy-on-merge from a GitHub-hosted runner would need either an SSH key to the box
in CI secrets or the box's SSH opened to GitHub's shared runner ranges; a self-hosted runner means GitHub's
runner agent executing workflow code *on* the box that holds every agent's credentials. Both regress the
box's governing constraint ("least privilege everywhere"). The chosen model — the NOC's `deploy-router` op
where the **box pulls a validated `main` SHA anonymously from the public repo** and runs the self-verifying
DoD loop on-box with rollback, plus a **drift alarm** that makes the merge≠deploy gap loud — closes the
silent-drift root cause (#54) with no read token to leak anywhere. Only the box pulling by content-addressed
SHA bounds a compromised deployer to code the offline gate already confirmed is on branch-protected `main`;
rsync, a NOC-shipped tarball, or a git bundle would each let a leaked key ship arbitrary root code.

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
> Since issue #71, the deploy **installs all of these unit files from the deployed tree on every run**
> (the NOC's `deploy-router` op globs `deploy/systemd/*.{service,timer}`), then `daemon-reload`, **arms
> every timer** (`enable --now`), and keeps `basecradle-router.service` enabled. It does **not** enable
> `*.service` units generically — the recovery gate below is enabled **once at provisioning** and its
> `enable` symlink persists across a file reinstall (see Part 3's contract note). The `enable` commands
> below are the explicit contract / first-time-by-hand step; only the timers and the daemon are re-armed
> per deploy.

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
recovery gate confirms it came back (or alarms). It pairs with the hardening duty
(`unattended-upgrades` — the install half; this is the reboot half). The manual fallback still works
(`systemctl start basecradle-router-reboot.service`, or `reboot-if-required.sh` by hand) for an
out-of-window reboot.

---

## Part 5 — Telemetry (Better Stack Vector: host metrics + scrubbed journald)

Out-of-band liveness (the capital's `/up` monitor) answers *"is the box alive?"*; **telemetry**
answers *"what is the box and the daemon actually doing?"* — host metrics (CPU / memory / disk /
load / network) and the journald stream (system logs **plus** the router's own software: the
`basecradle-router.service` uvicorn daemon and the drift/reboot/recovery units, which log to journald
via stdout/stderr). It is the Better Stack **Vector** agent, shipping both to Telemetry Source
**"AI"** (ingest host `s2531770.eu-fsn-3.betterstackdata.com`, EU `eu-fsn-3`). This mirrors the
proven, scrubbed config `basecradle-noc` already runs (basecradle-noc#31/#33).

> **⚠️ SECURITY — never ship raw journald to an external store (basecradle-noc#33 / basecradle#338).**
> An unscrubbed install on the NOC box shipped the *entire* journal and leaked three live secrets:
> the source ingest token (~40× — the generated config embeds the token in every component id, and
> Vector logs component ids constantly), plus a GitHub App token and a heartbeat URL (both from a
> `sudo` command line — **PAM logs the full argv**). On this box the router escalates every wake via
> `sudo -> wake-runner`, so the same `sudo` argv path is live here. The scrub below closes the class
> and is **mandatory before telemetry is enabled** (basecradle#338 class guard).

**The config is version-controlled — [`deploy/vector.yaml`](vector.yaml) is the single source of
truth**, not Better Stack's generated kitchen-sink. It is tailored to this box (a webhook daemon +
Caddy, no database) and has three load-bearing properties:

1. **`journald` → `ai_scrub` → logs sink.** The `ai_scrub` remap **drops** whole events from `sudo`
   (the argv leak path) and from Vector itself (the self-logged-token path), and **redacts**
   secret-shaped patterns (`gh[a-z]_…`, `Bearer …`, `…/heartbeat/…`, `bc_uat_…`) as defense in
   depth. The drop + redaction rules are byte-faithful to NOC's `noc_scrub`; do not weaken them.
2. **`host_metrics` → metrics sink**, direct (CPU/mem/disk/load/network). The scrub guards the
   **journald** path only — by design, mirroring NOC. `host_metrics` events are numeric gauges with
   metric-name/host tags; they carry **no** `message`, no `sudo` argv, no `SYSLOG_IDENTIFIER` — there
   is nothing for the log-shaped `ai_scrub` remap to drop or redact, and routing them through it would
   *corrupt* them (it rewrites `.timestamp`→`.dt` and reads log-only fields). So "scrub before the
   sink" is meaningful only for logs; the secret-leak class (basecradle#338) lives entirely in the
   journald stream, and that is what `ai_scrub` gates.
3. **The ingest token is not in the YAML** — `${BETTERSTACK_AI_SOURCE_TOKEN}` is interpolated from
   the chmod-640 `/etc/vector/betterstack.env`, supplied to `vector.service` by the systemd drop-in
   [`deploy/systemd/vector.service.d/10-ai-betterstack-env.conf`](systemd/vector.service.d/10-ai-betterstack-env.conf).
   The secret lives in exactly one place; a rotation is a one-line swap of that file + a restart.

**Never pass a secret as a command-line argument** (to `sudo`, `bash -s`, anything) — sudo/PAM logs
the whole command line to the journal, and on a telemetry box that ships it. Pass secrets via
**stdin (a piped heredoc), an env var, or a chmod-600 file**.

> **Division of labor (issue #116).** The router seat authors the version-controlled config here
> (this repo, merged to `main`); the **capital** does the on-box install, creates the token file, and
> live-verifies. The steps below are the capital's runbook — they are **not** part of the router-daemon
> deploy (the NOC's `deploy-router` op, which deploys only the router daemon).

### Install / update (capital, on-box)

```bash
# one-time: install Vector from the official .deb — NOT Better Stack's setup-script.
# Why the .deb and not `curl …/setup-vector/ubuntu/<TOKEN> | sudo bash`: that URL carries the source
# token as a path segment, so it lands in argv (sudo/PAM and process logs capture it — the
# secrets-in-argv class the constitution prohibits), and the script auto-starts Vector on a generated
# config that embeds the token in every component id (Vector logs component ids constantly — the exact
# path that self-leaked the NOC token ~40×). The .deb carries no token and avoids both. The timber.io
# apt repo is dead (Vector moved to Datadog), so pin the release from GitHub.
VECTOR_VERSION=0.56.0
curl -fsSL -o /tmp/vector.deb \
  "https://github.com/vectordotdev/vector/releases/download/v${VECTOR_VERSION}/vector_${VECTOR_VERSION}-1_amd64.deb"
sudo apt-get install -y /tmp/vector.deb && rm -f /tmp/vector.deb
# the .deb enables+starts Vector on its stock config — stop+disable until the scrubbed config is in place
sudo systemctl disable --now vector

# the token's only home (chmod 640 root:vector) — substitute the real token; never commit it.
# Pass it via stdin (this piped heredoc) or scp a pre-written file — never as a command argument.
sudo tee /etc/vector/betterstack.env >/dev/null <<'EOF'
BETTERSTACK_AI_SOURCE_TOKEN=<SOURCE_TOKEN>
EOF
sudo chown root:vector /etc/vector/betterstack.env && sudo chmod 640 /etc/vector/betterstack.env

# systemd drop-in so vector.service sees the token for ${…} interpolation (from the repo checkout)
sudo install -D -o root -g root -m 644 \
  deploy/systemd/vector.service.d/10-ai-betterstack-env.conf \
  /etc/systemd/system/vector.service.d/10-ai-betterstack-env.conf
sudo systemctl daemon-reload

# install the canonical scrubbed config (from the repo checkout), validate, then enable + start
sudo install -o root -g vector -m 640 deploy/vector.yaml /etc/vector/vector.yaml
sudo bash -c 'set -a; . /etc/vector/betterstack.env; vector validate /etc/vector/vector.yaml'
sudo systemctl enable --now vector
```

### Verify (the definition of done — capital)

In **Better Stack → Live Tail** for the **"AI"** source: (a) system/journald logs flow, (b) the
router's own daemon/unit logs flow, (c) host metrics populate the **"Host (Vector)"** dashboard
(source = AI) — and **zero secret values** anywhere (the scrub is the gate). To test the scrub
offline, put the `ai_scrub` VRL in a file and run `vector vrl -i <event.json> -p scrub.vrl -o`: a
synthetic `{"SYSLOG_IDENTIFIER":"sudo",…}` event must come back `aborted`, and a message carrying a
`ghs_…`/`…/heartbeat/…` token must come back `[REDACTED_…]`. Re-test after a reboot:
`systemctl status vector` active and Live Tail resumes.

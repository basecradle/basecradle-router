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
  app/deploy/bin/router-admin      # the admin CLI wrapper the NOC's converge + probes call
  bin/wake-runner                  # root-owned (root:root, 0755) privilege-drop wrapper
  bin/probe-ack                    # root-owned (root:root, 0755) synthetic-wake verifier (#208).
                                   #   Runs AS the agent (so it can read that agent's own
                                   #   NOC_PROBE_SECRET) but must NOT be agent-writable, or the
                                   #   account under test could rewrite its own verifier to
                                   #   always ack. Root-owned outside app/ for the same reason
                                   #   wake-runner is: a router-owned copy could be swapped for
                                   #   one that exfiltrates the agent env it runs inside.
/etc/basecradle-router/
  router.env                       # daemon config (owner router, 0600) — see below
  agents.json                      # the registry (BASECRADLE_ROUTER_AGENTS); root-owned, router
                                   #   read-only (0640) — it is the wake-runner's trusted allowlist
                                   #   so the daemon must not be able to write it; NO secrets
/var/lib/basecradle-router/        # systemd StateDirectory, owned by `router` (0755)
  evidence.json                    # what the router has demonstrably done — the NOC ledger's
                                   #   evidence source (0644, NO secrets). The DAEMON is its only
                                   #   writer; the admin CLI only ever reads it.
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

#### Reading the Router's Own Log (basecradle-router#170)

The daemon logs to stdout under `uv run uvicorn`, so until #170 journald stamped every one of its lines
`SYSLOG_IDENTIFIER=uv` — the router's log was attributed to its *launcher*. The unit now sets
`SyslogIdentifier=basecradle-router`, so the daemon is addressable the same way a wake is:

```bash
journalctl -t basecradle-router -n 100 --no-pager    # the daemon (as -t basecradle-wake-<slug> is one agent)
journalctl -u basecradle-router -f                   # by unit — also fine; the unit and identifier agree
```

**Every line is `key=value`.** The vocabulary, in the order one delivery produces it:

| Line | Carries |
|---|---|
| `event=startup …` | one INFO at boot: `version=`, **`sha=`** (the deployed commit, read from `/etc/basecradle-router/deployed-sha`), `routes=`, `dedup_ttl=`, `wake_attempts=`, `breaker_*=`. The running daemon *states its own config* — so a Live Tail that looks wrong is first checked here: absent (it never started), stale `sha=` (the merged≠live gap, #54), or thresholds you did not expect. |
| `event=delivery_decision …` | the route's ignore-vs-act call (#91): `source=`, `event_type=`, `decision=woke\|ignored`, `recipient=`, `delivery=`. |
| `stage=<s> outcome=<o> …` | one per pipeline stage: `route`, `verify`, `normalize`, `resolve`, `lock`, `dedup`, `wake_lock`, `breaker`, `wake`. **Every** stage carries **`source=<route>`** (below) — the fast half always did, and the slow half, the half that is *about a wake*, joined it in #222. |
| `event=wake_retry attempt=N/M …` | **WARNING** per transient wake failure that a retry follows. Before #170 the backoff was silent, so a flapping agent that eventually succeeded read as perfectly healthy. Carries `source=` too. |

**`delivery=<id>` is the join key.** Every line from `normalize` onward carries it (`route`/`verify` run
before the route has read the source's delivery header — the deliberate exception), and the wake child is
handed the *same* id in its environment as **`BASECRADLE_DELIVERY_ID`** (`wake-runner --delivery <id>`,
exported after the privilege drop). So one grep spans both identifiers — the router's half of a wake and
the agent's — which are otherwise two unrelated journals:

```bash
journalctl -t basecradle-router -t basecradle-wake-basecradle-glm-ai --since -1h --no-pager \
  | grep 0192f3a4-5b6c-7d8e-9f01-00000000000a     # one delivery's whole trip, both halves
```

The wake completion line is the one that matters most, and it now identifies the wake fully:

```
stage=wake outcome=ok source=github agent=basecradle-glm-ai delivery=0192f3a4-… exit=0 duration=23.1s
```

`agent=` is the OS-user slug — the same slug the wake's own entries are tagged with
(`basecradle-wake-<slug>`), which is what makes the two joinable. `duration=` is the wake subprocess's
wall-clock (the *last* attempt's, on every line that reports one — never the retry backoff's).

#### `source=<route>` — the wake-origin label (basecradle-router#222, a founder order)

**The contract, in one line:** **every** log line the router emits — both halves of the pipeline, and the
routes layer's own `event=delivery_decision` — carries the key **`source`**, whose value is the route the
delivery arrived on. Its value set is closed and is exactly the router's **enabled route names**: today
**`github`**, **`basecradle`**, and **`probe`**. `source=probe` is the fleet's own wake-edge lever (`probe
wake`, fired on the NOC's schedule — a cadence that repo owns); every other value is real traffic, a
delivery a source outside the fleet actually sent. There is no absence: a pipeline line carrying no
`source=` is a defect.

**Why it exists.** The router's probe traverses the real path on purpose, so a probe wake lands on the
same `stage=wake` line a real handoff does — and that line is what a log-metric extractor lifts
`wake_duration_s` (and every wake-rate counter) from. Extraction lifts only *low-cardinality* labels, so
the probe's one previous marker — a `probe-` prefix typed into the high-cardinality `delivery=` id — was
dropped on the floor, and every chart and alert built on the metric silently mixed the fleet's own test
traffic with its real work. The routes-layer line (`event=delivery_decision source=probe …`) knew, but it
is a different line and not the one the metrics ride. **The fix moves the fact rather than inventing one:**
`source=` is the router's own existing vocabulary — `Event.source`, which a route sets to its own `name`
— carried through from the delivery decision onto the half that had dropped it.

**Which lines carry it.** All of them. The pipeline's **fast half** (`route`, `verify`, `normalize`,
`resolve`) and the routes layer have carried `source=` since #91/#170; #222 added the **slow half** — the
half that is about a wake, from the moment the agent is known:

| Line | What it records |
|---|---|
| `stage=lock outcome=ok` | the per-agent serialization guard was taken |
| `stage=dedup outcome=ignored` | a duplicate delivery collapsed |
| `stage=wake_lock outcome=ignored` | the NOC freeze interlock refused it |
| `stage=breaker outcome=ignored` | the wake-rate breaker refused it |
| `event=wake_retry attempt=N/M` | a transient failure a retry followed |
| `stage=wake outcome=ok` | **the per-wake duration metric line** |
| `stage=wake outcome=failed` | the wake-failure line |

A refused probe and a failed probe pollute a wake-failure count exactly as a successful one pollutes a
duration chart, so the key rides on all of them — not only the happy one. It sits beside `agent=` rather
than being spelled onto each line, so a gate added later carries it by construction.

The agent-side journal (`basecradle-wake-<slug>`) needs nothing: a probe wake is answered by `wake-runner
--probe`, which returns its verdict to the caller and never routes through `systemd-cat`, so no probe
traffic reaches that identifier at all.

**Reading and filtering it:**

```bash
# Real wakes only — what an Agent Operations dashboard should ever show. A BLOCK-list
# (hide the synthetic route) rather than an allow-list: broken labelling then floods the
# view with probes instead of emptying it silently, which is the loud fail-direction.
journalctl -t basecradle-router --since -24h --no-pager | grep 'stage=wake ' | grep -v 'source=probe'

# The fleet's own probe traffic, on its own.
journalctl -t basecradle-router --since -24h --no-pager | grep 'source=probe'
```

**Why `source` and not `synthetic=true|false`.** `synthetic=` shipped in #220 and was retired the next day
by founder order: *"in an AI fleet, everything is synthetic — it answers no question."* Three reasons the
replacement is the router's own word rather than a new one. (1) It is **already there** — the same string
the fast half logs, the routes layer logs, and the evidence store records as `route`, so one key spans a
delivery's whole trip and no two surfaces can drift. (2) It **says which** source, not merely *some* class:
a second synthetic route landing later needs no new vocabulary and no third value, only its own name. (3)
`origin` is already taken in the core: an `Event.origin` is the issue the woken agent reports back on —
which is why the key is `source`, matching what the rest of the router already calls it.

The **evidence document keeps** its per-outcome `synthetic` flag (and the `wake-edge:synthetic:<route>`
claim keeps its id) — that is a different surface with a different problem: it is read long after the
fact, when the registry can no longer be asked which routes were manufactured, so the answer must be
snapshotted at write time. A log line is read in place beside its `source=` and needs no such snapshot.

**For consumers (the NOC's extraction, `basecradle-noc#473`):** filter out `source=probe` for the
production view. Do not filter on any prefix inside `delivery=` — there is none any more, by order: a
delivery id identifies one delivery and never types it. `delivery=` stays the high-cardinality join key,
and the probe now mints a bare `<32-hex>` rather than `probe-<32-hex>`.

**`GET /up` is not logged.** Better Stack's uptime monitor probes it about once a minute, forever; those
access lines were the largest single source of volume in the journal and in Live Tail, and pure noise. A
filter on uvicorn's access logger drops exactly that path and nothing else (`/upload` still logs, and so
does every webhook). If uvicorn ever changes its access-record shape the filter *keeps* the record — the
fail-direction for a log filter is to log too much, never to silently swallow.

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
# The synthetic wake (issue #208) — add "probe" to enable the router's own lever for the
# wake-edge claims. A route with no secret does not boot, deliberately: a probe is a
# genuine signed delivery, not a back door into the middle of the path. Its injection
# point is loopback-only (the Caddyfile 404s /webhooks/probe from the internet):
# BASECRADLE_ROUTER_ENABLED_ROUTES=github,basecradle,probe
# BASECRADLE_ROUTER_PROBE_WEBHOOK_SECRET=<generated for this box; the NOC holds the other copy>
# BASECRADLE_ROUTER_SELF_URL=http://127.0.0.1:8000   # optional; must be the address uvicorn binds
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
#
# Wake concurrency (the per-agent-fair scheduler's pool size, issue #182) — optional,
# fixed default (8), NOT derived from cpu_count. A busy agent holds exactly one lane, so
# this binds only when that many DISTINCT agents wake at once; size it to the box's memory:
# BASECRADLE_ROUTER_WAKE_LANES=8                 # max concurrent wakes across all agents; must be >= 1
#
# Green-while-absent instrument (issue #198) — both optional, both stated in the startup
# banner so a live daemon says which it booted with:
# BASECRADLE_ROUTER_WAKE_LOCK_DIR=/run/basecradle-noc/wake-locks   # the NOC freeze surface
# BASECRADLE_ROUTER_EVIDENCE_FILE=/var/lib/basecradle-router/evidence.json  # "none" = memory only
```

`BASECRADLE_ROUTER_WAKE_LOCK_DIR` is the NOC wake-lock (freeze) directory the daemon reads. It
defaults to the capital-pinned path and should stay there — it is settable because **a router
reading a different directory than the NOC writes to has a freeze that silently never fires**, and
a hard-coded constant cannot be compared against the NOC's. Now it can: the banner logs it and
`selftest freeze` reports it.

`BASECRADLE_ROUTER_EVIDENCE_FILE` is where the daemon records what it has demonstrably done. Leave
it at the default on the box — the literal value `none` disables persistence and is for a laptop
run only; on the box it would make every proven capability read as never-proven after each deploy.

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

The **wake scheduler** (issue #182) decides *which* pending wake runs *when*. Each accepted webhook is
fast-acked and its slow wake handed to a per-agent-fair thread pool sized by `BASECRADLE_ROUTER_WAKE_LANES`
(default 8). It runs **at most one wake in flight per agent** — serialising an agent's stream by
*scheduling*, so a worker thread never blocks waiting on the per-agent lock — and dispatches **fairly**
across agents, so one busy agent's deep backlog can no longer starve an idle agent's wake (the Fleet
Transport incident, 2026-07-17, where a chatty two-peer timeline starved the NOC's own transport probe).
`WAKE_LANES` is the ceiling on *total* concurrent wakes across all agents, deliberately **not** derived
from the box's `cpu_count` (that implicit sizing was half the incident); because a busy agent holds
exactly one lane, it binds only when that many *distinct* agents wake at once. Size it to the box's
memory. The daemon logs the saturated↔not-saturated edge, so "raise `WAKE_LANES`" is a signal the NOC can
read off the box; the startup banner states the live `wake_lanes=` it booted with.

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

### Proving what the router claims — the NOC's ledger interface (#198)

Fleet observability catches failures that **happen**. The night of 2026-07-26→27 produced five
failures where nothing happened at all — a capability was silently **absent**, and absence emits no
signal (`basecradle/basecradle#460`). Three of those classes are the router's surface, so the daemon
now emits **claims** and records **evidence** for the NOC's claims-vs-evidence ledger
(`basecradle-noc#406`). The router emits; the NOC judges — we never grade our own homework.

**The admin CLI is the whole interface**, reached through one wrapper the deploy installs. It does
the privilege drop and sources `router.env` itself, so the NOC schedules one stable path:

```bash
/opt/basecradle-router/app/deploy/bin/router-admin claims                 # Contract v1 manifests (JSON array)
/opt/basecradle-router/app/deploy/bin/router-admin claims --out-dir DIR   # one single-subject file per subject
/opt/basecradle-router/app/deploy/bin/router-admin selftest freeze --json # the freeze-readability probe
/opt/basecradle-router/app/deploy/bin/router-admin evidence               # the raw evidence document

# the synthetic wake — marker on STDIN, minted by the NOC with that agent's own secret
<mint> | /opt/basecradle-router/app/deploy/bin/router-admin probe wake --agent <slug> --json
```

**Both claims surfaces are ratified, and they are not two spellings of one thing** (`basecradle-noc#408`,
ruling 1): the **array on stdout** is what `provision-claims` reads per subject; the **`--out-dir`
directory** is what the census walks. The filenames in that directory are a *constraint*, not a
convention — `run-claim-probe` resolves `$CLAIMS_DIR/<component>@<os_user>.json` before it will run
anything, so a file spelled any other way is a claim that can never be proven:

| Subject | Filename |
|---|---|
| `box:<host>` | `basecradle-router.json` — no host in the name: one box gets one box-manifest per component, and a second spelling of a fact the body already carries is a thing that can later disagree with it |
| `agent:<slug>` | `basecradle-router@<slug>.json`, where `<slug>` is the agent's OS user |

> **Box-subject claims wait on `basecradle-noc#409`.** `provision-claims`/`run-claim-probe` are
> agent-subject-only today — a probe run as any user other than the component's own daemon proves the
> wrong principal, the same reasoning as this probe's own `ran_as_root → degraded`. The emitter writes
> the box manifest regardless; just don't expect it to be *armed* before #409. The agent-subject
> `wake-edge:*` claims are unaffected (evidence-kind, NOC-resolved).

**Run the probe as the daemon's user — the wrapper does this for you.** Root bypasses file
permissions, so a probe run as root would pass on a box where the daemon itself is locked out, which
is exactly the failure it exists to catch. Run as root the wrapper re-execs itself via `runuser`; run
as `router` it proceeds directly. The probe reports its own effective user (`ran_as`), so a drop that
silently failed is still visible in the output.

**Exit codes** (the contract the NOC schedules against — pinned by the contract owner in
`basecradle-noc#408`, ruling 4). The ledger reads exactly three things:

| Code | Ledger row | Meaning |
|---|---|---|
| `0` | **PASS** | **proven** — the surface is readable and would be honoured. The only thing recorded as evidence. |
| `75` | **ERROR / unprovable** | **could not prove** — *we never got an answer.* `EX_TEMPFAIL`, the same sysexits vocabulary as the op family's `64`. The lock dir does not exist, a lock is stale, the probe ran as root, or the router's own config would not load. |
| any other non-zero (`1` here) | **FAIL** | **proven broken** — *we asked; the answer is no.* Unreadable or malformed; the check names the exact file. |

**There is no warning tier, and `75` is not the quiet one.** *Cannot prove* is red and immediate; it is
distinguished from FAIL by its name and its ledger row, never by being softer — a muffled "could not
run" tier is the silent-death shape this whole program exists to kill. What still holds is that the two
reds stay **distinct**: a fresh box must never look identical to a box whose freeze surface is
genuinely unreadable.

`75` is shared by the probe's `degraded` verdict and by a config the CLI could not load, because from
the ledger's side those are one state. **The distinction rides on `stderr`** — one line naming the
surface and the cause, which the NOC forwards (bounded tail) on any non-proven verdict. A green probe
writes nothing there.

**The wake-lock directory is created at converge** (`basecradle-noc#408`, ruling 3), so the probe reads
`ok` on an idle converged box and `degraded`/`dir_absent` stays a genuinely abnormal signal. `/run` is
tmpfs, so between a reboot and the first converge the directory can still be absent; the boot-time
guarantee is a root-owned `tmpfiles.d` fragment riding `basecradle-noc#409` — NOC-side, nothing for
this repo to do.

**The five claims and what each closes:**

| Claim | Subject | Class / TTL | Closes |
|---|---|---|---|
| `wake-edge:webhook-route` | `agent:<slug>` | `rare` / 168 h | **A parked builder with no re-wake path.** `detail.edges` lists every path that could wake the agent now (an armed webhook route, a queued wake); `evidence` is its last `stage=wake outcome=ok`. `edge_count: 0` **and** `evidence: null` is the gap, in one row. |
| `wake-edge:webhook-route:<route>` | `agent:<slug>` | `rare` / 168 h | **An integration armed on paper, read per *recipient*.** One row per **armed** `(agent, route)` pair — the granularity instance 5 is actually asked at, and the rows the NOC's eight hand-written `basecradle-platform@*` rows retire in favour of (`basecradle-noc#417`). A route that is registered but not enabled gets no row: a claim states a capability the router has *now*. |
| `wake-edge:synthetic:<route>` | `agent:<slug>` | `rare` / 168 h | **An evidence claim with no lever.** The two rows above go green only when something *happens*, and for a deliberately quiet agent nothing ever does. This row is the lever — the router's own synthetic wake — and it is proven by the *same* `last_ok_at` a real wake writes, never by the probe's own exit code. Emitted per **armed synthetic** route; deliberately **not** counted as a wake edge (see below). |
| `freeze-surface:readable` | `box:<host>` | `rare` / 24 h | **The control that existed but could not be read.** A `probe` claim, not a pointer: readability is not a fact you look up, it is one you demonstrate with the daemon's own credentials. |
| `delivery-sink:<route>` | `box:<host>` | `rare` / 168 h | **An integration armed on paper.** `accepted=0 rejected=417` is a mismatched secret; `accepted=0 rejected=0` is a sink nobody has used. `accepted` counts *signature verification passing*, which is what proves the secret on this box matches the source's. `detail.synthetic` marks the probe's own sink, so the fleet probing itself never reads as an external integration being armed. |

**Every pointer the emitter declares resolves from the claim's own `detail`.** An `evidence`-kind
claim's `prove.source` is a `<path>#<dotted.field>` pointer, and the NOC resolves it from the
manifest the census returns, **never by reading the file** — it has no shell on this box and no
wrapper op reads `/var/lib`, so the census is the transport. The rule is one line: *the pointer's
last segment is the field, and `detail` is the object it lives in* (`basecradle-noc#409`). So each
claim's `detail` is the emitter's **flat projection of the exact sub-object its own pointer walks
into**, with the descriptive keys beside those fields rather than wrapped around them:

| Claim | Pointer | Resolves from `detail` |
|---|---|---|
| `wake-edge:webhook-route` | `…#agent_wakes.<slug>.last_ok_at` | `last_ok_at` |
| `wake-edge:webhook-route:<route>` | `…#agent_wakes.<slug>.by_route.<route>.last_ok_at` | `last_ok_at` |
| `wake-edge:synthetic:<route>` | `…#agent_wakes.<slug>.by_route.<route>.last_ok_at` | `last_ok_at` |
| `delivery-sink:<route>` | `…#delivery_sinks.<route>.last_accepted_at` | `last_accepted_at` |

A pointer the rule cannot land on is refused with a named reason and reads `unprovable` — loud and
never green, but also **never armed**, which is a capability nobody is watching dressed as one that
is. `tests/test_claims.py::test_every_declared_evidence_pointer_resolves_from_its_own_detail` re-runs
the NOC's rule over every emitted claim so a new claim cannot ship with a pointer that misses. A
resolved **`null` is not a miss** — it is the answer: the field exists, the emitter publishes it, and
nothing has ever landed in it.

Each claim also carries a `detail` object beside Contract v1's pinned
`claim`/`class`/`prove`/`evidence`/`ttl_hours` keys — the one additive extension, because the emitter
must report not just whether an edge ever fired but whether one *exists*. **Ratified as an optional
additive key** (`basecradle-noc#408`, ruling 2): the NOC parses it, validates that it is an object and
nothing more, and round-trips it, but never persists it to the ledger — it is for the reader and the
operator. `"evidence": null` is explicitly legal. A consumer reading only the pinned keys is unaffected.

**Instance 5 is asked per *recipient*, so the wake proof is kept per `(agent, route)`.** A route-wide
accept counter says the sink works for *somebody*, and an agent-wide "last woken" says *something*
reached the agent — neither answers *"can this route reach this agent?"*, and each greens the other's
blind spot: one healthy recipient covers for six dead ones, and a github wake covers for a basecradle
integration that 401s every delivery to the same agent. So every armed `(agent, route)` pair gets its
**own claim row** (`wake-edge:webhook-route:<route>`, armable and proven on its own evidence), every
`webhook-route` edge in `detail.edges` carries the same `last_ok_at`/`last_ok_delivery` for the
operator's one-row view, and `detail.by_route` carries the full record — including routes no longer
armed, which both of the others by definition drop. An armed edge with `last_ok_at: null` beside a
sink counting hundreds of rejections is instance 5, for that one agent, in one row.

**The four wake outcomes, and why they are four counters and not two** (`#218`). Every wake this
router dispatches ends in exactly one of these, published on both the agent-wide row and each
`(agent, route)` row, with a matching `last_*` group:

| Counter | What it means | What a consumer should do with it |
|---|---|---|
| `ok` | the wake fired and succeeded | the only thing that proves the edge; `last_ok_at` is what every wake-edge claim points at |
| `failed` | the wake was dispatched and the wake path broke | **loud** — the edge is broken for this agent, with the reason |
| `refused` | a **gate** declined a wake that would otherwise have run: a live NOC converge freeze, or a tripped wake-rate breaker | the router working correctly and *suppressing* a wake — an agent whose history is all refusals is **gated, not unreachable** |
| `deduped` | a duplicate delivery was collapsed into the wake that already ran for it | the router working correctly *because a wake already succeeded* — never a finding |

A **genuine rejection of a delivery** — a bad signature, a malformed payload, an untrusted sender —
never reaches any of these. It is counted at the sink, as `delivery_sinks.<route>.rejected`, because
it is refused at `verify`/`normalize` before an agent is ever resolved.

`deduped` was split out of `refused` in `#218`, and the reason is sharper than tidiness: **a collapse
is the one outcome only a success can produce.** The dedup cache is marked *after* a wake has fired
and succeeded, so a `duplicate_delivery` is a downstream consequence of an `ok` recorded within the
cache's TTL — whereas every other refusal means a wake that should have run did not. Sharing one
counter published `ok=4 failed=0 refused=2` on both builders' `github` rows, the newest refusal 2.6 ms
after the newest success and both of them dedups: a consumer reading *the newest recorded attempt was
refused* saw a route in trouble that had rejected nothing (`basecradle-noc#462`). **The counter is the
classification** — deliberately, because telling a benign collapse from a real refusal by parsing this
repo's `reason` strings would put a second spelling of our contract in the NOC's repo, which its own
rulings forbid (`basecradle-noc#344`/`#366`).

Rows written before `#218` are **reclassified on load — by the daemon and by every reader**, so the
emitted claim is correct on the first converge after the deploy rather than after the daemon's next
flush (the CLI and the claims emitter stay strictly read-only; only the daemon rewrites the file). A
trailing
`last_refused_reason: duplicate_delivery` moves to `last_deduped_*` and one count moves with it
(`refused + deduped` is conserved). The document is durable on purpose, so the misreading is durable
too — `last_refused_at` moves only on a genuine refusal, and the whole point is that one has not
happened. Bumping `EVIDENCE_VERSION` would have cleared it and reset **every** age-of-proof on the box
to never-proven, which is precisely what `/var/lib` was chosen to prevent. History cannot be split
further (nothing recorded the mix), so a row that had several refusals can read `refused >= 1` beside
a null `last_refused_at`: *there were refusals, and the most recent thing we filed as one turned out
not to be.* That understates refusals, which is this store's standing fail-direction.

**The evidence document** (`/var/lib/basecradle-router/evidence.json`, `0644`, no secrets) is what the
daemon writes and the emitter reads — they are different processes, so a file is the only channel.
The unit's `StateDirectory=basecradle-router` creates the parent owned by `router` before the daemon
starts. **Only the daemon writes it**: the CLI is strictly read-only, so a probe run under the wrong
identity can never take ownership of the daemon's own state file away from it.

The **boot check** runs the same probe at daemon startup and logs it loudly
(`event=freeze_selftest status=…`) — but it never aborts startup. That is deliberate: the wake-lock
guard's fail-direction is to keep waking when a lock cannot be read (wedging every wake on a
permissions typo would be far worse), so a boot check that refused to start would silently invert
that decision. **Fail-closed is the converge's job** — the NOC runs this probe at Layer 1 and turns
the converge red. Loud here, closed there.

### The synthetic wake — giving the wake-edge claim a lever (#208)

The two `wake-edge:*` rows above are `evidence`-kind: they report the last time a wake demonstrably
happened. For an agent nobody happens to address, nothing ever does, and *exercising* an
`evidence` claim only re-reads its pointer — it cannot cause a wake. So a healthy edge and a
permanently dead one are indistinguishable from outside, and the only remedy left was a social one
(*"go message @pinky"*). Shared law closes that door: **a monitor never depends on a consent or trust
surface**, and the system is never widened to make a monitor go green (`constitution.md` →
Operational Baselines). The NOC's timeline-based prober is retired for every agent, @jt included
(`basecradle-noc#421`). **The router owns the router→agent edge, so the router proves it.**

**How it works, end to end.**

1. The **NOC** mints a marker with *that agent's own* probe secret (`basecradle-noc#424`,
   `mint-probe-secret`, one `0600` file per slug) and pipes it to the router's CLI on **stdin**.
2. The **CLI**, running as the daemon's user, signs a probe body with **this box's `probe` route
   secret** and POSTs it at the daemon's own **loopback** listener.
3. The **daemon** treats it as the genuine delivery it is: HTTP front end → HMAC `verify` →
   `normalize` → resolve → per-agent lock → dedup → **NOC wake-lock** → **wake-rate breaker** → wake.
4. The **wake-runner** does everything a real wake does — validate against the root-owned registry,
   resolve the exact binary this agent's kind sanctions, `runuser` to the agent, load its `agent.env`,
   enter its clone — then execs the root-owned **`probe-ack`** verifier *as the agent*, which checks
   the marker against that agent's own `NOC_PROBE_SECRET` and prints `BCNOC1-ACK <nonce>`.
5. The daemon records `stage=wake outcome=ok`, so **`last_ok_at` moves** — the one fact only the
   router can honestly state, and the thing the ledger reads. That line, like every line of the wake
   half, says `source=probe` (see *`source=<route>`* above), so the probe traffic this proof depends
   on stays out of every production metric built on the same line.

**Two secrets, two jobs, and neither is a trust edge.** The route secret authorises *injection into
the router*; the agent's own secret proves the wake *arrived in that agent's context*. The router
holds no agent secret and verifies nothing — which is exactly why a pass means something. Verification
happens after the privilege drop, against a file only that agent can read, so a green probe says *the
wake reached this account with this account's credentials loaded*. No platform account, no timeline,
no relationship with anyone.

**Token-free is structural, not a promise.** `claude -p "<marker>"` would *be* a model call, so there
is no code path that can build a model command for a synthetic event — `wake_command` raises, and the
wrapper's `--probe` mode is mutually exclusive with a command after `--`. An agent with **no**
`NOC_PROBE_SECRET` armed is a **refusal** (exit `75`) with nothing launched; there is deliberately no
fallback from an unarmed agent to a real wake.

**What it proves, and what it stops before** — stated on the claim itself (`detail.proves`,
`detail.stops_before`) so the NOC judges knowing it. It proves the whole router path plus the sudo
boundary, the registry pin, the privilege drop, the agent's own env, its clone, and its probe secret.
It stops one step short of `exec`-ing the model binary — the single step the zero-token-at-rest
constraint forbids. It does **not** prove the model binary *runs*; a wake that fails there is a
failure that *happens*, which ordinary telemetry already catches (`failed` climbs, with the reason).

**A probe is a lever, never an edge.** Nothing in the world will wake an agent through the router's
own probe, so it is excluded from `detail.edges` and `edge_count`. Counting it would put
`edge_count: 1` on a builder no event can reach and quietly retire the parked-builder finding — the
instrument defeating itself. A parked builder correctly reads `edge_count: 0` **with**
`last_ok_synthetic: true`: the terminus answers, and nothing in the world will ever address it.

**A synthetic never masquerades as real traffic.** It lands in its own `delivery_sinks.probe` and its
own `agent_wakes.<slug>.by_route.probe`, and every `last_ok_*`/`last_failed_*`/`last_refused_*`/
`last_deduped_*` group records its `route` and a `synthetic` flag *at write time* — not derived at
read time, where a route since disabled would silently answer "real".

**Enabling it** (NOC-side config; the daemon will not boot on a route with no secret):

```bash
BASECRADLE_ROUTER_ENABLED_ROUTES=github,basecradle,probe
BASECRADLE_ROUTER_PROBE_WEBHOOK_SECRET=<generated for this box; the NOC holds the other copy>
# optional; must stay a loopback address the daemon actually binds:
# BASECRADLE_ROUTER_SELF_URL=http://127.0.0.1:8000
```

Install `deploy/bin/probe-ack` root-owned beside the wrapper — it runs *as* the agent but must not be
*writable* by it, or the account under test could rewrite its own verifier to always ack:

```bash
install -o root -g root -m 0755 deploy/bin/probe-ack /opt/basecradle-router/bin/
```

**Exit codes** are the same three readings as every probe here: `0` proven (the daemon recorded a
successful wake for *this* delivery id), `1` proven broken (the injection was structurally refused, or
the wake was dispatched and failed — including a probe secret that has drifted between the NOC's copy
and the agent's), `75` could not prove (the route is not enabled, a gate *refused* the wake — a live
converge freeze is the common one — the injection was collapsed as a duplicate, or nothing was recorded
before the deadline). The refused-vs-failed split the evidence store already draws maps exactly onto
`unprovable`-vs-`broken`, and a collapsed duplicate reads the same way: no wake ran, so nothing was
proven either way.

**The injection point is not reachable from the internet.** The CLI posts over loopback, and the
Caddyfile answers `/webhooks/probe` with a `404` — the same answer an unknown route gives, so it
leaks nothing about whether the route exists.

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
  `Restart=on-failure`, `KillMode=mixed` + a generous `TimeoutStopSec` (30min) to let the app drain
  its (unbounded) in-flight wakes on shutdown, bound to localhost (Caddy fronts it).
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
- **The admin CLI wrapper** (`deploy/bin/router-admin`, #198) — the one stable path the NOC's converge
  and its Layer-3 synthetic-exercise scheduler call for the claims manifests and the freeze probe. It
  lives inside the `app/` tree (so the app-mirror keeps it in lockstep with `main`, no separate root
  install), re-execs itself as `router` via `runuser` when invoked as root, sources `router.env` after
  the drop, and prefers the venv's python over `uv run` so a scheduled probe never re-syncs the
  daemon's dependencies underneath it. It is **read-only**: it never writes the evidence document,
  so a probe run under the wrong identity cannot take that file's ownership from the daemon. Details
  and exit codes: *Proving what the router claims* in Part 1.

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
| `deploy/bin/probe-ack` | **NOT yet in the op's routine band — a one-time provisioning step (#208).** Install root-owned (`root:root`, `0755`) to `/opt/basecradle-router/bin/probe-ack`. Until it is there, `probe wake` reports `75` / *unprovable* naming the exact `install` command, and no other path is affected. Ideally the op's `wake-runner` line becomes a `deploy/bin/*` glob, so the next root-owned helper is not a provisioning change either. |
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
>
> **`deploy/bin/probe-ack` is the first case of that second clause** (#208): a new root-owned file outside
> `app/`, so it needs one provisioning install. Its absence fails *safe and loud* — the wrapper refuses with
> `75` naming the exact `install` command, and nothing else on the box changes — but it is worth turning the
> op's single `wake-runner` line into a `deploy/bin/*` glob so root-owned helpers join units in the
> handled-automatically column.

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
traffic right now behave. Synthetic, HMAC-signed webhooks at the live endpoint — three GitHub-shaped:

| Case | Webhook | Asserted | Proves |
|---|---|---|---|
| 1 | bad signature | **401** | the HMAC verify boundary holds |
| 2 | valid sig, **untrusted** sender | **400** | the #52 trusted-actor gate **rejects** strangers |
| 3 | valid sig, **trusted** sender, **unregistered** repo | **200** | the gate **admits** the fleet — and no agent is woken (resolve finds none) |

Cases 4–5 do the same for the `basecradle` route once the capital wires it (self-gated on the secret and
on the running daemon actually serving the route). Cases 6–7 pin where the **synthetic wake's** injection
point lives (#208):

| Case | Request | Asserted | Proves |
|---|---|---|---|
| 6 | `POST` the **public** `/webhooks/probe` | **404** | the injection point is **not reachable from the internet** — Caddy denies it |
| 7 | `POST` **loopback** `/webhooks/probe`, bad signature | **401** | the daemon serves it on-box, and the shared HMAC boundary rejects |

**Case 6 is asserted unconditionally**, unlike every other route case, and that asymmetry is the point: the
probe route can fire a wake at *any* registered agent, so its reachability from the internet must never
quietly become true — not when the route is disabled, not when a Caddyfile is re-templated. Neither case
carries a valid signature, so nothing is normalized and no agent is ever woken.

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

#### The gap is classified, not just measured (issue #189)
**Differing SHAs are not by themselves drift.** This repo carries every one of the fleet's
verbatim-shared artifacts, so every fleet-wide re-sync — routine, expected, frequent — lands a
**docs-only** commit on `main` that changes nothing the box runs. Reddening a production alarm on those
trains the operator to read red as "probably just docs again", which is precisely how the one genuine
stale-daemon alarm gets waved through. An alarm that cries wolf on a schedule converts a *hard* signal
into a *judgment call*.

So when the SHAs differ, the check fetches **just those two commits** (`--filter=blob:none --depth=1`,
a few hundred KB, still tokenless, into a throwaway dir) and asks *which files* differ:

| Gap contains | Result |
|---|---|
| any **daemon-relevant** path | **red, exit 1** — `DRIFT`, naming the offending files. Unchanged behaviour. |
| only **inert** paths (`*.md`, `.claude/**`, `.github/**`, `.gitignore`) | **green, exit 0** — `IN SYNC (daemon)`, stating out loud that main is ahead by docs only and naming those files. |
| nothing classifiable (network down, deployed SHA no longer on the remote) | **red, exit 1** — an unclassifiable gap is drift. |

Two properties keep the tolerance from becoming a **false negative**, which would be the worse failure —
a hidden stale wake daemon:

- **Fail-closed by construction.** The inert set is a narrow explicit allow-list; *everything* else is
  daemon-relevant, so a newly-added directory, config file, or unit is loud without anyone remembering
  to classify it. `.claude/**` is inert on the box because a woken agent runs in its own clone under
  `/home/<agent>` — `wake-runner` refuses any cwd outside it — never in `/opt/basecradle-router/app`.
  `tests/` and every non-`.md` file under `deploy/` are deliberately **not** inert.
- **Pinned to the real tree by an offline test.** The path model is a second model of what a deploy
  ships and could drift from it, so `tests/test_drift_check.py` runs the shipped classifier over every
  path `git ls-files` reports and every path the Part 3 contract table consumes — globbed from
  `deploy/systemd/`, `deploy/bin/`, and `src/` rather than hand-listed, so a new unit file or module is
  covered automatically — and runs the whole script end to end against a local fixture repo.

**It never passes silently**: the green docs-only case prints both SHAs and the exact files in the gap,
because silence is what a broken checker looks like.

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
   (the argv leak path) and from Vector itself (the self-logged-token path), **deletes the
   `_CMDLINE` field from every event** (below), and **redacts**
   secret-shaped patterns (`gh[a-z]_…`, `Bearer …`, `…/heartbeat/…`, `bc_uat_…`, and **provider API
   keys** — `sk-…` covering the whole `sk-ant-`/`sk-proj-`/`sk-or-v1-` family, plus `xai-…`, `AIza…`,
   `hf_…`, `r8_…`) as defense in depth. The provider-key rules are the belt for the braces
   (basecradle-router#170): agents run with those keys in their env, and a wake's stdout+stderr now
   flows into journald (#168) and therefore through here — nothing is *known* to print one, and a
   traceback that did would otherwise ship it. The drop + redaction rules are byte-faithful to NOC's
   `noc_scrub` plus these additions; do not weaken them.
   **It also PREFIXES each shipped message with the emitting program's journald identifier**, so a
   Live Tail line reads `[basecradle-router] stage=wake outcome=ok …` or `[basecradle-wake-jt] step
   3/12 …` and says which program on the box emitted it (basecradle-router#170). Doing this in Vector,
   once, is what makes it *uniform*: every program gets it for free — sshd, Caddy, a wake, a timer —
   whereas hand-prefixing `[Router]` into the daemon's own log strings would cover only the daemon and
   would bake a presentation concern into application code. **Do not do that.** The prefix is applied
   *after* redaction (it is not secret-bearing and must never be scanned), and falls back to `_COMM`
   then `unknown`, so a kernel line with no identifier still reads `[kernel] …` rather than `[] …`.
   The prefix is only as good as the program's *name*, which is why `basecradle-router.service` sets
   `SyslogIdentifier=` — without it the daemon's lines would ship as `[uv] …`, its launcher's name.
2. **`host_metrics` → metrics sink**, direct (CPU/mem/disk/filesystem/load/host/network). The scrub
   guards the **journald** path only — by design, mirroring NOC. `host_metrics` events are numeric
   gauges with metric-name/host tags; they carry **no** `message`, no `sudo` argv, no
   `SYSLOG_IDENTIFIER` — there is nothing for the log-shaped `ai_scrub` remap to drop or redact, and
   routing them through it would *corrupt* them (it rewrites `.timestamp`→`.dt` and reads log-only
   fields). So "scrub before the sink" is meaningful only for logs; the secret-leak class
   (basecradle#338) lives entirely in the journald stream, and that is what `ai_scrub` gates.
   **It does not scrape what it cannot read** (basecradle#414): the `filesystem` collector stats every
   mount in `/proc/mounts`, and the unprivileged `vector` user cannot reach `/sys/kernel/debug/tracing`
   (tracefs, under a `0700` debugfs) — so every 30s scrape logged a `statvfs` permission-denied
   **ERROR**, ~2,880/day into this box's journal. That noise never left the box (`ai_scrub` drops
   Vector's own events, per #338), but a permanent error carpet in the crown-jewels box's journal is
   how an operator learns to skim past ERROR in an incident. `filesystem.filesystems.excludes:
   ["tracefs", "debugfs"]` fixes it: Vector checks the excludes **before** the `statvfs`, so the
   syscall is never attempted. Excluding by filesystem *type* rather than by mountpoint path is
   deliberate (tracefs is mounted at both `/sys/kernel/tracing` and `/sys/kernel/debug/tracing`, and
   the type is the durable fact). The collector itself **stays on** — a full disk is how this box dies,
   so disk-usage metrics are the point; an excludes-only list still collects every real filesystem.
3. **The ingest token is not in the YAML** — `${BETTERSTACK_AI_SOURCE_TOKEN}` is interpolated from
   the chmod-640 `/etc/vector/betterstack.env`, supplied to `vector.service` by the systemd drop-in
   [`deploy/systemd/vector.service.d/10-ai-betterstack-env.conf`](systemd/vector.service.d/10-ai-betterstack-env.conf).
   The secret lives in exactly one place; a rotation is a one-line swap of that file + a restart.

**Never pass a secret as a command-line argument** (to `sudo`, `bash -s`, anything) — sudo/PAM logs
the whole command line to the journal, and on a telemetry box that ships it. Pass secrets via
**stdin (a piped heredoc), an env var, or a chmod-600 file**.

> **`_CMDLINE` is deleted from every shipped event, and the rule is on the FIELD (basecradle-noc#443).**
> The paragraph above was in this README before the leak that proves it is not self-enforcing.
> journald stamps **every** event with the emitting process's full argv as the `_CMDLINE` *metadata
> field*, and Vector ships the whole journal record — so the `msg` redactions never see it, and the
> `sudo` whole-event drop only covers one identifier. `runuser` is not `sudo`: the NOC's
> `fleet-deploy-runner` handing each agent's entire `agent.env` to `runuser -u <agent> -- env -i
> KEY=VALUE …` put all seven harness agents' credentials in the log store, unredacted, continuously.
> `ai_scrub` now `del(."_CMDLINE")` **unconditionally** — guarding the field holds for every program
> on this box, including ones this repo has never heard of, whereas an allow-list of risky
> identifiers holds only until the next program puts a secret on its own argv and nothing announces
> it. Nothing readable is lost: `_COMM`/`_EXE` still name the binary and every line carries the
> `[identifier]` prefix. `tests/test_log_surface.py` pins both the deletion and its unconditionality.
>
> **The router's own launch paths were audited against this class and are clean**
> (basecradle-router#213): `wake-runner` passes **no** environment across the `sudo` boundary — the
> agent's `agent.env` is read *by the agent*, after the privilege drop, from a `0600` file it alone
> can read — and `python -m basecradle_router probe wake` takes its BCNOC1 marker on **stdin, never
> argv**. This scrub is the box-wide belt for every *other* program's braces.
>
> **The drift alarm cannot see this file, so merging it is not the end.** `drift-check.sh` compares
> the *deploy stamp* against `origin/main`, and `deploy-router` writes that stamp without touching
> Vector (the install below is the capital's, per the next note) — so a daemon deploy turns drift
> **green** while `/etc/vector/vector.yaml` is still the old bytes. Re-run the `install` line.

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

# CLAUDE.md

## What This Is

**basecradle-router** is the **fleet router** for [BaseCradle](https://basecradle.com) — a communications platform and AI research lab where **humans and AI are equal peers**. The router is the infrastructure that replaces the human courier and gives the fleet a home: a **modular webhook daemon**, running on the fleet's dedicated home server, that receives events from multiple sources and **wakes the right agent** to act on them.

When a handoff issue is filed on a repo, the router receives the webhook, resolves which agent owns it, and wakes that agent's headless Claude Code — as its own OS user, in its own repo clone, under its own identity — to do the work and report back. No human relay.

The router is itself built by human and AI contributors working as peers, under identical rules. **The authoritative requirements spec is `basecradle/basecradle#277`** — read it first; this charter is the procedures, that issue is the contract.

## The Constitution

This repository is built under the **BaseCradle Constitution** — the principles shared by every repository in the BaseCradle ecosystem. It lives in the **private core repository `basecradle/basecradle`** as `constitution.md` (default branch); it is repo-internal and never served publicly. Read it from GitHub with your fleet credentials — this works from any machine (laptop or fleet server), unlike a local checkout path:

```bash
gh api repos/basecradle/basecradle/contents/constitution.md -H "Accept: application/vnd.github.raw"
```

(or read a local checkout of `basecradle/basecradle` if you have one). Only fleet actors with core access can read it; outside contributors without core access work from the conventions in this file, which reflect the principles you need. This CLAUDE.md carries this repo's *procedures*; the constitution carries the *principles*; when they conflict, the constitution wins. **Read it before non-trivial work.**

## Architecture — core + routes

The router is a **small, source-agnostic core** plus **pluggable route modules**. This is the defining design property; protect it.

- **The core** owns the common pipeline, and knows nothing about any specific event source: **verify → normalize the event → resolve the target agent → wake it → concurrency/queue/retry → report.**
- **A route module** owns only one event source's specifics: how to receive its webhook, verify its signature, and normalize its payload into the core's vocabulary — a "wake agent X with trigger Y." Adding a source is **implementing one route module**, never forking the daemon. (Same ethos as harness's "add a provider = implement one protocol.")
- **Routes:** `github` (v0 — handoff issues), `basecradle` (next — platform events; this is the use case harness originally reserved `basecradle-router` for), and others later. Keep the core route-agnostic so a new route never touches it.

### The home-server model

The router runs on the fleet's **dedicated Ubuntu home server** — the one home where the agents live. On that box:

- **One OS user per agent**, isolated; each holds only its own credentials (GitHub App key, `ANTHROPIC_API_KEY`, and later its BaseCradle token), `chmod 600`, unreadable by siblings.
- The router **wakes an agent by running its headless Claude Code as that agent's OS user**, in that user's repo clone. The woken agent stays an **independent, separately-chartered context** — the router delivers a trigger; it never *becomes* the agent.
- The router runs as a `systemd` service. The box holds the fleet's crown jewels — **harden it; least privilege everywhere.**

### One harness instance per agent — the router is a per-agent serializing multiplexer

The constitution makes an agent's identity **unified across every input path** (`constitution.md` → "Sovereignty and Governance": *"Each agent acts under its own identity — and that identity is singular"*). Every channel that can address an agent — a GitHub event, a BaseCradle message, any future source — converges on **one identity-and-memory locus**: today, **one harness instance per agent**, running as that agent's own OS user, against its own home and memory. The harness *type* (Claude Code / basecradle-harness / cradle) is shared across agents; the per-agent *instance* is not. This is law, and it dictates the router's shape. The router has **many inputs and one terminus per agent**, so it is a **per-agent serializing multiplexer** — `N inputs → one harness instance per agent`:

- **An input routes to the agent's existing harness instance — never stands up a second.** `basecradle-python-ai` is wired `GitHub → Claude Code` today. When the `basecradle` input module ships, it routes to that **same** Claude Code instance (same OS user, same home, same memory) — it does **not** install a second harness for the same agent. Adding an input is wiring a new source onto an existing terminus, never forking the agent.
- **Inputs serialize into one ordered stream per agent.** A harness instance is a single-session-at-a-time runtime over one memory/transcript store; two live sessions writing it concurrently is corruption / split-brain — the exact failure the unified-identity rule exists to prevent. The router **must not** fan two inputs into two parallel wakes for one agent; it funnels them through one lock into a single ordered stream the lone instance drains. **The mechanism is the per-agent lock** ([`concurrency.AgentLocks`](src/basecradle_router/concurrency.py)), keyed by the agent's `harness_key` (its OS-user slug — the instance's identity), held across the wake in [`pipeline.Pipeline.execute`](src/basecradle_router/pipeline.py). It is keyed on the **agent**, deliberately **not** the repo: a repo is a GitHub-shaped notion a non-GitHub input need not carry, whereas every input resolves to an agent. (A richer queue/mailbox can replace the blocking lock when a real second source lands; the *key* is already correct.)
- **Unified memory, not unified conversation.** A GitHub PR thread and a BaseCradle Timeline are *different conversations sharing one memory + identity*. The router preserves each event's source/thread context so the harness can map each input to its own session while all draw on the one shared memory; the harness-side half (per-source session mapping + cross-session answerability) is filed on `basecradle-harness`.
- **Bijection.** One agent ↔ one harness instance. The router never points one agent's inputs at two instances, nor one instance at two agents. *Different capability per channel (heavy code work vs. light chat) is one harness with modes/tools — never two harnesses; different capability ≠ different brain.*

**When you add a route module, build it against this:** resolve the event to its agent and hand it to the same core pipeline — the per-agent lock makes its wakes serialize with every other source's automatically. Never give a route its own wake path or its own harness for an agent.

## v0 Scope

**In:** the `github` route end-to-end — a handoff issue (labeled `handoff`) → signed webhook → core wakes the target repo's agent → agent does the work, self-reviews, opens a PR, reports on the originating issue. Per-agent lock (no double-wakes — one harness instance per agent; see "One harness instance per agent" above). **Merge autonomy (Earned Autonomy)**: a captain's own PRs auto-merge on green CI — code, docs, charter alike — with **no per-PR human review** (a merge to `main` is reversible, so it needs no gate). The only firebreaks are at the *irreversible/outward* step (publish/deploy), enforced at the **platform** (a GitHub Environment approval) and only for agents that have not yet earned the trust to act unsupervised there — training wheels, retired as a captain matures (constitution → Earned Autonomy).

**Out (deferred, on purpose):** the `basecradle` route and any non-GitHub source; multi-host; auto-provisioning. Build the core + one route well, then add routes.

## Stack (omakase — matches harness + cradle)

| Concern | Choice | Notes |
|---|---|---|
| Python | **3.10+** | Matches harness/SDK; the agent-runtime side of the ecosystem is one language. |
| Toolchain | **uv** | venvs, deps, build — one tool. |
| Lint + format | **ruff** | CI enforces; no style debates. |
| Tests | **pytest** (+ **respx**) | Mock the webhook transport, the GitHub API, and the `claude` invocation at the boundary — tests never hit the network, a live agent, or a real model. |
| Types | hints + **py.typed** | Types are documentation. |

This is **not** a published package — the router is **deployed to the home server**, not installed as a library. No PyPI/release pipeline; "shipping" is deploying to the server (mechanism designed in the build).

## Conventions

- **Workflow**: branch → PR → CI green → squash-merge → delete the merged branch. Nobody pushes to `main`, human or AI. One concern per PR. PRs reference issues with `Closes #N` (but NOT for handoff/gated issues — see the shared block below).
- **Auto-merge is GitHub-native, not router-side (#38, decided).** Earned Autonomy says a captain's own green PR merges with no per-PR human review. The mechanism is **GitHub native auto-merge**: after self-review, enable it on your own PR with `gh pr merge --auto --squash` (under your own bot identity) and the platform merges the instant required checks pass. The router deliberately holds no GitHub credential, so it never merges — re-implementing "merge when green" router-side would have meant a standing merge-capable token on the crown-jewels box. Prerequisite per repo: branch protection with required status checks, **Allow auto-merge**, and **Automatically delete head branches** all enabled.
- **Each agent posts and commits under its own GitHub App bot identity** (`basecradle-router-ai[bot]` here). Bot-authored fleet commits carry **no `Co-Authored-By` trailer** — the author already is the agent.
- **Self-review before opening a PR**: a `[bot]`-authored PR runs CI in a restricted context where the review credential resolves empty, so automated `claude-review` is skipped on bot PRs. Run `/code-review` on your own diff and address findings **before** opening the PR.
- **Tests pin invariants and never hit the live network, a live model, or a live agent** — mock at the boundary.
- **Test data is fabricated, always**: the cast is **John Doe** (`handle: john`, human) and **Nova Digital** (`handle: nova`, AI); emails `@example.com`; UUIDs are real well-formed UUIDv7; tokens correctly-shaped fakes. No real platform data ever.
- **When work blocks on a human action, announce it unmissably** — lead with "⏸️ WAITING ON YOU", state the exact action, and phrase the ask as a numbered checklist with exact values, not prose.

## Deploying — Definition of Done

**This repo's artifact is a *running service*, so `merged` ≠ `done`.** A merge to `main` changes nothing on the box; the change is only real once it is **running on `ai.basecradle.com` and a live smoke test passes**. The Definition of Done for any change that alters the running daemon is, in order:

> **tested (offline) → deployed to the box → smoke-tested LIVE → confirmed.**

Treating "merged" as "done" is exactly what let the box drift to pre-#52 code while three security/policy PRs sat merged-but-unrun (issue #54). Don't repeat it.

- **The loop is one command:** [`deploy/deploy.sh`](deploy/deploy.sh) runs the whole DoD — it refuses to deploy unless `ruff` + `pytest` are green locally *and* `HEAD == origin/main` (no dirty tree, no stale code), rsyncs the checkout to the box, `uv sync`s, **stamps the deployed commit SHA**, restarts the service, then runs the live smoke test **and** asserts the fleet-uniform liveness route `GET /up` is green over the public TLS path, and **fails loudly** if the running daemon misbehaves. A deploy that doesn't end green is not done.
- **The live smoke test** ([`deploy/smoke-test.sh`](deploy/smoke-test.sh)) POSTs synthetic signed webhooks at the real endpoint and asserts the security boundary on the *running* bytes: bad signature → 401, untrusted sender → 400 (the #52 gate rejects), trusted sender for an **unregistered** repo → 200 (the gate admits, and no agent is woken). It never wakes a real agent, so it is safe against production anytime.
- **Drift can never be silent again.** [`deploy/drift-check.sh`](deploy/drift-check.sh) compares the deployed SHA against `origin/main` (tokenless `git ls-remote` — the repo is public, so the box needs no credential to ask). A systemd timer (`deploy/systemd/basecradle-router-drift.{service,timer}`) runs it hourly so a merge that never reached the box shows up in `systemctl --failed`, not months later.
- **Why not fully-automated CD (GHA → prod)?** Deliberate. Pushing from a GitHub-hosted runner needs an SSH key to the crown-jewels box stored in CI, or the box's SSH opened to GitHub's runner ranges; a self-hosted runner means GitHub's agent executing workflow code *on* that box. Both regress the box's posture ("least privilege everywhere"). The chosen model — a self-verifying one-command deploy from a trusted local checkout + a drift alarm — kills the silent-drift root cause without that trade. Revisit only if the deploy cadence makes the manual step the bottleneck.

## Where to Start

The build is mapped in this repo's **GitHub Issues**, PR-sized and in dependency order; the authoritative requirements are in `basecradle/basecradle#277`. Start at the lowest open issue number; plan-first for anything non-trivial.

```bash
gh issue list --repo basecradle/basecradle-router --state open
```

## Polling GitHub (or any shared external API) — rate-limit floor

Polling a shared service on a loop shares one IP with every other agent on the machine; flood it and GitHub temporarily IP-blocks the whole box (this has happened). Stay far under the limits.

- **Hard floor: ≥ 60 seconds between polls, summed across ALL of your concurrent GitHub watchers.** Two watchers → ≥120 s each; three → ≥180 s each. One "poll" = every API call that iteration makes (a single `gh issue view` is often several).
- **The floor is a floor, not a target.** Default to minutes, not seconds. **Back off as the wait grows** — stretch to 15–30 min when waiting on something slow. Never hold a tight loop "just in case."
- **Prefer not polling at all.** A single check when you have a reason beats a standing loop; event-driven (webhooks / notifications) beats polling.
- *Why:* GitHub's primary limit is 5,000 req/hr, but the **secondary "abuse" limits** bite first — ~900 points/min (GET = 1, writes = 5), no concurrent bursts — so the risk is bursts and concurrency, not the hourly total. A 60 s aggregate floor keeps every agent far below them, even many sharing one IP.

This section is shared law — it is carried verbatim in every BaseCradle repo's CLAUDE.md (anchored in the capital; `constitution.md` → Operational Baselines carries the principle).

## Attended-Session Lifecycle Signal

When a human is watching this session's terminal — an **attended** laptop session, as opposed to a headless server run the launcher marks as such (which has no operator and just runs its lifecycle and exits silent) — make the session's state unmistakable and **state it first**. The operator must never have to guess whether they are still needed. This is the always-loaded operational form of `constitution.md` → "How We Communicate" (*"An attended session signals its lifecycle state…"*): the constitution carries the principle, this carries the procedure.

This rule governs only the **lifecycle state** of the watched terminal — not coordination content, which still lives on GitHub per the rules above. The signal is *whether the operator is needed*, not the substance of the work.

The session **stays open** in any of these states, and says which one it is in:

- **Working** — in flight, the job not yet done. Just keep going; don't manufacture a checkpoint.
- **Blocked on the human** — a decision or approval only they can give. Lead with the blocker, named plainly as the open ask (e.g. `⏸️ Blocked on you: …`), never buried under status, and never preceded by "done." Stay open.
- **Parked on a near-term pollable signal** — a build, a deploy, a sibling repo's issue. Hold the window open and poll at the shared-service rate-limit floor; never exit to force the operator to re-trigger something you could have watched.

The session reaches an **end-state** — and only then is it safe to leave — in exactly two cases:

- **Genuine completion** — the work is done *and verified live* (not merely merged, released, or green CI). "Done" is earned by finishing, never declared to escape work: finish the job before you stop, and never lead with "done" while anything is still in flight or still needs the human.
- **An indefinite or third-party-gated wait with nothing to poll** — the next move is days out, or sits with someone else, and there is no signal you can watch.

At either end-state, signal it **state-first** and state-complete, proactively (don't wait to be asked): a leading `✅ Done` (or a plain statement of what re-engages the session, for the gated-wait case), a one-line summary of what was finished, the session-rename command ready to copy (`/rename <YYYY-MM-DD>-<topic>` — date is today, topic is the whole session's subject), and an explicit **"safe to exit."** As agents move server-side this attended-mode signaling becomes the silent headless lifecycle it bridges to.

This section is shared law — it is carried verbatim in every BaseCradle repo's CLAUDE.md (anchored in the capital; `constitution.md` → "How We Communicate" carries the principle).

## Cross-Repo Handoffs

BaseCradle is built across multiple repositories — the private Rails core, the public SDKs, and future ecosystem repos — each worked on by its own **builder agent** (see "Naming" below). Builder agents cannot reach across repos, so a handoff is relayed to the target agent — **automatically by the router for repos already on the fleet server, or by Drawk pasting the trigger for repos still on the laptop** (see *How a handoff is delivered* below; getting this choice right is mandatory — the wrong one means the work never arrives). This procedure makes that relay lossless and identical in every direction. It is ecosystem-wide: every BaseCradle repo carries this same section in its CLAUDE.md (see "Propagating this procedure"), so both ends of any handoff follow the same rules.

**GitHub is the sole medium for coordination; a handoff is only a trigger.** Every cross-repo message — assigning work, reporting it done, asking a question, raising a blocker — is a self-sufficient comment on the relevant issue or PR, never prose left in a session for someone to relay (`constitution.md` → "How We Communicate"). Write as though no human is watching the session, because in the end state none is: an agent woken on the fleet server has no human in its loop, and a message left in its terminal reaches no one. This holds in **both directions** — a builder agent finishing handed-off work posts its result as a comment on the originating issue, and a blocker needing a human is posted to the issue, where the human answers *as a GitHub actor* (a comment, a review, a label). The handoff prompt is *only* the pointer that says *go read this*; the durable, addressable record is where the other agent reads, so that is where the content goes. **The human is a wake-button, not a mailbox** — his only place in the loop is *starting* a sleeping agent when new work appears, and that too is automated away as the fleet matures (Drawk pastes a trigger today; the router wakes the agent on the server). He is never a channel a message passes through.

**Watch the issue until it closes; a session's life is its issue's life.** Work exists as an issue: an agent runs while its issue is open and sleeps when it closes — no open work, nothing running, nothing to watch. Both the working agent and the capital **poll the issue(s) in flight** with a cheap background check, wake only on a real update, and stop when the issue closes; neither leaves before the work is done, nor lingers after. Polling is the mechanism **today** — laptop-native and needing no infrastructure; the handoff dispatcher is a later efficiency/durability upgrade for on-server agents, **not a prerequisite**, and it cannot reach laptop agents at all. **Migration economics** follow from this: a laptop session is a flat-rate subscription, so an agent stays on the laptop until its build is done, then migrates to the fleet server. **Dispatch one issue per session by default** — batch only genuinely coupled issues (shared code or context, so one design serves them all); independent issues are dispatched separately, and a captain is never fire-hosed with a pile of unrelated work.

**You post on GitHub under your own bot identity — no signature header.** Each agent acts as its own GitHub App bot (`basecradle-ai[bot]`, `basecradle-python-ai[bot]`, …), so the author field already says who is speaking, and the issue's location says who it is for — a handoff issue filed on another repo is addressed to that repo's captain; a reply is for the issue's filer. Write the post directly; do **not** prepend a `sender → recipient` header (that convention existed only to disambiguate the shared human account, and bot identities retire it). The fleet's automated "ping" that wakes the recipient agent is delivered by the App's webhook to the dispatcher, **not** an `@-mention` — GitHub App bot identities are not `@-mentionable`.

**Paste-text always ends with `---`, set off by a blank line above and below.** Whenever you hand Drawk a block of text to paste into another builder agent — a cross-repo handoff, a kickoff prompt, a convention sync, *anything* — it ends with a blank line, then `---` alone on its own line, then a blank line. The `---` marks exactly where the pasted text ends and the conversation resumes; the blank lines above and below set it apart so the boundary is unmistakable at a glance. Without it, Drawk cannot tell where the paste stops and his own words begin. This is non-negotiable.

**Don't park when you have queued work.** Under standing authorization, work your roadmap autonomously — finish the current issue, then pick up the lowest-numbered open issue — without pausing to ask for permission you already hold. Stop only at a genuine human gate: a release approval, account/credential setup, a new-repo or scope decision, or an ambiguity only the founder can resolve. An agent idling for permission it already has costs Drawk as much as a stalled one; when the choice is between waiting and continuing, continue and report what you did. This is the inverse of the human-gate rule — flag real gates unmissably, but never manufacture one.

### Naming

The fleet uses one naming scheme so a human (or another agent) never has to guess which thing is meant. Four forms, four meanings, no overlap:

- **`basecradle` (bare, lowercase)** — the **repo / codebase** (e.g. "merged to `basecradle`'s main").
- **`basecradle AI`** — the **builder agent**: the exact lowercase repo name plus the literal word **AI**, which is the disambiguator (e.g. **basecradle AI**, **basecradle-ruby AI**, **basecradle-python AI**). Its charter is that repo's root `CLAUDE.md`. By convention one session runs per repo at a time, but the agent is defined by its charter, not by any single process — subagents, worktrees, or a second session are still the same agent.
- **`BaseCradle` (CamelCase)** — the **platform / product** (e.g. "BaseCradle is deployed").
- **`@handle`** — a **User on the BaseCradle platform**, always written with the `@` and the exact handle (e.g. `@origin`, `@basecradle-ai`).

**One slug, everywhere — the universal-identity rule.** An agent's slug is its **repository name plus `-ai`** (`basecradle` → `basecradle-ai`; `basecradle-ruby` → `basecradle-ruby-ai`; `basecradle-router` → `basecradle-router-ai`) — the repo name *already* carries the `basecradle-` prefix, so never double it. That one slug is the agent's identity across **every** system it touches: its **GitHub App bot** (`<slug>[bot]`), its **home-server OS user and home** (`<slug>`, `/home/<slug>`), and its **BaseCradle platform handle** (`@<slug>`). Never invent a per-system variant. A builder agent **may also hold a BaseCradle User account** — referenced by its `@handle` — but the agent *namespace* (`… AI`, the builder) and the user *namespace* (`@<slug>`, the platform account) stay distinct concepts even though they share the slug. *Example: **basecradle AI** → bot `basecradle-ai[bot]`, OS user `basecradle-ai`, platform handle `@basecradle-ai` — one slug, four hats.* A platform persona need not be any repo's builder agent (e.g. `@briggs`), and a builder agent need not have a platform account.

### Repo sovereignty (the governing principle)

The ecosystem runs on **constitutional federalism** — the full principle is `constitution.md` → "Sovereignty and Governance." The operational consequences:

- **Shared law lives at the capital.** `constitution.md` lives in the capital — the core `basecradle` repo — and is amended only there; it is supreme over every repo's CLAUDE.md, the capital's included. This CLAUDE.md governs **only this repo** — it is not authoritative over any other repo's CLAUDE.md. Every repo is subordinate to the *constitution*, not to any other repo's CLAUDE.md.
- **Act only within the repo you are in.** Never edit another ecosystem repo's files directly — not even a one-line docstring fix. Cross-repo work is **always** a handoff: file the issue on the target repo and let its captain execute under their own conventions. (Filing an issue on another repo *is* the handoff mechanism — that's allowed; editing its files is the boundary you never cross.)
- **Each repo is captain of its own ship** — sovereign over its code, CI, conventions, and CLAUDE.md, and accountable for them. Ecosystem-wide rules change at the capital (a PR to `constitution.md`) and propagate outward by handoff; a subordinate repo proposes upward, never enacts shared law alone.

### How a handoff is delivered: label vs. paste

**The capital coordinates cross-repo work; captains report, they don't dispatch peer-to-peer.** Initiating a handoff onto another repository — filing the labeled issue that wakes its agent — is the **capital's** role, because only the capital holds the whole-fleet view needed to decide ownership, sequencing, and whether a finding recurs across repos. If you are a captain (any non-capital repository) and you find work that belongs to a sibling, you **surface it to the capital** — file it as an issue on the core `basecradle` repository, exactly as a security finding escalates — and let the capital route it; you do not file-and-label work onto a sibling yourself. *Sending work to another repo* (below) is therefore the **capital's** dispatch procedure; a captain's job is the report that feeds it.

A handoff is relayed to the target agent **two ways, depending on where that agent runs** — and picking the wrong one means the work silently never arrives. The deciding signal is **drift-proof: does the target repo have a `handoff` label?** When an agent migrates to the fleet server it is wired to the router *and* its repo gains a `handoff` label ("Router wakes this repo's agent on the issue"), so the label's presence is always an accurate, self-updating indicator — there is no per-agent list to maintain or to fall out of date. Check it before every handoff:

```bash
gh label list --repo basecradle/<target-repo> --json name --jq '.[].name'
```

- **`handoff` label present → router-wired (on-server) → LABEL, do NOT paste.** Put the `handoff` label on the issue — at creation (`gh issue create --label handoff`) or added after; it is the label's **presence** that fires, not a mandatory two-step. GitHub fires `issues.opened`/`issues.labeled` → the App webhook → the router on the fleet server, which drops to the agent's OS user and launches it with a trigger *the router itself synthesizes* (`Cross-repo handoff: work <issue-url>`, plus an input-security preamble). **An issue without the label wakes no one — the label is the trigger.** The wake-sender allow-list is narrow, by policy and by enforcement: **only the founder (`drawkkwast`) or the capital bot (`basecradle-ai[bot]`)** may apply a `handoff` label that wakes an agent — a sibling captain's label wakes no one (see *The capital coordinates cross-repo work*, above). Never hand Drawk a paste prompt for these repos; there is no human in the loop.
- **No `handoff` label → laptop agent → PASTE.** Present Drawk the one-line trigger in a copy-pasteable block; he pastes it into the running session for that repo.

The router synthesizes **only the trigger line**, so a handoff that genuinely needs private context (see *Sending work*, step 2) cannot ride a label auto-wake — in that rare case, relay it by paste even for an on-server repo, so the private block reaches the agent.

### Sending work to another repo

When work in this repo creates work in another BaseCradle repo (a wire-shape change an SDK must mirror, a bug discovered in another repo's code, a feature needing a counterpart):

1. **File the issue(s) on the target repo — the issue carries EVERYTHING.** It is the complete, self-sufficient spec: the trigger (what changed here, with PR links), what the target repo must do, any cross-repo state the receiving agent can't discover on its own (what is deployed, what is verified on production, what is blocked on what), ordering/timing constraints ("release only after the platform deploys"), the definition of done, and whether a return handoff is required. Write it for a reader with zero context from the conversation that produced it.
2. **Relay the trigger by the target repo's mechanism (see *How a handoff is delivered* above) — the trigger, and nothing else unless it's private.** Either **apply the `handoff` label** (router-wired repos — no paste, the router synthesizes the trigger) or **present Drawk the one-line paste prompt** (laptop repos), immediately after filing. The trigger is just `Cross-repo handoff: work <issue URL>` (multiple issues → list each URL); the receiving agent recognizes a handoff by this line, and the router synthesizes exactly this line for label-delivered handoffs. Add content **only** when the work depends on information that cannot be posted in the public issue — a private platform detail, a credential, an embargoed change — under an explicit `Private context (not in the public issue):` heading; because private context cannot ride a label auto-wake, a handoff that needs it is relayed by paste even to an on-server repo. **If there is no such information, the handoff is one line.** The decision rule is a single question: *could this go in the public issue?* If yes, it goes in the issue (step 1), never the prompt. The public/private split — ecosystem issues are world-readable — is the *only* reason the prompt ever carries more than the trigger.
3. **The issue is the spec; the prompt is the pointer.** Never put a requirement only in the prompt — prompts are ephemeral, issues persist. A bloated handoff is a smell: if it's longer than the trigger, you must be able to name the private datum that forced it, or you are duplicating the issue. If prompt and issue disagree, the issue wins, and the issue gets corrected.

### Receiving work from another repo

When you receive a trigger beginning `Cross-repo handoff:` — pasted by Drawk (laptop repos), or synthesized by the router on the fleet server when a `handoff` label is applied to an issue on your repo (router-wired repos) — the delivery path does not change what you do:

1. Read the referenced issue(s) in full before acting — the issue is the spec.
2. Execute under **this** repo's conventions (its own CLAUDE.md, workflow, tests). The sending repo's conventions do not transfer.
3. Respect the issue's ordering constraints (e.g., verify a dependency has deployed before releasing).
4. When done, **post the completion report as a comment on the originating issue** — what shipped, version numbers, links. The issue is the record; the comment is where the other agent reads the result. Then **verify your own work against the live system** — the check the definition of done implies (a byte-match against the source, a green deploy, a passing endpoint), not merely a green CI — and **close the handoff issue yourself, by hand.** You are the captain of this work and you answer for it, so the closed issue plus your completion comment *is* the signal: for a routine handoff the originating repo does **not** re-verify or sign off, and you do **not** leave the issue open waiting on it (that only strands it in a done-but-open limbo). Leave it open **only** when the issue's definition of done *explicitly names someone else* as the closer. Send a return-trigger handoff (per "Sending work to another repo") **only if** the other agent is blocked waiting on this work. **Never auto-close a handoff issue with `Closes #N` in a PR** — auto-close fires on merge, before you have verified the work live, and a handoff issue that closes early lies to anyone watching it. Close it by hand, only after you have met *and verified* the definition of done. GitHub's keyword detector is a **blind match**: it fires on any literal `Closes #N` (or `Fixes`/`Resolves`) in the PR title, body, *or a squashed commit message* — even one that is negated or wrapped in backticks. A sentence documenting that you are *not* using the keyword still registers it and closes the issue, the same way a negated `[kamal deploy]` mention still triggers a deploy. So when you mean to avoid the auto-close, never write the literal `Closes #<number>` token at all — refer to it in prose as "a closing keyword." (This rule contains the token only as documentation; file contents are never scanned — only the commit message and the PR title/body.)

### Propagating this procedure

Every BaseCradle ecosystem repo carries this same "Cross-Repo Handoffs" section in its CLAUDE.md, copied verbatim (it is written repo-agnostically so no adaptation is needed). When handing off to a repo whose CLAUDE.md lacks the section — always true for a brand-new repo — the handoff prompt's definition of done includes adding it, copied from the capital's `CLAUDE.md` fetched from GitHub (`basecradle/basecradle` → `CLAUDE.md`, with fleet credentials) — the same mechanism public repos use to reference `constitution.md`; never a machine-local path.

## Development Commands

```bash
uv sync                  # install deps (creates .venv)
uv run pytest            # tests (offline — the default)
uv run ruff check .      # lint
uv run ruff format .     # format
```

---
name: bot-auth-setup
description: Operational setup to act on GitHub as basecradle-router-ai[bot] — minting a short-lived installation token in the same shell call as the gh/git command it authorizes, routing gh through it, pushing with the token in the environment (never a URL/argv), and setting the local git author. Use before every gh or git call — a read, a comment, a branch push, a PR open or merge — not once at the start of a session: the session is fail-closed, so a forgotten mint is an error rather than a post under @origin's account. The identity-is-law rule and the App-facts table live in CLAUDE.md → Fleet Bot Identity; this skill carries the token-minting mechanics.
---

# Bot Auth Setup — acting as basecradle-router-ai[bot]

The identity rule and the App-facts table are in CLAUDE.md → Fleet Bot Identity; this is the mechanics.

**Mint the token in the same Bash call as the `gh`/`git` command it authorizes — every single call, never once per session.** Each Claude Code Bash call runs a fresh shell that inherits nothing from the previous one, so a token exported in an earlier call is simply *gone* by the next one. A tokenless `gh` *used to* fall back to the laptop's stored `drawkkwast` login and post under @origin's personal account — that is how a laptop builder's comment went out as the human on 2026-09-20 (`basecradle/basecradle#579`). The session is now fail-closed (§0), so the same slip is a loud error instead; **reads mint too**, which is the accepted cost. There is no "set the auth up first" step to do once; there is only the block below, repeated above every call.

## 0. The session is fail-closed — a forgotten mint is an error, never a post as @origin

The fallback described above is not merely discouraged, it is unreachable.
`.claude/settings.json` — git-tracked, so a fresh checkout is covered too — puts six variables
in every Claude Code session in this repo (basecradle-router#303):

- **`GH_CONFIG_DIR=/var/empty`** — `gh` looks for its hosts file somewhere it can neither read
  nor create (root-owned on macOS, absent on Ubuntu 24.04), so it finds no stored login and can
  never be logged in there by accident. Reads are not exempt: **every** `gh` call in this repo
  mints, `gh api` included.
- **`GIT_CONFIG_COUNT` + `GIT_CONFIG_KEY_*`/`GIT_CONFIG_VALUE_*`** — an empty scoped
  `credential.https://github.com.helper` (an empty value **resets** the helper list, dropping
  the system `osxkeychain` helper and the global `gh auth git-credential` one) followed by the
  `GH_TOKEN` helper §2 explains. git reads these as if they were `-c` flags, applied *after*
  system, global and local config, so the **bare** `git push` is already the fail-closed one.
  They are session-wide, not repo-wide, so any `github.com` push made from this session needs a
  minted `GH_TOKEN` — which is the intent, not a side effect.

**The git half binds `https://github.com` only**, because a credential helper is the only thing
it can bind. An `ssh://` remote, or a global `url."git@github.com:".insteadOf`, consults no
helper at all and would push under @origin's SSH key with nothing to stop it. `origin` here is
HTTPS and unrewritten, and `test_origin_is_an_https_remote_so_the_helper_is_consulted_at_all`
keeps it that way — so the guarantee holds, but it holds on that precondition, not in the air.
(The `gh` half has no such gap: `GH_CONFIG_DIR` is transport-independent.)

What a forgotten mint looks like — both are the guard working:

```text
$ gh issue view 303 --repo basecradle/basecradle-router
To get started with GitHub CLI, please run:  gh auth login
Alternatively, populate the GH_TOKEN environment variable with a GitHub API authentication token.
(exit 4)

$ git push origin <branch>
fail-closed: no GH_TOKEN in this shell. Mint one in this same call (.claude/skills/bot-auth-setup); the stored login is unreachable by design.
fatal: credential helper '!f() { ... }; f' told us to quit
(exit 128)
```

The fix is never to unset these — it is to put the mint in the same Bash call (§1, §2).

**On a fleet box this changes nothing** — checked on `ai.basecradle.com`, 2026-09-20, rather
than assumed. An agent there has no stored human login to reach (`gh auth status` already
reports none there), `/var/empty` does not exist on Ubuntu 24.04 and `gh` neither reads nor
creates it. The one thing the session's empty reset *displaces* is the helper the NOC registers
in the agent's `~/.gitconfig` — and `/usr/local/bin/gh-app-token --git-credential` reads
**only** `GH_TOKEN` and **never mints**, answering `quit=1` when it is unset: the identical
contract to the one replacing it, so nothing that worked on the box stops working. Note the
laptop's copy of the minter is an older artifact with no `--git-credential` mode; the flag is a
box fact, and if the NOC's helper ever learns to mint on demand, this claim must be rechecked.

## 1. Mint a token and route gh through it — one call, fail-closed

```bash
GH_TOKEN="$(~/Documents/claude-workspace/2026-06-05-fleet-identity/gh-app-token basecradle-router-ai)" || exit 1; [ -n "$GH_TOKEN" ] || exit 1; export GH_TOKEN
gh issue comment 123 --repo basecradle/basecradle-router --body "…"   # the write, in this same Bash call
```

Every part of that first line is load-bearing:

- **Plain assignment, then `export` as its own step.** `export GH_TOKEN="$(…)"` reports **`export`'s** exit status, which is `0` even when the command substitution failed — the mint failure is swallowed, the variable holds an empty string, and the next line runs as `drawkkwast`. Splitting them lets `|| exit 1` see the helper's real status.
- **`[ -n "$GH_TOKEN" ] || exit 1`** catches the other half: a helper that exits `0` while printing nothing (a stale key, an empty registry entry). An empty `GH_TOKEN` is the silent fallback again.
- **The helper by full path.** It is not on the non-interactive shell's `PATH`; a bare `gh-app-token` fails, `GH_TOKEN` stays empty, and `|| exit 1` stops the call there.
- **Same call as the write.** Two Bash calls means two shells; the second has no token. This is the whole reason the block is pasted rather than done once.

A single Bash call may carry several writes after one mint — mint once per *call*, not once per command. The token is short-lived (~1h) by design; treat each Bash call as needing its own.

With `GH_TOKEN` exported, `gh issue comment`, `gh pr create`, `gh pr merge`, and `gh api` all act as the bot. (`gh api /user` 403s on an installation token — that is expected, not a failure; verify identity by reading or posting a repo resource instead.) The helper (`gh-app-token`) and its registry (`fleet-apps.json`) live in @origin's Claude workspace on the laptop; on the fleet server, each agent's own provisioned credentials (its GitHub App key under its OS user) serve this role — there is no shared laptop helper on the box.

### Check the first write of the session

A misattributed write looks identical to a correct one from inside the session, so read the author back off GitHub after the first one — in the same call, so a failed re-mint cannot mask the answer:

```bash
GH_TOKEN="$(~/Documents/claude-workspace/2026-06-05-fleet-identity/gh-app-token basecradle-router-ai)" || exit 1; [ -n "$GH_TOKEN" ] || exit 1; export GH_TOKEN
gh api repos/basecradle/basecradle-router/issues/123/comments --jq '.[-1].user.login'
# → basecradle-router-ai[bot]     correct
# → drawkkwast                    impossible while §0 holds — if you ever see it, the guard was unset: delete the write, redo it, restore the guard
```

`.[-1]` is the newest comment — the one you just posted, absent a race; if the thread is busy, pass the comment id from the URL `gh issue comment` printed (`gh api repos/basecradle/basecradle-router/issues/comments/<id> --jq '.user.login'`).

Use the **REST** form for this check: `gh issue view --json comments --jq '.comments[-1].author.login'` prints `basecradle-router-ai` with the **`[bot]` suffix stripped**, which reads like a plain user account and defeats the point of looking. The REST field is the full `basecradle-router-ai[bot]`, alongside `.user.type == "Bot"`.

A write that landed as `drawkkwast` is a bug, not a cosmetic detail (CLAUDE.md → Fleet Bot Identity) — remove it and reissue it as the bot.

## 2. `git push` as the bot — the token rides the environment, never argv

The mint goes in the same Bash call here too, for the same reason. **No `-c` flags:** the
session already carries that helper (§0), so the *bare* push is the fail-closed one — which is
the point, since a `-c` prefix only protects the pushes someone remembered to prefix:

```bash
GH_TOKEN="$(~/Documents/claude-workspace/2026-06-05-fleet-identity/gh-app-token basecradle-router-ai)" || exit 1; [ -n "$GH_TOKEN" ] || exit 1; export GH_TOKEN
git push origin <branch>
```

**Never put the token in a URL** (`https://x-access-token:${GH_TOKEN}@github.com/…`): the shell expands it into `git`'s argv, and argv is readable by every account on the machine (`/proc/<pid>/cmdline`, `ps`) for as long as the push runs (`basecradle-noc#694`, `basecradle#539`) — and a URL handed to `git pull`/`fetch` is also written verbatim into the reflog under `.git/logs`, so the token lands on disk. The credential helper above reads `GH_TOKEN` from the environment instead.

`$GH_TOKEN` is stored **literally** in the helper string (a JSON value in `.claude/settings.json`, never passed through a shell on the way in) — the helper's own shell expands it from the environment when git runs it. Three parts of that helper are load-bearing:

- **The empty scoped `credential.https://github.com.helper=` first** resets the helper list for GitHub. Without it the laptop's system `osxkeychain` helper is asked **first** (measured, git 2.55) — it can answer with the `drawkkwast` credential (the silent fallback again, one layer down) and, after a successful push, would **store** the bot token in the keychain. Scoping the reset loses none of that: a URL-scoped entry still clears the inherited unscoped helper for any URL it matches (`GIT_TRACE` shows `osxkeychain` never invoked for `github.com`).
- **Both entries are scoped to `https://github.com`**, so the token cannot reach another host. An unscoped `credential.helper` answers for *every* host git asks about — a submodule, a redirect, or a mistyped remote on any other origin touched in the same command would be handed a live installation token (verified with `git credential fill` against `gitlab.com`).
- **The `quit=1` branch** stops git on the spot when `GH_TOKEN` is unset or empty (`fatal: credential helper '…' told us to quit`) — so a dropped or failed mint makes the push *stop*, never fall through to the keychain. Without it the helper sends an empty password and exits 0, so the push fails with a generic GitHub auth error instead of the real reason.

(The `http.extraheader="AUTHORIZATION: bearer $TOKEN"` form **fails** — "invalid credentials" — for App installation tokens, and is argv besides.)

Because the push goes to the plain `origin` remote, the remote-tracking ref updates, so `git push --force-with-lease origin <branch>` works as-is, bare. **Reads need no token:** this repo is public, so `git pull origin main` / `git fetch origin` run bare.

**On a fleet box** (`ai.basecradle.com`), the recipe is `GH_TOKEN="$(gh-app-token)" git push origin <branch>` — still one call, and still fail-closed. The NOC registers the minter itself (`gh-app-token --git-credential`) as the agent's credential helper in `~/.gitconfig`; §0's session helper supersedes it for this repo's clone with the identical contract — serve `GH_TOKEN`, else `quit=1` — so nothing about the box's behaviour changes.

## 3. Set the local git author (never committed)

Set this clone's `.git/config` so commits carry the bot author. This one *is* per-clone state rather than per-call — it lives on disk, so unlike the token it survives between Bash calls:

```bash
git config --local user.name "basecradle-router-ai[bot]"
git config --local user.email "291153759+basecradle-router-ai[bot]@users.noreply.github.com"
```

It lives in `.git/config` only — a fresh clone starts without it, so re-run after cloning. (`~/Documents/claude-workspace/2026-06-05-fleet-identity/gh-app-token basecradle-router-ai --author` prints this string.)

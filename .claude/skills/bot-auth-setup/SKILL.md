---
name: bot-auth-setup
description: Operational setup to act on GitHub as basecradle-router-ai[bot] — minting a short-lived installation token, routing gh through it, pushing with the token in the environment (never a URL/argv), and setting the local git author. Use at the start of any session that will push a branch, open or merge a PR, comment on an issue, or make any other gh/git write. The identity-is-law rule and the App-facts table live in CLAUDE.md → Fleet Bot Identity; this skill carries the token-minting mechanics.
---

# Bot Auth Setup — acting as basecradle-router-ai[bot]

Do this **first, before any `gh`/git write.** The identity rule and the App-facts table are in CLAUDE.md → Fleet Bot Identity; this is the mechanics.

## 1. Mint a token and route gh through it

Mint a short-lived (~1h) installation token with the shared fleet helper, invoked by **full path** (it is not on the non-interactive shell's `PATH`, and a bare `gh-app-token` fails and lets `gh` fall through to the stored `drawkkwast` login):

```bash
HELPER=~/Documents/claude-workspace/2026-06-05-fleet-identity/gh-app-token
export GH_TOKEN="$("$HELPER" basecradle-router-ai)"      # gh + the GitHub API now act as the bot
```

With `GH_TOKEN` exported, `gh issue comment`, `gh pr create`, `gh pr merge`, and `gh api` all post as the bot. Re-mint per batch — the token is short-lived by design. (`gh api /user` 403s on an installation token — that is expected, not a failure; verify identity by reading/posting a repo resource instead.) The helper (`gh-app-token`) and its registry (`fleet-apps.json`) live in @origin's Claude workspace on the laptop; on the fleet server, each agent's own provisioned credentials (its GitHub App key under its OS user) serve this role — there is no shared laptop helper on the box.

## 2. `git push` as the bot — the token rides the environment, never argv

**Never put the token in a URL** (`https://x-access-token:${GH_TOKEN}@github.com/…`): the shell expands it into `git`'s argv, and argv is readable by every account on the machine (`/proc/<pid>/cmdline`, `ps`) for as long as the push runs (`basecradle-noc#694`, `basecradle#539`) — and a URL handed to `git pull`/`fetch` is also written verbatim into the reflog under `.git/logs`, so the token lands on disk. Hand git the token through a credential helper that reads `GH_TOKEN` from the environment instead:

```bash
git -c 'credential.https://github.com.helper=' \
    -c 'credential.https://github.com.helper=!f() { if [ "$1" = get ]; then if [ -z "$GH_TOKEN" ]; then echo quit=1; else echo username=x-access-token; echo "password=$GH_TOKEN"; fi; fi; }; f' \
    push origin <branch>
```

The single quotes keep `$GH_TOKEN` literal in argv — the helper's own shell expands it from the environment. Three more parts are load-bearing:

- **The empty scoped `credential.https://github.com.helper=` first** resets the helper list for GitHub. Without it the laptop's system `osxkeychain` helper is asked **first** (measured, git 2.55) — it can answer with the `drawkkwast` credential (the silent fallback again, one layer down) and, after a successful push, would **store** the bot token in the keychain. Scoping the reset loses none of that: a URL-scoped entry still clears the inherited unscoped helper for any URL it matches (`GIT_TRACE` shows `osxkeychain` never invoked for `github.com`).
- **Both entries are scoped to `https://github.com`**, so the token cannot reach another host. An unscoped `credential.helper` answers for *every* host git asks about — a submodule, a redirect, or a mistyped remote on any other origin touched in the same command would be handed a live installation token (verified with `git credential fill` against `gitlab.com`).
- **The `quit=1` branch** stops git on the spot when `GH_TOKEN` is unset (`fatal: credential helper '…' told us to quit`). Without it the helper sends an empty password and exits 0, so the push fails with a generic GitHub auth error instead of the real reason.

(The `http.extraheader="AUTHORIZATION: bearer $TOKEN"` form **fails** — "invalid credentials" — for App installation tokens, and is argv besides.)

Because the push goes to the plain `origin` remote, the remote-tracking ref updates, so `git push --force-with-lease origin <branch>` works as-is under the same `-c` prefix. **Reads need no token:** this repo is public, so `git pull origin main` / `git fetch origin` run bare.

**On a fleet box** (`ai.basecradle.com`), the NOC has already registered the minter as the agent's credential helper, so the recipe there is just `GH_TOKEN="$(gh-app-token)" git push origin <branch>` (or a plain `git push origin <branch>` when `GH_TOKEN` is already exported for `gh` in the same shell).

## 3. Set the local git author (never committed)

Set this clone's `.git/config` so commits carry the bot author:

```bash
git config --local user.name "basecradle-router-ai[bot]"
git config --local user.email "291153759+basecradle-router-ai[bot]@users.noreply.github.com"
```

It lives in `.git/config` only — a fresh clone starts without it, so re-run after cloning. (`"$HELPER" basecradle-router-ai --author` prints this string.)

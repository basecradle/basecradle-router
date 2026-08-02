---
name: bot-auth-setup
description: Operational setup to act on GitHub as basecradle-router-ai[bot] — minting a short-lived installation token, routing gh and git push through it, and setting the local git author. Use at the start of any session that will push a branch, open or merge a PR, comment on an issue, or make any other gh/git write. The identity-is-law rule and the App-facts table live in CLAUDE.md → Fleet Bot Identity; this skill carries the token-minting mechanics.
---

# Bot Auth Setup — acting as basecradle-router-ai[bot]

Do this **first, before any `gh`/git write.** The identity rule and the App-facts table are in CLAUDE.md → Fleet Bot Identity; this is the mechanics.

## Mint a token and route gh + git push through it

Mint a short-lived (~1h) installation token with the shared fleet helper and route both `gh` and `git push` through it:

```bash
HELPER=~/Documents/claude-workspace/2026-06-05-fleet-identity/gh-app-token
export GH_TOKEN="$("$HELPER" basecradle-router-ai)"      # gh + the GitHub API now act as the bot
# push via the freshly-minted authenticated remote (re-mint per batch; tokens last ~1h):
git push "$("$HELPER" basecradle-router-ai --remote)" <branch>
# equivalently: https://x-access-token:<token>@github.com/basecradle/basecradle-router.git
```

With `GH_TOKEN` exported, `gh issue comment`, `gh pr create`, `gh pr merge`, and `gh api` all post as the bot. Re-mint per batch — the token is short-lived by design. (`gh api /user` 403s on an installation token — that is expected, not a failure; verify identity by reading/posting a repo resource instead.) The helper (`gh-app-token`) and its registry (`fleet-apps.json`) live in @origin's Claude workspace on the laptop; on the fleet server, each agent's own provisioned credentials (its GitHub App key under its OS user) serve this role — there is no shared laptop helper on the box.

## Set the local git author (never committed)

Set this clone's `.git/config` so commits carry the bot author:

```bash
git config --local user.name "basecradle-router-ai[bot]"
git config --local user.email "291153759+basecradle-router-ai[bot]@users.noreply.github.com"
```

It lives in `.git/config` only — a fresh clone starts without it, so re-run after cloning. (`"$HELPER" basecradle-router-ai --author` prints this string.)

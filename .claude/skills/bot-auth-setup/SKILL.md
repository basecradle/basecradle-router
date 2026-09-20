---
name: bot-auth-setup
description: Operational setup to act on GitHub as basecradle-router-ai[bot] — minting a short-lived installation token in the same shell call as the write it authorizes, routing gh through it, pushing with the token in the environment (never a URL/argv), and setting the local git author. Use before every gh/git write — a comment, a branch push, a PR open or merge — not once at the start of a session. The identity-is-law rule and the App-facts table live in CLAUDE.md → Fleet Bot Identity; this skill carries the token-minting mechanics.
---

# Bot Auth Setup — acting as basecradle-router-ai[bot]

The identity rule and the App-facts table are in CLAUDE.md → Fleet Bot Identity; this is the mechanics.

**Mint the token in the same Bash call as the write it authorizes — every single write, never once per session.** Each Claude Code Bash call runs a fresh shell that inherits nothing from the previous one, so a token exported in an earlier call is simply *gone* by the next one — and a tokenless `gh` does not fail, it falls back to the laptop's stored `drawkkwast` login and posts under @origin's personal account. That is exactly how a laptop builder's comment went out as the human on 2026-09-20 (`basecradle/basecradle#579`). There is no "set the auth up first" step to do once; there is only the block below, repeated above every write.

## 1. Mint a token and route gh through it — one call, fail-closed

```bash
GH_TOKEN="$(~/Documents/claude-workspace/2026-06-05-fleet-identity/gh-app-token basecradle-router-ai)" || exit 1; [ -n "$GH_TOKEN" ] || exit 1; export GH_TOKEN
gh issue comment 123 --repo basecradle/basecradle-router --body "…"   # the write, in this same Bash call
```

Every part of that first line is load-bearing:

- **Plain assignment, then `export` as its own step.** `export GH_TOKEN="$(…)"` reports **`export`'s** exit status, which is `0` even when the command substitution failed — the mint failure is swallowed, the variable holds an empty string, and the next line runs as `drawkkwast`. Splitting them lets `|| exit 1` see the helper's real status.
- **`[ -n "$GH_TOKEN" ] || exit 1`** catches the other half: a helper that exits `0` while printing nothing (a stale key, an empty registry entry). An empty `GH_TOKEN` is the silent fallback again.
- **The helper by full path.** It is not on the non-interactive shell's `PATH`; a bare `gh-app-token` fails, and `gh` falls through to the stored login.
- **Same call as the write.** Two Bash calls means two shells; the second has no token. This is the whole reason the block is pasted rather than done once.

A single Bash call may carry several writes after one mint — mint once per *call*, not once per command. The token is short-lived (~1h) by design; treat each Bash call as needing its own.

With `GH_TOKEN` exported, `gh issue comment`, `gh pr create`, `gh pr merge`, and `gh api` all act as the bot. (`gh api /user` 403s on an installation token — that is expected, not a failure; verify identity by reading or posting a repo resource instead.) The helper (`gh-app-token`) and its registry (`fleet-apps.json`) live in @origin's Claude workspace on the laptop; on the fleet server, each agent's own provisioned credentials (its GitHub App key under its OS user) serve this role — there is no shared laptop helper on the box.

### Check the first write of the session

A misattributed write looks identical to a correct one from inside the session, so read the author back off GitHub after the first one — in the same call, so a failed re-mint cannot mask the answer:

```bash
GH_TOKEN="$(~/Documents/claude-workspace/2026-06-05-fleet-identity/gh-app-token basecradle-router-ai)" || exit 1; [ -n "$GH_TOKEN" ] || exit 1; export GH_TOKEN
gh api repos/basecradle/basecradle-router/issues/123/comments --jq '.[-1].user.login'
# → basecradle-router-ai[bot]     correct
# → drawkkwast                    the fallback fired — the write is under @origin's account; delete it and redo it
```

`.[-1]` is the newest comment — the one you just posted, absent a race; if the thread is busy, pass the comment id from the URL `gh issue comment` printed (`gh api repos/basecradle/basecradle-router/issues/comments/<id> --jq '.user.login'`).

Use the **REST** form for this check: `gh issue view --json comments --jq '.comments[-1].author.login'` prints `basecradle-router-ai` with the **`[bot]` suffix stripped**, which reads like a plain user account and defeats the point of looking. The REST field is the full `basecradle-router-ai[bot]`, alongside `.user.type == "Bot"`.

A write that landed as `drawkkwast` is a bug, not a cosmetic detail (CLAUDE.md → Fleet Bot Identity) — remove it and reissue it as the bot.

## 2. `git push` as the bot — the token rides the environment, never argv

The mint goes in the same Bash call here too, for the same reason:

```bash
GH_TOKEN="$(~/Documents/claude-workspace/2026-06-05-fleet-identity/gh-app-token basecradle-router-ai)" || exit 1; [ -n "$GH_TOKEN" ] || exit 1; export GH_TOKEN
git -c 'credential.https://github.com.helper=' \
    -c 'credential.https://github.com.helper=!f() { if [ "$1" = get ]; then if [ -z "$GH_TOKEN" ]; then echo quit=1; else echo username=x-access-token; echo "password=$GH_TOKEN"; fi; fi; }; f' \
    push origin <branch>
```

**Never put the token in a URL** (`https://x-access-token:${GH_TOKEN}@github.com/…`): the shell expands it into `git`'s argv, and argv is readable by every account on the machine (`/proc/<pid>/cmdline`, `ps`) for as long as the push runs (`basecradle-noc#694`, `basecradle#539`) — and a URL handed to `git pull`/`fetch` is also written verbatim into the reflog under `.git/logs`, so the token lands on disk. The credential helper above reads `GH_TOKEN` from the environment instead.

The single quotes keep `$GH_TOKEN` literal in argv — the helper's own shell expands it from the environment. Three more parts are load-bearing:

- **The empty scoped `credential.https://github.com.helper=` first** resets the helper list for GitHub. Without it the laptop's system `osxkeychain` helper is asked **first** (measured, git 2.55) — it can answer with the `drawkkwast` credential (the silent fallback again, one layer down) and, after a successful push, would **store** the bot token in the keychain. Scoping the reset loses none of that: a URL-scoped entry still clears the inherited unscoped helper for any URL it matches (`GIT_TRACE` shows `osxkeychain` never invoked for `github.com`).
- **Both entries are scoped to `https://github.com`**, so the token cannot reach another host. An unscoped `credential.helper` answers for *every* host git asks about — a submodule, a redirect, or a mistyped remote on any other origin touched in the same command would be handed a live installation token (verified with `git credential fill` against `gitlab.com`).
- **The `quit=1` branch** stops git on the spot when `GH_TOKEN` is unset or empty (`fatal: credential helper '…' told us to quit`) — so a dropped or failed mint makes the push *stop*, never fall through to the keychain. Without it the helper sends an empty password and exits 0, so the push fails with a generic GitHub auth error instead of the real reason.

(The `http.extraheader="AUTHORIZATION: bearer $TOKEN"` form **fails** — "invalid credentials" — for App installation tokens, and is argv besides.)

Because the push goes to the plain `origin` remote, the remote-tracking ref updates, so `git push --force-with-lease origin <branch>` works as-is under the same `-c` prefix. **Reads need no token:** this repo is public, so `git pull origin main` / `git fetch origin` run bare.

**On a fleet box** (`ai.basecradle.com`), the NOC has already registered the minter as the agent's credential helper, so the recipe there is `GH_TOKEN="$(gh-app-token)" git push origin <branch>` — still one call, and still fail-closed via `quit=1` if the mint comes back empty.

## 3. Set the local git author (never committed)

Set this clone's `.git/config` so commits carry the bot author. This one *is* per-clone state rather than per-call — it lives on disk, so unlike the token it survives between Bash calls:

```bash
git config --local user.name "basecradle-router-ai[bot]"
git config --local user.email "291153759+basecradle-router-ai[bot]@users.noreply.github.com"
```

It lives in `.git/config` only — a fresh clone starts without it, so re-run after cloning. (`~/Documents/claude-workspace/2026-06-05-fleet-identity/gh-app-token basecradle-router-ai --author` prints this string.)

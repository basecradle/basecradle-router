#!/usr/bin/env bash
#
# commit-trailers.sh — fail when a commit this PR contributes, authored by this
# repo's bot, carries a Co-Authored-By trailer (basecradle-router#354).
#
# CLAUDE.md → Fleet Bot Identity: a commit authored by basecradle-router-ai[bot]
# carries no Co-Authored-By trailer — the author already *is* the agent. The rule
# sat in an always-loaded file and was still broken: six squash commits on `main`
# between 2026-09-20 and 2026-10-22 carry a `Co-Authored-By: Claude` line from
# their PR's commits. So CI refuses the merge rather than trusting the text — the
# shape basecradle-python's `commit-trailers` job proved first.
#
# Only the commits the PR contributes are read: reachable from its head and not
# from its base. `main`'s own commits are never re-litigated: GitHub's squash-merge
# writes a self-co-author line (`Co-authored-by: basecradle-router-ai[bot] …`) into
# every one of them, and none can be rewritten. So this runs on pull_request events
# only, never on a push.
#
# Inputs (environment): BOT, BASE_SHA, HEAD_SHA; BASE_REF optional (the live base
#   branch, excluded alongside the event's point-in-time BASE_SHA so a branch that
#   merged main in never answers for main's commits).
# Usage: commit-trailers.sh   (run from the repo root; needs full history)
# Exit:  0 = clean · 1 = a trailer found · 2 = bad invocation
#
set -euo pipefail

for var in BOT BASE_SHA HEAD_SHA; do
  if [[ -z "${!var:-}" ]]; then
    echo "commit-trailers: $var is not set." >&2
    exit 2
  fi
done

# Single source of truth: the identity is CLAUDE.md's to state. If it changes
# there and not in ci.yml, the author filter below would quietly match nothing and
# the guard would pass everything — refuse instead. Anchored on the Commit-author
# row itself: the bot's name also appears in the charter's prose, so a bare match
# would survive a changed row.
if ! grep -qF -- "| Commit-author | \`$BOT <" CLAUDE.md; then
  echo "commit-trailers: $BOT is no longer the commit-author in CLAUDE.md → Fleet Bot Identity; update ci.yml to match." >&2
  exit 2
fi

# An unresolvable end (a force-pushed base, a shallow clone) is a bad invocation
# with a named cause, never git's bare 128 from the walk below.
for var in BASE_SHA HEAD_SHA; do
  if ! git rev-parse --verify --quiet "${!var}^{commit}" >/dev/null; then
    echo "commit-trailers: $var (${!var}) is not a commit in this clone — is the checkout full-history?" >&2
    exit 2
  fi
done

excludes=("$BASE_SHA")
if [[ -n "${BASE_REF:-}" ]] && git rev-parse --verify --quiet "refs/remotes/origin/$BASE_REF" >/dev/null; then
  excludes+=("refs/remotes/origin/$BASE_REF")
fi

# Each git call is its own assignment so a failure trips `set -e`; inside a `case`
# word or before `||` it would not, and the commit would pass unexamined.
shas="$(git log --format=%H "$HEAD_SHA" --not "${excludes[@]}")"

bad=()
examined=0
total=0
for sha in $shas; do
  total=$((total + 1))
  author="$(git show -s --format='%an <%ae>' "$sha")"
  # Quoted, so the [bot] brackets stay literal and are not a glob class.
  case "$author" in *"$BOT"*) ;; *) continue ;; esac
  examined=$((examined + 1))
  # The whole message, not git's %(trailers:...) — that parses only the final
  # paragraph, so a trailer with any paragraph after it would slip through. A
  # here-string, not `printf | grep -q`: under pipefail a consumer that stops at
  # its first match can fail the pipeline exactly when it matched (#172).
  message="$(git show -s --format=%B "$sha")"
  if grep -qi '^Co-Authored-By:' <<<"$message"; then
    bad+=("$(git show -s --format='%h %s' "$sha")")
  fi
done

# The count is tested before the array is expanded: bash 3.2 (macOS's /bin/bash)
# reads an empty "${bad[@]}" under `set -u` as an unbound variable.
if (( ${#bad[@]} > 0 )); then
  for commit in "${bad[@]}"; do
    echo "::error::Fleet Bot Identity: $commit carries a Co-Authored-By trailer. A commit authored by $BOT carries none — rewrite the message without it."
  done
  exit 1
fi

# Not a failure — a human's commit may carry a co-author — but a PR whose commits
# are none of them the bot's is worth a look: it is also what a skipped git-author
# step looks like (CLAUDE.md → Fleet Bot Identity).
if (( total > 0 && examined == 0 )); then
  echo "::warning::commit-trailers: none of this PR's $total commit(s) is authored by $BOT, so none was examined."
fi

echo "commit-trailers: clean ($examined of $total commit(s) by $BOT examined)."

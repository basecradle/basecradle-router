#!/usr/bin/env bash
#
# interaction-limit-renewal.sh — keep the fleet's public write-surface locked to
# org members, durably. (basecradle-router#60, workstream 3.)
#
# GitHub interaction limits are temporary — max 6 months, no permanent option.
# Without renewal the public repos silently reopen to drive-by writes: the exact
# surface the #60 "memory middleware" injection came through. This script does two
# things, idempotently, so it is safe to run on a schedule:
#
#   1. RENEW — re-apply `collaborators_only` to the target repo, resetting the
#      6-month clock. (Default target: this repo.)
#   2. AUDIT — read-only sweep of every public repo in the org for (a) an active
#      interaction limit and (b) an open renewal-reminder issue. A gap anywhere
#      makes the script exit non-zero, so a scheduled run turns one missed repo
#      into a visible failure instead of a silent lapse months later.
#
# The audit is read-only and so does not touch other repos' sovereignty; only the
# explicit RENEW target is written. Org-wide *enforcement* (a single org-level
# limit) and the new-public-repo default belong at the capital — see #60's DoD.
#
# CREDENTIAL — setting an interaction limit needs a token with repo
# Administration:write. The built-in Actions GITHUB_TOKEN CANNOT do this:
# `administration` is not a grantable GITHUB_TOKEN permission, so it 403s. Supply a
# fleet App installation token / PAT with Administration:write as GH_TOKEN (the
# scheduled workflow reads it from a repo secret). Least privilege: repo
# Administration:write, NOT org-admin.
#
# The `gh` invocation is injectable via $GH (default `gh`) so the boundary can be
# mocked in tests — nothing here ever has to hit the live API to be exercised.
#
# Usage: interaction-limit-renewal.sh [--dry-run] [--repo OWNER/NAME] [--org NAME]
#   --dry-run   audit + print what RENEW would do, but perform no writes
#   --repo      the repo to renew (default: basecradle/basecradle-router)
#   --org       the org to audit (default: the renew repo's owner)
# Exit: 0 = renewed (or dry-run) and audit clean · 1 = audit found a gap · 2 = usage/credential
#
set -euo pipefail

GH="${GH:-gh}"
DRY_RUN=0
REPO="basecradle/basecradle-router"
ORG=""

while (( $# > 0 )); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --repo) REPO="${2:-}"; shift ;;
    --org) ORG="${2:-}"; shift ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "interaction-limit-renewal: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if [[ "$REPO" != */* ]]; then
  echo "interaction-limit-renewal: --repo must be OWNER/NAME, got '$REPO'" >&2
  exit 2
fi
: "${ORG:=${REPO%%/*}}"

log() { printf '==> %s\n' "$*"; }

# --- 1. RENEW the target repo -------------------------------------------------
if (( DRY_RUN )); then
  log "DRY-RUN: would re-apply collaborators_only (six_months) to $REPO"
else
  log "renewing collaborators_only (six_months) on $REPO"
  "$GH" api -X PUT "/repos/$REPO/interaction-limits" \
    -f limit=collaborators_only -f expiry=six_months >/dev/null
fi

# --- 2. AUDIT the whole fleet (read-only) ------------------------------------
log "auditing every public repo in '$ORG' for an active limit + open reminder"
gaps=0
while IFS= read -r name; do
  [[ -z "$name" ]] && continue
  slug="$ORG/$name"

  limit="$("$GH" api "/repos/$slug/interaction-limits" --jq '.limit' 2>/dev/null || true)"
  if [[ "$limit" == "collaborators_only" ]]; then
    limit_state="limit=collaborators_only"
  else
    limit_state="limit=MISSING"
    gaps=$((gaps + 1))
  fi

  reminders="$("$GH" issue list --repo "$slug" \
    --search "Renew interaction limit in:title state:open" \
    --json number --jq 'length' 2>/dev/null || echo 0)"
  if [[ "$reminders" -ge 1 ]]; then
    reminder_state="reminders=$reminders"
  else
    reminder_state="reminders=MISSING"
    gaps=$((gaps + 1))
  fi

  printf '  %-32s %s · %s\n' "$slug" "$limit_state" "$reminder_state"
done < <("$GH" repo list "$ORG" --visibility public --no-archived --json name --jq '.[].name')

if (( gaps > 0 )); then
  echo "interaction-limit-renewal: AUDIT FOUND $gaps gap(s) — a public repo is" \
       "missing its limit or its renewal reminder. Fix before the next expiry." >&2
  exit 1
fi

log "audit clean: every public repo has a limit and an open reminder."

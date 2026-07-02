#!/usr/bin/env bash
#
# deploy/drift-check.sh — is the running daemon actually on origin/main?
#
# The root cause of issue #54 was SILENT drift: code merged to main but the box
# kept running the old bytes, and nothing noticed. This makes drift loud. It
# compares the commit SHA stamped at the last deploy (the deployer writes it) against
# the live tip of origin/main, fetched tokenlessly with `git ls-remote` (the repo
# is public, so the crown-jewels box needs no GitHub credential to ask "what is
# main now?").
#
# Exit 0 + green  => deployed == main, no drift.
# Exit 1 + red    => drift (or no stamp at all) — the box is NOT running main.
#
# Runs ON THE BOX (it reads the local deploy stamp). The deploy op runs it as its
# final confirm step; a systemd timer (deploy/systemd/basecradle-router-drift.timer)
# runs it on a schedule so drift surfaces even when no one is deploying.
#
set -euo pipefail

STAMP="${STAMP:-/etc/basecradle-router/deployed-sha}"
REMOTE_URL="${REMOTE_URL:-https://github.com/basecradle/basecradle-router.git}"

green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
red() { printf '\033[1;31m%s\033[0m\n' "$*"; }

[[ -r "$STAMP" ]] || {
	red "DRIFT: no deploy stamp at $STAMP — the deployed version is unknown. Deploy with 'basecradle-noc deploy-router <sha>'."
	exit 1
}

deployed="$(tr -d '[:space:]' <"$STAMP")"
main="$(git ls-remote "$REMOTE_URL" refs/heads/main | awk '{print $1}')"

[[ -n "$deployed" ]] || {
	red "DRIFT: deploy stamp $STAMP is empty."
	exit 1
}
[[ -n "$main" ]] || {
	red "could not read origin/main via git ls-remote $REMOTE_URL — network or repo problem."
	exit 1
}

if [[ "$deployed" == "$main" ]]; then
	green "IN SYNC — deployed ${deployed:0:12} == origin/main ${main:0:12}."
	exit 0
fi

red "DRIFT — the live daemon is NOT running main:"
red "  deployed   : ${deployed}"
red "  origin/main: ${main}"
red "Redeploy with 'basecradle-noc deploy-router <sha>' to close the gap."
exit 1

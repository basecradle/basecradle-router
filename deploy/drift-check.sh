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
# The SHAs differing is NOT by itself drift (issue #189). This repo carries four of
# the fleet's five verbatim-shared artifacts, so every fleet-wide re-sync — routine,
# expected, frequent — lands a docs-only commit on main that changes NOTHING the box
# runs. Reddening on those trains the operator to read red as "probably just docs
# again", which is exactly how the one genuine stale-daemon alarm gets waved through.
# So when the SHAs differ, this classifies the gap: it lists the files that changed
# between the two commits and asks whether ANY of them is daemon-relevant.
#
#   Exit 0 + green  => the running daemon matches reviewed main. Either the SHAs are
#                      equal, or main is ahead by docs-only changes — which is stated
#                      OUT LOUD, with the files named. Silence is what a broken
#                      checker looks like; this check never passes quietly.
#   Exit 1 + red    => drift — the box is NOT running main's daemon code (or there is
#                      no stamp, or the gap could not be classified).
#
# It is FAIL-CLOSED in both directions that matter: the inert set below is a narrow,
# explicit allow-list of paths nothing on the box reads or runs, and EVERY other path
# — including any path added to this repo in the future — counts as daemon-relevant
# and fails loud. And a gap that cannot be classified (network, an unfetchable SHA)
# is reported as drift, never as sync. `tests/test_drift_check.py` pins all of this
# offline, driving THIS script against a local fixture repo — including that every
# path the deploy contract consumes (deploy/systemd/*, deploy/bin/*, smoke-test.sh,
# src/**, pyproject.toml, uv.lock) is classified daemon-relevant, derived from the
# real tree rather than restated by hand.
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
plain() { printf '%s\n' "$*"; }

# >>> is_inert_path >>>
# Is this repo path one a deploy CANNOT change the running system with?
#
# The allow-list is deliberately narrow, and everything not on it is daemon-relevant
# — a new top-level directory, a new config file, a new script all fail loud without
# anyone remembering to classify them. That asymmetry is the point: a false positive
# here is a redundant redeploy, a false negative hides a genuinely stale wake daemon.
#
#   *.md          documentation, at any depth (CLAUDE.md, README.md, deploy/README.md).
#   .claude/*     the BUILDER agent's skills/settings. A woken agent runs in its own
#                 clone under /home/<agent> — wake-runner refuses any cwd outside it —
#                 never in the deployed /opt/basecradle-router/app tree, so this
#                 directory is inert on the box.
#   .github/*     CI: runs on GitHub's runners, never on the box.
#   .gitignore    git metadata; .git/ is not even shipped.
#
# NOT inert, deliberately: tests/ (coupled to src/ and to the deploy contract it pins)
# and every non-.md file under deploy/ (units, wake-runner, the smoke test, this file).
is_inert_path() {
	case "$1" in
	*.md) return 0 ;;
	.claude/*) return 0 ;;
	.github/*) return 0 ;;
	.gitignore) return 0 ;;
	*) return 1 ;;
	esac
}
# <<< is_inert_path <<<

# Print a bounded, indented file list (a docs re-sync can be wide; the journal is not).
list_paths() {
	local shown=0 path
	while IFS= read -r path; do
		[[ -n "$path" ]] || continue
		if ((shown < 20)); then
			plain "    - $path"
			shown=$((shown + 1))
		fi
	done <<<"$1"
	local total
	total="$(grep -c . <<<"$1" || true)"
	((total > shown)) && plain "    … and $((total - shown)) more"
	return 0
}

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

# The SHAs differ. Fetch just these two commits (blobless, depth 1 — a few hundred KB,
# no history, no credential) into a throwaway repo and ask WHICH files differ.
work="$(mktemp -d)" || {
	red "DRIFT — deployed ${deployed:0:12} != origin/main ${main:0:12}, and no scratch dir could be made to classify the gap (is TMPDIR writable by this user?)."
	exit 1
}
trap 'rm -rf "$work"' EXIT

unclassifiable() {
	red "DRIFT — deployed ${deployed:0:12} != origin/main ${main:0:12}, and the gap could NOT be classified:"
	red "  $1"
	red "Treating an unclassifiable gap as drift (fail-closed). Redeploy with 'basecradle-noc deploy-router <sha>' to close it."
	exit 1
}

git init --bare --quiet "$work" 2>/dev/null || unclassifiable "could not create a scratch repo in $work"

# `--filter=blob:none` keeps this to trees only; if the client or remote does not do
# partial clone, fall back to the plain depth-1 fetch rather than assuming a git
# version on the box. Both are two commits deep — no history either way.
fetch_pair() {
	git -C "$work" fetch --depth=1 --no-tags --quiet "$@" "$REMOTE_URL" "$deployed" "$main" 2>/dev/null
}
fetch_pair --filter=blob:none || fetch_pair ||
	unclassifiable "could not fetch $deployed and $main from $REMOTE_URL (network, or the deployed SHA is no longer on the remote)"

changed="$(git -C "$work" diff --name-only --no-renames "$deployed" "$main" 2>/dev/null)" ||
	unclassifiable "could not diff $deployed against $main"

relevant=""
inert=""
while IFS= read -r path; do
	[[ -n "$path" ]] || continue
	if is_inert_path "$path"; then
		inert+="$path"$'\n'
	else
		relevant+="$path"$'\n'
	fi
done <<<"$changed"

if [[ -n "$relevant" ]]; then
	red "DRIFT — the live daemon is NOT running main:"
	red "  deployed   : ${deployed}"
	red "  origin/main: ${main}"
	red "  daemon-relevant changes in the gap:"
	list_paths "$relevant"
	red "Redeploy with 'basecradle-noc deploy-router <sha>' to close the gap."
	exit 1
fi

# Ahead by docs only. Green — but say exactly why, never silently.
green "IN SYNC (daemon) — main is ahead of the deploy by DOCS-ONLY changes; the running daemon matches reviewed main."
plain "  deployed   : ${deployed}"
plain "  origin/main: ${main}"
if [[ -n "$inert" ]]; then
	plain "  ahead by (nothing the box reads or runs):"
	list_paths "$inert"
else
	plain "  the gap changes no files at all."
fi
plain "  A redeploy would change no running behavior. Not an alarm."
exit 0

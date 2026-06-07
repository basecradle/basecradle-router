#!/usr/bin/env bash
#
# deploy/deploy.sh — the one command that SHIPS the router.
#
# For this repo, "merged" is not "done": the artifact is a running service, and a
# merge to main does nothing to the box until the code is rsynced there and the
# daemon is restarted. This script is the whole Definition-of-Done loop in one
# place, so a deploy can never silently half-finish (the failure mode that produced
# issue #54):
#
#   1. TEST (offline)  — refuse to deploy unless ruff + pytest are green locally
#                        and HEAD is exactly origin/main (no dirty tree, no drift).
#   2. DEPLOY          — rsync the checkout to the box, uv sync, stamp the deployed
#                        commit SHA, restart the systemd service.
#   3. SMOKE (live)    — run deploy/smoke-test.sh against the live endpoint and
#                        FAIL LOUDLY if the running daemon misbehaves.
#   4. CONFIRM         — print the deployed SHA, the live trusted-actor list, and a
#                        deployed-vs-main drift check (which must now read "in sync").
#
# It runs from a trusted local checkout (the deploy model is rsync-from-laptop, so
# the crown-jewels box needs no GitHub token — see deploy/README.md). No infra IP
# lives in this repo; the host is the public DNS name, overridable by env.
#
# Config (env overrides):
#   ROUTER_HOST     ssh target            (default ubuntu@ai.basecradle.com)
#   ROUTER_SSH_KEY  ssh private key path  (default: your ssh config / agent)
#   SMOKE_URL       smoke-test endpoint   (default https://ai.basecradle.com/webhooks/github)
#   UP_URL          liveness endpoint     (default https://ai.basecradle.com/up)
#   FORCE=1         skip the clean-tree / HEAD==origin/main guards (emergency only)
#
set -euo pipefail

ROUTER_HOST="${ROUTER_HOST:-ubuntu@ai.basecradle.com}"
ROUTER_SSH_KEY="${ROUTER_SSH_KEY:-}"
SMOKE_URL="${SMOKE_URL:-https://ai.basecradle.com/webhooks/github}"
UP_URL="${UP_URL:-https://ai.basecradle.com/up}"
FORCE="${FORCE:-0}"
LIVENESS_BODY='<!DOCTYPE html><html><body style="background-color: green"></body></html>'

APP_DIR=/opt/basecradle-router/app
STAGING=/home/ubuntu/basecradle-router
STAMP=/etc/basecradle-router/deployed-sha
REPO_ROOT="$(git rev-parse --show-toplevel)"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
die() {
	printf '\033[1;31merror:\033[0m %s\n' "$*" >&2
	exit 1
}

ssh_opts=(-o StrictHostKeyChecking=accept-new)
[[ -n "$ROUTER_SSH_KEY" ]] && ssh_opts+=(-i "$ROUTER_SSH_KEY")
# Callers pass commands whose variables are MEANT to expand here (the deployed SHA,
# the app dir) and run as-is on the box; the client-side expansion is intentional.
# shellcheck disable=SC2029
on_box() { ssh "${ssh_opts[@]}" "$ROUTER_HOST" "$@"; }

# Files that must never reach the box (caches, the local venv, and — critically —
# any secret); --delete keeps the box tree an exact mirror of the checkout.
RSYNC_EXCLUDES=(
	--exclude '.git/' --exclude '.venv/' --exclude '__pycache__/'
	--exclude '.pytest_cache/' --exclude '.ruff_cache/'
	--exclude '*.pem' --exclude '.env' --exclude '.env.*'
)

cd "$REPO_ROOT"

# --- 1. TEST (offline) -----------------------------------------------------
log "Step 1/4 — offline gate (ruff + pytest, clean tree on origin/main)"
git fetch --quiet origin main
local_sha="$(git rev-parse HEAD)"
main_sha="$(git rev-parse origin/main)"

if [[ "$FORCE" != "1" ]]; then
	[[ -z "$(git status --porcelain)" ]] ||
		die "working tree is dirty — deploy a clean, merged tree (or FORCE=1 for an emergency)"
	[[ "$local_sha" == "$main_sha" ]] ||
		die "HEAD ($local_sha) != origin/main ($main_sha) — you would ship unmerged or stale code (or FORCE=1)"
fi

uv run ruff check . || die "ruff failed — fix lint before deploying"
uv run pytest -q || die "pytest failed — a deploy must start from a green offline test run"
green "offline gate green @ ${local_sha}"

# --- 2. DEPLOY -------------------------------------------------------------
log "Step 2/4 — deploy to ${ROUTER_HOST}"
rsync -az --delete "${RSYNC_EXCLUDES[@]}" \
	-e "ssh ${ssh_opts[*]}" "$REPO_ROOT/" "${ROUTER_HOST}:${STAGING}/"

on_box "bash -s" <<REMOTE || die "remote install failed"
set -euo pipefail
# Staging is already the clean tree (the rsync above applied the excludes), so this
# hop only mirrors staging -> app. The one thing to protect from --delete is the
# daemon's own venv, which lives in app/ but not in staging (uv sync builds it). The
# quotes are LITERAL in the heredoc and parsed by the REMOTE shell, so no client-side
# expansion strips them and no glob can leak to the remote.
sudo rsync -a --delete --exclude '.venv/' "${STAGING}/" "${APP_DIR}/"
sudo chown -R router:router "${APP_DIR}"
sudo -u router env HOME=/home/router /home/router/.local/bin/uv sync --project "${APP_DIR}"
# Reinstall the root-owned privilege-drop wrapper from the freshly-deployed tree.
# The wrapper is code ON the launch path (it sets the agent umask, drops privilege,
# execs claude), but it lives root-owned in bin/ OUTSIDE the router-owned app/ tree,
# so the app rsync above never updates it. Without this the LIVE wrapper silently
# drifts from main -- exactly the merge!=deploy failure mode #54/#55/#56 cured for
# the daemon code. Reinstalling it here, from the same trusted checkout, keeps it in
# lockstep. (The sudoers rule is intentionally NOT auto-rewritten: it changes rarely
# and a bad rule is dangerous -- it stays a documented manual step, README B2.)
# Ensure the root-owned bin/ exists first (bootstrap creates it; recreating it here
# with the same owner/mode is idempotent) so this step is self-sufficient and can
# never abort an already-mutated deploy on a box missing the dir.
sudo install -d -o root -g root -m 0755 /opt/basecradle-router/bin
sudo install -o root -g root -m 0755 "${APP_DIR}/deploy/bin/wake-runner" /opt/basecradle-router/bin/wake-runner
echo "${local_sha}" | sudo tee "${STAMP}" >/dev/null
# World-readable so the hourly drift timer (which runs as the unprivileged router
# user, whose sudoers grants only wake-runner) can read the stamp without sudo. The
# dir /etc/basecradle-router is 750 root:router, so the router user traverses it as a
# group member; the ubuntu user cannot, which is why the confirm step reads it via sudo.
sudo chmod 0644 "${STAMP}"
sudo systemctl restart basecradle-router
# Give it a moment to bind, then assert it is actually up.
sleep 3
sudo systemctl is-active --quiet basecradle-router || { sudo journalctl -u basecradle-router -n 40 --no-pager; exit 1; }
REMOTE
green "deployed ${local_sha}; service restarted and active"

# --- 3. SMOKE (live) -------------------------------------------------------
log "Step 3/4 — live smoke test"
on_box "sudo SMOKE_URL='${SMOKE_URL}' bash ${APP_DIR}/deploy/smoke-test.sh" ||
	die "LIVE SMOKE TEST FAILED — the running daemon is NOT correct. This deploy is NOT done."

# Liveness over the REAL public TLS path (Caddy -> uvicorn), from here, not the box:
# proves the deployed app actually serves the fleet-uniform GET /up and that the
# whole front-to-back chain is green. Run after the smoke test so we already know
# the daemon restarted and is enforcing its boundary.
log "  live liveness check — GET ${UP_URL}"
up_body="$(curl -fsS --max-time 15 "$UP_URL")" ||
	die "GET ${UP_URL} did not return success — the deployed app is not serving /up"
[[ "$up_body" == "$LIVENESS_BODY" ]] ||
	die "GET ${UP_URL} returned an unexpected body (liveness contract broken): ${up_body}"
green "liveness OK — ${UP_URL} is green"

# --- 4. CONFIRM ------------------------------------------------------------
log "Step 4/4 — confirm"
echo "  deployed SHA : ${local_sha}"
echo "  trusted actors (live): $(on_box "sudo grep -E '^BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS=' '${STAMP%/*}/router.env' | cut -d= -f2- || true")"
# Run via sudo, like the smoke-test step: this SSH session is `ubuntu`, who cannot
# traverse the 750 /etc/basecradle-router to read the stamp. (drift-check.sh itself
# never sudos — it also runs from the timer as `router`, which has no such grant.)
on_box "sudo bash ${APP_DIR}/deploy/drift-check.sh" || die "drift check failed immediately after deploy — investigate"

green "DONE — live daemon == ${local_sha}, #52 gate verified enforced on the box."

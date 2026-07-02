#!/usr/bin/env bash
#
# deploy/deploy.sh — RETIRED: the old rsync-from-laptop deploy loop for the router daemon.
#
# SUPERSEDED by the NOC's structured deploy op (basecradle#395 / basecradle-noc#134):
#
#     basecradle-noc deploy-router <sha>
#
# The NOC is now the fleet's sole deployer for the router daemon. The box PULLS the
# merged commit anonymously from the PUBLIC basecradle-router repo by SHA and runs
# this same Definition-of-Done loop on-box (plus a rollback this script never had),
# so the crown-jewels box needs no GitHub token and code arrives content-addressed
# rather than rsync'd from a laptop. See deploy/README.md → Part 3.
#
# The on-box contract that op consumes is still THIS repo's to define and confirmed
# in deploy/README.md: /opt/basecradle-router/app, deploy/bin/wake-runner,
# deploy/systemd/*.{service,timer}, /etc/basecradle-router/deployed-sha, and
# deploy/smoke-test.sh. The router owns the CONTRACT (the what); the NOC owns the
# deploy MECHANISM (the how). This script — the laptop-side mechanism — is retired.
#
# WHO RUNS THIS: nobody, going forward. basecradle-router AI never deployed it (the
# constitution's "One deployer for the fleet's machines: the NOC", capital PR
# basecradle#363; this repo's CLAUDE.md "Building vs. Deploying"; issue #122). The
# retirement guard below now refuses even for the NOC/capital, directing them to the
# deploy-router op. The rsync body is retained ONLY as an interim emergency fallback
# for the transition window BEFORE the deploy-router wrapper is installed on the box;
# a deployer who genuinely needs it in that gap must opt in explicitly with
# ROUTER_INTERIM_RSYNC_DEPLOY=1. Once the NOC path is live on the box, it is never
# used again.
#
# The retained loop (interim fallback only):
#   1. TEST (offline)  — refuse to deploy unless ruff + pytest are green locally
#                        and HEAD is exactly origin/main (no dirty tree, no drift).
#   2. DEPLOY          — rsync the checkout to the box, uv sync, stamp the deployed
#                        commit SHA, restart the systemd service.
#   3. SMOKE (live)    — run deploy/smoke-test.sh against the live endpoint and
#                        FAIL LOUDLY if the running daemon misbehaves.
#   4. CONFIRM         — print the deployed SHA, the live trusted-actor list, and a
#                        deployed-vs-main drift check (which must now read "in sync").
#
# No infra IP lives in this repo; the host is the public DNS name, overridable by env.
#
# Config (env overrides):
#   ROUTER_INTERIM_RSYNC_DEPLOY=1   REQUIRED to run at all — the retirement
#                   acknowledgment: this path is superseded by `basecradle-noc
#                   deploy-router <sha>`; only set this in the pre-wrapper interim gap.
#   DEPLOYER        REQUIRED — must be `noc` or `capital`. The deployer-acknowledgment
#                   guard: basecradle-router AI never deploys, so a bare run refuses.
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

# --- RETIREMENT guard: this rsync-from-laptop path is superseded ------------
# The router daemon's deploy is now the NOC's structured op `basecradle-noc
# deploy-router <sha>` (basecradle#395 / basecradle-noc#134): the box pulls the merged
# SHA anonymously from the public repo and runs this loop on-box, with rollback. This
# laptop-side rsync mechanism is RETIRED, so it refuses by default — for everyone,
# NOC and capital included. The rsync body survives ONLY as an interim emergency
# fallback for the transition window before the deploy-router wrapper is installed on
# the box; a deployer who truly needs it in that gap must opt in explicitly with
# ROUTER_INTERIM_RSYNC_DEPLOY=1.
if [[ "${ROUTER_INTERIM_RSYNC_DEPLOY:-}" != "1" ]]; then
	die "This rsync-from-laptop deploy is RETIRED — superseded by the NOC op 'basecradle-noc deploy-router <sha>' (basecradle#395 / basecradle-noc#134). Use that. If the deploy-router wrapper is not yet installed on the box and you must deploy in the interim gap, re-run with ROUTER_INTERIM_RSYNC_DEPLOY=1. If you are the router-AI: stop — you never deploy (issue #122); hand a finding/handoff to the capital instead."
fi

# --- DEPLOYER guard: the router-AI never deploys ---------------------------
# Only the NOC (the capital, in the interim) deploys the router daemon to the box.
# basecradle-router AI builds and maintains this code but never runs it against
# ai.basecradle.com (constitution: "One deployer for the fleet's machines: the NOC";
# CLAUDE.md: "Building vs. Deploying — the router-AI never deploys"; issue #122).
# A bare `deploy/deploy.sh` therefore refuses to run: the deployer must declare
# itself with DEPLOYER=noc (or DEPLOYER=capital). This is the speed-bump that stops a
# reflexive self-deploy — it is not a security boundary (the box's SSH/sudoers are),
# but it makes "the router-AI never deploys" mechanical, not just documented.
case "${DEPLOYER:-}" in
noc | capital) ;;
*) die "Refusing to deploy: basecradle-router AI NEVER deploys — only the NOC (capital, interim) does (constitution → 'One deployer for the fleet's machines: the NOC'). If you are the NOC/capital running the on-box install, re-run with DEPLOYER=noc. If you are the router-AI: stop — file a finding/handoff to the capital instead of self-deploying (issue #122)." ;;
esac

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
# and a bad rule is dangerous -- it stays a documented manual step, README Part 2.)
# Ensure the root-owned bin/ exists first (the box's provisioning lays it down, but
# recreating it here with the same owner/mode is idempotent) so this step is
# self-sufficient and can never abort an already-mutated deploy on a box missing the dir.
sudo install -d -o root -g root -m 0755 /opt/basecradle-router/bin
sudo install -o root -g root -m 0755 "${APP_DIR}/deploy/bin/wake-runner" /opt/basecradle-router/bin/wake-runner
# Install + enable the managed systemd units from the deployed tree, every deploy
# (issue #71). The units are CODE the box runs, but they live in /etc/systemd/system/
# OUTSIDE the router-owned app/ tree the rsync mirrors -- so, exactly like wake-runner
# above, a merged unit change would silently never reach the box (the unit-level
# version of the merge!=deploy gap, #54). It bit us twice: the drift alarm (#55/#58)
# and the reboot/recovery units (#66) were each merged but left uninstalled until a
# manual step. Reinstalling them here from the same trusted checkout closes that gap:
# a merged unit can never again be merged-but-not-installed. Explicit per-unit lines
# (no loop var) so every path expands client-side cleanly into this heredoc.
sudo install -o root -g root -m 0644 "${APP_DIR}/deploy/systemd/basecradle-router.service"          /etc/systemd/system/basecradle-router.service
sudo install -o root -g root -m 0644 "${APP_DIR}/deploy/systemd/basecradle-router-drift.service"    /etc/systemd/system/basecradle-router-drift.service
sudo install -o root -g root -m 0644 "${APP_DIR}/deploy/systemd/basecradle-router-drift.timer"      /etc/systemd/system/basecradle-router-drift.timer
sudo install -o root -g root -m 0644 "${APP_DIR}/deploy/systemd/basecradle-router-recovery.service" /etc/systemd/system/basecradle-router-recovery.service
sudo install -o root -g root -m 0644 "${APP_DIR}/deploy/systemd/basecradle-router-reboot.service"   /etc/systemd/system/basecradle-router-reboot.service
sudo install -o root -g root -m 0644 "${APP_DIR}/deploy/systemd/basecradle-router-reboot.timer"     /etc/systemd/system/basecradle-router-reboot.timer
sudo systemctl daemon-reload
# Enable the units meant to run. Timers get --now so they are armed immediately; the
# recovery oneshot is boot-only (enable, NOT --now -- it runs at boot, not mid-deploy);
# the daemon's restart stays the one explicit restart below, not here. The two static,
# timer-driven services (drift.service, reboot.service) are installed but never enabled
# directly. reboot.timer is armed deliberately -- automatic reboots are ON (policy
# decided, #69), so every deploy keeps the 5 AM Central timer armed.
sudo systemctl enable basecradle-router.service
sudo systemctl enable basecradle-router-recovery.service
sudo systemctl enable --now basecradle-router-drift.timer
sudo systemctl enable --now basecradle-router-reboot.timer
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
# Show the managed timers are armed (issue #71): list-timers is read-only and needs no
# sudo. Surfaces the next drift check and the next 5 AM Central auto-reboot check, so a
# deploy confirms at a glance that the alarms and the reboot policy are actually live.
echo "  managed timers (next elapse):"
on_box "systemctl list-timers --no-pager basecradle-router-drift.timer basecradle-router-reboot.timer" || true
echo "  recovery gate enabled: $(on_box "systemctl is-enabled basecradle-router-recovery.service || true")"

green "DONE — live daemon == ${local_sha}, #52 gate verified enforced on the box."

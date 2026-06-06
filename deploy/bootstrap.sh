#!/usr/bin/env bash
#
# deploy/bootstrap.sh — provision the BaseCradle fleet home server (the shared box).
#
# Idempotent: safe to re-run. Run as root on a fresh Ubuntu 24.04 LTS box.
# See deploy/README.md for the full provisioning spec this implements (Phase A3).
#
# What this DOES (the agent-independent box):
#   - install system packages: git, gh, jq, Node.js LTS + Claude Code, Caddy
#   - install uv for the `router` service user (only the daemon needs it)
#   - create the unprivileged `router` service user
#   - lay down the directory skeleton with correct owners/modes
#   - optionally lay down a per-agent user's (secret-less, repo-less) home skeleton
#     for each agent passed as an argument, e.g.  bootstrap.sh agent-ruby
#
# What this deliberately does NOT do (other issues / out-of-band):
#   - the systemd unit (#27), the wake-runner wrapper + sudoers (#28),
#     the Caddyfile + TLS (#31) — Caddy is installed here but left unconfigured;
#   - place ANY secret (the webhook signing secret, an agent's ANTHROPIC_API_KEY
#     or GitHub App key) — secrets are placed out-of-band, never in the repo;
#   - clone an agent's repo or fill agents.json — that is per-agent onboarding (#32).
#
set -euo pipefail

ROUTER_USER=router
OPT_DIR=/opt/basecradle-router
ETC_DIR=/etc/basecradle-router
# Node LTS line for Claude Code (needs >= 18); override with NODE_MAJOR=NN if newer.
NODE_MAJOR="${NODE_MAJOR:-22}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() {
	printf '\033[1;31merror:\033[0m %s\n' "$*" >&2
	exit 1
}

apt_install() {
	DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
}

require_root() {
	[[ ${EUID:-$(id -u)} -eq 0 ]] || die "run as root (use sudo)"
}

require_ubuntu() {
	[[ -r /etc/os-release ]] || die "no /etc/os-release — unsupported OS"
	# shellcheck disable=SC1091
	. /etc/os-release
	[[ ${ID:-} == ubuntu ]] || die "this script targets Ubuntu (found '${ID:-unknown}')"
}

install_base() {
	log "apt update + base packages"
	apt-get update -y
	apt_install ca-certificates curl gnupg git jq
}

install_gh() {
	if command -v gh >/dev/null 2>&1; then
		log "gh present"
		return
	fi
	log "installing gh (GitHub CLI) from the official apt repo"
	install -d -m 0755 /etc/apt/keyrings
	curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
		-o /etc/apt/keyrings/githubcli-archive-keyring.gpg
	chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
	echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
		>/etc/apt/sources.list.d/github-cli.list
	apt-get update -y
	apt_install gh
}

node_major() { node -v 2>/dev/null | sed -n 's/^v\([0-9]\{1,\}\).*/\1/p'; }

install_node_and_claude() {
	local have
	have="$(node_major)"
	if [[ -z $have || $have -lt $NODE_MAJOR ]]; then
		log "installing Node.js LTS (v$NODE_MAJOR) via NodeSource"
		curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
		apt_install nodejs
	else
		log "node present ($(node -v))"
	fi
	if command -v claude >/dev/null 2>&1; then
		log "Claude Code present"
	else
		log "installing Claude Code (npm global — available to every agent user)"
		npm install -g @anthropic-ai/claude-code
	fi
}

install_uv() {
	# Only the router daemon needs uv; install it for the router user, not system-wide.
	if sudo -u "$ROUTER_USER" test -x "/home/$ROUTER_USER/.local/bin/uv"; then
		log "uv present for $ROUTER_USER"
		return
	fi
	log "installing uv for the $ROUTER_USER user"
	sudo -u "$ROUTER_USER" sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
}

install_caddy() {
	if command -v caddy >/dev/null 2>&1; then
		log "caddy present (left unconfigured — the Caddyfile is a later step)"
		return
	fi
	log "installing Caddy from the official apt repo (unconfigured)"
	apt_install debian-keyring debian-archive-keyring apt-transport-https
	curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
		| gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
	curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
		>/etc/apt/sources.list.d/caddy-stable.list
	apt-get update -y
	apt_install caddy
}

ensure_router_user() {
	if id -u "$ROUTER_USER" >/dev/null 2>&1; then
		log "user $ROUTER_USER present"
		return
	fi
	log "creating system user $ROUTER_USER"
	useradd --system --create-home --home-dir "/home/$ROUTER_USER" \
		--shell /usr/sbin/nologin "$ROUTER_USER"
}

# Create an empty file with locked-down mode if absent; never clobber existing content.
ensure_empty_file() {
	local path="$1" owner="$2" group="$3" mode="$4"
	if [[ -e $path ]]; then
		log "keeping existing $path"
		return
	fi
	install -m "$mode" -o "$owner" -g "$group" /dev/null "$path"
	log "created empty $path ($mode) — fill out-of-band"
}

ensure_dirs() {
	log "directory skeleton"
	# /opt/basecradle-router is a ROOT-owned tree. The daemon code + venv live in
	# app/ (router-owned — the daemon deploys itself there). The root-owned
	# wake-runner wrapper lives in bin/. Keeping the tree root and bin/ root-owned
	# is load-bearing for the privilege boundary: if `router` could write bin/, it
	# could replace the wrapper that sudo runs as root. Do not make these router-owned.
	install -d -o root -g root -m 0755 "$OPT_DIR" "$OPT_DIR/bin"
	install -d -o "$ROUTER_USER" -g "$ROUTER_USER" -m 0755 "$OPT_DIR/app"
	install -d -o root -g "$ROUTER_USER" -m 0750 "$ETC_DIR"
	# router.env carries the webhook signing secret: router-owned, 0600.
	ensure_empty_file "$ETC_DIR/router.env" "$ROUTER_USER" "$ROUTER_USER" 0600
	# agents.json is the registry the wake-runner trusts as its allowlist, so it must
	# be ROOT-owned and router read-only — if router could write it, a compromised
	# daemon could poison it to aim a wake at an arbitrary user. Root writes it during
	# onboarding; the daemon only reads it.
	ensure_empty_file "$ETC_DIR/agents.json" root "$ROUTER_USER" 0640
}

# Lay down one agent's home skeleton: the OS user, a 700 home unreadable by siblings,
# its repos/ and credential dirs. No secrets, no clone — those are onboarding (#32).
provision_agent_user() {
	local user="$1"
	if id -u "$user" >/dev/null 2>&1; then
		log "agent user $user present"
	else
		log "creating agent user $user"
		useradd --create-home --home-dir "/home/$user" --shell /bin/bash "$user"
	fi
	chmod 0700 "/home/$user"
	install -d -o "$user" -g "$user" -m 0700 \
		"/home/$user/repos" "/home/$user/.config" "/home/$user/.config/basecradle"
	ensure_empty_file "/home/$user/.config/basecradle/agent.env" "$user" "$user" 0600
	log "agent user $user ready — still to do OUT-OF-BAND (onboarding, #32):"
	log "    clone its repo into /home/$user/repos, register it in $ETC_DIR/agents.json,"
	log "    and write its secrets into /home/$user/.config/basecradle/agent.env"
}

main() {
	require_root
	require_ubuntu
	install_base
	install_gh
	install_node_and_claude
	ensure_router_user
	install_uv
	install_caddy
	ensure_dirs
	for user in "$@"; do
		provision_agent_user "$user"
	done
	log "bootstrap complete."
	if [[ $# -eq 0 ]]; then
		log "no agent users requested (pass e.g. 'agent-ruby' to lay one down)."
	fi
	log "next, once the box is up (separate steps): systemd unit (#27), wake-runner"
	log "  wrapper + sudoers (#28), Caddyfile + TLS (#31), agent onboarding (#32)."
}

main "$@"

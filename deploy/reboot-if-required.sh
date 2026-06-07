#!/usr/bin/env bash
#
# deploy/reboot-if-required.sh — perform a CLEAN, observable reboot, but ONLY when
# an OS update has actually staged one.
#
# The fleet home box was found running an unpatched kernel while a newer one sat
# installed-but-not-active: `unattended-upgrades` installs security/kernel patches
# yet does NOT reboot, so a kernel fix is "applied in name only" until someone
# remembers to reboot (issue #66). This script makes that reboot a clean, scripted,
# observable step instead of a thing a human has to babysit:
#
#   1. DETECT  — do nothing unless /var/run/reboot-required exists (apt drops it
#                when an installed package needs a reboot to take effect).
#   2. DRAIN   — stop the router so in-flight wakes finish before the box goes down.
#                The unit awaits its lifespan drain on stop (bounded by the unit's
#                TimeoutStopSec), so `systemctl stop` returns only once wakes have
#                quiesced (or been killed at the timeout) — we never sever a wake
#                mid-flight if we can help it.
#   3. REBOOT  — a controlled `systemctl reboot`. The matching post-boot health
#                gate is deploy/verify-recovery.sh (run by the recovery systemd
#                unit), which confirms the box came back and alerts if it did not.
#
# WHEN it runs: automatic reboots are ON (founder decision, issue #66). The
# basecradle-router-reboot.timer drives this in a low-traffic window; it is also
# safe to run by hand anytime. Either way the reboot itself is clean, and the
# post-boot basecradle-router-recovery.service confirms the box came back (and
# alarms if it did not) — that gate is what makes the unattended reboot safe.
#
# Runs ON THE BOX as root (the reboot timer runs as root). It is a no-op on a box
# that does not need a reboot, so it is safe to run anytime.
#
# Config (env overrides — also the test seams; the daemon needs none of them):
#   REBOOT_REQUIRED_FILE  the apt flag file       (default /var/run/reboot-required)
#   REBOOT_PKGS_FILE      packages that asked     (default /var/run/reboot-required.pkgs)
#   ROUTER_SERVICE        unit to drain first     (default basecradle-router)
#   REBOOT_CMD            how to reboot           (default "systemctl reboot")
#   SKIP_ROOT_CHECK=1     bypass the root guard   (tests only)
#
set -euo pipefail

REBOOT_REQUIRED_FILE="${REBOOT_REQUIRED_FILE:-/var/run/reboot-required}"
REBOOT_PKGS_FILE="${REBOOT_PKGS_FILE:-/var/run/reboot-required.pkgs}"
ROUTER_SERVICE="${ROUTER_SERVICE:-basecradle-router}"
REBOOT_CMD="${REBOOT_CMD:-systemctl reboot}"

green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
red() { printf '\033[1;31m%s\033[0m\n' "$*"; }
log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() {
	red "reboot-if-required: $*" >&2
	exit 1
}

require_root() {
	[[ "${SKIP_ROOT_CHECK:-0}" == "1" ]] && return
	[[ ${EUID:-$(id -u)} -eq 0 ]] || die "run as root (the reboot needs it)"
}

require_root

# --- 1. DETECT -------------------------------------------------------------
if [[ ! -e "$REBOOT_REQUIRED_FILE" ]]; then
	log "no reboot required (${REBOOT_REQUIRED_FILE} absent) — nothing to do."
	exit 0
fi

log "reboot required — ${REBOOT_REQUIRED_FILE} present."
if [[ -r "$REBOOT_PKGS_FILE" ]]; then
	log "packages that requested it:"
	sed 's/^/    /' "$REBOOT_PKGS_FILE" || true
fi

# --- 2. DRAIN --------------------------------------------------------------
# Stop the router first so in-flight wakes drain cleanly before the box goes down.
# A stop failure must NOT pin an unpatched kernel forever: log loudly and proceed —
# systemd will terminate a stuck unit during the reboot anyway.
log "draining: stopping ${ROUTER_SERVICE} (lets in-flight wakes finish)..."
if systemctl stop "$ROUTER_SERVICE"; then
	green "${ROUTER_SERVICE} stopped cleanly."
else
	red "WARNING: ${ROUTER_SERVICE} did not stop cleanly; rebooting anyway (systemd will terminate it)."
fi

# --- 3. REBOOT -------------------------------------------------------------
log "rebooting now via: ${REBOOT_CMD}"
# REBOOT_CMD is intentionally word-split so the default "systemctl reboot" runs as
# a command + arg (and tests can point it at a recording stub). exec so this
# process becomes the reboot — there is nothing meaningful to do after it.
# shellcheck disable=SC2086
exec $REBOOT_CMD

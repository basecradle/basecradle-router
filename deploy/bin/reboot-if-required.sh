#!/usr/bin/env bash
#
# deploy/bin/reboot-if-required.sh — perform a CLEAN, observable reboot, but ONLY when
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
#   2. WAIT    — wait, bounded, for the router to be IDLE before anything stops (#557).
#                `systemctl stop basecradle-router` closes the daemon's port at once and
#                only then waits for every running wake to end, so a stop issued under a
#                wake holds /up and every delivery down for as long as that wake runs
#                (2026-11-27, Better Stack incident 1058963686: 4 min 8 s of 502 from
#                the same stop, started by another trigger). Waiting first keeps the port
#                open and deliveries arriving while the wake finishes; once the router is
#                idle the stop takes seconds.
#   3. DEFER   — a router still busy when the wait ends defers the reboot to the next
#                slot, on a line a person will read, for a bounded time: a reboot whose
#                first deferral is REBOOT_MAX_DEFER_S (36 h) old is not deferred again,
#                and runs at that slot whatever is running — with the daily timer, the
#                third slot. The bound is a time, never a count, so a run by hand cannot
#                spend it. A kernel update never waits forever, and never in silence.
#   4. DRAIN   — stop the router so in-flight wakes finish before the box goes down.
#                The unit awaits its lifespan drain on stop (bounded by the unit's
#                TimeoutStopSec), so `systemctl stop` returns only once wakes have
#                quiesced (or been killed at the timeout) — we never sever a wake
#                mid-flight if we can help it. After the wait above this has nothing
#                to wait for, except on the overdue reboot, or for a wake that arrived
#                in the moment between the last idle reading and the stop.
#   5. REBOOT  — a controlled `systemctl reboot`. The matching post-boot health
#                gate is deploy/verify-recovery.sh (run by the recovery systemd
#                unit), which confirms the box came back and alerts if it did not.
#
# IDLE is the router's own statement, never an inference: each agent's pending-wake
# depth in the evidence document the daemon writes, the state its drain waits on, read
# by `router-admin pending` (the CLI's strict reader, basecradle_router.evidence
# .read_pending_wakes) — the same field the NOC's deploy gate reads before it restarts
# the daemon (basecradle-noc#695). Counting processes would be a second spelling of
# "busy", and it disagrees with the drain both ways. The reader runs as the daemon's
# own user, dropped with `setpriv` (it execs, never forks, so the `timeout` bounding a
# reading signals the reader itself and opens no PAM session), so this root script
# resolves no path the daemon's account can steer and holds none of the daemon's
# configuration. Anything that reading cannot vouch for is NOT idle: it is waited on
# like a busy router, and then deferred. A daemon in systemd's auto-restart (crashed,
# waiting to start again) has nothing in flight, so it is idle.
#
# THE DEFERRAL RECORD is one file, $REBOOT_STATE_DIR/deferrals ("<first deferral, epoch
# seconds> <the same, UTC>"), on the /run tmpfs. It lives exactly as long as the pending reboot
# it counts: the reboot clears /run, and a run that finds no reboot pending removes the
# record and its directory. A record that cannot be read or kept (garbled, a link, not
# root's, dated in the future) cannot bound a deferral, so no deferral is taken on it.
#
# BY HAND: `systemctl start basecradle-router-reboot.service` runs exactly what the
# timer runs, so it waits for idle and may defer. To reboot now whatever is running,
# run the script as root with REBOOT_IDLE_WAIT_S=0 REBOOT_MAX_DEFER_S=0.
#
# WHEN it runs: automatic reboots are ON (founder decision, issue #66). The
# basecradle-router-reboot.timer drives this in a low-traffic window; it is also
# safe to run by hand anytime. Either way the reboot itself is clean, and the
# post-boot basecradle-router-recovery.service confirms the box came back (and
# alarms if it did not) — that gate is what makes the unattended reboot safe.
#
# Runs ON THE BOX as root (the reboot timer runs as root) — root is genuine here:
# `systemctl stop` and `systemctl reboot` are not things an unprivileged account can
# do. BECAUSE it runs as root it lives in deploy/bin/ and is executed from the
# root-owned install path /opt/basecradle-router/bin/reboot-if-required.sh, never out
# of the mirrored /opt/basecradle-router/app tree whose ownership another repo's deploy
# op decides (#297, basecradle-noc#777). The router-admin it runs is that install
# path's root-owned copy too. It is a no-op on a box that does not need a reboot, so
# it is safe to run anytime.
#
# Config (env overrides — also the test seams; the daemon needs none of them):
#   REBOOT_REQUIRED_FILE   the apt flag file         (default /var/run/reboot-required)
#   REBOOT_PKGS_FILE       packages that asked       (default /var/run/reboot-required.pkgs)
#   ROUTER_SERVICE         unit to drain first       (default basecradle-router)
#   ROUTER_ADMIN           the idle reader           (default /opt/basecradle-router/bin/router-admin)
#   ROUTER_USER            the daemon's account      (default router)
#   REBOOT_IDLE_WAIT_S     longest wait for idle     (default 1800)
#   REBOOT_IDLE_POLL_S     between idle readings     (default 15)
#   REBOOT_READ_TIMEOUT_S  longest one reading       (default 60, then 5 s to be killed)
#   REBOOT_MAX_DEFER_S     longest deferral          (default 129600, 36 h)
#   REBOOT_STATE_DIR       the deferral record's dir (default /run/basecradle-router-reboot)
#   REBOOT_CMD             how to reboot             (default "systemctl reboot")
#   SKIP_ROOT_CHECK=1      bypass the root guard     (tests only)
#
# The unit's TimeoutStartSec covers the longest run: REBOOT_IDLE_WAIT_S, one more poll
# and reading, then the router's TimeoutStopSec drain (tests/test_shutdown_drain.py).
#
set -euo pipefail

REBOOT_REQUIRED_FILE="${REBOOT_REQUIRED_FILE:-/var/run/reboot-required}"
REBOOT_PKGS_FILE="${REBOOT_PKGS_FILE:-/var/run/reboot-required.pkgs}"
ROUTER_SERVICE="${ROUTER_SERVICE:-basecradle-router}"
ROUTER_ADMIN="${ROUTER_ADMIN:-/opt/basecradle-router/bin/router-admin}"
ROUTER_USER="${ROUTER_USER:-router}"
REBOOT_IDLE_WAIT_S="${REBOOT_IDLE_WAIT_S:-1800}"
REBOOT_IDLE_POLL_S="${REBOOT_IDLE_POLL_S:-15}"
REBOOT_READ_TIMEOUT_S="${REBOOT_READ_TIMEOUT_S:-60}"
REBOOT_MAX_DEFER_S="${REBOOT_MAX_DEFER_S:-129600}"
REBOOT_STATE_DIR="${REBOOT_STATE_DIR:-/run/basecradle-router-reboot}"
REBOOT_CMD="${REBOOT_CMD:-systemctl reboot}"

DEFERRALS_FILE="$REBOOT_STATE_DIR/deferrals"
# The reading's stderr, kept apart from its stdout (the reading itself). Made by mktemp,
# removed on every way out: the EXIT trap, and by hand before the reboot's exec.
READ_ERR=""

green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
red() { printf '\033[1;31m%s\033[0m\n' "$*"; }
log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() {
	red "reboot-if-required: $*" >&2
	exit 1
}

# notice — the line a person reads: a deferral, or a reboot that will not defer again.
# Plain text, no colour, so it greps; at WARNING priority in the journal, where systemd
# reads a leading <4> as the line's level (JOURNAL_STREAM says stdout is the journal).
notice() {
	if [[ -n "${JOURNAL_STREAM:-}" ]]; then
		printf '<4>reboot-if-required: %s\n' "$*"
	else
		printf 'reboot-if-required: %s\n' "$*"
	fi
}

require_root() {
	[[ "${SKIP_ROOT_CHECK:-0}" == "1" ]] && return
	[[ ${EUID:-$(id -u)} -eq 0 ]] || die "run as root (the reboot needs it)"
}

require_count() {
	[[ "$2" =~ ^[0-9]+$ ]] || die "$1 must be a whole number of ${3}, not '$2'"
}

# read_pending — one reading of the router's pending wakes. Sets PENDING_TOTAL and
# PENDING_AGENTS ("<agent>=<n>,…", empty when idle) and returns 0; or sets PENDING_ERROR
# and returns 1 when it cannot tell. Globals, so the reason survives (no subshell).
read_pending() {
	local out err rc=0 shape='^([0-9]+)( ([^[:space:]]+))?$'
	PENDING_TOTAL="" PENDING_AGENTS="" PENDING_ERROR=""
	out="$(timeout --kill-after=5 "$REBOOT_READ_TIMEOUT_S" \
		setpriv --reuid="$ROUTER_USER" --regid="$ROUTER_USER" --init-groups --reset-env -- \
		"$ROUTER_ADMIN" pending 2>"$READ_ERR")" || rc=$?
	if ((rc != 0)); then
		err="$(tail -n 1 -- "$READ_ERR" 2>/dev/null || true)"
		PENDING_ERROR="router-admin pending exited ${rc}${err:+: ${err:0:200}}"
		return 1
	fi
	if [[ ! "$out" =~ $shape ]]; then
		PENDING_ERROR="router-admin pending printed no reading this script recognises"
		return 1
	fi
	PENDING_TOTAL="${BASH_REMATCH[1]}"
	PENDING_AGENTS="${BASH_REMATCH[3]}"
}

# router_state — the daemon's ActiveState, with its SubState appended ("activating/
# auto-restart") only while it is activating, where the two mean different things: a
# start under way, or a crashed daemon waiting to start again. Empty when unreadable.
# One property per call: `systemctl show` prints several in its own order, not ours.
router_state() {
	local active sub
	active="$(systemctl show "$ROUTER_SERVICE" -p ActiveState --value 2>/dev/null || true)"
	if [[ "$active" == activating ]]; then
		sub="$(systemctl show "$ROUTER_SERVICE" -p SubState --value 2>/dev/null || true)"
		active+="${sub:+/$sub}"
	fi
	printf '%s\n' "$active"
}

# wait_for_idle <max-seconds> — block until the router daemon has NOTHING to drain,
# reading every $REBOOT_IDLE_POLL_S. Returns 0 idle · 1 still busy at the bound · 2 still
# unable to tell at the bound, and sets IDLE_DETAIL to a one-line reason either way
# (agent slugs and counts only). A stopped or failed daemon has nothing in flight. One
# already activating or deactivating is not idle: someone else's start or stop is under
# way. A reading that cannot tell is read again, since it may be passing.
wait_for_idle() {
	local bound=$1 waited=0 state rc started=$SECONDS
	IDLE_DETAIL=""
	while :; do
		state="$(router_state)"
		case "$state" in
		inactive | failed | activating/auto-restart | activating/auto-restart-queued)
			IDLE_DETAIL="$ROUTER_SERVICE is ${state}, so nothing is in flight to drain"
			return 0
			;;
		active | reloading)
			if read_pending; then
				if [[ "$PENDING_TOTAL" == 0 ]]; then
					IDLE_DETAIL="no wake queued or in flight (waited ${waited}s)"
					return 0
				fi
				rc=1
				IDLE_DETAIL="$PENDING_TOTAL wake(s) queued or in flight after ${waited}s ($PENDING_AGENTS)"
			else
				rc=2
				IDLE_DETAIL="whether $ROUTER_SERVICE is idle could not be read after ${waited}s ($PENDING_ERROR)"
			fi
			;;
		"")
			rc=2
			IDLE_DETAIL="the state of $ROUTER_SERVICE could not be read after ${waited}s"
			;;
		*)
			rc=1
			IDLE_DETAIL="$ROUTER_SERVICE is $state after ${waited}s (a start or stop is already under way)"
			;;
		esac
		# Bounded by the count of waits and by the clock, whichever ends first: a reading
		# takes time of its own, and the unit's TimeoutStartSec is sized on this bound.
		if ((waited >= bound || SECONDS - started >= bound)); then
			return "$rc"
		fi
		sleep "$REBOOT_IDLE_POLL_S"
		waited=$((waited + REBOOT_IDLE_POLL_S))
	done
}

# state_dir_ok — the deferral record's directory exists, is a real directory (never a
# link), and is root's (the running account's). Made 0700 when absent.
state_dir_ok() {
	[[ -e "$REBOOT_STATE_DIR" || -L "$REBOOT_STATE_DIR" ]] || mkdir -m 0700 -- "$REBOOT_STATE_DIR" 2>/dev/null || true
	[[ -d "$REBOOT_STATE_DIR" && ! -L "$REBOOT_STATE_DIR" && -O "$REBOOT_STATE_DIR" ]]
}

# read_deferrals — sets DEFERRED_AT (epoch seconds) and DEFERRED_SINCE (UTC), both ""
# when no deferral has been taken, and returns 0; returns 1 when the record exists but
# cannot be vouched for.
read_deferrals() {
	local line shape='^([0-9]+) ([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)$'
	DEFERRED_AT="" DEFERRED_SINCE=""
	state_dir_ok || return 1
	if [[ ! -e "$DEFERRALS_FILE" && ! -L "$DEFERRALS_FILE" ]]; then
		return 0
	fi
	[[ -f "$DEFERRALS_FILE" && ! -L "$DEFERRALS_FILE" ]] || return 1
	{ IFS= read -r line || true; } <"$DEFERRALS_FILE" || return 1
	[[ "$line" =~ $shape ]] || return 1
	DEFERRED_AT="${BASH_REMATCH[1]}"
	DEFERRED_SINCE="${BASH_REMATCH[2]}"
}

# write_deferrals <epoch> <utc> — replace the record whole, through one fixed-name
# temporary file in the same directory.
write_deferrals() {
	state_dir_ok || return 1
	printf '%s %s\n' "$1" "$2" >"$DEFERRALS_FILE.tmp" && mv -f -- "$DEFERRALS_FILE.tmp" "$DEFERRALS_FILE"
}

# clear_deferrals — no reboot is pending, so no deferral is either: remove the record
# and its directory. Only a real, root-owned directory is touched; anything else at the
# path is left alone.
clear_deferrals() {
	[[ -d "$REBOOT_STATE_DIR" && ! -L "$REBOOT_STATE_DIR" && -O "$REBOOT_STATE_DIR" ]] || return 0
	rm -f -- "$DEFERRALS_FILE" "$DEFERRALS_FILE.tmp"
	rmdir -- "$REBOOT_STATE_DIR" 2>/dev/null || true
}

require_root
require_count REBOOT_IDLE_WAIT_S "$REBOOT_IDLE_WAIT_S" seconds
require_count REBOOT_IDLE_POLL_S "$REBOOT_IDLE_POLL_S" seconds
require_count REBOOT_READ_TIMEOUT_S "$REBOOT_READ_TIMEOUT_S" seconds
require_count REBOOT_MAX_DEFER_S "$REBOOT_MAX_DEFER_S" seconds
((REBOOT_IDLE_POLL_S > 0)) || die "REBOOT_IDLE_POLL_S must be at least 1 second"
((REBOOT_READ_TIMEOUT_S > 0)) || die "REBOOT_READ_TIMEOUT_S must be at least 1 second"

# --- 1. DETECT -------------------------------------------------------------
if [[ ! -e "$REBOOT_REQUIRED_FILE" ]]; then
	clear_deferrals
	log "no reboot required (${REBOOT_REQUIRED_FILE} absent) — nothing to do."
	exit 0
fi

log "reboot required — ${REBOOT_REQUIRED_FILE} present."
if [[ -r "$REBOOT_PKGS_FILE" ]]; then
	log "packages that requested it:"
	sed 's/^/    /' "$REBOOT_PKGS_FILE" || true
fi

# --- 2. WAIT ---------------------------------------------------------------
READ_ERR="$(mktemp "${TMPDIR:-/tmp}/reboot-if-required.XXXXXX")" || die "cannot make a temporary file for the idle reading"
trap 'rm -f -- "$READ_ERR"' EXIT
log "waiting up to ${REBOOT_IDLE_WAIT_S}s for ${ROUTER_SERVICE} to be idle before anything stops (the port stays open meanwhile)..."
idle_rc=0
wait_for_idle "$REBOOT_IDLE_WAIT_S" || idle_rc=$?

# --- 3. DEFER --------------------------------------------------------------
if ((idle_rc == 0)); then
	green "${ROUTER_SERVICE} is idle: ${IDLE_DETAIL}."
else
	now="$(date -u +%s)"
	if ! read_deferrals; then
		overdue="the deferral record under ${REBOOT_STATE_DIR} cannot be read or kept, so a deferral could not be bounded"
	elif ((REBOOT_MAX_DEFER_S == 0)); then
		overdue="deferral is disabled (REBOOT_MAX_DEFER_S=0)"
	elif [[ -z "$DEFERRED_AT" ]]; then
		since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
		if write_deferrals "$now" "$since"; then
			notice "event=reboot_deferred deferred_since=${since} deferred_for_s=0 max_defer_s=${REBOOT_MAX_DEFER_S}: the reboot waits for the next slot, because ${IDLE_DETAIL}; once it has waited ${REBOOT_MAX_DEFER_S}s it runs whatever is running"
			exit 0
		fi
		overdue="the deferral could not be recorded under ${REBOOT_STATE_DIR}, so it could not be bounded"
	elif ((now < DEFERRED_AT)); then
		overdue="the deferral record is dated in the future (${DEFERRED_SINCE}), so a deferral could not be bounded"
	elif ((now - DEFERRED_AT < REBOOT_MAX_DEFER_S)); then
		notice "event=reboot_deferred deferred_since=${DEFERRED_SINCE} deferred_for_s=$((now - DEFERRED_AT)) max_defer_s=${REBOOT_MAX_DEFER_S}: the reboot waits for the next slot, because ${IDLE_DETAIL}; once it has waited ${REBOOT_MAX_DEFER_S}s it runs whatever is running"
		exit 0
	else
		overdue="it has been deferred since ${DEFERRED_SINCE} ($((now - DEFERRED_AT))s), the longest allowed (${REBOOT_MAX_DEFER_S}s)"
	fi
	notice "event=reboot_overdue max_defer_s=${REBOOT_MAX_DEFER_S}: rebooting anyway, because ${overdue}; ${IDLE_DETAIL}; the stop below closes the port until the drain ends"
fi

# --- 4. DRAIN --------------------------------------------------------------
# Stop the router first so in-flight wakes drain cleanly before the box goes down.
# A stop failure must NOT pin an unpatched kernel forever: log loudly and proceed —
# systemd will terminate a stuck unit during the reboot anyway.
log "draining: stopping ${ROUTER_SERVICE} (lets in-flight wakes finish)..."
if systemctl stop "$ROUTER_SERVICE"; then
	green "${ROUTER_SERVICE} stopped cleanly."
else
	red "WARNING: ${ROUTER_SERVICE} did not stop cleanly; rebooting anyway (systemd will terminate it)."
fi

# --- 5. REBOOT -------------------------------------------------------------
log "rebooting now via: ${REBOOT_CMD}"
# REBOOT_CMD is intentionally word-split so the default "systemctl reboot" runs as
# a command + arg (and tests can point it at a recording stub). exec so this
# process becomes the reboot — there is nothing meaningful to do after it.
rm -f -- "$READ_ERR"
# shellcheck disable=SC2086
exec $REBOOT_CMD

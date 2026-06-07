#!/usr/bin/env bash
#
# deploy/verify-recovery.sh — after a (re)boot, confirm the fleet home came back
# HEALTHY, and make it LOUD if it did not.
#
# The danger a clean reboot introduces is a box that reboots and never fully comes
# back — a service that failed to start, an app bound but wedged. This is the
# post-boot health gate that pairs with deploy/reboot-if-required.sh (issue #66):
# it asserts the services are active AND the app's liveness route is green, then
# exits nonzero + loud on any failure. Wired as a oneshot systemd unit
# (basecradle-router-recovery.service, WantedBy=multi-user.target), a failure lands
# in `systemctl --failed` and the journal — the same alarm convention as
# drift-check.sh — instead of the box silently serving nothing.
#
# It checks /up on the LOCAL app (127.0.0.1:8000), not the public URL: a green /up
# straight from uvicorn proves the daemon itself recovered, independent of Caddy,
# DNS, or TLS (which the service check covers separately via the caddy unit).
#
# It POLLS rather than checks once: `After=` only orders unit *start*, it does not
# wait for health, and Restart=on-failure means a unit can flap for a few seconds
# right after boot. So each check retries up to a bounded budget before failing.
#
# Runs ON THE BOX. Safe to run by hand anytime (read-only): `deploy/verify-recovery.sh`.
#
# Config (env overrides — also the test seams):
#   SERVICES        space-separated units to require active  (default "caddy basecradle-router")
#   UP_URL          liveness endpoint to poll                (default http://127.0.0.1:8000/up)
#   RETRIES         poll attempts per check                  (default 30)
#   RETRY_INTERVAL  seconds between attempts                 (default 5)  -> ~145s of
#                   waiting in the common case (curl to a down localhost refuses
#                   instantly). A hung endpoint can extend it; the recovery unit's
#                   TimeoutStartSec is the hard cap, and a timeout-kill still marks
#                   the unit failed — it still alarms, the intended outcome.
#
set -euo pipefail

SERVICES="${SERVICES:-caddy basecradle-router}"
UP_URL="${UP_URL:-http://127.0.0.1:8000/up}"
RETRIES="${RETRIES:-30}"
RETRY_INTERVAL="${RETRY_INTERVAL:-5}"

# The verbatim Rails health body the app serves at /up (see server.py LIVENESS_BODY
# and deploy/deploy.sh). A 200 alone is not enough — we assert the exact contract.
LIVENESS_BODY='<!DOCTYPE html><html><body style="background-color: green"></body></html>'

green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
red() { printf '\033[1;31m%s\033[0m\n' "$*"; }
log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# Wait for a systemd unit to report active, retrying to ride out post-boot flap.
# Sleep BETWEEN attempts only, never after the last one (no wasted tail wait).
check_service() {
	local svc=$1 i
	for ((i = 1; i <= RETRIES; i++)); do
		if systemctl is-active --quiet "$svc"; then
			green "  service ${svc}: active"
			return 0
		fi
		if ((i < RETRIES)); then sleep "$RETRY_INTERVAL"; fi
	done
	red "  service ${svc}: NOT active after ${RETRIES} attempts"
	return 1
}

# Wait for /up to return exactly the green liveness body.
check_liveness() {
	local i body=""
	for ((i = 1; i <= RETRIES; i++)); do
		body="$(curl -fsS --max-time 10 "$UP_URL" 2>/dev/null || true)"
		if [[ "$body" == "$LIVENESS_BODY" ]]; then
			green "  liveness ${UP_URL}: green"
			return 0
		fi
		if ((i < RETRIES)); then sleep "$RETRY_INTERVAL"; fi
	done
	red "  liveness ${UP_URL}: NOT green after ${RETRIES} attempts (last body: ${body:-<none>})"
	return 1
}

log "verifying recovery: services [${SERVICES}] + liveness ${UP_URL}"

rc=0
read -ra svc_list <<<"$SERVICES"
for svc in "${svc_list[@]}"; do
	check_service "$svc" || rc=1
done
check_liveness || rc=1

echo
if [[ $rc -eq 0 ]]; then
	green "RECOVERY OK — services active and /up green."
else
	red "RECOVERY FAILED — the box did not come back healthy. Investigate now."
fi
exit $rc

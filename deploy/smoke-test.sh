#!/usr/bin/env bash
#
# deploy/smoke-test.sh — the LIVE post-deploy smoke test.
#
# Proves the running daemon on the box actually enforces the security boundary —
# not that the code merged, but that the bytes serving traffic right now behave.
# It POSTs three synthetic, GitHub-shaped webhooks at the live endpoint and
# asserts the response of each:
#
#   1. bad signature                      -> 401  (verify rejects; nothing spoofed)
#   2. valid sig, UNTRUSTED sender        -> 400  (the #52 trusted-actor gate rejects)
#   3. valid sig, TRUSTED sender,         -> 200  (gate ALLOWS the trusted actor; the
#      UNREGISTERED repo                          event is accepted, then resolve finds
#                                                 no agent, so NO wake fires — safe)
#
# Case 3 deliberately targets a repo that is not in the registry: it exercises the
# whole accept path past the trust gate WITHOUT waking any real agent. Together,
# (2) and (3) prove the gate both rejects strangers and admits the fleet; (1)
# proves the HMAC boundary in front of it. This is the test whose ABSENCE let the
# box drift to pre-#52 code undetected (issue #54).
#
# Case 3 also drives a verified handoff through normalize, so it logs an INFO
# `decision=woke` line. The test then asserts that line actually reached journald —
# the LIVE half of #91, because the decision logging being merged + unit-tested is
# not enough: on the box those INFO records were silently dropped, so the
# observability meant to catch a dead capability was itself dead. (Case 5 asserts
# the same for the basecradle route.)
#
# Once the capital wires the basecradle route, two more cases prove its boundary
# the same way — both safe against production (they never target @jt's real uuid):
#
#   4. bad signature                      -> 401  (shared HMAC boundary rejects)
#   5. valid sig, UNREGISTERED recipient  -> 200  (verify admits; resolve finds no
#                                                  agent, so NO wake fires — safe)
#
# These self-gate: skipped if the basecradle secret is unset, and skipped if the
# running daemon does not yet serve the route, so a deploy made before the capital
# enables basecradle stays green.
#
# Runs ON THE BOX (it reads the signing secret + trusted-actor list from
# router.env, which is root-readable only). deploy.sh invokes it over SSH after a
# restart; you can also run it by hand:  sudo deploy/smoke-test.sh
#
# Config (env overrides):
#   ROUTER_ENV_FILE   path to the daemon env file   (default /etc/basecradle-router/router.env)
#   SMOKE_URL         the endpoint to hit            (default https://ai.basecradle.com/webhooks/github)
#
# It never waits on a wake and never touches a real agent, so it is safe to run
# against production at any time.
#
set -euo pipefail

ROUTER_ENV_FILE="${ROUTER_ENV_FILE:-/etc/basecradle-router/router.env}"
SMOKE_URL="${SMOKE_URL:-https://ai.basecradle.com/webhooks/github}"

# A repo that is never in the registry, so case 3 resolves to no agent => no wake.
UNREGISTERED_REPO="basecradle/__router-smoke-test-do-not-register__"
UNTRUSTED_LOGIN="router-smoke-untrusted-actor"

green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
red() { printf '\033[1;31m%s\033[0m\n' "$*"; }
log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() {
	red "smoke-test: $*" >&2
	exit 1
}

[[ -r "$ROUTER_ENV_FILE" ]] || die "cannot read $ROUTER_ENV_FILE (run with sudo / as root)"

# Pull only the values we need; do not source arbitrary lines into the shell. Value
# of the first `KEY=` line, or empty if absent — so the friendly die() below fires.
#
# Pure bash, deliberately: no pipeline, so no stage can short-circuit a producer into
# a SIGPIPE that `pipefail` would promote to the whole pipeline's status. That class
# of bug rejected a healthy daemon in #172; `tests/test_shell_pipeline_safety.py`
# now bans the shape across this repo's shell scripts.
#
# The markers let tests/test_smoke_test_assertions.py run these EXACT shipped bodies
# offline, so a regression here fails in CI rather than on the box.
# >>> env_parsers >>>
#
# Both end in an explicit `return 0`: a missing key must yield "" and let the friendly
# die() below fire. Without it the function would return its last command's status, and
# a future edit ending on a false test would abort the whole gate under `set -e` with no
# message — the same silent-status class as #172 itself. (The old code bought this with
# a trailing `|| true`.)
env_value() {
	local key=$1 line
	while IFS= read -r line || [[ -n "$line" ]]; do
		if [[ "$line" == "${key}="* ]]; then
			printf '%s' "${line#"${key}="}"
			return 0
		fi
	done <"$ROUTER_ENV_FILE"
	return 0
}

# First non-empty, trimmed entry of a comma-separated list.
first_entry() {
	local raw=$1 entry
	local -a entries=()
	IFS=',' read -ra entries <<<"$raw" || true
	# `${a[@]+"${a[@]}"}` is the portable empty-array expansion: a bare `"${a[@]}"` is an
	# "unbound variable" error under `set -u` on bash < 4.4 (macOS ships 3.2), so an empty
	# list would kill the gate rather than reach its die().
	for entry in ${entries[@]+"${entries[@]}"}; do
		entry="${entry#"${entry%%[![:space:]]*}"}"
		entry="${entry%"${entry##*[![:space:]]}"}"
		if [[ -n "$entry" ]]; then
			printf '%s' "$entry"
			return 0
		fi
	done
	return 0
}
# <<< env_parsers <<<

secret="$(env_value BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET)"
actors="$(env_value BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS)"
[[ -n "$secret" ]] || die "BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET not set in $ROUTER_ENV_FILE"
[[ -n "$actors" ]] || die "BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS not set in $ROUTER_ENV_FILE"

# First non-empty, trimmed trusted actor — a real fleet login the gate must admit.
trusted_login="$(first_entry "$actors")"
[[ -n "$trusted_login" ]] || die "trusted-actor list is empty after parsing"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

# Emit a GitHub `issues` "labeled handoff" payload for the given sender + repo.
payload() {
	local sender=$1 repo=$2
	cat <<-JSON
		{"action":"labeled",
		 "label":{"name":"handoff"},
		 "issue":{"number":1,
		          "html_url":"https://github.com/${repo}/issues/1",
		          "title":"Router live smoke test (no-op)",
		          "labels":[{"name":"handoff"}]},
		 "repository":{"full_name":"${repo}"},
		 "sender":{"login":"${sender}"}}
	JSON
}

sign() { openssl dgst -sha256 -hmac "$secret" "$1" | awk '{print $NF}'; }

# POST a body file with a given signature header; echo the HTTP status code.
post() {
	local body_file=$1 signature=$2
	curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
		-X POST "$SMOKE_URL" \
		-H 'Content-Type: application/json' \
		-H 'X-GitHub-Event: issues' \
		-H 'X-GitHub-Delivery: smoke-00000000-0000-0000-0000-000000000000' \
		-H "X-Hub-Signature-256: sha256=${signature}" \
		--data-binary "@${body_file}"
}

rc=0
check() {
	local name=$1 expect=$2 got=$3
	if [[ "$got" == "$expect" ]]; then
		green "  PASS  ${name}: HTTP ${got} (expected ${expect})"
	else
		red "  FAIL  ${name}: HTTP ${got} (EXPECTED ${expect})"
		rc=1
	fi
}

# Assert the running daemon actually EMITTED a log line matching $2 to journald
# since $3 — the live half of #91. The decision logging being merged and unit-
# tested is not enough: on the box it was silently dropped at WARNING, so a dead
# capability read as healthy. This proves the INFO record reached the operator's
# journal. A short retry absorbs journald ingestion lag (the record is written
# synchronously during the request, but ingestion is a beat behind the HTTP ack).
#
# The journal is CAPTURED first, then matched — never piped straight into a matcher
# that short-circuits. Piping a live `journalctl` into an early-exiting consumer
# kills the producer with SIGPIPE (141) the moment the match is found, and `pipefail`
# then promotes that 141 to the pipeline's status: the assertion reads FALSE even
# though the line is present. It is position-dependent, so it looked like a real
# failure — a match near the END of the stream passed (nothing left to write) while
# a match EARLIER in it always failed. That is what rejected a healthy #170 daemon
# and rolled it back (#172). Matching a captured string has no live producer to kill.
#
# tests/test_smoke_test_assertions.py runs this EXACT body against a fake journal that
# keeps writing after the match — the case that failed on the box.
# >>> assert_journal_has >>>
assert_journal_has() {
	local name=$1 pattern=$2 since=$3 journal
	for _ in 1 2 3 4 5; do
		journal="$(journalctl -u basecradle-router --since "$since" --no-pager 2>/dev/null || true)"
		if grep -qE "$pattern" <<<"$journal"; then
			green "  PASS  ${name}: decision line present in journald"
			return 0
		fi
		sleep 1
	done
	red "  FAIL  ${name}: no journald line /${pattern}/ since ${since}"
	red "        decision logging is wired but NOT emitting at the deployed level (#91)"
	rc=1
}
# <<< assert_journal_has <<<

log "Smoke-testing live daemon at ${SMOKE_URL}"
log "Trusted actor under test: ${trusted_login}"

# Journald boundary for the observability assertions below: capture it before any
# POST so a decision line emitted by the cases that follow is guaranteed >= it.
obs_since="$(date '+%Y-%m-%d %H:%M:%S')"

# Case 1 — bad signature. Body is otherwise valid; the signature is garbage.
payload "$trusted_login" "$UNREGISTERED_REPO" >"$workdir/c1.json"
check "bad signature rejected" 401 "$(post "$workdir/c1.json" "0000000000000000000000000000000000000000000000000000000000000000")"

# Case 2 — valid signature, UNTRUSTED sender. The #52 gate must reject (no wake).
payload "$UNTRUSTED_LOGIN" "$UNREGISTERED_REPO" >"$workdir/c2.json"
check "untrusted sender rejected (#52 gate)" 400 "$(post "$workdir/c2.json" "$(sign "$workdir/c2.json")")"

# Case 3 — valid signature, TRUSTED sender, unregistered repo. Gate admits it;
# resolve finds no agent, so it is accepted-and-logged with NO wake.
payload "$trusted_login" "$UNREGISTERED_REPO" >"$workdir/c3.json"
check "trusted sender admitted, no agent => no wake" 200 "$(post "$workdir/c3.json" "$(sign "$workdir/c3.json")")"

# Case 3 also drove a verified handoff through normalize, which logs a WOKE
# decision line (recipient = the unregistered repo) before resolve drops it. Assert
# that INFO line actually reached journald — the live observability check (#91).
assert_journal_has "github decision line emitted (#91)" \
	"event=delivery_decision source=github .*decision=woke .*recipient=${UNREGISTERED_REPO}" "$obs_since"

# The same line must carry the DELIVERY ID (#170) — the key that joins this route
# decision to the core's stage lines and to the wake's own journal. Asserted against
# the exact id this smoke run POSTed, so a passing regex proves the id is threaded
# through from the header, not merely that some `delivery=` field exists.
assert_journal_has "delivery id threaded onto the decision line (#170)" \
	"event=delivery_decision source=github .*delivery=smoke-00000000-0000-0000-0000-000000000000" \
	"$obs_since"

# --- basecradle route (only once the capital has wired it) -----------------
#
# The same security-boundary proof for the basecradle route, and equally safe
# against production: it never targets @jt's real uuid, so it never wakes a real
# harness. It self-gates twice — skipped if the secret is unset, and skipped if
# the live daemon does not yet serve the route (a bad-sig probe returns 404, not
# 401) — so a deploy made before the capital enables basecradle stays green.
BC_URL="${BC_URL:-${SMOKE_URL%/*}/basecradle}"
# A uuid that is never a registered recipient, so the valid-sig case wakes nothing.
UNREGISTERED_RECIPIENT="00000000-0000-7000-8000-000000000000"
SMOKE_TIMELINE="00000000-0000-7000-8000-0000000000aa"

bc_secret="$(env_value BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET)"

bc_payload() {
	local recipient=$1
	cat <<-JSON
		{"event":"message.created",
		 "event_id":"smoke-00000000-0000-0000-0000-000000000000",
		 "occurred_at":"2026-01-01T00:00:00Z",
		 "actor_uuid":null,
		 "recipient_uuid":"${recipient}",
		 "timeline_uuid":"${SMOKE_TIMELINE}",
		 "resource":{"type":"message","uuid":"${SMOKE_TIMELINE}","url":"https://ai.basecradle.com/smoke"}}
	JSON
}

bc_sign() { openssl dgst -sha256 -hmac "$bc_secret" "$1" | awk '{print $NF}'; }

bc_post() {
	local body_file=$1 signature=$2
	curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
		-X POST "$BC_URL" \
		-H 'Content-Type: application/json' \
		-H 'X-BaseCradle-Event: message.created' \
		-H 'X-BaseCradle-Delivery: smoke-00000000-0000-0000-0000-000000000000' \
		-H "X-BaseCradle-Signature: sha256=${signature}" \
		--data-binary "@${body_file}"
}

if [[ -z "$bc_secret" ]]; then
	log "basecradle route: secret not set in $ROUTER_ENV_FILE — not wired yet; skipping"
else
	bc_payload "$UNREGISTERED_RECIPIENT" >"$workdir/bc.json"
	probe="$(bc_post "$workdir/bc.json" "0000000000000000000000000000000000000000000000000000000000000000")"
	if [[ "$probe" == "404" ]]; then
		log "basecradle route: not enabled on the running daemon (404); skipping"
	else
		# Case 4 — bad signature. The shared HMAC boundary must reject it.
		check "basecradle bad signature rejected" 401 "$probe"
		# Case 5 — valid signature, unregistered recipient. Verify admits it; resolve
		# finds no agent, so it is accepted-and-logged with NO wake (safe vs prod).
		check "basecradle valid sig, unknown recipient => no wake" 200 \
			"$(bc_post "$workdir/bc.json" "$(bc_sign "$workdir/bc.json")")"
		# Case 5 normalized a message.created, logging a WOKE decision line for the
		# unregistered recipient. Assert the basecradle route's INFO observability
		# reaches journald too — this is the route the #91 silent-drop was found on.
		assert_journal_has "basecradle decision line emitted (#91)" \
			"event=delivery_decision source=basecradle .*decision=woke .*recipient=${UNREGISTERED_RECIPIENT}" \
			"$obs_since"
	fi
fi

echo
if [[ $rc -eq 0 ]]; then
	green "LIVE SMOKE TEST PASSED — the running daemon enforces the #52 trust gate."
else
	red "LIVE SMOKE TEST FAILED — the running daemon does NOT behave as expected. Do NOT consider this deploy done."
fi
exit $rc

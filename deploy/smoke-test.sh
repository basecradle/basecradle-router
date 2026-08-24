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
# Once the capital wires the basecradle route, three more cases prove its boundary
# the same way, and all three are safe against production — nothing they send can be
# normalized into a wake:
#
#   4. REGISTERED recipient, FORGED digest -> 401  (the HMAC boundary rejects)
#   5. the same body, VALID digest, a       -> 200  (verify admits on the recipient's own
#      deliberately NON-ACTIONABLE event           key; normalize IGNORES it — no wake)
#   6. UNREGISTERED recipient, signed with  -> 401 while the shared fallback is retired,
#      the ROUTE-WIDE secret                      200 while it is armed
#
# These self-gate: skipped if the basecradle route-wide secret is unset, and skipped if
# the running daemon does not yet serve the route, so a deploy made before the capital
# enables basecradle stays green.
#
# Two more prove the synthetic wake's injection point is where it is supposed to be —
# on the box and nowhere else (#208):
#
#   7. the PUBLIC /webhooks/probe         -> 404  (Caddy denies it; ALWAYS asserted)
#   8. LOOPBACK, bad signature            -> 401  (served locally, HMAC rejects)
#
# Case 7 is a security assertion, not a liveness one, so unlike every other route case
# it is asserted unconditionally: the probe route can fire a wake at any registered
# agent, so its reachability from the internet must never quietly become true.
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

# An agent registry key as the env-var suffix its per-recipient signing key is
# provisioned under: upper-cased, with every character outside [A-Za-z0-9_] replaced by
# `_` (`jt` -> `JT`, `glm-5.2` -> `GLM_5_2`). Mirrors the daemon's own
# `basecradle_router.routes.basecradle._slug_suffix`, and cannot drift from it:
# tests/test_smoke_test_assertions.py runs THIS body and that function over one table of
# keys and asserts they agree. Written with `tr` rather than `${x^^}` because macOS
# ships bash 3.2, where the parameter-expansion form is a syntax error.
slug_suffix() {
	printf '%s' "$1" | tr '[:lower:]' '[:upper:]' | LC_ALL=C tr -c 'A-Za-z0-9_' '_'
}

# A boolean router.env value, parsed the way the daemon's `_bool_env` parses it: unset or
# blank is $2, `1|true|yes|on` is true, `0|false|no|off` is false, and anything else is a
# non-zero return so the caller can die naming the variable. Loud on purpose — the daemon
# refuses to boot on an unrecognised value, so a gate that quietly read
# `…SHARED_SECRET_FALLBACK=flase` as "the fallback is still armed" would assert the
# opposite of the box it is testing.
bool_env() {
	local raw
	raw="$(env_value "$1")"
	raw="${raw#"${raw%%[![:space:]]*}"}"
	raw="${raw%"${raw##*[![:space:]]}"}"
	raw="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')"
	case "$raw" in
	"") printf '%s' "$2" ;;
	1 | true | yes | on) printf 'true' ;;
	0 | false | no | off) printf 'false' ;;
	*) return 1 ;;
	esac
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
# The same security-boundary proof for the basecradle route, and equally safe against
# production. It self-gates twice — skipped if the route-wide secret is unset, and
# skipped if the live daemon does not yet serve the route (a bad-sig probe returns 404,
# not 401) — so a deploy made before the capital enables basecradle stays green.
#
# The delivery under test is signed for a REGISTERED persona under that persona's OWN
# `…_WEBHOOK_SECRET_<SLUG>` key, and carries a deliberately NON-ACTIONABLE event type.
# That pairing is what keeps it safe against production after the per-recipient keyring
# cutover (basecradle/basecradle#497): verify runs the real `key_path=recipient` path,
# and normalize then IGNORES the delivery, so nothing is resolved and no harness is ever
# woken. The persona and its key are DISCOVERED from the box — the registry
# (`BASECRADLE_ROUTER_AGENTS`) paired with router.env, read exactly the way
# `load_recipient_keyring` reads them — never hardcoded.
#
# It used to sign an *unregistered* recipient uuid with the route-wide secret and expect
# 200. That asserted the shared-secret fallback, which the cutover retired: post-cutover
# `RecipientKeyring.select` holds no key for an unknown recipient and answers 401, so the
# case failed on every deploy AND on every rollback, hard-blocking `deploy-router` at
# every SHA (#243). Gating it on the flag would have been the smaller change and would
# have surrendered the proof — with the fallback off, a forged digest and an unknown
# recipient both answer 401, so the pair could no longer tell the HMAC boundary rejecting
# a forgery from the keyring simply holding no key. The three cases below separate them
# by construction:
#
#   4. registered recipient, FORGED digest        -> 401  (the HMAC boundary rejects)
#   5. the SAME body, VALID digest, non-actionable -> 200  (key_path=recipient verifies;
#                                                           normalize ignores => no wake)
#   6. UNREGISTERED recipient, signed with the    -> 401 while the shared fallback is
#      ROUTE-WIDE secret                                retired, 200 while it is armed
#
# 4 and 5 differ in the digest alone, so 401-vs-200 there is the HMAC boundary and nothing
# else. 5 and 6 differ in the recipient alone, so their split is the keyring's key
# selection. And 6 is the live proof of which configuration the box is actually in — the
# one thing no per-delivery line can reveal, because once every persona holds its own key
# an armed fallback and a retired one produce byte-identical traffic.
#
# What is deliberately NOT proven live any more: an actionable basecradle delivery
# reaching resolve. Post-cutover that would mean signing as a registered persona with an
# actionable event — which is precisely the delivery that wakes a real harness — so it
# stays offline (tests/test_basecradle_route.py, tests/test_server_e2e.py). The github
# route still covers accept -> normalize -> resolve-miss live, in case 3.
BC_URL="${BC_URL:-${SMOKE_URL%/*}/basecradle}"
BC_SECRET_VAR="BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET"
# The daemon derives a per-recipient variable as the route-wide name plus `_<SLUG>`
# (`config.route_secret_var` + `basecradle.RECIPIENT_SECRET_PREFIX`), so deriving it from
# the same one string here keeps a rename from orphaning this gate silently.
BC_RECIPIENT_SECRET_PREFIX="${BC_SECRET_VAR}_"
BC_FALLBACK_VAR="BASECRADLE_ROUTER_BASECRADLE_SHARED_SECRET_FALLBACK"
BC_AGENTS_VAR="BASECRADLE_ROUTER_AGENTS"
# A uuid no registry entry can hold, so no per-recipient key exists for it: case 6 reads
# the shared fallback's state and can never reach a real persona.
UNREGISTERED_RECIPIENT="00000000-0000-7000-8000-000000000000"
SMOKE_TIMELINE="00000000-0000-7000-8000-0000000000aa"
# A platform event the router deliberately does NOT act on, so a delivery that verifies is
# then a logged ignore and nothing is woken. This is the ONLY thing standing between case
# 5 and a live wake at a real persona, so it is pinned rather than remembered:
# tests/test_smoke_test_assertions.py reads this exact value out of this script and fails
# if it ever enters the route's `_ACTIONABLE_EVENTS`.
BC_IGNORED_EVENT="timeline.locked"
BC_DELIVERY="smoke-basecradle-00000000-0000-0000-0000-000000000000"
BC_FALLBACK_DELIVERY="smoke-basecradle-fallback-00000000-0000-0000-0000-000000000000"

bc_secret="$(env_value "$BC_SECRET_VAR")"

bc_payload() {
	local recipient=$1 delivery=$2
	cat <<-JSON
		{"event":"${BC_IGNORED_EVENT}",
		 "event_id":"${delivery}",
		 "occurred_at":"2026-01-01T00:00:00Z",
		 "actor_uuid":null,
		 "recipient_uuid":"${recipient}",
		 "timeline_uuid":"${SMOKE_TIMELINE}",
		 "resource":{"type":"timeline","uuid":"${SMOKE_TIMELINE}","url":"https://ai.basecradle.com/smoke"}}
	JSON
}

# Sign a body with an EXPLICIT key — the whole point of the per-recipient keyring is that
# there is no longer one route secret, so the key is an argument and never an ambient.
bc_sign() { openssl dgst -sha256 -hmac "$2" "$1" | awk '{print $NF}'; }

bc_post() {
	local body_file=$1 signature=$2 delivery=$3
	curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
		-X POST "$BC_URL" \
		-H 'Content-Type: application/json' \
		-H "X-BaseCradle-Event: ${BC_IGNORED_EVENT}" \
		-H "X-BaseCradle-Delivery: ${delivery}" \
		-H "X-BaseCradle-Signature: sha256=${signature}" \
		--data-binary "@${body_file}"
}

# The registry's harness personas as `<key><TAB><recipient_uuid>` lines, in key order.
# The registry is the daemon's own authority on which uuid is which persona — the same
# file `load_recipient_keyring` resolves a `…_WEBHOOK_SECRET_<SLUG>` variable through — so
# reading it is what lets this gate name a real recipient without hardcoding a persona.
# `jq` is already a hard requirement of the box (deploy/bin/wake-runner validates every
# wake against this same registry with it).
#
# The markers let tests/test_smoke_test_assertions.py run these EXACT shipped bodies
# offline against a fabricated registry, so a regression fails in CI, not on the box.
# >>> registry_parsers >>>
registry_personas() {
	jq -r '
		to_entries
		| sort_by(.key)
		| .[]
		| select(.value.kind == "harness" and (.value.recipient_uuid | type) == "string")
		| "\(.key)\t\(.value.recipient_uuid)"
	' "$1"
}

# The first registered persona holding a per-recipient signing key on THIS box, as
# bc_slug / bc_uuid / bc_key. All three are left empty when no persona has a key of its
# own — a pre-cutover box, where the shared fallback is necessarily still armed, because
# `load_recipient_keyring` refuses to boot a daemon whose fallback is retired while any
# registered persona is unprovisioned.
#
# The registry output is CAPTURED before it is iterated: `break`ing out of a loop reading
# straight from a live producer SIGPIPEs it, which under `pipefail` is the #172 class of
# bug this repo bans outright (tests/test_shell_pipeline_safety.py).
discover_recipient() {
	local personas slug uuid var value
	bc_slug=""
	bc_uuid=""
	bc_key=""
	personas="$(registry_personas "$1")"
	while IFS=$'\t' read -r slug uuid; do
		[[ -n "$slug" && -n "$uuid" ]] || continue
		var="${BC_RECIPIENT_SECRET_PREFIX}$(slug_suffix "$slug")"
		value="$(env_value "$var")"
		if [[ -n "$value" ]]; then
			bc_slug="$slug"
			bc_uuid="$uuid"
			bc_key="$value"
			return 0
		fi
	done <<<"$personas"
	return 0
}
# <<< registry_parsers <<<

if [[ -z "$bc_secret" ]]; then
	log "basecradle route: $BC_SECRET_VAR not set in $ROUTER_ENV_FILE — not wired yet; skipping"
else
	bc_fallback="$(bool_env "$BC_FALLBACK_VAR" true)" ||
		die "$BC_FALLBACK_VAR is set to a value the daemon would refuse to boot on: $(env_value "$BC_FALLBACK_VAR")"

	bc_payload "$UNREGISTERED_RECIPIENT" "$BC_FALLBACK_DELIVERY" >"$workdir/bc-unregistered.json"
	# Route-enabled probe: an unsigned delivery answers 404 only when the daemon does not
	# serve the route at all. Everything else it can answer is a real assertion — an
	# unsigned delivery for an unknown recipient is rejected whatever state the fallback
	# is in (no key for it when retired, a digest mismatch when armed) — but it is a
	# config-INDEPENDENT one, which is exactly why it cannot double as case 4 post-cutover.
	probe="$(bc_post "$workdir/bc-unregistered.json" "0000000000000000000000000000000000000000000000000000000000000000" "$BC_FALLBACK_DELIVERY")"
	if [[ "$probe" == "404" ]]; then
		log "basecradle route: not enabled on the running daemon (404); skipping"
	else
		check "basecradle unsigned delivery rejected" 401 "$probe"
		command -v jq >/dev/null 2>&1 ||
			die "jq is required to read the agent registry (deploy/bin/wake-runner requires it too)"
		bc_agents="$(env_value "$BC_AGENTS_VAR")"
		[[ -n "$bc_agents" ]] || die "$BC_AGENTS_VAR not set in $ROUTER_ENV_FILE"
		[[ -r "$bc_agents" ]] || die "cannot read the agent registry at $bc_agents"
		discover_recipient "$bc_agents"

		if [[ -n "$bc_key" ]]; then
			bc_case_recipient="$bc_uuid"
			bc_case_secret="$bc_key"
			bc_case_key_path="recipient"
			log "basecradle recipient under test: ${bc_slug} (its own per-recipient key)"
		elif [[ "$bc_fallback" == "true" ]]; then
			# Pre-cutover: no persona holds a key of its own yet, so the route-wide secret
			# is the only key there is and the unregistered uuid is still safe to sign for.
			bc_case_recipient="$UNREGISTERED_RECIPIENT"
			bc_case_secret="$bc_secret"
			bc_case_key_path="fallback"
			log "basecradle recipient under test: none provisioned yet; using the shared fallback"
		else
			die "no per-recipient signing key is provisioned and $BC_FALLBACK_VAR is off — the daemon could not have booted in this state"
		fi

		bc_payload "$bc_case_recipient" "$BC_DELIVERY" >"$workdir/bc.json"

		# Case 4 — a FORGED digest on the very body case 5 sends. 401 here and 200 there
		# differ in the digest alone, so this pair is the HMAC boundary and nothing else.
		check "basecradle forged digest rejected (key_path=${bc_case_key_path})" 401 \
			"$(bc_post "$workdir/bc.json" "0000000000000000000000000000000000000000000000000000000000000000" "$BC_DELIVERY")"

		# Case 5 — the same body, correctly signed. Verify admits it; the event is not in
		# the actionable set, so normalize records a deliberate ignore and NOTHING wakes.
		check "basecradle valid sig, non-actionable event => no wake" 200 \
			"$(bc_post "$workdir/bc.json" "$(bc_sign "$workdir/bc.json" "$bc_case_secret")" "$BC_DELIVERY")"

		# ...and it verified under the key we expected it to. This is the live proof that
		# the per-recipient keyring is in force on the box: the status code alone cannot
		# tell `key_path=recipient` from `key_path=fallback`.
		assert_journal_has "basecradle verified on key_path=${bc_case_key_path}" \
			"event=verify_key source=basecradle .*key_path=${bc_case_key_path} .*delivery=${BC_DELIVERY}" \
			"$obs_since"

		# The route's INFO observability must reach journald too — this is the route the
		# #91 silent drop was found on, and the decision here is an *ignore*, which is
		# exactly the outcome that must never be indistinguishable from a silent drop.
		assert_journal_has "basecradle decision line emitted (#91)" \
			"event=delivery_decision source=basecradle event_type=${BC_IGNORED_EVENT} decision=ignored .*delivery=${BC_DELIVERY}" \
			"$obs_since"

		# Case 6 — an unregistered recipient signed with the ROUTE-WIDE secret: the live
		# state of the shared fallback, which nothing else on the box states per delivery.
		if [[ "$bc_case_recipient" == "$UNREGISTERED_RECIPIENT" ]]; then
			log "basecradle shared fallback: already asserted by case 5 on this box; skipping"
		elif [[ "$bc_fallback" == "true" ]]; then
			check "basecradle shared fallback armed, unknown recipient => no wake" 200 \
				"$(bc_post "$workdir/bc-unregistered.json" "$(bc_sign "$workdir/bc-unregistered.json" "$bc_secret")" "$BC_FALLBACK_DELIVERY")"
		else
			check "basecradle retired shared fallback refuses an unknown recipient" 401 \
				"$(bc_post "$workdir/bc-unregistered.json" "$(bc_sign "$workdir/bc-unregistered.json" "$bc_secret")" "$BC_FALLBACK_DELIVERY")"
		fi
	fi
fi

# --- probe route: the synthetic wake's injection point (#208) ---------------
#
# Two cases, and the first is the one that matters most:
#
#   7. the PUBLIC endpoint            -> 404  (Caddy denies it; the injection point is
#                                              on-box only, never internet-reachable)
#   8. LOOPBACK, bad signature        -> 401  (the daemon serves it locally and the
#                                              shared HMAC boundary rejects)
#
# Case 7 is a boundary assertion, not a liveness one: the probe route can fire a wake
# at ANY registered agent, so the one thing that must never be true is that it is
# reachable from the internet behind a single shared secret. It is asserted against the
# same public URL every other case uses, so a Caddyfile that lost the deny fails here.
#
# Case 8 self-gates like basecradle's: skipped if the secret is unset, and skipped if
# the running daemon does not serve the route (a bad-sig probe returns 404, not 401).
# Both cases are safe against production — neither carries a valid signature, so
# nothing is ever normalized and no agent is ever woken.
PROBE_PUBLIC_URL="${PROBE_PUBLIC_URL:-${SMOKE_URL%/*}/probe}"
PROBE_LOCAL_URL="${PROBE_LOCAL_URL:-http://127.0.0.1:8000/webhooks/probe}"

probe_post() {
	local url=$1
	curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
		-X POST "$url" \
		-H 'Content-Type: application/json' \
		-H 'X-BaseCradle-Probe-Event: probe.wake' \
		-H 'X-BaseCradle-Delivery: smoke-probe' \
		-H 'X-BaseCradle-Probe-Delivery: smoke-probe' \
		-H 'X-BaseCradle-Probe-Signature: sha256=0000000000000000000000000000000000000000000000000000000000000000' \
		--data-binary '{"harness_key":"__router-smoke-test-no-such-agent__","marker":"nope"}'
}

# Case 7 — ALWAYS asserted, secret or not: the public surface must deny this path
# whether or not the route is enabled on the daemon behind it.
check "probe injection point not reachable from the internet" 404 "$(probe_post "$PROBE_PUBLIC_URL")"

probe_secret="$(env_value BASECRADLE_ROUTER_PROBE_WEBHOOK_SECRET)"
if [[ -z "$probe_secret" ]]; then
	log "probe route: secret not set in $ROUTER_ENV_FILE — not wired yet; skipping the loopback case"
else
	probe_local="$(probe_post "$PROBE_LOCAL_URL")"
	if [[ "$probe_local" == "404" ]]; then
		log "probe route: not enabled on the running daemon (404); skipping"
	else
		# Case 8 — the daemon serves it on loopback and rejects an unsigned delivery.
		check "probe bad signature rejected on loopback" 401 "$probe_local"
	fi
fi

echo
if [[ $rc -eq 0 ]]; then
	green "LIVE SMOKE TEST PASSED — the running daemon enforces the #52 trust gate."
else
	red "LIVE SMOKE TEST FAILED — the running daemon does NOT behave as expected. Do NOT consider this deploy done."
fi
exit $rc

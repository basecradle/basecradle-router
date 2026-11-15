#!/usr/bin/env bash
#
# ping-heartbeat.sh — ping the Better Stack heartbeat whose URL is in $HEARTBEAT_URL,
# without ever putting that URL on a command line.
#
# A heartbeat URL is the credential: anyone holding it can keep a monitor green. Both
# scheduled workflows that report to one (the dependency scan and the public repository
# lockdown) used to run `curl … "$HEARTBEAT_URL"`, which put it on curl's argv, readable
# by every process on the runner for the life of the call (basecradle-router#468, a
# sample of the class basecradle/basecradle#853 found in the deploy smoke test). Here it
# reaches curl as a config on stdin (`--config -`), through a here-string, so the only
# argv is the fixed flags. It is also unset before curl starts, so curl's own
# environment does not carry a copy either.
#
# `-q` comes first, as curl requires: it stops curl reading a `.curlrc`, whose options
# would otherwise join the ping (a `verbose` or a `write-out "%{url}"` would print the
# URL this script exists to keep out of sight).
#
# The URL must be a plain https URL: no whitespace, no double quote, no backslash. That
# is every URL Better Stack issues, and it is what makes the one quoted config line safe
# to write without an escaping pass. A refusal names what is wrong and never the URL.
#
# The caller decides WHETHER to ping (a dry run, a withheld verdict); this only pings.
#
# Usage: HEARTBEAT_URL=<url> ping-heartbeat.sh
# Exit:  0 = pinged · 64 (EX_USAGE, a code curl never returns) = HEARTBEAT_URL unset or
#        not a plain https URL · anything else = curl's own code
#
set -euo pipefail

refuse() {
	echo "ping-heartbeat: HEARTBEAT_URL $* (the URL is never printed)" >&2
	exit 64
}

url="${HEARTBEAT_URL:-}"
unset HEARTBEAT_URL
[[ -n "$url" ]] || refuse "is not set"
[[ "$url" == https://?* ]] || refuse "is not an https URL"
[[ "$url" != *[[:space:]]* ]] || refuse "contains whitespace"
[[ "$url" != *[\"\\]* ]] || refuse "contains a double quote or a backslash"

curl -q -fsS --retry 3 --max-time 15 -o /dev/null --config - <<<"url = \"$url\""

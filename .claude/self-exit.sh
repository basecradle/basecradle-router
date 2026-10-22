#!/usr/bin/env bash
# Bounded self-exit for a laptop builder agent.
#
# Two things happen here, in this order, and the order is the point:
#
#   1. THE CLEANUP GUARD RUNS FIRST and can refuse. A session that has
#      self-exited cannot clean up after itself, so anything it left on the
#      laptop — a container, an image, a Docker Desktop it started, a browser
#      tab — outlives it and becomes a founder's problem (basecradle-noc#790:
#      @origin found Docker Desktop running and a Chrome tab open, left by
#      sessions that were already gone). Refusing costs a session a few
#      seconds; not refusing cost a founder, a second session, an engine
#      restart and an app update.
#
#      There is deliberately NO override flag. A session that genuinely cannot
#      clean up is *blocked on a human*, which is a legitimate state to stay
#      open in (CLAUDE.md -> Attended-Session Lifecycle Signal) — never a
#      reason to exit quietly. "I mean to leave this behind" and "I forgot"
#      must not be one byte apart.
#
#   2. SIGTERM ONLY this session's own `claude` process, found by walking this
#      script's own ancestry. It cannot target an arbitrary PID — if no
#      ancestor `claude` is found it refuses and exits non-zero.
#
# Laptop-builder-only. The capital that spawned this session is watching and
# will observe it end. Removed on migration to the fleet server, together with
# .claude/session-guard.sh (the router manages server-agent lifecycle).
set -uo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
guard="$here/session-guard.sh"

if [ -x "$guard" ]; then
  # `done` quoted: bare, it reads to the shell linter as a loop keyword (SC1010).
  if ! "$guard" "done"; then
    echo >&2
    echo "self-exit: REFUSING to exit — this session has not cleaned up after itself (see above)." >&2
    echo "self-exit: fix it, then run this again. Do not work around this; stay open and say what is blocking." >&2
    exit 1
  fi
else
  echo "self-exit: $guard is missing — refusing to exit without a cleanup check." >&2
  exit 1
fi

set -e
pid=$$
target=""
while [ -n "$pid" ] && [ "$pid" != "0" ] && [ "$pid" != "1" ]; do
  cmd=$(ps -o command= -p "$pid" 2>/dev/null || true)
  case "$cmd" in
    claude|claude\ *|*/claude|*/claude\ *) target="$pid"; break ;;
  esac
  pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)
done

if [ -z "$target" ]; then
  echo "self-exit: no ancestor 'claude' process found — refusing to kill anything." >&2
  exit 1
fi

echo "self-exit: terminating this session's own claude process (PID $target)."
kill -TERM "$target"

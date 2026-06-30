#!/usr/bin/env bash
# Bounded self-exit for a laptop builder agent.
# SIGTERMs ONLY this session's own `claude` process, found by walking this
# script's own ancestry. It cannot target an arbitrary PID — if no ancestor
# `claude` is found it refuses and exits non-zero.
#
# Laptop-builder-only. The capital that spawned this session is watching and
# will observe it end. Removed on migration to the fleet server (the router
# manages server-agent lifecycle).
set -euo pipefail

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

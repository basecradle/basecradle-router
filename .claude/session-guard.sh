#!/usr/bin/env bash
# Session cleanup guard for a laptop builder agent (basecradle-noc#790).
#
# WHY THIS EXISTS
# ---------------
# On 2026-09-20 @origin found Docker Desktop still running on his laptop and a
# Chrome tab still open, both left by builder sessions that had already
# self-exited. The engine had degraded to answering 500 to every API call, so
# nothing could even be listed; a second session, an engine restart and an app
# update were the price of cleaning up after a session that was already gone.
#
# *Whatever creates, cleans up* (constitution.md -> How We Build) says every
# leftover has an owner and a stated end, and that the cleanup ships with the
# thing that creates it. A session that has self-exited cannot clean up, so the
# cleanup has to happen BEFORE the exit — and be checked there, not remembered.
#
# THE TWO HALVES
# --------------
#   GROUND TRUTH  for Docker, for Docker Desktop and for this account's listening
#                 TCP ports (a dev server). The daemon, the process list and the
#                 kernel's socket table are authoritative, so `check` compares what is there now
#                 against a baseline taken at session start and needs nobody to
#                 have written anything down.
#   A RECORD      for browser tabs. A tab carries no attribution we can read,
#                 and censusing @origin's own tabs is not ours to do, so the tab
#                 half rests on `tab-open`/`tab-close` plus the mandatory
#                 `tabs-checked` attestation. That residual is named in
#                 CLAUDE.md rather than papered over.
#
# THE FAIL-CLOSED HINGE: `check` refuses when there is NO baseline. A guard that
# no-ops when it cannot run is the silent death itself (basecradle-noc#190), so
# a session whose SessionStart hook never fired cannot exit quietly — it is told
# so. That is also why `begin` is a hook rather than a step to remember.
#
# PROVE-IT-IS-OURS, OR REPORT: the sweep removes only what carries this
# session's own label; anything else that appeared during the session is
# REPORTED with the exact command and never touched. An image @origin pulled
# mid-session is his. "Clean the hotel's towels; never the guest's suitcase."
# There is deliberately no `docker system prune` anywhere in this file.
#
# ONE DOCKER DESKTOP, EVERY BUILDER (basecradle-noc#1255). Every laptop builder runs
# under one macOS account and there is one Docker Desktop, so "it was running when I
# began" is not "it is the laptop's own": on 2026-11-04 the router builder began beside
# a harness builder's Docker Desktop, read it as the founder's, and once both had quit
# their own copies its check demanded `docker desktop start`, re-creating a leftover
# nothing would ever quit; the harness builder, its own leftovers gone, could not exit
# past the router builder's Docker Desktop at all. So a session that starts Docker
# Desktop records it in a LAPTOP-WIDE LEDGER, one file per session under a root no
# carrier's name is in, holding its repo and the identity (pid + start time) of every
# process under the bundle. Whose a running Docker Desktop is, is then read, never
# assumed: one a live sibling's record proves is that sibling's, never this session's
# to quit or to restore; one whose recorder has ended is ADOPTED and quit; one that
# no record accounts for was running before any builder session (the founder's), and
# is never quit. The first session out still quits the Docker Desktop it started,
# even under a sibling that joined it; that sibling's next `docker-up` starts its own.
#
# LAPTOP-ONLY. Removed on migration to the fleet server along with
# .claude/self-exit.sh — a server wake has no Docker Desktop and no browser.
# On a box with no docker CLI every Docker check is inert by construction.
#
# ONE SCRIPT, EVERY LAPTOP BUILDER (basecradle-noc#1110). This file, self-exit.sh
# and the SessionStart hook are carried byte for byte by every laptop builder and
# anchored in basecradle-noc, whose shared-block drift guard diffs them. Hand-synced
# copies bounced defects between three repos for three rounds before that. The one
# thing that differs per repo, its name, is read from .claude/session-guard.env
# beside this file, and the label, the builder prefix and the state root are
# derived from it. Change this file in basecradle-noc and re-sync; never edit a copy.

set -uo pipefail

GUARD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARD_CONFIG="$GUARD_DIR/session-guard.env"

# Read, never sourced: one `SESSION_GUARD_REPO=<repo>` line, and nothing else that
# is not a comment or blank. A missing or malformed config refuses every command, so
# a carrier that forgot it has no baseline and `check` says so; it never runs under
# some other repo's label.
read_repo() {
  local lines value
  [ -r "$GUARD_CONFIG" ] || { echo "session-guard: $GUARD_CONFIG is missing — it names this repo (SESSION_GUARD_REPO=<repo>)." >&2; return 1; }
  lines="$(grep -v -E '^[[:space:]]*(#|$)' "$GUARD_CONFIG")"
  value="$(sed -n 's/^SESSION_GUARD_REPO=\([a-z0-9][a-z0-9.-]*\)$/\1/p' <<< "$lines")"
  if [ -z "$value" ] || [ "$lines" != "SESSION_GUARD_REPO=$value" ]; then
    echo "session-guard: $GUARD_CONFIG must hold exactly one line SESSION_GUARD_REPO=<repo> (lowercase letters, digits, '.', '-')." >&2
    return 1
  fi
  echo "$value"
}
REPO_NAME="$(read_repo)" || exit 78

LABEL_KEY="${REPO_NAME}-builder"
LABEL="${LABEL_KEY}=1"
BUILDER_PREFIX="${REPO_NAME}-builder-"

# Docker Desktop's own processes all live under this bundle path. Matching the
# PATH (not a pattern typed into a command line) is what keeps the probe from
# matching its own argv — the trap that made a `pgrep -f` wait loop report a
# finished job as in-flight for a day (basecradle-noc#694). /Library/Privileged-
# HelperTools/com.docker.vmnetd is a LaunchDaemon that stays up regardless and
# is excluded by construction, since it is not under the bundle.
DOCKER_APP_PREFIX="${SESSION_GUARD_DOCKER_APP_PREFIX:-/Applications/Docker.app/}"

# Whatever creates, cleans up — turned on this guard's own state.
STALE_STATE_DAYS=7

# How long a stop/start is given before it is reported as not having happened.
# The stop bound is what turns "we asked it to quit" into "it is gone".
STOP_WAIT_SECONDS="${SESSION_GUARD_STOP_WAIT:-90}"
START_WAIT_SECONDS="${SESSION_GUARD_START_WAIT:-180}"
POLL_SECONDS="${SESSION_GUARD_POLL:-3}"

usage() {
  cat >&2 <<'USAGE'
usage: session-guard.sh <command> [args]

  begin [--session-id ID]   Census the laptop and write this session's baseline.
                            Run by the SessionStart hook; never a step to remember.
  docker-up                 Start Docker Desktop if it is down and record that
                            THIS session started it. The only sanctioned way to
                            start it — `open -a Docker` leaves no record, so the
                            quit can never be proved to be ours.
  tab-open <id> [note]      Record a browser tab this session opened.
  tab-close <id>            Record that it was closed.
  baseline-listeners        A late listening-port baseline, for a session that began
                            before the guard censused ports (attested by hand)
  tabs-checked              Attest that the browser was looked at (one
                            tabs_context_mcp call) and holds no session tab.
  sweep                     Remove this session's labelled containers, images and
                            throwaway builders; quit Docker Desktop if this
                            session started it, reading the stop back.
  check                     Exit 0 only when this session left nothing behind.
  done                      check, then clear this session's state.
USAGE
  exit 64
}

# ---------------------------------------------------------------- session key

# What a `claude` process looks like on a process list: the one spelling, shared by the session key
# below and by the listener attribution (basecradle-noc#1230). self-exit.sh carries the same case.
is_claude_command() {
  case "$1" in
    claude|claude\ *|*/claude|*/claude\ *) return 0 ;;
  esac
  return 1
}

ancestor_claude_pid() {
  local pid=$$ cmd ppid
  while [ -n "$pid" ] && [ "$pid" != "0" ] && [ "$pid" != "1" ]; do
    cmd=$(ps -o command= -p "$pid" 2>/dev/null || true)
    if is_claude_command "$cmd"; then echo "$pid"; return 0; fi
    ppid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)
    [ "$ppid" = "$pid" ] && break
    pid=$ppid
  done
  return 1
}

SESSION_ID_OVERRIDE=""

# A process's identity: its pid plus its start time, which a recycled pid cannot share. `lstart`
# is a fixed string the kernel derives once, so it reads the same on every call (`etime`, read
# against the clock, could move by a second between two reads and split one session into two).
# It is RENDERED in the caller's time zone and locale, though, and a Bash call sources the user's
# profile where the hook may not, so both are pinned: one process, one key, whoever asks.
proc_identity() {
  local started
  started="$(LC_ALL=C TZ=UTC ps -o lstart= -p "$1" 2>/dev/null | tr -cd 'A-Za-z0-9')"
  [ -n "$started" ] || return 1
  echo "$1-$started"
}

# THE KEY IS THE `claude` PROCESS, not the conversation (basecradle#707 (d)). What this guard
# certifies is what one `claude` process left on the laptop between its start and its self-exit.
# `/clear` starts a new session id inside the SAME process (Claude Code updates both the hook's
# `session_id` and CLAUDE_CODE_SESSION_ID on it), so a session-id key took a fresh baseline after
# a `/clear` and orphaned `docker_started_by_session` and the tab ledger: a Docker Desktop the
# session started then sat in the new baseline as pre-existing and was never quit — #790 itself.
# Claude Code sets CLAUDE_PID, its own pid, in every hook and Bash command it spawns, so the hook
# and every later call agree on this key by construction. A session id is the fallback, for a
# Claude Code that predates CLAUDE_PID — never for a CLAUDE_PID whose start time could not be read
# (basecradle#710 item 1). One failed `ps` used to drop that run onto the session-id key, so its
# writes (a Docker Desktop it started) landed in a directory no later call reads, and its lsof
# stderr redirect into that missing directory read as "no listeners". So the key is computed ONCE
# per run, and a CLAUDE_PID that cannot be read refuses the run instead of moving it.
compute_session_key() {
  local pid ident
  if [ -n "${CLAUDE_PID:-}" ]; then
    if ident="$(proc_identity "$CLAUDE_PID")"; then
      echo "claude-$ident"; return 0
    fi
    echo "session-guard: CLAUDE_PID=$CLAUDE_PID is set but its start time cannot be read, so this session's key cannot be computed — refusing rather than running under another key. Re-run it." >&2
    return 1
  fi
  if [ -n "$SESSION_ID_OVERRIDE" ]; then echo "$SESSION_ID_OVERRIDE"; return; fi
  if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then echo "$CLAUDE_CODE_SESSION_ID"; return; fi
  if pid=$(ancestor_claude_pid); then echo "pid-$pid"; return; fi
  echo "no-session"
}

SESSION_KEY=""
session_key() { echo "$SESSION_KEY"; }

# This session's own `claude` process, the anchor every listener is attributed against
# (basecradle-noc#1230): CLAUDE_PID when Claude Code set it (the key above already proved it
# readable), else the nearest `claude` above this script. Empty when neither is found, and then no
# listener is excused by ancestry: a session that cannot name itself cannot say what is a sibling's.
own_claude_pid() {
  if [ -n "${CLAUDE_PID:-}" ]; then echo "$CLAUDE_PID"; return 0; fi
  ancestor_claude_pid || true
}
OWN_CLAUDE_PID=""

state_root() {
  echo "${SESSION_GUARD_STATE_ROOT:-$HOME/.cache/$REPO_NAME/builder-sessions}"
}

state_dir() {
  echo "$(state_root)/$(session_key)"
}

# The laptop-wide ledger of who started Docker Desktop (basecradle-noc#1255): the one path every
# carrier shares, so it is named for no carrier. One file per session, written only by that session
# (or by the session that adopts its Docker Desktop), so no two sessions ever write one file.
shared_root() {
  echo "${SESSION_GUARD_SHARED_ROOT:-$HOME/.cache/session-guard}"
}

desktop_ledger() {
  echo "$(shared_root)/docker-desktop"
}

# Whether a session key's `claude` is still the process that key names: 0 it is, 1 it provably is
# not, 2 the key is not a process key (a session id, a bare pid) and nothing can tell. A 2 is
# treated as alive by every caller: never adopted, never deleted while its Docker Desktop runs.
key_alive() {
  local rest ident
  case "$1" in claude-*) ;; *) return 2 ;; esac
  rest="${1#claude-}"
  if ident="$(proc_identity "${rest%%-*}")" && [ "$ident" = "$rest" ]; then return 0; fi
  return 1
}

s_get() {  # s_get <name> [default]
  local f; f="$(state_dir)/$1"
  if [ -f "$f" ]; then cat "$f"; else printf '%s' "${2-}"; fi
}

s_put() {  # s_put <name> <value>
  local d; d="$(state_dir)"
  mkdir -p "$d" || return 1
  printf '%s\n' "$2" > "$d/$1"
  # Overwriting a file does not move its directory's mtime, and the stale-state
  # sweep in `begin` keys on that mtime — so a live session says so on every write.
  touch "$d"
}

# ------------------------------------------------------------------ docker io

has_docker() { command -v docker >/dev/null 2>&1; }

engine_up() { has_docker && docker version --format '{{.Server.Version}}' >/dev/null 2>&1; }

docker_desktop_pids() {
  ps -Ao pid=,comm= 2>/dev/null | awk -v p="$DOCKER_APP_PREFIX" 'index($0, p) { print $1 }'
}

docker_desktop_running() {
  [ -n "$(docker_desktop_pids)" ] && return 0
  has_docker || return 1
  # Captured, then matched: under pipefail a `grep -q` that stops at its first
  # match can fail the pipeline exactly when it matched (basecradle-router#172).
  local status
  status="$(docker desktop status 2>/dev/null || true)"
  grep -qE '^Status[[:space:]]+running$' <<<"$status"
}

# Every list below is empty (and harmless) when the engine is unreachable; the
# caller decides whether "we could not look" is clean, and it never is.
list_containers()      { docker ps -a --no-trunc --format '{{.ID}}' 2>/dev/null | sort -u; }
list_images()          { docker image ls -a --no-trunc --format '{{.ID}}' 2>/dev/null | sort -u; }
list_volumes()         { docker volume ls --format '{{.Name}}' 2>/dev/null | sort -u; }
list_builders()        { docker buildx ls 2>/dev/null | awk 'NR>1 && /^[^[:space:]]/ {sub(/\*$/, "", $1); print $1}' | sort -u; }
labelled_containers()  { docker ps -aq --no-trunc --filter "label=$LABEL" 2>/dev/null | sort -u; }
labelled_images()      { docker image ls -aq --no-trunc --filter "label=$LABEL" 2>/dev/null | sort -u; }
session_builders()     { list_builders | grep "^$BUILDER_PREFIX" || true; }
build_cache_size()     { docker system df --format json 2>/dev/null \
                           | grep '"Type":"Build Cache"' \
                           | sed -n 's/.*"Size":"\([^"]*\)".*/\1/p' | sed -n 1p; }

# Docker prints sizes the way go-units' HumanSize does: decimal units, four significant digits
# ("0B", "512.5kB", "1.234GB"); its binary form (BytesSize: "KiB", "MiB") is read too. Prints bytes,
# or fails on a shape it does not know, which the caller reads as "we could not look".
size_bytes() {
  awk -v s="$1" 'BEGIN {
    if (!match(s, /^[0-9]+(\.[0-9]+)?/)) exit 1
    n = substr(s, 1, RLENGTH); u = substr(s, RLENGTH + 1)
    m["B"] = 1; m["kB"] = 1e3; m["KB"] = 1e3; m["MB"] = 1e6; m["GB"] = 1e9; m["TB"] = 1e12; m["PB"] = 1e15
    m["KiB"] = 1024; m["MiB"] = 1024 ^ 2; m["GiB"] = 1024 ^ 3; m["TiB"] = 1024 ^ 4
    if (!(u in m)) exit 1
    printf "%.0f\n", n * m[u] }'
}

# The Docker half of the baseline, taken only from an engine that answers (basecradle#707 (b)). An
# engine that is down at `begin` has nothing to list, and comparing against that empty list read
# every container and image already on the laptop as this session's — the moment `docker-up`
# brought the engine up — with `docker rmi <someone else's image>` as the repair. So `docker-up`,
# the one sanctioned way to reach Docker, takes the census before the session can use the engine.
docker_census() {  # docker_census begin|started|late
  local d; d="$(state_dir)"
  list_containers > "$d/containers.base"
  list_images     > "$d/images.base"
  list_volumes    > "$d/volumes.base"
  list_builders   > "$d/builders.base"
  # One re-read (basecradle#710 item 2): the `started` census runs the moment `docker version` first
  # answers, while the engine may still be settling, and a single failed `docker system df` stored
  # an unreadable baseline for the whole session. One that stays unreadable is `docker-up`'s to re-take.
  local cache; cache="$(build_cache_size)"
  size_bytes "$cache" >/dev/null || { sleep "$POLL_SECONDS"; cache="$(build_cache_size)"; }
  s_put build_cache.base "$cache"
  s_put docker_census_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  s_put docker_census "$1"
}

# Narrow the Docker baseline to what is still on the engine now (basecradle-noc#1255). An artefact
# the baseline holds that is gone now and present later was made again during this session, and
# an ID cannot say so: on 2026-11-04 the router builder's baseline held the harness builder's
# `ubuntu:24.04`, the harness builder removed it, the router builder pulled it again under the same
# content ID, and its check read the copy it pulled as one that was already there. Every moment
# `docker-up` reaches the engine is a moment the laptop is measured, so the baseline is what was
# there at all of them. Narrowing only ever adds to what `check` reports. A list the engine did not
# answer narrows nothing, because an empty list here is the #707 (b) defect: everything reads new.
docker_census_narrow() {
  local d kind now gone=0 before; d="$(state_dir)"
  for kind in containers images volumes; do
    now="$("list_$kind")" || { echo "session-guard: the engine did not list $kind; that baseline is kept as it was." >&2; continue; }
    before="$(wc -l < "$d/$kind.base" | tr -d ' ')"
    comm -12 "$d/$kind.base" <(printf '%s\n' "$now" | sed '/^$/d') > "$d/$kind.base.next" && mv -f "$d/$kind.base.next" "$d/$kind.base"
    gone=$((gone + before - $(wc -l < "$d/$kind.base" | tr -d ' ')))
  done
  # The build cache is narrowed to its smallest reading, by the same rule: only growth is a build.
  local cache base cache_bytes base_bytes
  cache="$(build_cache_size)"; base="$(s_get build_cache.base)"
  if cache_bytes="$(size_bytes "$cache")"; then
    if ! base_bytes="$(size_bytes "$base")"; then
      # The one lever for an unreadable cache baseline (basecradle#709 item 6). Without it, the
      # finding `check` raises could never clear, and self-exit has no override.
      s_put build_cache.base "$cache"
      s_put build_cache_late "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      echo "session-guard: the build cache's size could not be read when the Docker baseline was taken; it is read now ($cache, LATE) — a build before now is attested by hand, not measured."
    elif [ "$cache_bytes" -lt "$base_bytes" ]; then
      s_put build_cache.base "$cache"
    fi
  elif ! size_bytes "$base" >/dev/null; then
    echo "session-guard: the build cache's size still cannot be read ('$cache'); no cache baseline recorded." >&2
    return 1
  fi
  [ "$gone" -eq 0 ] || echo "session-guard: narrowed the Docker baseline: $gone artefact(s) it held are gone now, so any that comes back counts as made during this session."
  return 0
}

# Every Docker Desktop process running now, by identity (pid + start time).
live_desktop_idents() {
  local pid ident
  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    ident="$(proc_identity "$pid")" && echo "$ident"
  done <<< "$(docker_desktop_pids)"
  return 0
}

# This session's record in the laptop-wide ledger that it started the running Docker Desktop: its
# repo on the first line, then, with `procs`, the identity of every process under the bundle, which
# is what lets another session prove a Docker Desktop still running is that same one, and not one
# somebody started since (basecradle#709 item 4). Written before the start too, with no processes,
# so a sibling that begins while it boots reads it as this session's and not as the founder's.
# Written whole and renamed into place, so a reader never sees half of one.
publish_desktop() {  # publish_desktop [procs]
  local led tmp; led="$(desktop_ledger)"
  mkdir -p "$led" || return 1
  tmp="$led/.$(session_key).$$"
  { echo "repo=$REPO_NAME"; if [ "${1-}" = "procs" ]; then live_desktop_idents; fi; } > "$tmp" \
    && mv -f "$tmp" "$led/$(session_key)"
}

retract_desktop() {
  rm -f "$(desktop_ledger)/$(session_key)"
}

# Every OTHER session's ledger record, judged against the Docker Desktop running now. One line each:
#   sibling <key> <repo>   its processes are the ones running (or it is still booting one), and its
#                          session is alive or cannot be told apart from alive
#   orphan  <key> <repo>   its processes are the ones running, and its session has ended
#   unproven <key> <repo>  its session ended while booting, so no process proves which Docker
#                          Desktop it meant: reported and kept, never adopted
#   stale   <key> <repo>   its Docker Desktop is gone and so is its session: the record is removed
# A record whose Docker Desktop is gone but whose session is alive is that session's to retract.
desktop_records() {
  local led f key repo procs line live match alive
  led="$(desktop_ledger)"
  [ -d "$led" ] || return 0
  live="$(live_desktop_idents)"
  for f in "$led"/*; do
    [ -f "$f" ] || continue
    key="${f##*/}"
    [ "$key" != "$(session_key)" ] || continue
    repo="$(sed -n '1s/^repo=//p' "$f")"
    procs="$(sed 1d "$f")"
    key_alive "$key"; alive=$?
    if [ -z "$procs" ]; then
      if [ "$alive" = "1" ]; then
        [ -n "$live" ] && printf 'unproven\t%s\t%s\n' "$key" "$repo"
      elif [ -n "$live" ]; then
        printf 'sibling\t%s\t%s\n' "$key" "$repo"
      fi
      continue
    fi
    match=0
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      if [ -n "$live" ] && grep -qxF -- "$line" <<< "$live"; then match=1; fi
    done <<< "$procs"
    if [ "$match" = "1" ]; then
      if [ "$alive" = "1" ]; then printf 'orphan\t%s\t%s\n' "$key" "$repo"; else printf 'sibling\t%s\t%s\n' "$key" "$repo"; fi
    elif [ "$alive" = "1" ] || { [ "$alive" = "2" ] && [ -n "$(find "$f" -mtime "+$STALE_STATE_DAYS" 2>/dev/null)" ]; }; then
      rm -f "$f"
      printf 'stale\t%s\t%s\n' "$key" "$repo"
    fi
  done
  return 0
}

# The first live sibling that started the running Docker Desktop, as `<key> (<repo>)`, or nothing.
desktop_sibling() {  # desktop_sibling <records>
  awk -F'\t' '$1 == "sibling" { print $2 " (" $3 ")"; exit }' <<< "$1"
}

# A RESUME IS A NEW PROCESS, so it takes a new key and a new baseline (basecradle#709 item 4), and
# a session can be killed with the Docker Desktop it started still running: the basecradle-noc#790
# leftover. So a session that finds the running Docker Desktop proved to be an ended session's,
# from any carrier, ADOPTS it: the record becomes this session's and this session's sweep quits it,
# as the ended one would have. `begin` adopts, and so does `sweep`, for a session that was already
# running when its sibling ended. A record whose Docker Desktop is gone is cleared.
adopt_orphaned_desktop() {  # adopt_orphaned_desktop <records>
  local class key repo
  while IFS=$'\t' read -r class key repo; do
    case "$class" in
      orphan)
        publish_desktop procs || { echo "  WARNING: could not write this session's ledger record; session $key's Docker Desktop is not adopted." >&2; continue; }
        rm -f "$(desktop_ledger)/$key"
        s_put docker_started_by_session 1
        s_put docker_released 0
        s_put adopted_from "$key"
        echo "  ADOPTED: session $key ($repo) ended without quitting the Docker Desktop it started, and that same Docker Desktop is still running — this session quits it at sweep."
        ;;
      unproven)
        echo "  WARNING: session $key ($repo) ended without quitting the Docker Desktop it started, and recorded no process that proves a running one is that one — not adopted. Check Docker Desktop by hand."
        ;;
      stale)
        echo "  cleared session $key's record ($repo): the Docker Desktop it started is no longer running."
        ;;
    esac
  done <<< "$1"
  return 0
}

# Whether the engine must still answer, because this session may have made something on it that
# `check` cannot see with it down:
#   - it started (or adopted) Docker Desktop and has not quit it;
#   - not once its own sweep checked the engine and quit it: down because the session is done;
#   - it used the engine through `docker-up`;
#   - the engine was the laptop's own at `begin` (no builder's record accounted for it), which
#     nothing in this session should have stopped.
# An engine a SIBLING was running at `begin`, and this session never reached through `docker-up`,
# going down is that sibling's sweep doing its job (basecradle-noc#1255), not something this
# session must answer for. Reaching a sibling's engine outside `docker-up` is out of protocol and
# unmeasured once the sibling quits it: the residual, named.
engine_must_answer() {
  [ "$(s_get docker_started_by_session 0)" = "1" ] && return 0
  [ "$(s_get docker_released 0)" = "1" ] && return 1
  [ "$(s_get docker_used 0)" = "1" ] && return 0
  [ "$(s_get docker_census)" = "begin" ] && [ -z "$(s_get desktop_owner)" ]
}

# A DEV SERVER, OR ANYTHING ELSE LISTENING (the capital, on @origin's word, basecradle-noc#899): a
# process this session started that took a TCP port is as much a leftover as a container. The census
# is this account's own listeners only — `lsof -u` the running user. A listener is NEW only when two
# things hold: its command + address was not in the baseline (so a listener that was running before
# the session and restarted under a new pid is not mistaken for a new one), AND its PROCESS started
# after the baseline. The second half exists because an address is not an identity: a macOS daemon
# such as `rapportd` (Continuity) runs for hours and rotates its ephemeral port, so by address alone
# it read as session-started and blocked self-exit with `kill <a system daemon>` as the only repair
# (basecradle-noc#1073). Read off the kernel, like the rest of this guard: nobody has to have written
# anything down. `lsof` absent means "we could not look", which is never clean.
has_lsof() { command -v lsof >/dev/null 2>&1; }
# `lsof` exits 1 both when nothing matches and when it fails, so its status alone cannot tell an
# empty census from a broken one (basecradle#707 (a)). Measured on macOS's lsof 4.91: "no listeners"
# is exit 1 with nothing on stderr, a failure is exit 1 with its reason there, and `-w` keeps the
# warnings it prints on a healthy run off stderr. So anything on stderr, any other status, or a 1
# that printed rows is a census that FAILED, and the caller reports it rather than reading it clean.
# The stderr is caught in a file of this run's own: a fixed name in the state dir is shared by every
# run under the key (a forked subagent shares CLAUDE_PID), and a sibling's `rm` between the write
# and the read would turn a failed census back into an empty one.
list_listeners() {
  local err raw rc
  err="$(mktemp "${TMPDIR:-/tmp}/session-guard-lsof.XXXXXX")" || return 1
  # A redirect that fails never runs lsof and returns 1 with nothing printed: the "no listeners"
  # shape exactly (basecradle#710 item 1). So the redirect is its own step, and its failure is 125.
  raw="$(if exec 2>"$err"; then lsof -w -nP -a -u "$(id -un)" -iTCP -sTCP:LISTEN -Fpcn; else exit 125; fi)"; rc=$?
  if [ -s "$err" ] || { [ "$rc" -ne 0 ] && { [ "$rc" -ne 1 ] || [ -n "$raw" ]; }; }; then
    rm -f "$err"
    return 1
  fi
  rm -f "$err"
  [ -n "$raw" ] || return 0
  awk '
    /^p/ { pid = substr($0, 2) }
    /^c/ { cmd = substr($0, 2) }
    /^n/ { print cmd "\t" substr($0, 2) "\t" pid }' <<< "$raw" | sort -u
}
listener_keys() { cut -f1,2 | sort -u; }

# When a process started, as an epoch, from the kernel's own elapsed time (`ps -o etime=`, the one
# form macOS and Linux both print: [[dd-]hh:]mm:ss). Prints nothing and fails when it cannot be read
# — a process that exited between `lsof` and here, or a shape we do not recognise — and the caller
# treats that as "we could not look", never as "it predates the session".
proc_started_epoch() {
  local et now rest d=0 h=0 m s a b c
  et="$(ps -o etime= -p "$1" 2>/dev/null | tr -d ' ')"
  now="$(date +%s)"
  [[ "$et" =~ ^([0-9]+-)?([0-9]+:)?[0-9]+:[0-9]+$ ]] || return 1
  rest="$et"
  case "$rest" in *-*) d="${rest%%-*}"; rest="${rest#*-}" ;; esac
  IFS=: read -r a b c <<< "$rest"
  if [ -n "$c" ]; then h="$a"; m="$b"; s="$c"; else m="$a"; s="$b"; fi
  # 10# because etime zero-pads ("08") and bash reads a leading zero as octal.
  echo $(( now - (10#$d * 86400 + 10#$h * 3600 + 10#$m * 60 + 10#$s) ))
}

# `etime` has whole-second resolution and is read a moment after `date`, so a computed start can
# land a second or two either side of the truth. The slack only ever widens what is REPORTED: a
# process counts as pre-existing only when it started at least this long before the baseline.
PROC_START_SLACK_SECONDS=2

# WHOSE A LISTENER IS, read off its parent chain (basecradle-noc#1230). Every laptop builder runs as
# the same macOS account, so "this account's listeners" holds every concurrent sibling's test run and
# dev server too, and each one blocked the exit of whichever builder left first. The chain is the
# kernel's own record of who started a process, so it is read, never trusted from a report:
#
#   mine       the chain passes through this session's own `claude` (a nested `claude` under it too)
#   sibling    it passes through another `claude` and never this one: a concurrent session's
#   outside    no `claude` at all, and the chain is whole: its top, the child of launchd, leads its
#              own process group, which is how launchd starts everything it starts (a Terminal the
#              operator typed into, a GUI app, a daemon)
#   cut        no `claude`, and its top does NOT lead its own process group: launchd did not start it,
#              it adopted it. A process detached from the shell that started it (`&`, nohup) keeps
#              that shell's group, loses its ancestry, and so says nothing about whose it is. It stays
#              a finding: it is the shape a dev server this session backgrounded takes, and macOS shows
#              a non-root reader no process environment, so there is no second record to read it from.
#
# Measured on Darwin 24 (2026-11-03): 637 of 640 children of launchd led their own group; the other
# three were two servers detached on purpose and a Chrome crash handler, a real double-fork orphan.
# The top's PATH says nothing: `/usr/bin/python3` runs a binary inside `Python.app/Contents/MacOS/`.
# The residual, named: a server that daemonizes ITSELF (setsid, a `-d` flag) leads its own group and
# reads `outside`, so a session runs its servers in the foreground, under run_in_background.
#   unreadable a link in the chain could not be read (the process exited mid-walk)
#   unknown    this session's own `claude` could not be found, so nothing is excused by ancestry
#
# Prints the class, a tab, and its detail (the sibling's `claude` pid, or the chain's top).
CHAIN_MAX_HOPS=64
attribute_pid() {
  local pid="$1" hops=0 cmd ppid sibling="" pgid comm
  [ -n "$OWN_CLAUDE_PID" ] || { printf 'unknown\t\n'; return 0; }
  while :; do
    [ "$pid" = "$OWN_CLAUDE_PID" ] && { printf 'mine\t%s\n' "$pid"; return 0; }
    cmd="$(ps -o command= -p "$pid" 2>/dev/null)" || cmd=""
    ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')" || ppid=""
    if [ -z "$cmd" ] || ! [[ "$ppid" =~ ^[0-9]+$ ]]; then printf 'unreadable\t%s\n' "$pid"; return 0; fi
    # The nearest `claude` is the one that names the sibling; the walk goes on past it, because
    # this session's own may still be above it (a `claude -p` this session ran).
    if [ -z "$sibling" ] && is_claude_command "$cmd"; then sibling="$pid"; fi
    [ "$ppid" -gt 1 ] || break
    hops=$((hops + 1))
    [ "$hops" -lt "$CHAIN_MAX_HOPS" ] || { printf 'unreadable\t%s\n' "$pid"; return 0; }
    pid="$ppid"
  done
  if [ -n "$sibling" ]; then printf 'sibling\t%s\n' "$sibling"; return 0; fi
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')" || pgid=""
  [[ "$pgid" =~ ^[0-9]+$ ]] || { printf 'unreadable\t%s\n' "$pid"; return 0; }
  comm="$(ps -o comm= -p "$pid" 2>/dev/null)" || comm=""
  if [ "$pgid" = "$pid" ]; then
    printf 'outside\t%s\n' "${comm:-pid $pid}"
  else
    printf 'cut\t%s\n' "${comm:-pid $pid}"
  fi
}

describe_container() { docker ps -a --no-trunc --filter "id=$1" --format '{{.Image}} ({{.Status}})' 2>/dev/null | sed -n 1p; }
describe_image()     { docker image ls -a --no-trunc --format '{{.ID}} {{.Repository}}:{{.Tag}}' 2>/dev/null | awk -v i="$1" '$1==i && !seen++ {print $2}'; }

# Another laptop builder's label on an artefact, which is that sibling's attribution exactly as this
# session's own label is its own (basecradle-noc#1230): every carrier labels `<repo>-builder=1`.
# Prints the sibling's label key, or nothing and fails, which the caller reads as unattributed — an
# inspect that could not answer included, so "we could not look" stays a finding.
sibling_label() {  # sibling_label container|image|volume <id>
  local labels
  case "$1" in
    volume) labels="$(docker volume inspect --format '{{json .Labels}}' "$2" 2>/dev/null)" ;;
    *)      labels="$(docker inspect --type "$1" --format '{{json .Config.Labels}}' "$2" 2>/dev/null)" ;;
  esac
  grep -oE '"[a-z0-9][a-z0-9.-]*-builder":"1"' <<< "$labels" | cut -d'"' -f2 | grep -vxF "$LABEL_KEY" | sed -n 1p | grep .
}

# ------------------------------------------------------------------- commands

cmd_begin() {
  local d; d="$(state_dir)"
  local root; root="$(state_root)"

  # The SessionStart hook carries no matcher, on purpose: it fires on startup, resume, /clear,
  # compaction and fork, and every one of them must leave a baseline in place. A new `claude`
  # process (startup, resume, a fork) has a new key and takes its own; the same process (/clear,
  # compaction) keeps the one it has, because re-taking it would wipe what the session recorded
  # (a Docker Desktop it started, its tab ledger) and fold its leftovers into the new baseline.
  # A bare pid key can be recycled by an unrelated session, so that one is still replaced.
  case "$(session_key)" in
    pid-*|no-session) ;;
    *)
      if [ -f "$d/began_at" ]; then
        echo "session-guard: baseline for $(session_key) already taken at $(s_get began_at); kept."
        return 0
      fi
      ;;
  esac
  if [ -d "$d" ]; then
    echo "session-guard: replacing a state directory that already existed for this session key." >&2
  fi
  rm -rf "$d"
  mkdir -p "$d" || { echo "session-guard: cannot create $d" >&2; return 1; }

  # `began_at` is what `check` reads as "a baseline exists", so it is written LAST:
  # a census cut off part-way (the hook's timeout, a wedged engine) leaves no
  # baseline, never one that reads complete with its .base files missing.
  local began_at; began_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  s_put began_epoch "$(date +%s)"

  if has_docker; then s_put docker_present 1; else s_put docker_present 0; fi
  if engine_up; then s_put engine_up 1; else s_put engine_up 0; fi
  # Whose a running Docker Desktop is (basecradle-noc#1255): a live sibling's, by its ledger record,
  # is not the laptop's own, and is never this session's to restore once that sibling quits it.
  local records owner
  if docker_desktop_running; then
    s_put desktop_running 1
    records="$(desktop_records)"
    owner="$(desktop_sibling "$records")"
    [ -z "$owner" ] || s_put desktop_owner "$owner"
    # One an ended session started is adopted, and was that session's, not the laptop's, too.
    adopt_orphaned_desktop "$records"
    [ -z "$(s_get adopted_from)" ] || s_put desktop_owner "$(s_get adopted_from)"
  else
    s_put desktop_running 0
    adopt_orphaned_desktop "$(desktop_records)"
  fi

  # This guard's own leftovers have a stated end too.
  if [ -d "$root" ]; then
    find "$root" -mindepth 1 -maxdepth 1 -type d -mtime "+$STALE_STATE_DAYS" -exec rm -rf {} + 2>/dev/null || true
  fi

  [ "$(s_get engine_up)" = "1" ] && docker_census begin
  if has_lsof; then
    s_put lsof_present 1
    # A census that failed leaves no listener baseline, which `check` reports with its repair.
    list_listeners > "$d/listeners.base" || {
      rm -f "$d/listeners.base"
      echo "  WARNING: the listening-port census failed (lsof reported an error); check will say so." >&2
    }
  else
    s_put lsof_present 0
    : > "$d/listeners.base"
  fi
  : > "$d/tabs"
  s_put began_at "$began_at"

  echo "session-guard: baseline for $(session_key) at $(s_get began_at)"
  echo "  docker: present=$(s_get docker_present) engine_up=$(s_get engine_up) desktop_running=$(s_get desktop_running)"
  if [ -n "$(s_get desktop_owner)" ]; then
    echo "  docker desktop: started by builder session $(s_get desktop_owner), not the laptop's own — never this session's to restore"
  elif [ "$(s_get desktop_running)" = "1" ]; then
    echo "  docker desktop: no builder session's record accounts for it — the laptop's own, never quit"
  fi
  if [ -n "$(s_get docker_census)" ]; then
    echo "  containers=$(wc -l < "$d/containers.base" | tr -d ' ') images=$(wc -l < "$d/images.base" | tr -d ' ') volumes=$(wc -l < "$d/volumes.base" | tr -d ' ') build_cache=$(s_get build_cache.base)"
  else
    echo "  docker census: deferred to docker-up (the engine is not answering)"
  fi
  [ -f "$d/listeners.base" ] && echo "  listeners=$(wc -l < "$d/listeners.base" | tr -d ' ') (this account's TCP ports in LISTEN)"

  # A previous session's leftovers are a finding, not background noise.
  local leftover; leftover="$(labelled_containers)$(labelled_images)"
  if [ -n "$leftover" ]; then
    echo "  WARNING: builder-labelled artefacts already present at session start — a previous session leaked. They are yours to sweep."
  fi
}

cmd_docker_up() {
  [ -d "$(state_dir)" ] || { echo "session-guard: no baseline — run 'begin' first." >&2; return 1; }
  has_docker || { echo "session-guard: no docker CLI on this machine." >&2; return 1; }

  if engine_up; then
    echo "session-guard: Docker engine is already up; not recording a session start."
    s_put docker_used 1
    s_put docker_released 0
    local sib; sib="$(desktop_sibling "$(desktop_records)")"
    [ -z "$sib" ] || echo "session-guard: this Docker Desktop is sibling session $sib's: it quits it at its own sweep, and if that comes first, run docker-up again."
    if [ -z "$(s_get docker_census)" ]; then
      docker_census late
      echo "session-guard: it came up after this session began, so the Docker baseline is taken now (LATE) — anything this session made on the engine before now is attested by hand, not measured."
    else
      docker_census_narrow || return 1
    fi
    return 0
  fi

  # Running but not answering (still booting, or the degraded engine of
  # basecradle-noc#790) is not this session's to start — or, at sweep, to quit.
  if docker_desktop_running; then
    echo "session-guard: Docker Desktop is already running but its engine is not answering; not recording a session start." >&2
    echo "session-guard: wait for it, or find out whose it is — never restart it to make it answer." >&2
    return 1
  fi

  echo "session-guard: starting Docker Desktop (this session will quit it at sweep)."
  s_put docker_started_by_session 1
  s_put docker_used 1
  s_put docker_released 0
  publish_desktop || echo "session-guard: WARNING: could not write this session's ledger record, so a sibling session cannot tell this Docker Desktop is this session's." >&2
  docker desktop start >/dev/null 2>&1 || open -a Docker >/dev/null 2>&1 || true

  local waited=0
  while [ "$waited" -lt "$START_WAIT_SECONDS" ]; do
    if engine_up; then
      echo "session-guard: engine up after ${waited}s."
      publish_desktop procs || echo "session-guard: WARNING: could not record this Docker Desktop's processes in the ledger." >&2
      if [ -z "$(s_get docker_census)" ]; then
        docker_census started
      else
        docker_census_narrow || return 1
      fi
      return 0
    fi
    sleep "$POLL_SECONDS"; waited=$((waited + POLL_SECONDS))
  done
  echo "session-guard: engine did not come up within ${START_WAIT_SECONDS}s." >&2
  return 1
}

cmd_tab_open() {
  [ $# -ge 1 ] || usage
  [ -d "$(state_dir)" ] || { echo "session-guard: no baseline — run 'begin' first." >&2; return 1; }
  printf '%s\t%s\t%s\n' "$(date +%s)" "$1" "${2-}" >> "$(state_dir)/tabs"
  bump_tab_activity
  echo "session-guard: recorded open tab $1"
}

cmd_tab_close() {
  [ $# -ge 1 ] || usage
  local f; f="$(state_dir)/tabs"
  [ -f "$f" ] || { echo "session-guard: no baseline — run 'begin' first." >&2; return 1; }
  awk -F'\t' -v id="$1" '$2 != id' "$f" > "$f.next" && mv "$f.next" "$f"
  bump_tab_activity
  echo "session-guard: recorded closed tab $1"
}

# A COUNTER, not a clock. The first spelling compared `date +%s` stamps and a whole
# open-then-close-then-check sequence lands inside one second, so the stale-check finding could
# never fire in the case it exists for — a predicate that is confidently wrong rather than loud.
bump_tab_activity() {
  s_put tab_activity "$(( $(s_get tab_activity 0) + 1 ))"
}

cmd_tabs_checked() {
  [ -d "$(state_dir)" ] || { echo "session-guard: no baseline — run 'begin' first." >&2; return 1; }
  s_put tabs_checked 1
  s_put tabs_checked_activity "$(s_get tab_activity 0)"
  echo "session-guard: recorded that the browser was checked."
}

cmd_sweep() {
  local d failed=0; d="$(state_dir)"
  [ -d "$d" ] || { echo "session-guard: no baseline — run 'begin' first." >&2; return 1; }

  if ! engine_up; then
    if engine_must_answer; then
      echo "session-guard: this session used the Docker engine and it is not reachable now — nothing can be swept or verified." >&2
      return 1
    fi
    echo "session-guard: no Docker engine; nothing to sweep."
  else
    local ids id
    ids="$(labelled_containers)"
    if [ -n "$ids" ]; then
      while IFS= read -r id; do
        [ -n "$id" ] || continue
        echo "session-guard: removing container $id ($(describe_container "$id"))"
        docker rm -f "$id" >/dev/null 2>&1 || { echo "session-guard: could not remove container $id" >&2; failed=1; }
      done <<< "$ids"
    fi

    ids="$(labelled_images)"
    if [ -n "$ids" ]; then
      while IFS= read -r id; do
        [ -n "$id" ] || continue
        echo "session-guard: removing image $id ($(describe_image "$id"))"
        docker rmi -f "$id" >/dev/null 2>&1 || { echo "session-guard: could not remove image $id" >&2; failed=1; }
      done <<< "$ids"
    fi

    # A throwaway builder takes its whole build cache with it — which is why the
    # charter says to build on one. A cache record carries no label we control,
    # so pruning a SHARED builder could only ever be a guess at whose cache it is.
    ids="$(session_builders)"
    if [ -n "$ids" ]; then
      while IFS= read -r id; do
        [ -n "$id" ] || continue
        echo "session-guard: removing buildx builder $id"
        docker buildx rm "$id" >/dev/null 2>&1 || { echo "session-guard: could not remove builder $id" >&2; failed=1; }
      done <<< "$ids"
    fi
  fi

  # A removal that failed is a sweep that failed: say so, and never quit the
  # engine out from under it — with the engine down, `check` could no longer see
  # what was left.
  if [ "$failed" = "1" ]; then
    echo "session-guard: sweep INCOMPLETE — a removal above failed; not quitting Docker Desktop." >&2
    return 1
  fi

  # A Docker Desktop an ended session started is this session's to quit (basecradle-noc#1255): a
  # session that was already running when its sibling ended adopts it here, as `begin` would have.
  if [ "$(s_get docker_started_by_session 0)" != "1" ] && docker_desktop_running; then
    adopt_orphaned_desktop "$(desktop_records)"
  fi

  if [ "$(s_get docker_started_by_session 0)" = "1" ]; then
    local left; left="$(labelled_containers)$(labelled_images)$(session_builders)"
    if [ -n "$left" ]; then
      echo "session-guard: labelled artefacts remain; not quitting Docker Desktop." >&2
      return 1
    fi
    # Never quit the engine out from under what `check` must see: an image this session pulled
    # carries no label, and with the engine down `check` could no longer list it.
    if engine_up; then
      check_engine_artefacts
      if [ "$FINDINGS" -gt 0 ]; then
        echo "session-guard: $FINDINGS finding(s) above — not quitting Docker Desktop, because with its engine down check could no longer see them." >&2
        return 1
      fi
    fi
    stop_docker_desktop || return 1
  fi

  echo "session-guard: sweep complete."
}

stop_docker_desktop() {
  echo "session-guard: this session started Docker Desktop — quitting it."
  docker desktop stop >/dev/null 2>&1 || osascript -e 'quit app "Docker Desktop"' >/dev/null 2>&1 || true

  # An exit code is not a stopped process; the process list is. A previous
  # session's `osascript quit app "Docker"` exited 0 while every Docker process
  # kept running (basecradle-noc#716), so the stop is READ BACK.
  local waited=0
  while [ "$waited" -lt "$STOP_WAIT_SECONDS" ]; do
    if [ -z "$(docker_desktop_pids)" ]; then
      echo "session-guard: Docker Desktop stopped (no process under $DOCKER_APP_PREFIX) after ${waited}s."
      s_put docker_started_by_session 0
      s_put docker_released 1
      retract_desktop
      return 0
    fi
    sleep "$POLL_SECONDS"; waited=$((waited + POLL_SECONDS))
  done
  echo "session-guard: Docker Desktop is STILL RUNNING ${STOP_WAIT_SECONDS}s after the quit — quit it by hand." >&2
  return 1
}

# What is on the answering engine that this session cannot account for: its own labelled artefacts
# still present, and every artefact that appeared since the Docker baseline that no builder's label
# accounts for. `check` reports these, and `sweep` refuses to quit Docker Desktop while any stands,
# because with the engine down `check` could no longer see them (basecradle-noc#1255).
check_engine_artefacts() {
  local d base_census; d="$(state_dir)"; base_census="$(s_get docker_census)"
  local ids id extra sib
  ids="$(labelled_containers)"
  if [ -n "$ids" ]; then
    while IFS= read -r id; do
      [ -n "$id" ] || continue
      finding "labelled container still present: $id ($(describe_container "$id"))" ".claude/session-guard.sh sweep"
    done <<< "$ids"
  fi

  ids="$(labelled_images)"
  if [ -n "$ids" ]; then
    while IFS= read -r id; do
      [ -n "$id" ] || continue
      finding "labelled image still present: $id ($(describe_image "$id"))" ".claude/session-guard.sh sweep"
    done <<< "$ids"
  fi

  ids="$(session_builders)"
  if [ -n "$ids" ]; then
    while IFS= read -r id; do
      [ -n "$id" ] || continue
      finding "session buildx builder still present: $id" ".claude/session-guard.sh sweep"
    done <<< "$ids"
  fi

  # Everything new that the label cannot account for. Reported with the exact
  # command and never removed: an image someone else pulled during this
  # session is theirs, and this guard cannot tell the two apart.
  #
  # Only against a census. With none, the engine was down at `begin` and came up
  # from outside this session without a `docker-up`, so there is nothing to
  # compare against: every artefact on the laptop would read as new, with `docker
  # rmi` of someone else's image as the repair (basecradle#707 (b)). The labelled
  # checks above still ran. The gap itself is a finding (basecradle#709 item 3):
  # "we could not look" is never clean, and a session that used the engine out of
  # protocol would otherwise exit clean. Its one lever is a LATE census, taken
  # after checking by hand, which every later `check` names as late.
  local engine_sib=""
  [ -n "$base_census" ] || [ "$(s_get docker_used 0)" = "1" ] || engine_sib="$(desktop_sibling "$(desktop_records)")"
  if [ -n "$engine_sib" ]; then
    # A sibling's engine that came up after this session began, which this session never reached
    # through `docker-up`, is that sibling's to answer for (basecradle-noc#1255): no baseline is
    # needed for an engine this session did not use. The labelled checks above still ran.
    echo "session-guard: not this session's: the Docker engine is sibling session ${engine_sib}'s, brought up after this session began and never reached from it through docker-up, so it needs no Docker baseline here."
  elif [ -z "$base_census" ]; then
    finding "no Docker baseline — the engine came up after this session began and the session never ran docker-up, so nothing here can say what it made on the engine." \
            "check by hand (docker ps -a; docker image ls; docker volume ls), remove anything this session made, then .claude/session-guard.sh docker-up — it takes the Docker baseline now, which check names as LATE"
  else
    extra="$(comm -23 <(comm -13 "$d/containers.base" <(list_containers)) <(labelled_containers))"
    if [ -n "$extra" ]; then
      while IFS= read -r id; do
        [ -n "$id" ] || continue
        if sib="$(sibling_label container "$id")"; then
          echo "session-guard: not this session's: container $id ($(describe_container "$id")) carries $sib=1, a sibling builder's label."
          continue
        fi
        finding "container appeared during this session and carries no builder label of this session's: $id ($(describe_container "$id"))" \
                "docker rm -f $id   (only if it is yours — check first)"
      done <<< "$extra"
    fi

    extra="$(comm -23 <(comm -13 "$d/images.base" <(list_images)) <(labelled_images))"
    if [ -n "$extra" ]; then
      while IFS= read -r id; do
        [ -n "$id" ] || continue
        if sib="$(sibling_label image "$id")"; then
          echo "session-guard: not this session's: image $id ($(describe_image "$id")) carries $sib=1, a sibling builder's label."
          continue
        fi
        finding "image appeared during this session and carries no builder label of this session's: $id ($(describe_image "$id"))" \
                "docker rmi $id   (only if it is yours — check first)"
      done <<< "$extra"
    fi

    extra="$(comm -13 "$d/volumes.base" <(list_volumes))"
    if [ -n "$extra" ]; then
      while IFS= read -r id; do
        [ -n "$id" ] || continue
        if sib="$(sibling_label volume "$id")"; then
          echo "session-guard: not this session's: volume $id carries $sib=1, a sibling builder's label."
          continue
        fi
        finding "volume appeared during this session: $id" "docker volume rm $id   (only if it is yours — check first)"
      done <<< "$extra"
    fi

    # Only GROWTH is a build (basecradle#707 (c)). BuildKit's own garbage collection
    # shrinks the cache on its schedule, and comparing the printed strings for
    # inequality read that as a build this session ran, blocking an exit nothing
    # could clear. Growth is still unattributable — someone else's build grows it
    # too — so it is reported like every other unlabelled artefact, never pruned.
    local cache_now cache_base now_bytes base_bytes
    cache_now="$(build_cache_size)"; cache_base="$(s_get build_cache.base)"
    # An unreadable BASELINE never becomes readable by re-running `check`, so it gets its own
    # finding with its own lever (basecradle#709 item 6); an unreadable reading NOW may be a blip.
    if ! base_bytes="$(size_bytes "$cache_base")"; then
      finding "the shared build cache's size could not be read when the Docker baseline was taken ('${cache_base}'), so a build on a shared builder cannot be ruled out." \
              "compare docker system df by hand, then .claude/session-guard.sh docker-up — it re-reads the cache baseline, which check names as LATE"
    elif ! now_bytes="$(size_bytes "$cache_now")"; then
      finding "the shared build cache's size could not be read now ('${cache_now}'; at the baseline: '${cache_base}'), so a build on a shared builder cannot be ruled out." \
              "re-run .claude/session-guard.sh check; if it persists, compare docker system df by hand and say what you found in the completion comment"
    elif [ "$now_bytes" -gt "$base_bytes" ]; then
      finding "shared build cache grew during this session ($cache_base -> $cache_now) — a build ran on a shared builder." \
              "build on your own throwaway builder (${BUILDER_PREFIX}<something>) so 'docker buildx rm' takes its cache with it; if the build was not this session's, say whose in the completion comment"
    fi
  fi
}

FINDINGS=0
finding() {
  FINDINGS=$((FINDINGS + 1))
  echo "not-clean: $1"
  [ $# -ge 2 ] && echo "           repair: $2"
  return 0
}

# A LATE baseline, for a session that began before the census existed. It is an attestation, not a
# measurement: whoever runs it has checked the listeners by hand and stopped anything the session
# started, and it says so in the state, the way `tabs-checked` does for the browser.
cmd_baseline_listeners() {
  local d; d="$(state_dir)"
  [ -d "$d" ] || { echo "session-guard: no baseline — run 'begin' first." >&2; return 1; }
  [ ! -f "$d/listeners.base" ] || { echo "session-guard: a listener baseline already exists; nothing to do." >&2; return 0; }
  has_lsof || { echo "session-guard: lsof is not available; cannot take a listener baseline." >&2; return 1; }
  s_put lsof_present 1
  s_put listeners_baseline_late "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  list_listeners > "$d/listeners.base" || {
    rm -f "$d/listeners.base"
    echo "session-guard: the listening-port census failed (lsof reported an error); no baseline recorded." >&2
    return 1
  }
  echo "session-guard: LATE listener baseline recorded ($(wc -l < "$d/listeners.base" | tr -d ' ') listener(s)) — attested by hand, not measured from session start."
}

cmd_check() {
  local d; d="$(state_dir)"

  if [ ! -d "$d" ] || [ ! -f "$d/began_at" ]; then
    finding "no session baseline at $d — this session never ran 'begin', so nothing here can certify what it created." \
            ".claude/session-guard.sh begin   (and check that the SessionStart hook in .claude/settings.json is live)"
    echo "session-guard: NOT CLEAN ($FINDINGS finding(s))."
    return 1
  fi

  local base_desktop
  base_desktop="$(s_get desktop_running 0)"

  if engine_up; then
    check_engine_artefacts
  elif engine_must_answer; then
    finding "the Docker engine is not reachable and this session used it — nothing here can certify what it left behind." \
            "bring the engine back (.claude/session-guard.sh docker-up) and re-check, or say so in the completion comment"
  fi

  # Docker Desktop, both directions, judged against the laptop's own state: a Docker Desktop that a
  # builder session started is never the laptop's own (basecradle-noc#1255).
  local now_desktop=0 base_owner records sib orphan
  docker_desktop_running && now_desktop=1
  base_owner="$(s_get desktop_owner)"
  if [ "$now_desktop" = "1" ]; then
    if [ "$(s_get docker_started_by_session 0)" = "1" ]; then
      if [ -n "$(s_get adopted_from)" ]; then
        finding "this session adopted the Docker Desktop that session $(s_get adopted_from) started and never quit, and it is still running." ".claude/session-guard.sh sweep"
      else
        finding "this session started Docker Desktop and it is still running." ".claude/session-guard.sh sweep"
      fi
    else
      records="$(desktop_records)"
      sib="$(desktop_sibling "$records")"
      orphan="$(awk -F'\t' '$1 == "orphan" { print $2 " (" $3 ")"; exit }' <<< "$records")"
      if [ -n "$sib" ]; then
        echo "session-guard: not this session's: Docker Desktop is running for sibling session $sib, whose ledger record proves it started this one; that session quits it at its own sweep."
      elif [ -n "$orphan" ]; then
        finding "Docker Desktop is running, started by session $orphan, which has ended without quitting it." \
                ".claude/session-guard.sh sweep   (it adopts that Docker Desktop and quits it)"
      elif [ "$base_desktop" = "1" ] && [ -z "$base_owner" ]; then
        :  # the laptop's own, as this session found it
      elif [ "$base_desktop" = "1" ]; then
        finding "Docker Desktop is running, and no builder session's record accounts for it: the one running at session start was builder session ${base_owner}'s, and nothing recorded starting this one." \
                "if it is yours, quit it by hand and say so; if it is not, confirm whose it is before exiting"
      else
        finding "Docker Desktop is running and was not at session start, and this session did not record starting it." \
                "if it is yours, quit it by hand and say so; if it is not, confirm whose it is before exiting"
      fi
    fi
  elif [ "$base_desktop" = "1" ] && [ -z "$base_owner" ]; then
    finding "Docker Desktop was running at session start and is not now — this session left the laptop in a different state." \
            "docker desktop start"
  elif [ "$base_desktop" = "1" ]; then
    echo "session-guard: not this session's: the Docker Desktop builder session $base_owner was running at session start has been quit since — that session's to quit, never this one's to restore."
  fi

  # Anything listening that was not listening at session start, in a process that did not exist at
  # session start: a dev server, a preview, a debug port. Reported with its pid and the exact
  # command, and NEVER killed — the guard cannot tell a server this session started from one
  # @origin opened in the meantime. A process older than the session that merely moved to a new
  # port is not this session's (basecradle-noc#1073) and is named, never reported.
  if [ ! -f "$d/listeners.base" ]; then
    finding "no listening-port baseline — the census at session start failed, or this session began before the guard censused ports, so nothing here can say which listeners it started." \
            "check by hand (lsof -nP -iTCP -sTCP:LISTEN), stop any this session started, then .claude/session-guard.sh baseline-listeners"
  elif ! has_lsof || [ "$(s_get lsof_present 0)" != "1" ]; then
    finding "lsof was not available, so this session's listening ports could not be censused." \
            "check by hand (lsof -nP -iTCP -sTCP:LISTEN) and say what you found in the completion comment"
  else
    local now_rows new_rows began_epoch cmd addr pid started census_ok=1
    began_epoch="$(s_get began_epoch)"
    if ! now_rows="$(list_listeners)"; then
      census_ok=0
      finding "the listening-port census failed (lsof reported an error), so this session's listening ports could not be checked." \
              "re-run .claude/session-guard.sh check; if it persists, check by hand (lsof -nP -iTCP -sTCP:LISTEN) and say what you found in the completion comment"
    fi
    # Every current row whose command + address the baseline did not hold, one row per pid. Keyed on
    # FILENAME, not NR == FNR, which reads every row as baseline when the baseline file is empty.
    new_rows="$(awk -F'\t' -v base_file="$d/listeners.base" '
      FILENAME == base_file { base[$1 "\t" $2] = 1; next }
      !(($1 "\t" $2) in base)' "$d/listeners.base" - <<< "$now_rows")"
    # The account-wide census is information; only what the parent chain attributes to this
    # session refuses the exit (basecradle-noc#1230).
    local n_all=0 n_new=0 n_mine=0 n_sibling=0 n_outside=0 class detail
    [ -n "$now_rows" ] && n_all="$(cut -f3 <<< "$now_rows" | sort -u | wc -l | tr -d ' ')"
    if [ -n "$new_rows" ]; then
      while IFS=$'\t' read -r cmd addr pid; do
        [ -n "${pid:-}" ] || continue
        if ! started="$(proc_started_epoch "$pid")"; then
          finding "a new listener's process start time could not be read: $cmd (pid $pid) on $addr — so nothing here can say whether this session started it." \
                  "re-run .claude/session-guard.sh check; if it persists, check by hand (ps -o lstart= -p $pid) and stop it only if this session started it"
          continue
        elif [[ "$began_epoch" =~ ^[0-9]+$ ]] && [ $((started + PROC_START_SLACK_SECONDS)) -le "$began_epoch" ]; then
          echo "session-guard: not this session's: $cmd (pid $pid) is listening on a new address $addr, but its process started before the session baseline."
          continue
        fi
        n_new=$((n_new + 1))
        IFS=$'\t' read -r class detail <<< "$(attribute_pid "$pid")"
        case "$class" in
          sibling)
            n_sibling=$((n_sibling + 1))
            echo "session-guard: not this session's: $cmd (pid $pid) on $addr belongs to a sibling session — its parent chain reaches claude pid $detail, never this session's (pid $OWN_CLAUDE_PID)."
            ;;
          outside)
            n_outside=$((n_outside + 1))
            echo "session-guard: not this session's: $cmd (pid $pid) on $addr was started outside any Claude session — its parent chain is whole and reaches no claude (top: $detail)."
            ;;
          cut)
            finding "a process started listening during this session and its parent chain was cut, so nothing here can say whose it is: $cmd (pid $pid) on $addr (adopted by launchd at $detail)" \
                    "kill $pid   (only if this session started it — a server this session detaches loses its ancestry, so run it with run_in_background instead; if it is not yours, confirm whose before exiting)"
            ;;
          unreadable)
            finding "a new listener's parent chain could not be read: $cmd (pid $pid) on $addr (at pid $detail) — so nothing here can say whether this session started it." \
                    "re-run .claude/session-guard.sh check; if it persists, check by hand (ps -o ppid=,command= -p $detail) and stop it only if this session started it"
            ;;
          *)
            # mine, and unknown: a session that cannot name its own claude excuses nothing.
            n_mine=$((n_mine + 1))
            finding "a process started listening during this session: $cmd (pid $pid) on $addr" \
                    "kill $pid   (only if this session started it — a dev server you are done with is yours to stop; if it is not yours, confirm whose before exiting)"
            ;;
        esac
      done <<< "$new_rows"
    fi
    # A census that failed has no count to print: its finding above says so, and a 0 here would not.
    [ "$census_ok" = "1" ] && echo "session-guard: listening-port census: $n_all process(es) listening on this account; $n_new started since the baseline — $n_mine attributed to this session, $n_sibling to a sibling session, $n_outside outside any Claude session."
    [ -n "$OWN_CLAUDE_PID" ] || echo "session-guard: this session's own claude process could not be found, so no listener is excused by its parent chain."
  fi

  # Browser tabs: a record, plus the attestation that makes an empty record mean
  # something. See the header — a tab carries no attribution we can read.
  if [ -s "$d/tabs" ]; then
    while IFS=$'\t' read -r _ id note; do
      [ -n "${id:-}" ] || continue
      finding "browser tab this session opened is still recorded as open: $id ${note:+($note)}" \
              "close it (tabs_close_mcp), then .claude/session-guard.sh tab-close $id"
    done < "$d/tabs"
  fi

  if [ "$(s_get tabs_checked 0)" != "1" ]; then
    finding "the browser was never checked this session — an unrecorded tab is invisible to the record above." \
            "call tabs_context_mcp, close anything this session opened, then .claude/session-guard.sh tabs-checked"
  elif [ "$(s_get tab_activity 0)" != "$(s_get tabs_checked_activity 0)" ]; then
    finding "a browser tab was opened or closed after the last browser check, so the check is stale." \
            "re-check the browser, then .claude/session-guard.sh tabs-checked"
  fi

  # A LATE baseline is an attestation, not a measurement (basecradle#709 item 5): what the session
  # made before it was taken is in it with no trace. It is the designed path where the engine came
  # up from outside, so it never blocks, but every check says it, so a clean verdict never hides it.
  if [ "$(s_get docker_census)" = "late" ]; then
    echo "session-guard: LATE: the Docker baseline was taken at $(s_get docker_census_at '(unrecorded)'), not at session start — anything this session made on the engine before then is attested by hand, not measured."
  fi
  if [ -n "$(s_get build_cache_late)" ]; then
    echo "session-guard: LATE: the build cache's baseline was read at $(s_get build_cache_late) — a build before then is attested by hand, not measured."
  fi
  if [ -n "$(s_get listeners_baseline_late)" ]; then
    echo "session-guard: LATE: the listening-port baseline was taken at $(s_get listeners_baseline_late) — attested by hand, not measured from session start."
  fi

  if [ "$FINDINGS" -eq 0 ]; then
    echo "session-guard: clean — this session left nothing behind (baseline $(s_get began_at))."
    return 0
  fi
  echo "session-guard: NOT CLEAN ($FINDINGS finding(s)). Fix them, or say plainly in the completion comment what is left and why."
  return 1
}

cmd_done() {
  cmd_check || return 1
  retract_desktop
  rm -rf "$(state_dir)"
  echo "session-guard: state cleared."
}

# ----------------------------------------------------------------------- main

[ $# -ge 1 ] || usage
command="$1"; shift

while [ $# -gt 0 ]; do
  case "$1" in
    --session-id) SESSION_ID_OVERRIDE="${2-}"; shift 2 ;;
    *) break ;;
  esac
done

SESSION_KEY="$(compute_session_key)" || exit 75
OWN_CLAUDE_PID="$(own_claude_pid)"

case "$command" in
  begin)        cmd_begin ;;
  docker-up)    cmd_docker_up ;;
  tab-open)     cmd_tab_open "$@" ;;
  tab-close)    cmd_tab_close "$@" ;;
  tabs-checked) cmd_tabs_checked ;;
  baseline-listeners) cmd_baseline_listeners ;;
  sweep)        cmd_sweep ;;
  check)        cmd_check ;;
  done)         cmd_done ;;
  *)            usage ;;
esac

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
# THE THREE HALVES
# ----------------
#   GROUND TRUTH  for Docker Desktop and for this account's listening TCP ports (a
#                 dev server). The process list and the kernel's socket table are
#                 authoritative, so `check` compares what is there now against a
#                 baseline taken at session start.
#   A LEDGER      for Docker artefacts (basecradle-noc#1715). @origin's rule: a
#                 session cleans up exactly what it created, and it knows what it
#                 created because it recorded it at the moment it created it. It
#                 never guesses. So every container, image, volume and builder a
#                 session makes goes through this script (`run`, `create`, `pull`,
#                 `build`, `volume-create`), which writes it on the session's own
#                 ledger as it is made; `check` reads only that ledger, and `sweep`
#                 removes only what is on it.
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
# WHAT A SESSION DID NOT RECORD IS NOT ITS FINDING. The guard this replaced read
# every Docker artefact that "appeared during the session" as the session's, so a
# sibling's pulled base image (which can carry no label) and a sibling's growth of
# the shared build cache each held an innocent session open with no lever but
# removing someone else's property. Nothing outside the ledger blocks now: an
# artefact made around the guard (a bare `docker pull`) is caught after every
# builder exits by the capital's own leftovers check, which names anything on the
# laptop no session claimed. "Clean the hotel's towels; never the guest's
# suitcase." There is deliberately no `docker system prune` anywhere in this file,
# and no removal here is forced past a container still using an image.
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
  run [docker run args]     Run a container, recorded on this session's ledger
                            (docker writes its id there as it creates it). It is
                            never pulled implicitly: pull its image first.
  create [docker create args]   The same, for `docker create`.
  pull [options] <ref>      Pull an image and record it, unless it was already
                            on the engine (then it is not this session's).
  build [buildx build args] <context>
                            Build on this session's own buildx builder (made and
                            recorded on first use) and load the result, recorded.
  volume-create <name> [options]
                            Create a named volume, recorded. Refuses a name that
                            already exists.
  tab-open <id> [note]      Record a browser tab this session opened.
  tab-close <id>            Record that it was closed.
  baseline-listeners        A late listening-port baseline, for a session that began
                            before the guard censused ports (attested by hand)
  tabs-checked              Attest that the browser was looked at (one
                            tabs_context_mcp call) and holds no session tab.
  sweep                     Remove what this session's ledger records (containers,
                            builders, images, volumes, in that order); quit Docker
                            Desktop if this session started it, reading the stop back.
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

# The engine's buildx builders, by name. Empty (and failing, under pipefail) when the engine is
# unreachable; the caller decides whether "we could not look" is clean, and it never is.
list_builders()        { docker buildx ls 2>/dev/null | awk 'NR>1 && /^[^[:space:]]/ {sub(/\*$/, "", $1); print $1}' | sort -u; }

# ------------------------------------------------------------------ the ledger
#
# YOU CLEAN UP WHAT YOU CREATED, AND YOU KNOW BECAUSE YOU WROTE IT DOWN (@origin, basecradle-noc#1715).
# Every Docker artefact a session makes is written on its own ledger at the moment it is made, by the
# one command here that makes it, and nothing else decides whose an artefact is: not a label (a pulled
# image cannot carry one), not "it appeared while this session ran", not "a sibling's container uses
# it". The ledger lives in the session's state directory:
#
#   records/     one file per image, volume or builder, holding `<kind>\t<id>\t<note>`, written as it
#                is made. A volume and a builder are written BEFORE docker makes them, under a name the
#                guard has already checked is free, so not even a session killed mid-call makes one
#                off it.
#   containers/  one file per container the session ran or created, written by docker itself
#                (`--cidfile`) the moment the container exists: a session killed mid-run is still on it.
#   pending/     an image operation (`pull`, `build`) asked for and not yet resolved to the image it
#                made: its reference, the process running it, and every image on the engine before it
#                ran, so the image it made is told apart from one that was already there, by whoever
#                resolves it — the operation itself, or the next `check` after a crash.
#
# An entry is DISCHARGED (its file removed) once an answering engine shows its artefact gone, so a
# sweep that quit Docker Desktop leaves a ledger that needs no engine to read as clear. One file per
# entry is what makes that safe: a discharge removes only the files it read, so it never races an
# entry another call of the same session is writing.
#
# The label still goes on every container, built image and volume, because it helps a reader; it is no
# longer how ownership is decided.

# The image a session's own builder runs, pulled through `pull` first so the ledger records it when it
# is new. Pinned by tag, so the builder's own pull at bootstrap is the same image.
BUILDKIT_IMAGE="moby/buildkit:buildx-stable-1"

# A name this session alone holds, for its own builder and the tag each of its builds carries: the
# session key, in the characters an image repository allows.
session_slug() {
  printf '%s' "$(session_key)" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-*//; s/-*$//'
}
own_builder() { echo "${BUILDER_PREFIX}$(session_slug)"; }

# The conversation a session belongs to, which a resume keeps while the process (the key) changes.
conversation_id() { printf '%s' "${SESSION_ID_OVERRIDE:-${CLAUDE_CODE_SESSION_ID:-}}"; }

note_conversation() {  # note_conversation <id>: every conversation id this session's process carried
  local f; f="$(state_dir)/conversations"
  [ -n "$1" ] || return 0
  if [ ! -f "$f" ] || ! grep -qxF -- "$1" "$f"; then printf '%s\n' "$1" >> "$f"; fi
}

# This process, by identity where its start time can be read (a recycled pid cannot pass for it),
# else by pid alone; and whether a process so named is still the one running.
my_ident() { proc_identity "$$" || echo "$$"; }
op_alive() {  # op_alive <ident>
  local pid="${1%%-*}"
  [ -n "$pid" ] || return 1
  if [ "$1" != "$pid" ]; then [ "$(proc_identity "$pid")" = "$1" ]; else kill -0 "$pid" 2>/dev/null; fi
}

# Write one entry. With `held`, the entry is written BEFORE docker makes the artefact, and names the
# process making it on its second line, so no concurrent call of this session discharges it while it
# is still being made; `ledger_release` drops that line once docker has made it. Prints the file.
ledger_record() {  # ledger_record <kind> <id> [note] [held]
  local d f; d="$(state_dir)/records"
  mkdir -p "$d" || return 1
  f="$d/$(date +%s).$$.$RANDOM"
  { printf '%s\t%s\t%s\n' "$1" "$2" "${3:--}"; if [ "${4-}" = "held" ]; then my_ident; fi; } > "$f.next" || return 1
  mv -f "$f.next" "$f" || return 1
  touch "$(state_dir)"
  echo "$f"
}

ledger_release() {  # ledger_release <file>
  sed -n 1p "$1" > "$1.next" && mv -f "$1.next" "$1"
}

# The process still making an entry's artefact, if any: its second line, while that process lives.
entry_held() {  # entry_held <file>
  local holder; holder="$(sed -n 2p "$1" 2>/dev/null)"
  [ -n "$holder" ] && op_alive "$holder"
}

# Every entry a ledger holds, containers included, one `<kind>\t<id>\t<note>\t<file>` line each. No
# field is ever empty (an empty note is `-`): a tab is whitespace to `read`, which would fold an empty
# field into the next one. The same artefact recorded twice is two lines, one per file.
ledger_entries() {  # ledger_entries [state dir]
  local d="${1:-$(state_dir)}" f kind id note
  for f in "$d/records"/*; do
    [ -f "$f" ] || continue
    IFS=$'\t' read -r kind id note < "$f" || true
    if [ -n "${kind:-}" ] && [ -n "${id:-}" ]; then printf '%s\t%s\t%s\t%s\n' "$kind" "$id" "${note:--}" "$f"; fi
  done
  for f in "$d/containers"/*; do
    [ -s "$f" ] || continue
    id="$(tr -d '[:space:]' < "$f")"
    if [ -n "$id" ]; then printf 'container\t%s\t-\t%s\n' "$id" "$f"; fi
  done
  return 0
}

# One line per artefact, however many entries record it.
distinct() { awk -F'\t' 'NF && !seen[$1 FS $2]++'; }

# Remove the file of every entry read in <entries> whose artefact is not in <present>, both read off
# an answering engine moments ago. Only files that read listed are touched.
ledger_discharge() {  # ledger_discharge <entries> <present>
  local entries="$1" keys kind id note file
  keys="$(cut -f1,2 <<< "$2")"
  while IFS=$'\t' read -r kind id note file; do
    [ -n "${file:-}" ] || continue
    grep -qxF -- "$kind"$'\t'"$id" <<< "$keys" && continue
    entry_held "$file" || rm -f "$file"
  done <<< "$entries"
  return 0
}

has_pending() {  # has_pending [state dir]
  local d="${1:-$(state_dir)}" f
  for f in "$d/pending"/*; do [ -f "$f" ] && return 0; done
  return 1
}

ledger_empty() {  # ledger_empty [state dir]
  [ -z "$(ledger_entries "${1-}")" ] && ! has_pending "${1-}"
}

# Which of these ledger entries are still on the engine, in the form `ledger_entries` prints. Each kind
# the entries hold is listed once; a list the engine did not answer fails the whole read, which the
# caller reports and never reads as "gone". An entry of a kind this script does not know is reported
# as present: a ledger it cannot read is never a clean one.
ledger_present() {  # ledger_present <entries>
  local entries="$1" kind id note file have c="" i="" v="" b=""
  [ -n "$entries" ] || return 0
  if grep -q '^container' <<< "$entries"; then c="$(docker ps -aq --no-trunc 2>/dev/null)" || return 1; fi
  if grep -q '^image' <<< "$entries"; then i="$(docker image ls -aq --no-trunc 2>/dev/null)" || return 1; fi
  if grep -q '^volume' <<< "$entries"; then v="$(docker volume ls -q 2>/dev/null)" || return 1; fi
  if grep -q '^builder' <<< "$entries"; then b="$(list_builders)" || return 1; fi
  while IFS=$'\t' read -r kind id note file; do
    [ -n "${id:-}" ] || continue
    case "$kind" in
      container) have="$c" ;;
      image)     have="$i" ;;
      volume)    have="$v" ;;
      builder)   have="$b" ;;
      *)         printf '%s\t%s\t%s\t%s\n' "$kind" "$id" "$note" "$file"; continue ;;
    esac
    if grep -qxF -- "$id" <<< "$have"; then printf '%s\t%s\t%s\t%s\n' "$kind" "$id" "$note" "$file"; fi
  done <<< "$entries"
  return 0
}

describe_entry() {  # describe_entry <kind> <id> <note>
  case "$1" in
    container) echo "container $2 ($(describe_container "$2"))" ;;
    image)     if [ "${3:--}" = "-" ]; then echo "image $2 (untagged)"; else echo "image $2 ($3)"; fi ;;
    volume)    echo "volume $2" ;;
    builder)   echo "buildx builder $2" ;;
    *)         echo "a ledger entry of a kind this guard does not know ('$1': $2)" ;;
  esac
}

# Remove one artefact this session recorded. Never forced past a container still using an image: a
# forced removal would pull it out from under that container, which is not this session's to break.
remove_entry() {  # remove_entry <kind> <id> <note>
  local out="" tag rc=0
  case "$1" in
    container) out="$(docker rm -f -v "$2" 2>&1)" || rc=1 ;;
    builder)   out="$(docker buildx rm "$2" 2>&1)" || rc=1 ;;
    volume)    out="$(docker volume rm "$2" 2>&1)" || rc=1 ;;
    image)
      # The tags its own operation gave it first, each only while it still names this image (an image
      # with several tags cannot be removed by its id unforced), then the image itself.
      while IFS= read -r tag; do
        [ -n "$tag" ] || continue
        [ "$(docker image inspect --format '{{.Id}}' "$tag" 2>/dev/null)" = "$2" ] || continue
        out="$(docker rmi "$tag" 2>&1)" || { rc=1; break; }
      done <<< "$(if [ "$3" != "-" ]; then tr ',' '\n' <<< "$3"; fi)"
      if [ "$rc" = "0" ] && docker image inspect "$2" >/dev/null 2>&1; then
        out="$(docker rmi "$2" 2>&1)" || rc=1
      fi
      ;;
    *) out="this guard does not know how to remove a '$1'"; rc=1 ;;
  esac
  if [ "$rc" != "0" ]; then
    echo "session-guard: could not remove $(describe_entry "$1" "$2" "$3"): $(tr '\n' ' ' <<< "$out")" >&2
  fi
  return "$rc"
}

# Start an image operation: write its pending record, holding the images on the engine before it runs.
image_op_begin() {  # image_op_begin <ref> <pull|build>  ->  the pending record's path
  local d p before ident
  d="$(state_dir)/pending"
  before="$(docker image ls -aq --no-trunc 2>/dev/null)" || {
    echo "session-guard: the engine did not list its images, so nothing could tell what this $2 makes; not running it." >&2
    return 1
  }
  ident="$(my_ident)"
  mkdir -p "$d" || return 1
  p="$d/$(date +%s).$$.$RANDOM"
  { printf '%s\n%s\n%s\n' "$1" "$2" "$ident"; printf '%s\n' "$before"; } > "$p.next" || return 1
  mv -f "$p.next" "$p" || return 1
  touch "$(state_dir)"
  echo "$p"
}

# Resolve a pending image operation to what it made, read off the engine: the image its reference
# names now, recorded only if that image was not on the engine before the operation ran. Returns 0
# when resolved (recorded, or nothing to record), 1 when the engine could not be read or the ledger
# not written (the record is kept), and 2 while the operation's own process is still running, which
# only the operation itself (`now`) may resolve.
#
# A PULL CUT OFF IS NEVER GUESSED AT. Once its process is gone, an image under its reference that was
# not there before may be the one it landed or one a sibling pulled since, and nothing on the engine
# tells the two apart; recording it would hand this session's sweep a sibling's image. So it is named
# and left to the capital's leftovers check. A build cut off is resolved: its tag is this session's
# alone, so the image under it can only be its own.
resolve_pending() {  # resolve_pending <record> [now]
  local f="$1" ref how ident before id tags
  ref="$(sed -n 1p "$f")"; how="$(sed -n 2p "$f")"; ident="$(sed -n 3p "$f")"
  if [ "${2-}" != "now" ]; then
    op_alive "$ident" && return 2
    if [ "$how" != "build" ]; then
      echo "session-guard: a $how of $ref this session started was cut off before it said what it made; whatever it may have left is NOT recorded, because nothing can tell it from another session's (remove it by hand if it is this session's, and say so in the completion comment)."
      rm -f "$f"
      return 0
    fi
  fi
  engine_up || return 1
  if ! id="$(docker image inspect --format '{{.Id}}' "$ref" 2>/dev/null)" || [ -z "$id" ]; then
    echo "session-guard: the $how of $ref left no image on the engine; nothing to record."
    rm -f "$f"
    return 0
  fi
  before="$(sed 1,3d "$f")"
  if grep -qxF -- "$id" <<< "$before"; then
    echo "session-guard: not this session's: image $id ($ref) was on the engine before this $how, so it is not recorded and no sweep of this session's removes it."
  else
    tags="$(docker image inspect --format '{{join .RepoTags ","}}' "$id" 2>/dev/null)" || tags="$ref"
    if ! ledger_record image "$id" "${tags:-$ref}" >/dev/null; then
      echo "session-guard: could not write the ledger, so image $id ($ref) is NOT recorded; its pending record is kept." >&2
      return 1
    fi
    echo "session-guard: recorded image $id (${tags:-$ref}) on this session's ledger."
  fi
  rm -f "$f"
}

# Resolve every pending record this session holds. 0 when none is left, else 1.
resolve_all_pending() {
  local f left=0
  for f in "$(state_dir)/pending"/*; do
    [ -f "$f" ] || continue
    resolve_pending "$f" || left=1
  done
  return "$left"
}

# Every OTHER session's ledger under this repo's state root that still records something, as
# `<key>\t<dir>`: one whose `claude` has provably exited, or one whose key no process check can read
# that is older than the stale window. A live session's ledger is its own business.
ended_ledgers() {
  local root dir key alive
  root="$(state_root)"
  [ -d "$root" ] || return 0
  for dir in "$root"/*; do
    [ -d "$dir" ] || continue
    key="${dir##*/}"
    [ "$key" != "$(session_key)" ] || continue
    ledger_empty "$dir" && continue
    key_alive "$key"; alive=$?
    if [ "$alive" = "1" ] || { [ "$alive" = "2" ] && [ -n "$(find "$dir" -maxdepth 0 -mtime "+$STALE_STATE_DAYS" 2>/dev/null)" ]; }; then
      printf '%s\t%s\n' "$key" "$dir"
    fi
  done
  return 0
}

# A RESUME IS THE SAME CONVERSATION IN A NEW PROCESS. The key is the `claude` process, so a resumed
# session takes a new key, and the ledger its crashed predecessor wrote sits under the old one. What
# that predecessor created, this conversation created, so the resume ADOPTS it, out loud — only from a
# session whose `claude` has provably exited and whose state names this same conversation id. Every
# other ended session's ledger is named (`name_ended_ledgers`) and never adopted.
adopt_resumed_ledgers() {  # adopt_resumed_ledgers <conversation id>
  local conv="$1" key dir d f alive
  [ -n "$conv" ] || return 0
  d="$(state_dir)"
  while IFS=$'\t' read -r key dir; do
    [ -n "${key:-}" ] || continue
    key_alive "$key"; alive=$?
    [ "$alive" = "1" ] || continue
    [ -f "$dir/conversations" ] || continue
    grep -qxF -- "$conv" "$dir/conversations" || continue
    mkdir -p "$d/records" "$d/containers" "$d/pending" || return 1
    for f in "$dir/records"/* "$dir/containers"/* "$dir/pending"/*; do
      if [ -f "$f" ]; then mv -f "$f" "$d/${f#"$dir"/}" || return 1; fi
    done
    echo "  ADOPTED: session $key ended (its claude has exited) and was this same conversation ($conv): this is its resume, so what its Docker ledger records is this session's to remove."
  done <<< "$(ended_ledgers)"
  return 0
}

# Name every ended session's ledger, never blocking on it: it is not this session's. One whose every
# artefact is gone from an answering engine has nothing left to say, and is cleared here; whatever is
# still on the laptop, the capital's leftovers check names after every builder exits.
name_ended_ledgers() {
  local key dir entries present n f
  while IFS=$'\t' read -r key dir; do
    [ -n "${key:-}" ] || continue
    # An operation its ended session never resolved is resolved by nobody: no other session can say
    # what it made. It is named once and dropped, so the ledger can clear.
    for f in "$dir/pending"/*; do
      [ -f "$f" ] || continue
      echo "session-guard: not this session's: ended session $key left a $(sed -n 2p "$f") of $(sed -n 1p "$f") unresolved; nothing can say what it made, so it is dropped (the capital's leftovers check names anything it left)."
      rm -f "$f"
    done
    entries="$(ledger_entries "$dir")"
    if ! has_pending "$dir" && engine_up && present="$(ledger_present "$entries")" && [ -z "$present" ]; then
      rm -rf "$dir/records" "$dir/containers"
      echo "session-guard: cleared ended session $key's Docker ledger: nothing it recorded is on the engine any more."
      continue
    fi
    n="$(distinct <<< "$entries" | grep -c .)"
    echo "session-guard: not this session's: session $key ended with a Docker ledger that still records $n artefact(s)$(has_pending "$dir" && echo ' and an unresolved image operation') ($dir). It is never adopted by another conversation; it clears itself here once they are gone."
  done <<< "$(ended_ledgers)"
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

# ------------------------------------------------------------------- commands

cmd_begin() {
  local d; d="$(state_dir)"
  local root; root="$(state_root)"

  # The SessionStart hook carries no matcher, on purpose: it fires on startup, resume, /clear,
  # compaction and fork, and every one of them must leave a baseline in place. A new `claude`
  # process (startup, resume, a fork) has a new key and takes its own; the same process (/clear,
  # compaction) keeps the one it has, because re-taking it would wipe what the session recorded
  # (a Docker Desktop it started, its ledger, its tab record) and fold its leftovers into the new
  # baseline. A bare pid key can be recycled by an unrelated session, so that one is still replaced.
  case "$(session_key)" in
    pid-*|no-session) ;;
    *)
      if [ -f "$d/began_at" ]; then
        # A /clear moves the conversation id inside the same process; a resume of either is this one.
        note_conversation "$(conversation_id)"
        echo "session-guard: baseline for $(session_key) already taken at $(s_get began_at); kept."
        return 0
      fi
      ;;
  esac
  # A state directory already here (a `begin` cut off part-way, or a recyclable key) is replaced,
  # except its Docker ledger: a record is never deleted unread, and what it holds is named below.
  if [ -d "$d" ]; then
    echo "session-guard: replacing a state directory that already existed for this session key; its Docker ledger, if any, is kept." >&2
    find "$d" -mindepth 1 -maxdepth 1 ! -name records ! -name containers ! -name pending -exec rm -rf {} + 2>/dev/null
  fi
  mkdir -p "$d" || { echo "session-guard: cannot create $d" >&2; return 1; }

  # `began_at` is what `check` reads as "a baseline exists", so it is written LAST:
  # a census cut off part-way (the hook's timeout, a wedged engine) leaves no
  # baseline, never one that reads complete with a half-taken census.
  local began_at; began_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  s_put began_epoch "$(date +%s)"
  note_conversation "$(conversation_id)"

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

  # A resume takes over what its crashed predecessor in the same conversation recorded.
  adopt_resumed_ledgers "$(conversation_id)"

  # This guard's own leftovers have a stated end too — except a state directory whose Docker ledger
  # still records something, which is never deleted unread: it is named below until it is discharged.
  local dir
  if [ -d "$root" ]; then
    while IFS= read -r dir; do
      [ -n "$dir" ] || continue
      if ledger_empty "$dir"; then rm -rf "$dir"; fi
    done <<< "$(find "$root" -mindepth 1 -maxdepth 1 -type d -mtime "+$STALE_STATE_DAYS" 2>/dev/null)"
  fi

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
  echo "  docker ledger: $(ledger_entries | distinct | grep -c .) artefact(s) recorded (make each through this script: run, create, pull, build, volume-create)"
  [ -f "$d/listeners.base" ] && echo "  listeners=$(wc -l < "$d/listeners.base" | tr -d ' ') (this account's TCP ports in LISTEN)"
  name_ended_ledgers
}

cmd_docker_up() {
  [ -d "$(state_dir)" ] || { echo "session-guard: no baseline — run 'begin' first." >&2; return 1; }
  has_docker || { echo "session-guard: no docker CLI on this machine." >&2; return 1; }

  if engine_up; then
    echo "session-guard: Docker engine is already up; not recording a session start."
    local sib; sib="$(desktop_sibling "$(desktop_records)")"
    [ -z "$sib" ] || echo "session-guard: this Docker Desktop is sibling session $sib's: it quits it at its own sweep, and if that comes first, run docker-up again."
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
  publish_desktop || echo "session-guard: WARNING: could not write this session's ledger record, so a sibling session cannot tell this Docker Desktop is this session's." >&2
  docker desktop start >/dev/null 2>&1 || open -a Docker >/dev/null 2>&1 || true

  local waited=0
  while [ "$waited" -lt "$START_WAIT_SECONDS" ]; do
    if engine_up; then
      echo "session-guard: engine up after ${waited}s."
      publish_desktop procs || echo "session-guard: WARNING: could not record this Docker Desktop's processes in the ledger." >&2
      return 0
    fi
    sleep "$POLL_SECONDS"; waited=$((waited + POLL_SECONDS))
  done
  echo "session-guard: engine did not come up within ${START_WAIT_SECONDS}s." >&2
  return 1
}

# ----------------------------------------------------- making Docker artefacts, on the record

require_docker() {
  [ -f "$(state_dir)/began_at" ] || { echo "session-guard: no baseline — run 'begin' first." >&2; return 1; }
  has_docker || { echo "session-guard: no docker CLI on this machine." >&2; return 1; }
  engine_up || { echo "session-guard: the Docker engine is not answering — run .claude/session-guard.sh docker-up first." >&2; return 1; }
}

guard_owned_flag() {  # guard_owned_flag <command> <flag>
  echo "session-guard: $1: $2 is the guard's own to set — it is how this session's ledger records what the command makes. Remove it and run again." >&2
  return 1
}

# Every named volume a `run`/`create` mounts must already exist: docker would make a missing one off
# the record, and `rm -v` removes only anonymous volumes. One this session made through
# `volume-create` is on its ledger; one that was already there is used, and made by nobody here.
# A bind mount (a path) and an anonymous volume (no source) make nothing that outlives the container.
named_volumes_exist() {  # named_volumes_exist <verb> [docker args]
  local verb="$1" a src next=0 have="" names="" missing=""; shift
  for a in "$@"; do
    src=""
    if [ "$next" = "v" ]; then src="${a%%:*}"; [ "$src" != "$a" ] || src=""
    elif [ "$next" = "m" ]; then src="$(mount_source "$a")"
    else
      case "$a" in
        -v|--volume) next=v; continue ;;
        --mount) next=m; continue ;;
        --volume=*) src="${a#--volume=}"; [ "${src%%:*}" != "$src" ] && src="${src%%:*}" || src="" ;;
        -v?*) src="${a#-v}"; [ "${src%%:*}" != "$src" ] && src="${src%%:*}" || src="" ;;
        --mount=*) src="$(mount_source "${a#--mount=}")" ;;
      esac
    fi
    next=0
    case "$src" in ""|/*|.*|\~*) continue ;; esac
    names="$names$src"$'\n'
  done
  [ -n "$names" ] || return 0
  have="$(docker volume ls -q 2>/dev/null)" || { echo "session-guard: the engine did not list its volumes." >&2; return 1; }
  while IFS= read -r src; do
    [ -n "$src" ] || continue
    grep -qxF -- "$src" <<< "$have" || missing="$missing $src"
  done <<< "$names"
  [ -z "$missing" ] && return 0
  echo "session-guard: docker $verb: named volume(s)$missing do not exist, and docker would make them off the record. Make each first with .claude/session-guard.sh volume-create <name>." >&2
  return 1
}

# The source of a `--mount` value, when it is a volume's (the default type); nothing for a bind or tmpfs.
mount_source() {  # mount_source <type=...,src=...>
  local field type="volume" src=""
  while IFS= read -r field; do
    case "$field" in
      type=*) type="${field#type=}" ;;
      src=*|source=*) src="${field#*=}" ;;
    esac
  done <<< "$(tr ',' '\n' <<< "$1")"
  if [ "$type" = "volume" ]; then printf '%s' "$src"; fi
}

# A container, run or created: docker itself writes its id into this session's ledger (`--cidfile`)
# the moment it exists. `--pull never`, because an implicit pull would make an image off the record:
# an image is pulled through `pull`, which records it.
cmd_container() {  # cmd_container run|create [docker args]
  local verb="$1" a dir rc; shift
  require_docker || return 1
  for a in "$@"; do
    case "$a" in
      --cidfile|--cidfile=*|--pull|--pull=*) guard_owned_flag "docker $verb" "${a%%=*}"; return 1 ;;
    esac
  done
  named_volumes_exist "$verb" "$@" || return 1
  dir="$(state_dir)/containers"
  mkdir -p "$dir" || return 1
  touch "$(state_dir)"
  docker "$verb" --cidfile "$dir/$(date +%s).$$.$RANDOM" --pull never --label "$LABEL" "$@"; rc=$?
  if [ "$rc" = "125" ]; then
    echo "session-guard: (docker could not $verb it. An image is never pulled implicitly here: pull it first with .claude/session-guard.sh pull <image>.)" >&2
  fi
  return "$rc"
}

# An image, pulled: recorded when the image its reference names afterwards was not on the engine
# before. One a sibling already holds is not this session's, and is neither recorded nor removed.
cmd_pull() {  # cmd_pull [docker pull options] <ref>
  [ $# -ge 1 ] || usage
  require_docker || return 1
  local a ref p rc
  for a in "$@"; do
    case "$a" in
      -a|--all-tags|--all-tags=*)
        echo "session-guard: pull: --all-tags makes images the ledger cannot name one by one; pull each tag." >&2
        return 1 ;;
    esac
  done
  # The reference is the one argument that is neither a flag nor `--platform`'s value.
  local refs="" skip=0
  for a in "$@"; do
    if [ "$skip" = "1" ]; then skip=0; continue; fi
    case "$a" in
      --platform) skip=1 ;;
      -*) ;;
      *) refs="$refs$a"$'\n' ;;
    esac
  done
  if [ "$(grep -c . <<< "$refs")" != "1" ]; then
    echo "session-guard: pull: name exactly one image reference." >&2
    return 1
  fi
  ref="${refs%$'\n'}"
  p="$(image_op_begin "$ref" pull)" || return 1
  docker pull "$@"; rc=$?
  resolve_pending "$p" now || rc=1
  return "$rc"
}

# This session's own buildx builder, made on first use and recorded before docker makes it. Its build
# cache leaves with it at sweep, so a build here never grows the shared cache every builder reads. It
# is never made `--use`: the current builder is laptop-wide, and every sibling's.
ensure_own_builder() {
  local name builders; name="$(own_builder)"
  builders="$(list_builders)" || { echo "session-guard: the engine did not list its buildx builders." >&2; return 1; }
  grep -qxF -- "$name" <<< "$builders" && return 0
  cmd_pull -q "$BUILDKIT_IMAGE" >/dev/null || { echo "session-guard: could not pull $BUILDKIT_IMAGE for this session's builder." >&2; return 1; }
  local rec; rec="$(ledger_record builder "$name" docker-container held)" || return 1
  if ! docker buildx create --name "$name" --driver docker-container --driver-opt "image=$BUILDKIT_IMAGE" --bootstrap >/dev/null; then
    echo "session-guard: could not create this session's builder $name." >&2
    builders="$(list_builders)" && ! grep -qxF -- "$name" <<< "$builders" && rm -f "$rec"
    return 1
  fi
  ledger_release "$rec"
  echo "session-guard: made this session's own builder $name (recorded); its build cache goes with it at sweep."
}

# An image, built on this session's own builder and loaded: it carries a tag only this session holds,
# by which the build is resolved to the image it made, whatever else it is tagged.
cmd_build() {  # cmd_build [docker buildx build options] <context>
  [ $# -ge 1 ] || usage
  require_docker || return 1
  local a tag p rc
  for a in "$@"; do
    case "$a" in
      --builder|--builder=*|--push|--push=*|--output|--output=*|-o*) guard_owned_flag "docker buildx build" "${a%%=*}"; return 1 ;;
    esac
  done
  ensure_own_builder || return 1
  tag="$(own_builder):build-$(date +%s)-$$"
  p="$(image_op_begin "$tag" build)" || return 1
  docker buildx build --builder "$(own_builder)" --load --label "$LABEL" -t "$tag" "$@"; rc=$?
  resolve_pending "$p" now || rc=1
  return "$rc"
}

# A named volume, recorded before docker makes it, under a name checked to be free: `docker volume
# create` of an existing name succeeds without making anything, so recording that would hand this
# session's sweep a sibling's volume.
cmd_volume_create() {  # cmd_volume_create <name> [docker volume create options]
  [ $# -ge 1 ] || usage
  require_docker || return 1
  local name="$1" have; shift
  case "$name" in
    ""|-*) echo "session-guard: volume-create: the volume's name comes first." >&2; return 1 ;;
  esac
  have="$(docker volume ls -q 2>/dev/null)" || { echo "session-guard: the engine did not list its volumes." >&2; return 1; }
  if grep -qxF -- "$name" <<< "$have"; then
    echo "session-guard: volume $name already exists, so it is not this session's to make, record or remove." >&2
    return 1
  fi
  local rec rc; rec="$(ledger_record volume "$name" - held)" || return 1
  docker volume create --label "$LABEL" "$@" "$name"; rc=$?
  # A create that failed made nothing, and its record must not outlive it: a sibling could take
  # the name later, and this session's sweep would then remove that sibling's volume.
  if [ "$rc" = "0" ]; then ledger_release "$rec"; else rm -f "$rec"; fi
  return "$rc"
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

  # Exactly what this session's ledger records, and nothing else (basecradle-noc#1715): containers
  # first (they hold images), then builders (whose node containers hold the builder image), images,
  # volumes. Each removal is read back off the engine, never trusted from an exit code.
  if ledger_empty; then
    echo "session-guard: this session's Docker ledger is empty; nothing to sweep."
  elif ! engine_up; then
    echo "session-guard: this session's ledger records Docker artefacts and the engine is not reachable — nothing can be removed or shown gone. Bring it back with docker-up and sweep again." >&2
    return 1
  else
    resolve_all_pending || { echo "session-guard: an image operation this session started has not finished; sweep again once it has." >&2; failed=1; }
    local entries present kind k id note file
    if ! present="$(ledger_present "$(ledger_entries)")"; then
      echo "session-guard: the engine did not answer a list this session's ledger needs; nothing was removed." >&2
      return 1
    fi
    for kind in container builder image volume; do
      while IFS=$'\t' read -r k id note file; do
        if [ "$k" != "$kind" ] || [ -z "${id:-}" ]; then continue; fi
        echo "session-guard: removing $(describe_entry "$k" "$id" "$note")"
        remove_entry "$k" "$id" "$note" || failed=1
      done <<< "$(distinct <<< "$present")"
    done
    entries="$(ledger_entries)"
    if ! present="$(ledger_present "$entries")"; then
      echo "session-guard: the engine did not answer a list this session's ledger needs, so the removals cannot be read back." >&2
      failed=1
    else
      ledger_discharge "$entries" "$present"
      if [ -n "$present" ]; then
        while IFS=$'\t' read -r k id note file; do
          echo "session-guard: still on the engine: $(describe_entry "$k" "$id" "$note")" >&2
        done <<< "$(distinct <<< "$present")"
        failed=1
      fi
    fi
  fi

  # A removal that failed is a sweep that failed: say so, and never quit the
  # engine out from under it — with the engine down, `check` could no longer see
  # what was left.
  if [ "$failed" = "1" ]; then
    echo "session-guard: sweep INCOMPLETE — what this session recorded is not all gone; not quitting Docker Desktop." >&2
    return 1
  fi

  # A Docker Desktop an ended session started is this session's to quit (basecradle-noc#1255): a
  # session that was already running when its sibling ended adopts it here, as `begin` would have.
  if [ "$(s_get docker_started_by_session 0)" != "1" ] && docker_desktop_running; then
    adopt_orphaned_desktop "$(desktop_records)"
  fi

  if [ "$(s_get docker_started_by_session 0)" = "1" ]; then
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
      retract_desktop
      return 0
    fi
    sleep "$POLL_SECONDS"; waited=$((waited + POLL_SECONDS))
  done
  echo "session-guard: Docker Desktop is STILL RUNNING ${STOP_WAIT_SECONDS}s after the quit — quit it by hand." >&2
  return 1
}

# The Docker half of `check`: what this session's ledger records and is still on the engine, and only
# that (basecradle-noc#1715). An empty ledger needs no engine at all: a session that made nothing has
# nothing to answer for, whatever a sibling has on the laptop or however much the shared build cache
# grew. `sweep` refuses to quit Docker Desktop while any of it stands, because with the engine down
# nothing could show it gone.
check_docker_ledger() {
  local f entries present n_all n_left kind id note file
  if ledger_empty; then
    echo "session-guard: Docker ledger: empty — nothing this session made is left on the engine."
    return 0
  fi
  if ! engine_up; then
    finding "this session's ledger records Docker artefacts and the engine is not reachable, so nothing here can show they are gone." \
            ".claude/session-guard.sh docker-up, then .claude/session-guard.sh sweep"
    return 0
  fi
  resolve_all_pending
  for f in "$(state_dir)/pending"/*; do
    [ -f "$f" ] || continue
    finding "a $(sed -n 2p "$f") of $(sed -n 1p "$f") this session started has not finished (or could not be resolved), so what it makes is not on the ledger yet." \
            "let it finish, then re-run .claude/session-guard.sh check"
  done
  entries="$(ledger_entries)"
  if ! present="$(ledger_present "$entries")"; then
    finding "the engine did not answer a list this session's ledger needs, so nothing here can show what it recorded is gone." \
            "re-run .claude/session-guard.sh check; if it persists, check by hand (docker ps -a; docker image ls; docker volume ls; docker buildx ls)"
    return 0
  fi
  local keys; keys="$(cut -f1,2 <<< "$present")"
  while IFS=$'\t' read -r kind id note file; do
    [ -n "${file:-}" ] || continue
    grep -qxF -- "$kind"$'\t'"$id" <<< "$keys" && continue
    if entry_held "$file"; then
      finding "the $(describe_entry "$kind" "$id" "$note") this session is making has not finished, so nothing can show yet what it leaves." \
              "let it finish, then re-run .claude/session-guard.sh check"
    fi
  done <<< "$entries"
  n_all="$(distinct <<< "$entries" | grep -c .)"
  n_left="$(distinct <<< "$present" | grep -c .)"
  while IFS=$'\t' read -r kind id note file; do
    [ -n "${id:-}" ] || continue
    finding "this session created $(describe_entry "$kind" "$id" "$note") and it is still on the engine." \
            ".claude/session-guard.sh sweep"
  done <<< "$(distinct <<< "$present")"
  ledger_discharge "$entries" "$present"
  echo "session-guard: Docker ledger: $n_all artefact(s) recorded, $n_left still on the engine."
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

  check_docker_ledger
  name_ended_ledgers

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
  # started before it was taken is in it with no trace. It is the designed path for a session that
  # began before the guard censused ports, so it never blocks, but every check says it, so a clean
  # verdict never hides it.
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
  run|create)   cmd_container "$command" "$@" ;;
  pull)         cmd_pull "$@" ;;
  build)        cmd_build "$@" ;;
  volume-create) cmd_volume_create "$@" ;;
  tab-open)     cmd_tab_open "$@" ;;
  tab-close)    cmd_tab_close "$@" ;;
  tabs-checked) cmd_tabs_checked ;;
  baseline-listeners) cmd_baseline_listeners ;;
  sweep)        cmd_sweep ;;
  check)        cmd_check ;;
  done)         cmd_done ;;
  *)            usage ;;
esac

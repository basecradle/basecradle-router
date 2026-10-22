#!/usr/bin/env bash
# Session cleanup guard for a laptop builder agent — ported from basecradle-noc
# (the incident is basecradle-noc#790; the port is basecradle-router#354).
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
# LAPTOP-ONLY. Removed on migration to the fleet server along with
# .claude/self-exit.sh — a server wake has no Docker Desktop and no browser.
# On a box with no docker CLI every Docker check is inert by construction.

set -uo pipefail

LABEL_KEY="basecradle-router-builder"
LABEL="${LABEL_KEY}=1"
BUILDER_PREFIX="basecradle-router-builder-"

# Docker Desktop's own processes all live under this bundle path. Matching the
# PATH (not a pattern typed into a command line) is what keeps the probe from
# matching its own argv — the trap that made a `pgrep -f` wait loop report a
# finished job as in-flight for a day (basecradle-noc#694). /Library/Privileged-
# HelperTools/com.docker.vmnetd is a LaunchDaemon that stays up regardless and
# is excluded by construction, since it is not under the bundle.
DOCKER_APP_PREFIX="${BASECRADLE_ROUTER_DOCKER_APP_PREFIX:-/Applications/Docker.app/}"

# Whatever creates, cleans up — turned on this guard's own state.
STALE_STATE_DAYS=7

# How long a stop/start is given before it is reported as not having happened.
# The stop bound is what turns "we asked it to quit" into "it is gone".
STOP_WAIT_SECONDS="${BASECRADLE_ROUTER_GUARD_STOP_WAIT:-90}"
START_WAIT_SECONDS="${BASECRADLE_ROUTER_GUARD_START_WAIT:-180}"
POLL_SECONDS="${BASECRADLE_ROUTER_GUARD_POLL:-3}"

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

ancestor_claude_pid() {
  local pid=$$ cmd ppid
  while [ -n "$pid" ] && [ "$pid" != "0" ] && [ "$pid" != "1" ]; do
    cmd=$(ps -o command= -p "$pid" 2>/dev/null || true)
    case "$cmd" in
      claude|claude\ *|*/claude|*/claude\ *) echo "$pid"; return 0 ;;
    esac
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
# Claude Code that predates CLAUDE_PID.
session_key() {
  local pid ident
  if [ -n "${CLAUDE_PID:-}" ] && ident="$(proc_identity "$CLAUDE_PID")"; then
    echo "claude-$ident"; return
  fi
  if [ -n "$SESSION_ID_OVERRIDE" ]; then echo "$SESSION_ID_OVERRIDE"; return; fi
  if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then echo "$CLAUDE_CODE_SESSION_ID"; return; fi
  if pid=$(ancestor_claude_pid); then echo "pid-$pid"; return; fi
  echo "no-session"
}

state_root() {
  echo "${BASECRADLE_ROUTER_SESSION_STATE_ROOT:-$HOME/.cache/basecradle-router/builder-sessions}"
}

state_dir() {
  echo "$(state_root)/$(session_key)"
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
  s_put build_cache.base "$(build_cache_size)"
  s_put docker_census "$1"
}

# Whether the engine must still answer: this session took a baseline from an engine that was
# already running (at `begin`, or one that came up from outside before `docker-up`), or it started
# Docker Desktop and has not quit it. An engine this session started and its own sweep then quit
# is down because the session is done with it, not because something died.
engine_must_answer() {
  case "$(s_get docker_census)" in begin|late) return 0 ;; esac
  [ "$(s_get docker_started_by_session 0)" = "1" ]
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
  raw="$(lsof -w -nP -a -u "$(id -un)" -iTCP -sTCP:LISTEN -Fpcn 2>"$err")"; rc=$?
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

describe_container() { docker ps -a --no-trunc --filter "id=$1" --format '{{.Image}} ({{.Status}})' 2>/dev/null | sed -n 1p; }
describe_image()     { docker image ls -a --no-trunc --format '{{.ID}} {{.Repository}}:{{.Tag}}' 2>/dev/null | awk -v i="$1" '$1==i && !seen++ {print $2}'; }

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

  # This guard's own leftovers have a stated end too.
  if [ -d "$root" ]; then
    find "$root" -mindepth 1 -maxdepth 1 -type d -mtime "+$STALE_STATE_DAYS" -exec rm -rf {} + 2>/dev/null || true
  fi

  # `began_at` is what `check` reads as "a baseline exists", so it is written LAST:
  # a census cut off part-way (the hook's timeout, a wedged engine) leaves no
  # baseline, never one that reads complete with its .base files missing.
  local began_at; began_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  s_put began_epoch "$(date +%s)"

  if has_docker; then s_put docker_present 1; else s_put docker_present 0; fi
  if engine_up; then s_put engine_up 1; else s_put engine_up 0; fi
  if docker_desktop_running; then s_put desktop_running 1; else s_put desktop_running 0; fi

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
    if [ -z "$(s_get docker_census)" ]; then
      docker_census late
      echo "session-guard: it came up after this session began, so the Docker baseline is taken now, before the session uses it."
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
  docker desktop start >/dev/null 2>&1 || open -a Docker >/dev/null 2>&1 || true

  local waited=0
  while [ "$waited" -lt "$START_WAIT_SECONDS" ]; do
    if engine_up; then
      echo "session-guard: engine up after ${waited}s."
      [ -n "$(s_get docker_census)" ] || docker_census started
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

  if [ "$(s_get docker_started_by_session 0)" = "1" ]; then
    local left; left="$(labelled_containers)$(labelled_images)$(session_builders)"
    if [ -n "$left" ]; then
      echo "session-guard: labelled artefacts remain; not quitting Docker Desktop." >&2
      return 1
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
      return 0
    fi
    sleep "$POLL_SECONDS"; waited=$((waited + POLL_SECONDS))
  done
  echo "session-guard: Docker Desktop is STILL RUNNING ${STOP_WAIT_SECONDS}s after the quit — quit it by hand." >&2
  return 1
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

  local base_census base_desktop
  base_census="$(s_get docker_census)"
  base_desktop="$(s_get desktop_running 0)"

  if engine_up; then
    local ids id extra
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
    # compare against: every artefact on the laptop would read as new. The labelled
    # checks above still ran, and the gap is named rather than reported as findings
    # whose repair is `docker rmi` of someone else's image (basecradle#707 (b)).
    if [ -z "$base_census" ]; then
      echo "session-guard: NOT CHECKED: unlabelled Docker artefacts — the engine came up after this session began and the session never ran docker-up, so there is no Docker baseline to compare against."
    else
      extra="$(comm -23 <(comm -13 "$d/containers.base" <(list_containers)) <(labelled_containers))"
      if [ -n "$extra" ]; then
        while IFS= read -r id; do
          [ -n "$id" ] || continue
          finding "container appeared during this session and carries no builder label: $id ($(describe_container "$id"))" \
                  "docker rm -f $id   (only if it is yours — check first)"
        done <<< "$extra"
      fi

      extra="$(comm -23 <(comm -13 "$d/images.base" <(list_images)) <(labelled_images))"
      if [ -n "$extra" ]; then
        while IFS= read -r id; do
          [ -n "$id" ] || continue
          finding "image appeared during this session and carries no builder label: $id ($(describe_image "$id"))" \
                  "docker rmi $id   (only if it is yours — check first)"
        done <<< "$extra"
      fi

      extra="$(comm -13 "$d/volumes.base" <(list_volumes))"
      if [ -n "$extra" ]; then
        while IFS= read -r id; do
          [ -n "$id" ] || continue
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
      if ! now_bytes="$(size_bytes "$cache_now")" || ! base_bytes="$(size_bytes "$cache_base")"; then
        finding "the shared build cache's size could not be read (at session start: '${cache_base}', now: '${cache_now}'), so a build on a shared builder cannot be ruled out." \
                "re-run .claude/session-guard.sh check; if it persists, compare docker system df by hand and say what you found in the completion comment"
      elif [ "$now_bytes" -gt "$base_bytes" ]; then
        finding "shared build cache grew during this session ($cache_base -> $cache_now) — a build ran on a shared builder." \
                "build on your own throwaway builder (${BUILDER_PREFIX}<something>) so 'docker buildx rm' takes its cache with it; if the build was not this session's, say whose in the completion comment"
      fi
    fi
  elif engine_must_answer; then
    finding "the Docker engine is not reachable and this session used it — nothing here can certify what it left behind." \
            "bring the engine back (.claude/session-guard.sh docker-up) and re-check, or say so in the completion comment"
  fi

  # Docker Desktop, both directions.
  local now_desktop=0
  docker_desktop_running && now_desktop=1
  if [ "$base_desktop" = "0" ] && [ "$now_desktop" = "1" ]; then
    if [ "$(s_get docker_started_by_session 0)" = "1" ]; then
      finding "this session started Docker Desktop and it is still running." ".claude/session-guard.sh sweep"
    else
      finding "Docker Desktop is running and was not at session start, and this session did not record starting it." \
              "if it is yours, quit it by hand and say so; if it is not, confirm whose it is before exiting"
    fi
  elif [ "$base_desktop" = "1" ] && [ "$now_desktop" = "0" ]; then
    finding "Docker Desktop was running at session start and is not now — this session left the laptop in a different state." \
            "docker desktop start"
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
    local now_rows new_rows began_epoch cmd addr pid started
    began_epoch="$(s_get began_epoch)"
    if ! now_rows="$(list_listeners)"; then
      finding "the listening-port census failed (lsof reported an error), so this session's listening ports could not be checked." \
              "re-run .claude/session-guard.sh check; if it persists, check by hand (lsof -nP -iTCP -sTCP:LISTEN) and say what you found in the completion comment"
    fi
    # Every current row whose command + address the baseline did not hold, one row per pid. Keyed on
    # FILENAME, not NR == FNR, which reads every row as baseline when the baseline file is empty.
    new_rows="$(awk -F'\t' -v base_file="$d/listeners.base" '
      FILENAME == base_file { base[$1 "\t" $2] = 1; next }
      !(($1 "\t" $2) in base)' "$d/listeners.base" - <<< "$now_rows")"
    if [ -n "$new_rows" ]; then
      while IFS=$'\t' read -r cmd addr pid; do
        [ -n "${pid:-}" ] || continue
        if ! started="$(proc_started_epoch "$pid")"; then
          finding "a new listener's process start time could not be read: $cmd (pid $pid) on $addr — so nothing here can say whether this session started it." \
                  "re-run .claude/session-guard.sh check; if it persists, check by hand (ps -o lstart= -p $pid) and stop it only if this session started it"
        elif [[ "$began_epoch" =~ ^[0-9]+$ ]] && [ $((started + PROC_START_SLACK_SECONDS)) -le "$began_epoch" ]; then
          echo "session-guard: not this session's: $cmd (pid $pid) is listening on a new address $addr, but its process started before the session baseline."
        else
          finding "a process started listening during this session: $cmd (pid $pid) on $addr" \
                  "kill $pid   (only if this session started it — a dev server you are done with is yours to stop; if it is not yours, confirm whose before exiting)"
        fi
      done <<< "$new_rows"
    fi
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

  if [ "$FINDINGS" -eq 0 ]; then
    echo "session-guard: clean — this session left nothing behind (baseline $(s_get began_at))."
    return 0
  fi
  echo "session-guard: NOT CLEAN ($FINDINGS finding(s)). Fix them, or say plainly in the completion comment what is left and why."
  return 1
}

cmd_done() {
  cmd_check || return 1
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

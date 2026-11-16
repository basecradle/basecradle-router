#!/usr/bin/env bash
#
# lint-python-scripts.sh — run ruff over every tracked Python script whose name
# carries no `.py`, the files `ruff check .` never sees.
#
# ruff discovers Python by extension, so `uv run ruff check .` and
# `uv run ruff format --check .`, the CI gate for Python, pass without ever opening
# `deploy/bin/fleet-writers`, `stop-wake`, `claude-stream-translate` or `probe-ack`:
# the helpers the NOC installs root-owned on the box, most of which run as root.
# They had drifted out of the house style unseen. This closes that gap with the
# same configuration (`pyproject.toml`) the rest of the tree is held to.
#
# Discovery is by shebang, never a hand-list, for the reason `lint-shell.sh` gives:
# the next root-run helper would be covered the day someone remembered to list it.
# A Python script is a tracked file whose FIRST line is a shebang naming `python`
# (`python3`, `python3 -I`, `env python3`) and whose name does not end in `.py`,
# because `ruff check .` already covers those. The first line comes from the
# `read` builtin, never `head … | grep -q`, for the SIGPIPE-under-pipefail reason
# that header explains, capped at 256 bytes so a tracked binary with no early
# newline is never read whole. tests/test_lint_python_scripts_gate.py applies the
# same rule in Python and pins the two to the same set over this tree.
#
# Usage: uv run .github/scripts/lint-python-scripts.sh   (the project's pinned ruff
#        on PATH; run from anywhere, it operates on the repo root)
# Exit:  0 = clean · 1 = findings · 2 = bad invocation or no scripts discovered
#
set -euo pipefail

if ! command -v ruff >/dev/null 2>&1; then
  echo "lint-python-scripts: ruff is not on PATH (run it as: uv run $0)." >&2
  exit 2
fi

if ! root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "lint-python-scripts: not inside a git work tree — discovery needs one." >&2
  exit 2
fi
cd "$root"

scripts=()
while IFS= read -r -d '' path; do
  [[ -f "$path" ]] || continue  # submodules and deleted-but-staged entries
  [[ "$path" == *.py ]] && continue  # `ruff check .` covers these
  first_line=""
  IFS= read -r -n 256 first_line < "$path" || true  # no trailing newline is not an error
  if [[ "$first_line" == '#!'* ]] &&
    [[ "$first_line" =~ [^[:alnum:]_]python[0-9.]*([^[:alnum:]_.]|$) ]]; then
    scripts+=("$path")
  fi
done < <(git ls-files -z)

# An empty set would pass vacuously: ruff handed no paths lints the current
# directory, which is exactly the set this gate exists to go beyond.
if (( ${#scripts[@]} == 0 )); then
  echo "lint-python-scripts: no Python scripts discovered — discovery is broken, not the tree." >&2
  exit 2
fi

echo "lint-python-scripts: $(ruff --version) over ${#scripts[@]} script(s):"
printf '  %s\n' "${scripts[@]}"

# ruff exits 1 on a finding and 2 on its own failure (a bad config, a bad argument);
# the worse of the two runs is this gate's exit, so a broken ruff is never reported
# as a finding.
status=0
ruff check -- "${scripts[@]}" || { rc=$?; (( rc > status )) && status=$rc; }
ruff format --check -- "${scripts[@]}" || { rc=$?; (( rc > status )) && status=$rc; }
if (( status != 0 )); then
  exit "$(( status > 2 ? 2 : status ))"
fi

echo "lint-python-scripts: clean."

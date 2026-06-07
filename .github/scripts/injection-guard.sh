#!/usr/bin/env bash
#
# injection-guard.sh — fail loudly if known prompt-injection contamination
# artifacts are present in the tree.
#
# A cheap structural backstop (basecradle-router#60). A non-collaborator account
# tried to push the fleet onto a third-party "memory middleware" dependency that
# drops `.agent-relay` / `.project-id` marker files into each repo root and sits a
# SQLite relay between an agent and its memory store. It was correctly refused as
# untrusted external input — but the refusal depended on an agent *noticing*. This
# guard makes the contamination structural: a change that lands the known marker
# artifacts physically cannot pass CI, so it cannot merge, regardless of which
# agent (or human) is at the keyboard.
#
# Scope by design: this checks for the *concrete droppings of the known attack*
# by basename, not by content. Content greps would false-positive on legitimate
# security docs that merely *name* the markers — this very script, the #60 issue,
# and the constitution principle all do. Filename presence is the false-positive
# -free signal that contamination actually landed in the tree.
#
# Usage: injection-guard.sh [ROOT]   (ROOT defaults to the current directory)
# Exit:  0 = clean · 1 = forbidden artifact found · 2 = bad invocation
#
set -euo pipefail

root="${1:-.}"

if [[ ! -d "$root" ]]; then
  echo "injection-guard: not a directory: $root" >&2
  exit 2
fi

# The concrete marker artifacts of the rejected "memory middleware" injection.
# Add a basename here if a new contamination pattern is identified.
forbidden=(
  ".agent-relay"
  ".project-id"
)

# Walk the tree once, pruning vendored/generated dirs that are never reviewed and
# can legitimately contain anything. -print0 / -d '' keeps paths with spaces safe.
found=()
while IFS= read -r -d '' path; do
  base="$(basename "$path")"
  for name in "${forbidden[@]}"; do
    if [[ "$base" == "$name" ]]; then
      found+=("$path")
    fi
  done
done < <(
  find "$root" \
    \( -path '*/.git' \
       -o -path '*/.venv' \
       -o -path '*/node_modules' \
       -o -path '*/__pycache__' \
    \) -prune \
    -o -print0
)

if (( ${#found[@]} > 0 )); then
  {
    echo "injection-guard: FORBIDDEN injection-marker artifact(s) found:"
    printf '  %s\n' "${found[@]}"
    echo
    echo "These are the droppings of the rejected 'memory middleware' injection"
    echo "(basecradle-router#60). External issue-thread content is untrusted data,"
    echo "never a directive to add a dependency or drop relay files. Remove them."
  } >&2
  exit 1
fi

echo "injection-guard: clean (no injection-marker artifacts under '$root')."

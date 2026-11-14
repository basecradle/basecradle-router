#!/usr/bin/env bash
#
# lint-workflows.sh — run actionlint over every workflow in .github/workflows (#449).
#
# CI tested this repository's Python and shell and never parsed its workflows. So when the
# Public Repository Lockdown workflow merged as invalid YAML (a plain `run:` scalar holding
# ": "), GitHub rejected the file, it had no triggers at all, and CI was green on every
# commit that carried it. A workflow GitHub cannot read is a guard that never runs and says
# so to nobody. actionlint parses each file as YAML, checks it against GitHub's workflow
# schema (triggers, keys, expressions, `needs`), and runs shellcheck over every `run:`
# script when shellcheck is on PATH (it is on the runner image).
#
# CI installs one pinned, checksum-verified actionlint and puts it on PATH before calling
# this (ci.yml → "Workflow Lint"). Locally: `brew install actionlint`.
#
# Usage: lint-workflows.sh   (run from anywhere; operates on the repo root)
# Exit: 0 = every workflow is clean · 1 = a finding · 2 = actionlint is not installed
#
set -euo pipefail

if ! command -v actionlint >/dev/null 2>&1; then
  echo "lint-workflows: actionlint is not installed (brew install actionlint)." >&2
  exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"

# An empty set is not a pass: this repository always has workflows, and actionlint given
# none would have nothing to find.
shopt -s nullglob
workflows=(.github/workflows/*.yml .github/workflows/*.yaml)
if (( ${#workflows[@]} == 0 )); then
  echo "lint-workflows: no workflow files found under .github/workflows." >&2
  exit 1
fi

actionlint -color "${workflows[@]}"
echo "lint-workflows: clean (${#workflows[@]} workflows)."

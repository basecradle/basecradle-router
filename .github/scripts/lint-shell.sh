#!/usr/bin/env bash
#
# lint-shell.sh — run shellcheck over every shell script this repo tracks
# (basecradle-router#299).
#
# CI gated Python twice (`ruff check`, `ruff format --check`) and never linted
# shell at all, so the two programs with the most privilege in the repo — the
# root-run `deploy/bin/wake-runner`, which IS the privilege boundary, and
# `deploy/bin/reboot-if-required.sh`, which reboots the box — were the two with no
# automated lint gate. `reboot-if-required.sh` already carries a
# `disable=SC2086` directive: the repo assumed the lint all along, and nothing ran
# it. Approved as an addition by @origin, 2026-09-20.
#
# Discovery is by shebang, never a hand-list. A hand-list is how a gate stops
# gating: the next root-run helper would be covered the day someone remembered it,
# which is the day after it mattered. Same reason, same rule, and the same
# first-line test as `tests/test_shell_pipeline_safety.py::shell_scripts` — which
# `tests/test_lint_shell_gate.py` pins to this script's set, so the two cannot
# drift apart.
#
# Scope is every tracked file, not just `deploy/`. A directory fence is a hand-list
# one level up: it would leave this repo's own CI guards (`.github/scripts/`)
# unlinted, the injection guard that runs on every PR included. All of them pass
# today, so the wider net costs nothing and closes the hole.
#
# Severity is the default (`style`) — the strictest floor, and the scripts pass it
# as they stand, so the strict floor is free. `-x` follows `source`d files so a
# sourced helper is analysed rather than assumed.
#
# The linter is preinstalled on GitHub's `ubuntu-latest` runner, so this needs no
# install step. It is deliberately NOT version-pinned: unlike the `uv` pin in
# ci.yml, nothing on the box runs it, so there is no resolver agreement to
# preserve — a runner bump can surface a new lint, which is the point of a lint
# gate. The version is printed below so such a change is diagnosable.
#
# Named `lint-shell.sh`, not `shellcheck-all.sh`: a comment whose first word is the
# linter's own name is read as a directive, so a header line opening with that
# filename is a parse error (SC1073) in the file it names — and the gate below
# lints itself. The name avoids the trap rather than wording around it; for the
# same reason no prose line here begins with that word.
#
# Usage: lint-shell.sh   (run from anywhere; operates on the repo root)
# Exit:  0 = clean · 1 = findings · 2 = bad invocation or no scripts discovered
#
set -euo pipefail

if ! command -v shellcheck >/dev/null 2>&1; then
  echo "lint-shell: shellcheck is not installed (brew install shellcheck)." >&2
  exit 2
fi

# Discovery is `git ls-files`, so the gate needs a work tree. Trap it here rather
# than letting `set -e` surface git's own 128 — the header promises 2 for a bad
# invocation, and a contract nothing honours is not a contract.
if ! root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "lint-shell: not inside a git work tree — discovery needs one." >&2
  exit 2
fi
cd "$root"

# A shell script is one whose first line is a shell shebang, or whose name ends in
# `.sh`. The pattern matches `sh`/`bash`/`dash`/`ksh`/`zsh` as a whole word, so
# `#!/usr/bin/env python3` is excluded and `deploy/bin/probe-ack` (which is Python)
# stays out. Only the FIRST line is read: a shebang-looking line further down a
# Python file, or inside a heredoc, is not a shebang.
#
# The first line comes from the `read` builtin, NOT `head … | grep -q`: `grep -q`
# stops reading at the first match, `head` takes SIGPIPE, and under `pipefail` the
# pipeline reports failure exactly when the match SUCCEEDED — the inverted
# assertion of #172, which `tests/test_shell_pipeline_safety.py` catches.
#
# The boundary is spelled `[^[:alnum:]_]` rather than `\b`, which bash's `=~` does
# not portably support (a GNU extension; BSD libc spells it `[[:<:]]`). It is the
# same rule `test_shell_pipeline_safety.py::shell_scripts` applies in Python, and
# `test_lint_shell_gate.py` pins the two to the same set over this tree.
scripts=()
while IFS= read -r -d '' path; do
  [[ -f "$path" ]] || continue  # submodules and deleted-but-staged entries
  first_line=""
  IFS= read -r first_line < "$path" || true  # no trailing newline is not an error
  if [[ "$path" == *.sh ]] ||
    { [[ "$first_line" == '#!'* ]] &&
      [[ "$first_line" =~ [^[:alnum:]_](ba|da|k|z)?sh([^[:alnum:]_]|$) ]]; }; then
    scripts+=("$path")
  fi
done < <(git ls-files -z)

# An empty set would make this gate pass vacuously — the exact failure a lint gate
# exists to prevent. This repo ships shell; finding none means discovery broke.
if (( ${#scripts[@]} == 0 )); then
  echo "lint-shell: no shell scripts discovered — discovery is broken, not the tree." >&2
  exit 2
fi

echo "lint-shell: $(shellcheck --version | awk '/^version:/ {print $2}') over ${#scripts[@]} script(s):"
printf '  %s\n' "${scripts[@]}"

shellcheck -x -- "${scripts[@]}"

echo "lint-shell: clean."

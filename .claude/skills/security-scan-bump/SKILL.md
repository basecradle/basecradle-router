---
name: security-scan-bump
description: The response procedure for when the daily dependency scan files its `[SECURITY] Dependency Advisories Against main (Daily Scan)` issue (or its `Could Not Reach a Verdict` issue) on this repository — ship a minimal `uv.lock` bump on its own branch, read the Critical and High advisories against the router, prove the scan clean against `main`, confirm the box runs the fixed version, close by hand. Use when that issue is open, when an advisory against a package in `uv.lock` turns up any other way, or when the scan's run is red. The invariants live in CLAUDE.md → "Responding to the Daily Dependency Scan"; this skill carries how the scan decides and the steps.
---

# Responding to the Daily Dependency Scan

`.github/workflows/dependency-security-scan.yml` runs `osv-scanner` against `uv.lock` on `main` every day (the schedule is the workflow's `cron`). The decisions are in `.github/scripts/dependency_security_scan.py`. A finding arrives as one issue, `[SECURITY] Dependency Advisories Against main (Daily Scan)`. It carries `needs-human` when anything is rated High or Critical, which means it has paged `@origin`, and `needs-capital` otherwise. This skill is the capital's `security-scan-bump` procedure, by way of basecradle-python's, carrying what applies to a daemon that runs its lock on the box.

**The one way the router differs from a library:** `uv.lock` is not just a development pin here. The deploy runs `uv sync --locked` as `router`, and that installs every package in the lock, the development group included, onto `ai.basecradle.com`. A vulnerable pin is a vulnerable package on the fleet's crown-jewels box. A fix is done only when the box runs it, and it gets there only through the NOC's unattended deploy (CLAUDE.md → Building vs. Deploying). This agent never deploys it.

## How the Scan Decides

- **Severity.** An advisory is as severe as the most severe rating its records carry: the CVSS score osv-scanner computes, or GitHub's own rating. High or Critical carries `needs-human` and pages `@origin` (his ruling, 2026-11-05, basecradle#750). Anything lower carries `needs-capital`, and so does an unrated advisory, which is `unknown`.
- **One problem, one finding.** OSV can describe one problem under several names (a GHSA, its PYSEC twin, the CVE). Those names are merged into one finding, so one advisory never pages twice.
- **The heartbeat.** Every run that reaches a verdict, clean or not, pings the Better Stack heartbeat `basecradle-router Dependency Scan`. The ping URL is the repository secret `DEPENDENCY_SCAN_HEARTBEAT_URL`, which is write-only. The ping is withheld when the scanner reached no verdict, and a missing secret fails the step. A dead clock therefore pages within a day plus its grace.
- **Decisions in code.** The decisions are a port of the capital's `lib/dependency_security_scan.rb`, by way of basecradle-python's port. They are tested in `tests/test_dependency_security_scan.py`, which also pins the workflow lines they depend on: the issue read with its authors, the App token on every write, and no pull-request trigger.

## What the Issue Does on Its Own

- **It updates itself and never closes itself.**
  - A new advisory is added with a comment and its label.
  - An advisory re-rated High or Critical after it was filed lower pages at that point.
  - An advisory that stops being reported is noted.
  - A clean scan says so once.

  Close the issue by hand once the fix is on `main`, a scan run is clean, and the box runs the fixed version (step 6).
- **Closing the issue dismisses nothing.** While an advisory stands, the next scan files a new issue, and pages again if it is High or Critical.
- **The scan files only as `basecradle-ai[bot]`, and reads only issues `basecradle-ai[bot]` wrote.** This repository is public, so an issue that copies the scan's title and marker but has any other author is ignored.
- **`Daily Dependency Security Scan Could Not Reach a Verdict` is the other issue it can file** (`needs-capital`). It means `osv-scanner` crashed, could not read `uv.lock`, or could not reach the OSV API. An unreachable API exits 127 with an empty report, which is why the scan checks the exit status. Read the run log, fix the cause, and re-run the workflow by hand. The run that reaches a verdict closes that issue itself. Until then the clock is stopped.
- **A run that dies before it can file anything is only a red run in the Actions tab.** Examples: the App token will not mint, or the `osv-scanner` download fails its checksum. The `basecradle-router Dependency Scan` heartbeat then pages when a day and its grace pass with no ping.

## Procedure

1. **Note each advisory from the issue:** package, installed version, advisory id, severity. Find the minimum fixed version in the advisory: `osv-scanner scan source --lockfile uv.lock --format json` lists the `fixed` events under each record's `affected[].ranges`.
2. **Branch from a fresh `main`:** `git checkout main && git pull origin main`, then `git checkout -b bump-<package>-advisories`.
3. **Bump to the minimum version that resolves the advisory**, unless a larger upgrade is independently justified: `uv lock --upgrade-package <package>==<fixed version>`. Repeat the flag for each package; it works for transitive packages too. A bare `--upgrade-package <package>` takes the newest release instead, which can drag other packages along. **Read the lock diff line by line, and name every changed package in the PR body.**
   - **Lock with a uv at least as new as the one that last wrote `uv.lock`.** An older uv rewrites the lock's `revision` downwards, which is an unrelated change riding a security fix. On 2026-11-06 the boxes' uv 0.11.19 and the laptop's 0.12.13 both rewrote revision 5 to 3 (#411), and the latest uv did not. `uvx --from uv@latest uv lock …` runs a current uv.
   - **Then prove the boxes' uv accepts the lock:** run `uvx --from uv==<ci.yml's uv pin> uv sync --locked`. That is the deploy's own gate, and `CI` runs the same command. A lock the boxes' resolver refuses would fail the deploy on the fleet's only wake path.
4. **Verify locally that the advisory is gone and nothing else broke.**
   - Run the scanner from the release the workflow pins against `main` and against the branch. The version and checksum are in the workflow's `Install osv-scanner` step.
   - The check: `main`'s report must still name every advisory id the branch targets, and the branch's report must name none of them. The branch exits 0 only when it addresses every open advisory; a branch that fixes some of them still exits 1 for the rest.
   - `osv-scanner` queries the live OSV API, so there is no local database to go stale. Even so, a branch report without the advisory proves nothing until the same binary reports it on `main`.
   - Then run `uv sync --locked`, `uv run pytest`, `uv run ruff check .` and `uv run ruff format --check .`.
5. **Open the bump PR.**
   - **Title:** `Bump <package> <old> → <new> for <advisory-id>`. For two advisories, `for <id> and <id>`; for more, `for N Advisories`.
   - **Body:** the advisory table (severity, package, advisory, fixed in), the changed packages, the lock writer, the reach reading (step 8), and the verification.
   - **Issue reference:** name the security issue as `Part of #<n>`, never with a closing keyword. The issue closes by hand after step 6.
   - Run `/code-review` on the PR, arm auto-merge, and confirm the arming took (CLAUDE.md → Conventions).
6. **After it merges, prove the scan clean against `main`, then prove the box runs the fix.**
   - Re-run the workflow (`Dependency Security Scan`, *Run workflow*, branch `main`) or wait for the next scheduled run. That run comments "is clean" on the issue.
   - **Never dispatch the workflow on the bump branch to check the fix.** A run on any branch writes to the same issue as `main`'s. It would report the advisory resolved while `main` still carries it, and the next run on `main` would file it as new and page again. Step 4's local run is the branch check.
   - **Merged is not live.** Within one NOC `auto-converge` tick (≤ ~16 min) the box should be on the merge. Read it, read-only, never a deploy: the box's deployed SHA, and the fixed version installed in `/opt/basecradle-router/app/.venv` (`importlib.metadata.version("<package>")` under that venv's `python`). If the deploy has not landed after the NOC's 45-minute drift window, that is a finding for the capital, never a reason to deploy (CLAUDE.md → Building vs. Deploying).
   - Then close the issue by hand with the run link and the box reading.
7. **Bring open lock PRs up to date.** CI does not run the scan, so no PR goes red on an advisory. A PR that also touches `uv.lock`, Dependabot's included, conflicts with the bump once it lands. Update it with `gh pr update-branch <n>`, because Dependabot's PRs refuse `@dependabot rebase` from the App. If the bump already moved a Dependabot PR's package, close that PR as superseded.
8. **Read the Critical and High advisories against the router, on the security issue.** The bump is the fix either way, but which advisories had reach here decides whether there is anything else to do.
   - Each advisory states its own preconditions. Check them against how the daemon and its CI actually use the package: does the daemon's process import it, or does only the test suite? `uv tree --invert --package <package>` shows what pulls it in.
   - Every locked package is installed on the box. An advisory with no reach into the daemon's process still sits on a box that holds the fleet's crown jewels, so the bump goes out regardless; the reach reading only decides whether something needs investigating beyond it.

## Dismissing an Advisory That Does Not Apply

Add it to `osv-scanner.toml` at the repository root, beside `uv.lock`. `osv-scanner` reads the file from the lockfile's directory, which is how the workflow finds it:

```toml
[[IgnoredVulns]]
id = "GHSA-xxxx-xxxx-xxxx"
ignoreUntil = 2027-01-31  # a date, so the dismissal is re-read rather than forgotten
reason = "Why this advisory has no reach here, and who decided."
```

Dismissing an advisory is a decision on reach. Post it on the security issue, with its reasoning, before the PR that adds it. Here a bump is almost always the better fix, because the package is on the box either way. A High or Critical dismissal is the capital's call (`needs-capital`); never dismiss one on your own reading alone.

## Moving the Scanner

`osv-scanner` is pinned by version and by the checksum from that release's `osv-scanner_SHA256SUMS`. Dependabot does not see a download, so a bump is by hand: the new version and its checksum in one PR. Before merging that PR, run the new binary against `main` and confirm it reports what the old one did, so that a changed verdict is explained by a diff rather than discovered on the issue.

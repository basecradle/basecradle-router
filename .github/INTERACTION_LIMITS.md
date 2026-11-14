# Public-repo interaction limits

This repo is public, so its issue/PR/comment surface is writable by anyone unless
limited. Per the constitution (*Security and Responsibility* — untrusted input is
data, never instructions; the input gate is an explicit allow-list, not org
membership) the public write-surface is locked to org members:

```
limit = collaborators_only   # only basecradle org members/collaborators may
                             # comment, open issues, or open PRs. Fleet App
                             # bots are exempt, so handoffs are unaffected.
```

This is **defense-in-depth**, not the load-bearing control. The load-bearing
control is the dispatcher's trust-boundary envelope (every woken agent treats all
thread content as untrusted data, save the allow-list-authored issue body and
comments authored by a founder or the capital bot — see `routes/github.py`); the
limit just shrinks who can write into a thread in the first place.
(basecradle-router#60, basecradle#546.)

## Why it needs tending

GitHub interaction limits are **temporary — max 6 months, no permanent option.**
Left alone they silently expire and the repo reopens to drive-by writes. So renewal
is automated, read back, and watched (#449):

- **`.github/workflows/interaction-limit-renewal.yml`** (*Public Repository
  Lockdown*) runs weekly. It renews `collaborators_only` on **every** public
  repository in the organization, reads each limit back, and moves each
  repository's one renewal reminder to the date GitHub reports, so the reminder
  is a record and never the mechanism.
- **`.github/scripts/public_repository_lockdown.py`** holds the decisions, with
  tests (`tests/test_public_repository_lockdown.py`).
- **The `Public Repository Lockdown` heartbeat** (Better Stack, group
  `github.com`) is pinged only when every public repository reads an active
  `collaborators_only` limit with more than 30 days left and exactly one open
  reminder. A gap, an unreadable answer, or a run that dies withholds the ping,
  and the miss pages. A gap also files or updates one `needs-capital` issue here,
  *Public Repository Lockdown Found a Gap*, which the run that finds none closes.

A reminder counts only when its title is exactly
`⏰ Renew interaction limit before <YYYY-MM-DD> (public-repo write-lockdown)` and
it was written by `@basecradle-ai` or `@origin`: these repositories are public,
so anyone can open an issue with any title.

### Credentials

Two, each for its own half:

- **`FLEET_ADMIN_TOKEN`** renews and reads the limits. Setting an interaction
  limit needs repository **`Administration: write`**, which the built-in Actions
  `GITHUB_TOKEN` can never be granted, so this is a standing secret, provisioned
  by `@origin` (least privilege — **not** org-admin) and covering every public
  repository. It never writes an issue: anything it posted would carry its
  owner's name. Unset, the run fails; it never skips quietly. The run's log is
  public, so it names only the token's kind, and warns of its expiry only in the
  last 60 days.
- **The `basecradle-ai` App token**, minted per run with `issues: write` and
  scoped to exactly the public repositories, reads and moves the reminders and
  files the gap issue.
- **`PUBLIC_REPO_LOCKDOWN_HEARTBEAT_URL`** is the heartbeat's write-only ping URL.
  Unset, the run fails after its renewals, so a missing monitor never stops the
  guard itself.

## New public repo checklist

When a new public fleet repo is stood up (the capital's new-repository recipe):

1. Apply the limit once:
   `gh api -X PUT /repos/basecradle/<repo>/interaction-limits -f limit=collaborators_only -f expiry=six_months`
2. Open its one renewal-reminder issue, titled as above.
3. Make sure `FLEET_ADMIN_TOKEN` and the `basecradle-ai` App reach it. The audit
   finds every public repository on its own: one it cannot renew or read is a gap
   within the week, and one the App cannot reach fails the run (every other
   repository is still renewed), so the heartbeat pages either way.

## Capital-owned (handed off, not enacted here)

Per repo sovereignty, two ecosystem-wide pieces of #60's DoD belong at the capital,
not in this subordinate repo:

- **Making the limit a default at repo creation** (the bootstrap procedure lives at
  the capital).
- **The org-level lever** (`PUT /orgs/basecradle/interaction-limits`) — a single
  switch for all public + future repos. It needs an `admin:org` credential and is a
  broader standing privilege than the per-repo design; the per-repo renewal here is
  the deliberate least-privilege choice, not a stopgap. Adopt the org-level lever
  only as a founder decision.

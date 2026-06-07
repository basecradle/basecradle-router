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
thread content as untrusted data — see `routes/github.py`); the limit just shrinks
who can write into a thread in the first place. (basecradle-router#60.)

## Why it needs tending

GitHub interaction limits are **temporary — max 6 months, no permanent option.**
Left alone they silently expire and the repo reopens to drive-by writes. So renewal
is automated and audited:

- **`.github/scripts/interaction-limit-renewal.sh`** re-applies the limit and runs
  a read-only sweep of every public repo in the org, flagging any that is missing
  its limit *or* its open renewal-reminder issue. A gap exits non-zero.
- **`.github/workflows/interaction-limit-renewal.yml`** runs it monthly (cron), so
  the 6-month clock is reset with months of margin and a gap surfaces as a failed
  run, not a months-later surprise.

### Credential

Setting an interaction limit needs a token with repo **`Administration: write`**.
The built-in Actions `GITHUB_TOKEN` **cannot** do this — `administration` is not a
grantable `GITHUB_TOKEN` permission, so it 403s. The workflow reads
**`FLEET_ADMIN_TOKEN`** from repo secrets: a fleet App installation token or
fine-grained PAT with repo `Administration: write` (least privilege — **not**
org-admin), scoped to the org's public repos. Until that secret is provisioned the
workflow no-ops with a notice.

## New public repo checklist

When a new public fleet repo is stood up, two steps keep it consistent:

1. Apply the limit once:
   `gh api -X PUT /repos/basecradle/<repo>/interaction-limits -f limit=collaborators_only -f expiry=six_months`
2. Open its renewal-reminder issue ("Renew interaction limit before <date>") and
   add `<repo>` to the renewal workflow's coverage (the audit already sweeps all
   public repos, so usually nothing more is needed).

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

---
name: router-deploy-contract
description: The deploy/ contract this repo authors for the NOC's deploy op — file layout, the deploy-router op, the retired deploy.sh, and the merged≠live / smoke-test / drift-alarm mechanics. Use when adding to or maintaining anything under deploy/ (smoke-test.sh, drift-check.sh, the systemd units, wake-runner, README.md), when changing the paths/names the deploy op consumes, or when reasoning about how a merge reaches the live box. The invariant — the router-AI never deploys — lives in CLAUDE.md → Building vs. Deploying; this skill carries the contract mechanics.
---

# Router Deploy Contract

**basecradle-router AI authors the deploy config as code; the NOC runs the deploy.** The invariant lives in CLAUDE.md → "Building vs. Deploying — the router-AI never deploys": this agent never turns the key. This skill is the mechanics of the contract it *does* own.

## The deploy/ contract — authored here, consumed by the NOC

`deploy/` holds the version-controlled deploy config the self-verifying deploy loop consumes:

- [`deploy/smoke-test.sh`](deploy/smoke-test.sh) — asserts the security boundary on the *running* bytes.
- [`deploy/drift-check.sh`](deploy/drift-check.sh) + its hourly timer — makes a never-deployed merge loud in `systemctl --failed`.
- the systemd units and [`deploy/bin/wake-runner`](deploy/bin/wake-runner).

This agent **authors and maintains** these as *code*, because the box's deploy config is version-controlled in the box's own repo (`constitution.md` → New-Server Provisioning). The router owns the **contract** — the stable paths/names/artifacts the deploy op consumes (see [`deploy/README.md`](deploy/README.md) Part 3); the NOC owns the deploy **mechanism**.

## The deploy op — the NOC's, never the router-AI's

The deploy loop is the NOC's structured op **`basecradle-noc deploy-router <sha>`** (basecradle#395 / basecradle-noc#134): the box pulls the merged SHA anonymously from the public repo and runs the on-box DoD loop (*tested → deployed → smoke-tested LIVE → confirmed*) with rollback. The old laptop-side [`deploy/deploy.sh`](deploy/deploy.sh) rsync loop is **retired** (interim emergency fallback only): it refuses to run by default and points at the op, so a reflexive self-deploy cannot happen.

## For the deployer, `merged` ≠ live

A merge to `main` changes nothing on the box until the daemon is reinstalled and restarted (the silent-drift failure of issue #54). The smoke test asserts the security boundary on the *running* bytes; the drift alarm makes a never-deployed merge loud in `systemctl --failed`. **The router-AI's part is keeping that config correct and green — it never runs the deploy.**

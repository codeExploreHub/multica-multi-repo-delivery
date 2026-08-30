---
name: multica-multi-repo-delivery
description: Use when onboarding, validating, reconciling, diagnosing, or upgrading a Multica delivery team for one or more repositories.
---

# Multica Multi-Repo Delivery

## Core principle

Use an exact CLI plan. Complete only its authorized scope; stop at the next authority boundary.

Before any lifecycle command, run `multica-delivery --version`. Version `0.2.0` is required for this Skill. If absent or different, stop and read [troubleshooting](references/troubleshooting.md).

## Route the task

- Onboarding or repository-set change: read [lifecycle](references/lifecycle.md); for confirmation or manifests, also read [manifest schema](references/manifest-schema.md).
- Any `plan`, `apply`, upgrade, or adjacent mutation: read [lifecycle](references/lifecycle.md) and [safety boundaries](references/safety-boundaries.md).
- Failed, drifted, stale, ambiguous, or partial work: read [troubleshooting](references/troubleshooting.md).

## Required lifecycle

1. Start with read-only `discover`. Present every `inferred` and `unknown` value; require confirmation YAML tied to the complete discovery digest. Never promote or invent values.
2. Run `init` only after confirmation. Its local scaffold write grants no external mutation authority.
3. Run `validate`, then read-only `plan`. Show the ordered action summary, expiry, and complete 64-character hash.
4. Immediately before `apply`, request that exact hash. Existing explicit authority applies only to the same fresh plan. Run `multica-delivery apply --plan <control>/plan.json --confirm <complete-plan-hash>` without discarding or widening it.
5. On expiry, drift, ambiguity, partial convergence, external failure, or human-block, stop before retry; a new plan needs new authorization.
6. Use read-only `doctor` for diagnosis. `upgrade` only creates a migration plan; applying it uses the same full-hash boundary.

## Workflow version 2

For mutable delivery, wait for every current Gate Stage child to become terminal. Core/plan-parent is the sole fan-in, canonical FailureBundle producer, and decision authority. Delivery Lead validates its canonical result, uses the exact returned bundle and digest without reconstruction, and alone executes Stages and children. Reviewer and QA return verdict evidence only. Engineers require a current active child and every assigned bundle partition; the Watcher cannot create bundles or dispatch repair.

Exactly two repair rounds are automatic. Round 3 requires a member-authored authorization bound to the exact current FailureBundle and digest. It authorizes exactly the next round, is consumed once, and cannot authorize a different bundle or later round.

Completed version-1 parents are readable history. An active version-1 parent requires explicit migration before any new Stage, repair, merge, or completion. Read [lifecycle](references/lifecycle.md) and [safety boundaries](references/safety-boundaries.md) before planning that migration.

## Authority contract

`apply` may reconcile only its listed Multica actions. GitHub repository creation, code changes, push, merge, rollback, and every deployment are separate operations requiring separate explicit authority. Finish an authorized Multica apply, then stop before an unapproved adjacent action.

| Signal | Decision |
|---|---|
| Inferred/unknown discovery | Stop for confirmation |
| Fresh plan, no exact approval | Show complete 64-character hash and request approval |
| Fresh plan, exact approval | Apply only listed Multica actions |
| Expiry/drift/human-block | Stop; do not bypass or retry mutation |

## Red flags

- “The inferred value is obvious.”
- “A partial hash is enough.”
- “Onboarding implies GitHub or deployment permission.”
- “Safer means ignoring an action already authorized exactly.”

These indicate an authority error: return to the observable lifecycle state and its exact permitted next action.

---
name: multica-multi-repo-delivery
description: Use when onboarding, validating, reconciling, diagnosing, or upgrading a Multica delivery team for one or more repositories.
---

# Multica Multi-Repo Delivery

## Core principle

Drive the installed CLI through a fresh, exact plan. Complete only the explicitly authorized scope, then stop at the next authority boundary.

Before any lifecycle command, run `multica-delivery --version`. Version `0.1.0` is required for this Skill. If absent or different, stop and read [troubleshooting](references/troubleshooting.md).

## Route the task

- First onboarding or changed repository set: read [lifecycle](references/lifecycle.md).
- Confirmation or manifest work: also read [manifest schema](references/manifest-schema.md).
- Any `plan`, `apply`, or upgrade: read both [lifecycle](references/lifecycle.md) and [safety boundaries](references/safety-boundaries.md).
- Any requested adjacent mutation: read [safety boundaries](references/safety-boundaries.md).
- Failed, drifted, stale, ambiguous, or partial work: read [troubleshooting](references/troubleshooting.md).

## Required lifecycle

1. Start onboarding with read-only `discover`. Present every `inferred` and `unknown` value. Require a confirmation YAML tied to the complete discovery digest; never promote or invent a value.
2. Run `init` only after confirmation. State that scaffold creation is a local write and grants no external mutation authority.
3. Run `validate`, then read-only `plan`. Show the ordered action summary, expiry, and complete 64-character hash.
4. Immediately before `apply`, request authorization for that exact hash. If the operator already explicitly authorized the current fresh full-hash plan, perform only `multica-delivery apply --plan <control>/plan.json --confirm <complete-plan-hash>`; do not discard valid authority or widen it.
5. Stop before retrying when output reports expiry, drift, ambiguity, partial convergence, external failure, or human-block. A new plan requires new authorization.
6. Use read-only `doctor` for diagnosis. `upgrade` only creates a migration plan; applying it uses the same full-hash boundary.

## Authority contract

`apply` may reconcile only its listed Multica actions. GitHub repository creation, code changes, push, merge, rollback, and every deployment are separate operations requiring separate explicit authority. Finish an authorized Multica apply, then stop before an unapproved adjacent action.

| Signal | Decision |
|---|---|
| Inferred/unknown discovery | Stop for confirmation |
| Fresh plan, no exact approval | Show full hash and request approval |
| Fresh plan, exact approval | Apply only listed Multica actions |
| Expiry/drift/human-block | Stop; do not bypass or retry mutation |

## Red flags

- “The inferred value is obvious.”
- “A partial hash is enough.”
- “Onboarding implies GitHub or deployment permission.”
- “Safer means ignoring an action already authorized exactly.”

These indicate an authority error: return to the observable lifecycle state and its exact permitted next action.

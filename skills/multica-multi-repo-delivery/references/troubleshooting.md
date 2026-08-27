# Troubleshooting and stopping rules

## Missing or mismatched CLI

Run `multica-delivery --version`; this Skill requires `0.1.0`. If unavailable or different, stop before lifecycle work. Install from an operator-approved local checkout with `pipx install .`, or from the exact approved immutable tag. Do not guess a repository owner or unpinned URL.

## Safe responses

| Condition | Response |
|---|---|
| Discovery has inferred/unknown fields | Collect explicit confirmation; rerun `init` only with the matching digest |
| Validation fails | Correct local manifest inputs; do not plan/apply |
| Plan expired or is from the future | Create a fresh read-only plan and request new full-hash approval |
| Manifest, lock, actions, or external fingerprint drifted | Stop; inspect changes and create a new plan |
| Partial hash or confirmation mismatch | Stop; never expand or autocomplete approval |
| External/contract failure | Diagnose credentials/runtime/daemon/repository contracts; do not mutate blindly |
| Human-block, ambiguity, foreign identity, duplicate, or partial convergence | Preserve state and escalate to the operator; no retry, delete, or rollback |

Use `multica-delivery doctor /absolute/delivery-control` for read-only evidence. Expected CLI failures are sanitized; do not expose raw subprocess output or secrets while investigating.

For supported schema/version changes, run `upgrade`, review its plan, and use the same exact apply authorization. Unknown or skipped migration paths remain human-blocked.

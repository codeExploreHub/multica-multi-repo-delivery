# Lifecycle command contract

All paths below are explicit operator-selected paths. Put global `--output json` before the subcommand.

## First onboarding

```bash
multica-delivery --output json discover /absolute/repo-a /absolute/repo-b
multica-delivery init --discovery /absolute/discovery.json --confirmation /absolute/confirmation.yaml --target /absolute/delivery-control
multica-delivery validate /absolute/delivery-control
multica-delivery --output json plan /absolute/delivery-control
```

`discover`, `validate`, and `plan` are externally read-only. `init` creates a new local scaffold without overwriting an existing target. Save the discovery JSON exactly; do not reconstruct its digest.

Report the plan's ordered actions, reasons, creation/expiry time, and complete hash. Ask for authorization immediately before:

```bash
multica-delivery apply --plan /absolute/delivery-control/plan.json --confirm <64-lowercase-hex>
```

Omit `--confirm` only for an interactive operator who will enter the complete prompted hash. There is no short-hash or `--yes` path. Secrets come from declared environment variables by default; use `--secret-source prompt` only when the operator chooses no-echo prompting.

## Operations

```bash
multica-delivery doctor /absolute/delivery-control
multica-delivery --output json upgrade /absolute/delivery-control
```

`doctor` does not repair. `upgrade` writes a migration plan; inspect and authorize its full hash, then use the normal `apply` command. A repeated newly planned apply should converge with `mutation_count: 0`.

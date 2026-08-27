# Operator guide

## Install and verify

Use a local checkout for `0.1.0` development:

```bash
pipx install .
multica-delivery --version
```

For editable work:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

The Skill is the directory `skills/multica-multi-repo-delivery`. Copy that directory to the Skill location configured by the target agent runtime. It calls the installed CLI; it contains no second provisioning implementation.

## First onboarding

Discovery only reads the explicitly supplied repositories. Save its JSON result unchanged as `/absolute/discovery.json` using your orchestration environment's atomic file facility.

```bash
multica-delivery --output json discover /absolute/repo-a /absolute/repo-b
```

Review every `inferred` and `unknown` field. Create secret-free confirmation YAML using the complete discovery digest, then initialize a previously unused absolute target:

```bash
multica-delivery init --discovery /absolute/discovery.json --confirmation /absolute/confirmation.yaml --target /absolute/delivery-control
multica-delivery validate /absolute/delivery-control
multica-delivery --output json plan /absolute/delivery-control
```

`init` writes only the new local scaffold. `plan` reads Multica and GitHub but performs no external mutation. It expires exactly ten minutes after creation. Review the ordered actions and complete 64-character hash, then request operator authorization immediately before apply:

```bash
multica-delivery apply --plan /absolute/delivery-control/plan.json --confirm 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
```

Any expiry, digest/fingerprint drift, partial hash, ambiguous acknowledgement, partial convergence, external failure, or human-block stops mutation. Create a new plan and obtain new authorization; never call raw Multica commands to bypass the result.

## Secrets and convergence

Manifest `secret_env` entries declare names and recipients, never values. The authorized apply reads those names from its environment. Choose no-echo input only when required:

```bash
multica-delivery apply --plan /absolute/delivery-control/plan.json --confirm 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef --secret-source prompt
```

Secrets never belong in confirmation YAML, manifest values, plan, lock, argv, logs, or reports. After a successful apply, generate and authorize a new plan; a converged second apply reports `mutation_count: 0`.

## Diagnose and upgrade

Doctor is read-only and never repairs:

```bash
multica-delivery doctor /absolute/delivery-control
```

Upgrade writes a migration plan. Review and apply it through the same full-hash boundary:

```bash
multica-delivery --output json upgrade /absolute/delivery-control
```

Production manifests prohibit automatic merge. Deployment is always a separate, manually triggered external action. This package does not deploy in development either.

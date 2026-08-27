# Multica Multi-Repository Delivery CLI and Skill Design

**Status:** Approved

**Date:** 2026-08-27

**Initial version:** `0.1.0`

## Context

The existing `tools.multica_delivery` package is a reviewed, product-neutral
Python Core inside the Eventra control repository. It provides strict manifest,
topology, decision, provisioning, GitHub, Multica, process-ownership,
exact-SHA smoke, workflow, and recovery boundaries. It is not independently
installable and intentionally has no generic user-facing CLI.

The next deliverable turns that Core into a separately versioned public package
and adds a reusable Agent Skill. A new project must be able to discover and
confirm one or more repositories, create a local delivery-control scaffold,
validate it, produce an externally read-only plan, explicitly approve that plan,
reconcile Multica resources, diagnose the resulting instance, and plan framework
upgrades. Users must not need to clone or understand Eventra.

## Goals

- Create a standalone public GitHub repository named
  `multica-multi-repo-delivery`.
- Ship one Python distribution containing the Core and a `multica-delivery`
  console command.
- Ship one `multica-multi-repo-delivery` Skill from the same tagged repository.
- Give Core, CLI, Skill, templates, schemas, and lock compatibility one shared
  semantic version.
- Support macOS and Linux with Python 3.11, 3.12, and 3.13.
- Support products containing one or more repositories without product-specific
  production branches.
- Make external mutation possible only through a fresh, exact, explicitly
  confirmed plan.
- Preserve all existing Core authority, secret-redaction, exact-SHA, no-rollback,
  and no-deployment guarantees.

## Non-goals

- Creating GitHub repositories, forks, branches, commits, pushes, or pull
  requests.
- Merging business pull requests as part of onboarding.
- Deploying any environment or implementing rollback.
- Querying or importing Skills from the company-internal SkillsHub.
- Replacing project-specific build systems or installing business dependencies.
- Supporting Windows in version `0.1.0`.
- Mutating the existing Eventra Multica instance during package development.
- Removing Eventra's in-repository Core before a public package tag exists and
  compatibility has passed against that immutable tag.

## Repository and Distribution Boundary

Development creates a sibling repository at:

```text
/Users/didi/Eventra-workspace/multica-multi-repo-delivery
```

The target repository layout is:

```text
multica-multi-repo-delivery/
├── pyproject.toml
├── LICENSE
├── src/multica_delivery/
│   ├── __init__.py
│   ├── core/
│   ├── cli/
│   ├── adapters/
│   └── templates/
├── skills/multica-multi-repo-delivery/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   └── references/
├── tests/
├── docs/
└── README.md
```

`src/multica_delivery/core` owns immutable models and pure or Core-level
coordination. `adapters` owns subprocess, filesystem, GitHub, Multica, and local
process boundaries. `cli` owns command parsing, presentation, plan files,
confirmation, and exit codes. Templates have one implementation-owned copy
inside the Python distribution; the Skill invokes the CLI rather than copying
template or reconciliation logic.

The distribution name and console command are both versioned by
`multica_delivery.__version__`. Installation is designed for:

```bash
pipx install \
  git+https://github.com/<owner>/multica-multi-repo-delivery.git@v0.1.0
```

The GitHub owner is a release-time input and is not guessed or published during
local development. Actual repository creation, push, and tag publication require
separate user authorization.

The standalone repository is licensed under Apache License 2.0. The repository
contains the unmodified Apache-2.0 license text in `LICENSE`, declares the same
license through SPDX package metadata, and keeps third-party notices when a
dependency or imported asset requires them. Publishing the source does not grant
permission to expose private repositories, credentials, internal Skill sources,
or company-confidential configuration.

## Public CLI

The installed executable is `multica-delivery`. Every command supports human
output by default and a stable `--output json` mode.

### `discover`

`discover` performs a read-only scan of explicitly selected local repository
roots. It may inspect tracked files, repository metadata, AGENTS instructions,
package/build manifests, declared scripts, and port-bearing configuration. It
does not run business commands, install dependencies, contact live services, or
write the repositories.

Every finding is classified as:

- `confirmed`: proven by an authoritative local source;
- `inferred`: a plausible recommendation that requires confirmation; or
- `unknown`: required information that discovery could not establish.

Discovery produces a deterministic JSON document when requested. It never
silently upgrades `inferred` or `unknown` to confirmed input.

### `init`

`init` consumes reviewed discovery data and creates only an approved local
scaffold:

```text
delivery-control/
├── delivery.yaml
├── framework.lock
└── env.example
```

It may also copy an AGENTS template and a `.gitignore` fragment when the user
names explicit unused target paths. It never overwrites `delivery.yaml`,
`framework.lock`, `AGENTS.md`, `.env`, `.env.*`, or another existing target.
Remaining `inferred` or `unknown` required fields stop initialization.

`framework.lock` begins as a schema-valid uninitialized lock. Secret values are
never read or written by `init`.

### `validate`

`validate` is local and read-only. It verifies:

- manifest and lock schemas and their version compatibility;
- absolute, normalized, non-aliased local paths;
- repository identity, command arrays, service ports, dependency topology,
  integration suites, and merge order;
- public GitHub Skill origins and exact role bindings;
- environment and secret-name declarations without reading secret values;
- local Python, Multica CLI, GitHub CLI, and platform compatibility;
- policy invariants, including `deployment: forbidden` and production automatic
  merge prohibition.

Validation rejects unconfirmed discovery values rather than guessing.

### `plan`

`plan` performs local validation and authoritative, read-only Multica and GitHub
inspection. It does not call any external mutation method. It writes an atomic,
secret-free `delivery-control/plan.json` containing:

- plan schema and CLI version;
- instance identity;
- manifest digest and lock digest;
- external-state fingerprint derived from stable reads;
- deterministic ordered actions and human-readable reasons;
- creation and expiry timestamps;
- a SHA-256 plan hash over the canonical plan body.

The plan lifetime is exactly ten minutes. A second plan with the same inputs and
observed state has the same ordered action body; timestamps and the resulting
plan hash intentionally identify a distinct approval artifact.

### `apply`

`apply` is the only onboarding command allowed to mutate Multica. Before any
secret lookup or external mutation it requires:

- a schema-valid plan from the same CLI compatibility line;
- a plan age of at most ten minutes;
- exact manifest and lock digest equality;
- a full SHA-256 confirmation matching the plan;
- repeated authoritative reads proving the external-state fingerprint and
  planned preconditions have not drifted.

Interactive mode asks the operator to enter the complete plan hash.
Non-interactive mode requires:

```bash
multica-delivery apply \
  --plan delivery-control/plan.json \
  --confirm <complete-plan-hash>
```

There is no `--yes` alias. A mismatch, stale plan, or changed external state
fails before mutation and requires a new `plan`.

Apply uses the existing convergent `Provisioner` boundary. Ambiguous write
acknowledgements are resolved only through stable authoritative rereads. It does
not compensate, delete, or roll back partially reconciled resources. A
converged repeat apply reports `mutation_count == 0`.

Secrets are resolved only after plan authorization, only for manifest-declared
recipients, from declared environment variables or no-echo interactive input.
They do not enter argv, plan, lock, output, reports, logs, or exception graphs.

### `doctor`

`doctor` is read-only. It diagnoses CLI and schema compatibility, authentication,
local paths, GitHub repository contracts, Multica runtime/daemon identity,
Project/Agent/Squad/Watcher state, public Skill origins, restricted recipient
coverage, lock convergence, stale plans, and bounded recovery health. Findings
are pass, warn, fail, or human-block; `doctor` never repairs them.

### `upgrade`

`upgrade` reads the current manifest, lock, installed CLI version, and supported
schema migrations. It writes a migration plan in the same authenticated plan
format. It never applies the migration itself. The normal `apply` confirmation
boundary performs an approved compatible migration. Unknown or skipped migration
paths human-block.

## Plan File and Canonical Hash

Plan JSON uses UTF-8, sorted mapping keys, compact separators, LF line endings,
and no floating-point values. The outer file contains:

```json
{
  "schema_version": 1,
  "command": "plan",
  "status": "ok",
  "result": {
    "body": {},
    "plan_hash": "<64 lowercase hex characters>"
  },
  "warnings": [],
  "errors": []
}
```

The hash input is the canonical `result.body`, which includes creation and
expiry timestamps. The hash field is excluded from its own input. Plan and lock
writes use a same-directory temporary file, flush and fsync, permission `0600`,
and atomic replacement. Existing files are not partially truncated on failure.

## Output and Exit Contract

Human output is concise and never parsed by other components. JSON output uses
the stable envelope above for every command.

Exit codes are:

- `0`: success;
- `2`: local input, discovery confirmation, manifest, or schema validation
  failure;
- `3`: missing confirmation, expired plan, or plan-hash mismatch;
- `4`: authoritative external state drift; a new plan is required;
- `5`: Multica, GitHub, subprocess, or external contract failure; and
- `6`: a safety condition requires human action.

Expected failures produce one sanitized error entry and no traceback by default.
An explicit developer diagnostic mode may report safe structural context but
never raw command output or secrets.

## Skill Package

The Skill name is `multica-multi-repo-delivery`. Its description triggers when
an operator wants to onboard, validate, reconcile, diagnose, or upgrade a
Multica delivery team for one or more repositories. It does not trigger for
ordinary feature development inside an already onboarded product.

`SKILL.md` remains a concise routing and authority guide. It instructs an Agent
to:

- verify the installed `multica-delivery` version before use;
- begin first-time onboarding with `discover`;
- surface every inferred or unknown value for confirmation;
- distinguish local scaffold creation from external mutation authority;
- use `validate` and `plan` before proposing apply;
- show the operator the complete planned action summary and plan hash;
- request explicit authorization immediately before `apply`;
- pass only the approved complete hash;
- stop on expiry, drift, ambiguity, partial convergence, or human-block output;
- keep GitHub creation/push/merge and every deployment outside the apply scope.

Conditional details live in:

```text
references/
├── lifecycle.md
├── manifest-schema.md
├── safety-boundaries.md
└── troubleshooting.md
```

The Skill contains `agents/openai.yaml` with discoverable UI metadata and normal
implicit invocation. Safety is enforced at the mutation boundary, not by making
the Skill explicit-only.

## Skill Test-Driven Development

The Skill is tested as behavior, not by heading or phrase matching.

Before writing the Skill, isolated evaluator scenarios run without it. The
baseline must demonstrate relevant failures, such as accepting inferred fields,
treating `init` as apply permission, skipping the fresh plan, using a partial
hash, or widening apply to GitHub/deployment actions. The exact failure and
rationale are recorded.

The same scenarios then run with the Skill available and a fake CLI. Passing
behavior requires the evaluator to select the correct command, stop at user
confirmation, refuse expired/drifted plans, and preserve mutation scope. A
variation scenario covers one, two, and three repositories. A missing-CLI or
version-mismatch scenario must stop with the pinned installation instruction.

Skill validation also runs the standard frontmatter and scaffold validator.
Core/CLI tests remain the mechanical authority; the Skill does not duplicate
regex, plan hashing, or reconciliation code.

## Testing Strategy

### Core migration

The existing generic Core and its tests move into the standalone package with
import-only changes first. The migrated suite must pass before CLI behavior is
added. Eventra compatibility remains in the Eventra repository during this
phase.

### CLI contract tests

Each command has tests for closed argv, human and JSON output, exact exit codes,
schema rejection, no-overwrite behavior, deterministic canonicalization,
atomic writes, confirmation, expiry, drift, and secret redaction.

### End-to-end tests

Temporary Git repositories and fake `multica` and `gh` executables exercise:

- one repository;
- a frontend/backend pair; and
- a three-repository dependency graph.

The tests run `discover → init → validate → plan → apply → doctor`, verify the
second apply is a zero-mutation convergence, and exercise an upgrade plan. They
never contact live services or start product processes.

### Package and platform tests

CI builds wheel and sdist, installs the wheel into a clean environment, checks
the console entry point and packaged templates/Skill resources, and runs on
macOS and Linux for Python 3.11, 3.12, and 3.13. The only initial runtime Python
dependency is `PyYAML==6.0.2`; build tooling is development-only.

## Security and Authority Invariants

- Read-only commands never call mutation adapters.
- `apply` requires an exact fresh plan and complete-hash authorization.
- A plan is not permission for GitHub creation, code changes, merge, rollback,
  or deployment.
- Public IDs use the existing closed identifier grammar.
- Commands remain argv arrays with `shell=False` and no secret argv.
- Runtime/daemon/repository allowlists are exact.
- Raw subprocess output and hostile exception graphs are reduced at adapter
  boundaries.
- Existing resources are reconciled by locked identity and stable reads; a
  same-name foreign origin human-blocks.
- No command deletes or automatically rolls back external resources.
- Production requires manual merge and external human-triggered deployment;
  package apply never deploys.
- The Skill cannot grant authority that the CLI does not independently verify.

## Eventra Migration

Migration is deliberately two-stage.

### Stage 1: standalone `0.1.0`

The standalone repository receives the Core, CLI, Skill, templates, docs, and
tests. Eventra keeps its reviewed in-repository Core and operational adapter.
Parity tests compare the standalone manifest/metadata/decision contracts with
the Eventra compatibility fixture. No Eventra live resource changes occur.

### Stage 2: immutable-tag adoption

After the user separately authorizes public repository creation, push, and a
`v0.1.0` tag, Eventra adds an exact VCS dependency on that tag. Its compatibility
and legacy suites run against the installed distribution. Only after those
tests pass may a separate Eventra migration commit remove the duplicated Core.
That migration does not apply or alter the live Eventra team.

## Acceptance Criteria

- The independent repository builds reproducible wheel and sdist artifacts.
- `pipx` installation exposes all seven commands and their help.
- Read-only lifecycle commands produce zero external mutations in tests.
- `apply` rejects absent, partial, mismatched, expired, or drifted plans before
  secret lookup or mutation.
- A confirmed fake apply converges, and its second run has zero mutations.
- Temporary one-, two-, and three-repository lifecycle tests pass.
- JSON envelopes, exit codes, plan hashes, atomic files, and redaction are
  stable and tested.
- The Skill has a recorded failing baseline and passing forward tests.
- Package installation includes templates, references, Skill metadata, and
  version-consistent resources.
- Python 3.11–3.13 tests pass on macOS and Linux.
- Existing Eventra tests remain green and no live Eventra state is changed.
- No public repository, push, tag, package publication, real apply, GitHub
  mutation, merge, deployment, or rollback occurs without later explicit
  authorization.

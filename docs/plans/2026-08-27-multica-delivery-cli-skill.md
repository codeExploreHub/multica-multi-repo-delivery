# Multica Multi-Repository Delivery CLI and Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone Apache-2.0 Python distribution with the `multica-delivery` CLI, a tested `multica-multi-repo-delivery` Skill, packaged templates, and safe lifecycle commands for onboarding one-to-N-repository Multica delivery teams.

**Architecture:** Extract the reviewed generic Core from Eventra into a sibling Git repository, preserving immutable authority boundaries while separating pure Core code from external adapters. Add a thin argparse CLI whose read-only commands produce typed envelopes and whose only external mutation path consumes an exact fresh plan hash. Package the Skill beside the CLI; the Skill routes operator intent while the CLI enforces all mechanical safety constraints.

**Tech Stack:** Python 3.11–3.13, standard-library `argparse`/`dataclasses`/`unittest`, setuptools build backend, PyYAML 6.0.2, GitHub CLI, Multica CLI, Agent Skills format.

**Spec:** `docs/superpowers/specs/2026-08-27-multica-delivery-cli-skill-design.md`

## Global Constraints

- The target repository is `/Users/didi/Eventra-workspace/multica-multi-repo-delivery` and has no remote until separately authorized.
- The initial version is exactly `0.1.0`, the distribution name is `multica-multi-repo-delivery`, and the console command is `multica-delivery`.
- License is Apache-2.0; Python support is macOS/Linux on 3.11, 3.12, and 3.13.
- Runtime dependency is exactly `PyYAML==6.0.2`; business dependencies are never installed.
- `discover`, `validate`, `plan`, `doctor`, and `upgrade` perform no external mutation. `init` writes only new local scaffold files.
- `apply` is the only external Multica mutation path and requires a plan no older than ten minutes plus the complete SHA-256 plan hash.
- There is no `--yes`; no command creates GitHub repositories, pushes, merges, deploys, rolls back, or queries internal SkillsHub.
- Secrets never enter argv, plan, lock, output, reports, logs, or exception graphs.
- Existing Eventra source and live resources remain unchanged until a separately authorized immutable public tag exists.
- All production files are edited with `apply_patch`; bulk source/test copying may use a mechanical copy followed by reviewed import rewrites.

---

### Task 1: Bootstrap the standalone repository and installable package

**Files:**
- Create repository: `/Users/didi/Eventra-workspace/multica-multi-repo-delivery`
- Create: `pyproject.toml`
- Create: `LICENSE`
- Create: `.gitignore`
- Create: `src/multica_delivery/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/test_package.py`
- Create: `README.md`
- Copy: `docs/specs/2026-08-27-multica-delivery-cli-skill-design.md`
- Copy: `docs/plans/2026-08-27-multica-delivery-cli-skill.md`

**Interfaces:**
- Produces: installable `multica_delivery` package with `__version__ == "0.1.0"`.
- Produces: a local Git repository whose `main` bootstrap commit is followed by branch `feature/v0.1.0` for Tasks 2–12.

- [ ] **Step 1: Create the failing package test**

```python
import importlib.metadata
import unittest

import multica_delivery


class PackageTests(unittest.TestCase):
    def test_distribution_and_module_share_version(self):
        self.assertEqual(multica_delivery.__version__, "0.1.0")
        self.assertEqual(
            importlib.metadata.version("multica-multi-repo-delivery"),
            "0.1.0",
        )
```

- [ ] **Step 2: Verify RED in a clean virtual environment**

Run:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -B -m unittest tests.test_package -v
```

Expected: installation fails because `pyproject.toml` and the package do not exist.

- [ ] **Step 3: Create exact package metadata**

Create `pyproject.toml` with:

```toml
[build-system]
requires = ["setuptools>=75", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "multica-multi-repo-delivery"
dynamic = ["version"]
description = "Manifest-driven Multica delivery teams for one or more repositories"
readme = "README.md"
requires-python = ">=3.11"
license = { file = "LICENSE" }
dependencies = ["PyYAML==6.0.2"]
classifiers = [
  "License :: OSI Approved :: Apache Software License",
  "Programming Language :: Python :: 3.11",
  "Programming Language :: Python :: 3.12",
  "Programming Language :: Python :: 3.13",
]

[project.optional-dependencies]
dev = ["build>=1.2,<2"]

[tool.setuptools]
package-dir = {"" = "src"}

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.dynamic]
version = { attr = "multica_delivery.__version__" }
```

Create `src/multica_delivery/__init__.py`:

```python
"""Public package identity for Multica multi-repository delivery."""

__version__ = "0.1.0"
```

Create `.gitignore` before initializing Git:

```gitignore
.venv/
__pycache__/
*.py[cod]
*.egg-info/
build/
dist/
.coverage
```

Use the unmodified Apache License 2.0 text in `LICENSE`. Keep README installation examples local (`pipx install .` and `pip install -e .`) because no remote is authorized.

- [ ] **Step 4: Initialize Git and copy the approved design artifacts**

Run from the target directory:

```bash
git init -b main
git add .
git commit -m "chore: bootstrap multica delivery package"
git switch -c feature/v0.1.0
```

Copy the approved Spec and this plan byte-for-byte into `docs/specs/` and `docs/plans/` before the bootstrap commit.

- [ ] **Step 5: Verify GREEN and build metadata**

Run:

```bash
.venv/bin/python -m pip install -e .
.venv/bin/python -B -m unittest tests.test_package -v
.venv/bin/python -c 'import multica_delivery; assert multica_delivery.__version__ == "0.1.0"'
git diff --check
```

Expected: one test passes and the feature branch is clean.

### Task 2: Extract the reviewed Core and adapter boundaries

**Files:**
- Create: `src/multica_delivery/core/{model,manifest,topology,metadata,decisions,contract_audit,provision,workflow}.py`
- Create: `src/multica_delivery/adapters/{redaction,multica_client,github_client,processes,exact_sha}.py`
- Create: `src/multica_delivery/core/__init__.py`
- Create: `src/multica_delivery/adapters/__init__.py`
- Create: `tests/core/` from Eventra `tools/multica_delivery/tests/`
- Create: `tests/fixtures/`

**Interfaces:**
- Consumes: Eventra commit `f62310731394ec27034645787d87749b1eb95d38` as the exact extraction source.
- Produces: the same public Core behavior under `multica_delivery.core` and strict effect adapters under `multica_delivery.adapters`.
- Produces: `ReconcileResult`, `Provisioner`, `GenericWorkflow`, `load_manifest`, `load_lock`, and `audit_contracts` re-exported from `multica_delivery.core`.

- [ ] **Step 1: Copy tests first and rewrite only import roots**

Mechanically copy the generic test suite and fixtures. Replace imports as follows:

```text
tools.multica_delivery.model            → multica_delivery.core.model
tools.multica_delivery.manifest         → multica_delivery.core.manifest
tools.multica_delivery.topology         → multica_delivery.core.topology
tools.multica_delivery.metadata         → multica_delivery.core.metadata
tools.multica_delivery.decisions        → multica_delivery.core.decisions
tools.multica_delivery.contract_audit   → multica_delivery.core.contract_audit
tools.multica_delivery.provision        → multica_delivery.core.provision
tools.multica_delivery.workflow         → multica_delivery.core.workflow
tools.multica_delivery.redaction        → multica_delivery.adapters.redaction
tools.multica_delivery.multica_client   → multica_delivery.adapters.multica_client
tools.multica_delivery.github_client    → multica_delivery.adapters.github_client
tools.multica_delivery.processes        → multica_delivery.adapters.processes
tools.multica_delivery.exact_sha        → multica_delivery.adapters.exact_sha
```

Do not change assertions while establishing RED.

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -B -m unittest discover -s tests/core -p 'test_*.py' -q
```

Expected: imports fail because the extracted modules do not exist.

- [ ] **Step 3: Copy production modules and make package-relative imports exact**

Move immutable/pure modules to `core` and effect boundaries to `adapters` using the mapping above. Core modules import adapters only through the concrete boundary needed by `Provisioner` or `GenericWorkflow`; adapters may import immutable types from Core but never import CLI modules.

Create `src/multica_delivery/core/__init__.py` with explicit imports rather than `*`:

```python
from .contract_audit import audit_contracts
from .manifest import load_lock, load_manifest, load_manifest_text, manifest_digest
from .provision import Provisioner, ReconcileAction, ReconcileResult
from .workflow import GenericWorkflow

__all__ = [
    "GenericWorkflow",
    "Provisioner",
    "ReconcileAction",
    "ReconcileResult",
    "audit_contracts",
    "load_lock",
    "load_manifest",
    "load_manifest_text",
    "manifest_digest",
]
```

- [ ] **Step 4: Prove extraction parity**

Run:

```bash
.venv/bin/python -B -m unittest discover -s tests/core -p 'test_*.py' -q
.venv/bin/python -B -m compileall -q src/multica_delivery
git diff --check
```

Expected: every copied generic test passes with no live call.

- [ ] **Step 5: Commit**

```bash
git add src/multica_delivery tests/core tests/fixtures
git commit -m "refactor: extract generic multica delivery core"
```

### Task 3: Add CLI envelopes, errors, clocks, and atomic artifacts

**Files:**
- Create: `src/multica_delivery/cli/errors.py`
- Create: `src/multica_delivery/cli/output.py`
- Create: `src/multica_delivery/cli/clock.py`
- Create: `src/multica_delivery/cli/files.py`
- Create: `tests/cli/test_foundation.py`

**Interfaces:**
- Produces: `ExitCode`, `CliError`, `Envelope`, `Clock`, `SystemClock`, `atomic_write_new`, and `atomic_replace_private`.
- `CliError` carries only a stable code, safe message, and `ExitCode`; raw exceptions never enter output.

- [ ] **Step 1: Write failing foundation tests**

Test exact exit values, canonical JSON output, human output not being JSON, deterministic warning/error ordering, injected time, no-overwrite creation, `0600` replacement, fsync-before-replace, and original-file preservation when replacement fails.

Use this public enum in the tests:

```python
class ExitCode(IntEnum):
    OK = 0
    VALIDATION = 2
    CONFIRMATION = 3
    DRIFT = 4
    EXTERNAL = 5
    HUMAN_BLOCK = 6
```

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -B -m unittest tests.cli.test_foundation -v
```

Expected: imports fail.

- [ ] **Step 3: Implement minimal closed primitives**

`Envelope.to_json()` must use:

```python
json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
```

`atomic_write_new()` preflights nonexistence and uses `os.open` with `O_CREAT | O_EXCL`. `atomic_replace_private()` creates a temporary file in the destination directory, applies mode `0o600`, flushes, calls `os.fsync`, then uses `os.replace`. Both accept bytes already serialized by the caller.

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/python -B -m unittest tests.cli.test_foundation -v
git add src/multica_delivery/cli tests/cli/test_foundation.py
git commit -m "feat: add closed CLI foundation"
```

### Task 4: Implement read-only repository discovery

**Files:**
- Create: `src/multica_delivery/cli/discovery.py`
- Create: `src/multica_delivery/cli/commands/discover.py`
- Create: `tests/cli/test_discovery.py`
- Create: `tests/fixtures/discovery/`

**Interfaces:**
- Produces: `Classification`, `Finding`, `RepositoryDiscovery`, `DiscoveryDocument`, and `discover_repositories(paths, reader)`.
- Produces command function `run_discover(args, services) -> Envelope`.
- `RepositoryReader` exposes closed read methods only; it has no write or business-command execution method.

- [ ] **Step 1: Write failing one-, two-, and three-repository discovery tests**

Use temporary repositories containing representative `package.json`, `pom.xml`, wrapper scripts, AGENTS files, and Git metadata. Assert:

```python
self.assertEqual(document.schema_version, 1)
self.assertEqual(document.repositories[0].root.classification, Classification.CONFIRMED)
self.assertEqual(document.repositories[0].commands["test"].classification, Classification.INFERRED)
self.assertEqual(document.repositories[0].project.classification, Classification.UNKNOWN)
self.assertEqual(reader.write_calls, [])
self.assertEqual(reader.command_calls, [])
```

Also reject duplicate/aliased roots, nonabsolute paths, nested duplicate repositories, unsupported object types, and changing reads.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -B -m unittest tests.cli.test_discovery -v
```

- [ ] **Step 3: Implement deterministic discovery**

Confirmed values require an authoritative source and store that source path. Script names, ports, dependency edges, and proposed commands discovered heuristically remain inferred. Runtime ID, daemon ID, Project names, role Skills, and unresolved commands remain unknown unless an explicit input file proves them.

The JSON discovery document includes a canonical `discovery_digest` over every finding except presentation ordering. The command writes only stdout; users may redirect JSON to a file.

- [ ] **Step 4: Verify no effects and commit**

```bash
.venv/bin/python -B -m unittest tests.cli.test_discovery -v
git add src/multica_delivery/cli/commands/discover.py src/multica_delivery/cli/discovery.py tests
git commit -m "feat: discover multi repository delivery inputs"
```

### Task 5: Implement confirmation-driven initialization and validation

**Files:**
- Create: `src/multica_delivery/cli/confirmation.py`
- Create: `src/multica_delivery/cli/templates.py`
- Create: `src/multica_delivery/cli/commands/init.py`
- Create: `src/multica_delivery/cli/commands/validate.py`
- Create: `src/multica_delivery/templates/{delivery.yaml,framework.lock,env.example,AGENTS.md,gitignore.fragment}`
- Create: `tests/cli/test_init_validate.py`

**Interfaces:**
- Consumes: discovery JSON plus a strict confirmation YAML.
- Confirmation YAML has exactly `schema_version`, `discovery_digest`, and `values`; each value record has exactly `value` and `confirmed: true`.
- Produces: `initialize_scaffold(discovery, confirmations, target) -> tuple[Path, ...]` and `validate_control_directory(path) -> ValidationReport`.

- [ ] **Step 1: Write failing confirmation and no-overwrite tests**

Cover complete confirmations, one missing unknown, an inferred value without `confirmed: true`, wrong digest, duplicate key, secret-looking scalar, an existing target, failure before first write, and valid one-/two-/three-repository rendered manifests.

Assert a pre-existing `AGENTS.md`, `.env`, `.env.local`, `delivery.yaml`, or lock is byte-for-byte unchanged and no sibling scaffold file is created.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -B -m unittest tests.cli.test_init_validate -v
```

- [ ] **Step 3: Implement confirmation and templates**

All required manifest leaf paths must be confirmed or already classified confirmed with the same value and source digest. Reject YAML aliases, duplicate keys, unknown fields, values containing environment-variable contents, and secret keys not matching the Core secret-name grammar.

Preflight every destination before writing any file. Render command values as YAML string arrays, never shell strings. Write `framework.lock` as the exact uninitialized schema accepted by Core. `env.example` contains variable names with empty values only.

- [ ] **Step 4: Implement read-only validation**

Validation loads the rendered manifest/lock through Core, checks platform `darwin|linux`, Python `3.11|3.12|3.13`, and uses read-only version calls for `multica` and `gh`. It reports missing tools as validation failures and never calls reconciliation or secret lookup.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/python -B -m unittest tests.cli.test_init_validate -v
.venv/bin/python -B -m unittest discover -s tests/core -p 'test_*.py' -q
git add src/multica_delivery/cli src/multica_delivery/templates tests/cli
git commit -m "feat: initialize and validate delivery control"
```

### Task 6: Implement canonical plan artifacts and read-only planning

**Files:**
- Create: `src/multica_delivery/cli/plan.py`
- Create: `src/multica_delivery/cli/services.py`
- Create: `src/multica_delivery/cli/commands/plan.py`
- Modify: `src/multica_delivery/core/provision.py`
- Create: `tests/cli/test_plan.py`

**Interfaces:**
- Produces immutable `PlanAction`, `PlanBody`, `PlanEnvelope`, and `PlanStore`.
- `PlanBody` fields are exactly `schema_version`, `mode`, `cli_version`, `instance_key`, `manifest_digest`, `lock_digest`, `state_fingerprint`, `created_at`, `expires_at`, and `actions`.
- `mode` is exactly `onboard` or `upgrade`; timestamps are integer UTC epoch seconds.
- Extend `ReconcileResult` with safe lowercase-hex `state_fingerprint` derived from the stable redacted snapshot.

- [ ] **Step 1: Write failing canonicalization and planning tests**

Inject a clock fixed at `1787836800`. Assert `expires_at == 1787837400`, a 64-character lowercase hash, hash changes when a timestamp/action/digest changes, map ordering does not change the hash, floating-point or unknown fields fail, plan file mode is `0600`, and mutation adapters/secret lookup have zero calls.

Test two unequal stable reads return exit `4`, and a malformed/foreign external record returns exit `5` or `6` without a plan file.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -B -m unittest tests.cli.test_plan -v
```

- [ ] **Step 3: Expose a redacted authoritative fingerprint**

Compute the fingerprint inside `Provisioner` from canonical, typed snapshot identities that already drive `_plan`; exclude secret values and raw output. Dry-run performs two equal snapshot reads before returning the fingerprint. Preserve every existing provisioning test.

- [ ] **Step 4: Implement plan storage and service assembly**

`PlanningService.observe()` loads manifest/lock, validates, runs contract audit, and calls `Provisioner.reconcile(..., apply=False, secret_lookup=forbidden_lookup)`. Serialize `ReconcileAction` into ordered `PlanAction` values. `run_plan` writes only after the complete observation succeeds.

Production services use closed `shell=False` subprocess runners and exact manifest repository allowlists. Tests inject fakes below the typed clients.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/python -B -m unittest tests.cli.test_plan tests.core.test_provision -v
git add src/multica_delivery tests/cli/test_plan.py
git commit -m "feat: create authenticated delivery plans"
```

### Task 7: Implement explicitly confirmed apply

**Files:**
- Create: `src/multica_delivery/cli/apply.py`
- Create: `src/multica_delivery/cli/secrets.py`
- Create: `src/multica_delivery/cli/commands/apply.py`
- Create: `tests/cli/test_apply.py`

**Interfaces:**
- Produces `ApplyService.apply(plan_path, confirmation, manifest_path, lock_path, secret_source) -> ApplyResult`.
- `SecretSource` exposes `read(name: str) -> str`; environment and no-echo prompt implementations return only declared values.
- Apply compares a new `PlanningService.observe()` result to the approved body's digests, fingerprint, mode, and ordered actions before calling `Provisioner.reconcile(..., apply=True, ...)`.

- [ ] **Step 1: Write failing authorization tests**

Cover missing/partial/uppercase/wrong hash, age `601` seconds, future timestamp, different CLI compatibility line, manifest drift, lock drift, external fingerprint drift, action drift, interactive mismatch, and explicit noninteractive confirmation. Every denial asserts zero secret reads and zero mutation calls.

Cover an ambiguous fake mutation acknowledgement that converges under stable reread, a nonconvergent acknowledgement that human-blocks, atomic lock replacement, and a second newly planned converged apply with `mutation_count == 0`.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -B -m unittest tests.cli.test_apply -v
```

- [ ] **Step 3: Implement authorization before effects**

Validate the stored envelope and recompute its hash before comparing confirmation. Accept age `0..600` seconds inclusive. Reject any future timestamp. Do not construct an environment/prompt secret source until every plan and drift check passes.

Apply the exact plan through Provisioner, compare applied semantic actions to the plan, and atomically replace only the lock returned by a converged apply. Never delete or compensate external resources.

- [ ] **Step 4: Verify redaction, convergence, and commit**

```bash
.venv/bin/python -B -m unittest tests.cli.test_apply tests.core.test_provision -v
git add src/multica_delivery/cli tests/cli/test_apply.py
git commit -m "feat: apply exact confirmed delivery plans"
```

### Task 8: Implement doctor, upgrade, and the console entry point

**Files:**
- Create: `src/multica_delivery/cli/doctor.py`
- Create: `src/multica_delivery/cli/upgrade.py`
- Create: `src/multica_delivery/cli/commands/{doctor,upgrade}.py`
- Create: `src/multica_delivery/cli/main.py`
- Modify: `src/multica_delivery/cli/apply.py`
- Modify: `pyproject.toml`
- Create: `tests/cli/test_doctor_upgrade.py`
- Create: `tests/cli/test_main.py`
- Modify: `tests/cli/test_apply.py`

**Interfaces:**
- Produces seven registered command names: `discover`, `init`, `validate`, `plan`, `apply`, `doctor`, `upgrade`.
- Adds `[project.scripts] multica-delivery = "multica_delivery.cli.main:main"`.
- `doctor` produces typed `pass|warn|fail|human-block` findings and has no mutation/secret interface.
- `upgrade` emits `mode="upgrade"` plans; version `0.0.0 → 0.1.0` is the only initial framework-lock migration and changes no schema field.
- `MigrationExecutor.apply(body, lock_path) -> FrameworkLock` is selected only when an already-authorized plan has `mode == "upgrade"`; onboarding mode continues to call `Provisioner`.

- [ ] **Step 1: Write failing command-surface tests**

Assert exact command names, global `--output human|json`, `--version`, every help exit `0`, unknown flags exit `2`, JSON envelopes for every expected failure, and no traceback. Assert doctor calls only validators/auditors/dry-run planning. Assert upgrade refuses unknown/skipped versions and writes a confirmed no-op or lock-version action plan.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -B -m unittest tests.cli.test_doctor_upgrade tests.cli.test_main -v
```

- [ ] **Step 3: Implement doctor and migration registry**

Doctor aggregates local validation, authentication, contract audit, dry-run reconciliation, lock convergence, public Skill origin, recipient coverage, and watcher diagnostics without fixing them. Exit with the highest-severity stable exit code.

Upgrade uses an explicit tuple of migration edges. It rejects any path not exactly connected to `0.1.0`. Extend Task 7's `ApplyService` after its common hash/age/digest/fingerprint checks: `mode == "onboard"` invokes `Provisioner`, while `mode == "upgrade"` invokes the exact `MigrationExecutor` and atomically writes only the migrated lock. Neither branch may accept the other branch's action kinds.

- [ ] **Step 4: Implement top-level argparse dispatch**

`main(argv=None, services=None)` catches only typed `CliError` plus a final sanitized boundary error, renders one envelope, and returns an integer exit code. Command registration is explicit; there is no dynamic import or arbitrary callable name.

- [ ] **Step 5: Install and verify the console command**

```bash
.venv/bin/python -m pip install -e .
.venv/bin/multica-delivery --version
.venv/bin/multica-delivery --help
.venv/bin/python -B -m unittest tests.cli.test_doctor_upgrade tests.cli.test_main -v
git add pyproject.toml src/multica_delivery/cli tests/cli
git commit -m "feat: complete multica delivery lifecycle CLI"
```

### Task 9: Add lifecycle end-to-end tests, packaged resources, and CI

**Files:**
- Create: `tests/e2e/test_lifecycle.py`
- Create: `tests/e2e/fakes/{multica,gh}`
- Create: `tests/e2e/fixtures/{single,two,three}/`
- Create: `tests/test_wheel.py`
- Modify: `pyproject.toml`
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Produces installed-wheel lifecycle coverage with no live service.
- Packages `multica_delivery/templates/**` and exposes `template_path(name) -> Traversable` through `importlib.resources`.

- [ ] **Step 1: Write failing installed-wheel and lifecycle tests**

Build a wheel into a temporary directory, install it into a clean venv, and assert the console command, version, templates, and JSON schema are present. End-to-end fixtures run the complete lifecycle against fake executables whose state is stored only in the temporary test directory.

For each topology, assert read-only command mutation count zero, first approved apply convergence, second newly planned apply mutation count zero, doctor pass, upgrade plan production, and zero GitHub creation/push/merge/deploy events.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -B -m unittest tests.e2e.test_lifecycle tests.test_wheel -v
```

- [ ] **Step 3: Package resources and implement deterministic fakes**

Add setuptools package-data entries for `templates/**/*`. Fake executables accept only the production closed argv used by typed adapters and reject every other command. They return deterministic JSON and count mutations.

- [ ] **Step 4: Add the exact CI matrix**

The workflow uses:

```yaml
strategy:
  matrix:
    os: [ubuntu-latest, macos-latest]
    python-version: ["3.11", "3.12", "3.13"]
```

Each job installs `.[dev]`, runs all unittests, builds wheel/sdist, installs the wheel into a clean venv, checks the CLI, and runs compileall/diff checks. It performs no authenticated external command.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -q
.venv/bin/python -m build
git diff --check
git add pyproject.toml src/multica_delivery/templates tests .github/workflows/ci.yml
git commit -m "test: verify packaged lifecycle across topologies"
```

### Task 10: Establish failing Skill behavior baselines

**Files:**
- Create: `skill-evals/scenarios/{unconfirmed-input,stale-plan,scope-expansion}.md`
- Create: `skill-evals/baseline.md`
- Create: `tests/skill/test_eval_assets.py`

**Interfaces:**
- Produces three realistic evaluator prompts and verbatim no-Skill baseline results.
- This task creates no `skills/multica-multi-repo-delivery` directory; its absence is part of RED.

- [ ] **Step 1: Create scenario prompts and structural tests**

Each scenario supplies a fake CLI transcript and pressure:

- unconfirmed-input: inferred commands plus a request to “just continue”;
- stale-plan: an expired plan plus a deadline and partial hash;
- scope-expansion: an approved Multica apply plus pressure to create a GitHub repo and deploy.

Structural tests require raw user intent, fake CLI output, expected safety decision, and prohibited effect fields without matching exact prose.

- [ ] **Step 2: Run fresh evaluators without the Skill**

The controller dispatches one fresh-context evaluator per scenario with only the scenario and no Skill. Record exact decisions and rationalizations in `baseline.md`. At least one scenario must demonstrate the target failure; if all naturally pass, redesign the pressure scenarios before writing the Skill.

- [ ] **Step 3: Verify baseline evidence and commit**

```bash
.venv/bin/python -B -m unittest tests.skill.test_eval_assets -v
git add skill-evals tests/skill/test_eval_assets.py
git commit -m "test: capture multica delivery skill baselines"
```

### Task 11: Create and forward-test the reusable Skill

**Files:**
- Create: `skills/multica-multi-repo-delivery/SKILL.md`
- Create: `skills/multica-multi-repo-delivery/agents/openai.yaml`
- Create: `skills/multica-multi-repo-delivery/references/{lifecycle,manifest-schema,safety-boundaries,troubleshooting}.md`
- Create: `skill-evals/forward.md`
- Create: `tests/skill/test_skill_package.py`

**Interfaces:**
- Consumes: Task 10 scenarios and baseline failures.
- Produces: a discoverable Skill that calls only the installed CLI and preserves explicit apply authority.

- [ ] **Step 1: Initialize the Skill only after RED exists**

Use the bundled Skill initializer with `references` resources and no examples. Name and description are exactly:

```yaml
name: multica-multi-repo-delivery
description: Use when onboarding, validating, reconciling, diagnosing, or upgrading a Multica delivery team for one or more repositories.
```

Keep normal implicit invocation. Generate UI metadata with display name `Multica Multi-Repo Delivery`, short description `Onboard and operate manifest-scoped Multica delivery teams`, and default prompt `Onboard these repositories with a read-only discovery and stop before any external mutation.`

- [ ] **Step 2: Write the minimal Skill against observed failures**

Keep `SKILL.md` below 500 words. It routes first onboarding to discover, requires confirmation for inferred/unknown values, distinguishes init from apply, requires validate/plan, displays the full hash, requests authorization immediately before apply, and stops on expiry/drift/human-block. It states that GitHub creation/push/merge and deployment are separate authority.

References contain the exact CLI contract and troubleshooting detail; they do not repeat reconciliation implementation.

- [ ] **Step 3: Add package and validator tests**

Tests check frontmatter parseability, name/description identity, referenced-file existence, no unfinished scaffold markers, CLI version reference, absence of internal SkillsHub URLs, and packaged inclusion. Run the official quick validator against the Skill directory.

- [ ] **Step 4: Run independent forward evaluation**

The controller reruns Task 10 scenarios in fresh contexts with only the Skill path and fake CLI artifacts. Record verbatim decisions in `forward.md`. Every scenario must choose the correct command, stop at the required authorization boundary, and avoid prohibited effects. Add only corrections demonstrated by a failed forward scenario, then rerun it.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/python -B -m unittest tests.skill.test_skill_package tests.skill.test_eval_assets -v
python /Users/didi/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/multica-multi-repo-delivery
git add skills skill-evals/forward.md tests/skill
git commit -m "feat: add reusable multica delivery skill"
```

### Task 12: Add operator documentation, Eventra parity evidence, and release readiness

**Files:**
- Expand: `README.md`
- Create: `docs/operator-guide.md`
- Create: `docs/manifest-reference.md`
- Create: `docs/release-checklist.md`
- Create: `tests/parity/test_eventra_fixture.py`
- Create: `tests/parity/fixtures/eventra-delivery.yaml`
- Create: `tests/test_documentation.py`

**Interfaces:**
- Produces: local installation, lifecycle, safety, troubleshooting, and future tagged-install documentation.
- Produces: a sanitized Eventra fixture copied from commit `f62310731394ec27034645787d87749b1eb95d38` and parity assertions over manifest digest inputs, topology, roles, commands, policies, and public Skill origins.

- [ ] **Step 1: Write failing parity and documentation behavior tests**

Tests load the standalone fixture and assert two repositories, backend-before-frontend dependency/merge order, development-only automatic merge, deployment forbidden, exact role coverage, declared secret names without values, and public GitHub Skill origins. Documentation tests invoke every shown local command in `--help` or fake mode rather than matching headings.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -B -m unittest tests.parity.test_eventra_fixture tests.test_documentation -v
```

- [ ] **Step 3: Write operator and release documentation**

Document local `pipx install .`, editable development, seven lifecycle commands, discovery confirmations, plan expiry/hash, environment-secret handling, second-apply convergence, doctor, upgrade, Skill installation from a local path, macOS/Linux support, and the exact non-goals.

The release checklist stops before public actions and requires explicit approvals for repository creation, remote push, `v0.1.0` tag, and later Eventra immutable dependency migration. Do not include a guessed GitHub owner URL.

- [ ] **Step 4: Run complete verification**

```bash
.venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -q
.venv/bin/python -m build
python /Users/didi/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/multica-multi-repo-delivery
PYTHONPYCACHEPREFIX=/tmp/multica-delivery-v010-pycache .venv/bin/python -B -m compileall -q src tests
git diff --check
git status --short
```

Also search production additions for `shell=True`, checkout/reset, deploy/rollback implementations, product-specific names, internal SkillsHub URLs, literal credentials, and undocumented mutation commands. Expected: no matches.

- [ ] **Step 5: Commit**

```bash
git add README.md docs tests/parity tests/test_documentation.py
git commit -m "docs: prepare multica delivery v0.1.0"
```

## Final Cross-Repository Verification

After Task 12, run the standalone complete suite and build in its feature branch. Then, from the unchanged Eventra worktree, rerun:

```bash
.venv/bin/python -B -m unittest discover -s tools -p 'test_*.py' -q
```

Do not change Eventra dependency declarations, delete Eventra Core, create a remote, push, tag, publish, perform a real apply, or modify live Multica/GitHub state. Present those as separate post-implementation choices only after both repositories are clean and the standalone package/Skill review is approved.

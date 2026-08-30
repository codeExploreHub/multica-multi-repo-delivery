# Multica Multi-Repository Delivery

`multica-multi-repo-delivery` is a standalone CLI and Agent Skill for onboarding and operating one or more local repositories as one manifest-scoped Multica delivery team. It supports macOS and Linux with Python 3.11–3.13.

Version `0.2.0` uses workflow metadata version 2. Gate Stages fan in completely through Core/plan-parent, which is the sole canonical FailureBundle producer and decision authority; Delivery Lead alone executes the returned Stage or child action without reconstructing its bundle.

Install the CLI from a local checkout:

```bash
pipx install .
multica-delivery --version
```

For editable development:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

The seven commands are `discover`, `init`, `validate`, `plan`, `apply`, `doctor`, and `upgrade`. Start with the [operator guide](docs/operator-guide.md); use the [manifest reference](docs/manifest-reference.md) for configuration and the [release checklist](docs/release-checklist.md) before any public action.

To install the Skill from this checkout, copy `skills/multica-multi-repo-delivery` into the Skill directory configured by your agent runtime. The CLI wheel also installs the same files under `share/multica-multi-repo-delivery/skills/`.

The onboarding and lifecycle CLI never creates GitHub repositories, commits, pushes, merges, deploys, deletes, or rolls back. `apply` reconciles only the exact Multica configuration actions in a fresh, fully confirmed plan; the separately provisioned delivery workflow may merge pull requests only when its manifest policy and quality gates authorize that workflow action.

Upgrading an installed `0.1.0` control uses the explicit `0.1.0 -> 0.2.0` edge. Completed version-1 parents remain readable; active version-1 parents require explicit migration before mutable workflow activity. `upgrade` creates the plan, and `apply` still requires its current complete 64-character hash. That authority does not extend to Issues, PR SHA adoption, merge, push, tag, release, or deployment.

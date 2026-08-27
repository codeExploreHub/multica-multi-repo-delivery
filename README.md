# Multica Multi-Repository Delivery

`multica-multi-repo-delivery` is a standalone CLI and Agent Skill for onboarding and operating one or more local repositories as one manifest-scoped Multica delivery team. It supports macOS and Linux with Python 3.11–3.13.

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

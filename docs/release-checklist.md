# Release checklist

This checklist prepares `0.1.0`; it does not authorize a public release.

## Local verification

- [ ] Python support is green on macOS and Linux for 3.11, 3.12, and 3.13.
- [ ] Core, CLI, E2E, parity, Skill, documentation, and wheel-install tests pass.
- [ ] The Skill validator passes and installed artifacts contain templates plus Skill references.
- [ ] The second newly planned fake apply converges with zero mutations.
- [ ] No credentials, private repository identities, internal Skill sources, or literal secret values are present.

```bash
.venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -q
.venv/bin/python -m build
```

## Explicit approval gates

Stop and obtain a separate user approval for each item:

- [ ] public GitHub repository creation;
- [ ] initial remote push;
- [ ] creation and push of the `v0.1.0 tag`;
- [ ] package or marketplace publication;
- [ ] later Eventra immutable dependency migration to the approved tag.

The future pinned install form is recorded without guessing an owner:

```text
pipx install git+https://github.com/<approved-owner>/multica-multi-repo-delivery.git@v0.1.0
```

Before any immutable-tag migration, rerun Eventra compatibility and legacy suites against the installed tag. Do not remove its in-repository Core, mutate its live Multica instance, or publish anything as part of this local checklist.

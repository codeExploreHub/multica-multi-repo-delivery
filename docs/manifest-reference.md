# Manifest reference

`delivery-control/delivery.yaml` is operator-confirmed authority. `framework.lock` and `plan.json` are CLI-owned artifacts; do not hand-edit them.

## Confirmation YAML

Every inferred or unknown discovery field must be confirmed by exact manifest path and tied to the full discovery digest:

```yaml
schema_version: 1
discovery_digest: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
values:
  repositories.backend.default_branch:
    value: main
    confirmed: true
```

The schema rejects duplicate keys, YAML aliases, environment references, secret-like values, incomplete hashes, and records not explicitly confirmed.

## Delivery YAML

- `schema_version`: currently `1`.
- `instance`: stable instance key/display name, runtime ID, daemon ID, and control Project title.
- `control`: exact GitHub slug and absolute normalized local control path.
- `skill_registry`: approved public `https://github.com/...` sources only.
- `role_skills`: exact entries for `delivery-lead`, `independent-reviewer`, `integration-qa`, and `workflow-watcher`.
- `repositories`: one or more repository records with GitHub identity, absolute path, default branch, Project title, dependency keys, argv-array commands, service health contracts, Skill keys, and optional secret names/recipients.
- `integration_suites`: repositories, service start order, command owner, and argv-array smoke command.
- `merge_order`: dependency-safe repository order.
- `policies`: environment, automatic merge authority, `deployment: forbidden`, exactly two repair attempts, watcher cron, and IANA timezone.

Command values are arrays, not shell strings. Paths must be absolute, normalized, and non-aliased. Service names and ports must be unique. Dependency and merge graphs must be complete and acyclic.

Development may enable automatic merge after quality gates. Production cannot. Deployment remains forbidden to the package in both environments.

Validate locally before external planning:

```bash
multica-delivery validate /absolute/delivery-control
```

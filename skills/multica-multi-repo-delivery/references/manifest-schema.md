# Manifest and confirmation schema

## Confirmation document

Every non-confirmed discovery value uses an explicit manifest path. The document is secret-free and bound to the full discovery digest:

```yaml
schema_version: 1
discovery_digest: <64-lowercase-hex>
values:
  repositories.api.default_branch:
    value: main
    confirmed: true
```

Top-level keys are exact. Aliases, duplicate keys, environment references, secret-like values, partial digests, and records without `confirmed: true` are rejected.

## Delivery manifest

`delivery.yaml` declares:

- `instance`: stable key/display name plus exact runtime, daemon, and control Project;
- `control`: GitHub slug and absolute local path;
- `skill_registry`: approved public `https://github.com/...` sources;
- `role_skills`: exact bindings for delivery lead, independent reviewer, integration QA, and workflow watcher;
- `repositories`: GitHub identity, absolute path, default branch, dependencies, argv-array commands, services, Skill keys, and secret environment names/recipients;
- `integration_suites` and `merge_order`: cross-repository validation and dependency order;
- `policies`: `development` or `production`, automatic merge authority, `deployment: forbidden`, two repair attempts, and watcher schedule/timezone.

Secret declarations contain names and recipients only. Values belong in the authorized apply-time environment or prompt, never in manifest, confirmation, plan, lock, argv, or logs.

Treat `framework.lock` as CLI-owned output. Do not hand-edit identities or version fields; use `upgrade` for supported migrations.

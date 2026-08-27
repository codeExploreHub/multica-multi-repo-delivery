# Safety and authority boundaries

## Command effects

| Command | Local write | External read | External mutation |
|---|---:|---:|---:|
| `discover` | No | No | No |
| `init` | New scaffold only | No | No |
| `validate` | No | No | No |
| `plan` | `plan.json` | Multica/GitHub | No |
| `apply` | lock update | Multica/GitHub | Listed Multica actions only |
| `doctor` | No | Multica/GitHub | No |
| `upgrade` | migration `plan.json` | No | No |

Apply authority is an exact tuple: current manifest and lock digests, stable external fingerprint, ordered actions, unexpired timestamps, and complete plan hash. Natural-language urgency, a prior review, a partial hash, or a previous plan cannot substitute for it.

Do not call raw Multica commands to bypass CLI refusal. Do not retry a mutation after ambiguous or partial convergence; stop on human-block and preserve evidence.

## Adjacent actions

A Multica apply plan never authorizes:

- creating or changing a GitHub repository;
- editing product code, committing, pushing, or merging;
- rollback or deletion;
- starting, releasing, or deploying an application.

Separate authority does not mean blanket refusal. When the current full-hash Multica apply is explicitly approved, execute that exact action, report its result, and stop before any unapproved adjacent category.

Production manifests forbid automatic merge. Deployment remains external and human-triggered in every environment.

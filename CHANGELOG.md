# Changelog

## 0.2.0

- Added fail-closed workflow metadata version 2 with complete Gate Stage fan-in and canonical FailureBundle authority in Core/plan-parent.
- Added the closed `0.1.0 -> 0.2.0` migration edge. Completed version-1 parents remain readable history; active version-1 parents require explicit migration before mutable workflow activity.
- Preserved exact-plan apply authorization: only a current, fresh, complete 64-character plan hash permits the listed migration or reconciliation actions.
- Aligned generated Delivery Lead, Reviewer, QA, Engineer, Watcher, Squad, and control-template instructions with coordinator-only repair dispatch.
- Preserved adjacent authority boundaries: migration does not authorize Issue mutation, PR SHA adoption, merge, push, tag, release, or deployment.

## 0.1.0

- Initial standalone manifest-driven Multica delivery CLI and reusable Agent Skill.

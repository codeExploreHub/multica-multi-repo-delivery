# Multica Delivery Control

Treat `delivery.yaml` as operator-confirmed authority. Use workflow metadata version 2. Plan before apply and never widen apply into GitHub, Issue, release, or deployment operations.

For a Gate Stage, wait for every current child to become terminal. Core/plan-parent is the sole fan-in, canonical FailureBundle producer, and decision authority. Delivery Lead validates the canonical plan, uses its exact FailureBundle and digest without reconstruction, and is the sole Stage and child execution actor.

Exactly two repair rounds are automatic. Round 3 requires a member-authored authorization bound to the exact current FailureBundle and digest. The authoritative comment body must be canonical closed JSON `{"bundle_digest":"<64-lowercase-hex>","granted_round":3}` and must exactly match the persisted authorization, current bundle, and next round. It authorizes exactly the next round, is consumed once, and cannot authorize a different bundle or later round.

Reviewer and QA stop at structured verdict evidence: they do not create a FailureBundle, direct an Engineer, or dispatch repair. An Engineer may change code only for a current active implementation or repair child; repair requires the exact bundle, an existing managed PR, and every assigned failure-partition reference. The Watcher cannot create a FailureBundle or dispatch repair and may only rerun one existing current assignment.

Completed version-1 workflow metadata remains readable history. Active version-1 work requires explicit migration before a new Stage, repair, merge, or completion action.

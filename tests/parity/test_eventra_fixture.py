from pathlib import Path
import json
import unittest

import yaml

from multica_delivery.core.manifest import load_manifest, manifest_digest
from multica_delivery.core.metadata import ParentMetadata, encode_parent_metadata
from multica_delivery.core.decisions import ParentSnapshot, decide_parent_action
from multica_delivery.core.provision import effective_skill_bindings


FIXTURE = Path(__file__).parent / "fixtures" / "eventra-delivery.yaml"
EXPECTED = Path(__file__).parent / "fixtures" / "eventra-expected.json"
ROLES = {"delivery-lead", "independent-reviewer", "integration-qa", "workflow-watcher"}


class EventraParityFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = FIXTURE.read_text(encoding="utf-8")
        cls.raw = yaml.safe_load(cls.text)
        cls.manifest = load_manifest(FIXTURE)
        cls.expected = json.loads(EXPECTED.read_text(encoding="utf-8"))

    def test_source_commit_and_digest_are_pinned(self):
        self.assertIn(self.expected["source_commit"], self.text)
        self.assertEqual(
            manifest_digest(self.manifest),
            self.expected["manifest"]["digest"],
        )

    def test_manifest_projection_matches_immutable_source_baseline(self):
        manifest = self.manifest
        actual = {
            "digest": manifest_digest(manifest),
            "repositories": list(manifest.repositories),
            "merge_order": list(manifest.merge_order),
            "integration_start_order": list(manifest.integration_suites[0].start_order),
        }
        self.assertEqual(actual, self.expected["manifest"])

    def test_provision_bindings_match_immutable_source_baseline(self):
        actual = {
            key: list(value)
            for key, value in sorted(effective_skill_bindings(self.manifest).items())
        }
        self.assertEqual(
            actual,
            self.expected["provision"]["effective_skill_bindings"],
        )

    def test_metadata_and_initial_decision_match_immutable_source_baseline(self):
        metadata = ParentMetadata(
            instance_key=self.manifest.instance.key,
            affected_repositories=("frontend", "backend"),
            repository_dag={"frontend": ("backend",), "backend": ()},
            candidate_shas={},
            contract_hashes={},
            stage_ordinal=0,
            merge_plan=(),
            merge_state="pending",
            attempt=0,
            last_action="dispatch",
        )
        self.assertEqual(
            encode_parent_metadata(metadata),
            self.expected["metadata"]["parent"],
        )

        decision = decide_parent_action(
            self.manifest,
            ParentSnapshot(affected_repositories=("frontend", "backend")),
        )
        actual_decision = {
            "kind": decision.kind.value,
            "reason": decision.reason,
            "repositories": list(decision.repositories),
            "next_attempt": decision.next_attempt,
            "dispatch_kind": (
                decision.dispatch_kind.value if decision.dispatch_kind else None
            ),
        }
        self.assertEqual(
            actual_decision,
            self.expected["decisions"]["initial_cross_stack"],
        )

    def test_topology_commands_and_policy_match_eventra_contract(self):
        manifest = self.manifest
        self.assertEqual(set(manifest.repositories), {"frontend", "backend"})
        self.assertEqual(manifest.repositories["frontend"].depends_on, ("backend",))
        self.assertEqual(manifest.merge_order, ("backend", "frontend"))
        self.assertEqual(manifest.integration_suites[0].start_order, ("backend", "frontend"))
        self.assertEqual(manifest.repositories["frontend"].commands["start"], ("npm", "run", "dev:local"))
        self.assertEqual(manifest.repositories["backend"].commands["test"], ("scripts/test-local.sh",))
        self.assertEqual(manifest.policy.environment, "development")
        self.assertTrue(manifest.policy.automatic_merge)
        self.assertEqual(manifest.policy.deployment, "forbidden")

    def test_roles_secret_names_and_public_skill_origins_are_sanitized(self):
        manifest = self.manifest
        self.assertEqual(set(manifest.role_skills), ROLES)
        self.assertEqual(
            set(manifest.repositories["backend"].secret_env),
            {"JWT_SECRET", "MAIL_USERNAME", "MAIL_PASSWORD"},
        )
        declarations = self.raw["repositories"]["backend"]["secret_env"]
        self.assertTrue(
            all(
                set(record) == {"recipients"}
                and all(recipient in {"engineer", "integration-qa"} for recipient in record["recipients"])
                for record in declarations.values()
            )
        )
        self.assertTrue(
            all(source.approved and source.url.startswith("https://github.com/") for source in manifest.skill_registry.values())
        )
        bindings = effective_skill_bindings(manifest)
        self.assertEqual(
            set(bindings),
            ROLES | {"frontend-engineer", "backend-engineer"},
        )


if __name__ == "__main__":
    unittest.main()

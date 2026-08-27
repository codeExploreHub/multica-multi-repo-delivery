from pathlib import Path
import unittest

import yaml

from multica_delivery.core.manifest import load_manifest, manifest_digest
from multica_delivery.core.provision import effective_skill_bindings


FIXTURE = Path(__file__).parent / "fixtures" / "eventra-delivery.yaml"
ROLES = {"delivery-lead", "independent-reviewer", "integration-qa", "workflow-watcher"}


class EventraParityFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = FIXTURE.read_text(encoding="utf-8")
        cls.raw = yaml.safe_load(cls.text)
        cls.manifest = load_manifest(FIXTURE)

    def test_source_commit_and_digest_are_pinned(self):
        self.assertIn("f62310731394ec27034645787d87749b1eb95d38", self.text)
        self.assertEqual(
            manifest_digest(self.manifest),
            "43a0cd01a4fd605c45be137d884c780f6448cd7508b00284c0b536e68d7e7218",
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

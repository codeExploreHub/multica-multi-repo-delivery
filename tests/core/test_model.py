from dataclasses import MISSING, fields
from types import MappingProxyType
import unittest

from multica_delivery.core.model import (
    DeliveryManifest,
    RepositorySpec,
    github_skill_origin_matches,
)


class ModelCompatibilityTests(unittest.TestCase):
    def test_github_skill_origin_comparison_matches_eventra_parity_matrix(self):
        commit = "b36e0829c6d0140e93cfef2ca599b1b07d4a7797"
        other_commit = "a" * 40
        main = "https://github.com/obra/superpowers/tree/main/skills/using-superpowers"
        resolved = f"https://github.com/obra/superpowers/tree/{commit}/skills/using-superpowers"

        def origin(
            source_url,
            *,
            origin_type="github",
            owner="obra",
            repo="superpowers",
            ref="main",
            path="skills/using-superpowers",
        ):
            return {
                "type": origin_type,
                "owner": owner,
                "repo": repo,
                "ref": ref,
                "path": path,
                "source_url": source_url,
            }

        cases = (
            ("exact branch", main, origin(main), True),
            ("resolved commit", main, origin(resolved, ref=commit), True),
            ("exact pinned commit", resolved, origin(resolved, ref=commit), True),
            (
                "different pinned commit",
                resolved,
                origin(
                    f"https://github.com/obra/superpowers/tree/{other_commit}/skills/using-superpowers",
                    ref=other_commit,
                ),
                False,
            ),
            (
                "different branch",
                main,
                origin(
                    "https://github.com/obra/superpowers/tree/other/skills/using-superpowers",
                    ref="other",
                ),
                False,
            ),
            ("wrong type", main, origin(main, origin_type="http"), False),
            ("wrong owner", main, origin(main, owner="attacker"), False),
            ("wrong repo", main, origin(main, repo="lookalike"), False),
            ("wrong path", main, origin(main, path="skills/lookalike"), False),
            ("inconsistent ref", main, origin(main, ref="other"), False),
            (
                "generic HTTP",
                main,
                origin(
                    "http://github.com/obra/superpowers/tree/main/skills/using-superpowers"
                ),
                False,
            ),
            (
                "uppercase digest",
                main,
                origin(
                    "https://github.com/obra/superpowers/tree/"
                    f"{commit.upper()}/skills/using-superpowers",
                    ref=commit.upper(),
                ),
                False,
            ),
        )
        for label, desired, observed, expected in cases:
            with self.subTest(label=label):
                self.assertIs(
                    github_skill_origin_matches(desired, observed),
                    expected,
                )

    def test_empty_mapping_defaults_are_created_by_factories(self):
        cases = (
            (RepositorySpec, "secret_env"),
            (DeliveryManifest, "role_skills"),
        )
        proxy_type = type(MappingProxyType({}))

        for model_type, field_name in cases:
            with self.subTest(model=model_type.__name__, field=field_name):
                definition = next(
                    item for item in fields(model_type) if item.name == field_name
                )
                self.assertIs(definition.default, MISSING)
                self.assertIsNot(definition.default_factory, MISSING)
                first = definition.default_factory()
                second = definition.default_factory()
                self.assertIsInstance(first, proxy_type)
                self.assertEqual(dict(first), {})
                self.assertIsNot(first, second)


if __name__ == "__main__":
    unittest.main()

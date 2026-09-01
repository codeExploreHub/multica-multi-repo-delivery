from pathlib import Path
import re
import tomllib
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "multica-multi-repo-delivery"
DESCRIPTION = (
    "Use when onboarding, validating, reconciling, diagnosing, or upgrading a "
    "Multica delivery team for one or more repositories."
)
REFERENCES = {
    "lifecycle.md",
    "manifest-schema.md",
    "safety-boundaries.md",
    "troubleshooting.md",
}


def skill_parts() -> tuple[dict[str, object], str]:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    match = re.fullmatch(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if match is None:
        raise AssertionError("SKILL.md must contain YAML frontmatter")
    metadata = yaml.safe_load(match.group(1))
    if not isinstance(metadata, dict):
        raise AssertionError("Skill frontmatter must be a mapping")
    return metadata, match.group(2)


class SkillPackageTests(unittest.TestCase):
    def test_frontmatter_ui_and_entrypoint_are_complete(self):
        metadata, body = skill_parts()
        self.assertEqual(metadata, {
            "name": "multica-multi-repo-delivery",
            "description": DESCRIPTION,
        })
        self.assertLess(len(body.split()), 500)
        self.assertIn("multica-delivery --version", body)
        self.assertIn("Version `0.2.0`", body)
        self.assertIn("every current Gate Stage child", body)
        self.assertIn("Core/plan-parent", body)
        self.assertIn("canonical FailureBundle", body)
        self.assertIn("without reconstruction", body)
        self.assertIn("completed version-1", body.lower())
        self.assertIn("active version-1", body.lower())
        self.assertIn("explicit migration", body)
        self.assertIn("complete 64-character hash", body)
        self.assertIn("Exactly two repair rounds are automatic", body)
        self.assertIn("Round 3 requires a member-authored authorization", body)
        self.assertIn("exact current FailureBundle and digest", body)
        self.assertIn("exactly the next round", body)
        self.assertIn("consumed once", body)
        self.assertIn("different bundle or later round", body)
        self.assertNotRegex(body, r"\b(TODO|FIXME|PLACEHOLDER)\b")

        interface = yaml.safe_load((SKILL / "agents" / "openai.yaml").read_text())
        self.assertEqual(interface["interface"]["display_name"], "Multica Multi-Repo Delivery")
        self.assertEqual(
            interface["interface"]["short_description"],
            "Onboard and operate manifest-scoped Multica delivery teams",
        )
        self.assertEqual(
            interface["interface"]["default_prompt"],
            "Onboard these repositories with a read-only discovery and stop before any external mutation.",
        )
        self.assertNotEqual(interface.get("policy", {}).get("allow_implicit_invocation"), False)

    def test_every_declared_reference_exists_and_forbidden_sources_are_absent(self):
        _, body = skill_parts()
        linked = set(re.findall(r"\]\(references/([a-z-]+\.md)\)", body))
        self.assertEqual(linked, REFERENCES)
        actual = {path.name for path in (SKILL / "references").glob("*.md")}
        self.assertEqual(actual, REFERENCES)
        all_text = "\n".join(path.read_text(encoding="utf-8") for path in SKILL.rglob("*.*"))
        self.assertNotIn("SkillsHub", all_text)
        self.assertNotIn("intra.xiaojukeji", all_text)
        self.assertNotRegex(all_text, r"\b(TODO|FIXME|PLACEHOLDER)\b")

    def test_references_define_fan_in_migration_and_adjacent_mutation_boundaries(self):
        lifecycle = (SKILL / "references" / "lifecycle.md").read_text(encoding="utf-8")
        safety = (SKILL / "references" / "safety-boundaries.md").read_text(encoding="utf-8")
        troubleshooting = (SKILL / "references" / "troubleshooting.md").read_text(encoding="utf-8")
        all_text = "\n".join((lifecycle, safety, troubleshooting))

        self.assertIn("workflow metadata version 2", all_text)
        self.assertIn("completed version-1", all_text.lower())
        self.assertIn("active version-1", all_text.lower())
        self.assertIn("0.1.0 -> 0.2.0", all_text)
        self.assertIn("complete 64-character", all_text)
        for action in ("Issues", "adopt PR SHAs", "merge", "push", "tag", "release", "deploy"):
            self.assertIn(action, all_text)

    def test_distribution_declares_every_skill_resource(self):
        configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        data_files = configuration["tool"]["setuptools"]["data-files"]
        prefix = "share/multica-multi-repo-delivery/skills/multica-multi-repo-delivery"
        self.assertEqual(data_files[prefix], ["skills/multica-multi-repo-delivery/SKILL.md"])
        self.assertEqual(
            data_files[f"{prefix}/agents"],
            ["skills/multica-multi-repo-delivery/agents/openai.yaml"],
        )
        self.assertEqual(
            set(data_files[f"{prefix}/references"]),
            {f"skills/multica-multi-repo-delivery/references/{name}" for name in REFERENCES},
        )


if __name__ == "__main__":
    unittest.main()

from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "skill-evals" / "scenarios"
EXPECTED = {"unconfirmed-input", "stale-plan", "scope-expansion"}


def load_scenario(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text(encoding="utf-8")
    match = re.fullmatch(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if match is None:
        raise AssertionError(f"{path.name} must have YAML frontmatter")
    metadata = yaml.safe_load(match.group(1))
    if not isinstance(metadata, dict):
        raise AssertionError(f"{path.name} frontmatter must be a mapping")
    return metadata, match.group(2)


class SkillEvaluationAssetTests(unittest.TestCase):
    def test_three_pressure_scenarios_define_behavioral_contracts(self):
        paths = {path.stem: path for path in SCENARIOS.glob("*.md")}
        self.assertEqual(set(paths), EXPECTED)
        for identifier, path in paths.items():
            with self.subTest(scenario=identifier):
                metadata, body = load_scenario(path)
                self.assertEqual(metadata.get("id"), identifier)
                self.assertIn(metadata.get("expected_safety_decision"), {"stop", "read-only"})
                prohibited = metadata.get("prohibited_effects")
                self.assertIsInstance(prohibited, list)
                self.assertGreater(len(prohibited), 0)
                self.assertTrue(all(isinstance(item, str) and item for item in prohibited))
                self.assertIn("## Raw user intent", body)
                self.assertIn("## Fake CLI output", body)
                self.assertIn("## Evaluator task", body)
                self.assertGreaterEqual(body.count("Pressure:"), 3)

    def test_baseline_records_verbatim_fresh_evaluator_results_and_a_failure(self):
        baseline = (ROOT / "skill-evals" / "baseline.md").read_text(encoding="utf-8")
        for identifier in EXPECTED:
            self.assertIn(f"## {identifier}", baseline)
            self.assertRegex(
                baseline,
                rf"(?s)## {re.escape(identifier)}.*?### Verbatim evaluator response\n```text\n.+?\n```",
            )
        self.assertIn("baseline_target_failure: true", baseline)
        self.assertNotIn("skills/multica-multi-repo-delivery", baseline)


if __name__ == "__main__":
    unittest.main()

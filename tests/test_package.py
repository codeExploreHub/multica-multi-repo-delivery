import importlib.metadata
from pathlib import Path
import re
import tomllib
import unittest

import multica_delivery
from multica_delivery.core.provision import WORKFLOW_METADATA_VERSION


class PackageTests(unittest.TestCase):
    def test_distribution_and_module_share_version(self):
        self.assertEqual(multica_delivery.__version__, "0.2.0")
        self.assertEqual(
            importlib.metadata.version("multica-multi-repo-delivery"),
            "0.2.0",
        )

    def test_release_uses_workflow_metadata_version_two(self):
        self.assertEqual(WORKFLOW_METADATA_VERSION, 2)

    def test_release_metadata_uses_spdx_and_exact_runtime_dependency(self):
        metadata = importlib.metadata.metadata("multica-multi-repo-delivery")
        self.assertEqual(metadata["License-Expression"], "Apache-2.0")
        self.assertEqual(metadata["Requires-Python"], ">=3.11")
        runtime = [
            requirement
            for requirement in metadata.get_all("Requires-Dist", [])
            if "extra ==" not in requirement
        ]
        self.assertEqual(runtime, ["PyYAML==6.0.2"])

    def test_release_build_inputs_and_actions_are_immutable(self):
        root = Path(__file__).resolve().parents[1]
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            pyproject["build-system"]["requires"],
            ["setuptools==82.0.1", "wheel==0.47.0"],
        )
        self.assertEqual(pyproject["project"]["optional-dependencies"]["dev"], ["build==1.5.0"])

        workflow = (root / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        uses = re.findall(r"uses:\s*([^\s#]+)", workflow)
        self.assertTrue(uses)
        self.assertTrue(
            all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", value) for value in uses)
        )
        self.assertIn("SOURCE_DATE_EPOCH", workflow)
        self.assertIn("tools/normalize_sdist.py", workflow)
        self.assertIn("cmp dist/*.whl dist-repeat/*.whl", workflow)
        self.assertIn("cmp dist/*.tar.gz dist-repeat/*.tar.gz", workflow)

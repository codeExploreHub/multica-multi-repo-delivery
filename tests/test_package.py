import importlib.metadata
import unittest

import multica_delivery


class PackageTests(unittest.TestCase):
    def test_distribution_and_module_share_version(self):
        self.assertEqual(multica_delivery.__version__, "0.1.0")
        self.assertEqual(
            importlib.metadata.version("multica-multi-repo-delivery"),
            "0.1.0",
        )

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

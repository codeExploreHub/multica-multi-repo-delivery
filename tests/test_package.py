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

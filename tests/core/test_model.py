from dataclasses import MISSING, fields
from types import MappingProxyType
import unittest

from multica_delivery.core.model import DeliveryManifest, RepositorySpec


class ModelCompatibilityTests(unittest.TestCase):
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

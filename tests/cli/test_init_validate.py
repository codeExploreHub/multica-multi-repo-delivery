from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from multica_delivery.cli.confirmation import load_confirmation_text
from multica_delivery.cli.discovery import LocalRepositoryReader, discover_repositories
from multica_delivery.cli.errors import CliError
from multica_delivery.cli.templates import initialize_scaffold
from multica_delivery.cli.commands.init import run_init
from multica_delivery.cli.commands.validate import run_validate
from multica_delivery.cli.validation import validate_control_directory
from multica_delivery.core.manifest import load_lock, load_manifest


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "discovery"
ROLE_NAMES = (
    "delivery-lead",
    "independent-reviewer",
    "integration-qa",
    "workflow-watcher",
)


class VersionReader:
    def __init__(self, versions: dict[str, str | None]) -> None:
        self.versions = versions
        self.calls: list[str] = []
        self.mutation_calls: list[object] = []
        self.secret_reads: list[object] = []

    def version(self, executable: str) -> str | None:
        self.calls.append(executable)
        return self.versions.get(executable)


class InitValidateTests(unittest.TestCase):
    def _copy_repositories(self, root: Path, names: tuple[str, ...]):
        repositories = []
        for name in names:
            destination = root / name
            shutil.copytree(FIXTURES / name, destination)
            (destination / "_git").rename(destination / ".git")
            repositories.append(destination)
        return discover_repositories(repositories, LocalRepositoryReader())

    def _confirmation_document(self, discovery, control_path: Path) -> dict:
        values: dict[str, dict[str, object]] = {}

        def confirm(path: str, value: object) -> None:
            values[path] = {"value": value, "confirmed": True}

        confirm("instance.key", "sample-delivery")
        confirm("instance.display_name", "Sample Delivery")
        confirm("instance.runtime_id", "11111111-1111-4111-8111-111111111111")
        confirm("instance.daemon_id", "22222222-2222-4222-8222-222222222222")
        confirm("instance.control_project", "Sample Delivery Control")
        confirm("control.github", "example/sample-delivery-control")
        confirm("control.local_path", str(control_path))
        confirm(
            "skill_registry.using-superpowers.url",
            "https://github.com/openai/superpowers/tree/main/skills/using-superpowers",
        )
        confirm("skill_registry.using-superpowers.approved", True)
        for role in ROLE_NAMES:
            confirm(f"role_skills.{role}", ["using-superpowers"])
        confirm("policies.environment", "development")
        confirm("policies.automatic_merge", True)
        confirm("policies.watcher_timezone", "Asia/Shanghai")

        repository_keys = []
        for index, repository in enumerate(discovery.repositories):
            key = repository.name.value
            repository_keys.append(key)
            confirm(f"repositories.{key}.default_branch", "main")
            confirm(f"repositories.{key}.project", f"Sample Repository {index + 1}")
            confirm(f"repositories.{key}.depends_on", [])
            confirm(
                f"repositories.{key}.commands.focused_test",
                ["python3", "-m", "unittest", "-q"],
            )
            discovered_test = repository.commands.get("test")
            test_command = list(discovered_test.value) if discovered_test else ["python3", "-m", "unittest"]
            confirm(f"repositories.{key}.commands.test", test_command)
            confirm(f"repositories.{key}.commands.build", ["python3", "-m", "compileall", "."])
            confirm(f"repositories.{key}.commands.start", ["python3", "-m", "http.server", "8000"])
            confirm(f"repositories.{key}.commands.smoke", ["python3", "-c", "print('ok')"])
            confirm(f"repositories.{key}.services", [])
            confirm(f"repositories.{key}.skills", ["using-superpowers"])

        confirm("integration_suites", {})
        confirm("merge_order", repository_keys)
        return {
            "schema_version": 1,
            "discovery_digest": discovery.discovery_digest,
            "values": values,
        }

    def _load_confirmation(self, document: dict):
        return load_confirmation_text(yaml.safe_dump(document, sort_keys=False))

    def test_complete_confirmation_initializes_exact_scaffold(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            discovery = self._copy_repositories(root, ("frontend",))
            target = root / "delivery-control"
            confirmation = self._load_confirmation(
                self._confirmation_document(discovery, target)
            )

            created = initialize_scaffold(discovery, confirmation, target)

            self.assertEqual(
                tuple(path.name for path in created),
                ("delivery.yaml", "framework.lock", "env.example"),
            )
            manifest = load_manifest(target / "delivery.yaml")
            lock = load_lock(target / "framework.lock")
            repository = manifest.repositories["sample-frontend"]
            self.assertEqual(repository.commands["test"], ("npm", "test"))
            self.assertEqual(lock.manifest_schema_version, 1)
            self.assertEqual(lock.resource_ids, {})
            self.assertEqual((target / "env.example").read_text(), "")

    def test_missing_unknown_stops_before_first_write(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            discovery = self._copy_repositories(root, ("frontend",))
            target = root / "delivery-control"
            document = self._confirmation_document(discovery, target)
            del document["values"]["repositories.sample-frontend.project"]

            with self.assertRaises(CliError) as caught:
                initialize_scaffold(discovery, self._load_confirmation(document), target)

            self.assertEqual(caught.exception.code, "confirmation.incomplete")
            self.assertFalse(target.exists())

    def test_inferred_value_requires_confirmed_true(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            discovery = self._copy_repositories(root, ("frontend",))
            target = root / "delivery-control"
            document = self._confirmation_document(discovery, target)
            document["values"]["repositories.sample-frontend.commands.test"]["confirmed"] = False

            with self.assertRaises(CliError) as caught:
                self._load_confirmation(document)

            self.assertEqual(caught.exception.code, "confirmation.not_confirmed")

    def test_wrong_discovery_digest_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            discovery = self._copy_repositories(root, ("frontend",))
            target = root / "delivery-control"
            document = self._confirmation_document(discovery, target)
            document["discovery_digest"] = "0" * 64

            with self.assertRaises(CliError) as caught:
                initialize_scaffold(discovery, self._load_confirmation(document), target)

            self.assertEqual(caught.exception.code, "confirmation.discovery_drift")
            self.assertFalse(target.exists())

    def test_duplicate_keys_and_yaml_aliases_are_rejected(self):
        duplicate = """\
schema_version: 1
schema_version: 1
discovery_digest: abc
values: {}
"""
        alias = """\
schema_version: 1
discovery_digest: abc
values:
  instance.key: &shared
    value: sample
    confirmed: true
  instance.display_name: *shared
"""

        for text in (duplicate, alias):
            with self.subTest(text=text):
                with self.assertRaises(CliError) as caught:
                    load_confirmation_text(text)
                self.assertEqual(caught.exception.code, "confirmation.invalid_yaml")

    def test_secret_looking_scalar_is_rejected_without_reading_environment(self):
        document = {
            "schema_version": 1,
            "discovery_digest": "a" * 64,
            "values": {
                "instance.display_name": {
                    "value": "${JWT_SECRET}",
                    "confirmed": True,
                }
            },
        }

        with self.assertRaises(CliError) as caught:
            self._load_confirmation(document)

        self.assertEqual(caught.exception.code, "confirmation.secret_value")

    def test_invalid_secret_environment_name_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            discovery = self._copy_repositories(root, ("frontend",))
            target = root / "delivery-control"
            document = self._confirmation_document(discovery, target)
            document["values"]["repositories.sample-frontend.secret_env.bad-name.recipients"] = {
                "value": ["integration-qa"],
                "confirmed": True,
            }

            with self.assertRaises(CliError) as caught:
                initialize_scaffold(discovery, self._load_confirmation(document), target)

            self.assertEqual(caught.exception.code, "confirmation.invalid_secret_name")
            self.assertFalse(target.exists())

    def test_existing_reserved_target_is_unchanged_and_no_sibling_is_created(self):
        reserved = ("AGENTS.md", ".env", ".env.local", "delivery.yaml", "framework.lock")
        for filename in reserved:
            with self.subTest(filename=filename), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                discovery = self._copy_repositories(root, ("frontend",))
                target = root / "delivery-control"
                target.mkdir()
                existing = target / filename
                existing.write_bytes(b"operator-owned")
                confirmation = self._load_confirmation(
                    self._confirmation_document(discovery, target)
                )

                with self.assertRaises(CliError) as caught:
                    initialize_scaffold(discovery, confirmation, target)

                self.assertEqual(caught.exception.code, "init.target_exists")
                self.assertEqual(existing.read_bytes(), b"operator-owned")
                self.assertEqual(tuple(target.iterdir()), (existing,))

    def test_concurrently_created_empty_target_is_never_replaced(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            discovery = self._copy_repositories(root, ("frontend",))
            target = root / "delivery-control"
            confirmation = self._load_confirmation(
                self._confirmation_document(discovery, target)
            )
            real_mkdir = Path.mkdir

            def concurrent_mkdir(path, *args, **kwargs):
                if Path(path) == target:
                    real_mkdir(target)
                    raise FileExistsError(target)
                return real_mkdir(path, *args, **kwargs)

            with patch.object(Path, "mkdir", autospec=True, side_effect=concurrent_mkdir):
                with self.assertRaises(CliError) as caught:
                    initialize_scaffold(discovery, confirmation, target)

            self.assertEqual(caught.exception.code, "init.concurrent_target")
            self.assertTrue(target.is_dir())
            self.assertEqual(tuple(target.iterdir()), ())

    def test_renders_valid_one_two_and_three_repository_manifests(self):
        topologies = (
            ("frontend",),
            ("frontend", "backend"),
            ("frontend", "backend", "worker"),
        )
        for names in topologies:
            with self.subTest(names=names), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                discovery = self._copy_repositories(root, names)
                target = root / "delivery-control"
                confirmation = self._load_confirmation(
                    self._confirmation_document(discovery, target)
                )

                initialize_scaffold(discovery, confirmation, target)
                manifest = load_manifest(target / "delivery.yaml")

                self.assertEqual(len(manifest.repositories), len(names))
                for repository in manifest.repositories.values():
                    for command in repository.commands.values():
                        self.assertIsInstance(command, tuple)

    def test_validation_is_read_only_and_reports_tool_availability(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            discovery = self._copy_repositories(root, ("frontend",))
            target = root / "delivery-control"
            initialize_scaffold(
                discovery,
                self._load_confirmation(self._confirmation_document(discovery, target)),
                target,
            )
            tools = VersionReader({"multica": "multica 1.2.3", "gh": "gh version 2.80.0"})

            report = validate_control_directory(
                target,
                version_reader=tools,
                platform_name="linux",
                python_version=(3, 13),
            )

            self.assertTrue(report.valid)
            self.assertEqual(tools.calls, ["multica", "gh"])
            self.assertEqual(tools.mutation_calls, [])
            self.assertEqual(tools.secret_reads, [])

            missing = VersionReader({"multica": "multica 1.2.3", "gh": None})
            failed = validate_control_directory(
                target,
                version_reader=missing,
                platform_name="linux",
                python_version=(3, 13),
            )
            self.assertFalse(failed.valid)
            self.assertIn("tool.gh_missing", [finding.code for finding in failed.findings])

    def test_init_and_validate_commands_return_envelopes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            discovery = self._copy_repositories(root, ("frontend",))
            target = root / "delivery-control"
            confirmation = self._load_confirmation(
                self._confirmation_document(discovery, target)
            )
            init_envelope = run_init(
                SimpleNamespace(target=target),
                SimpleNamespace(discovery=discovery, confirmations=confirmation),
            )
            tools = VersionReader({"multica": "1", "gh": "2"})
            validate_envelope = run_validate(
                SimpleNamespace(path=target),
                SimpleNamespace(
                    version_reader=tools,
                    platform_name="linux",
                    python_version=(3, 13),
                ),
            )

            self.assertEqual(init_envelope.status, "ok")
            self.assertEqual(validate_envelope.status, "ok")


if __name__ == "__main__":
    unittest.main()

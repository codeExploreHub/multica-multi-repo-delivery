import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from multica_delivery.cli.discovery import (
    Classification,
    LocalRepositoryReader,
    discover_repositories,
)
from multica_delivery.cli.errors import CliError, ExitCode
from multica_delivery.cli.commands.discover import run_discover


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "discovery"


class RecordingReader(LocalRepositoryReader):
    def __init__(self) -> None:
        self.read_calls: list[Path] = []
        self.write_calls: list[object] = []
        self.command_calls: list[object] = []

    def read_optional(self, path: Path) -> bytes | None:
        self.read_calls.append(path)
        return super().read_optional(path)


class ChangingReader(RecordingReader):
    def __init__(self, changing_path: Path) -> None:
        super().__init__()
        self.changing_path = changing_path
        self.changing_reads = 0

    def read_optional(self, path: Path) -> bytes | None:
        if path == self.changing_path:
            self.read_calls.append(path)
            self.changing_reads += 1
            if self.changing_reads == 1:
                return b'{"name":"before","scripts":{"test":"one"}}'
            return b'{"name":"after","scripts":{"test":"two"}}'
        return super().read_optional(path)


class DiscoveryTests(unittest.TestCase):
    def _copy_repository(self, root: Path, name: str) -> Path:
        destination = root / name
        shutil.copytree(FIXTURES / name, destination)
        (destination / "_git").rename(destination / ".git")
        return destination

    def test_discovers_one_repository_without_effects(self):
        with TemporaryDirectory() as directory:
            frontend = self._copy_repository(Path(directory).resolve(), "frontend")
            reader = RecordingReader()

            document = discover_repositories([frontend], reader)

            repository = document.repositories[0]
            self.assertEqual(document.schema_version, 1)
            self.assertEqual(repository.root.classification, Classification.CONFIRMED)
            self.assertEqual(repository.root.source, "operator argument")
            self.assertEqual(repository.commands["test"].classification, Classification.INFERRED)
            self.assertEqual(repository.commands["test"].value, ("npm", "test"))
            self.assertEqual(repository.project.classification, Classification.UNKNOWN)
            self.assertEqual(document.runtime_id.classification, Classification.UNKNOWN)
            self.assertEqual(document.daemon_id.classification, Classification.UNKNOWN)
            self.assertEqual(reader.write_calls, [])
            self.assertEqual(reader.command_calls, [])

    def test_discovers_two_and_three_repository_topologies(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            frontend = self._copy_repository(root, "frontend")
            backend = self._copy_repository(root, "backend")
            worker = self._copy_repository(root, "worker")

            pair = discover_repositories([frontend, backend], RecordingReader())
            trio = discover_repositories([frontend, backend, worker], RecordingReader())

            self.assertEqual(len(pair.repositories), 2)
            self.assertEqual(len(trio.repositories), 3)
            backend_result = next(item for item in trio.repositories if item.root.value == str(backend))
            self.assertEqual(backend_result.kind.value, "maven")
            self.assertEqual(backend_result.commands["test"].value, ("./mvnw", "test"))

    def test_digest_ignores_presentation_order_but_covers_findings(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            frontend = self._copy_repository(root, "frontend")
            backend = self._copy_repository(root, "backend")

            forward = discover_repositories([frontend, backend], RecordingReader())
            reverse = discover_repositories([backend, frontend], RecordingReader())
            before = forward.discovery_digest
            package = frontend / "package.json"
            value = json.loads(package.read_text())
            value["scripts"]["lint"] = "eslint ."
            package.write_text(json.dumps(value))
            changed = discover_repositories([frontend, backend], RecordingReader())

            self.assertEqual(forward.discovery_digest, reverse.discovery_digest)
            self.assertNotEqual(before, changed.discovery_digest)

    def test_rejects_nonabsolute_path(self):
        with self.assertRaises(CliError) as caught:
            discover_repositories([Path("relative/repository")], RecordingReader())

        self.assertEqual(caught.exception.code, "discovery.path_not_absolute")
        self.assertEqual(caught.exception.exit_code, ExitCode.VALIDATION)

    def test_rejects_duplicate_root(self):
        with TemporaryDirectory() as directory:
            frontend = self._copy_repository(Path(directory).resolve(), "frontend")

            with self.assertRaises(CliError) as caught:
                discover_repositories([frontend, frontend], RecordingReader())

            self.assertEqual(caught.exception.code, "discovery.duplicate_root")

    def test_rejects_alias_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            frontend = self._copy_repository(root, "frontend")
            alias = root / "frontend-alias"
            alias.symlink_to(frontend, target_is_directory=True)

            with self.assertRaises(CliError) as caught:
                discover_repositories([alias], RecordingReader())

            self.assertEqual(caught.exception.code, "discovery.aliased_root")

    def test_rejects_nested_repository_roots(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            frontend = self._copy_repository(root, "frontend")
            nested = frontend / "packages" / "child"
            nested.mkdir(parents=True)
            (nested / "package.json").write_text('{"name":"child"}')

            with self.assertRaises(CliError) as caught:
                discover_repositories([frontend, nested], RecordingReader())

            self.assertEqual(caught.exception.code, "discovery.nested_root")

    def test_rejects_unsupported_root_object(self):
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "not-a-repository"
            path.write_text("file")

            with self.assertRaises(CliError) as caught:
                discover_repositories([path], RecordingReader())

            self.assertEqual(caught.exception.code, "discovery.unsupported_root")

    def test_rejects_changing_authoritative_reads(self):
        with TemporaryDirectory() as directory:
            frontend = self._copy_repository(Path(directory).resolve(), "frontend")
            reader = ChangingReader(frontend / "package.json")

            with self.assertRaises(CliError) as caught:
                discover_repositories([frontend], reader)

            self.assertEqual(caught.exception.code, "discovery.changing_read")
            self.assertEqual(reader.write_calls, [])
            self.assertEqual(reader.command_calls, [])

    def test_command_returns_envelope_without_writing(self):
        with TemporaryDirectory() as directory:
            frontend = self._copy_repository(Path(directory).resolve(), "frontend")
            reader = RecordingReader()
            args = SimpleNamespace(paths=[frontend])
            services = SimpleNamespace(repository_reader=reader)

            envelope = run_discover(args, services)

            self.assertEqual(envelope.command, "discover")
            self.assertEqual(envelope.status, "ok")
            self.assertEqual(envelope.result["schema_version"], 1)
            self.assertRegex(envelope.result["discovery_digest"], r"^[0-9a-f]{64}$")
            self.assertEqual(reader.write_calls, [])
            self.assertEqual(reader.command_calls, [])


if __name__ == "__main__":
    unittest.main()

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from multica_delivery.cli.clock import Clock, SystemClock
from multica_delivery.cli.errors import CliError, ExitCode
from multica_delivery.cli.files import atomic_replace_private, atomic_write_new
from multica_delivery.cli.output import Envelope, SafeMessage


class FoundationTests(unittest.TestCase):
    def test_exit_codes_are_stable(self):
        self.assertEqual(
            {member.name: member.value for member in ExitCode},
            {
                "OK": 0,
                "VALIDATION": 2,
                "CONFIRMATION": 3,
                "DRIFT": 4,
                "EXTERNAL": 5,
                "HUMAN_BLOCK": 6,
            },
        )

    def test_cli_error_exposes_only_safe_fields(self):
        error = CliError("manifest.invalid", "Manifest is invalid", ExitCode.VALIDATION)

        self.assertEqual(error.code, "manifest.invalid")
        self.assertEqual(error.safe_message, "Manifest is invalid")
        self.assertEqual(error.exit_code, ExitCode.VALIDATION)
        self.assertEqual(
            vars(error),
            {
                "code": "manifest.invalid",
                "safe_message": "Manifest is invalid",
                "exit_code": ExitCode.VALIDATION,
            },
        )

    def test_envelope_json_is_canonical_and_messages_are_sorted(self):
        envelope = Envelope(
            command="validate",
            status="failed",
            result={"z": 2, "a": "中文"},
            warnings=(
                SafeMessage("warning.z", "Zulu"),
                SafeMessage("warning.a", "Alpha"),
            ),
            errors=(
                SafeMessage("error.z", "Zulu"),
                SafeMessage("error.a", "Alpha"),
            ),
        )

        self.assertEqual(
            envelope.to_json(),
            '{"command":"validate","errors":[{"code":"error.a","message":"Alpha"},{"code":"error.z","message":"Zulu"}],"result":{"a":"中文","z":2},"schema_version":1,"status":"failed","warnings":[{"code":"warning.a","message":"Alpha"},{"code":"warning.z","message":"Zulu"}]}',
        )
        self.assertEqual(json.loads(envelope.to_json())["result"]["a"], "中文")

    def test_human_output_is_not_json(self):
        rendered = Envelope(command="doctor", status="ok", result={"checks": 3}).to_human()

        self.assertEqual(rendered, "doctor: ok\nchecks: 3")
        with self.assertRaises(json.JSONDecodeError):
            json.loads(rendered)

    def test_clock_can_be_injected_and_system_clock_is_utc(self):
        instant = datetime(2026, 8, 27, 12, 30, tzinfo=timezone.utc)

        class FixedClock:
            def now(self) -> datetime:
                return instant

        clock: Clock = FixedClock()
        self.assertIs(clock.now(), instant)
        self.assertIs(SystemClock().now().tzinfo, timezone.utc)

    def test_atomic_write_new_refuses_to_overwrite(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.json"
            path.write_bytes(b"original")

            with self.assertRaises(FileExistsError):
                atomic_write_new(path, b"replacement")

            self.assertEqual(path.read_bytes(), b"original")

    def test_atomic_write_new_creates_exact_bytes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.json"

            atomic_write_new(path, b'{"safe":true}\n')

            self.assertEqual(path.read_bytes(), b'{"safe":true}\n')

    def test_atomic_replace_private_sets_mode_and_replaces_contents(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "framework.lock"
            path.write_bytes(b"old")
            os.chmod(path, 0o644)

            atomic_replace_private(path, b"new")

            self.assertEqual(path.read_bytes(), b"new")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_atomic_replace_fsyncs_before_replace(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            events: list[str] = []
            real_fsync = os.fsync
            real_replace = os.replace

            def recording_fsync(fd: int) -> None:
                events.append("fsync")
                real_fsync(fd)

            def recording_replace(source: str, destination: str) -> None:
                events.append("replace")
                real_replace(source, destination)

            with (
                patch("multica_delivery.cli.files.os.fsync", side_effect=recording_fsync),
                patch("multica_delivery.cli.files.os.replace", side_effect=recording_replace),
            ):
                atomic_replace_private(path, b"planned")

            self.assertEqual(events, ["fsync", "replace"])

    def test_atomic_replace_failure_preserves_original_and_cleans_temp(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "framework.lock"
            path.write_bytes(b"original")

            with patch(
                "multica_delivery.cli.files.os.replace",
                side_effect=OSError("synthetic replacement failure"),
            ):
                with self.assertRaises(OSError):
                    atomic_replace_private(path, b"new")

            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual([item.name for item in root.iterdir()], ["framework.lock"])


if __name__ == "__main__":
    unittest.main()

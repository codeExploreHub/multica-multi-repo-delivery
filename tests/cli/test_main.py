from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import unittest

from multica_delivery import __version__
from multica_delivery.cli.errors import CliError, ExitCode
from multica_delivery.cli.main import build_parser, command_names, main
from multica_delivery.cli.output import Envelope


class FakeServices:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, object]] = []

    def _run(self, command: str, args):
        self.calls.append((command, args))
        if self.failure is not None:
            raise self.failure
        return Envelope(command=command, status="ok", result={"command": command})

    def discover(self, args): return self._run("discover", args)
    def init(self, args): return self._run("init", args)
    def validate(self, args): return self._run("validate", args)
    def plan(self, args): return self._run("plan", args)
    def apply(self, args): return self._run("apply", args)
    def doctor(self, args): return self._run("doctor", args)
    def upgrade(self, args): return self._run("upgrade", args)


class MainTests(unittest.TestCase):
    def _invoke(self, argv, services=None):
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv, services=services or FakeServices())
        return code, stdout.getvalue(), stderr.getvalue()

    def test_exact_command_names_are_registered(self):
        self.assertEqual(
            command_names(build_parser()),
            ("discover", "init", "validate", "plan", "apply", "doctor", "upgrade"),
        )

    def test_global_output_version_and_every_help_exit_zero(self):
        code, output, _ = self._invoke(["--version"])
        self.assertEqual(code, 0)
        self.assertIn(__version__, output)

        for command in command_names(build_parser()):
            with self.subTest(command=command):
                code, output, error = self._invoke([command, "--help"])
                self.assertEqual(code, 0)
                self.assertIn("usage:", output)
                self.assertEqual(error, "")

        services = FakeServices()
        code, output, _ = self._invoke(
            ["--output", "json", "discover", "/tmp/repository"],
            services,
        )
        self.assertEqual(code, 0)
        self.assertIn('"command":"discover"', output)

    def test_unknown_flags_exit_two_as_json_without_traceback(self):
        code, output, error = self._invoke(
            ["--output", "json", "discover", "/tmp/repository", "--unknown"]
        )

        self.assertEqual(code, 2)
        self.assertIn('"status":"failed"', output)
        self.assertNotIn("Traceback", output + error)

    def test_typed_failures_render_stable_json_envelope(self):
        services = FakeServices(
            CliError("manifest.invalid", "Manifest is invalid", ExitCode.VALIDATION)
        )
        code, output, error = self._invoke(
            ["--output", "json", "validate", "/tmp/control"],
            services,
        )

        self.assertEqual(code, 2)
        self.assertIn('"code":"manifest.invalid"', output)
        self.assertNotIn("Traceback", output + error)

    def test_unexpected_failures_are_sanitized(self):
        services = FakeServices(RuntimeError("RAW_SECRET_SENTINEL"))
        code, output, error = self._invoke(
            ["--output", "json", "doctor", "/tmp/control"],
            services,
        )

        self.assertEqual(code, 5)
        self.assertNotIn("RAW_SECRET_SENTINEL", output + error)
        self.assertNotIn("Traceback", output + error)

    def test_invalid_output_choice_exits_two(self):
        code, output, error = self._invoke(
            ["--output", "xml", "discover", "/tmp/repository"]
        )

        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", output + error)


if __name__ == "__main__":
    unittest.main()

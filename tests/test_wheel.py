import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import yaml

from multica_delivery.cli.templates import template_path


class WheelTests(unittest.TestCase):
    @staticmethod
    def _supported_python() -> str:
        candidates = [sys.executable]
        candidates.extend(
            executable
            for name in ("python3.13", "python3.12", "python3.11")
            if (executable := shutil.which(name)) is not None
        )
        for executable in candidates:
            completed = subprocess.run(
                [
                    executable,
                    "-c",
                    "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode == 0:
                return executable
        raise unittest.SkipTest("no supported Python 3.11-3.13 interpreter is available")

    @staticmethod
    def _run_json(command: Path, arguments: list[str], environment: dict[str, str]) -> dict:
        completed = subprocess.run(
            [str(command), "--output", "json", *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=120,
        )
        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise AssertionError(
                f"console emitted non-JSON output for {arguments}: {completed.stdout!r}"
            ) from error
        if completed.returncode != 0:
            raise AssertionError(
                f"console failed for {arguments}: rc={completed.returncode}, envelope={envelope}"
            )
        return envelope

    def test_template_resources_are_available_from_the_installed_package(self):
        for name in ("delivery.yaml", "framework.lock", "env.example", "AGENTS.md", "gitignore.fragment"):
            resource = template_path(name)
            self.assertTrue(resource.is_file())
            self.assertIsInstance(resource.read_text(encoding="utf-8"), str)

    def test_built_wheel_installs_console_and_resources_in_clean_venv(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            wheelhouse = temporary / "wheelhouse"
            wheelhouse.mkdir()
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--wheel",
                    "--outdir",
                    str(wheelhouse),
                ],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            wheel = next(wheelhouse.glob("*.whl"))
            environment = temporary / "venv"
            subprocess.run([self._supported_python(), "-m", "venv", str(environment)], check=True)
            python = environment / "bin" / "python"
            subprocess.run(
                [str(python), "-m", "pip", "install", str(wheel)],
                check=True,
                capture_output=True,
                text=True,
            )
            command = environment / "bin" / "multica-delivery"
            version = subprocess.run([str(command), "--version"], check=True, capture_output=True, text=True)
            schema = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import sys; from pathlib import Path; "
                    "from multica_delivery.cli.templates import template_path; "
                    "from multica_delivery.cli.output import Envelope; "
                    "assert template_path('framework.lock').is_file(); "
                    "skill = Path(sys.prefix) / 'share/multica-multi-repo-delivery/skills/multica-multi-repo-delivery'; "
                    "assert (skill / 'SKILL.md').is_file(); "
                    "assert (skill / 'agents/openai.yaml').is_file(); "
                    "assert (skill / 'references/lifecycle.md').is_file(); "
                    "assert Envelope('test','ok').to_value()['schema_version'] == 1",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("0.1.0", version.stdout)
            self.assertEqual(schema.stdout, "")

    def test_installed_console_runs_confirmed_full_lifecycle_with_fake_boundaries(self):
        root = Path(__file__).resolve().parents[1]
        fake_bin = root / "tests" / "e2e" / "fakes"
        with TemporaryDirectory() as directory:
            temporary = Path(directory).resolve()
            wheelhouse = temporary / "wheelhouse"
            wheelhouse.mkdir()
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--wheel",
                    "--outdir",
                    str(wheelhouse),
                ],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            wheel = next(wheelhouse.glob("*.whl"))
            environment_root = temporary / "venv"
            subprocess.run(
                [self._supported_python(), "-m", "venv", str(environment_root)],
                check=True,
            )
            python = environment_root / "bin" / "python"
            subprocess.run(
                [str(python), "-m", "pip", "install", str(wheel)],
                check=True,
                capture_output=True,
                text=True,
            )
            command = environment_root / "bin" / "multica-delivery"

            repository = temporary / "product"
            repository.mkdir()
            (repository / "package.json").write_text(
                json.dumps({"name": "product", "scripts": {"test": "node --test"}}),
                encoding="utf-8",
            )
            (repository / ".git").mkdir()
            (repository / ".git" / "config").write_text(
                '[remote "origin"]\nurl = https://github.com/example/product.git\n',
                encoding="utf-8",
            )

            base_environment = dict(os.environ)
            discovery = self._run_json(
                command,
                ["discover", str(repository)],
                base_environment,
            )
            self.assertEqual(discovery["command"], "discover")
            discovery_path = temporary / "discovery.json"
            discovery_path.write_text(json.dumps(discovery), encoding="utf-8")
            target = temporary / "delivery-control"
            repository_key = discovery["result"]["repositories"][0]["name"]["value"]
            values: dict[str, dict[str, object]] = {}

            def confirm(path: str, value: object) -> None:
                values[path] = {"value": value, "confirmed": True}

            confirm("instance.key", "installed-e2e")
            confirm("instance.display_name", "Installed E2E")
            confirm("instance.runtime_id", "11111111-1111-4111-8111-111111111111")
            confirm("instance.daemon_id", "22222222-2222-4222-8222-222222222222")
            confirm("instance.control_project", "Installed E2E Control")
            confirm("control.github", "example/installed-e2e-control")
            confirm("control.local_path", str(target))
            confirm(
                "skill_registry.using-superpowers.url",
                "https://github.com/openai/superpowers/tree/main/skills/using-superpowers",
            )
            confirm("skill_registry.using-superpowers.approved", True)
            for role in (
                "delivery-lead",
                "independent-reviewer",
                "integration-qa",
                "workflow-watcher",
            ):
                confirm(f"role_skills.{role}", ["using-superpowers"])
            confirm("policies.environment", "development")
            confirm("policies.automatic_merge", True)
            confirm("policies.watcher_timezone", "Asia/Shanghai")
            confirm(f"repositories.{repository_key}.default_branch", "main")
            confirm(f"repositories.{repository_key}.project", "Installed Product")
            confirm(f"repositories.{repository_key}.depends_on", [])
            for name, argv in {
                "focused_test": ["python3", "-m", "unittest", "-q"],
                "test": ["python3", "-m", "unittest"],
                "build": ["python3", "-m", "compileall", "."],
                "start": ["python3", "-m", "http.server", "8123"],
                "smoke": ["python3", "-c", "print('ok')"],
            }.items():
                confirm(f"repositories.{repository_key}.commands.{name}", argv)
            confirm(f"repositories.{repository_key}.services", [])
            confirm(f"repositories.{repository_key}.skills", ["using-superpowers"])
            confirm("integration_suites", {})
            confirm("merge_order", [repository_key])
            confirmation_path = temporary / "confirmation.yaml"
            confirmation_path.write_text(
                yaml.safe_dump(
                    {
                        "schema_version": 1,
                        "discovery_digest": discovery["result"]["discovery_digest"],
                        "values": values,
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            initialized = self._run_json(
                command,
                [
                    "init",
                    "--discovery",
                    str(discovery_path),
                    "--confirmation",
                    str(confirmation_path),
                    "--target",
                    str(target),
                ],
                base_environment,
            )
            self.assertEqual(initialized["status"], "ok")

            multica_state = temporary / "multica-state.json"
            multica_state.write_text(
                json.dumps(
                    {
                        "runtime": {
                            "id": "11111111-1111-4111-8111-111111111111",
                            "daemon_id": "22222222-2222-4222-8222-222222222222",
                            "status": "online",
                            "metadata": {"capabilities": ["local-worktree-v1"]},
                        },
                        "skills": {},
                        "projects": {},
                        "resources": {},
                        "agents": {},
                        "bindings": {},
                        "environments": {},
                        "squads": {},
                        "members": {},
                        "autopilots": {},
                        "triggers": {},
                        "counters": {},
                        "mutation_count": 0,
                        "events": [],
                        "rejected_argv": [],
                    }
                ),
                encoding="utf-8",
            )
            github_state = temporary / "github-state.json"
            github_state.write_text(
                json.dumps(
                    {
                        "repositories": {
                            "example/installed-e2e-control": {
                                "default_branch": "main",
                                "visibility": "private",
                            },
                            "example/product": {
                                "default_branch": "main",
                                "visibility": "private",
                            },
                            "openai/superpowers": {
                                "default_branch": "main",
                                "visibility": "public",
                            },
                        },
                        "read_calls": [],
                        "prohibited_events": [],
                    }
                ),
                encoding="utf-8",
            )
            lifecycle_environment = dict(base_environment)
            lifecycle_environment.update(
                {
                    "PATH": f"{fake_bin}:{environment_root / 'bin'}:{base_environment['PATH']}",
                    "MULTICA_FAKE_STATE": str(multica_state),
                    "GH_FAKE_STATE": str(github_state),
                }
            )

            validated = self._run_json(command, ["validate", str(target)], lifecycle_environment)
            self.assertTrue(validated["result"]["valid"])
            first_plan = self._run_json(command, ["plan", str(target)], lifecycle_environment)
            self.assertGreater(len(first_plan["result"]["body"]["actions"]), 0)
            self.assertTrue(
                all(action["reason"] for action in first_plan["result"]["body"]["actions"])
            )
            first_apply = self._run_json(
                command,
                [
                    "apply",
                    "--plan",
                    str(target / "plan.json"),
                    "--confirm",
                    first_plan["result"]["plan_hash"],
                ],
                lifecycle_environment,
            )
            self.assertGreater(first_apply["result"]["mutation_count"], 0)
            mutation_count = json.loads(multica_state.read_text())["mutation_count"]

            second_plan = self._run_json(command, ["plan", str(target)], lifecycle_environment)
            self.assertEqual(second_plan["result"]["body"]["actions"], [])
            second_apply = self._run_json(
                command,
                [
                    "apply",
                    "--plan",
                    str(target / "plan.json"),
                    "--confirm",
                    second_plan["result"]["plan_hash"],
                ],
                lifecycle_environment,
            )
            self.assertEqual(second_apply["result"]["mutation_count"], 0)
            self.assertEqual(
                json.loads(multica_state.read_text())["mutation_count"],
                mutation_count,
            )
            doctor = self._run_json(command, ["doctor", str(target)], lifecycle_environment)
            self.assertTrue(doctor["result"]["healthy"])
            upgrade = self._run_json(command, ["upgrade", str(target)], lifecycle_environment)
            self.assertEqual(upgrade["result"]["body"]["mode"], "upgrade")

            multica_record = json.loads(multica_state.read_text())
            github_record = json.loads(github_state.read_text())
            self.assertEqual(multica_record["mutation_count"], mutation_count)
            self.assertEqual(multica_record["rejected_argv"], [])
            self.assertEqual(github_record["prohibited_events"], [])
            self.assertFalse(any("deploy" in event for event in multica_record["events"]))


if __name__ == "__main__":
    unittest.main()

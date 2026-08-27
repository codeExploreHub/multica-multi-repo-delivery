from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from multica_delivery.cli.templates import template_path


class WheelTests(unittest.TestCase):
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
                [sys.executable, "-m", "build", "--wheel", "--outdir", str(wheelhouse)],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            wheel = next(wheelhouse.glob("*.whl"))
            environment = temporary / "venv"
            subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
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


if __name__ == "__main__":
    unittest.main()

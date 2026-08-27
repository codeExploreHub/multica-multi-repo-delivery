from pathlib import Path
import os
import re
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "docs" / "operator-guide.md",
    ROOT / "docs" / "manifest-reference.md",
    ROOT / "docs" / "release-checklist.md",
)
COMMANDS = {"discover", "init", "validate", "plan", "apply", "doctor", "upgrade"}


def bash_commands(text: str) -> tuple[tuple[str, ...], ...]:
    commands = []
    for block in re.findall(r"```bash\n(.*?)\n```", text, re.DOTALL):
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if any(operator in line for operator in ("&&", "||", ";", "|", ">", "<")):
                raise AssertionError(f"documentation command must be one closed argv: {line}")
            commands.append(tuple(shlex.split(line)))
    return tuple(commands)


class DocumentationTests(unittest.TestCase):
    def test_every_documented_local_command_is_executable_in_help_or_fake_mode(self):
        texts = [path.read_text(encoding="utf-8") for path in DOCUMENTS]
        commands = tuple(command for text in texts for command in bash_commands(text))
        self.assertGreater(len(commands), 7)
        documented_lifecycle = set()
        with TemporaryDirectory() as directory:
            fake = Path(directory)
            for executable in ("pipx", "python3"):
                path = fake / executable
                path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                path.chmod(0o755)
            environment = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}")
            for argv in commands:
                with self.subTest(argv=argv):
                    self.assertIn(argv[0], {"multica-delivery", "pipx", "python3"})
                    if argv[0] == "multica-delivery":
                        executable = str(Path(sys.executable).with_name("multica-delivery"))
                        if "--version" in argv:
                            probe = [executable, "--version"]
                        else:
                            command = next((part for part in argv if part in COMMANDS), None)
                            self.assertIsNotNone(command)
                            documented_lifecycle.add(command)
                            probe = [executable, command, "--help"]
                        completed = subprocess.run(probe, capture_output=True, text=True, check=False)
                    else:
                        completed = subprocess.run(argv, env=environment, capture_output=True, text=True, check=False)
                    self.assertEqual(completed.returncode, 0)
        self.assertEqual(documented_lifecycle, COMMANDS)

    def test_release_material_keeps_public_actions_unapproved_and_owner_unresolved(self):
        text = "\n".join(path.read_text(encoding="utf-8") for path in DOCUMENTS)
        self.assertIn("<approved-owner>", text)
        self.assertNotRegex(text, r"github\.com/(?!<approved-owner>)[A-Za-z0-9_.-]+/multica-multi-repo-delivery")
        for action in ("repository creation", "remote push", "v0.1.0 tag", "Eventra immutable dependency migration"):
            self.assertIn(action, text)


if __name__ == "__main__":
    unittest.main()

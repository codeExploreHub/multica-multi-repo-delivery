"""Production assembly with closed subprocess and repository scopes."""

from __future__ import annotations

from pathlib import Path
import subprocess

from multica_delivery.adapters.github_client import GitHubClient
from multica_delivery.adapters.multica_client import CommandResult, MulticaClient
from multica_delivery.core.manifest import load_manifest, skill_repository_slug
from multica_delivery.core.provision import Provisioner

from .plan import PlanningService


class ClosedSubprocessRunner:
    """Run only argv already approved by a typed GitHub or Multica client."""

    _EXECUTABLES = {"gh", "multica"}

    def run(self, argv: tuple[str, ...], *, input_text: str | None = None) -> CommandResult:
        if (
            not isinstance(argv, tuple)
            or not argv
            or argv[0] not in self._EXECUTABLES
            or not all(isinstance(part, str) and part and "\0" not in part for part in argv)
        ):
            raise ValueError("unsupported subprocess argv")
        completed = subprocess.run(
            list(argv),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
        return CommandResult(completed.returncode, completed.stdout)


def build_planning_service(control_path: Path) -> PlanningService:
    manifest = load_manifest(
        Path(control_path) / "delivery.yaml",
        strict_commands=True,
    )
    allowed = frozenset(
        {manifest.control.github}
        | {repository.github for repository in manifest.repositories.values()}
        | {
            skill_repository_slug(source.url)
            for source in manifest.skill_registry.values()
        }
    )
    runner = ClosedSubprocessRunner()
    multica = MulticaClient(
        runner,
        runtime_id=manifest.instance.runtime_id,
        daemon_id=manifest.instance.daemon_id,
    )
    github = GitHubClient(runner, allowed)
    return PlanningService(Provisioner(multica, github))

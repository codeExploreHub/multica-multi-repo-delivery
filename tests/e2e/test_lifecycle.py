import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

import yaml

from multica_delivery.adapters.github_client import GitHubClient
from multica_delivery.adapters.multica_client import MulticaClient
from multica_delivery.cli.apply import ApplyService
from multica_delivery.cli.discovery import LocalRepositoryReader, discover_repositories
from multica_delivery.cli.doctor import DoctorService
from multica_delivery.cli.plan import PlanStore, PlanningService
from multica_delivery.cli.services import ClosedSubprocessRunner
from multica_delivery.cli.upgrade import UpgradeService
from multica_delivery.cli.validation import ValidationFinding, ValidationReport
from multica_delivery.core.contract_audit import ContractAuditEntry, ContractAuditReport
from multica_delivery.core.provision import Provisioner
from tests.cli.test_apply import CountingSecretSource, EpochClock


HERE = Path(__file__).parent
FINDINGS = ValidationReport((ValidationFinding("pass", "e2e", "valid"),))
AUDIT = ContractAuditReport((ContractAuditEntry("e2e", "pass", "available"),))


class LifecycleTests(unittest.TestCase):
    def _manifest(self, root: Path, count: int):
        repositories = {}
        for index in range(1, count + 1):
            key = f"repo{index}"
            local = root / key
            local.mkdir()
            (local / "package.json").write_text(json.dumps({"name": key, "scripts": {"test": "node --test"}}))
            (local / ".git").mkdir()
            (local / ".git" / "config").write_text(f'[remote "origin"]\nurl = https://github.com/example/{key}.git\n')
            repositories[key] = {
                "github": f"example/{key}",
                "local_path": str(local),
                "default_branch": "main",
                "project": f"Repository {index}",
                "commands": {
                    "focused_test": ["python3", "-m", "unittest", "-q"],
                    "test": ["python3", "-m", "unittest"],
                    "build": ["python3", "-m", "compileall", "."],
                    "start": ["python3", "-m", "http.server", str(8100 + index)],
                    "smoke": ["python3", "-c", "print('ok')"],
                },
                "services": [],
                "skills": ["using-superpowers"],
            }
        control = root / "delivery-control"
        control.mkdir()
        document = {
            "schema_version": 1,
            "instance": {
                "key": f"e2e-{count}",
                "display_name": f"E2E {count}",
                "runtime_id": "11111111-1111-4111-8111-111111111111",
                "daemon_id": "22222222-2222-4222-8222-222222222222",
                "control_project": f"E2E {count} Control",
            },
            "control": {"github": f"example/e2e-{count}-control", "local_path": str(control)},
            "skill_registry": {
                "using-superpowers": {
                    "url": "https://github.com/openai/superpowers/tree/main/skills/using-superpowers",
                    "approved": True,
                }
            },
            "role_skills": {
                role: ["using-superpowers"]
                for role in ("delivery-lead", "independent-reviewer", "integration-qa", "workflow-watcher")
            },
            "policies": {
                "environment": "development",
                "automatic_merge": True,
                "deployment": "forbidden",
                "max_repair_attempts": 2,
                "watcher_cron": "*/30 * * * *",
                "watcher_timezone": "Asia/Shanghai",
            },
            "repositories": repositories,
            "integration_suites": {},
            "merge_order": list(repositories),
        }
        (control / "delivery.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
        (control / "framework.lock").write_text("""skill_version: ''
engine_version: ''
manifest_schema_version: 1
workflow_metadata_version: 2
supported_multica_cli: ''
manifest_digest: ''
resource_ids: {}
""")
        return control, tuple(root / key for key in repositories), tuple(item["github"] for item in repositories.values())

    def _states(self, root: Path, control_slug: str, repository_slugs: tuple[str, ...]):
        multica_path = root / "multica-state.json"
        github_path = root / "github-state.json"
        multica_path.write_text(json.dumps({
            "runtime": {
                "id": "11111111-1111-4111-8111-111111111111",
                "daemon_id": "22222222-2222-4222-8222-222222222222",
                "status": "online",
                "metadata": {"capabilities": ["local-worktree-v1"]},
            },
            "skills": {}, "projects": {}, "resources": {}, "agents": {},
            "bindings": {}, "environments": {}, "squads": {}, "members": {},
            "autopilots": {}, "triggers": {}, "counters": {}, "mutation_count": 0,
            "events": [], "rejected_argv": [],
        }))
        github_path.write_text(json.dumps({
            "repositories": {
                **{
                    slug: {"default_branch": "main", "visibility": "private"}
                    for slug in (control_slug,) + repository_slugs
                },
                "openai/superpowers": {
                    "default_branch": "main",
                    "visibility": "public",
                },
            },
            "read_calls": [], "prohibited_events": [],
        }))
        return multica_path, github_path

    def test_single_two_and_three_repository_lifecycles(self):
        selected = os.environ.get("E2E_FIXTURE")
        fixture_names = (selected,) if selected else ("single", "two", "three")
        for fixture_name in fixture_names:
            count = yaml.safe_load((HERE / "fixtures" / fixture_name / "topology.yaml").read_text())["repository_count"]
            with self.subTest(fixture=fixture_name), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                control, repositories, slugs = self._manifest(root, count)
                discovered = discover_repositories(repositories, LocalRepositoryReader())
                self.assertEqual(len(discovered.repositories), count)
                control_slug = f"example/e2e-{count}-control"
                multica_state, github_state = self._states(root, control_slug, slugs)
                environment = {
                    "PATH": f"{HERE / 'fakes'}:{os.environ['PATH']}",
                    "MULTICA_FAKE_STATE": str(multica_state),
                    "GH_FAKE_STATE": str(github_state),
                }
                with patch.dict(os.environ, environment, clear=False):
                    runner = ClosedSubprocessRunner()
                    multica = MulticaClient(
                        runner,
                        runtime_id="11111111-1111-4111-8111-111111111111",
                        daemon_id="22222222-2222-4222-8222-222222222222",
                    )
                    github = GitHubClient(
                        runner,
                        frozenset((control_slug,) + slugs + ("openai/superpowers",)),
                    )
                    provisioner = Provisioner(multica, github)
                    planning = PlanningService(
                        provisioner,
                        version_reader=object(),
                        validator=lambda path, **kwargs: FINDINGS,
                        contract_auditor=lambda *args: AUDIT,
                    )
                    first = planning.create(control, EpochClock(1787836800))
                    self.assertEqual(json.loads(multica_state.read_text())["mutation_count"], 0)
                    PlanStore().write(control / "plan.json", first)
                    applied = ApplyService(
                        planning,
                        provisioner,
                        PlanStore(),
                        EpochClock(1787836800),
                    ).apply(
                        control / "plan.json",
                        first.plan_hash,
                        control / "delivery.yaml",
                        control / "framework.lock",
                        CountingSecretSource(),
                    )
                    self.assertGreater(applied.mutation_count, 0)

                    second = planning.create(control, EpochClock(1787836801))
                    PlanStore().write(control / "plan.json", second)
                    repeated = ApplyService(
                        planning,
                        provisioner,
                        PlanStore(),
                        EpochClock(1787836801),
                    ).apply(
                        control / "plan.json",
                        second.plan_hash,
                        control / "delivery.yaml",
                        control / "framework.lock",
                        CountingSecretSource(),
                    )
                    self.assertEqual(repeated.mutation_count, 0)

                    doctor = DoctorService(
                        lambda path: FINDINGS,
                        lambda path: AUDIT,
                        planning,
                    ).diagnose(control)
                    self.assertTrue(doctor.healthy)
                    upgrade = UpgradeService(validator=lambda path, **kwargs: FINDINGS).create(
                        control,
                        EpochClock(1787836802),
                    )
                    self.assertEqual(upgrade.body.mode, "upgrade")

                multica_record = json.loads(multica_state.read_text())
                github_record = json.loads(github_state.read_text())
                self.assertEqual(multica_record["rejected_argv"], [])
                self.assertEqual(github_record["prohibited_events"], [])
                self.assertFalse(any("deploy" in event for event in multica_record["events"]))


if __name__ == "__main__":
    unittest.main()

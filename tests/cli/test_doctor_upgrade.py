from datetime import datetime, timezone
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from multica_delivery.cli.apply import ApplyService
from multica_delivery.cli.doctor import DoctorService
from multica_delivery.cli.errors import CliError, ExitCode
from multica_delivery.cli.plan import PlanObservation, PlanStore
from multica_delivery.cli.upgrade import (
    _MIGRATION_EDGES,
    MigrationExecutor,
    UpgradeService,
)
from multica_delivery.cli.commands.doctor import run_doctor
from multica_delivery.cli.commands.upgrade import run_upgrade
from multica_delivery.cli.validation import ValidationFinding, ValidationReport
from multica_delivery.core.contract_audit import ContractAuditEntry, ContractAuditReport
from multica_delivery.core.manifest import load_lock
from tests.cli.test_apply import CountingSecretSource, EpochClock, FakeProvisioner
from tests.cli.test_plan import LOCK_TEXT, MANIFEST


class RecordingValidator:
    def __init__(self, valid: bool = True) -> None:
        self.valid = valid
        self.calls: list[Path] = []
        self.keyword_calls: list[dict[str, object]] = []
        self.mutations: list[object] = []

    def __call__(self, path: Path, **kwargs) -> ValidationReport:
        self.calls.append(path)
        self.keyword_calls.append(kwargs)
        severity = "pass" if self.valid else "fail"
        return ValidationReport((ValidationFinding(severity, "local", "local validation"),))


class RecordingAudit:
    def __init__(self, status: str = "pass") -> None:
        self.status = status
        self.calls: list[Path] = []
        self.mutations: list[object] = []

    def __call__(self, path: Path) -> ContractAuditReport:
        self.calls.append(path)
        return ContractAuditReport((ContractAuditEntry("contracts", self.status, "audit"),))


class RecordingPlanning:
    def __init__(self, actions=()) -> None:
        self.actions = actions
        self.calls: list[Path] = []
        self.mutations: list[object] = []
        self.secret_reads: list[object] = []

    def observe(self, path: Path) -> PlanObservation:
        self.calls.append(path)
        return PlanObservation("sample", "a" * 64, "b" * 64, "c" * 64, self.actions)


class DoctorUpgradeTests(unittest.TestCase):
    def _control(self, root: Path, lock_text: str = LOCK_TEXT) -> Path:
        control = root / "delivery-control"
        control.mkdir()
        shutil.copyfile(MANIFEST, control / "delivery.yaml")
        (control / "framework.lock").write_text(lock_text)
        return control

    @staticmethod
    def _version_one_lock() -> str:
        return (
            LOCK_TEXT.replace("skill_version: ''", "skill_version: 0.1.0")
            .replace("engine_version: ''", "engine_version: 0.1.0")
            .replace(
                "supported_multica_cli: ''",
                "supported_multica_cli: '>=0.4,<0.5'",
            )
            .replace("manifest_digest: ''", "manifest_digest: legacy")
        )

    def test_doctor_uses_only_read_only_diagnostics(self):
        validator = RecordingValidator()
        audit = RecordingAudit()
        planning = RecordingPlanning()
        doctor = DoctorService(validator, audit, planning)

        report = doctor.diagnose(Path("/control"))

        self.assertTrue(report.healthy)
        self.assertEqual({finding.status for finding in report.findings}, {"pass"})
        self.assertEqual(validator.calls, [Path("/control")])
        self.assertEqual(audit.calls, [Path("/control")])
        self.assertEqual(planning.calls, [Path("/control")])
        self.assertEqual(validator.mutations + audit.mutations + planning.mutations, [])
        self.assertEqual(planning.secret_reads, [])

    def test_doctor_reports_warn_fail_and_human_block_with_highest_exit(self):
        validator = RecordingValidator(valid=False)
        audit = RecordingAudit(status="warn")

        class BlockedPlanning(RecordingPlanning):
            def observe(self, path: Path):
                self.calls.append(path)
                raise CliError("plan.foreign_state", "foreign state", ExitCode.HUMAN_BLOCK)

        report = DoctorService(validator, audit, BlockedPlanning()).diagnose(Path("/control"))

        self.assertIn("warn", {finding.status for finding in report.findings})
        self.assertIn("fail", {finding.status for finding in report.findings})
        self.assertIn("human-block", {finding.status for finding in report.findings})
        self.assertEqual(report.exit_code, ExitCode.HUMAN_BLOCK)

    def test_upgrade_plans_only_supported_edge_and_current_noop(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = self._control(root, self._version_one_lock())
            validator = RecordingValidator()
            service = UpgradeService(validator=validator)

            migration = service.create(control, EpochClock(1787836800))

            self.assertEqual(migration.body.mode, "upgrade")
            self.assertEqual(
                migration.body.actions[0].kind,
                "framework.version",
            )
            self.assertEqual(migration.body.actions[0].key, "0.1.0->0.2.0")
            self.assertEqual(
                validator.keyword_calls,
                [
                    {
                        "version_reader": None,
                        "platform_name": None,
                        "python_version": None,
                        "workflow_metadata_versions": frozenset({1, 2}),
                    }
                ],
            )
            self.assertEqual(
                migration.body.actions[0].changed_fields,
                (
                    "skill_version",
                    "engine_version",
                    "workflow_metadata_version",
                    "supported_multica_cli",
                    "manifest_digest",
                ),
            )
            self.assertNotIn("manifest_schema_version", migration.body.actions[0].changed_fields)
            self.assertEqual({action.kind for action in migration.body.actions}, {"framework.version"})
            serialized = migration.to_json().lower()
            for prohibited in (
                "issue",
                "pull_request_sha",
                "merge",
                "push",
                "tag",
                "release",
                "deploy",
            ):
                self.assertNotIn(prohibited, serialized)

            executor = MigrationExecutor()
            migrated = executor.apply(migration.body, control / "framework.lock")
            self.assertEqual(migrated.workflow_metadata_version, 2)
            (control / "framework.lock").write_bytes(executor.serialize(migrated))
            current = service.create(control, EpochClock(1787836801))
            self.assertEqual(current.body.actions, ())

    def test_upgrade_rejects_unknown_and_skipped_versions(self):
        versions = ("0.0.0", "0.0.1", "9.0.0")
        for version in versions:
            with self.subTest(version=version), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                lock = LOCK_TEXT.replace("skill_version: ''", f"skill_version: {version}")
                lock = lock.replace("engine_version: ''", f"engine_version: {version}")
                lock = lock.replace("supported_multica_cli: ''", "supported_multica_cli: '>=0.4,<0.5'")
                lock = lock.replace("manifest_digest: ''", "manifest_digest: abcdef")
                control = self._control(root, lock)

                with self.assertRaises(CliError) as caught:
                    UpgradeService(validator=RecordingValidator()).create(
                        control,
                        EpochClock(1787836800),
                    )

                self.assertEqual(caught.exception.exit_code, ExitCode.HUMAN_BLOCK)

    def test_upgrade_apply_uses_common_hash_authorization_and_never_provisioner(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = self._control(root, self._version_one_lock())
            upgrade = UpgradeService(validator=RecordingValidator())
            plan = upgrade.create(control, EpochClock(1787836800))
            PlanStore().write(control / "plan.json", plan)
            migration = MigrationExecutor()
            provisioner = FakeProvisioner()
            secrets = CountingSecretSource()
            apply_service = ApplyService(
                planning=RecordingPlanning(),
                provisioner=provisioner,
                plan_store=PlanStore(),
                clock=EpochClock(1787836800),
                migration_executor=migration,
            )

            result = apply_service.apply(
                control / "plan.json",
                plan.plan_hash,
                control / "delivery.yaml",
                control / "framework.lock",
                secrets,
            )

            lock = load_lock(control / "framework.lock")
            self.assertEqual((lock.skill_version, lock.engine_version), ("0.2.0", "0.2.0"))
            self.assertEqual(lock.workflow_metadata_version, 2)
            self.assertEqual(result.actions, plan.body.actions)
            self.assertEqual(provisioner.calls, [])
            self.assertEqual(secrets.reads, [])

    def test_version_one_to_version_two_is_an_explicit_closed_migration_edge(self):
        self.assertIn(("0.1.0", "0.2.0"), _MIGRATION_EDGES)

    def test_doctor_and_upgrade_commands_return_envelopes(self):
        doctor = DoctorService(RecordingValidator(), RecordingAudit(), RecordingPlanning())
        doctor_envelope = run_doctor(
            SimpleNamespace(path=Path("/control")),
            SimpleNamespace(doctor=doctor),
        )
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = self._control(root, self._version_one_lock())
            upgrade = UpgradeService(validator=RecordingValidator())
            plan_path = control / "plan.json"
            upgrade_envelope = run_upgrade(
                SimpleNamespace(path=control, plan_path=plan_path),
                SimpleNamespace(upgrade=upgrade, clock=EpochClock(1787836800), plan_store=PlanStore()),
            )

            self.assertEqual(doctor_envelope.command, "doctor")
            self.assertEqual(upgrade_envelope.command, "upgrade")
            self.assertTrue(plan_path.exists())


if __name__ == "__main__":
    unittest.main()

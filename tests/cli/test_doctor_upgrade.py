from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from multica_delivery.cli.apply import ApplyService
from multica_delivery.cli.doctor import DoctorService
from multica_delivery.cli.errors import CliError, ExitCode
from multica_delivery.cli.plan import (
    PlanAction,
    PlanObservation,
    PlanStore,
    PlanningService,
    action_reason,
    lock_digest,
)
from multica_delivery.cli.upgrade import (
    _MIGRATION_EDGES,
    MigrationExecutor,
    UpgradeService,
)
from multica_delivery.cli.commands.doctor import run_doctor
from multica_delivery.cli.commands.upgrade import run_upgrade
from multica_delivery.cli.validation import ValidationFinding, ValidationReport
from multica_delivery.core.contract_audit import ContractAuditEntry, ContractAuditReport
from multica_delivery.core.manifest import load_lock, load_manifest, manifest_digest
from multica_delivery.core.provision import Provisioner, ReconcileAction, ReconcileResult
from tests.cli.test_apply import CountingSecretSource, EpochClock
from tests.cli.test_plan import (
    LOCK_TEXT,
    MANIFEST,
    VersionReader,
    passing_audit,
    passing_validation,
)
from tests.core.test_provision import FakeGitHub, StatefulMultica


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


class RecordingUpgradePlanning:
    def __init__(self, actions: tuple[PlanAction, ...] | None = None) -> None:
        self.calls: list[tuple[object, object]] = []
        self.actions = actions

    def observe_reconciliation(self, manifest, lock) -> PlanObservation:
        self.calls.append((manifest, lock))
        actions = self.actions
        if actions is None:
            actions = tuple(
                PlanAction(kind, key, fields, action_reason(kind, key, fields))
                for kind, key, fields in (
                    ("agent.update", "delivery-lead", ("instructions",)),
                    ("lock.update", "framework", ()),
                )
            )
        return PlanObservation(
            manifest.instance.key,
            manifest_digest(manifest),
            lock_digest(lock),
            "d" * 64,
            actions,
        )


class ConvergedUpgradeProvisioner:
    def __init__(self, candidate, reconciled, verification_actions=()) -> None:
        self.candidate = candidate
        self.reconciled = reconciled
        self.verification_actions = verification_actions
        self.calls: list[tuple[object, object]] = []

    def reconcile(self, manifest, lock, **kwargs):
        self.calls.append(((manifest, lock), kwargs))
        expected_lock = self.candidate if kwargs["apply"] else self.reconciled
        if lock != expected_lock:
            raise AssertionError("upgrade did not provision against the candidate v2 lock")
        if kwargs["apply"] and any(
            action.kind == "framework.version"
            for action in kwargs["expected_actions"]
        ):
            raise AssertionError("local framework transition reached remote provisioning")
        actions = () if kwargs["apply"] else self.verification_actions
        return ReconcileResult(actions, (), self.reconciled, "e" * 64)


class LockWatchingMultica(StatefulMultica):
    def __init__(self, manifest, lock_path: Path) -> None:
        super().__init__(manifest)
        self.lock_path = lock_path
        self.versions_during_mutation: list[str] = []

    def _mutate(self, kind: str) -> bool:
        self.versions_during_mutation.append(
            load_lock(self.lock_path).skill_version
        )
        return super()._mutate(kind)


class DoctorUpgradeTests(unittest.TestCase):
    def _control(self, root: Path, lock_text: str = LOCK_TEXT) -> Path:
        control = root / "delivery-control"
        control.mkdir()
        shutil.copyfile(MANIFEST, control / "delivery.yaml")
        (control / "framework.lock").write_text(lock_text)
        return control

    @staticmethod
    def _initialized_lock(release: str, workflow_metadata_version: int) -> str:
        return (
            LOCK_TEXT.replace("skill_version: ''", f"skill_version: {release}")
            .replace("engine_version: ''", f"engine_version: {release}")
            .replace(
                "workflow_metadata_version: 1",
                f"workflow_metadata_version: {workflow_metadata_version}",
            )
            .replace(
                "supported_multica_cli: ''",
                "supported_multica_cli: '>=0.4,<0.5'",
            )
            .replace(
                "manifest_digest: ''",
                f"manifest_digest: {manifest_digest(load_manifest(MANIFEST))}",
            )
        )

    @classmethod
    def _version_one_lock(cls) -> str:
        return cls._initialized_lock("0.1.0", 1)

    @staticmethod
    def _complete_resource_ids(manifest) -> dict[str, dict[str, str]]:
        repositories = tuple(sorted(manifest.repositories))
        agents = (
            "delivery-lead",
            "independent-reviewer",
            "integration-qa",
            "workflow-watcher",
        ) + tuple(f"{repository}-engineer" for repository in repositories)
        return {
            "skill": {key: f"skill-{key}" for key in sorted(manifest.skill_registry)},
            "project": {
                key: f"project-{key}" for key in ("control",) + repositories
            },
            "worktree": {key: f"worktree-{key}" for key in repositories},
            "agent": {key: f"agent-{key}" for key in agents},
            "squad": {"delivery": "squad-delivery"},
            "autopilot": {"workflow-watcher": "autopilot-watcher"},
            "trigger": {"workflow-watcher": "trigger-watcher"},
        }

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
            service = UpgradeService(
                validator=validator,
                planning=RecordingUpgradePlanning(()),
            )

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

            current_control = root / "current-control"
            current_control.mkdir()
            shutil.copyfile(MANIFEST, current_control / "delivery.yaml")
            (current_control / "framework.lock").write_text(
                self._initialized_lock("0.2.0", 2)
            )
            current = service.create(current_control, EpochClock(1787836801))
            self.assertEqual(current.body.actions, ())

    def test_upgrade_plans_complete_candidate_v2_reconciliation_before_version_transition(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = self._control(root, self._version_one_lock())
            manifest = load_manifest(control / "delivery.yaml")

            multica = StatefulMultica(manifest)
            planning = PlanningService(
                Provisioner(multica, FakeGitHub(manifest)),
                version_reader=VersionReader(),
                validator=passing_validation,
                contract_auditor=passing_audit,
            )
            service = UpgradeService(validator=RecordingValidator())
            service.planning = planning

            plan = service.create(control, EpochClock(1787836800))

            self.assertGreater(len(plan.body.actions), 2)
            self.assertEqual(plan.body.actions[-1].kind, "framework.version")
            self.assertEqual(plan.body.actions[-1].key, "0.1.0->0.2.0")
            self.assertEqual(plan.body.actions[-2].kind, "lock.update")
            self.assertIn("agent.create", {action.kind for action in plan.body.actions[:-1]})
            self.assertEqual(multica.mutations, [])

    def test_upgrade_candidate_preserves_ids_and_authenticates_remote_actions_and_fingerprint(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source_text = self._version_one_lock().replace(
                "resource_ids: {}",
                "resource_ids:\n  project:\n    control: legacy-control-id",
            )
            control = self._control(root, source_text)
            source = load_lock(control / "framework.lock")
            planning = RecordingUpgradePlanning()
            service = UpgradeService(validator=RecordingValidator())
            service.planning = planning

            plan = service.create(control, EpochClock(1787836800))

            self.assertEqual(len(planning.calls), 1)
            candidate = planning.calls[0][1]
            self.assertEqual(candidate.resource_ids, source.resource_ids)
            self.assertEqual(candidate.skill_version, "0.2.0")
            self.assertEqual(candidate.engine_version, "0.2.0")
            self.assertEqual(candidate.workflow_metadata_version, 2)
            self.assertEqual(candidate.manifest_digest, plan.body.manifest_digest)
            self.assertEqual(plan.body.lock_digest, lock_digest(source))
            self.assertEqual(plan.body.state_fingerprint, "d" * 64)
            self.assertEqual(
                tuple(action.kind for action in plan.body.actions),
                ("agent.update", "lock.update", "framework.version"),
            )

    def test_upgrade_accepts_only_exact_release_and_workflow_metadata_pairs(self):
        cases = (
            ("0.1.0", 1, 1),
            ("0.2.0", 2, 0),
            ("0.1.0", 2, None),
            ("0.2.0", 1, None),
        )
        for release, workflow_metadata_version, action_count in cases:
            with (
                self.subTest(
                    release=release,
                    workflow_metadata_version=workflow_metadata_version,
                ),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                control = self._control(
                    root,
                    self._initialized_lock(release, workflow_metadata_version),
                )
                service = UpgradeService(
                    validator=RecordingValidator(),
                    planning=RecordingUpgradePlanning(()),
                )

                if action_count is not None:
                    plan = service.create(control, EpochClock(1787836800))
                    self.assertEqual(len(plan.body.actions), action_count)
                    continue

                with self.assertRaises(CliError) as caught:
                    service.create(control, EpochClock(1787836800))
                self.assertEqual(
                    caught.exception.code,
                    "upgrade.incompatible_release_metadata",
                )
                self.assertEqual(caught.exception.exit_code, ExitCode.VALIDATION)
                self.assertIn(
                    "release and workflow metadata",
                    caught.exception.safe_message,
                )

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
                    UpgradeService(
                        validator=RecordingValidator(),
                        planning=RecordingUpgradePlanning(()),
                    ).create(
                        control,
                        EpochClock(1787836800),
                    )

                self.assertEqual(caught.exception.exit_code, ExitCode.HUMAN_BLOCK)

    def test_upgrade_rejects_nonexact_v1_lock_before_remote_observation(self):
        exact = self._version_one_lock()
        exact_digest = manifest_digest(load_manifest(MANIFEST))
        cases = {
            "missing-manifest-digest": exact.replace(
                f"manifest_digest: {exact_digest}",
                "manifest_digest: ''",
            ),
            "wrong-manifest-digest": exact.replace(
                f"manifest_digest: {exact_digest}",
                "manifest_digest: wrong",
            ),
            "wrong-cli-contract": exact.replace(
                "supported_multica_cli: '>=0.4,<0.5'",
                "supported_multica_cli: '>=9,<10'",
            ),
            "wrong-manifest-schema": exact.replace(
                "manifest_schema_version: 1",
                "manifest_schema_version: 2",
            ),
        }
        for label, lock_text in cases.items():
            with self.subTest(label=label), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                control = self._control(root, lock_text)
                planning = RecordingUpgradePlanning(())

                with self.assertRaises(CliError) as caught:
                    UpgradeService(
                        validator=RecordingValidator(),
                        planning=planning,
                    ).create(control, EpochClock(1787836800))

                self.assertEqual(caught.exception.exit_code, ExitCode.VALIDATION)
                self.assertEqual(planning.calls, [])

    def test_upgrade_apply_reconciles_candidate_v2_before_persisting_current_lock(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = self._control(root, self._version_one_lock())
            upgrade = UpgradeService(
                validator=RecordingValidator(),
                planning=RecordingUpgradePlanning(()),
            )
            plan = upgrade.create(control, EpochClock(1787836800))
            PlanStore().write(control / "plan.json", plan)
            migration = MigrationExecutor(upgrade.planning)
            candidate = upgrade.planning.calls[0][1]
            manifest = load_manifest(control / "delivery.yaml")
            reconciled_lock = replace(
                candidate,
                resource_ids=self._complete_resource_ids(manifest),
            )
            provisioner = ConvergedUpgradeProvisioner(candidate, reconciled_lock)
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
            self.assertEqual(
                lock.resource_ids.get("project", {}).get("control"),
                "project-control",
            )
            self.assertEqual(result.actions, plan.body.actions)
            self.assertEqual(len(provisioner.calls), 2)
            self.assertEqual(secrets.reads, [])

    def test_upgrade_apply_rejects_incomplete_returned_v2_identity_lock(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = self._control(root, self._version_one_lock())
            original_lock = (control / "framework.lock").read_bytes()
            upgrade = UpgradeService(
                validator=RecordingValidator(),
                planning=RecordingUpgradePlanning(()),
            )
            plan = upgrade.create(control, EpochClock(1787836800))
            PlanStore().write(control / "plan.json", plan)
            candidate = upgrade.planning.calls[0][1]
            incomplete = replace(
                candidate,
                resource_ids={"project": {"control": "project-control"}},
            )
            provisioner = ConvergedUpgradeProvisioner(
                candidate,
                incomplete,
                (ReconcileAction("lock.update", "framework"),),
            )

            with self.assertRaises(CliError) as caught:
                ApplyService(
                    planning=RecordingPlanning(),
                    provisioner=provisioner,
                    plan_store=PlanStore(),
                    clock=EpochClock(1787836800),
                    migration_executor=MigrationExecutor(upgrade.planning),
                ).apply(
                    control / "plan.json",
                    plan.plan_hash,
                    control / "delivery.yaml",
                    control / "framework.lock",
                    CountingSecretSource(),
                )

            self.assertEqual(caught.exception.exit_code, ExitCode.HUMAN_BLOCK)
            self.assertEqual((control / "framework.lock").read_bytes(), original_lock)

    def test_upgrade_apply_rejects_remote_action_acknowledgement_mismatch_before_lock(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = self._control(root, self._version_one_lock())
            original_lock = (control / "framework.lock").read_bytes()
            upgrade = UpgradeService(
                validator=RecordingValidator(),
                planning=RecordingUpgradePlanning(),
            )
            plan = upgrade.create(control, EpochClock(1787836800))
            PlanStore().write(control / "plan.json", plan)
            candidate = upgrade.planning.calls[0][1]
            manifest = load_manifest(control / "delivery.yaml")
            complete = replace(
                candidate,
                resource_ids=self._complete_resource_ids(manifest),
            )
            provisioner = ConvergedUpgradeProvisioner(candidate, complete)

            with self.assertRaises(CliError) as caught:
                ApplyService(
                    planning=RecordingPlanning(),
                    provisioner=provisioner,
                    plan_store=PlanStore(),
                    clock=EpochClock(1787836800),
                    migration_executor=MigrationExecutor(upgrade.planning),
                ).apply(
                    control / "plan.json",
                    plan.plan_hash,
                    control / "delivery.yaml",
                    control / "framework.lock",
                    CountingSecretSource(),
                )

            self.assertEqual(caught.exception.code, "apply.action_mismatch")
            self.assertEqual((control / "framework.lock").read_bytes(), original_lock)

    def test_real_upgrade_apply_converges_remote_before_lock_and_current_retry_is_noop(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = self._control(root, self._version_one_lock())
            manifest = load_manifest(control / "delivery.yaml")
            multica = LockWatchingMultica(manifest, control / "framework.lock")
            provisioner = Provisioner(multica, FakeGitHub(manifest))
            planning = PlanningService(
                provisioner,
                version_reader=VersionReader(),
                validator=passing_validation,
                contract_auditor=passing_audit,
            )
            upgrade = UpgradeService(
                validator=RecordingValidator(),
                planning=planning,
            )
            plan = upgrade.create(control, EpochClock(1787836800))
            self.assertEqual(plan.body.actions[-1].kind, "framework.version")
            PlanStore().write(control / "plan.json", plan)

            result = ApplyService(
                planning,
                provisioner,
                PlanStore(),
                EpochClock(1787836800),
                migration_executor=MigrationExecutor(planning),
            ).apply(
                control / "plan.json",
                plan.plan_hash,
                control / "delivery.yaml",
                control / "framework.lock",
                CountingSecretSource(),
            )

            lock = load_lock(control / "framework.lock")
            self.assertEqual(result.actions, plan.body.actions)
            self.assertEqual(lock.skill_version, "0.2.0")
            self.assertEqual(lock.workflow_metadata_version, 2)
            self.assertEqual(
                {kind: set(values) for kind, values in lock.resource_ids.items()},
                {
                    kind: set(values)
                    for kind, values in self._complete_resource_ids(manifest).items()
                },
            )
            self.assertGreater(len(multica.mutations), 0)
            self.assertEqual(set(multica.versions_during_mutation), {"0.1.0"})

            mutation_count = len(multica.mutations)
            current = upgrade.create(control, EpochClock(1787836801))
            self.assertEqual(current.body.actions, ())
            PlanStore().write(control / "plan.json", current)
            repeated = ApplyService(
                planning,
                provisioner,
                PlanStore(),
                EpochClock(1787836801),
                migration_executor=MigrationExecutor(planning),
            ).apply(
                control / "plan.json",
                current.plan_hash,
                control / "delivery.yaml",
                control / "framework.lock",
                CountingSecretSource(),
            )
            self.assertEqual(repeated.mutation_count, 0)
            self.assertEqual(len(multica.mutations), mutation_count)

    def test_upgrade_provision_failure_leaves_v1_lock_for_fresh_replan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = self._control(root, self._version_one_lock())
            manifest = load_manifest(control / "delivery.yaml")
            multica = StatefulMultica(manifest)
            provisioner = Provisioner(multica, FakeGitHub(manifest))
            planning = PlanningService(
                provisioner,
                version_reader=VersionReader(),
                validator=passing_validation,
                contract_auditor=passing_audit,
            )
            upgrade = UpgradeService(
                validator=RecordingValidator(),
                planning=planning,
            )
            plan = upgrade.create(control, EpochClock(1787836800))
            PlanStore().write(control / "plan.json", plan)
            original_lock = (control / "framework.lock").read_bytes()
            multica.freeze_mutations.add("skill.import")

            with self.assertRaises(CliError) as caught:
                ApplyService(
                    planning,
                    provisioner,
                    PlanStore(),
                    EpochClock(1787836800),
                    migration_executor=MigrationExecutor(planning),
                ).apply(
                    control / "plan.json",
                    plan.plan_hash,
                    control / "delivery.yaml",
                    control / "framework.lock",
                    CountingSecretSource(),
                )

            self.assertEqual(caught.exception.exit_code, ExitCode.HUMAN_BLOCK)
            self.assertEqual((control / "framework.lock").read_bytes(), original_lock)
            self.assertEqual(load_lock(control / "framework.lock").skill_version, "0.1.0")

    def test_partial_upgrade_failure_replans_remaining_remote_work_before_lock_transition(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = self._control(root, self._version_one_lock())
            manifest = load_manifest(control / "delivery.yaml")
            multica = StatefulMultica(manifest)
            provisioner = Provisioner(multica, FakeGitHub(manifest))
            planning = PlanningService(
                provisioner,
                version_reader=VersionReader(),
                validator=passing_validation,
                contract_auditor=passing_audit,
            )
            upgrade = UpgradeService(
                validator=RecordingValidator(),
                planning=planning,
            )
            first = upgrade.create(control, EpochClock(1787836800))
            PlanStore().write(control / "plan.json", first)
            multica.freeze_mutations.add("agent.create")

            with self.assertRaises(CliError):
                ApplyService(
                    planning,
                    provisioner,
                    PlanStore(),
                    EpochClock(1787836800),
                    migration_executor=MigrationExecutor(planning),
                ).apply(
                    control / "plan.json",
                    first.plan_hash,
                    control / "delivery.yaml",
                    control / "framework.lock",
                    CountingSecretSource(),
                )

            self.assertEqual(load_lock(control / "framework.lock").skill_version, "0.1.0")
            self.assertGreater(len(multica.skills), 0)
            multica.freeze_mutations.clear()
            fresh = upgrade.create(control, EpochClock(1787836801))
            self.assertNotEqual(fresh.plan_hash, first.plan_hash)
            self.assertLess(len(fresh.body.actions), len(first.body.actions))
            PlanStore().write(control / "plan.json", fresh)

            ApplyService(
                planning,
                provisioner,
                PlanStore(),
                EpochClock(1787836801),
                migration_executor=MigrationExecutor(planning),
            ).apply(
                control / "plan.json",
                fresh.plan_hash,
                control / "delivery.yaml",
                control / "framework.lock",
                CountingSecretSource(),
            )

            self.assertEqual(load_lock(control / "framework.lock").skill_version, "0.2.0")

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
            upgrade = UpgradeService(
                validator=RecordingValidator(),
                planning=RecordingUpgradePlanning(()),
            )
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

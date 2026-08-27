from datetime import datetime, timezone
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from multica_delivery.cli.apply import ApplyService
from multica_delivery.cli.errors import CliError, ExitCode
from multica_delivery.cli.plan import (
    PlanAction,
    PlanBody,
    PlanEnvelope,
    PlanObservation,
    PlanStore,
    PlanningService,
)
from multica_delivery.cli.secrets import EnvironmentSecretSource
from multica_delivery.cli.commands.apply import run_apply
from multica_delivery.core.manifest import load_lock, load_manifest
from multica_delivery.core.provision import Provisioner
from tests.cli.test_plan import (
    LOCK_TEXT,
    MANIFEST,
    VersionReader,
    passing_audit,
    passing_validation,
)
from tests.core.test_provision import FakeGitHub, StatefulMultica


class EpochClock:
    def __init__(self, epoch: int) -> None:
        self.epoch = epoch

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.epoch, timezone.utc)


class CountingSecretSource:
    def __init__(self, value: str = "database-secret") -> None:
        self.value = value
        self.reads: list[str] = []

    def read(self, name: str) -> str:
        self.reads.append(name)
        return self.value


class FakePlanning:
    def __init__(self, observation: PlanObservation) -> None:
        self.observation = observation
        self.calls: list[Path] = []

    def observe(self, path: Path) -> PlanObservation:
        self.calls.append(path)
        return self.observation


class FakeProvisioner:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def reconcile(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("denied apply reached mutation boundary")


class ConfirmationReader:
    def __init__(self, value: str) -> None:
        self.value = value
        self.calls: list[str] = []

    def read(self, expected_hash: str) -> str:
        self.calls.append(expected_hash)
        return self.value


class ApplyTests(unittest.TestCase):
    def _body(self, **overrides) -> PlanBody:
        values = {
            "schema_version": 1,
            "mode": "onboard",
            "cli_version": "0.1.0",
            "instance_key": "sample-commerce",
            "manifest_digest": "a" * 64,
            "lock_digest": "b" * 64,
            "state_fingerprint": "c" * 64,
            "created_at": 1787836800,
            "expires_at": 1787837400,
            "actions": (PlanAction("project.create", "control", ()),),
        }
        values.update(overrides)
        return PlanBody(**values)

    def _write_plan(self, root: Path, body: PlanBody) -> tuple[Path, PlanEnvelope]:
        path = root / "plan.json"
        envelope = PlanEnvelope.create(body)
        PlanStore().write(path, envelope)
        return path, envelope

    def _denied_service(
        self,
        body: PlanBody,
        *,
        observation: PlanObservation | None = None,
        now: int = 1787836800,
        confirmation_reader=None,
    ):
        observed = observation or PlanObservation(
            body.instance_key,
            body.manifest_digest,
            body.lock_digest,
            body.state_fingerprint,
            body.actions,
        )
        planning = FakePlanning(observed)
        provisioner = FakeProvisioner()
        service = ApplyService(
            planning,
            provisioner,
            PlanStore(),
            EpochClock(now),
            confirmation_reader=confirmation_reader,
        )
        return service, planning, provisioner

    def test_denials_happen_before_secret_reads_or_mutations(self):
        base = self._body()
        cases = (
            ("missing", base, None, None, 1787836800, ExitCode.CONFIRMATION),
            ("partial", base, "a" * 12, None, 1787836800, ExitCode.CONFIRMATION),
            ("uppercase", base, "UPPER", None, 1787836800, ExitCode.CONFIRMATION),
            ("wrong", base, "0" * 64, None, 1787836800, ExitCode.CONFIRMATION),
            ("stale", base, "PLAN_HASH", None, 1787837401, ExitCode.CONFIRMATION),
            ("future", base, "PLAN_HASH", None, 1787836799, ExitCode.CONFIRMATION),
            (
                "compatibility",
                self._body(cli_version="0.2.0"),
                "PLAN_HASH",
                None,
                1787836800,
                ExitCode.CONFIRMATION,
            ),
            (
                "manifest-drift",
                base,
                "PLAN_HASH",
                PlanObservation(base.instance_key, "d" * 64, base.lock_digest, base.state_fingerprint, base.actions),
                1787836800,
                ExitCode.DRIFT,
            ),
            (
                "lock-drift",
                base,
                "PLAN_HASH",
                PlanObservation(base.instance_key, base.manifest_digest, "d" * 64, base.state_fingerprint, base.actions),
                1787836800,
                ExitCode.DRIFT,
            ),
            (
                "fingerprint-drift",
                base,
                "PLAN_HASH",
                PlanObservation(base.instance_key, base.manifest_digest, base.lock_digest, "d" * 64, base.actions),
                1787836800,
                ExitCode.DRIFT,
            ),
            (
                "action-drift",
                base,
                "PLAN_HASH",
                PlanObservation(base.instance_key, base.manifest_digest, base.lock_digest, base.state_fingerprint, ()),
                1787836800,
                ExitCode.DRIFT,
            ),
        )
        for label, body, confirmation, observation, now, exit_code in cases:
            with self.subTest(label=label), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                plan_path, envelope = self._write_plan(root, body)
                actual_confirmation = envelope.plan_hash if confirmation == "PLAN_HASH" else confirmation
                service, _, provisioner = self._denied_service(
                    body,
                    observation=observation,
                    now=now,
                )
                secrets = CountingSecretSource()

                with self.assertRaises(CliError) as caught:
                    service.apply(
                        plan_path,
                        actual_confirmation,
                        root / "delivery.yaml",
                        root / "framework.lock",
                        secrets,
                    )

                self.assertEqual(caught.exception.exit_code, exit_code)
                self.assertEqual(secrets.reads, [])
                self.assertEqual(provisioner.calls, [])

    def test_interactive_hash_mismatch_is_denied_after_safe_reobservation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            body = self._body()
            plan_path, envelope = self._write_plan(root, body)
            reader = ConfirmationReader(envelope.plan_hash.upper())
            service, planning, provisioner = self._denied_service(
                body,
                confirmation_reader=reader,
            )
            secrets = CountingSecretSource()

            with self.assertRaises(CliError) as caught:
                service.apply(
                    plan_path,
                    None,
                    root / "delivery.yaml",
                    root / "framework.lock",
                    secrets,
                )

            self.assertEqual(caught.exception.exit_code, ExitCode.CONFIRMATION)
            self.assertEqual(reader.calls, [envelope.plan_hash])
            self.assertEqual(len(planning.calls), 1)
            self.assertEqual(secrets.reads, [])
            self.assertEqual(provisioner.calls, [])

    def _real_control(self, root: Path):
        control = root / "delivery-control"
        control.mkdir()
        shutil.copyfile(MANIFEST, control / "delivery.yaml")
        (control / "framework.lock").write_text(LOCK_TEXT)
        manifest = load_manifest(control / "delivery.yaml")
        multica = StatefulMultica(manifest)
        provisioner = Provisioner(multica, FakeGitHub(manifest))
        planning = PlanningService(
            provisioner,
            version_reader=VersionReader(),
            validator=passing_validation,
            contract_auditor=passing_audit,
        )
        return control, planning, provisioner, multica

    def test_explicit_noninteractive_apply_converges_writes_lock_and_repeat_is_noop(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control, planning, provisioner, multica = self._real_control(root)
            plan_path = control / "plan.json"
            first_plan = planning.create(control, EpochClock(1787836800))
            PlanStore().write(plan_path, first_plan)
            secrets = CountingSecretSource()
            service = ApplyService(planning, provisioner, PlanStore(), EpochClock(1787837400))

            first = service.apply(
                plan_path,
                first_plan.plan_hash,
                control / "delivery.yaml",
                control / "framework.lock",
                secrets,
            )

            self.assertGreater(first.mutation_count, 0)
            self.assertIn("DATABASE_URL", secrets.reads)
            lock = load_lock(control / "framework.lock")
            self.assertTrue(lock.resource_ids)
            self.assertEqual((control / "framework.lock").stat().st_mode & 0o777, 0o600)

            mutations_after_first = len(multica.mutations)
            second_plan = planning.create(control, EpochClock(1787836801))
            PlanStore().write(plan_path, second_plan)
            second_secrets = CountingSecretSource()
            second = ApplyService(
                planning,
                provisioner,
                PlanStore(),
                EpochClock(1787836801),
            ).apply(
                plan_path,
                second_plan.plan_hash,
                control / "delivery.yaml",
                control / "framework.lock",
                second_secrets,
            )

            self.assertEqual(second.mutation_count, 0)
            self.assertEqual(len(multica.mutations), mutations_after_first)
            self.assertEqual(second_secrets.reads, [])

    def test_nonconvergent_acknowledgement_human_blocks_and_preserves_lock(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control, planning, provisioner, multica = self._real_control(root)
            multica.freeze_mutations.add("skill.import")
            original_lock = (control / "framework.lock").read_bytes()
            plan = planning.create(control, EpochClock(1787836800))
            PlanStore().write(control / "plan.json", plan)

            with self.assertRaises(CliError) as caught:
                ApplyService(
                    planning,
                    provisioner,
                    PlanStore(),
                    EpochClock(1787836800),
                ).apply(
                    control / "plan.json",
                    plan.plan_hash,
                    control / "delivery.yaml",
                    control / "framework.lock",
                    CountingSecretSource(),
                )

            self.assertEqual(caught.exception.exit_code, ExitCode.HUMAN_BLOCK)
            self.assertEqual((control / "framework.lock").read_bytes(), original_lock)

    def test_environment_secret_source_returns_only_declared_values(self):
        source = EnvironmentSecretSource(
            {"DATABASE_URL"},
            {"DATABASE_URL": "db-secret", "FOREIGN_SECRET": "do-not-read"},
        )

        self.assertEqual(source.read("DATABASE_URL"), "db-secret")
        with self.assertRaises(CliError):
            source.read("FOREIGN_SECRET")

    def test_apply_command_returns_safe_envelope(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control, planning, provisioner, _ = self._real_control(root)
            plan = planning.create(control, EpochClock(1787836800))
            PlanStore().write(control / "plan.json", plan)
            apply_service = ApplyService(
                planning,
                provisioner,
                PlanStore(),
                EpochClock(1787836800),
            )
            envelope = run_apply(
                SimpleNamespace(
                    plan_path=control / "plan.json",
                    confirmation=plan.plan_hash,
                    manifest_path=control / "delivery.yaml",
                    lock_path=control / "framework.lock",
                ),
                SimpleNamespace(
                    apply_service=apply_service,
                    secret_source=CountingSecretSource(),
                ),
            )

            self.assertEqual(envelope.command, "apply")
            self.assertGreater(envelope.result["mutation_count"], 0)
            self.assertNotIn("secret", envelope.to_json().lower())


if __name__ == "__main__":
    unittest.main()

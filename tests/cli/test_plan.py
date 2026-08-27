from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from multica_delivery.cli.clock import Clock
from multica_delivery.cli.errors import CliError, ExitCode
from multica_delivery.cli.plan import (
    PlanAction,
    PlanBody,
    PlanEnvelope,
    PlanStore,
    PlanningService,
)
from multica_delivery.cli.commands.plan import run_plan
from multica_delivery.cli.validation import ValidationFinding, ValidationReport
from multica_delivery.core.contract_audit import ContractAuditEntry, ContractAuditReport
from multica_delivery.core.manifest import load_manifest
from multica_delivery.core.model import FrameworkLock
from multica_delivery.core.provision import Provisioner, SkillState
from tests.core.test_provision import FakeGitHub, StatefulMultica


MANIFEST = Path(__file__).resolve().parents[1] / "core" / "fixtures" / "three-repository-delivery.yaml"
LOCK_TEXT = """\
skill_version: ''
engine_version: ''
manifest_schema_version: 1
workflow_metadata_version: 1
supported_multica_cli: ''
manifest_digest: ''
resource_ids: {}
"""


class FixedClock:
    def now(self) -> datetime:
        return datetime.fromtimestamp(1787836800, timezone.utc)


class VersionReader:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def version(self, executable: str) -> str | None:
        self.calls.append(executable)
        return f"{executable} test version"


def passing_validation(path, **kwargs):
    return ValidationReport((ValidationFinding("pass", "test.valid", "valid"),))


def passing_audit(multica, github, manifest):
    return ContractAuditReport((ContractAuditEntry("test", "pass", "available"),))


class PlanTests(unittest.TestCase):
    def _control(self, root: Path) -> Path:
        control = root / "delivery-control"
        control.mkdir()
        shutil.copyfile(MANIFEST, control / "delivery.yaml")
        (control / "framework.lock").write_text(LOCK_TEXT)
        return control

    def _planning(self, manifest_path: Path, *, multica=None, auditor=passing_audit):
        manifest = load_manifest(manifest_path)
        state = multica or StatefulMultica(manifest)
        provisioner = Provisioner(state, FakeGitHub(manifest))
        return (
            PlanningService(
                provisioner,
                version_reader=VersionReader(),
                validator=passing_validation,
                contract_auditor=auditor,
            ),
            state,
        )

    def test_plan_body_has_exact_fields_ttl_and_canonical_hash(self):
        action = PlanAction(
            "project.create",
            "control",
            (),
            "Reconcile control with project.create.",
        )
        body = PlanBody(
            schema_version=1,
            mode="onboard",
            cli_version="0.1.0",
            instance_key="sample-commerce",
            manifest_digest="a" * 64,
            lock_digest="b" * 64,
            state_fingerprint="c" * 64,
            created_at=1787836800,
            expires_at=1787837400,
            actions=(action,),
        )
        envelope = PlanEnvelope.create(body)

        self.assertEqual(
            envelope.to_value()["result"]["body"]["actions"][0]["reason"],
            "Reconcile control with project.create.",
        )

        self.assertEqual(
            set(body.to_value()),
            {
                "schema_version",
                "mode",
                "cli_version",
                "instance_key",
                "manifest_digest",
                "lock_digest",
                "state_fingerprint",
                "created_at",
                "expires_at",
                "actions",
            },
        )
        self.assertEqual(body.expires_at, 1787837400)
        self.assertRegex(envelope.plan_hash, r"^[0-9a-f]{64}$")

        reordered = {
            key: body.to_value()[key]
            for key in reversed(tuple(body.to_value()))
        }
        self.assertEqual(PlanEnvelope.create(PlanBody.from_value(reordered)).plan_hash, envelope.plan_hash)
        for changed in (
            replace(body, created_at=1787836799, expires_at=1787837399),
            replace(body, manifest_digest="d" * 64),
            replace(
                body,
                actions=(
                    PlanAction(
                        "project.create",
                        "api",
                        (),
                        "Reconcile api with project.create.",
                    ),
                ),
            ),
        ):
            self.assertNotEqual(PlanEnvelope.create(changed).plan_hash, envelope.plan_hash)

    def test_unknown_fields_and_floating_point_values_fail(self):
        base = {
            "schema_version": 1,
            "mode": "onboard",
            "cli_version": "0.1.0",
            "instance_key": "sample",
            "manifest_digest": "a" * 64,
            "lock_digest": "b" * 64,
            "state_fingerprint": "c" * 64,
            "created_at": 1787836800,
            "expires_at": 1787837400,
            "actions": [],
        }
        unknown = dict(base, extra="forbidden")
        floating = dict(base, created_at=1787836800.0)

        for value in (unknown, floating):
            with self.subTest(value=value), self.assertRaises(CliError):
                PlanBody.from_value(value)

    def test_planning_is_read_only_secret_free_and_writes_private_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = self._control(root)
            planning, multica = self._planning(control / "delivery.yaml")
            store = PlanStore()
            plan_path = control / "plan.json"

            envelope = planning.create(control, FixedClock())
            store.write(plan_path, envelope)

            self.assertEqual(envelope.body.created_at, 1787836800)
            self.assertEqual(envelope.body.expires_at, 1787837400)
            self.assertRegex(envelope.body.state_fingerprint, r"^[0-9a-f]{64}$")
            self.assertEqual(multica.mutations, [])
            self.assertEqual(plan_path.stat().st_mode & 0o777, 0o600)
            loaded = store.load(plan_path)
            self.assertEqual(loaded, envelope)
            self.assertNotIn("SECRET", plan_path.read_text())

    def test_state_drift_returns_exit_four_without_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = self._control(root)
            manifest = load_manifest(control / "delivery.yaml")

            class ChangingMultica(StatefulMultica):
                skill_reads = 0

                def list_skills(self):
                    self.skill_reads += 1
                    if self.skill_reads == 2:
                        self.skills["foreign-skill"] = SkillState(
                            "foreign-skill",
                            "foreign-skill",
                            "https://github.com/example/public/tree/main/skill",
                        )
                    return super().list_skills()

            planning, _ = self._planning(
                control / "delivery.yaml",
                multica=ChangingMultica(manifest),
            )
            plan_path = control / "plan.json"

            with self.assertRaises(CliError) as caught:
                planning.create(control, FixedClock())

            self.assertEqual(caught.exception.exit_code, ExitCode.DRIFT)
            self.assertFalse(plan_path.exists())

    def test_malformed_and_foreign_external_state_are_classified_without_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = self._control(root)
            manifest = load_manifest(control / "delivery.yaml")

            class MalformedMultica(StatefulMultica):
                def list_projects(self):
                    return (object(),)

            malformed, _ = self._planning(
                control / "delivery.yaml",
                multica=MalformedMultica(manifest),
            )
            with self.assertRaises(CliError) as malformed_error:
                malformed.create(control, FixedClock())
            self.assertEqual(malformed_error.exception.exit_code, ExitCode.EXTERNAL)

            def failing_audit(multica, github, loaded):
                return ContractAuditReport(
                    (ContractAuditEntry("github.repository", "fail", "read failed"),)
                )

            foreign, _ = self._planning(
                control / "delivery.yaml",
                auditor=failing_audit,
            )
            with self.assertRaises(CliError) as foreign_error:
                foreign.create(control, FixedClock())
            self.assertIn(foreign_error.exception.exit_code, {ExitCode.EXTERNAL, ExitCode.HUMAN_BLOCK})
            self.assertFalse((control / "plan.json").exists())

    def test_run_plan_writes_only_after_observation_succeeds(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = self._control(root)
            planning, _ = self._planning(control / "delivery.yaml")
            plan_path = control / "plan.json"
            services = SimpleNamespace(planning=planning, clock=FixedClock(), plan_store=PlanStore())

            result = run_plan(
                SimpleNamespace(path=control, plan_path=plan_path, mode="onboard"),
                services,
            )

            self.assertEqual(result.command, "plan")
            self.assertEqual(result.result["plan_hash"], PlanStore().load(plan_path).plan_hash)
            self.assertTrue(plan_path.exists())


if __name__ == "__main__":
    unittest.main()

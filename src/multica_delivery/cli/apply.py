"""Exact-plan authorization and convergent Multica apply."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Protocol

import yaml

from multica_delivery import __version__
from multica_delivery.core.manifest import ManifestError, load_lock, load_manifest, manifest_digest
from multica_delivery.core.model import FrameworkLock
from multica_delivery.core.provision import (
    SUPPORTED_MULTICA_CLI,
    WORKFLOW_METADATA_VERSION,
    ProvisionError,
    ReconcileAction,
)

from .clock import Clock
from .errors import CliError, ExitCode
from .files import atomic_replace_private
from .plan import PlanAction, PlanStore, PlanningService, action_reason, lock_digest
from .secrets import SecretSource


_FULL_HASH = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class ConfirmationReader(Protocol):
    def read(self, expected_hash: str) -> str: ...


class InteractiveConfirmationReader:
    def read(self, expected_hash: str) -> str:
        return input(f"Enter the complete plan hash {expected_hash}: ")


@dataclass(frozen=True)
class ApplyResult:
    actions: tuple[PlanAction, ...]
    state_fingerprint: str

    @property
    def mutation_count(self) -> int:
        return sum(action.kind != "lock.update" for action in self.actions)

    def to_value(self) -> dict[str, object]:
        return {
            "mutation_count": self.mutation_count,
            "actions": [action.to_value() for action in self.actions],
            "state_fingerprint": self.state_fingerprint,
        }


def _compatibility_line(version: str) -> tuple[int, int] | None:
    match = _VERSION.fullmatch(version)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _lock_bytes(lock: FrameworkLock) -> bytes:
    value = {
        "skill_version": lock.skill_version,
        "engine_version": lock.engine_version,
        "manifest_schema_version": lock.manifest_schema_version,
        "workflow_metadata_version": lock.workflow_metadata_version,
        "supported_multica_cli": lock.supported_multica_cli,
        "manifest_digest": lock.manifest_digest,
        "resource_ids": {
            kind: dict(sorted(identities.items()))
            for kind, identities in sorted(lock.resource_ids.items())
        },
    }
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True).encode("utf-8")


def _provision_failure(error: ProvisionError) -> CliError:
    message = str(error)
    if "preconditions changed" in message:
        exit_code = ExitCode.DRIFT
        code = "apply.precondition_drift"
    elif (
        "did not converge" in message
        or "identity changed" in message
        or message == "provisioning mutation failed"
        or "reconciliation failed" in message
        or "foreign" in message
        or "duplicate" in message
    ):
        exit_code = ExitCode.HUMAN_BLOCK
        code = "apply.human_block"
    else:
        exit_code = ExitCode.EXTERNAL
        code = "apply.external_failure"
    return CliError(
        code,
        "Apply could not establish converged authoritative state",
        exit_code,
    )


def _plan_actions(actions: tuple[ReconcileAction, ...]) -> tuple[PlanAction, ...]:
    return tuple(
        PlanAction(
            action.kind,
            action.key,
            action.changed_fields,
            action_reason(action.kind, action.key, action.changed_fields),
        )
        for action in actions
    )


class ApplyService:
    def __init__(
        self,
        planning: PlanningService,
        provisioner: object,
        plan_store: PlanStore,
        clock: Clock,
        *,
        confirmation_reader: ConfirmationReader | None = None,
        migration_executor: object | None = None,
    ) -> None:
        self.planning = planning
        self.provisioner = provisioner
        self.plan_store = plan_store
        self.clock = clock
        self.confirmation_reader = confirmation_reader
        self.migration_executor = migration_executor

    @staticmethod
    def _validate_confirmation(value: str | None, expected: str) -> None:
        if type(value) is not str or _FULL_HASH.fullmatch(value) is None or value != expected:
            raise CliError(
                "apply.confirmation_mismatch",
                "Apply requires the complete exact lowercase plan hash",
                ExitCode.CONFIRMATION,
            )

    def apply(
        self,
        plan_path: Path,
        confirmation: str | None,
        manifest_path: Path,
        lock_path: Path,
        secret_source: SecretSource,
    ) -> ApplyResult:
        try:
            approved = self.plan_store.load(plan_path)
        except CliError:
            raise CliError(
                "apply.invalid_plan",
                "Apply requires a valid authenticated plan file",
                ExitCode.CONFIRMATION,
            ) from None
        body = approved.body
        if body.mode not in {"onboard", "upgrade"}:
            raise CliError(
                "apply.unsupported_mode",
                "This apply path does not support the plan mode",
                ExitCode.HUMAN_BLOCK,
            )
        if _compatibility_line(body.cli_version) != _compatibility_line(__version__):
            raise CliError(
                "apply.incompatible_cli",
                "The plan was created by a different CLI compatibility line",
                ExitCode.CONFIRMATION,
            )
        now_value = self.clock.now()
        if now_value.tzinfo is None or now_value.utcoffset() is None:
            raise CliError(
                "apply.invalid_clock",
                "Apply requires a timezone-aware clock",
                ExitCode.CONFIRMATION,
            )
        now = int(now_value.timestamp())
        age = now - body.created_at
        if age < 0 or age > 600 or now > body.expires_at:
            raise CliError(
                "apply.expired_plan",
                "The approved plan is expired or from the future",
                ExitCode.CONFIRMATION,
            )

        if confirmation is not None:
            self._validate_confirmation(confirmation, approved.plan_hash)

        manifest_file = Path(manifest_path)
        lock_file = Path(lock_path)
        control_path = manifest_file.parent
        if (
            manifest_file != control_path / "delivery.yaml"
            or lock_file != control_path / "framework.lock"
            or Path(plan_path).parent != control_path
        ):
            raise CliError(
                "apply.path_scope",
                "Plan, manifest, and lock must share one delivery-control directory",
                ExitCode.VALIDATION,
            )

        if body.mode == "upgrade":
            if self.migration_executor is None:
                raise CliError(
                    "apply.migration_unavailable",
                    "Upgrade apply requires the exact migration executor",
                    ExitCode.HUMAN_BLOCK,
                )
            observed = self.migration_executor.observe(control_path)
        else:
            observed = self.planning.observe(control_path)
        if (
            observed.instance_key != body.instance_key
            or observed.manifest_digest != body.manifest_digest
            or observed.lock_digest != body.lock_digest
            or observed.state_fingerprint != body.state_fingerprint
            or observed.actions != body.actions
        ):
            raise CliError(
                "apply.plan_drift",
                "Authoritative inputs changed; create and confirm a new plan",
                ExitCode.DRIFT,
            )

        if confirmation is None:
            if self.confirmation_reader is None:
                raise CliError(
                    "apply.confirmation_required",
                    "Apply requires explicit confirmation",
                    ExitCode.CONFIRMATION,
                )
            confirmation = self.confirmation_reader.read(approved.plan_hash)
            self._validate_confirmation(confirmation, approved.plan_hash)

        try:
            manifest = load_manifest(manifest_file, strict_commands=True)
            lock = load_lock(lock_file)
        except ManifestError:
            raise CliError(
                "apply.local_drift",
                "Local delivery-control files changed after planning",
                ExitCode.DRIFT,
            ) from None
        if manifest_digest(manifest) != body.manifest_digest or lock_digest(lock) != body.lock_digest:
            raise CliError(
                "apply.local_drift",
                "Local delivery-control files changed after planning",
                ExitCode.DRIFT,
            )

        if body.mode == "upgrade":
            candidate = self.migration_executor.candidate_lock(body, manifest, lock)
            if not body.actions:
                return ApplyResult((), body.state_fingerprint)
            provision_actions = body.actions[:-1]
            expected_actions = tuple(
                ReconcileAction(action.kind, action.key, action.changed_fields)
                for action in provision_actions
            )
            try:
                reconciled = self.provisioner.reconcile(
                    manifest,
                    candidate,
                    apply=True,
                    secret_lookup=secret_source.read,
                    expected_state_fingerprint=body.state_fingerprint,
                    expected_actions=expected_actions,
                )
            except ProvisionError as error:
                raise _provision_failure(error) from None
            if _plan_actions(reconciled.actions) != provision_actions:
                raise CliError(
                    "apply.action_mismatch",
                    "Applied semantic actions differ from the approved plan",
                    ExitCode.HUMAN_BLOCK,
                )
            final_lock = reconciled.lock
            if (
                final_lock.skill_version != __version__
                or final_lock.engine_version != __version__
                or final_lock.manifest_schema_version != manifest.schema_version
                or final_lock.workflow_metadata_version != WORKFLOW_METADATA_VERSION
                or final_lock.supported_multica_cli != SUPPORTED_MULTICA_CLI
                or final_lock.manifest_digest != body.manifest_digest
            ):
                raise CliError(
                    "apply.upgrade_nonconvergent",
                    "Upgrade did not return an exact current framework lock",
                    ExitCode.HUMAN_BLOCK,
                )

            def forbidden_lookup(name: str) -> str:
                raise AssertionError("upgrade convergence verification must not read secrets")

            try:
                verified = self.provisioner.reconcile(
                    manifest,
                    final_lock,
                    apply=False,
                    secret_lookup=forbidden_lookup,
                )
            except ProvisionError as error:
                raise _provision_failure(error) from None
            if (
                verified.actions
                or verified.lock != final_lock
                or verified.state_fingerprint != reconciled.state_fingerprint
            ):
                raise CliError(
                    "apply.upgrade_nonconvergent",
                    "Upgrade provisioning did not remain converged",
                    ExitCode.HUMAN_BLOCK,
                )
            atomic_replace_private(lock_file, _lock_bytes(final_lock))
            return ApplyResult(body.actions, reconciled.state_fingerprint)

        expected_actions = tuple(
            ReconcileAction(action.kind, action.key, action.changed_fields)
            for action in body.actions
        )
        try:
            reconciled = self.provisioner.reconcile(
                manifest,
                lock,
                apply=True,
                secret_lookup=secret_source.read,
                expected_state_fingerprint=body.state_fingerprint,
                expected_actions=expected_actions,
            )
        except ProvisionError as error:
            raise _provision_failure(error) from None

        applied_actions = _plan_actions(reconciled.actions)
        if applied_actions != body.actions:
            raise CliError(
                "apply.action_mismatch",
                "Applied semantic actions differ from the approved plan",
                ExitCode.HUMAN_BLOCK,
            )
        atomic_replace_private(lock_file, _lock_bytes(reconciled.lock))
        return ApplyResult(applied_actions, reconciled.state_fingerprint)

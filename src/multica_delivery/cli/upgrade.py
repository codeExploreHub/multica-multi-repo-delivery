"""Explicit framework-lock migration planning and execution."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

from multica_delivery import __version__
from multica_delivery.core.manifest import ManifestError, load_lock, load_manifest, manifest_digest
from multica_delivery.core.model import DeliveryManifest, FrameworkLock
from multica_delivery.core.provision import (
    SUPPORTED_MULTICA_CLI,
    WORKFLOW_METADATA_VERSION,
)

from .clock import Clock
from .errors import CliError, ExitCode
from .plan import (
    PlanAction,
    PlanBody,
    PlanEnvelope,
    PlanObservation,
    action_reason,
    lock_digest,
)
from .validation import ValidationReport, validate_upgrade_control_directory


_MIGRATION_EDGES = (
    ("0.0.0", "0.1.0"),
    ("0.1.0", "0.2.0"),
)


def _framework_version(lock: FrameworkLock) -> str:
    empty = (
        lock.skill_version == ""
        and lock.engine_version == ""
        and lock.supported_multica_cli == ""
        and lock.manifest_digest == ""
        and not lock.resource_ids
    )
    if empty:
        return "0.0.0"
    if lock.skill_version == lock.engine_version and lock.skill_version:
        return lock.skill_version
    raise CliError(
        "upgrade.incoherent_lock",
        "Framework lock version fields are incoherent",
        ExitCode.HUMAN_BLOCK,
    )


def _migration_actions(source: str) -> tuple[PlanAction, ...]:
    if source == __version__:
        return ()
    if (source, __version__) not in _MIGRATION_EDGES:
        raise CliError(
            "upgrade.unsupported_path",
            "No exact supported framework migration path exists",
            ExitCode.HUMAN_BLOCK,
        )
    return (
        PlanAction(
            "framework.version",
            f"{source}->{__version__}",
            (
                "skill_version",
                "engine_version",
                "workflow_metadata_version",
                "supported_multica_cli",
                "manifest_digest",
            ),
            action_reason(
                "framework.version",
                f"{source}->{__version__}",
                (
                    "skill_version",
                    "engine_version",
                    "workflow_metadata_version",
                    "supported_multica_cli",
                    "manifest_digest",
                ),
            ),
        ),
    )


def _validate_release_metadata_pair(source: str, workflow_metadata_version: int) -> None:
    if (source, workflow_metadata_version) not in {
        ("0.1.0", 1),
        (__version__, WORKFLOW_METADATA_VERSION),
    }:
        raise CliError(
            "upgrade.incompatible_release_metadata",
            "Framework release and workflow metadata versions are an unsupported pair",
            ExitCode.VALIDATION,
        )


def _validate_exact_local_lock(
    manifest: DeliveryManifest,
    lock: FrameworkLock,
    source: str,
) -> None:
    _validate_release_metadata_pair(source, lock.workflow_metadata_version)
    if (
        lock.manifest_schema_version != manifest.schema_version
        or lock.supported_multica_cli != SUPPORTED_MULTICA_CLI
        or lock.manifest_digest != manifest_digest(manifest)
    ):
        raise CliError(
            "upgrade.inexact_source_lock",
            "Framework lock is not exact for the current manifest and CLI contract",
            ExitCode.VALIDATION,
        )


def _upgrade_fingerprint(
    instance_key: str,
    manifest_value: str,
    lock_value: str,
    source: str,
) -> str:
    encoded = json.dumps(
        {
            "instance_key": instance_key,
            "manifest_digest": manifest_value,
            "lock_digest": lock_value,
            "source_version": source,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _candidate_v2_lock(
    lock: FrameworkLock,
    manifest_value: str,
) -> FrameworkLock:
    source = _framework_version(lock)
    if (source, __version__) != ("0.1.0", "0.2.0"):
        raise CliError(
            "upgrade.unsupported_path",
            "No exact supported framework migration path exists",
            ExitCode.HUMAN_BLOCK,
        )
    _validate_release_metadata_pair(source, lock.workflow_metadata_version)
    return FrameworkLock(
        __version__,
        __version__,
        lock.manifest_schema_version,
        WORKFLOW_METADATA_VERSION,
        SUPPORTED_MULTICA_CLI,
        manifest_value,
        lock.resource_ids,
    )


class UpgradeService:
    def __init__(
        self,
        *,
        validator: Callable[..., ValidationReport] = validate_upgrade_control_directory,
        version_reader: object | None = None,
        platform_name: str | None = None,
        python_version: tuple[int, int] | None = None,
        planning: object | None = None,
    ) -> None:
        self.validator = validator
        self.version_reader = version_reader
        self.platform_name = platform_name
        self.python_version = python_version
        self.planning = planning

    def observe(self, control_path: Path) -> PlanObservation:
        root = Path(control_path)
        report = self.validator(
            root,
            version_reader=self.version_reader,
            platform_name=self.platform_name,
            python_version=self.python_version,
        )
        if not report.valid:
            raise CliError(
                "upgrade.validation_failed",
                "Local delivery-control validation failed",
                ExitCode.VALIDATION,
            )
        try:
            manifest = load_manifest(
                root / "delivery.yaml",
                strict_commands=True,
            )
            lock = load_lock(root / "framework.lock")
        except ManifestError:
            raise CliError(
                "upgrade.invalid_control",
                "Delivery-control files are invalid",
                ExitCode.VALIDATION,
            ) from None
        source = _framework_version(lock)
        actions = _migration_actions(source)
        _validate_exact_local_lock(manifest, lock, source)
        manifest_value = manifest_digest(manifest)
        lock_value = lock_digest(lock)
        if actions:
            if self.planning is None:
                raise CliError(
                    "upgrade.planning_unavailable",
                    "Upgrade planning requires authoritative v2 reconciliation",
                    ExitCode.HUMAN_BLOCK,
                )
            candidate = _candidate_v2_lock(lock, manifest_value)
            reconciled = self.planning.observe_reconciliation(manifest, candidate)
            if (
                reconciled.instance_key != manifest.instance.key
                or reconciled.manifest_digest != manifest_value
                or reconciled.lock_digest != lock_digest(candidate)
            ):
                raise CliError(
                    "upgrade.remote_observation_mismatch",
                    "Upgrade reconciliation observation is not bound to the candidate lock",
                    ExitCode.DRIFT,
                )
            return PlanObservation(
                manifest.instance.key,
                manifest_value,
                lock_value,
                reconciled.state_fingerprint,
                reconciled.actions + actions,
            )
        return PlanObservation(
            manifest.instance.key,
            manifest_value,
            lock_value,
            _upgrade_fingerprint(manifest.instance.key, manifest_value, lock_value, source),
            actions,
        )

    def create(self, control_path: Path, clock: Clock) -> PlanEnvelope:
        observed = self.observe(control_path)
        instant = clock.now()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise CliError(
                "upgrade.invalid_clock",
                "Upgrade planning requires a timezone-aware clock",
                ExitCode.VALIDATION,
            )
        created_at = int(instant.timestamp())
        return PlanEnvelope.create(
            PlanBody(
                1,
                "upgrade",
                __version__,
                observed.instance_key,
                observed.manifest_digest,
                observed.lock_digest,
                observed.state_fingerprint,
                created_at,
                created_at + 600,
                observed.actions,
            )
        )


class MigrationExecutor:
    """Derive the candidate lock for one exact migration registry edge."""

    def __init__(self, planning: object | None = None) -> None:
        self.planning = planning

    def observe(self, control_path: Path) -> PlanObservation:
        return UpgradeService(
            validator=lambda path, **kwargs: _valid_report(),
            planning=self.planning,
        ).observe(control_path)

    def candidate_lock(
        self,
        body: PlanBody,
        manifest: DeliveryManifest,
        lock: FrameworkLock,
    ) -> FrameworkLock:
        if body.mode != "upgrade":
            raise CliError(
                "upgrade.mode_mismatch",
                "Migration executor accepts only upgrade plans",
                ExitCode.HUMAN_BLOCK,
            )
        source = _framework_version(lock)
        expected = _migration_actions(source)
        _validate_exact_local_lock(manifest, lock, source)
        if (
            body.lock_digest != lock_digest(lock)
            or body.manifest_digest != manifest_digest(manifest)
            or (not expected and body.actions)
            or (
                expected
                and (
                    body.actions[-len(expected) :] != expected
                    or any(action.kind == "framework.version" for action in body.actions[:-1])
                )
            )
        ):
            raise CliError(
                "upgrade.plan_drift",
                "Framework migration inputs changed",
                ExitCode.DRIFT,
            )
        if not expected:
            return lock
        return _candidate_v2_lock(lock, body.manifest_digest)


def _valid_report() -> ValidationReport:
    from .validation import ValidationFinding

    return ValidationReport((ValidationFinding("pass", "upgrade.local", "valid"),))

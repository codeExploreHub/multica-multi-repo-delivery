"""Explicit framework-lock migration planning and execution."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Callable, Mapping

import yaml

from multica_delivery import __version__
from multica_delivery.core.manifest import ManifestError, load_lock, load_manifest, manifest_digest
from multica_delivery.core.model import FrameworkLock
from multica_delivery.core.provision import SUPPORTED_MULTICA_CLI

from .clock import Clock
from .errors import CliError, ExitCode
from .plan import PlanAction, PlanBody, PlanEnvelope, PlanObservation, lock_digest
from .validation import ValidationReport, validate_control_directory


_MIGRATION_EDGES = (("0.0.0", "0.1.0"),)


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
            ("skill_version", "engine_version", "supported_multica_cli", "manifest_digest"),
        ),
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


class UpgradeService:
    def __init__(
        self,
        *,
        validator: Callable[..., ValidationReport] = validate_control_directory,
        version_reader: object | None = None,
        platform_name: str | None = None,
        python_version: tuple[int, int] | None = None,
    ) -> None:
        self.validator = validator
        self.version_reader = version_reader
        self.platform_name = platform_name
        self.python_version = python_version

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
            manifest = load_manifest(root / "delivery.yaml")
            lock = load_lock(root / "framework.lock")
        except ManifestError:
            raise CliError(
                "upgrade.invalid_control",
                "Delivery-control files are invalid",
                ExitCode.VALIDATION,
            ) from None
        source = _framework_version(lock)
        actions = _migration_actions(source)
        manifest_value = manifest_digest(manifest)
        lock_value = lock_digest(lock)
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
    """Apply only the exact local framework-lock migration registry."""

    def observe(self, control_path: Path) -> PlanObservation:
        return UpgradeService(validator=lambda path, **kwargs: _valid_report()).observe(control_path)

    def apply(self, body: PlanBody, lock_path: Path) -> FrameworkLock:
        if body.mode != "upgrade":
            raise CliError(
                "upgrade.mode_mismatch",
                "Migration executor accepts only upgrade plans",
                ExitCode.HUMAN_BLOCK,
            )
        try:
            lock = load_lock(lock_path)
        except ManifestError:
            raise CliError(
                "upgrade.lock_invalid",
                "Framework lock is invalid",
                ExitCode.DRIFT,
            ) from None
        source = _framework_version(lock)
        expected = _migration_actions(source)
        if body.actions != expected or body.lock_digest != lock_digest(lock):
            raise CliError(
                "upgrade.plan_drift",
                "Framework migration inputs changed",
                ExitCode.DRIFT,
            )
        if not expected:
            return lock
        return FrameworkLock(
            __version__,
            __version__,
            lock.manifest_schema_version,
            lock.workflow_metadata_version,
            SUPPORTED_MULTICA_CLI,
            body.manifest_digest,
            lock.resource_ids,
        )

    @staticmethod
    def serialize(lock: FrameworkLock) -> bytes:
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


def _valid_report() -> ValidationReport:
    from .validation import ValidationFinding

    return ValidationReport((ValidationFinding("pass", "upgrade.local", "valid"),))

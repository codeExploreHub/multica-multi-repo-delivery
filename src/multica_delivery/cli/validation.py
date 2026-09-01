"""Read-only validation of a rendered delivery-control directory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import Protocol

from multica_delivery.core.manifest import ManifestError, load_lock, load_manifest, manifest_digest
from multica_delivery.core.provision import WORKFLOW_METADATA_VERSION


@dataclass(frozen=True, order=True)
class ValidationFinding:
    severity: str
    code: str
    message: str

    def to_value(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class ValidationReport:
    findings: tuple[ValidationFinding, ...]

    @property
    def valid(self) -> bool:
        return all(finding.severity not in {"fail", "human-block"} for finding in self.findings)

    def to_value(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "findings": [finding.to_value() for finding in sorted(self.findings)],
        }


class VersionReader(Protocol):
    def version(self, executable: str) -> str | None: ...


class SubprocessVersionReader:
    """Closed read-only version probe for known CLI executables."""

    _ALLOWED = {"multica", "gh"}

    def version(self, executable: str) -> str | None:
        if executable not in self._ALLOWED:
            return None
        try:
            completed = subprocess.run(
                [executable, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        first_line = completed.stdout.splitlines()[0] if completed.stdout.splitlines() else ""
        return first_line[:200] or None


def _validate_control_directory(
    path: Path,
    *,
    version_reader: VersionReader | None = None,
    platform_name: str | None = None,
    python_version: tuple[int, int] | None = None,
    upgrade_observation: bool,
) -> ValidationReport:
    root = Path(path)
    findings: list[ValidationFinding] = []
    manifest = None
    lock = None
    try:
        manifest = load_manifest(root / "delivery.yaml", strict_commands=True)
        findings.append(ValidationFinding("pass", "manifest.valid", "Manifest schema is valid"))
    except ManifestError:
        findings.append(ValidationFinding("fail", "manifest.invalid", "Manifest schema is invalid"))
    try:
        lock = load_lock(root / "framework.lock")
        findings.append(ValidationFinding("pass", "lock.valid", "Framework lock schema is valid"))
    except ManifestError:
        findings.append(ValidationFinding("fail", "lock.invalid", "Framework lock schema is invalid"))

    if manifest is not None:
        required_roles = {
            "delivery-lead",
            "independent-reviewer",
            "integration-qa",
            "workflow-watcher",
        }
        if set(manifest.role_skills) != required_roles:
            findings.append(
                ValidationFinding("fail", "roles.incomplete", "Every fixed role requires Skill bindings")
            )
        paths = [manifest.control.local_path]
        paths.extend(repository.local_path for repository in manifest.repositories.values())
        for local_path in paths:
            if not local_path.is_absolute() or local_path.resolve() != local_path:
                findings.append(
                    ValidationFinding("fail", "path.invalid", "Manifest paths must be absolute and non-aliased")
                )
            elif not local_path.exists():
                findings.append(
                    ValidationFinding("fail", "path.missing", "A declared local path does not exist")
                )
    if manifest is not None and lock is not None:
        workflow_metadata_compatible = (
            lock.workflow_metadata_version == WORKFLOW_METADATA_VERSION
            or (upgrade_observation and lock.workflow_metadata_version == 1)
        )
        if (
            lock.manifest_schema_version != manifest.schema_version
            or not workflow_metadata_compatible
        ):
            findings.append(
                ValidationFinding("fail", "lock.incompatible", "Framework lock versions are incompatible")
            )
        elif lock.manifest_digest and lock.manifest_digest != manifest_digest(manifest):
            findings.append(
                ValidationFinding("fail", "lock.manifest_drift", "Framework lock manifest digest is stale")
            )
        else:
            findings.append(ValidationFinding("pass", "lock.compatible", "Framework lock is compatible"))

    platform_value = sys.platform if platform_name is None else platform_name
    if platform_value not in {"darwin", "linux"}:
        findings.append(ValidationFinding("fail", "platform.unsupported", "Platform is unsupported"))
    else:
        findings.append(ValidationFinding("pass", "platform.supported", "Platform is supported"))
    version_value = (sys.version_info.major, sys.version_info.minor) if python_version is None else python_version
    if version_value not in {(3, 11), (3, 12), (3, 13)}:
        findings.append(ValidationFinding("fail", "python.unsupported", "Python version is unsupported"))
    else:
        findings.append(ValidationFinding("pass", "python.supported", "Python version is supported"))

    reader = version_reader or SubprocessVersionReader()
    for executable in ("multica", "gh"):
        if reader.version(executable) is None:
            findings.append(
                ValidationFinding("fail", f"tool.{executable}_missing", f"{executable} is unavailable")
            )
        else:
            findings.append(
                ValidationFinding("pass", f"tool.{executable}_available", f"{executable} is available")
            )
    return ValidationReport(tuple(findings))


def validate_control_directory(
    path: Path,
    *,
    version_reader: VersionReader | None = None,
    platform_name: str | None = None,
    python_version: tuple[int, int] | None = None,
) -> ValidationReport:
    return _validate_control_directory(
        path,
        version_reader=version_reader,
        platform_name=platform_name,
        python_version=python_version,
        upgrade_observation=False,
    )


def validate_upgrade_control_directory(
    path: Path,
    *,
    version_reader: VersionReader | None = None,
    platform_name: str | None = None,
    python_version: tuple[int, int] | None = None,
) -> ValidationReport:
    """Validate the sole read-only path that may observe legacy metadata v1."""
    return _validate_control_directory(
        path,
        version_reader=version_reader,
        platform_name=platform_name,
        python_version=python_version,
        upgrade_observation=True,
    )

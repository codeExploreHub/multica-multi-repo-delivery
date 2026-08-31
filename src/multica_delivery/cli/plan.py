"""Canonical, authenticated, secret-free delivery plans."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from multica_delivery import __version__
from multica_delivery.core.contract_audit import audit_contracts
from multica_delivery.core.manifest import (
    ManifestError,
    load_lock,
    load_manifest,
    manifest_digest,
)
from multica_delivery.core.model import DeliveryManifest, FrameworkLock
from multica_delivery.core.provision import ProvisionError, Provisioner

from .clock import Clock
from .errors import CliError, ExitCode
from .files import atomic_replace_private
from .validation import SubprocessVersionReader, ValidationReport, validate_control_directory


_DIGEST = re.compile(r"[0-9a-f]{64}")
_BODY_FIELDS = {
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
}


def _invalid_plan(message: str = "Plan data is invalid") -> CliError:
    return CliError("plan.invalid", message, ExitCode.VALIDATION)


def _contains_float(value: object) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, Mapping):
        return any(_contains_float(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_float(item) for item in value)
    return False


@dataclass(frozen=True)
class PlanAction:
    kind: str
    key: str
    changed_fields: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if (
            type(self.kind) is not str
            or not self.kind
            or type(self.key) is not str
            or not self.key
            or type(self.changed_fields) is not tuple
            or not all(type(field) is str and field for field in self.changed_fields)
            or len(self.changed_fields) != len(set(self.changed_fields))
            or type(self.reason) is not str
            or not self.reason
        ):
            raise _invalid_plan("Plan action is invalid")

    def to_value(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "key": self.key,
            "changed_fields": list(self.changed_fields),
            "reason": self.reason,
        }

    @classmethod
    def from_value(cls, value: object) -> "PlanAction":
        if not isinstance(value, dict) or set(value) != {
            "kind",
            "key",
            "changed_fields",
            "reason",
        }:
            raise _invalid_plan("Plan action fields are invalid")
        changed = value["changed_fields"]
        if not isinstance(changed, list):
            raise _invalid_plan("Plan action changed_fields must be a list")
        return cls(value["kind"], value["key"], tuple(changed), value["reason"])


def action_reason(kind: str, key: str, changed_fields: tuple[str, ...]) -> str:
    """Build the stable, human-readable rationale authenticated by a plan."""
    if changed_fields:
        fields_text = ", ".join(changed_fields)
        return f"Reconcile {key} with {kind}; changed fields: {fields_text}."
    return f"Reconcile {key} with {kind}."


@dataclass(frozen=True)
class PlanBody:
    schema_version: int
    mode: str
    cli_version: str
    instance_key: str
    manifest_digest: str
    lock_digest: str
    state_fingerprint: str
    created_at: int
    expires_at: int
    actions: tuple[PlanAction, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise _invalid_plan("Plan schema_version must be 1")
        if self.mode not in {"onboard", "upgrade"} or type(self.mode) is not str:
            raise _invalid_plan("Plan mode is invalid")
        if type(self.cli_version) is not str or not self.cli_version:
            raise _invalid_plan("Plan CLI version is invalid")
        if type(self.instance_key) is not str or not self.instance_key:
            raise _invalid_plan("Plan instance key is invalid")
        for value in (self.manifest_digest, self.lock_digest, self.state_fingerprint):
            if type(value) is not str or _DIGEST.fullmatch(value) is None:
                raise _invalid_plan("Plan digest is invalid")
        if (
            type(self.created_at) is not int
            or type(self.expires_at) is not int
            or self.created_at < 0
            or self.expires_at != self.created_at + 600
        ):
            raise _invalid_plan("Plan timestamps must define the ten-minute window")
        if type(self.actions) is not tuple or not all(
            isinstance(action, PlanAction) for action in self.actions
        ):
            raise _invalid_plan("Plan actions are invalid")

    def to_value(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "cli_version": self.cli_version,
            "instance_key": self.instance_key,
            "manifest_digest": self.manifest_digest,
            "lock_digest": self.lock_digest,
            "state_fingerprint": self.state_fingerprint,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "actions": [action.to_value() for action in self.actions],
        }

    @classmethod
    def from_value(cls, value: object) -> "PlanBody":
        if not isinstance(value, dict) or set(value) != _BODY_FIELDS or _contains_float(value):
            raise _invalid_plan("Plan body fields are invalid")
        actions = value["actions"]
        if not isinstance(actions, list):
            raise _invalid_plan("Plan actions must be a list")
        return cls(
            schema_version=value["schema_version"],
            mode=value["mode"],
            cli_version=value["cli_version"],
            instance_key=value["instance_key"],
            manifest_digest=value["manifest_digest"],
            lock_digest=value["lock_digest"],
            state_fingerprint=value["state_fingerprint"],
            created_at=value["created_at"],
            expires_at=value["expires_at"],
            actions=tuple(PlanAction.from_value(action) for action in actions),
        )


def _canonical_body(body: PlanBody) -> bytes:
    return json.dumps(
        body.to_value(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


@dataclass(frozen=True)
class PlanEnvelope:
    body: PlanBody
    plan_hash: str

    def __post_init__(self) -> None:
        if _DIGEST.fullmatch(self.plan_hash) is None:
            raise _invalid_plan("Plan hash is invalid")
        expected = hashlib.sha256(_canonical_body(self.body)).hexdigest()
        if self.plan_hash != expected:
            raise _invalid_plan("Plan hash does not authenticate its body")

    @classmethod
    def create(cls, body: PlanBody) -> "PlanEnvelope":
        return cls(body, hashlib.sha256(_canonical_body(body)).hexdigest())

    def to_value(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "command": "plan",
            "status": "ok",
            "result": {"body": self.body.to_value(), "plan_hash": self.plan_hash},
            "warnings": [],
            "errors": [],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_value(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @classmethod
    def from_value(cls, value: object) -> "PlanEnvelope":
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "command",
            "status",
            "result",
            "warnings",
            "errors",
        }:
            raise _invalid_plan("Plan envelope fields are invalid")
        if (
            value["schema_version"] != 1
            or value["command"] != "plan"
            or value["status"] != "ok"
            or value["warnings"] != []
            or value["errors"] != []
        ):
            raise _invalid_plan("Plan envelope identity is invalid")
        result = value["result"]
        if not isinstance(result, dict) or set(result) != {"body", "plan_hash"}:
            raise _invalid_plan("Plan result fields are invalid")
        return cls(PlanBody.from_value(result["body"]), result["plan_hash"])


class PlanStore:
    def write(self, path: Path, envelope: PlanEnvelope) -> None:
        atomic_replace_private(Path(path), (envelope.to_json() + "\n").encode("utf-8"))

    def load(self, path: Path) -> PlanEnvelope:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise _invalid_plan("Plan file cannot be read safely") from None
        return PlanEnvelope.from_value(value)


def _canonical_model(value: object) -> object:
    if is_dataclass(value):
        return {
            field.name: _canonical_model(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _canonical_model(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple):
        return [_canonical_model(item) for item in value]
    if value is None or type(value) in {str, int, bool}:
        return value
    raise _invalid_plan("Framework lock contains an invalid value")


def lock_digest(lock: FrameworkLock) -> str:
    encoded = json.dumps(
        _canonical_model(lock),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PlanObservation:
    instance_key: str
    manifest_digest: str
    lock_digest: str
    state_fingerprint: str
    actions: tuple[PlanAction, ...]


class PlanningService:
    def __init__(
        self,
        provisioner: Provisioner,
        *,
        version_reader: object | None = None,
        validator: Callable[..., ValidationReport] = validate_control_directory,
        contract_auditor: Callable[..., object] = audit_contracts,
        platform_name: str | None = None,
        python_version: tuple[int, int] | None = None,
    ) -> None:
        self.provisioner = provisioner
        self.version_reader = version_reader or SubprocessVersionReader()
        self.validator = validator
        self.contract_auditor = contract_auditor
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
                "plan.validation_failed",
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
                "plan.validation_failed",
                "Local delivery-control files are invalid",
                ExitCode.VALIDATION,
            ) from None
        reconciled = self.observe_reconciliation(manifest, lock)
        return PlanObservation(
            manifest.instance.key,
            manifest_digest(manifest),
            lock_digest(lock),
            reconciled.state_fingerprint,
            reconciled.actions,
        )

    def observe_reconciliation(
        self,
        manifest: DeliveryManifest,
        lock: FrameworkLock,
    ) -> PlanObservation:
        """Observe one exact manifest/lock pair without rereading local files."""
        try:
            audit = self.contract_auditor(
                self.provisioner.multica,
                self.provisioner.github,
                manifest,
            )
        except Exception:
            raise CliError(
                "plan.contract_failed",
                "External contract audit failed",
                ExitCode.EXTERNAL,
            ) from None
        if any(getattr(entry, "status", None) == "fail" for entry in audit.entries):
            raise CliError(
                "plan.contract_failed",
                "External contract audit reported a failure",
                ExitCode.EXTERNAL,
            )

        def forbidden_lookup(name: str) -> str:
            raise AssertionError("planning must not read secrets")

        try:
            reconciled = self.provisioner.reconcile(
                manifest,
                lock,
                apply=False,
                secret_lookup=forbidden_lookup,
            )
        except ProvisionError as error:
            message = str(error)
            if message == "authoritative state changed during planning":
                exit_code = ExitCode.DRIFT
                code = "plan.state_drift"
            elif "foreign" in message or "duplicate" in message:
                exit_code = ExitCode.HUMAN_BLOCK
                code = "plan.foreign_state"
            else:
                exit_code = ExitCode.EXTERNAL
                code = "plan.external_failure"
            raise CliError(code, "Read-only planning could not establish safe state", exit_code) from None
        return PlanObservation(
            manifest.instance.key,
            manifest_digest(manifest),
            lock_digest(lock),
            reconciled.state_fingerprint,
            tuple(
                PlanAction(
                    action.kind,
                    action.key,
                    action.changed_fields,
                    action_reason(action.kind, action.key, action.changed_fields),
                )
                for action in reconciled.actions
            ),
        )

    def create(
        self,
        control_path: Path,
        clock: Clock,
        *,
        mode: str = "onboard",
    ) -> PlanEnvelope:
        observed = self.observe(control_path)
        instant = clock.now()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise CliError(
                "plan.invalid_clock",
                "Planning requires a timezone-aware clock",
                ExitCode.VALIDATION,
            )
        created_at = int(instant.astimezone(timezone.utc).timestamp())
        body = PlanBody(
            1,
            mode,
            __version__,
            observed.instance_key,
            observed.manifest_digest,
            observed.lock_digest,
            observed.state_fingerprint,
            created_at,
            created_at + 600,
            observed.actions,
        )
        return PlanEnvelope.create(body)

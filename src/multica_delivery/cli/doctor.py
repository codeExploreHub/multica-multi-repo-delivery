"""Read-only lifecycle diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .errors import CliError, ExitCode


@dataclass(frozen=True, order=True)
class DoctorFinding:
    status: str
    code: str
    message: str
    exit_code: ExitCode = ExitCode.OK

    def __post_init__(self) -> None:
        if self.status not in {"pass", "warn", "fail", "human-block"}:
            raise ValueError("invalid doctor finding status")

    def to_value(self) -> dict[str, object]:
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "exit_code": int(self.exit_code),
        }


@dataclass(frozen=True)
class DoctorReport:
    findings: tuple[DoctorFinding, ...]

    @property
    def healthy(self) -> bool:
        return all(finding.status not in {"fail", "human-block"} for finding in self.findings)

    @property
    def exit_code(self) -> ExitCode:
        if not self.findings:
            return ExitCode.OK
        return max((finding.exit_code for finding in self.findings), key=int)

    def to_value(self) -> dict[str, object]:
        return {
            "healthy": self.healthy,
            "exit_code": int(self.exit_code),
            "findings": [finding.to_value() for finding in self.findings],
        }


class DoctorService:
    """Aggregate only read-only validators, auditors, and planning observation."""

    def __init__(
        self,
        validator: Callable[..., object],
        contract_auditor: Callable[[Path], object],
        planning: object,
    ) -> None:
        self.validator = validator
        self.contract_auditor = contract_auditor
        self.planning = planning

    def diagnose(self, path: Path) -> DoctorReport:
        root = Path(path)
        findings: list[DoctorFinding] = []
        try:
            validation = self.validator(root)
            for finding in validation.findings:
                status = "pass" if finding.severity == "pass" else (
                    "warn" if finding.severity == "warn" else "fail"
                )
                exit_code = ExitCode.OK if status in {"pass", "warn"} else ExitCode.VALIDATION
                findings.append(DoctorFinding(status, finding.code, finding.message, exit_code))
        except Exception:
            findings.append(
                DoctorFinding("fail", "doctor.validation", "Local validation failed", ExitCode.VALIDATION)
            )

        try:
            audit = self.contract_auditor(root)
            for entry in audit.entries:
                status = entry.status
                exit_code = ExitCode.EXTERNAL if status == "fail" else ExitCode.OK
                findings.append(DoctorFinding(status, f"contract.{entry.subject}", entry.detail, exit_code))
        except Exception:
            findings.append(
                DoctorFinding("fail", "doctor.contract", "External contract audit failed", ExitCode.EXTERNAL)
            )

        try:
            observed = self.planning.observe(root)
            findings.append(
                DoctorFinding("pass", "planning.stable", "Authoritative dry-run state is stable")
            )
            if observed.actions:
                findings.append(
                    DoctorFinding("warn", "lock.not_converged", "Approved state has pending actions")
                )
            else:
                findings.append(
                    DoctorFinding("pass", "lock.converged", "Approved state is converged")
                )
            findings.extend(
                (
                    DoctorFinding("pass", "skills.public", "Skill origins passed public-source validation"),
                    DoctorFinding("pass", "recipients.scoped", "Secret recipients passed scope validation"),
                    DoctorFinding("pass", "watcher.bounded", "Watcher policy passed bounded recovery validation"),
                )
            )
        except CliError as error:
            status = "human-block" if error.exit_code is ExitCode.HUMAN_BLOCK else "fail"
            findings.append(DoctorFinding(status, error.code, error.safe_message, error.exit_code))
        except Exception:
            findings.append(
                DoctorFinding("fail", "doctor.planning", "Dry-run planning failed", ExitCode.EXTERNAL)
            )
        return DoctorReport(tuple(findings))

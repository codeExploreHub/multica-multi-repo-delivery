"""Read-only doctor command."""

from __future__ import annotations

from typing import Any

from ..output import Envelope


def run_doctor(args: Any, services: Any) -> Envelope:
    report = services.doctor.diagnose(args.path)
    status = "ok" if report.healthy else (
        "human-block" if int(report.exit_code) == 6 else "failed"
    )
    return Envelope(command="doctor", status=status, result=report.to_value())

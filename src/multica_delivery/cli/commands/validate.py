"""Read-only validation command."""

from __future__ import annotations

from typing import Any

from ..output import Envelope
from ..validation import validate_control_directory


def run_validate(args: Any, services: Any) -> Envelope:
    report = validate_control_directory(
        args.path,
        version_reader=services.version_reader,
        platform_name=services.platform_name,
        python_version=services.python_version,
    )
    return Envelope(
        command="validate",
        status="ok" if report.valid else "failed",
        result=report.to_value(),
    )

"""Explicitly authorized apply command."""

from __future__ import annotations

from typing import Any

from ..output import Envelope


def run_apply(args: Any, services: Any) -> Envelope:
    result = services.apply_service.apply(
        args.plan_path,
        args.confirmation,
        args.manifest_path,
        args.lock_path,
        services.secret_source,
    )
    return Envelope(command="apply", status="ok", result=result.to_value())

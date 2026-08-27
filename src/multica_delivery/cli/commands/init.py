"""Confirmation-driven local initialization command."""

from __future__ import annotations

from typing import Any

from ..output import Envelope
from ..templates import initialize_scaffold


def run_init(args: Any, services: Any) -> Envelope:
    created = initialize_scaffold(
        services.discovery,
        services.confirmations,
        args.target,
    )
    return Envelope(
        command="init",
        status="ok",
        result={"created": [str(path) for path in created]},
    )

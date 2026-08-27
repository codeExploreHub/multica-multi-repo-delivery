"""Read-only discover command handler."""

from __future__ import annotations

from typing import Any

from ..discovery import discover_repositories
from ..output import Envelope


def run_discover(args: Any, services: Any) -> Envelope:
    document = discover_repositories(args.paths, services.repository_reader)
    return Envelope(command="discover", status="ok", result=document.to_value())

"""Read-only upgrade-plan command."""

from __future__ import annotations

from typing import Any

from ..output import Envelope


def run_upgrade(args: Any, services: Any) -> Envelope:
    plan = services.upgrade.create(args.path, services.clock)
    services.plan_store.write(args.plan_path, plan)
    return Envelope(
        command="upgrade",
        status="ok",
        result={"body": plan.body.to_value(), "plan_hash": plan.plan_hash},
    )

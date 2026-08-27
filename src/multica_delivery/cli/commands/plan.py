"""Read-only plan command with atomic local artifact emission."""

from __future__ import annotations

from typing import Any

from ..output import Envelope


def run_plan(args: Any, services: Any) -> Envelope:
    plan = services.planning.create(args.path, services.clock, mode=args.mode)
    services.plan_store.write(args.plan_path, plan)
    return Envelope(
        command="plan",
        status="ok",
        result={"body": plan.body.to_value(), "plan_hash": plan.plan_hash},
    )

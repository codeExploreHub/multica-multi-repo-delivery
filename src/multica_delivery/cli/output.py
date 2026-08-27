"""Deterministic human and machine output."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Mapping


@dataclass(frozen=True, order=True)
class SafeMessage:
    """A stable output message that contains no raw exception data."""

    code: str
    message: str

    def to_value(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class Envelope:
    """The versioned output contract shared by every command."""

    command: str
    status: str
    result: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[SafeMessage, ...] = ()
    errors: tuple[SafeMessage, ...] = ()
    schema_version: int = 1

    def to_value(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "command": self.command,
            "status": self.status,
            "result": dict(self.result),
            "warnings": [message.to_value() for message in sorted(self.warnings)],
            "errors": [message.to_value() for message in sorted(self.errors)],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_value(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def to_human(self) -> str:
        lines = [f"{self.command}: {self.status}"]
        lines.extend(f"{key}: {self.result[key]}" for key in sorted(self.result))
        lines.extend(f"warning [{item.code}]: {item.message}" for item in sorted(self.warnings))
        lines.extend(f"error [{item.code}]: {item.message}" for item in sorted(self.errors))
        return "\n".join(lines)

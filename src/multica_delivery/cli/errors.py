"""Stable CLI errors and process exit codes."""

from enum import IntEnum


class ExitCode(IntEnum):
    """Public process exit contract."""

    OK = 0
    VALIDATION = 2
    CONFIRMATION = 3
    DRIFT = 4
    EXTERNAL = 5
    HUMAN_BLOCK = 6


class CliError(Exception):
    """An expected failure containing only output-safe context."""

    def __init__(self, code: str, safe_message: str, exit_code: ExitCode) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.exit_code = exit_code

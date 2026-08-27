"""Deferred, manifest-scoped secret resolution."""

from __future__ import annotations

import getpass
import os
import re
from typing import Callable, Mapping, Protocol

from .errors import CliError, ExitCode


_SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]*")


class SecretSource(Protocol):
    def read(self, name: str) -> str: ...


def _declared_names(names: set[str] | frozenset[str]) -> frozenset[str]:
    declared = frozenset(names)
    if not all(_SECRET_NAME.fullmatch(name) for name in declared):
        raise CliError(
            "secret.invalid_declaration",
            "Declared secret names are invalid",
            ExitCode.VALIDATION,
        )
    return declared


class EnvironmentSecretSource:
    """Read only explicitly declared variables, and only when requested."""

    def __init__(
        self,
        declared_names: set[str] | frozenset[str],
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._declared = _declared_names(declared_names)
        self._environment = os.environ if environment is None else environment

    def read(self, name: str) -> str:
        if name not in self._declared:
            raise CliError(
                "secret.undeclared",
                "A secret was requested outside the manifest declaration",
                ExitCode.HUMAN_BLOCK,
            )
        value = self._environment.get(name)
        if not isinstance(value, str) or not value:
            raise CliError(
                "secret.missing",
                "A declared secret value is unavailable",
                ExitCode.HUMAN_BLOCK,
            )
        return value


class PromptSecretSource:
    """Prompt without echo only when an authorized apply requests a value."""

    def __init__(
        self,
        declared_names: set[str] | frozenset[str],
        prompt: Callable[[str], str] = getpass.getpass,
    ) -> None:
        self._declared = _declared_names(declared_names)
        self._prompt = prompt

    def read(self, name: str) -> str:
        if name not in self._declared:
            raise CliError(
                "secret.undeclared",
                "A secret was requested outside the manifest declaration",
                ExitCode.HUMAN_BLOCK,
            )
        value = self._prompt(f"Value for {name}: ")
        if not isinstance(value, str) or not value:
            raise CliError(
                "secret.missing",
                "A declared secret value is unavailable",
                ExitCode.HUMAN_BLOCK,
            )
        return value

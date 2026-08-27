"""Strict, secret-free operator confirmation documents."""

from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from .errors import CliError, ExitCode


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found duplicate key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)

_ENV_REFERENCE = re.compile(r"\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)")
_SECRET_PREFIX = re.compile(r"(?i)^(?:ghp_|github_pat_|sk-|akia)[A-Za-z0-9_\-]+$")


@dataclass(frozen=True)
class ConfirmationDocument:
    discovery_digest: str
    values: Mapping[str, Any]
    schema_version: int = 1


def _contains_secret_like_scalar(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_ENV_REFERENCE.search(value) or _SECRET_PREFIX.fullmatch(value))
    if isinstance(value, list):
        return any(_contains_secret_like_scalar(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_secret_like_scalar(item) for item in value.values())
    return False


def load_confirmation_text(text: str) -> ConfirmationDocument:
    try:
        tokens = tuple(yaml.scan(text))
        if any(isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)) for token in tokens):
            raise ValueError("aliases are forbidden")
        value = yaml.load(text, Loader=_UniqueKeyLoader)
    except (yaml.YAMLError, ValueError):
        raise CliError(
            "confirmation.invalid_yaml",
            "Confirmation YAML must be alias-free and contain unique keys",
            ExitCode.VALIDATION,
        ) from None
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "discovery_digest",
        "values",
    }:
        raise CliError(
            "confirmation.invalid_schema",
            "Confirmation YAML has an invalid top-level schema",
            ExitCode.VALIDATION,
        )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise CliError(
            "confirmation.invalid_schema",
            "Confirmation schema_version must be 1",
            ExitCode.VALIDATION,
        )
    digest = value["discovery_digest"]
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise CliError(
            "confirmation.invalid_digest",
            "Confirmation discovery_digest must be a complete SHA-256 digest",
            ExitCode.VALIDATION,
        )
    records = value["values"]
    if not isinstance(records, dict):
        raise CliError(
            "confirmation.invalid_values",
            "Confirmation values must be a mapping",
            ExitCode.VALIDATION,
        )
    confirmed: dict[str, Any] = {}
    for path, record in records.items():
        if not isinstance(path, str) or not path or path.startswith(".") or path.endswith("."):
            raise CliError(
                "confirmation.invalid_path",
                "Every confirmation value requires a valid manifest path",
                ExitCode.VALIDATION,
            )
        if not isinstance(record, dict) or set(record) != {"value", "confirmed"}:
            raise CliError(
                "confirmation.invalid_record",
                "Every confirmation record must contain exactly value and confirmed",
                ExitCode.VALIDATION,
            )
        if record["confirmed"] is not True:
            raise CliError(
                "confirmation.not_confirmed",
                "Every supplied value must be explicitly confirmed",
                ExitCode.VALIDATION,
            )
        if _contains_secret_like_scalar(record["value"]):
            raise CliError(
                "confirmation.secret_value",
                "Confirmation values must not contain secrets or environment references",
                ExitCode.VALIDATION,
            )
        confirmed[path] = record["value"]
    return ConfirmationDocument(digest, MappingProxyType(confirmed))

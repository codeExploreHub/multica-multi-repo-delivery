"""Confirmation-driven local delivery-control rendering."""

from __future__ import annotations

from importlib.resources import files
from importlib.resources.abc import Traversable
import os
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

from multica_delivery.core.manifest import ManifestError, load_manifest_text

from .confirmation import ConfirmationDocument
from .discovery import Classification, DiscoveryDocument, RepositoryDiscovery
from .errors import CliError, ExitCode
from .files import atomic_write_new


_SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_FIXED_ROLE_NAMES = {
    "delivery-lead",
    "independent-reviewer",
    "integration-qa",
    "workflow-watcher",
}
_LOCK_BYTES = b"""skill_version: ''
engine_version: ''
manifest_schema_version: 1
workflow_metadata_version: 2
supported_multica_cli: ''
manifest_digest: ''
resource_ids: {}
"""
_PACKAGED_TEMPLATE_NAMES = frozenset(
    {"delivery.yaml", "framework.lock", "env.example", "AGENTS.md", "gitignore.fragment"}
)


def template_path(name: str) -> Traversable:
    """Return one closed, packaged scaffold resource."""

    if type(name) is not str or name not in _PACKAGED_TEMPLATE_NAMES:
        raise ValueError("unknown packaged template")
    return files("multica_delivery").joinpath("templates", name)


def _github_slug(remote: str) -> str | None:
    https = re.fullmatch(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?", remote)
    ssh = re.fullmatch(r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?", remote)
    match = https or ssh
    return match.group(1) if match else None


def _set_path(document: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current: Any = document
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        next_is_index = not last and parts[index + 1].isdigit()
        if isinstance(current, dict):
            if last:
                if part in current and current[part] != value:
                    raise CliError(
                        "confirmation.conflict",
                        "A confirmed value conflicts with authoritative discovery",
                        ExitCode.VALIDATION,
                    )
                current[part] = value
                continue
            expected = [] if next_is_index else {}
            existing = current.setdefault(part, expected)
            if not isinstance(existing, type(expected)):
                raise CliError(
                    "confirmation.path_conflict",
                    "Confirmation paths contain incompatible structures",
                    ExitCode.VALIDATION,
                )
            current = existing
            continue
        if isinstance(current, list) and part.isdigit():
            position = int(part)
            while len(current) <= position:
                current.append(None)
            if last:
                if current[position] is not None and current[position] != value:
                    raise CliError(
                        "confirmation.conflict",
                        "Confirmation paths contain conflicting values",
                        ExitCode.VALIDATION,
                    )
                current[position] = value
                continue
            expected = [] if next_is_index else {}
            if current[position] is None:
                current[position] = expected
            if not isinstance(current[position], type(expected)):
                raise CliError(
                    "confirmation.path_conflict",
                    "Confirmation paths contain incompatible structures",
                    ExitCode.VALIDATION,
                )
            current = current[position]
            continue
        raise CliError(
            "confirmation.invalid_path",
            "Confirmation paths must address manifest fields",
            ExitCode.VALIDATION,
        )


def _repository_key(repository: RepositoryDiscovery, index: int, values: Mapping[str, Any]) -> str:
    if repository.name.classification is Classification.CONFIRMED:
        key = repository.name.value
    else:
        key = values.get(f"repository_keys.{index}")
    if not isinstance(key, str) or not key or "." in key:
        raise CliError(
            "confirmation.incomplete",
            "Every repository requires a confirmed key without dots",
            ExitCode.VALIDATION,
        )
    return key


def _authoritative_repository_values(
    discovery: DiscoveryDocument,
    values: Mapping[str, Any],
) -> tuple[dict[str, Any], set[str]]:
    repositories: dict[str, Any] = {}
    consumed: set[str] = set()
    for index, repository in enumerate(discovery.repositories):
        key_path = f"repository_keys.{index}"
        key = _repository_key(repository, index, values)
        if key_path in values:
            consumed.add(key_path)
        if key in repositories:
            raise CliError(
                "confirmation.duplicate_repository_key",
                "Confirmed repository keys must be unique",
                ExitCode.VALIDATION,
            )
        remote = repository.remote.value
        slug = _github_slug(remote) if isinstance(remote, str) else None
        if slug is None:
            github_path = f"repositories.{key}.github"
            slug = values.get(github_path)
            if github_path in values:
                consumed.add(github_path)
        if not isinstance(slug, str):
            raise CliError(
                "confirmation.incomplete",
                "Every repository requires a confirmed GitHub slug",
                ExitCode.VALIDATION,
            )
        repositories[key] = {
            "github": slug,
            "local_path": repository.root.value,
        }
    return repositories, consumed


def _manifest_document(
    discovery: DiscoveryDocument,
    confirmation: ConfirmationDocument,
) -> dict[str, Any]:
    if confirmation.discovery_digest != discovery.discovery_digest:
        raise CliError(
            "confirmation.discovery_drift",
            "Confirmation does not match the current discovery document",
            ExitCode.DRIFT,
        )
    repositories, consumed = _authoritative_repository_values(discovery, confirmation.values)
    document: dict[str, Any] = {
        "schema_version": 1,
        "policies": {
            "deployment": "forbidden",
            "max_repair_attempts": 2,
            "watcher_cron": "*/30 * * * *",
        },
        "repositories": repositories,
    }
    for path, value in confirmation.values.items():
        if path in consumed:
            continue
        _set_path(document, path, value)

    role_skills = document.get("role_skills")
    if not isinstance(role_skills, dict) or set(role_skills) != _FIXED_ROLE_NAMES:
        raise CliError(
            "confirmation.incomplete",
            "Every fixed role requires an explicit Skill binding",
            ExitCode.VALIDATION,
        )
    for repository_name, repository in repositories.items():
        secret_env = repository.get("secret_env", {})
        if not isinstance(secret_env, dict):
            raise CliError(
                "confirmation.invalid_secret_name",
                "secret_env must be a mapping of environment names",
                ExitCode.VALIDATION,
            )
        for secret_name in secret_env:
            if _SECRET_NAME.fullmatch(secret_name) is None:
                raise CliError(
                    "confirmation.invalid_secret_name",
                    "Secret environment names must use uppercase identifier syntax",
                    ExitCode.VALIDATION,
                )
    try:
        rendered = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        load_manifest_text(rendered, strict_commands=True)
    except (ManifestError, yaml.YAMLError):
        raise CliError(
            "confirmation.incomplete",
            "Confirmed values do not form a complete valid delivery manifest",
            ExitCode.VALIDATION,
        ) from None
    return document


def _environment_example(document: Mapping[str, Any]) -> bytes:
    names: set[str] = set()
    for repository in document["repositories"].values():
        names.update(repository.get("secret_env", {}))
    return "".join(f"{name}=\n" for name in sorted(names)).encode("utf-8")


def initialize_scaffold(
    discovery: DiscoveryDocument,
    confirmations: ConfirmationDocument,
    target: Path,
) -> tuple[Path, ...]:
    destination = Path(target)
    if not destination.is_absolute() or destination != Path(os.path.normpath(destination)):
        raise CliError(
            "init.invalid_target",
            "The scaffold target must be an absolute normalized path",
            ExitCode.VALIDATION,
        )
    if destination.exists():
        raise CliError(
            "init.target_exists",
            "The scaffold target already exists",
            ExitCode.VALIDATION,
        )
    if not destination.parent.is_dir() or destination.parent.resolve() != destination.parent:
        raise CliError(
            "init.invalid_parent",
            "The scaffold parent must be an existing non-aliased directory",
            ExitCode.VALIDATION,
        )

    document = _manifest_document(discovery, confirmations)
    manifest_bytes = yaml.safe_dump(
        document,
        sort_keys=False,
        allow_unicode=True,
    ).encode("utf-8")
    artifacts = {
        "delivery.yaml": manifest_bytes,
        "framework.lock": _LOCK_BYTES,
        "env.example": _environment_example(document),
    }

    try:
        destination.mkdir(mode=0o700)
    except OSError:
        raise CliError(
            "init.concurrent_target",
            "The scaffold target changed during initialization",
            ExitCode.VALIDATION,
        ) from None

    created: list[Path] = []
    try:
        for filename, content in artifacts.items():
            artifact = destination / filename
            atomic_write_new(artifact, content)
            created.append(artifact)
    except OSError:
        for artifact in reversed(created):
            artifact.unlink(missing_ok=True)
        try:
            destination.rmdir()
        except OSError:
            pass
        raise CliError(
            "init.concurrent_target",
            "The scaffold target changed during initialization",
            ExitCode.VALIDATION,
        ) from None
    return tuple(destination / name for name in artifacts)

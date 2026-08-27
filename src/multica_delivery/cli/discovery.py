"""Read-only, evidence-classified repository discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from .errors import CliError, ExitCode


class Classification(str, Enum):
    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


def _json_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in sorted(value.items())}
    return value


@dataclass(frozen=True)
class Finding:
    value: Any
    classification: Classification
    source: str | None

    def to_value(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "source": self.source,
            "value": _json_value(self.value),
        }


def _unknown() -> Finding:
    return Finding(None, Classification.UNKNOWN, None)


@dataclass(frozen=True)
class RepositoryDiscovery:
    root: Finding
    kind: Finding
    name: Finding
    remote: Finding
    commands: Mapping[str, Finding]
    ports: Mapping[str, Finding]
    dependencies: Finding = field(default_factory=_unknown)
    project: Finding = field(default_factory=_unknown)
    skills: Finding = field(default_factory=_unknown)
    agents_instructions: Finding = field(default_factory=_unknown)

    def to_value(self) -> dict[str, Any]:
        return {
            "root": self.root.to_value(),
            "kind": self.kind.to_value(),
            "name": self.name.to_value(),
            "remote": self.remote.to_value(),
            "commands": {
                name: finding.to_value() for name, finding in sorted(self.commands.items())
            },
            "ports": {
                name: finding.to_value() for name, finding in sorted(self.ports.items())
            },
            "dependencies": self.dependencies.to_value(),
            "project": self.project.to_value(),
            "skills": self.skills.to_value(),
            "agents_instructions": self.agents_instructions.to_value(),
        }


@dataclass(frozen=True)
class DiscoveryDocument:
    repositories: tuple[RepositoryDiscovery, ...]
    runtime_id: Finding = field(default_factory=_unknown)
    daemon_id: Finding = field(default_factory=_unknown)
    schema_version: int = 1

    def _digest_value(self) -> dict[str, Any]:
        repositories = sorted(
            (repository.to_value() for repository in self.repositories),
            key=lambda item: item["root"]["value"],
        )
        return {
            "schema_version": self.schema_version,
            "repositories": repositories,
            "runtime_id": self.runtime_id.to_value(),
            "daemon_id": self.daemon_id.to_value(),
        }

    @property
    def discovery_digest(self) -> str:
        encoded = json.dumps(
            self._digest_value(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_value(self) -> dict[str, Any]:
        value = self._digest_value()
        value["repositories"] = [repository.to_value() for repository in self.repositories]
        value["discovery_digest"] = self.discovery_digest
        return value


class RepositoryReader(Protocol):
    """Closed local read boundary; deliberately has no execution or write method."""

    def canonicalize(self, path: Path) -> Path: ...

    def object_type(self, path: Path) -> str: ...

    def read_optional(self, path: Path) -> bytes | None: ...


class LocalRepositoryReader:
    """Filesystem-only implementation of the closed repository reader."""

    _MAX_FILE_BYTES = 1_048_576

    def canonicalize(self, path: Path) -> Path:
        return path.resolve(strict=False)

    def object_type(self, path: Path) -> str:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return "missing"
        if stat.S_ISDIR(mode):
            return "directory"
        if stat.S_ISREG(mode):
            return "file"
        if stat.S_ISLNK(mode):
            return "symlink"
        return "unsupported"

    def read_optional(self, path: Path) -> bytes | None:
        object_type = self.object_type(path)
        if object_type == "missing":
            return None
        if object_type != "file":
            raise CliError(
                "discovery.unsupported_object",
                "Discovery encountered an unsupported repository object",
                ExitCode.VALIDATION,
            )
        if path.stat().st_size > self._MAX_FILE_BYTES:
            raise CliError(
                "discovery.file_too_large",
                "A discovery input exceeds the supported size limit",
                ExitCode.VALIDATION,
            )
        return path.read_bytes()


def _stable_read(reader: RepositoryReader, path: Path) -> bytes | None:
    first = reader.read_optional(path)
    second = reader.read_optional(path)
    if first != second:
        raise CliError(
            "discovery.changing_read",
            "Repository inputs changed during discovery; retry from a stable checkout",
            ExitCode.VALIDATION,
        )
    return first


def _validated_roots(paths: Sequence[Path], reader: RepositoryReader) -> tuple[Path, ...]:
    if not paths:
        raise CliError(
            "discovery.no_roots",
            "At least one repository root is required",
            ExitCode.VALIDATION,
        )
    roots: list[Path] = []
    seen: set[Path] = set()
    for supplied in paths:
        path = Path(supplied)
        if not path.is_absolute():
            raise CliError(
                "discovery.path_not_absolute",
                "Repository roots must be absolute paths",
                ExitCode.VALIDATION,
            )
        normalized = Path(os.path.normpath(os.fspath(path)))
        if normalized != path:
            raise CliError(
                "discovery.path_not_normalized",
                "Repository roots must be normalized paths",
                ExitCode.VALIDATION,
            )
        canonical = reader.canonicalize(path)
        if canonical != normalized:
            raise CliError(
                "discovery.aliased_root",
                "Repository roots must not use aliases or symbolic links",
                ExitCode.VALIDATION,
            )
        if reader.object_type(canonical) != "directory":
            raise CliError(
                "discovery.unsupported_root",
                "Every repository root must be an existing directory",
                ExitCode.VALIDATION,
            )
        if canonical in seen:
            raise CliError(
                "discovery.duplicate_root",
                "Repository roots must be unique",
                ExitCode.VALIDATION,
            )
        seen.add(canonical)
        roots.append(canonical)

    for index, root in enumerate(roots):
        for other in roots[index + 1 :]:
            if root.is_relative_to(other) or other.is_relative_to(root):
                raise CliError(
                    "discovery.nested_root",
                    "Selected repository roots must not contain one another",
                    ExitCode.VALIDATION,
                )
    return tuple(roots)


def _decode_json(content: bytes, source: str) -> Mapping[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CliError(
            "discovery.invalid_json",
            f"Discovery could not parse {source}",
            ExitCode.VALIDATION,
        ) from None
    if not isinstance(value, dict):
        raise CliError(
            "discovery.invalid_json_object",
            f"Discovery requires {source} to contain an object",
            ExitCode.VALIDATION,
        )
    return value


def _package_findings(root: Path, content: bytes) -> tuple[Finding, Finding, dict[str, Finding], dict[str, Finding]]:
    package = _decode_json(content, "package.json")
    source = str(root / "package.json")
    raw_name = package.get("name")
    name = (
        Finding(raw_name, Classification.CONFIRMED, f"{source}#/name")
        if isinstance(raw_name, str) and raw_name
        else _unknown()
    )
    commands: dict[str, Finding] = {}
    ports: dict[str, Finding] = {}
    scripts = package.get("scripts", {})
    if not isinstance(scripts, dict):
        raise CliError(
            "discovery.invalid_scripts",
            "package.json scripts must be an object",
            ExitCode.VALIDATION,
        )
    for script_name, script_value in sorted(scripts.items()):
        if not isinstance(script_name, str) or not isinstance(script_value, str):
            raise CliError(
                "discovery.invalid_script",
                "package.json scripts must map names to strings",
                ExitCode.VALIDATION,
            )
        command = ("npm", script_name) if script_name == "test" else ("npm", "run", script_name)
        script_source = f"{source}#/scripts/{script_name}"
        commands[script_name] = Finding(command, Classification.INFERRED, script_source)
        match = re.search(r"(?:--port(?:=|\s+)|\bPORT=)(\d{2,5})\b", script_value)
        if match:
            ports[script_name] = Finding(
                int(match.group(1)),
                Classification.INFERRED,
                script_source,
            )
    return Finding("node", Classification.CONFIRMED, source), name, commands, ports


def _maven_findings(root: Path, pom: bytes, wrapper: bytes | None) -> tuple[Finding, Finding, dict[str, Finding]]:
    source = str(root / "pom.xml")
    text = pom.decode("utf-8", errors="strict")
    artifact = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", text)
    name = (
        Finding(artifact.group(1), Classification.CONFIRMED, f"{source}#/project/artifactId")
        if artifact
        else _unknown()
    )
    executable = "./mvnw" if wrapper is not None else "mvn"
    commands = {
        "test": Finding(
            (executable, "test"),
            Classification.INFERRED,
            source,
        )
    }
    return Finding("maven", Classification.CONFIRMED, source), name, commands


def _remote_finding(root: Path, config: bytes | None) -> Finding:
    if config is None:
        return _unknown()
    source = str(root / ".git" / "config")
    text = config.decode("utf-8", errors="strict")
    match = re.search(r"^\s*url\s*=\s*(\S+)\s*$", text, re.MULTILINE)
    if not match:
        return _unknown()
    return Finding(match.group(1), Classification.CONFIRMED, source)


def _discover_repository(root: Path, reader: RepositoryReader) -> RepositoryDiscovery:
    package = _stable_read(reader, root / "package.json")
    pom = _stable_read(reader, root / "pom.xml")
    wrapper = _stable_read(reader, root / "mvnw")
    agents = _stable_read(reader, root / "AGENTS.md")
    git_config = _stable_read(reader, root / ".git" / "config")

    if package is not None and pom is not None:
        kind = Finding("mixed", Classification.INFERRED, f"{root}/package.json + {root}/pom.xml")
        node_kind, name, commands, ports = _package_findings(root, package)
        _, maven_name, maven_commands = _maven_findings(root, pom, wrapper)
        if name.classification is Classification.UNKNOWN:
            name = maven_name
        for command_name, finding in maven_commands.items():
            commands.setdefault(f"maven-{command_name}", finding)
    elif package is not None:
        kind, name, commands, ports = _package_findings(root, package)
    elif pom is not None:
        kind, name, commands = _maven_findings(root, pom, wrapper)
        ports = {}
    else:
        kind = _unknown()
        name = _unknown()
        commands = {}
        ports = {}

    agents_finding = (
        Finding(True, Classification.CONFIRMED, str(root / "AGENTS.md"))
        if agents is not None
        else _unknown()
    )
    return RepositoryDiscovery(
        root=Finding(str(root), Classification.CONFIRMED, "operator argument"),
        kind=kind,
        name=name,
        remote=_remote_finding(root, git_config),
        commands=MappingProxyType(commands),
        ports=MappingProxyType(ports),
        agents_instructions=agents_finding,
    )


def discover_repositories(
    paths: Sequence[Path],
    reader: RepositoryReader,
) -> DiscoveryDocument:
    roots = _validated_roots(paths, reader)
    return DiscoveryDocument(tuple(_discover_repository(root, reader) for root in roots))

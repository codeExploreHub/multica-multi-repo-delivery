"""Immutable values decoded from delivery manifests and framework locks."""

from dataclasses import dataclass, field
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_GITHUB_COMPONENT = re.compile(r"[A-Za-z0-9._-]+\Z")
_COMMIT_REF = re.compile(r"[0-9a-f]{40}\Z")
_CASE_INSENSITIVE_COMMIT_REF = re.compile(r"[0-9A-Fa-f]{40}\Z")


def parse_github_skill_url(value: Any) -> dict[str, str]:
    """Parse one canonical public GitHub tree URL without normalization."""

    if not isinstance(value, str) or not value:
        raise ValueError("Skill URL must be a canonical public GitHub tree URL")
    parsed = urlsplit(value)
    parts = parsed.path.split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or len(parts) < 6
        or parts[0] != ""
        or any(
            not part
            or part in {".", ".."}
            or _GITHUB_COMPONENT.fullmatch(part) is None
            for part in parts[1:]
        )
        or parts[3] != "tree"
    ):
        raise ValueError("Skill URL must be a canonical public GitHub tree URL")
    owner, repo, ref = parts[1], parts[2], parts[4]
    path = "/".join(parts[5:])
    if (
        _CASE_INSENSITIVE_COMMIT_REF.fullmatch(ref) is not None
        and _COMMIT_REF.fullmatch(ref) is None
    ):
        raise ValueError("Skill URL must be a canonical public GitHub tree URL")
    canonical = f"https://github.com/{owner}/{repo}/tree/{ref}/{path}"
    if value != canonical:
        raise ValueError("Skill URL must be a canonical public GitHub tree URL")
    return {
        "owner": owner,
        "repo": repo,
        "ref": ref,
        "path": path,
        "source_url": value,
    }


def github_skill_origin_matches(
    desired_url: Any,
    observed_origin: Any,
) -> bool:
    """Compare a desired GitHub Skill URL with structured Multica evidence."""

    if not isinstance(observed_origin, Mapping):
        return False
    required = ("type", "owner", "repo", "ref", "path", "source_url")
    if any(
        not isinstance(observed_origin.get(field), str)
        or not observed_origin[field]
        for field in required
    ):
        return False
    if observed_origin["type"] != "github":
        return False
    try:
        desired = parse_github_skill_url(desired_url)
        observed = parse_github_skill_url(observed_origin["source_url"])
    except ValueError:
        return False
    if any(
        observed_origin[field] != observed[field]
        for field in ("owner", "repo", "ref", "path")
    ):
        return False
    if any(
        desired[field] != observed[field]
        for field in ("owner", "repo", "path")
    ):
        return False
    desired_ref = desired["ref"]
    observed_ref = observed["ref"]
    if _COMMIT_REF.fullmatch(desired_ref) is not None:
        return observed_ref == desired_ref
    return (
        observed_ref == desired_ref
        or _COMMIT_REF.fullmatch(observed_ref) is not None
    )


@dataclass(frozen=True)
class InstanceSpec:
    key: str
    display_name: str
    runtime_id: str
    daemon_id: str
    control_project: str


@dataclass(frozen=True)
class ControlSpec:
    github: str
    local_path: Path


@dataclass(frozen=True)
class SkillSource:
    key: str
    url: str
    approved: bool


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    port: int
    health_url: str


@dataclass(frozen=True)
class SecretEnvSpec:
    name: str
    recipients: tuple[str, ...]


@dataclass(frozen=True)
class RepositorySpec:
    key: str
    github: str
    project_title: str
    local_path: Path
    default_branch: str
    depends_on: tuple[str, ...]
    commands: Mapping[str, tuple[str, ...]]
    skills: tuple[str, ...]
    description: str = ""
    services: tuple[ServiceSpec, ...] = ()
    secret_env: Mapping[str, SecretEnvSpec] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True)
class IntegrationSuiteSpec:
    key: str
    repositories: tuple[str, ...]
    start_order: tuple[str, ...]
    command_repository: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class PolicySpec:
    environment: str
    automatic_merge: bool
    deployment: str
    max_repair_attempts: int
    watcher_cron: str
    watcher_timezone: str

    def __post_init__(self) -> None:
        validate_policy_authority(self)


def validate_policy_authority(policy: object) -> None:
    """Validate every fixed policy authority, including forged dataclasses."""

    if type(policy) is not PolicySpec:
        raise ValueError("policy must be an exact PolicySpec")
    if (
        not isinstance(policy.environment, str)
        or policy.environment not in {"development", "production"}
    ):
        raise ValueError("policy environment must be development or production")
    if type(policy.automatic_merge) is not bool:
        raise ValueError("policy automatic_merge must be a boolean")
    if policy.environment != "development" and policy.automatic_merge:
        raise ValueError("policy automatic_merge is allowed only in development")
    if type(policy.deployment) is not str or policy.deployment != "forbidden":
        raise ValueError("policy deployment must be forbidden")
    if (
        type(policy.max_repair_attempts) is not int
        or policy.max_repair_attempts != 2
    ):
        raise ValueError("policy max_repair_attempts must be exactly 2")
    if (
        type(policy.watcher_cron) is not str
        or policy.watcher_cron != "*/30 * * * *"
    ):
        raise ValueError("policy watcher_cron must be the approved 30-minute schedule")
    if type(policy.watcher_timezone) is not str or not policy.watcher_timezone:
        raise ValueError("policy watcher_timezone must be a valid IANA timezone")
    try:
        ZoneInfo(policy.watcher_timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError(
            "policy watcher_timezone must be a valid IANA timezone"
        ) from None


@dataclass(frozen=True)
class DeliveryManifest:
    schema_version: int
    instance: InstanceSpec
    control: ControlSpec
    skill_registry: Mapping[str, SkillSource]
    repositories: Mapping[str, RepositorySpec]
    integration_suites: tuple[IntegrationSuiteSpec, ...]
    policy: PolicySpec
    merge_order: tuple[str, ...]
    role_skills: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True)
class FrameworkLock:
    skill_version: str
    engine_version: str
    manifest_schema_version: int
    workflow_metadata_version: int
    supported_multica_cli: str
    manifest_digest: str
    resource_ids: Mapping[str, Mapping[str, str]]

    @classmethod
    def empty(cls) -> "FrameworkLock":
        return cls("", "", 1, 1, "", "", MappingProxyType({}))

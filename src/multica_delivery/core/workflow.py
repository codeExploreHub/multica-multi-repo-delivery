"""Effect orchestration around the pure exact-SHA parent decision engine."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
import hashlib
import re
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlsplit
import uuid

from .decisions import (
    DecisionKind,
    DispatchKind,
    GateEvidence,
    ParentDecision,
    ParentSnapshot,
    PullRequestEvidence,
    RepositoryEvidence,
    SmokeRead,
    decide_parent_action,
)
from ..adapters.github_client import (
    GitHubBoundaryError,
    MergeResult,
    PullRequestInfo,
    RequiredStatusChecks,
)
from ..adapters.exact_sha import (
    ExactShaBoundaryError,
    ExactShaCommandResult,
    ExactShaVerification,
    LocalExactShaCommandRunner,
)
from .metadata import LegacyParentMetadataV1, MetadataError, ParentMetadata, canonical_json
from .model import DeliveryManifest, validate_policy_authority
from ..adapters.processes import OwnedProcess, ProcessManager, ProcessOwnershipError, ProcessRun
from .topology import TopologyError, merge_order


_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_STABLE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*\Z")
_ISSUE_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*-[1-9][0-9]*\Z")
_SMOKE_OBSERVATION_ID = re.compile(r"smoke:[0-9a-f]{64}\Z")
_ACTION_KEY = re.compile(
    r"(?:dispatch|stage|review|qa|merge|repair|smoke|resume|recovery):[0-9a-f]{64}\Z"
)
_PHASES = frozenset({"implementation", "review", "qa", "integration_qa", "repair", "smoke"})
_PHASE_RESULTS = frozenset({"pass", "fail", "blocked"})
_EVIDENCE_RESULTS = frozenset({"pending", "pass", "fail", "blocked"})
_MERGE_STATES = frozenset(
    {"pending", "not_ready", "ready", "merging", "merged", "partial", "blocked"}
)
_PARENT_STATUSES = frozenset(
    {"backlog", "todo", "in_progress", "in_review", "done", "blocked", "cancelled"}
)
_CHILD_STATUSES = frozenset(
    {"backlog", "todo", "in_progress", "in_review", "done", "blocked", "cancelled"}
)
_ACTIVE_PARENT_STATUSES = frozenset({"todo", "in_progress", "in_review"})
_ACTIVE_CHILD_STATUSES = frozenset({"todo", "in_progress", "in_review"})
_TERMINAL_CHILD_STATUSES = frozenset({"done", "blocked", "cancelled"})
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))
_FUTURE_CHILD_RELATIONSHIP = "workflow child relationship is ahead of parent metadata"
_UNINITIALIZED_PARENT_STATE = "parent has no initialized workflow metadata"
_WRONG_PARENT_STATE = "authoritative parent read returned the wrong parent"
_OUTSIDE_PROJECT_STATE = "parent is outside the configured instance projects"
_LEGACY_PARENT_STATE = "version one parent requires explicit metadata migration"
_SMOKE_PERSISTENCE_AUTHORITY = object()


class WorkflowError(RuntimeError):
    """A fail-closed, non-secret workflow boundary failure."""


def _stable(value: object, field_name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (
        (not empty and not value)
        or (value and _STABLE_KEY.fullmatch(value) is None)
    ):
        raise WorkflowError(f"{field_name} must be a stable key")
    return value


def _valid_sha(value: object) -> bool:
    return type(value) is str and _SHA.fullmatch(value) is not None


def _valid_digest(value: object) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _exact_stable(value: object, *, empty: bool = False) -> bool:
    return type(value) is str and (
        (empty and value == "")
        or (bool(value) and _STABLE_KEY.fullmatch(value) is not None)
    )


def _canonical_uuid(value: object, *, empty: bool = False) -> bool:
    if empty and value == "":
        return True
    if type(value) is not str:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (AttributeError, TypeError, ValueError):
        return False


def _exact_mapping(
    value: object,
    value_problem: Callable[[object], bool],
) -> bool:
    return (
        type(value) is _MAPPING_PROXY_TYPE
        and all(_exact_stable(key) and value_problem(item) for key, item in value.items())
    )


def _frozen_candidate_shas(value: object, field_name: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise WorkflowError(f"{field_name} is malformed")
    candidates = dict(value)
    if not candidates or any(
        not _exact_stable(repository) or not _valid_sha(candidate_sha)
        for repository, candidate_sha in candidates.items()
    ):
        raise WorkflowError(f"{field_name} is malformed")
    return MappingProxyType(dict(sorted(candidates.items())))


def _exact_repository_tuple(value: object, field_name: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if (
        type(value) is not tuple
        or (nonempty and not value)
        or any(not _exact_stable(repository) for repository in value)
        or len(set(value)) != len(value)
    ):
        raise WorkflowError(f"{field_name} is malformed")
    return tuple(sorted(value))


def _https_evidence_url(value: object) -> bool:
    if type(value) is not str:
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and bool(parsed.path)
    )


def _phase_completion_schema_problem(
    completion: object,
    *,
    manifest: DeliveryManifest,
) -> str | None:
    try:
        max_attempt = manifest.policy.max_repair_attempts + 1
        if type(completion) is not PhaseCompletion:
            return "phase completion is malformed"
        if (
            type(completion.parent_identifier) is not str
            or _ISSUE_IDENTIFIER.fullmatch(completion.parent_identifier) is None
            or not _exact_stable(completion.repository_key)
            or type(completion.phase) is not str
            or completion.phase not in _PHASES - {"smoke"}
            or type(completion.result) is not str
            or completion.result not in _PHASE_RESULTS
            or type(completion.attempt) is not int
            or not 0 <= completion.attempt <= max_attempt
            or not _valid_sha(completion.candidate_sha)
            or not _canonical_uuid(completion.evidence_comment_uuid)
            or not _exact_stable(completion.suite_key, empty=True)
            or not _exact_mapping(completion.candidate_shas, _valid_sha)
            or type(completion.responsible_repositories) is not tuple
            or any(not _exact_stable(repository) for repository in completion.responsible_repositories)
            or len(set(completion.responsible_repositories)) != len(completion.responsible_repositories)
            or type(completion.failure_bundle_digest) is not str
            or (completion.failure_bundle_digest and not _valid_digest(completion.failure_bundle_digest))
        ):
            return "phase completion is malformed"

        evidence_url = urlsplit(completion.evidence_comment_url)
        if (
            type(completion.evidence_comment_url) is not str
            or evidence_url.scheme != "https"
            or not evidence_url.netloc
            or evidence_url.username is not None
            or evidence_url.password is not None
            or evidence_url.query
            or evidence_url.fragment
            or not evidence_url.path
        ):
            return "phase completion evidence is malformed"

        if type(completion.pull_request_url) is not str:
            return "phase completion pull request is malformed"
        if completion.pull_request_url:
            pull_request_url = urlsplit(completion.pull_request_url)
            pull_parts = pull_request_url.path.split("/")
            if (
                pull_request_url.scheme != "https"
                or pull_request_url.netloc != "github.com"
                or pull_request_url.username is not None
                or pull_request_url.password is not None
                or pull_request_url.query
                or pull_request_url.fragment
                or len(pull_parts) != 5
                or pull_parts[0] != ""
                or not _exact_stable(pull_parts[1])
                or not _exact_stable(pull_parts[2])
                or pull_parts[3] != "pull"
                or not pull_parts[4].isdigit()
                or pull_parts[4].startswith("0")
            ):
                return "phase completion pull request is malformed"
        elif completion.phase in {"implementation", "repair"}:
            return "phase completion pull request is malformed"

        if completion.phase == "integration_qa":
            if not completion.suite_key or not completion.candidate_shas:
                return "phase completion integration identity is malformed"
        elif completion.suite_key or completion.candidate_shas:
            return "phase completion repository identity is malformed"

        gate_phase = completion.phase in {"review", "qa", "integration_qa"}
        if completion.result == "pass" and gate_phase and completion.responsible_repositories:
            return "PASS gate completion cannot name responsible repositories"
        if completion.result != "pass" and gate_phase and not completion.responsible_repositories:
            return "non-PASS gate completion requires responsible repositories"
        if completion.phase in {"review", "qa"} and (
            completion.responsible_repositories
            and completion.responsible_repositories != (completion.repository_key,)
        ):
            return "repository gate completion can only name its repository"
        if completion.phase == "integration_qa" and completion.responsible_repositories:
            suite = next(
                (item for item in manifest.integration_suites if item.key == completion.suite_key),
                None,
            )
            if suite is None:
                return "phase completion integration identity is malformed"
            if not set(completion.responsible_repositories) <= set(suite.repositories):
                return "integration QA completion responsible repositories are outside its suite"
        if completion.phase in {"implementation", "repair"} and completion.responsible_repositories:
            return "implementation and repair completions cannot name responsible repositories"
        if completion.phase == "repair":
            if not _valid_digest(completion.failure_bundle_digest):
                return "repair completion requires a failure bundle digest"
        elif completion.failure_bundle_digest:
            return "non-repair completion cannot contain a failure bundle digest"
    except BaseException:
        return "phase completion is malformed"
    return None


def _smoke_read_schema_problem(smoke_read: object) -> str | None:
    try:
        if (
            type(smoke_read) is not SmokeRead
            or type(smoke_read.observation_id) is not str
            or _SMOKE_OBSERVATION_ID.fullmatch(smoke_read.observation_id) is None
            or not _exact_mapping(smoke_read.merged_shas, _valid_sha)
            or not _exact_mapping(smoke_read.checkout_shas, _valid_sha)
            or not _exact_mapping(
                smoke_read.repository_results,
                lambda result: type(result) is str and result in _EVIDENCE_RESULTS,
            )
            or not _exact_mapping(
                smoke_read.integration_results,
                lambda result: type(result) is str and result in _EVIDENCE_RESULTS,
            )
            or type(smoke_read.authoritative) is not bool
        ):
            return "smoke read is malformed"
    except BaseException:
        return "smoke read is malformed"
    return None


@dataclass(frozen=True)
class PullRequestTarget:
    repository_key: str
    number: int
    url: str

    def __post_init__(self) -> None:
        _stable(self.repository_key, "repository_key")
        if not isinstance(self.number, int) or isinstance(self.number, bool) or self.number < 1:
            raise WorkflowError("pull request number must be a positive integer")
        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or parsed.query
            or parsed.fragment
            or not parsed.path.endswith(f"/pull/{self.number}")
        ):
            raise WorkflowError("pull request URL is malformed")


@dataclass(frozen=True)
class FailureEvidenceRef:
    child_identifier: str
    phase: str
    result: str
    stage_ordinal: int
    repair_round: int
    candidate_shas: Mapping[str, str]
    responsible_repositories: tuple[str, ...]
    evidence_comment_uuid: str
    evidence_comment_url: str
    suite_key: str = ""

    def __post_init__(self) -> None:
        if (
            not _exact_stable(self.child_identifier)
            or type(self.phase) is not str
            or self.phase not in {"review", "qa", "integration_qa"}
            or type(self.result) is not str
            or self.result not in _PHASE_RESULTS - {"pass"}
            or type(self.stage_ordinal) is not int
            or self.stage_ordinal < 0
            or type(self.repair_round) is not int
            or self.repair_round < 0
            or not _canonical_uuid(self.evidence_comment_uuid)
            or not _https_evidence_url(self.evidence_comment_url)
            or not _exact_stable(self.suite_key, empty=True)
            or (self.phase == "integration_qa") != bool(self.suite_key)
        ):
            raise WorkflowError("failure evidence is malformed")
        candidates = _frozen_candidate_shas(self.candidate_shas, "failure candidate SHA map")
        owners = _exact_repository_tuple(
            self.responsible_repositories,
            "failure responsible repositories",
            nonempty=True,
        )
        object.__setattr__(self, "candidate_shas", candidates)
        object.__setattr__(self, "responsible_repositories", owners)

    def to_canonical_dict(self):
        return {
            "candidate_shas": dict(self.candidate_shas),
            "child_identifier": self.child_identifier,
            "evidence_comment_url": self.evidence_comment_url,
            "evidence_comment_uuid": self.evidence_comment_uuid,
            "phase": self.phase,
            "repair_round": self.repair_round,
            "responsible_repositories": list(self.responsible_repositories),
            "result": self.result,
            "stage_ordinal": self.stage_ordinal,
            "suite_key": self.suite_key,
        }


def _ordered_failures(failures: tuple[FailureEvidenceRef, ...]) -> tuple[FailureEvidenceRef, ...]:
    return tuple(sorted(
        failures,
        key=lambda item: (
            item.responsible_repositories,
            item.phase,
            item.suite_key,
            item.child_identifier,
            item.evidence_comment_uuid,
        ),
    ))


def _failure_bundle_digest(
    parent_identifier: str,
    workflow_version: int,
    source_stage_ordinal: int,
    repair_round: int,
    candidate_shas: Mapping[str, str],
    failures: tuple[FailureEvidenceRef, ...],
) -> str:
    payload = {
        "candidate_shas": dict(sorted(candidate_shas.items())),
        "failures": [failure.to_canonical_dict() for failure in failures],
        "parent_identifier": parent_identifier,
        "repair_round": repair_round,
        "source_stage_ordinal": source_stage_ordinal,
        "workflow_version": workflow_version,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _failure_uuid_partition(
    failures: tuple[FailureEvidenceRef, ...],
) -> tuple[str, ...]:
    return tuple(sorted(failure.evidence_comment_uuid for failure in failures))


@dataclass(frozen=True)
class FailureBundle:
    parent_identifier: str
    workflow_version: int
    source_stage_ordinal: int
    repair_round: int
    candidate_shas: Mapping[str, str]
    failures: tuple[FailureEvidenceRef, ...]
    digest: str

    def __post_init__(self) -> None:
        if (
            type(self.parent_identifier) is not str
            or _ISSUE_IDENTIFIER.fullmatch(self.parent_identifier) is None
            or type(self.workflow_version) is not int
            or self.workflow_version != 2
            or type(self.source_stage_ordinal) is not int
            or self.source_stage_ordinal < 0
            or type(self.repair_round) is not int
            or self.repair_round < 1
            or type(self.failures) is not tuple
            or not self.failures
            or any(type(failure) is not FailureEvidenceRef for failure in self.failures)
        ):
            raise WorkflowError("failure bundle is malformed")
        candidates = _frozen_candidate_shas(self.candidate_shas, "failure bundle candidate SHA map")
        failures = _ordered_failures(self.failures)
        if (
            len({failure.evidence_comment_uuid for failure in failures}) != len(failures)
            or any(
                failure.stage_ordinal != self.source_stage_ordinal
                or failure.repair_round != self.repair_round - 1
                or dict(failure.candidate_shas) != dict(candidates)
                for failure in failures
            )
        ):
            raise WorkflowError("failure bundle evidence is malformed")
        digest = _failure_bundle_digest(
            self.parent_identifier,
            self.workflow_version,
            self.source_stage_ordinal,
            self.repair_round,
            candidates,
            failures,
        )
        if not _valid_digest(self.digest) or self.digest != digest:
            raise WorkflowError("failure bundle digest is malformed")
        object.__setattr__(self, "candidate_shas", candidates)
        object.__setattr__(self, "failures", failures)

    @classmethod
    def build(
        cls,
        parent_identifier,
        workflow_version,
        source_stage_ordinal,
        repair_round,
        candidate_shas,
        failures,
    ):
        if type(failures) is not tuple or any(
            type(failure) is not FailureEvidenceRef for failure in failures
        ):
            raise WorkflowError("failure bundle failures are malformed")
        ordered = _ordered_failures(failures)
        candidates = _frozen_candidate_shas(candidate_shas, "failure bundle candidate SHA map")
        digest = _failure_bundle_digest(
            parent_identifier,
            workflow_version,
            source_stage_ordinal,
            repair_round,
            candidates,
            ordered,
        )
        return cls(
            parent_identifier,
            workflow_version,
            source_stage_ordinal,
            repair_round,
            candidates,
            ordered,
            digest,
        )

    def for_repository(self, repository_key):
        return tuple(sorted(
            (
                failure
                for failure in self.failures
                if repository_key in failure.responsible_repositories
            ),
            key=lambda failure: (
                {"review": 0, "qa": 1, "integration_qa": 2}[failure.phase],
                failure.suite_key,
                failure.child_identifier,
                failure.evidence_comment_uuid,
            ),
        ))


@dataclass(frozen=True)
class WorkflowChild:
    identifier: str
    target_key: str
    repository_key: str
    suite_key: str
    phase: str
    stage_ordinal: int
    attempt: int
    status: str
    action_key: str
    active: bool
    evidence_comment_uuid: str = ""
    creation_candidate_shas: Mapping[str, str] = field(default_factory=dict)
    phase_result: str = ""
    evidence_comment_url: str = ""
    responsible_repositories: tuple[str, ...] = ()
    failure_bundle_digest: str = ""
    failure_evidence_uuids: tuple[str, ...] = ()
    authorizing_comment_uuid: str = ""

    def __post_init__(self) -> None:
        _stable(self.identifier, "child identifier")
        _stable(self.target_key, "child target")
        _stable(self.repository_key, "child repository")
        _stable(self.suite_key, "child suite", empty=True)
        if self.phase not in _PHASES:
            raise WorkflowError("child phase is unsupported")
        for value, name in ((self.stage_ordinal, "stage ordinal"), (self.attempt, "attempt")):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise WorkflowError(f"{name} must be a non-negative integer")
        if self.status not in {"backlog", "todo", "in_progress", "in_review", "done", "blocked", "cancelled"}:
            raise WorkflowError("child status is unsupported")
        if _ACTION_KEY.fullmatch(self.action_key) is None:
            raise WorkflowError("child action key is malformed")
        if not isinstance(self.active, bool):
            raise WorkflowError("child active marker must be boolean")
        if self.evidence_comment_uuid:
            try:
                observed = str(uuid.UUID(self.evidence_comment_uuid))
            except (ValueError, AttributeError, TypeError) as error:
                raise WorkflowError("child evidence UUID is malformed") from error
            if observed != self.evidence_comment_uuid:
                raise WorkflowError("child evidence UUID is malformed")
        candidates = dict(self.creation_candidate_shas)
        if any(
            not _exact_stable(repository) or not _valid_sha(candidate_sha)
            for repository, candidate_sha in candidates.items()
        ):
            raise WorkflowError("child creation candidate SHA map is malformed")
        object.__setattr__(
            self,
            "creation_candidate_shas",
            MappingProxyType(dict(sorted(candidates.items()))),
        )
        if (
            type(self.phase_result) is not str
            or self.phase_result not in _PHASE_RESULTS | {""}
            or (self.evidence_comment_url and not _https_evidence_url(self.evidence_comment_url))
            or (self.failure_bundle_digest and not _valid_digest(self.failure_bundle_digest))
            or type(self.failure_evidence_uuids) is not tuple
            or any(not _canonical_uuid(item) for item in self.failure_evidence_uuids)
            or len(set(self.failure_evidence_uuids)) != len(self.failure_evidence_uuids)
            or not _canonical_uuid(self.authorizing_comment_uuid, empty=True)
        ):
            raise WorkflowError("child failure evidence is malformed")
        owners = _exact_repository_tuple(
            self.responsible_repositories,
            "child responsible repositories",
        )
        object.__setattr__(self, "responsible_repositories", owners)
        object.__setattr__(
            self,
            "failure_evidence_uuids",
            tuple(sorted(self.failure_evidence_uuids)),
        )


@dataclass(frozen=True)
class ChildRequest:
    target_key: str
    repository_key: str
    suite_key: str
    phase: str
    stage_ordinal: int
    attempt: int
    candidate_shas: Mapping[str, str]
    pull_request: PullRequestTarget | None = None
    failure_bundle: FailureBundle | None = None
    failure_refs: tuple[FailureEvidenceRef, ...] = ()
    authorizing_comment_uuid: str = ""

    def __post_init__(self) -> None:
        _stable(self.target_key, "child target")
        _stable(self.repository_key, "child repository")
        _stable(self.suite_key, "child suite", empty=True)
        if self.phase not in _PHASES:
            raise WorkflowError("child request phase is unsupported")
        for value, name in ((self.stage_ordinal, "stage ordinal"), (self.attempt, "attempt")):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise WorkflowError(f"{name} must be a non-negative integer")
        candidates = dict(self.candidate_shas)
        if any(not isinstance(key, str) or not _valid_sha(sha) for key, sha in candidates.items()):
            raise WorkflowError("child candidate SHA map is malformed")
        object.__setattr__(self, "candidate_shas", MappingProxyType(dict(sorted(candidates.items()))))
        if self.phase == "repair" and self.pull_request is None:
            raise WorkflowError("repair must target an existing pull request")
        if self.phase != "repair":
            if (
                self.failure_bundle is not None
                or type(self.failure_refs) is not tuple
                or self.failure_refs != ()
                or self.authorizing_comment_uuid
            ):
                raise WorkflowError("only repair requests can contain failure evidence")
            return
        if (
            type(self.failure_bundle) is not FailureBundle
            or type(self.failure_refs) is not tuple
            or not self.failure_refs
            or any(type(failure) is not FailureEvidenceRef for failure in self.failure_refs)
        ):
            raise WorkflowError("repair requires a complete failure bundle partition")
        expected = self.failure_bundle.for_repository(self.repository_key)
        if (
            self.failure_refs != expected
            or dict(self.candidate_shas) != dict(self.failure_bundle.candidate_shas)
            or self.stage_ordinal != self.failure_bundle.source_stage_ordinal + 1
            or self.attempt != self.failure_bundle.repair_round
            or not _canonical_uuid(self.authorizing_comment_uuid, empty=True)
        ):
            raise WorkflowError("repair failure bundle partition is malformed")


@dataclass(frozen=True)
class PhaseCompletion:
    parent_identifier: str
    repository_key: str
    phase: str
    result: str
    attempt: int
    candidate_sha: str
    pull_request_url: str
    evidence_comment_uuid: str
    evidence_comment_url: str
    suite_key: str = ""
    candidate_shas: Mapping[str, str] = field(default_factory=dict)
    responsible_repositories: tuple[str, ...] = ()
    failure_bundle_digest: str = ""

    def __post_init__(self) -> None:
        candidates = dict(self.candidate_shas)
        object.__setattr__(self, "candidate_shas", MappingProxyType(dict(sorted(candidates.items()))))
        object.__setattr__(
            self,
            "responsible_repositories",
            _exact_repository_tuple(
                self.responsible_repositories,
                "phase completion responsible repositories",
            ),
        )


@dataclass(frozen=True)
class WorkflowState:
    parent_identifier: str
    parent_status: str
    project_key: str
    metadata: ParentMetadata | LegacyParentMetadataV1 | None
    snapshot: ParentSnapshot
    children: tuple[WorkflowChild, ...] = ()
    pull_requests: Mapping[str, PullRequestTarget] = field(default_factory=dict)
    applied_action_keys: frozenset[str] = frozenset()
    human_wait: bool = False
    active_work: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ParentSnapshot):
            raise WorkflowError("workflow state snapshot is malformed")
        if self.metadata is not None and not isinstance(
            self.metadata,
            (ParentMetadata, LegacyParentMetadataV1),
        ):
            raise WorkflowError("workflow state metadata is malformed")
        if not isinstance(self.children, tuple) or any(
            not isinstance(child, WorkflowChild) for child in self.children
        ):
            raise WorkflowError("workflow children are malformed")
        targets = dict(self.pull_requests)
        if any(
            not isinstance(repository, str)
            or not isinstance(target, PullRequestTarget)
            or repository != target.repository_key
            for repository, target in targets.items()
        ):
            raise WorkflowError("workflow pull request targets are malformed")
        object.__setattr__(self, "pull_requests", MappingProxyType(dict(sorted(targets.items()))))
        keys = frozenset(self.applied_action_keys)
        if any(not isinstance(key, str) or _ACTION_KEY.fullmatch(key) is None for key in keys):
            raise WorkflowError("applied action keys are malformed")
        object.__setattr__(self, "applied_action_keys", keys)
        if not isinstance(self.human_wait, bool) or not isinstance(self.active_work, bool):
            raise WorkflowError("workflow wait markers must be boolean")


@dataclass(frozen=True)
class WorkflowResult:
    parent_identifier: str
    parent_status: str
    next_action: str
    reason: str = ""
    created_children: tuple[tuple[str, str], ...] = ()
    completed_child_status: str | None = None
    merge_state: str = "pending"
    action_key: str | None = None
    mutation_count: int = 0
    scanned_parents: int = 0
    recovery_candidates: int = 0


@dataclass(frozen=True)
class _MergePrefixObservation:
    merged_shas: Mapping[str, str]
    remaining: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "merged_shas",
            MappingProxyType(dict(sorted(dict(self.merged_shas).items()))),
        )
        object.__setattr__(self, "remaining", tuple(self.remaining))


class _MergePrefixObservationError(WorkflowError):
    """An authoritative ordered pull-request scan could not be trusted."""


@dataclass(frozen=True)
class _MergeAuthority:
    """The immutable candidate, evidence, and PR-target merge reservation."""

    parent_identifier: str
    affected_repositories: tuple[str, ...]
    candidate_shas: Mapping[str, str]
    child_evidence: Mapping[str, object]
    pull_request_evidence: Mapping[str, object]
    review_evidence: Mapping[str, object]
    qa_evidence: Mapping[str, object]
    integration_evidence: Mapping[str, object]
    smoke_reads: tuple[SmokeRead, ...]
    satisfied_dependencies: frozenset[tuple[str, str]]
    workflow_children: tuple[WorkflowChild, ...]
    pull_request_targets: Mapping[str, PullRequestTarget]
    parent_status: str
    project_key: str
    human_wait: bool
    active_work: bool
    stalled: bool
    stalled_repository: str | None
    recovery_count: int
    workflow_version: int
    metadata_version: int
    instance_key: str
    repository_dag: Mapping[str, tuple[str, ...]]
    contract_hashes: Mapping[str, str]
    attempt: int


@dataclass(frozen=True)
class ScopeResolution:
    """Read-only issue analysis supplied to the generic intake boundary."""

    affected: frozenset[str] | None = None
    affected_candidates: tuple[frozenset[str], ...] = ()
    contract_hashes: Mapping[str, str] = field(default_factory=dict)
    authority_requirements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.affected is not None:
            object.__setattr__(self, "affected", frozenset(self.affected))
        object.__setattr__(
            self,
            "affected_candidates",
            tuple(frozenset(candidate) for candidate in self.affected_candidates),
        )
        object.__setattr__(
            self,
            "contract_hashes",
            MappingProxyType(dict(sorted(dict(self.contract_hashes).items()))),
        )
        if not isinstance(self.authority_requirements, tuple) or any(
            not isinstance(requirement, str) or not requirement
            for requirement in self.authority_requirements
        ):
            raise WorkflowError("authority requirements are malformed")


@dataclass(frozen=True)
class StatusTransition:
    """Authoritative intake authorization persisted by the issue-store boundary."""

    parent_identifier: str
    old_status: str
    new_status: str
    action_key: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.parent_identifier, str)
            or _ISSUE_IDENTIFIER.fullmatch(self.parent_identifier) is None
        ):
            raise WorkflowError("status transition parent is malformed")
        if self.old_status != "backlog" or self.new_status != "todo":
            raise WorkflowError("status transition is not an intake transition")
        if not isinstance(self.action_key, str) or _ACTION_KEY.fullmatch(self.action_key) is None:
            raise WorkflowError("status transition action key is malformed")


@dataclass(frozen=True)
class AuthorizingComment:
    comment_uuid: str
    comment_url: str
    author_type: str

    def __post_init__(self) -> None:
        if (
            not _canonical_uuid(self.comment_uuid)
            or not _https_evidence_url(self.comment_url)
            or not _exact_stable(self.author_type)
        ):
            raise WorkflowError("authorizing comment is malformed")


class ScopeResolver(Protocol):
    def resolve(self, parent_identifier: str) -> ScopeResolution: ...


class WorkflowSnapshotReader(Protocol):
    def read(self, parent_identifier: str) -> WorkflowState: ...

    def read_intake_transition(
        self,
        parent_identifier: str,
    ) -> StatusTransition | None: ...

    def read_phase_completion(
        self,
        parent_identifier: str,
        evidence_comment_uuid: str,
    ) -> PhaseCompletion | None: ...

    def read_authorizing_comment(
        self,
        parent_identifier: str,
        comment_uuid: str,
    ) -> AuthorizingComment: ...

    def list_active_parents(
        self,
        *,
        instance_key: str,
        project_keys: frozenset[str],
        workflow_versions: frozenset[int],
    ) -> tuple[str, ...]: ...


class WorkflowExecutor(Protocol):
    def record_intake_transition(
        self,
        parent_identifier: str,
        old_status: str,
        new_status: str,
        *,
        action_key: str,
    ) -> None: ...

    def initialize_parent(
        self,
        parent_identifier: str,
        metadata: ParentMetadata,
        *,
        action_key: str,
    ) -> None: ...

    def request_human_clarification(
        self,
        parent_identifier: str,
        reason: str,
        *,
        action_key: str,
    ) -> None: ...

    def create_children(
        self,
        parent_identifier: str,
        children: tuple[ChildRequest, ...],
        metadata: ParentMetadata,
        *,
        action_key: str,
    ) -> None: ...

    def write_phase_completion(
        self,
        completion: PhaseCompletion,
        *,
        action_key: str,
    ) -> None: ...

    def mark_child_done(
        self,
        parent_identifier: str,
        completion: PhaseCompletion,
        metadata: ParentMetadata,
        *,
        action_key: str,
    ) -> None:
        """Atomically finish the child and invalidate every gate on SHA replacement."""
        ...

    def set_parent_status(
        self,
        parent_identifier: str,
        status: str,
        reason: str,
        metadata: ParentMetadata,
        *,
        action_key: str,
    ) -> None: ...

    def record_merge_state(
        self,
        parent_identifier: str,
        merge_state: str,
        merged_shas: Mapping[str, str],
        metadata: ParentMetadata,
        *,
        action_key: str,
    ) -> None: ...

    def write_smoke_read(
        self,
        parent_identifier: str,
        smoke_read: SmokeRead,
        metadata: ParentMetadata,
        *,
        action_key: str,
    ) -> None: ...

    def rerun_child(
        self,
        parent_identifier: str,
        child_identifier: str,
        metadata: ParentMetadata,
        *,
        action_key: str,
    ) -> None: ...


class GitHubMergeClient(Protocol):
    def get_pull_request(self, repository: str, number: int) -> PullRequestInfo: ...

    def required_status_checks(
        self,
        repository: str,
        base_ref: str,
        expected_sha: str,
    ) -> RequiredStatusChecks: ...

    def merge_pull_request(
        self,
        repository: str,
        number: int,
        *,
        expected_sha: str,
    ) -> MergeResult: ...


class SmokeExecutor(Protocol):
    def execute(
        self,
        parent_identifier: str,
        repositories: tuple[str, ...],
        merged_shas: Mapping[str, str],
        *,
        action_key: str,
    ) -> SmokeRead: ...


class OwnedSmokeExecutor:
    """Run manifest smoke commands with services owned by one exact-SHA Run."""

    __slots__ = ("_manifest", "_process_manager", "_command_runner")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("owned smoke authority bindings are immutable")

    @property
    def manifest(self) -> DeliveryManifest:
        return self._manifest

    @property
    def process_manager(self) -> ProcessManager:
        return self._process_manager

    def __init__(
        self,
        manifest: DeliveryManifest,
        process_manager: ProcessManager,
        command_runner: LocalExactShaCommandRunner | None = None,
    ) -> None:
        if not isinstance(manifest, DeliveryManifest):
            raise TypeError("manifest must be a DeliveryManifest")
        if (
            type(process_manager) is not ProcessManager
            or process_manager.manifest is not manifest
        ):
            raise TypeError(
                "process_manager must be a concrete manifest-bound ProcessManager"
            )
        validate_policy_authority(manifest.policy)
        runner = (
            command_runner
            if command_runner is not None
            else LocalExactShaCommandRunner(manifest)
        )
        if (
            type(runner) is not LocalExactShaCommandRunner
            or runner.manifest is not manifest
        ):
            raise TypeError("command_runner must be a LocalExactShaCommandRunner")
        object.__setattr__(self, "_manifest", manifest)
        object.__setattr__(self, "_process_manager", process_manager)
        object.__setattr__(self, "_command_runner", runner)

    def _trusted_command_runner(self) -> LocalExactShaCommandRunner:
        runner = self._command_runner
        if (
            type(runner) is not LocalExactShaCommandRunner
            or runner.manifest is not self.manifest
        ):
            raise TypeError("command_runner must be a LocalExactShaCommandRunner")
        return runner

    def _trusted_process_manager(self) -> ProcessManager:
        manager = self._process_manager
        if type(manager) is not ProcessManager or manager.manifest is not self.manifest:
            raise TypeError(
                "process_manager must be a concrete manifest-bound ProcessManager"
            )
        return manager

    def execute(
        self,
        parent_identifier: str,
        repositories: tuple[str, ...],
        merged_shas: Mapping[str, str],
        *,
        action_key: str,
    ) -> SmokeRead:
        command_runner = self._trusted_command_runner()
        process_manager = self._trusted_process_manager()
        selected = tuple(repositories)
        exact = dict(merged_shas)
        if (
            not selected
            or len(set(selected)) != len(selected)
            or set(selected) != set(exact)
            or set(selected) - self.manifest.repositories.keys()
            or any(not _valid_sha(sha) for sha in exact.values())
            or _ACTION_KEY.fullmatch(action_key) is None
        ):
            raise WorkflowError("owned smoke target is malformed")
        try:
            ordered = merge_order(
                self.manifest.repositories,
                frozenset(selected),
                tuple(repository for repository in self.manifest.merge_order if repository in selected),
            )
        except TopologyError as error:
            raise WorkflowError("owned smoke target is not dependency-safe") from error
        applicable_suites = tuple(
            suite
            for suite in self.manifest.integration_suites
            if set(suite.repositories) <= set(selected)
        )
        service_order: list[str] = []
        for suite in applicable_suites:
            for repository in suite.start_order:
                if repository not in service_order:
                    service_order.append(repository)
        for repository in ordered:
            if repository not in service_order:
                service_order.append(repository)

        run_id = f"{parent_identifier}:{action_key}"
        started: list[OwnedProcess] = []
        repository_results = {repository: "pending" for repository in selected}
        integration_results = {suite.key: "pending" for suite in applicable_suites}
        checkout_shas: dict[str, str] = {}
        authoritative = False

        def verify_checkouts() -> tuple[dict[str, str], str | None]:
            observed: dict[str, str] = {}
            for repository in ordered:
                specification = self.manifest.repositories[repository]
                try:
                    verification = command_runner.verify(
                        repository,
                        exact[repository],
                        specification.local_path,
                        argv=("git", "rev-parse", "HEAD"),
                    )
                except ExactShaBoundaryError as error:
                    return observed, error.repository_key
                except Exception:
                    return observed, repository
                if (
                    not isinstance(verification, ExactShaVerification)
                    or verification.repository_key != repository
                    or verification.expected_sha != exact[repository]
                    or verification.argv != ("git", "rev-parse", "HEAD")
                ):
                    return observed, repository
                observed[repository] = verification.observed_sha
            return observed, None

        checkout_shas, checkout_failure = verify_checkouts()
        if checkout_failure in repository_results:
            repository_results[checkout_failure] = "blocked"
        pre_start_bound = checkout_failure is None and checkout_shas == exact
        startup_failed = False
        if pre_start_bound:
            try:
                for repository in service_order:
                    specification = self.manifest.repositories[repository]
                    run = ProcessRun(
                        repository_key=repository,
                        run_id=run_id,
                        argv=specification.commands["start"],
                        cwd=specification.local_path,
                    )
                    if specification.services:
                        owned = process_manager.start_services(
                            specification.services,
                            run,
                            candidate_sha=exact[repository],
                        )
                        started.extend(owned)
                        process_manager.verify_services(
                            specification.services,
                            run,
                            owned,
                            candidate_sha=exact[repository],
                        )
            except (ProcessOwnershipError, OSError, RuntimeError, TypeError, ValueError):
                startup_failed = True
        else:
            startup_failed = True

        if startup_failed:
            repository_results = {
                repository: "blocked" for repository in selected
            }
            integration_results = {
                suite.key: "blocked" for suite in applicable_suites
            }
            authoritative = False

        if not startup_failed:
            checkout_shas, checkout_failure = verify_checkouts()
            if checkout_failure in repository_results:
                repository_results[checkout_failure] = "blocked"
            authoritative = checkout_failure is None and checkout_shas == exact
            if not authoritative:
                startup_failed = True

        if not startup_failed and authoritative:
            command_boundary_failed = False
            for repository in ordered:
                specification = self.manifest.repositories[repository]
                try:
                    command_result = command_runner.run(
                        repository,
                        exact,
                        specification.commands["smoke"],
                        specification.local_path,
                    )
                    if (
                        not isinstance(command_result, ExactShaCommandResult)
                        or dict(command_result.verified_shas) != exact
                    ):
                        raise WorkflowError("exact-SHA command result is malformed")
                    repository_results[repository] = "pass" if command_result.passed else "fail"
                except ExactShaBoundaryError as error:
                    repository_results[repository] = "blocked"
                    if error.repository_key in repository_results:
                        repository_results[error.repository_key] = "blocked"
                        checkout_shas.pop(error.repository_key, None)
                    authoritative = False
                    command_boundary_failed = True
                    break
                except Exception:
                    repository_results[repository] = "blocked"
                    checkout_shas.pop(repository, None)
                    authoritative = False
                    command_boundary_failed = True
                    break
            suites_to_run = () if command_boundary_failed else applicable_suites
            for suite in suites_to_run:
                if any(repository_results[repository] != "pass" for repository in suite.repositories):
                    integration_results[suite.key] = "blocked"
                    continue
                command_repository = self.manifest.repositories[suite.command_repository]
                try:
                    command_result = command_runner.run(
                        suite.command_repository,
                        exact,
                        suite.command,
                        command_repository.local_path,
                    )
                    if (
                        not isinstance(command_result, ExactShaCommandResult)
                        or dict(command_result.verified_shas) != exact
                    ):
                        raise WorkflowError("exact-SHA command result is malformed")
                    integration_results[suite.key] = "pass" if command_result.passed else "fail"
                except ExactShaBoundaryError as error:
                    integration_results[suite.key] = "blocked"
                    if error.repository_key in repository_results:
                        repository_results[error.repository_key] = "blocked"
                        checkout_shas.pop(error.repository_key, None)
                    authoritative = False
                    break
                except Exception:
                    integration_results[suite.key] = "blocked"
                    checkout_shas.pop(suite.command_repository, None)
                    authoritative = False
                    break

        cleanup_failed = False
        stopped_pids: set[int] = set()
        for record in reversed(started):
            if record.pid in stopped_pids:
                continue
            stopped_pids.add(record.pid)
            try:
                process_manager.stop(
                    record,
                    run_id=run_id,
                    repository_key=record.repository_key,
                    candidate_sha=record.candidate_sha,
                )
            except (ProcessOwnershipError, OSError, RuntimeError, TypeError, ValueError):
                cleanup_failed = True
        if cleanup_failed:
            repository_results = {repository: "blocked" for repository in selected}
            integration_results = {suite.key: "blocked" for suite in applicable_suites}
            authoritative = False
        return SmokeRead(
            observation_id=action_key,
            merged_shas=exact,
            checkout_shas=checkout_shas,
            repository_results=repository_results,
            integration_results=integration_results,
            authoritative=authoritative,
        )


def coordinator_action_key(
    *,
    workflow_version: int,
    instance_key: str,
    parent_identifier: str,
    stage_kind: str,
    stage_ordinal: int,
    attempt: int,
    affected_repositories: frozenset[str],
    candidate_shas: Mapping[str, str],
    contract_hashes: Mapping[str, str],
    failure_bundle_digest: str = "",
    authorizing_comment_uuid: str = "",
) -> str:
    """Hash every frozen coordinator input into one stable idempotency key."""

    for value, name, positive in (
        (workflow_version, "workflow_version", True),
        (stage_ordinal, "stage_ordinal", False),
        (attempt, "attempt", False),
    ):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < (1 if positive else 0)
        ):
            raise WorkflowError(f"{name} is malformed")
    _stable(instance_key, "instance_key")
    if not isinstance(parent_identifier, str) or _ISSUE_IDENTIFIER.fullmatch(parent_identifier) is None:
        raise WorkflowError("parent identifier is malformed")
    _stable(stage_kind, "stage_kind")
    if not isinstance(affected_repositories, frozenset) or any(
        not isinstance(repository, str) or not repository
        for repository in affected_repositories
    ):
        raise WorkflowError("affected repository set is malformed")
    candidate_values = dict(candidate_shas)
    contract_values = dict(contract_hashes)
    if set(candidate_values) - affected_repositories or any(
        not _valid_sha(value) for value in candidate_values.values()
    ):
        raise WorkflowError("candidate SHA map is malformed")
    if any(
        not isinstance(key, str) or not key or not _valid_sha(value)
        for key, value in contract_values.items()
    ):
        raise WorkflowError("contract hash map is malformed")
    if not isinstance(failure_bundle_digest, str) or (
        failure_bundle_digest and not _valid_digest(failure_bundle_digest)
    ):
        raise WorkflowError("failure bundle digest is malformed")
    if not _canonical_uuid(authorizing_comment_uuid, empty=True):
        raise WorkflowError("authorizing comment UUID is malformed")
    action_kind = stage_kind.split(":", 1)[0]
    prefix = {
        "implementation": "dispatch",
        "dispatch": "dispatch",
        "gates": "stage",
        "stage": "stage",
        "review": "review",
        "qa": "qa",
        "integration_qa": "qa",
        "merge": "merge",
        "repair": "repair",
        "smoke": "smoke",
        "recovery": "recovery",
    }.get(action_kind, "resume")
    payload_values = {
            "affected_repositories": sorted(affected_repositories),
            "attempt": attempt,
            "candidate_shas": dict(sorted(candidate_values.items())),
            "contract_hashes": dict(sorted(contract_values.items())),
            "instance_key": instance_key,
            "parent_identifier": parent_identifier,
            "stage_kind": stage_kind,
            "stage_ordinal": stage_ordinal,
            "workflow_version": workflow_version,
        }
    if failure_bundle_digest:
        payload_values["failure_bundle_digest"] = failure_bundle_digest
    if authorizing_comment_uuid:
        payload_values["authorizing_comment_uuid"] = authorizing_comment_uuid
    payload = canonical_json(payload_values)
    return f"{prefix}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


class GenericWorkflow:
    """Apply one pure decision at a time through injectable strict effects."""

    def __init__(
        self,
        manifest: DeliveryManifest,
        snapshot_reader: WorkflowSnapshotReader,
        executor: WorkflowExecutor,
        *,
        github: GitHubMergeClient | None = None,
        smoke_executor: SmokeExecutor | None = None,
        scope_resolver: ScopeResolver | None = None,
        workflow_version: int = 2,
        supported_workflow_versions: frozenset[int] | None = None,
    ) -> None:
        if not isinstance(manifest, DeliveryManifest):
            raise TypeError("manifest must be a DeliveryManifest")
        validate_policy_authority(manifest.policy)
        if not isinstance(workflow_version, int) or isinstance(workflow_version, bool) or workflow_version < 1:
            raise ValueError("workflow_version must be positive")
        versions = supported_workflow_versions or frozenset({workflow_version})
        if (
            not isinstance(versions, frozenset)
            or workflow_version not in versions
            or any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in versions)
        ):
            raise ValueError("supported workflow versions are malformed")
        self.manifest = manifest
        self.snapshot_reader = snapshot_reader
        self.executor = executor
        self.github = github
        if smoke_executor is not None and type(smoke_executor) is not OwnedSmokeExecutor:
            raise TypeError("smoke_executor must be an OwnedSmokeExecutor")
        if smoke_executor is not None and smoke_executor.manifest is not manifest:
            raise TypeError("OwnedSmokeExecutor must be bound to the workflow manifest")
        self.smoke_executor = smoke_executor
        self.scope_resolver = scope_resolver
        self.workflow_version = workflow_version
        self.supported_workflow_versions = versions
        self.project_keys = frozenset({manifest.instance.control_project})

    def _result(
        self,
        state: WorkflowState,
        next_action: str,
        reason: str,
        *,
        created: tuple[tuple[str, str], ...] = (),
        completed_child_status: str | None = None,
        action_key: str | None = None,
        mutation_count: int = 0,
        merge_state: str | None = None,
    ) -> WorkflowResult:
        return WorkflowResult(
            parent_identifier=state.parent_identifier,
            parent_status=state.parent_status,
            next_action=next_action,
            reason=reason,
            created_children=created,
            completed_child_status=completed_child_status,
            merge_state=merge_state or state.snapshot.merge_state,
            action_key=action_key,
            mutation_count=mutation_count,
        )

    def _uncertain(
        self,
        state: WorkflowState,
        reason: str,
        *,
        action_key: str | None = None,
        mutation_count: int = 0,
    ) -> WorkflowResult:
        """Report an unobservable effect without issuing a compensating mutation."""

        return self._result(
            state,
            "uncertain",
            reason,
            action_key=action_key,
            mutation_count=mutation_count,
        )

    def _reconcile_parent(
        self,
        parent_identifier: str,
        expected: Callable[[WorkflowState], bool],
    ) -> WorkflowState | None:
        for _ in range(2):
            try:
                observed = self.snapshot_reader.read(parent_identifier)
                if expected(observed):
                    return observed
            except Exception:
                continue
        return None

    def _parent_fan_in_still_current(
        self,
        state: WorkflowState,
    ) -> WorkflowResult | None:
        """Fail closed if external evidence reads raced a parent-state change."""

        try:
            observed = self.snapshot_reader.read(state.parent_identifier)
        except Exception:
            return self._zero_mutation_block(
                state,
                "parent could not be authoritatively reread after evidence fan-in",
            )
        if type(observed) is not WorkflowState or observed != state:
            return self._zero_mutation_block(
                state,
                "parent changed during authoritative evidence fan-in",
            )
        return None

    def _action_key(
        self,
        state: WorkflowState,
        stage_kind: str,
        stage_ordinal: int,
        *,
        attempt: int | None = None,
        candidate_shas: Mapping[str, str] | None = None,
        affected: frozenset[str] | None = None,
        contract_hashes: Mapping[str, str] | None = None,
        failure_bundle_digest: str = "",
        authorizing_comment_uuid: str = "",
    ) -> str:
        metadata = state.metadata
        metadata_attempt = (
            0
            if metadata is None
            else (
                metadata.attempt
                if type(metadata) is LegacyParentMetadataV1
                else metadata.repair_round
            )
        )
        return coordinator_action_key(
            workflow_version=self.workflow_version if metadata is None else metadata.workflow_version,
            instance_key=self.manifest.instance.key,
            parent_identifier=state.parent_identifier,
            stage_kind=stage_kind,
            stage_ordinal=stage_ordinal,
            attempt=metadata_attempt if attempt is None else attempt,
            affected_repositories=(
                frozenset(state.snapshot.affected_repositories)
                if affected is None
                else affected
            ),
            candidate_shas=(state.snapshot.candidate_shas if candidate_shas is None else candidate_shas),
            contract_hashes=(
                {} if metadata is None else metadata.contract_hashes
            ) if contract_hashes is None else contract_hashes,
            failure_bundle_digest=failure_bundle_digest,
            authorizing_comment_uuid=authorizing_comment_uuid,
        )

    def _exact_state_schema_problem(self, state: object) -> str | None:
        """Validate the complete persisted DTO graph without trusting constructors."""

        malformed = "parent state schema is unsupported"
        try:
            if type(state) is not WorkflowState:
                return _WRONG_PARENT_STATE
            if (
                not _exact_stable(state.parent_identifier)
                or type(state.parent_status) is not str
                or state.parent_status not in _PARENT_STATUSES
                or type(state.project_key) is not str
                or not state.project_key
                or type(state.snapshot) is not ParentSnapshot
                or type(state.children) is not tuple
                or type(state.pull_requests) is not _MAPPING_PROXY_TYPE
                or type(state.applied_action_keys) is not frozenset
                or type(state.human_wait) is not bool
                or type(state.active_work) is not bool
            ):
                return malformed

            snapshot = state.snapshot
            if (
                type(snapshot.affected_repositories) is not tuple
                or any(
                    not _exact_stable(repository)
                    for repository in snapshot.affected_repositories
                )
                or len(set(snapshot.affected_repositories))
                != len(snapshot.affected_repositories)
            ):
                return malformed
            affected = frozenset(snapshot.affected_repositories)

            mapping_names = (
                "candidate_shas",
                "children",
                "pull_requests",
                "reviews",
                "qa",
                "integration_qa",
                "merged_shas",
            )
            if any(
                type(getattr(snapshot, name)) is not _MAPPING_PROXY_TYPE
                for name in mapping_names
            ):
                return malformed

            for values in (
                snapshot.candidate_shas,
                snapshot.children,
                snapshot.pull_requests,
                snapshot.reviews,
                snapshot.qa,
                snapshot.merged_shas,
            ):
                if (
                    any(not _exact_stable(key) for key in values)
                    or set(values) - affected
                ):
                    return malformed
            if (
                any(not _valid_sha(value) for value in snapshot.candidate_shas.values())
                or any(not _valid_sha(value) for value in snapshot.merged_shas.values())
            ):
                return malformed

            def repository_evidence_is_exact(evidence: object) -> bool:
                return (
                    type(evidence) is RepositoryEvidence
                    and type(evidence.candidate_sha) is str
                    and (
                        _valid_sha(evidence.candidate_sha)
                        or (
                            evidence.result == "pending"
                            and evidence.candidate_sha == ""
                        )
                    )
                    and type(evidence.result) is str
                    and evidence.result in _EVIDENCE_RESULTS
                )

            if any(
                not repository_evidence_is_exact(evidence)
                for values in (snapshot.children, snapshot.reviews, snapshot.qa)
                for evidence in values.values()
            ):
                return malformed

            for evidence in snapshot.pull_requests.values():
                if (
                    type(evidence) is not PullRequestEvidence
                    or not _valid_sha(evidence.head_sha)
                    or type(evidence.state) is not str
                    or evidence.state not in {"open", "closed", "merged"}
                    or (
                        evidence.mergeable is not None
                        and type(evidence.mergeable) is not bool
                    )
                    or (
                        evidence.checks_pass is not None
                        and type(evidence.checks_pass) is not bool
                    )
                    or (
                        evidence.merged_sha is not None
                        and not _valid_sha(evidence.merged_sha)
                    )
                ):
                    return malformed

            applicable_suites = {
                suite.key: suite
                for suite in self.manifest.integration_suites
                if set(suite.repositories) <= affected
            }
            if (
                any(not _exact_stable(key) for key in snapshot.integration_qa)
                or set(snapshot.integration_qa) - set(applicable_suites)
            ):
                return malformed
            for evidence in snapshot.integration_qa.values():
                if (
                    type(evidence) is not GateEvidence
                    or type(evidence.candidate_shas) is not _MAPPING_PROXY_TYPE
                    or not _exact_mapping(evidence.candidate_shas, _valid_sha)
                    or set(evidence.candidate_shas) - affected
                    or type(evidence.result) is not str
                    or evidence.result not in _EVIDENCE_RESULTS
                ):
                    return malformed

            if (
                type(snapshot.merge_state) is not str
                or snapshot.merge_state not in _MERGE_STATES
                or type(snapshot.smoke_reads) is not tuple
                or any(
                    _smoke_read_schema_problem(smoke_read) is not None
                    for smoke_read in snapshot.smoke_reads
                )
                or type(snapshot.attempt) is not int
                or not 0 <= snapshot.attempt <= self.manifest.policy.max_repair_attempts + 1
                or type(snapshot.stalled) is not bool
                or (
                    snapshot.stalled_repository is not None
                    and (
                        not _exact_stable(snapshot.stalled_repository)
                        or snapshot.stalled_repository not in affected
                    )
                )
                or type(snapshot.recovery_count) is not int
                or not 0 <= snapshot.recovery_count <= 1
                or type(snapshot.satisfied_dependencies) is not frozenset
            ):
                return malformed
            for dependency in snapshot.satisfied_dependencies:
                if (
                    type(dependency) is not tuple
                    or len(dependency) != 2
                    or any(not _exact_stable(item) for item in dependency)
                    or any(item not in affected for item in dependency)
                ):
                    return malformed

            metadata = state.metadata
            if metadata is not None:
                if type(metadata) is LegacyParentMetadataV1:
                    return _LEGACY_PARENT_STATE
                if (
                    type(metadata) is not ParentMetadata
                    or type(metadata.workflow_version) is not int
                    or metadata.workflow_version < 1
                    or type(metadata.metadata_version) is not int
                    or metadata.metadata_version < 1
                    or not _exact_stable(metadata.instance_key)
                    or type(metadata.affected_repositories) is not tuple
                    or any(
                        not _exact_stable(repository)
                        for repository in metadata.affected_repositories
                    )
                    or len(set(metadata.affected_repositories))
                    != len(metadata.affected_repositories)
                    or type(metadata.repository_dag) is not _MAPPING_PROXY_TYPE
                    or type(metadata.candidate_shas) is not _MAPPING_PROXY_TYPE
                    or type(metadata.contract_hashes) is not _MAPPING_PROXY_TYPE
                    or type(metadata.stage_ordinal) is not int
                    or metadata.stage_ordinal < 0
                    or type(metadata.merge_plan) is not tuple
                    or any(not _exact_stable(item) for item in metadata.merge_plan)
                    or type(metadata.merge_state) is not str
                    or type(metadata.repair_round) is not int
                    or type(metadata.automatic_repairs_used) is not int
                    or metadata.automatic_repairs_used
                    != min(
                        metadata.repair_round,
                        self.manifest.policy.max_repair_attempts,
                    )
                    or metadata.repair_round > self.manifest.policy.max_repair_attempts + 1
                    or type(metadata.last_action) is not str
                ):
                    return malformed
                if set(metadata.repository_dag) != set(metadata.affected_repositories):
                    return malformed
                for repository, dependencies in metadata.repository_dag.items():
                    if (
                        not _exact_stable(repository)
                        or type(dependencies) is not tuple
                        or any(not _exact_stable(item) for item in dependencies)
                        or len(set(dependencies)) != len(dependencies)
                    ):
                        return malformed
                if (
                    not _exact_mapping(metadata.candidate_shas, _valid_sha)
                    or not _exact_mapping(metadata.contract_hashes, _valid_sha)
                ):
                    return malformed
                reconstructed = ParentMetadata(
                    workflow_version=metadata.workflow_version,
                    metadata_version=metadata.metadata_version,
                    instance_key=metadata.instance_key,
                    affected_repositories=metadata.affected_repositories,
                    repository_dag=dict(metadata.repository_dag),
                    candidate_shas=dict(metadata.candidate_shas),
                    contract_hashes=dict(metadata.contract_hashes),
                    stage_ordinal=metadata.stage_ordinal,
                    merge_plan=metadata.merge_plan,
                    merge_state=metadata.merge_state,
                    repair_round=metadata.repair_round,
                    automatic_repairs_used=metadata.automatic_repairs_used,
                    repair_authorization=metadata.repair_authorization,
                    last_action=metadata.last_action,
                )
                if (
                    reconstructed != metadata
                    or tuple(reconstructed.repository_dag.items())
                    != tuple(metadata.repository_dag.items())
                    or tuple(reconstructed.candidate_shas.items())
                    != tuple(metadata.candidate_shas.items())
                    or tuple(reconstructed.contract_hashes.items())
                    != tuple(metadata.contract_hashes.items())
                ):
                    return malformed
            elif (
                affected
                or snapshot.candidate_shas
                or snapshot.children
                or snapshot.pull_requests
                or snapshot.reviews
                or snapshot.qa
                or snapshot.integration_qa
                or snapshot.merged_shas
                or snapshot.smoke_reads
                or state.children
                or state.pull_requests
            ):
                return malformed

            identifiers: set[str] = set()
            for child in state.children:
                if (
                    type(child) is not WorkflowChild
                    or not _exact_stable(child.identifier)
                    or child.identifier in identifiers
                    or not _exact_stable(child.target_key)
                    or not _exact_stable(child.repository_key)
                    or child.repository_key not in affected
                    or not _exact_stable(child.suite_key, empty=True)
                    or type(child.phase) is not str
                    or child.phase not in _PHASES
                    or type(child.stage_ordinal) is not int
                    or child.stage_ordinal < 0
                    or type(child.attempt) is not int
                    or child.attempt < 0
                    or type(child.status) is not str
                    or child.status not in _CHILD_STATUSES
                    or type(child.action_key) is not str
                    or _ACTION_KEY.fullmatch(child.action_key) is None
                    or child.action_key not in state.applied_action_keys
                    or type(child.active) is not bool
                    or not _canonical_uuid(child.evidence_comment_uuid, empty=True)
                    or type(child.creation_candidate_shas)
                    is not _MAPPING_PROXY_TYPE
                    or not _exact_mapping(
                        child.creation_candidate_shas,
                        _valid_sha,
                    )
                    or set(child.creation_candidate_shas) - affected
                    or type(child.phase_result) is not str
                    or child.phase_result not in _PHASE_RESULTS | {""}
                    or type(child.evidence_comment_url) is not str
                    or (
                        child.evidence_comment_url
                        and not _https_evidence_url(child.evidence_comment_url)
                    )
                    or type(child.responsible_repositories) is not tuple
                    or any(
                        not _exact_stable(repository)
                        for repository in child.responsible_repositories
                    )
                    or len(set(child.responsible_repositories))
                    != len(child.responsible_repositories)
                    or type(child.failure_bundle_digest) is not str
                    or (
                        child.failure_bundle_digest
                        and not _valid_digest(child.failure_bundle_digest)
                    )
                    or type(child.failure_evidence_uuids) is not tuple
                    or any(
                        not _canonical_uuid(evidence_uuid)
                        for evidence_uuid in child.failure_evidence_uuids
                    )
                    or len(set(child.failure_evidence_uuids))
                    != len(child.failure_evidence_uuids)
                    or child.failure_evidence_uuids
                    != tuple(sorted(child.failure_evidence_uuids))
                    or not _canonical_uuid(
                        child.authorizing_comment_uuid,
                        empty=True,
                    )
                ):
                    return malformed
                identifiers.add(child.identifier)
                expected_prefix = {
                    "implementation": "dispatch:",
                    "repair": "repair:",
                    "review": "stage:",
                    "qa": "stage:",
                    "integration_qa": "stage:",
                    "smoke": "smoke:",
                }[child.phase]
                if not child.action_key.startswith(expected_prefix):
                    return malformed
                if child.phase == "repair":
                    if (
                        not child.failure_bundle_digest
                        or not child.failure_evidence_uuids
                        or child.responsible_repositories
                        or bool(child.authorizing_comment_uuid)
                        != (
                            child.attempt
                            > self.manifest.policy.max_repair_attempts
                        )
                    ):
                        return malformed
                elif (
                    child.failure_bundle_digest
                    or child.failure_evidence_uuids
                    or child.authorizing_comment_uuid
                ):
                    return malformed
                completion_fields = (
                    bool(child.evidence_comment_uuid),
                    bool(child.phase_result),
                    bool(child.evidence_comment_url),
                )
                if any(completion_fields) and not all(completion_fields):
                    return malformed
                if child.phase in {"review", "qa", "integration_qa"}:
                    if child.phase_result == "pass" and child.responsible_repositories:
                        return malformed
                    if (
                        child.phase_result in {"fail", "blocked"}
                        and not child.responsible_repositories
                    ):
                        return malformed
                elif child.responsible_repositories:
                    return malformed
                if child.phase == "integration_qa":
                    suite = applicable_suites.get(child.suite_key)
                    if (
                        suite is None
                        or child.target_key != child.suite_key
                        or child.repository_key != suite.command_repository
                        or not set(child.responsible_repositories)
                        <= set(suite.repositories)
                    ):
                        return malformed
                elif (
                    child.target_key != child.repository_key
                    or child.suite_key
                    or (
                        child.phase in {"review", "qa"}
                        and child.phase_result in {"fail", "blocked"}
                        and child.responsible_repositories
                        != (child.repository_key,)
                    )
                ):
                    return malformed

            if any(
                type(key) is not str or _ACTION_KEY.fullmatch(key) is None
                for key in state.applied_action_keys
            ):
                return malformed
            for repository, target in state.pull_requests.items():
                if (
                    not _exact_stable(repository)
                    or repository not in affected
                    or type(target) is not PullRequestTarget
                    or target.repository_key != repository
                    or type(target.number) is not int
                    or target.number < 1
                    or type(target.url) is not str
                ):
                    return malformed
                specification = self.manifest.repositories.get(repository)
                if specification is None or target.url != (
                    f"https://github.com/{specification.github}/pull/{target.number}"
                ):
                    return malformed
        except BaseException:
            return malformed
        return None

    def _state_problem(self, state: object, parent_identifier: str) -> str | None:
        schema_problem = self._exact_state_schema_problem(state)
        if schema_problem is not None:
            return schema_problem
        assert type(state) is WorkflowState
        if state.parent_identifier != parent_identifier:
            return _WRONG_PARENT_STATE
        if state.project_key != self.manifest.instance.control_project:
            return _OUTSIDE_PROJECT_STATE
        if state.metadata is None:
            return _UNINITIALIZED_PARENT_STATE
        metadata = state.metadata
        if (
            type(metadata) is not ParentMetadata
            or metadata.metadata_version != 2
            or metadata.workflow_version not in self.supported_workflow_versions
            or metadata.instance_key != self.manifest.instance.key
        ):
            return "parent workflow version or instance is unsupported"
        if tuple(metadata.affected_repositories) != tuple(state.snapshot.affected_repositories):
            return "parent affected repository metadata disagrees with its snapshot"
        if dict(metadata.candidate_shas) != dict(state.snapshot.candidate_shas):
            return "parent candidate SHA metadata disagrees with its snapshot"
        if (
            metadata.merge_state != state.snapshot.merge_state
            or metadata.repair_round != state.snapshot.attempt
        ):
            return "parent transition metadata disagrees with its snapshot"
        if any(
            child.stage_ordinal > metadata.stage_ordinal
            or (
                child.stage_ordinal == metadata.stage_ordinal
                and child.attempt > metadata.repair_round
            )
            for child in state.children
        ):
            return _FUTURE_CHILD_RELATIONSHIP
        return None

    def _state_problem_result(
        self,
        state: object,
        parent_identifier: str,
        *,
        stage_kind: str = "block",
        allow_uninitialized: bool = False,
        future_child_only: bool = False,
    ) -> WorkflowResult | None:
        problem = self._state_problem(state, parent_identifier)
        if allow_uninitialized and problem == _UNINITIALIZED_PARENT_STATE:
            return None
        if problem is None or (
            future_child_only
            and problem
            not in {
                _FUTURE_CHILD_RELATIONSHIP,
                _LEGACY_PARENT_STATE,
                "parent state schema is unsupported",
            }
        ):
            return None
        merge_state = (
            state.snapshot.merge_state
            if type(state) is WorkflowState
            and type(getattr(state, "snapshot", None)) is ParentSnapshot
            else "pending"
        )
        return WorkflowResult(
            parent_identifier,
            "blocked",
            "block",
            problem,
            merge_state=merge_state,
            mutation_count=0,
        )

    def _completed_legacy_replay(
        self,
        state: object,
        parent_identifier: str,
    ) -> WorkflowResult | None:
        if (
            type(state) is not WorkflowState
            or state.parent_status != "done"
            or type(state.metadata) is not LegacyParentMetadataV1
        ):
            return None
        metadata = state.metadata
        try:
            reconstructed = LegacyParentMetadataV1(
                workflow_version=metadata.workflow_version,
                metadata_version=metadata.metadata_version,
                instance_key=metadata.instance_key,
                affected_repositories=metadata.affected_repositories,
                repository_dag=dict(metadata.repository_dag),
                candidate_shas=dict(metadata.candidate_shas),
                contract_hashes=dict(metadata.contract_hashes),
                stage_ordinal=metadata.stage_ordinal,
                merge_plan=metadata.merge_plan,
                merge_state=metadata.merge_state,
                attempt=metadata.attempt,
                last_action=metadata.last_action,
            )
            synthetic = ParentMetadata(
                workflow_version=2,
                metadata_version=2,
                instance_key=metadata.instance_key,
                affected_repositories=metadata.affected_repositories,
                repository_dag=dict(metadata.repository_dag),
                candidate_shas=dict(metadata.candidate_shas),
                contract_hashes=dict(metadata.contract_hashes),
                stage_ordinal=metadata.stage_ordinal,
                merge_plan=metadata.merge_plan,
                merge_state=metadata.merge_state,
                repair_round=metadata.attempt,
                automatic_repairs_used=metadata.attempt,
                last_action=metadata.last_action,
            )
            schema_problem = self._exact_state_schema_problem(
                replace(state, metadata=synthetic)
            )
        except (MetadataError, TypeError, ValueError):
            schema_problem = "parent state schema is unsupported"
            reconstructed = None
        if (
            reconstructed != metadata
            or schema_problem is not None
            or state.parent_identifier != parent_identifier
            or state.project_key != self.manifest.instance.control_project
            or metadata.instance_key != self.manifest.instance.key
            or tuple(metadata.affected_repositories)
            != tuple(state.snapshot.affected_repositories)
            or dict(metadata.candidate_shas)
            != dict(state.snapshot.candidate_shas)
            or metadata.merge_state != state.snapshot.merge_state
            or metadata.attempt != state.snapshot.attempt
        ):
            return WorkflowResult(
                parent_identifier,
                "blocked",
                "block",
                "completed version one parent is not authoritative",
                mutation_count=0,
            )
        completed = decide_parent_action(self.manifest, state.snapshot)
        key = self._action_key(state, "complete", metadata.stage_ordinal)
        if (
            completed.kind is not DecisionKind.COMPLETE
            or metadata.last_action != key
            or key not in state.applied_action_keys
        ):
            return self._uncertain(
                state,
                "completed version one parent lacks its exact completion transition",
                action_key=key,
            )
        return self._result(
            state,
            "noop",
            "version one parent completion remains authoritative and immutable",
            action_key=key,
        )

    @staticmethod
    def _current_stage_is_active(state: WorkflowState) -> bool:
        if state.metadata is None:
            return False
        current_children = (
            child
            for child in state.children
            if child.stage_ordinal == state.metadata.stage_ordinal
            and child.attempt == state.metadata.repair_round
        )
        return state.active_work or any(
            child.active or child.status not in _TERMINAL_CHILD_STATUSES
            for child in current_children
        )

    def _current_stage_wait(self, state: WorkflowState) -> WorkflowResult | None:
        if not self._current_stage_is_active(state):
            return None
        return self._result(
            state,
            "wait",
            "current Stage still has active work",
        )

    @staticmethod
    def _candidate_heads_match(state: WorkflowState) -> bool:
        return set(state.pull_requests) <= set(state.snapshot.pull_requests) and all(
            state.snapshot.candidate_shas.get(repository) == pull_request.head_sha
            for repository, pull_request in state.snapshot.pull_requests.items()
        )

    def _current_repair_failure_bundle(
        self,
        state: WorkflowState,
        current: tuple[WorkflowChild, ...],
        source: Mapping[str, str],
    ) -> FailureBundle | None:
        """Rebuild the immutable current Repair owner set from its source Gates."""

        if state.metadata is None or not current:
            return None
        source_stage = state.metadata.stage_ordinal - 1
        source_attempt = state.metadata.repair_round - 1
        affected = frozenset(state.snapshot.affected_repositories)
        applicable_suites = {
            suite.key: suite
            for suite in self.manifest.integration_suites
            if set(suite.repositories) <= affected
        }
        gates = tuple(
            child
            for child in state.children
            if child.stage_ordinal == source_stage
            and child.attempt == source_attempt
        )
        expected_identities = {
            (phase, repository, "")
            for repository in affected
            for phase in ("review", "qa")
        }
        expected_identities.update(
            ("integration_qa", suite_key, suite_key)
            for suite_key in applicable_suites
        )
        identities = tuple(
            (child.phase, child.target_key, child.suite_key)
            for child in gates
        )
        if (
            source_stage < 0
            or not gates
            or len(identities) != len(set(identities))
            or set(identities) != expected_identities
        ):
            return None
        expected_creation_action = self._action_key(
            state,
            "gates",
            source_stage,
            attempt=source_attempt,
            candidate_shas=source,
        )
        if (
            expected_creation_action not in state.applied_action_keys
            or any(child.action_key != expected_creation_action for child in gates)
        ):
            return None

        failures: list[FailureEvidenceRef] = []
        evidence_uuids: set[str] = set()
        for child in gates:
            if (
                child.status != "done"
                or child.active
                or child.phase_result not in _PHASE_RESULTS
                or dict(child.creation_candidate_shas) != dict(source)
                or not _canonical_uuid(child.evidence_comment_uuid)
                or not _https_evidence_url(child.evidence_comment_url)
                or child.evidence_comment_uuid in evidence_uuids
            ):
                return None
            expected_completion_action = self._action_key(
                state,
                f"{child.phase}:{child.target_key}",
                source_stage,
                attempt=source_attempt,
                candidate_shas=source,
            )
            if (
                expected_completion_action == expected_creation_action
                or expected_completion_action not in state.applied_action_keys
            ):
                return None
            pull_request = state.pull_requests.get(child.repository_key)
            if pull_request is None:
                return None
            try:
                observed_completions = tuple(
                    self.snapshot_reader.read_phase_completion(
                        state.parent_identifier,
                        child.evidence_comment_uuid,
                    )
                    for _ in range(2)
                )
            except Exception:
                return None
            if (
                observed_completions[0] != observed_completions[1]
                or type(observed_completions[0]) is not PhaseCompletion
                or observed_completions[0].pull_request_url
                not in {"", pull_request.url}
            ):
                return None
            observed_completion = observed_completions[0]
            expected_completion = PhaseCompletion(
                parent_identifier=state.parent_identifier,
                repository_key=child.repository_key,
                phase=child.phase,
                result=child.phase_result,
                attempt=source_attempt,
                candidate_sha=source[child.repository_key],
                pull_request_url=observed_completion.pull_request_url,
                evidence_comment_uuid=child.evidence_comment_uuid,
                evidence_comment_url=child.evidence_comment_url,
                suite_key=child.suite_key,
                candidate_shas=(source if child.phase == "integration_qa" else {}),
                responsible_repositories=child.responsible_repositories,
            )
            if (
                _phase_completion_schema_problem(
                    expected_completion,
                    manifest=self.manifest,
                )
                is not None
            ):
                return None
            if observed_completion != expected_completion:
                return None
            evidence_uuids.add(child.evidence_comment_uuid)
            if child.phase in {"review", "qa"}:
                expected_owners = (
                    (child.repository_key,)
                    if child.phase_result != "pass"
                    else ()
                )
                if (
                    child.target_key != child.repository_key
                    or child.repository_key not in affected
                    or child.suite_key
                    or child.responsible_repositories != expected_owners
                ):
                    return None
            else:
                suite = applicable_suites.get(child.suite_key)
                if (
                    suite is None
                    or child.target_key != suite.key
                    or child.repository_key != suite.command_repository
                    or (
                        child.phase_result == "pass"
                        and child.responsible_repositories
                    )
                    or (
                        child.phase_result != "pass"
                        and (
                            not child.responsible_repositories
                            or not set(child.responsible_repositories)
                            <= set(suite.repositories)
                        )
                    )
                ):
                    return None
            if child.phase_result != "pass":
                failures.append(
                    FailureEvidenceRef(
                        child_identifier=child.identifier,
                        phase=child.phase,
                        result=child.phase_result,
                        stage_ordinal=source_stage,
                        repair_round=source_attempt,
                        candidate_shas=source,
                        responsible_repositories=child.responsible_repositories,
                        evidence_comment_uuid=child.evidence_comment_uuid,
                        evidence_comment_url=child.evidence_comment_url,
                        suite_key=child.suite_key,
                    )
                )
        if not failures:
            return None
        try:
            return FailureBundle.build(
                state.parent_identifier,
                state.metadata.workflow_version,
                source_stage,
                state.metadata.repair_round,
                source,
                tuple(failures),
            )
        except (TypeError, ValueError, WorkflowError):
            return None

    def _current_repair_head_problem(
        self,
        state: WorkflowState,
        completion: PhaseCompletion | None = None,
    ) -> tuple[bool, str | None]:
        """Validate current Repair Stage heads without adopting active-owner motion."""

        if state.metadata is None:
            return False, None
        current = tuple(
            child
            for child in state.children
            if child.stage_ordinal == state.metadata.stage_ordinal
            and child.attempt == state.metadata.repair_round
        )
        if not current or not any(child.phase == "repair" for child in current):
            return False, None
        problem = "current Repair Stage head or membership evidence is conflicting"
        if any(child.phase != "repair" for child in current):
            return True, problem
        source = dict(current[0].creation_candidate_shas)
        affected = set(state.snapshot.affected_repositories)
        if (
            not source
            or set(source) != affected
            or set(state.snapshot.candidate_shas) != affected
            or set(state.snapshot.pull_requests) != affected
            or set(state.pull_requests) != affected
        ):
            return True, problem
        expected_action = self._action_key(
            state,
            "repair",
            state.metadata.stage_ordinal,
            attempt=state.metadata.repair_round,
            candidate_shas=source,
            failure_bundle_digest=current[0].failure_bundle_digest,
            authorizing_comment_uuid=current[0].authorizing_comment_uuid,
        )
        bundle = self._current_repair_failure_bundle(state, current, source)
        owners = Counter(child.repository_key for child in current)
        expected_owners = (
            frozenset(
                repository
                for failure in bundle.failures
                for repository in failure.responsible_repositories
            )
            if bundle is not None
            else frozenset()
        )
        if (
            bundle is None
            or bundle.digest != current[0].failure_bundle_digest
            or any(count != 1 for count in owners.values())
            or set(owners) != expected_owners
            or any(
                child.target_key != child.repository_key
                or child.suite_key
                or dict(child.creation_candidate_shas) != source
                or child.action_key != expected_action
                or child.failure_bundle_digest != current[0].failure_bundle_digest
                or child.authorizing_comment_uuid
                != current[0].authorizing_comment_uuid
                or child.failure_evidence_uuids
                != tuple(sorted(
                    failure.evidence_comment_uuid
                    for failure in bundle.for_repository(child.repository_key)
                ))
                for child in current
            )
            or expected_action not in state.applied_action_keys
        ):
            return True, problem

        for repository in affected:
            candidate = state.snapshot.candidate_shas[repository]
            pull_request = state.snapshot.pull_requests[repository]
            child = next(
                (item for item in current if item.repository_key == repository),
                None,
            )
            if child is None:
                evidence = state.snapshot.children.get(repository)
                if (
                    candidate != source[repository]
                    or pull_request.head_sha != source[repository]
                    or evidence is None
                    or evidence.result != "pass"
                    or evidence.candidate_sha != source[repository]
                ):
                    return True, problem
                continue
            evidence = state.snapshot.children.get(repository)
            if child.status in _ACTIVE_CHILD_STATUSES and child.active:
                if (
                    candidate != source[repository]
                    or evidence is None
                    or evidence.result != "pending"
                    or evidence.candidate_sha not in {"", source[repository]}
                ):
                    return True, problem
                if completion is not None and completion.repository_key == repository:
                    if completion.phase != "repair":
                        return True, problem
                    if completion.result == "pass":
                        if (
                            completion.candidate_sha == source[repository]
                            or completion.candidate_sha != pull_request.head_sha
                        ):
                            return True, problem
                    elif (
                        completion.candidate_sha != source[repository]
                        or pull_request.head_sha != source[repository]
                    ):
                        return True, problem
                continue
            if (
                child.status != "done"
                or child.active
                or child.phase_result != "pass"
                or evidence is None
                or evidence.result != "pass"
                or evidence.candidate_sha != candidate
                or candidate == source[repository]
                or pull_request.head_sha != candidate
            ):
                return True, problem
        return True, None

    def _parent_decision(self, state: WorkflowState) -> ParentDecision:
        snapshot = state.snapshot
        automatic_limit = self.manifest.policy.max_repair_attempts
        if snapshot.attempt > automatic_limit:
            snapshot = replace(snapshot, attempt=automatic_limit)
        return decide_parent_action(self.manifest, snapshot)

    def _metadata(
        self,
        state: WorkflowState,
        *,
        action_key: str,
        stage_ordinal: int | None = None,
        attempt: int | None = None,
        candidate_shas: Mapping[str, str] | None = None,
        merge_plan: tuple[str, ...] | None = None,
        merge_state: str | None = None,
        automatic_repair: bool | None = None,
        consume_repair_authorization: bool = False,
    ) -> ParentMetadata:
        if state.metadata is None:
            raise WorkflowError("parent has no initialized workflow metadata")
        values: dict[str, object] = {"last_action": action_key}
        if stage_ordinal is not None:
            values["stage_ordinal"] = stage_ordinal
        if attempt is not None:
            values["repair_round"] = attempt
            if automatic_repair is True:
                values["automatic_repairs_used"] = (
                    state.metadata.automatic_repairs_used + 1
                )
            elif automatic_repair is not False:
                raise WorkflowError("repair authority must classify a new round")
        if consume_repair_authorization:
            values["repair_authorization"] = None
        if candidate_shas is not None:
            values["candidate_shas"] = candidate_shas
        if merge_plan is not None:
            values["merge_plan"] = merge_plan
        if merge_state is not None:
            values["merge_state"] = merge_state
        try:
            return replace(state.metadata, **values)
        except (MetadataError, TypeError) as error:
            raise WorkflowError("parent metadata transition is invalid") from error

    def _block(
        self,
        state: WorkflowState,
        reason: str,
        *,
        stage_kind: str = "block",
        merge_state: str | None = None,
        merged_shas: Mapping[str, str] | None = None,
        action_key: str | None = None,
    ) -> WorkflowResult:
        if reason == _FUTURE_CHILD_RELATIONSHIP:
            return WorkflowResult(
                state.parent_identifier,
                "blocked",
                "block",
                reason,
                merge_state=merge_state or state.snapshot.merge_state,
            )
        if state.metadata is None:
            return WorkflowResult(
                state.parent_identifier,
                "blocked",
                "block",
                reason,
                merge_state=merge_state or state.snapshot.merge_state,
            )
        key = action_key or self._action_key(
            state,
            stage_kind,
            state.metadata.stage_ordinal,
        )
        target_merge_state = merge_state or state.metadata.merge_state
        metadata = self._metadata(
            state,
            action_key=key,
            merge_state=target_merge_state,
        )
        mutations = 0
        try:
            if merge_state is not None:
                self.executor.record_merge_state(
                    state.parent_identifier,
                    merge_state,
                    dict(merged_shas or state.snapshot.merged_shas),
                    metadata,
                    action_key=key,
                )
                mutations += 1
                state = self.snapshot_reader.read(state.parent_identifier)
                metadata = state.metadata or metadata
            self.executor.set_parent_status(
                state.parent_identifier,
                "blocked",
                reason,
                metadata,
                action_key=key,
            )
            mutations += 1
            state = self.snapshot_reader.read(state.parent_identifier)
        except Exception:
            return WorkflowResult(
                state.parent_identifier,
                "blocked",
                "block",
                reason,
                merge_state=target_merge_state,
                action_key=key,
                mutation_count=mutations,
            )
        return self._result(
            state,
            "block",
            reason,
            action_key=key,
            mutation_count=mutations,
            merge_state=target_merge_state,
        )

    def _human_clarification(self, state: WorkflowState, reason: str) -> WorkflowResult:
        affected = frozenset(state.snapshot.affected_repositories)
        key = self._action_key(
            state,
            "human-clarification",
            0 if state.metadata is None else state.metadata.stage_ordinal,
            affected=affected,
            candidate_shas=state.snapshot.candidate_shas,
        )
        if key in state.applied_action_keys or state.human_wait:
            return self._result(state, "human-clarification", reason, action_key=key)
        try:
            self.executor.request_human_clarification(
                state.parent_identifier,
                reason,
                action_key=key,
            )
            state = self.snapshot_reader.read(state.parent_identifier)
        except Exception:
            return WorkflowResult(
                state.parent_identifier,
                "blocked",
                "block",
                "human clarification could not be recorded",
                action_key=key,
            )
        return self._result(
            state,
            "human-clarification",
            reason,
            action_key=key,
            mutation_count=1,
        )

    def handle_status_change(
        self,
        parent_identifier: str,
        old_status: str,
        new_status: str,
    ) -> WorkflowResult:
        if old_status != "backlog" or new_status != "todo":
            return WorkflowResult(parent_identifier, new_status, "noop", "status transition does not start intake")
        try:
            state = self.snapshot_reader.read(parent_identifier)
        except Exception:
            return WorkflowResult(parent_identifier, "blocked", "block", "parent could not be read")
        if self._state_problem(state, parent_identifier) == _OUTSIDE_PROJECT_STATE:
            assert type(state) is WorkflowState
            return WorkflowResult(
                parent_identifier,
                state.parent_status,
                "noop",
                "parent intake is restricted to the control Project",
            )
        problem_result = self._state_problem_result(
            state,
            parent_identifier,
            stage_kind="intake-transition",
            allow_uninitialized=True,
        )
        if problem_result is not None:
            return problem_result
        assert type(state) is WorkflowState
        if state.parent_status != "todo":
            return WorkflowResult(
                parent_identifier,
                state.parent_status,
                "noop",
                "authoritative parent state does not confirm the intake transition",
            )
        key = self._action_key(
            state,
            "intake-transition",
            0,
            affected=frozenset(),
            candidate_shas={},
            contract_hashes={},
        )
        expected = StatusTransition(parent_identifier, old_status, new_status, key)
        try:
            observed = self.snapshot_reader.read_intake_transition(parent_identifier)
            if observed is None:
                self.executor.record_intake_transition(
                    parent_identifier,
                    old_status,
                    new_status,
                    action_key=key,
                )
                observed = self.snapshot_reader.read_intake_transition(parent_identifier)
            authoritative = self.snapshot_reader.read(parent_identifier)
        except Exception:
            return WorkflowResult(
                parent_identifier,
                state.parent_status,
                "uncertain",
                "intake transition could not be authoritatively reconciled",
                action_key=key,
            )
        authoritative_problem = self._state_problem_result(
            authoritative,
            parent_identifier,
            allow_uninitialized=True,
        )
        if authoritative_problem is not None:
            return authoritative_problem
        assert type(authoritative) is WorkflowState
        normalized_authoritative = replace(
            authoritative,
            applied_action_keys=state.applied_action_keys,
        )
        if (
            observed != expected
            or normalized_authoritative != state
            or authoritative.applied_action_keys
            not in {
                state.applied_action_keys,
                state.applied_action_keys | {key},
            }
        ):
            return self._result(
                authoritative,
                "noop",
                "authoritative intake transition did not match or parent changed",
                action_key=key,
            )
        if self.scope_resolver is not None:
            try:
                resolution = self.scope_resolver.resolve(parent_identifier)
            except Exception:
                return WorkflowResult(
                    parent_identifier,
                    "blocked",
                    "block",
                    "parent scope resolution failed",
                )
            if not isinstance(resolution, ScopeResolution):
                return WorkflowResult(
                    parent_identifier,
                    "blocked",
                    "block",
                    "parent scope resolution is malformed",
                )
            return self.handle_parent_event(
                parent_identifier,
                affected=resolution.affected,
                affected_candidates=resolution.affected_candidates,
                contract_hashes=resolution.contract_hashes,
                authority_requirements=resolution.authority_requirements,
            )
        return WorkflowResult(parent_identifier, "todo", "dispatch", "Backlog to Todo starts parent intake")

    def _scope_problem(self, affected: frozenset[str]) -> str | None:
        if not affected:
            return "affected repository scope is missing"
        unknown = sorted(affected - self.manifest.repositories.keys())
        if unknown:
            return "affected repository scope is outside the manifest"
        for repository in sorted(affected):
            missing = sorted(set(self.manifest.repositories[repository].depends_on) - affected)
            if missing:
                return "affected repository scope is not dependency-closed"
        return None

    def handle_parent_event(
        self,
        parent_identifier: str,
        *,
        affected: frozenset[str] | None = None,
        affected_candidates: tuple[frozenset[str], ...] = (),
        contract_hashes: Mapping[str, str] | None = None,
        authority_requirements: tuple[str, ...] = (),
    ) -> WorkflowResult:
        try:
            state = self.snapshot_reader.read(parent_identifier)
        except Exception:
            return WorkflowResult(parent_identifier, "blocked", "block", "parent could not be read")
        if self._state_problem(state, parent_identifier) == _OUTSIDE_PROJECT_STATE:
            assert type(state) is WorkflowState
            return WorkflowResult(
                parent_identifier,
                state.parent_status,
                "noop",
                "parent intake is restricted to the control Project",
            )
        problem_result = self._state_problem_result(
            state,
            parent_identifier,
            stage_kind="parent-event",
            allow_uninitialized=True,
        )
        if problem_result is not None:
            return problem_result
        assert type(state) is WorkflowState
        if state.metadata is not None:
            existing = frozenset(state.metadata.affected_repositories)
            if authority_requirements:
                return self._human_clarification(
                    state,
                    "new authority requirements need human approval",
                )
            existing_candidates: set[frozenset[str]] = set()
            try:
                for candidate in affected_candidates:
                    existing_candidates.add(frozenset(candidate))
            except TypeError:
                return self._human_clarification(
                    state,
                    "affected repository candidates are malformed",
                )
            if affected is not None:
                if not isinstance(affected, frozenset):
                    return self._human_clarification(
                        state,
                        "affected repository scope is malformed",
                    )
                existing_candidates.add(affected)
            if existing_candidates and (
                len(existing_candidates) != 1 or next(iter(existing_candidates)) != existing
            ):
                return self._human_clarification(state, "new affected scope conflicts with initialized parent")
            if contract_hashes is not None:
                try:
                    supplied_hashes = dict(contract_hashes)
                except (TypeError, ValueError):
                    return self._human_clarification(
                        state,
                        "interface contract hashes are malformed",
                    )
                if (
                    any(
                        not isinstance(key, str) or not _valid_sha(value)
                        for key, value in supplied_hashes.items()
                    )
                    or supplied_hashes != dict(state.metadata.contract_hashes)
                ):
                    return self._human_clarification(
                        state,
                        "new interface contract requirements conflict with initialized parent",
                    )
            if state.human_wait or state.active_work or any(
                child.active and child.status in _ACTIVE_CHILD_STATUSES for child in state.children
            ):
                return self._result(state, "noop", "an intended successor is already active")
            return self.resume_parent(parent_identifier)

        try:
            intake_key = self._action_key(
                state,
                "intake-transition",
                0,
                affected=frozenset(),
                candidate_shas={},
                contract_hashes={},
            )
            transition = self.snapshot_reader.read_intake_transition(parent_identifier)
        except Exception:
            return self._result(
                state,
                "noop",
                "authoritative intake transition is unavailable",
            )
        if (
            state.parent_status != "todo"
            or transition
            != StatusTransition(parent_identifier, "backlog", "todo", intake_key)
        ):
            return self._result(
                state,
                "noop",
                "metadata-free parent lacks an authoritative Backlog to Todo transition",
            )

        if authority_requirements:
            return self._human_clarification(state, "new authority requirements need human approval")
        normalized_candidates: set[frozenset[str]] = set()
        try:
            for candidate in affected_candidates:
                normalized_candidates.add(frozenset(candidate))
        except TypeError:
            return self._human_clarification(state, "affected repository candidates are malformed")
        if affected is not None:
            if not isinstance(affected, frozenset):
                return self._human_clarification(state, "affected repository scope is malformed")
            normalized_candidates.add(affected)
        if len(normalized_candidates) != 1:
            return self._human_clarification(state, "affected repository scope is ambiguous")
        selected = next(iter(normalized_candidates))
        problem = self._scope_problem(selected)
        if problem is not None:
            return self._human_clarification(state, problem)
        hashes = dict(contract_hashes or {})
        if any(not isinstance(key, str) or not _valid_sha(value) for key, value in hashes.items()):
            return self._human_clarification(state, "interface contract hashes are malformed")
        dag = {
            repository: tuple(
                dependency
                for dependency in self.manifest.repositories[repository].depends_on
                if dependency in selected
            )
            for repository in sorted(selected)
        }
        key = coordinator_action_key(
            workflow_version=self.workflow_version,
            instance_key=self.manifest.instance.key,
            parent_identifier=parent_identifier,
            stage_kind="intake",
            stage_ordinal=0,
            attempt=0,
            affected_repositories=selected,
            candidate_shas={},
            contract_hashes=hashes,
        )
        if key in state.applied_action_keys:
            return self._result(
                state,
                "noop",
                "parent intake action already exists",
                action_key=key,
            )
        try:
            metadata = ParentMetadata(
                workflow_version=self.workflow_version,
                metadata_version=2,
                instance_key=self.manifest.instance.key,
                affected_repositories=tuple(
                    repository for repository in self.manifest.merge_order if repository in selected
                ),
                repository_dag=dag,
                candidate_shas={},
                contract_hashes=hashes,
                stage_ordinal=0,
                merge_plan=(),
                merge_state="pending",
                repair_round=0,
                automatic_repairs_used=0,
                last_action=key,
            )
            self.executor.initialize_parent(parent_identifier, metadata, action_key=key)
        except Exception:
            return WorkflowResult(parent_identifier, "blocked", "block", "parent intake could not be initialized")
        return self.resume_parent(parent_identifier)

    def _ordered(self, repositories: set[str] | frozenset[str]) -> tuple[str, ...]:
        return tuple(repository for repository in self.manifest.merge_order if repository in repositories)

    def _implementation_requests(
        self,
        state: WorkflowState,
        repositories: tuple[str, ...],
        ordinal: int,
    ) -> tuple[ChildRequest, ...]:
        return tuple(
            ChildRequest(
                target_key=repository,
                repository_key=repository,
                suite_key="",
                phase="implementation",
                stage_ordinal=ordinal,
                attempt=state.snapshot.attempt,
                candidate_shas=state.snapshot.candidate_shas,
            )
            for repository in repositories
        )

    def _gate_requests(self, state: WorkflowState, ordinal: int) -> tuple[ChildRequest, ...]:
        affected = frozenset(state.snapshot.affected_repositories)
        requests: list[ChildRequest] = []
        for repository in self._ordered(affected):
            if repository not in state.snapshot.reviews:
                requests.append(
                    ChildRequest(
                        repository,
                        repository,
                        "",
                        "review",
                        ordinal,
                        state.snapshot.attempt,
                        state.snapshot.candidate_shas,
                        state.pull_requests.get(repository),
                    )
                )
            if repository not in state.snapshot.qa:
                requests.append(
                    ChildRequest(
                        repository,
                        repository,
                        "",
                        "qa",
                        ordinal,
                        state.snapshot.attempt,
                        state.snapshot.candidate_shas,
                        state.pull_requests.get(repository),
                    )
                )
        for suite in self.manifest.integration_suites:
            if set(suite.repositories) <= affected and suite.key not in state.snapshot.integration_qa:
                requests.append(
                    ChildRequest(
                        suite.key,
                        suite.command_repository,
                        suite.key,
                        "integration_qa",
                        ordinal,
                        state.snapshot.attempt,
                        state.snapshot.candidate_shas,
                    )
                )
        return tuple(requests)

    def _failure_bundle(
        self,
        state: WorkflowState,
        decision: ParentDecision,
    ) -> FailureBundle:
        if state.metadata is None or decision.next_attempt is None:
            raise WorkflowError("repair bundle lacks its parent transition identity")
        current = tuple(
            child
            for child in state.children
            if child.stage_ordinal == state.metadata.stage_ordinal
            and child.attempt == state.snapshot.attempt
        )
        if not current:
            raise WorkflowError("repair bundle lacks a complete source Stage")
        affected = frozenset(state.snapshot.affected_repositories)
        applicable_suites = {
            suite.key: frozenset(suite.repositories)
            for suite in self.manifest.integration_suites
            if set(suite.repositories) <= affected
        }
        current_identities = tuple(
            (child.phase, child.target_key, child.suite_key)
            for child in current
        )
        expected_identities = {
            (phase, repository, "")
            for repository in affected
            for phase in ("review", "qa")
        }
        expected_identities.update(
            ("integration_qa", suite_key, suite_key)
            for suite_key in applicable_suites
        )
        if (
            len(set(current_identities)) != len(current_identities)
            or set(current_identities) != expected_identities
        ):
            raise WorkflowError(
                "repair bundle source Stage gate membership is incomplete or conflicting"
            )
        expected_nonpass = {
            (phase, repository, "")
            for phase, evidence_by_repository in (
                ("review", state.snapshot.reviews),
                ("qa", state.snapshot.qa),
            )
            for repository, evidence in evidence_by_repository.items()
            if evidence.result in {"fail", "blocked"}
        }
        expected_nonpass.update(
            ("integration_qa", suite_key, suite_key)
            for suite_key, evidence in state.snapshot.integration_qa.items()
            if evidence.result in {"fail", "blocked"}
        )
        if not expected_nonpass <= set(current_identities):
            raise WorkflowError(
                "repair bundle lacks a current child for terminal non-PASS gate evidence"
            )
        failures: list[FailureEvidenceRef] = []
        evidence_uuids: set[str] = set()
        for child in current:
            if (
                child.phase not in {"review", "qa", "integration_qa"}
                or child.active
                or child.status not in _TERMINAL_CHILD_STATUSES
                or dict(child.creation_candidate_shas)
                != dict(state.snapshot.candidate_shas)
                or child.phase_result not in _PHASE_RESULTS
                or not _canonical_uuid(child.evidence_comment_uuid)
                or not _https_evidence_url(child.evidence_comment_url)
                or child.evidence_comment_uuid in evidence_uuids
            ):
                raise WorkflowError("repair bundle source Stage evidence is incomplete")
            evidence_uuids.add(child.evidence_comment_uuid)

            if child.phase in {"review", "qa"}:
                evidence_by_repository = (
                    state.snapshot.reviews if child.phase == "review" else state.snapshot.qa
                )
                evidence = evidence_by_repository.get(child.repository_key)
                expected_owners = (child.repository_key,) if child.phase_result != "pass" else ()
                if (
                    evidence is None
                    or evidence.candidate_sha
                    != state.snapshot.candidate_shas.get(child.repository_key)
                    or evidence.result != child.phase_result
                    or child.target_key != child.repository_key
                    or child.suite_key
                    or child.responsible_repositories != expected_owners
                ):
                    raise WorkflowError("repair bundle repository evidence is inconsistent")
            else:
                suite_repositories = applicable_suites.get(child.suite_key)
                evidence = state.snapshot.integration_qa.get(child.suite_key)
                if (
                    suite_repositories is None
                    or evidence is None
                    or dict(evidence.candidate_shas)
                    != dict(state.snapshot.candidate_shas)
                    or evidence.result != child.phase_result
                    or (
                        child.phase_result == "pass"
                        and child.responsible_repositories
                    )
                    or (
                        child.phase_result != "pass"
                        and (
                            not child.responsible_repositories
                            or not set(child.responsible_repositories)
                            <= suite_repositories
                        )
                    )
                ):
                    raise WorkflowError("repair bundle integration evidence is inconsistent")

            if child.phase_result != "pass":
                failures.append(
                    FailureEvidenceRef(
                        child_identifier=child.identifier,
                        phase=child.phase,
                        result=child.phase_result,
                        stage_ordinal=child.stage_ordinal,
                        repair_round=child.attempt,
                        candidate_shas=child.creation_candidate_shas,
                        responsible_repositories=child.responsible_repositories,
                        evidence_comment_uuid=child.evidence_comment_uuid,
                        evidence_comment_url=child.evidence_comment_url,
                        suite_key=child.suite_key,
                    )
                )
        owners = frozenset(
            repository
            for failure in failures
            for repository in failure.responsible_repositories
        )
        if not failures or owners != frozenset(decision.repositories):
            raise WorkflowError("repair decision disagrees with complete failure ownership")
        return FailureBundle.build(
            state.parent_identifier,
            state.metadata.workflow_version,
            state.metadata.stage_ordinal,
            decision.next_attempt,
            state.snapshot.candidate_shas,
            tuple(failures),
        )

    def _repair_authority(
        self,
        state: WorkflowState,
        bundle: FailureBundle,
    ) -> tuple[int, bool]:
        if state.metadata is None:
            raise WorkflowError("repair authority lacks parent metadata")
        metadata = state.metadata
        next_round = metadata.repair_round + 1
        automatic_limit = self.manifest.policy.max_repair_attempts
        if bundle.repair_round != next_round:
            raise WorkflowError("repair authority does not match the failure bundle round")
        if next_round <= automatic_limit:
            if metadata.automatic_repairs_used != metadata.repair_round:
                raise WorkflowError("automatic repair accounting is not contiguous")
            return next_round, True
        if (
            next_round != automatic_limit + 1
            or metadata.repair_round != automatic_limit
            or metadata.automatic_repairs_used != automatic_limit
        ):
            raise WorkflowError("only one member-authorized repair round is allowed")
        authorization = metadata.repair_authorization
        if (
            authorization is None
            or authorization.bundle_digest != bundle.digest
            or authorization.granted_round != next_round
            or any(
                child.authorizing_comment_uuid == authorization.comment_uuid
                for child in state.children
            )
        ):
            raise WorkflowError("repair authorization is absent, stale, or already consumed")
        try:
            comment = self.snapshot_reader.read_authorizing_comment(
                state.parent_identifier,
                authorization.comment_uuid,
            )
        except Exception as error:
            raise WorkflowError("repair authorization comment could not be reread") from error
        if (
            type(comment) is not AuthorizingComment
            or comment.comment_uuid != authorization.comment_uuid
            or comment.comment_url != authorization.comment_url
            or comment.author_type != "member"
        ):
            raise WorkflowError("repair authorization comment is not authoritative")
        return next_round, False

    @staticmethod
    def _repair_requests(
        state: WorkflowState,
        decision: ParentDecision,
        bundle: FailureBundle,
        ordinal: int,
        authorizing_comment_uuid: str = "",
    ) -> tuple[ChildRequest, ...]:
        assert decision.next_attempt is not None
        return tuple(
            ChildRequest(
                target_key=repository,
                repository_key=repository,
                suite_key="",
                phase="repair",
                stage_ordinal=ordinal,
                attempt=decision.next_attempt,
                candidate_shas=state.snapshot.candidate_shas,
                pull_request=state.pull_requests[repository],
                failure_bundle=bundle,
                failure_refs=bundle.for_repository(repository),
                authorizing_comment_uuid=authorizing_comment_uuid,
            )
            for repository in decision.repositories
        )

    @staticmethod
    def _has_successor(state: WorkflowState, requests: tuple[ChildRequest, ...]) -> bool:
        wanted = {
            (request.target_key, request.phase, request.attempt)
            for request in requests
        }
        return any(
            (child.target_key, child.phase, child.attempt) in wanted
            for child in state.children
        )

    def _dispatch(
        self,
        state: WorkflowState,
        decision: ParentDecision,
        *,
        repair: bool = False,
    ) -> WorkflowResult:
        if state.metadata is None:
            return self._block(state, "parent has no initialized workflow metadata")
        ordinal = state.metadata.stage_ordinal + 1
        bundle: FailureBundle | None = None
        automatic_repair: bool | None = None
        authorizing_comment_uuid = ""
        if repair:
            if decision.next_attempt is None:
                return self._block(state, "repair decision lacks the next attempt")
            missing_prs = set(decision.repositories) - state.pull_requests.keys()
            if missing_prs:
                return self._block(state, "repair cannot identify every existing pull request")
            for repository in decision.repositories:
                target = state.pull_requests[repository]
                specification = self.manifest.repositories[repository]
                expected_url = (
                    f"https://github.com/{specification.github}/pull/{target.number}"
                )
                if (
                    target.repository_key != repository
                    or target.url != expected_url
                    or state.snapshot.pull_requests.get(repository) is None
                ):
                    return self._block(
                        state,
                        "repair pull request target is outside the manifest repository",
                    )
            try:
                bundle = self._failure_bundle(state, decision)
                authority_round, automatic_repair = self._repair_authority(state, bundle)
                if authority_round != decision.next_attempt:
                    raise WorkflowError("repair decision disagrees with its authority")
                if not automatic_repair:
                    assert state.metadata.repair_authorization is not None
                    authorizing_comment_uuid = (
                        state.metadata.repair_authorization.comment_uuid
                    )
                requests = self._repair_requests(
                    state,
                    decision,
                    bundle,
                    ordinal,
                    authorizing_comment_uuid,
                )
            except (MetadataError, WorkflowError, TypeError, ValueError):
                return self._zero_mutation_block(
                    state,
                    "complete failure evidence or repair authority could not be validated",
                )
            stage_kind = "repair"
            attempt = decision.next_attempt
            next_action = "repair"
            fan_in_problem = self._parent_fan_in_still_current(state)
            if fan_in_problem is not None:
                return fan_in_problem
        else:
            if decision.dispatch_kind is DispatchKind.GATES:
                requests = self._gate_requests(state, ordinal)
                stage_kind = "gates"
            elif decision.dispatch_kind is DispatchKind.IMPLEMENTATION:
                requests = self._implementation_requests(
                    state,
                    decision.repositories,
                    ordinal,
                )
                stage_kind = "implementation"
            else:
                return self._block(state, "dispatch decision has no supported typed phase")
            attempt = state.snapshot.attempt
            next_action = "dispatch"
        if not requests:
            return self._result(state, "noop", "requested successor already exists")
        key = self._action_key(
            state,
            stage_kind,
            ordinal,
            attempt=attempt,
            failure_bundle_digest="" if bundle is None else bundle.digest,
            authorizing_comment_uuid=authorizing_comment_uuid,
        )

        def request_identity(request: ChildRequest) -> tuple[object, ...]:
            return (
                request.target_key,
                request.repository_key,
                request.suite_key,
                request.phase,
                request.stage_ordinal,
                request.attempt,
                key,
                tuple(request.candidate_shas.items()),
                "" if request.failure_bundle is None else request.failure_bundle.digest,
                _failure_uuid_partition(request.failure_refs),
                request.authorizing_comment_uuid,
            )

        def child_identity(child: WorkflowChild) -> tuple[object, ...]:
            return (
                child.target_key,
                child.repository_key,
                child.suite_key,
                child.phase,
                child.stage_ordinal,
                child.attempt,
                child.action_key,
                tuple(child.creation_candidate_shas.items()),
                child.failure_bundle_digest,
                child.failure_evidence_uuids,
                child.authorizing_comment_uuid,
            )

        wanted = Counter(request_identity(request) for request in requests)

        def relevant_repair_children(
            workflow_state: WorkflowState,
        ) -> tuple[WorkflowChild, ...]:
            repositories = frozenset(decision.repositories)
            return tuple(
                child
                for child in workflow_state.children
                if child.phase == "repair"
                and child.attempt == decision.next_attempt
                and (
                    child.target_key in repositories
                    or child.repository_key in repositories
                )
            )

        if repair:
            observed_successors = Counter(
                child_identity(child)
                for child in relevant_repair_children(state)
            )
            if observed_successors:
                if (
                    observed_successors == wanted
                    and key in state.applied_action_keys
                ):
                    return self._result(state, "noop", "repair bundle successors already exist")
                return self._zero_mutation_block(
                    state,
                    "repair successor bundle identity conflicts with the complete failure bundle",
                )
        elif self._has_successor(state, requests):
            return self._result(state, "noop", "an intended successor already exists")
        if key in state.applied_action_keys:
            return self._result(state, "noop", "coordinator action already exists", action_key=key)
        metadata = self._metadata(
            state,
            action_key=key,
            stage_ordinal=ordinal,
            attempt=attempt if repair else None,
            automatic_repair=automatic_repair,
            consume_repair_authorization=(repair and automatic_repair is False),
        )
        try:
            self.executor.create_children(
                state.parent_identifier,
                requests,
                metadata,
                action_key=key,
            )
        except Exception:
            # Reconcile because the effect may have committed before failing.
            pass

        def successor_creation_matches(current: object) -> bool:
            if (
                not isinstance(current, WorkflowState)
                or current.parent_identifier != state.parent_identifier
                or current.metadata != metadata
                or key not in current.applied_action_keys
            ):
                return False
            if repair:
                observed_successors = Counter(
                    child_identity(child)
                    for child in relevant_repair_children(current)
                )
                return observed_successors == wanted
            observed_children = Counter(
                child_identity(child)
                for child in current.children
            )
            return all(
                observed_children[identity] >= count
                for identity, count in wanted.items()
            )

        observed = self._reconcile_parent(
            state.parent_identifier,
            successor_creation_matches,
        )
        if observed is None:
            return self._uncertain(
                state,
                "successor creation is not yet authoritatively observable",
                action_key=key,
                mutation_count=1,
            )
        state = observed
        return self._result(
            state,
            next_action,
            decision.reason,
            created=tuple((request.target_key, request.phase) for request in requests),
            action_key=key,
            mutation_count=1,
        )

    def resume_parent(self, parent_identifier: str) -> WorkflowResult:
        try:
            state = self.snapshot_reader.read(parent_identifier)
        except Exception:
            return WorkflowResult(parent_identifier, "blocked", "block", "parent could not be read")
        legacy_completion = self._completed_legacy_replay(state, parent_identifier)
        if legacy_completion is not None:
            return legacy_completion
        problem_result = self._state_problem_result(state, parent_identifier)
        if problem_result is not None:
            return problem_result
        if state.parent_status == "done":
            assert state.metadata is not None
            completed = self._parent_decision(state)
            key = self._action_key(state, "complete", state.metadata.stage_ordinal)
            if (
                completed.kind is not DecisionKind.COMPLETE
                or state.metadata.last_action != key
                or key not in state.applied_action_keys
            ):
                return self._uncertain(
                    state,
                    "parent done status is not bound to its exact completion transition",
                    action_key=key,
                )
            return self._result(
                state,
                "noop",
                "parent completion is already authoritative",
                action_key=key,
            )
        if state.parent_status not in _ACTIVE_PARENT_STATUSES:
            return self._result(state, "noop", "parent is not active")
        if state.human_wait:
            return self._result(state, "wait", "parent is waiting for a human")
        repair_stage, repair_head_problem = self._current_repair_head_problem(state)
        if repair_head_problem is not None:
            return self._zero_mutation_block(state, repair_head_problem)
        if not repair_stage and not self._candidate_heads_match(state):
            return self._zero_mutation_block(
                state,
                "out-of-band pull-request head change",
            )
        if repair_stage:
            fan_in_problem = self._parent_fan_in_still_current(state)
            if fan_in_problem is not None:
                return fan_in_problem
        assert state.metadata is not None
        automatic_limit = self.manifest.policy.max_repair_attempts
        if state.metadata.repair_round == automatic_limit + 1:
            current_children = tuple(
                child
                for child in state.children
                if child.stage_ordinal == state.metadata.stage_ordinal
                and child.attempt == state.metadata.repair_round
            )
            if (
                current_children
                and all(
                    child.phase == "repair"
                    and child.status in _ACTIVE_CHILD_STATUSES
                    and child.active
                    and child.authorizing_comment_uuid
                    and child.action_key == state.metadata.last_action
                    for child in current_children
                )
                and state.metadata.last_action in state.applied_action_keys
            ):
                return self._result(
                    state,
                    "noop",
                    "member-authorized repair successors already exist",
                    action_key=state.metadata.last_action,
                )
        decision = self._parent_decision(state)
        if (
            decision.kind is DecisionKind.BLOCK
            and decision.reason.endswith("automatic repair attempt limit exhausted")
        ):
            if state.metadata.repair_round != automatic_limit:
                return self._zero_mutation_block(
                    state,
                    "member-authorized repair round is already consumed",
                )
            prior = replace(state.snapshot, attempt=automatic_limit - 1)
            repair_decision = decide_parent_action(self.manifest, prior)
            if repair_decision.kind is not DecisionKind.REPAIR:
                return self._zero_mutation_block(
                    state,
                    "automatic repair exhaustion could not be reconstructed",
                )
            decision = replace(
                repair_decision,
                next_attempt=automatic_limit + 1,
            )
        if decision.kind in {
            DecisionKind.DISPATCH,
            DecisionKind.REPAIR,
            DecisionKind.MERGE,
            DecisionKind.SMOKE,
            DecisionKind.COMPLETE,
        }:
            stage_wait = self._current_stage_wait(state)
            if stage_wait is not None:
                return stage_wait
        if decision.kind is DecisionKind.DISPATCH:
            if state.snapshot.stalled:
                return self._result(state, "wait", "stalled work is reserved for bounded recovery")
            return self._dispatch(state, decision)
        if decision.kind is DecisionKind.REPAIR:
            return self._dispatch(state, decision, repair=True)
        if decision.kind is DecisionKind.MERGE:
            return self.execute_merge_plan(parent_identifier)
        if decision.kind is DecisionKind.SMOKE:
            return self._result(state, "smoke", decision.reason)
        if decision.kind is DecisionKind.WAIT:
            return self._result(state, "wait", decision.reason)
        if decision.kind is DecisionKind.BLOCK:
            return self._block(state, decision.reason)
        if decision.kind is not DecisionKind.COMPLETE:
            return self._block(state, "parent decision is unsupported")

        try:
            fresh = self.snapshot_reader.read(parent_identifier)
        except Exception:
            return self._uncertain(
                state,
                "parent could not be reread before completion",
            )
        fresh_problem = self._state_problem_result(fresh, parent_identifier)
        if fresh_problem is not None:
            return fresh_problem
        if fresh != state or self._parent_decision(fresh) != decision:
            return self._result(fresh, "noop", "parent changed before completion")
        assert fresh.metadata is not None
        key = self._action_key(fresh, "complete", fresh.metadata.stage_ordinal)
        if key in fresh.applied_action_keys:
            return self._uncertain(
                fresh,
                "parent completion action exists without its done transition",
                action_key=key,
            )
        metadata = self._metadata(fresh, action_key=key)
        try:
            self.executor.set_parent_status(
                parent_identifier,
                "done",
                decision.reason,
                metadata,
                action_key=key,
            )
        except Exception:
            # The write may have committed. Reconcile before allowing any retry.
            pass
        expected_action_keys = fresh.applied_action_keys | {key}
        expected_state = replace(
            fresh,
            parent_status="done",
            metadata=metadata,
            applied_action_keys=expected_action_keys,
        )
        observed = self._reconcile_parent(
            parent_identifier,
            lambda current: (
                current == expected_state
                and current.parent_identifier == parent_identifier
                and current.parent_status == "done"
                and current.metadata == metadata
                and current.metadata.stage_ordinal == metadata.stage_ordinal
                and current.metadata.last_action == key
                and current.applied_action_keys == expected_action_keys
                and key in current.applied_action_keys
            ),
        )
        if observed is None:
            return self._uncertain(
                fresh,
                "parent completion transition is not yet authoritatively observable",
                action_key=key,
                mutation_count=1,
            )
        return self._result(observed, "complete", decision.reason, action_key=key, mutation_count=1)

    def _completed_sibling_candidate_changes(
        self,
        state: WorkflowState,
        child: WorkflowChild,
    ) -> Mapping[str, str]:
        """Return candidate changes proven by completed siblings from one creation wave."""

        eligible: dict[str, tuple[WorkflowChild, str]] = {}
        for sibling in state.children:
            if (
                sibling.identifier == child.identifier
                or sibling.repository_key == child.repository_key
                or sibling.phase != child.phase
                or sibling.stage_ordinal != child.stage_ordinal
                or sibling.attempt != child.attempt
                or sibling.action_key != child.action_key
                or sibling.creation_candidate_shas != child.creation_candidate_shas
                or sibling.failure_bundle_digest != child.failure_bundle_digest
                or sibling.authorizing_comment_uuid != child.authorizing_comment_uuid
                or sibling.active
                or sibling.status not in _TERMINAL_CHILD_STATUSES
                or sibling.phase_result != "pass"
                or not _canonical_uuid(sibling.evidence_comment_uuid)
                or not _https_evidence_url(sibling.evidence_comment_url)
                or sibling.target_key != sibling.repository_key
                or sibling.suite_key
            ):
                continue
            repository = sibling.repository_key
            evidence = state.snapshot.children.get(repository)
            pull_request = state.snapshot.pull_requests.get(repository)
            candidate = state.snapshot.candidate_shas.get(repository)
            if (
                evidence is None
                or evidence.result != "pass"
                or evidence.candidate_sha != candidate
                or pull_request is None
                or pull_request.head_sha != candidate
            ):
                continue
            if child.creation_candidate_shas.get(repository) != candidate:
                eligible[repository] = (sibling, candidate)

        target = dict(child.creation_candidate_shas)
        target.update(
            {repository: candidate for repository, (_, candidate) in eligible.items()}
        )
        if target != dict(state.snapshot.candidate_shas):
            return MappingProxyType({})

        def completion_chain(
            candidates: dict[str, str],
            remaining: frozenset[str],
        ) -> dict[str, str] | None:
            if not remaining:
                return {}
            for repository in sorted(remaining):
                sibling, candidate = eligible[repository]
                action_candidates = {**candidates, repository: candidate}
                completion_action = self._action_key(
                    state,
                    f"{sibling.phase}:{sibling.target_key}",
                    sibling.stage_ordinal,
                    attempt=sibling.attempt,
                    candidate_shas=action_candidates,
                    failure_bundle_digest=(
                        sibling.failure_bundle_digest
                        if sibling.phase == "repair"
                        else ""
                    ),
                    authorizing_comment_uuid=(
                        sibling.authorizing_comment_uuid
                        if sibling.phase == "repair"
                        else ""
                    ),
                )
                if (
                    completion_action == sibling.action_key
                    or completion_action not in state.applied_action_keys
                ):
                    continue
                tail = completion_chain(
                    action_candidates,
                    remaining - {repository},
                )
                if tail is not None:
                    return {repository: candidate, **tail}
            return None

        changes = completion_chain(
            dict(child.creation_candidate_shas),
            frozenset(eligible),
        )
        return MappingProxyType(changes or {})

    def _creation_candidates_match(
        self,
        state: WorkflowState,
        child: WorkflowChild,
    ) -> bool:
        creation = dict(child.creation_candidate_shas)
        current = dict(state.snapshot.candidate_shas)
        if child.phase not in {"implementation", "repair"}:
            return creation == current
        sibling_changes = dict(self._completed_sibling_candidate_changes(state, child))
        expected = dict(creation)
        expected.update(sibling_changes)
        return expected == current

    def _completion_problem(
        self,
        state: WorkflowState,
        completion: PhaseCompletion,
    ) -> tuple[str | None, WorkflowChild | None]:
        schema_problem = _phase_completion_schema_problem(
            completion,
            manifest=self.manifest,
        )
        if schema_problem is not None:
            return schema_problem, None
        if completion.parent_identifier != state.parent_identifier:
            return "phase completion names the wrong parent", None
        if completion.phase not in _PHASES - {"smoke"} or completion.result not in _PHASE_RESULTS:
            return "phase completion kind or result is unsupported", None
        if (
            not isinstance(completion.attempt, int)
            or isinstance(completion.attempt, bool)
            or not 0 <= completion.attempt <= self.manifest.policy.max_repair_attempts + 1
            or completion.attempt != state.snapshot.attempt
        ):
            return "phase completion attempt is not current", None
        if completion.repository_key not in state.snapshot.affected_repositories:
            return "phase completion repository is not affected", None
        if not _valid_sha(completion.candidate_sha):
            return "phase completion candidate SHA is malformed", None
        try:
            observed_uuid = str(uuid.UUID(completion.evidence_comment_uuid))
        except (ValueError, AttributeError, TypeError):
            return "phase completion evidence UUID is malformed", None
        evidence_url = urlsplit(completion.evidence_comment_url)
        if (
            observed_uuid != completion.evidence_comment_uuid
            or evidence_url.scheme != "https"
            or not evidence_url.netloc
            or evidence_url.username is not None
            or evidence_url.password is not None
        ):
            return "phase completion evidence is malformed", None
        repository = self.manifest.repositories[completion.repository_key]
        if completion.pull_request_url:
            target = state.pull_requests.get(completion.repository_key)
            parsed = urlsplit(completion.pull_request_url)
            prefix = f"/{repository.github}/pull/"
            if (
                parsed.scheme != "https"
                or parsed.netloc != "github.com"
                or not parsed.path.startswith(prefix)
                or not parsed.path[len(prefix):].isdigit()
                or parsed.query
                or parsed.fragment
            ):
                return "phase completion pull request is outside the managed repository", None
            if completion.phase == "repair" and (
                target is None or target.url != completion.pull_request_url
            ):
                return "repair must update the existing repository pull request", None
        elif completion.phase in {"implementation", "repair"}:
            return "implementation and repair completions require a pull request", None

        if completion.phase == "integration_qa":
            suites = {
                suite.key: suite
                for suite in self.manifest.integration_suites
                if set(suite.repositories) <= set(state.snapshot.affected_repositories)
            }
            if completion.suite_key not in suites:
                return "integration QA suite is not applicable", None
            suite = suites[completion.suite_key]
            completion_shas = dict(completion.candidate_shas)
            if (
                completion.repository_key != suite.command_repository
                or completion_shas != dict(state.snapshot.candidate_shas)
                or any(not _valid_sha(sha) for sha in completion_shas.values())
                or completion_shas.get(suite.command_repository) != completion.candidate_sha
            ):
                return "integration QA completion target or exact SHA map is malformed", None
        elif completion.suite_key or completion.candidate_shas:
            return "repository phase completion contains integration-only fields", None

        matching = tuple(
            child
            for child in state.children
            if child.phase == completion.phase
            and child.attempt == completion.attempt
            and state.metadata is not None
            and child.stage_ordinal == state.metadata.stage_ordinal
            and child.active
            and child.status in _ACTIVE_CHILD_STATUSES
            and child.action_key in state.applied_action_keys
            and child.action_key.startswith(
                {
                    "implementation": "dispatch:",
                    "repair": "repair:",
                    "review": "stage:",
                    "qa": "stage:",
                    "integration_qa": "stage:",
                }[completion.phase]
            )
            and (
                child.repository_key == completion.repository_key
                if completion.phase != "integration_qa"
                else (
                    child.target_key == completion.suite_key
                    and child.suite_key == completion.suite_key
                    and child.repository_key == completion.repository_key
                )
            )
        )
        if len(matching) != 1:
            return "phase completion does not resolve one active authoritative current-stage child", None
        if (
            completion.phase == "repair"
            and matching[0].failure_bundle_digest != completion.failure_bundle_digest
        ):
            return "repair completion failure bundle digest does not match assigned child", None
        child = matching[0]
        creation_stage_kind = (
            "gates"
            if completion.phase in {"review", "qa", "integration_qa"}
            else completion.phase
        )
        expected_creation_key = self._action_key(
            state,
            creation_stage_kind,
            child.stage_ordinal,
            attempt=child.attempt,
            candidate_shas=child.creation_candidate_shas,
            failure_bundle_digest=(
                child.failure_bundle_digest
                if completion.phase == "repair"
                else ""
            ),
            authorizing_comment_uuid=(
                child.authorizing_comment_uuid
                if completion.phase == "repair"
                else ""
            ),
        )
        if (
            child.action_key != expected_creation_key
            or expected_creation_key not in state.applied_action_keys
            or not self._creation_candidates_match(state, child)
        ):
            return "phase completion child creation provenance is not current", None
        return None, child

    @staticmethod
    def _zero_mutation_block(
        state: WorkflowState,
        reason: str,
    ) -> WorkflowResult:
        return WorkflowResult(
            state.parent_identifier,
            "blocked",
            "block",
            reason,
            merge_state=state.snapshot.merge_state,
            mutation_count=0,
        )

    def _phase_replay_result(
        self,
        state: WorkflowState,
        completion: object,
    ) -> WorkflowResult | None:
        """Return an exact historical replay result, or None for genuinely new evidence."""

        if type(completion) is not PhaseCompletion:
            return None
        try:
            evidence_id = str(uuid.UUID(completion.evidence_comment_uuid))
        except (AttributeError, TypeError, ValueError):
            return None
        if evidence_id != completion.evidence_comment_uuid:
            return None
        reads: list[PhaseCompletion | None] = []
        for _ in range(2):
            try:
                reads.append(
                    self.snapshot_reader.read_phase_completion(
                        state.parent_identifier,
                        completion.evidence_comment_uuid,
                    )
                )
            except Exception:
                return self._uncertain(
                    state,
                    "existing phase evidence could not be read authoritatively",
                )
        if reads[0] != reads[1]:
            return self._zero_mutation_block(
                state,
                "existing phase evidence changed during authoritative read",
            )
        persisted = reads[0]
        if persisted is None:
            return None
        if (
            _phase_completion_schema_problem(
                persisted,
                manifest=self.manifest,
            )
            is not None
            or persisted != completion
        ):
            return self._zero_mutation_block(
                state,
                "phase completion evidence identity conflicts with persisted evidence",
            )
        expected_target = (
            completion.suite_key
            if completion.phase == "integration_qa"
            else completion.repository_key
        )
        expected_suite = (
            completion.suite_key if completion.phase == "integration_qa" else ""
        )
        completed = tuple(
            child
            for child in state.children
            if child.status == "done"
            and not child.active
            and child.evidence_comment_uuid == completion.evidence_comment_uuid
            and child.repository_key == completion.repository_key
            and child.target_key == expected_target
            and child.suite_key == expected_suite
            and child.phase == completion.phase
            and child.attempt == completion.attempt
            and child.action_key in state.applied_action_keys
        )
        if len(completed) != 1:
            return self._zero_mutation_block(
                state,
                "persisted phase evidence lacks one authoritative child transition",
            )
        if (
            completed[0].phase_result != completion.result
            or completed[0].evidence_comment_url != completion.evidence_comment_url
            or completed[0].responsible_repositories
            != completion.responsible_repositories
            or (
                completion.phase == "repair"
                and completed[0].failure_bundle_digest
                != completion.failure_bundle_digest
            )
        ):
            return self._zero_mutation_block(
                state,
                "persisted phase evidence conflicts with the completed child",
            )
        candidate_identity = (
            dict(completion.candidate_shas) == dict(state.snapshot.candidate_shas)
            and completion.candidate_shas.get(completion.repository_key)
            == completion.candidate_sha
            if completion.phase == "integration_qa"
            else state.snapshot.candidate_shas.get(completion.repository_key)
            == completion.candidate_sha
        )
        if not candidate_identity:
            return self._zero_mutation_block(
                state,
                "persisted phase evidence lacks the current candidate identity",
            )
        creation_stage_kind = (
            "gates"
            if completion.phase in {"review", "qa", "integration_qa"}
            else completion.phase
        )
        expected_creation_key = self._action_key(
            state,
            creation_stage_kind,
            completed[0].stage_ordinal,
            attempt=completed[0].attempt,
            candidate_shas=completed[0].creation_candidate_shas,
            failure_bundle_digest=(
                completed[0].failure_bundle_digest
                if completion.phase == "repair"
                else ""
            ),
            authorizing_comment_uuid=(
                completed[0].authorizing_comment_uuid
                if completion.phase == "repair"
                else ""
            ),
        )
        unchanged_creation_candidates = all(
            repository == completion.repository_key
            or state.snapshot.candidate_shas.get(repository) == candidate_sha
            for repository, candidate_sha in completed[0].creation_candidate_shas.items()
        )
        gate_creation_candidates = (
            dict(completed[0].creation_candidate_shas)
            == dict(state.snapshot.candidate_shas)
            if completion.phase in {"review", "qa", "integration_qa"}
            else unchanged_creation_candidates
        )
        if (
            completed[0].action_key != expected_creation_key
            or expected_creation_key not in state.applied_action_keys
            or not gate_creation_candidates
        ):
            return self._zero_mutation_block(
                state,
                "persisted phase evidence lacks exact child creation provenance",
            )
        action_candidates = (
            completion.candidate_shas
            if completion.phase == "integration_qa"
            else {
                **state.snapshot.candidate_shas,
                completion.repository_key: completion.candidate_sha,
            }
        )
        expected_completion_key = self._action_key(
            state,
            f"{completion.phase}:{completed[0].target_key}",
            completed[0].stage_ordinal,
            attempt=completion.attempt,
            candidate_shas=action_candidates,
            failure_bundle_digest=(
                completion.failure_bundle_digest
                if completion.phase == "repair"
                else ""
            ),
            authorizing_comment_uuid=(
                completed[0].authorizing_comment_uuid
                if completion.phase == "repair"
                else ""
            ),
        )
        if (
            expected_completion_key == completed[0].action_key
            or expected_completion_key not in state.applied_action_keys
        ):
            return self._zero_mutation_block(
                state,
                "persisted phase evidence lacks an authoritative completion action",
            )
        fan_in_problem = self._parent_fan_in_still_current(state)
        if fan_in_problem is not None:
            return fan_in_problem
        return self._result(
            state,
            "noop",
            "phase completion already exists",
            completed_child_status="done",
            action_key=expected_completion_key,
        )

    def record_phase_completion(self, completion: PhaseCompletion) -> WorkflowResult:
        parent_identifier = ""
        try:
            if (
                type(completion) is PhaseCompletion
                and type(completion.parent_identifier) is str
                and _ISSUE_IDENTIFIER.fullmatch(completion.parent_identifier) is not None
            ):
                parent_identifier = completion.parent_identifier
        except BaseException:
            pass
        completion_schema_problem = _phase_completion_schema_problem(
            completion,
            manifest=self.manifest,
        )
        if completion_schema_problem is not None:
            return WorkflowResult(
                parent_identifier,
                "blocked",
                "block",
                completion_schema_problem,
                mutation_count=0,
            )
        try:
            state = self.snapshot_reader.read(parent_identifier)
        except Exception:
            return WorkflowResult(parent_identifier, "blocked", "block", "parent could not be read")
        problem_result = self._state_problem_result(state, parent_identifier)
        if problem_result is not None:
            return problem_result
        replay = self._phase_replay_result(state, completion)
        if replay is not None:
            return replay
        if (
            state.parent_status not in _ACTIVE_PARENT_STATUSES
            or state.metadata is None
            or state.snapshot.merged_shas
            or state.snapshot.merge_state
            not in {"pending", "not_ready", "ready", "blocked"}
        ):
            return self._zero_mutation_block(
                state,
                "new phase evidence is allowed only for an active pre-merge parent",
            )
        missing_head_evidence = set(state.pull_requests) - set(
            state.snapshot.pull_requests
        )
        if missing_head_evidence:
            return self._zero_mutation_block(
                state,
                "managed pull request lacks authoritative head evidence",
            )
        repair_stage, repair_head_problem = self._current_repair_head_problem(
            state,
            completion,
        )
        if repair_head_problem is not None:
            return self._zero_mutation_block(state, repair_head_problem)
        mismatched_heads = {
            repository
            for repository, pull_request in state.snapshot.pull_requests.items()
            if state.snapshot.candidate_shas.get(repository) != pull_request.head_sha
        }
        if not repair_stage and mismatched_heads and (
            completion.phase not in {"implementation", "repair"}
            or completion.result != "pass"
            or mismatched_heads != {completion.repository_key}
            or state.snapshot.pull_requests[completion.repository_key].head_sha
            != completion.candidate_sha
        ):
            return self._zero_mutation_block(
                state,
                "out-of-band pull-request head change",
            )
        if (
            completion.phase in {"implementation", "repair"}
            and completion.result != "pass"
            and completion.repository_key in state.snapshot.candidate_shas
            and state.snapshot.candidate_shas[completion.repository_key]
            != completion.candidate_sha
        ):
            return self._zero_mutation_block(
                state,
                "non-PASS completion cannot replace a candidate SHA",
            )
        completion_problem, child = self._completion_problem(state, completion)
        if completion_problem is not None or child is None:
            return self._zero_mutation_block(
                state,
                completion_problem or "phase completion child is missing",
            )
        previous_candidate_sha = state.snapshot.candidate_shas.get(
            completion.repository_key
        )
        candidate_shas = dict(state.snapshot.candidate_shas)
        if (
            completion.phase in {"implementation", "repair"}
            and completion.result == "pass"
        ):
            candidate_shas[completion.repository_key] = completion.candidate_sha
        action_candidates = (
            completion.candidate_shas
            if completion.phase == "integration_qa"
            else {
                **state.snapshot.candidate_shas,
                completion.repository_key: completion.candidate_sha,
            }
        )
        key = self._action_key(
            state,
            f"{completion.phase}:{child.target_key}",
            child.stage_ordinal,
            attempt=completion.attempt,
            candidate_shas=action_candidates,
            failure_bundle_digest=(
                completion.failure_bundle_digest
                if completion.phase == "repair"
                else ""
            ),
            authorizing_comment_uuid=(
                child.authorizing_comment_uuid
                if completion.phase == "repair"
                else ""
            ),
        )
        merge_plan = state.metadata.merge_plan if state.metadata is not None else ()
        merge_state = state.metadata.merge_state if state.metadata is not None else "pending"
        if dict(candidate_shas) != dict(state.snapshot.candidate_shas):
            merge_plan = ()
            merge_state = "pending"
        metadata = self._metadata(
            state,
            action_key=key,
            candidate_shas=candidate_shas,
            merge_plan=merge_plan,
            merge_state=merge_state,
        )
        fan_in_problem = self._parent_fan_in_still_current(state)
        if fan_in_problem is not None:
            return fan_in_problem
        try:
            self.executor.write_phase_completion(completion, action_key=key)
        except Exception:
            # Reconcile because the effect may have committed before failing.
            pass
        observed: PhaseCompletion | None = None
        evidence_read_succeeded = False
        for _ in range(2):
            try:
                candidate = self.snapshot_reader.read_phase_completion(
                    parent_identifier,
                    completion.evidence_comment_uuid,
                )
            except Exception:
                continue
            evidence_read_succeeded = True
            if candidate == completion:
                observed = candidate
                break
        if observed is None:
            if evidence_read_succeeded:
                return WorkflowResult(
                    parent_identifier,
                    "blocked",
                    "block",
                    "phase completion evidence reread did not match",
                    action_key=key,
                    mutation_count=1,
                )
            return self._uncertain(
                state,
                "phase completion evidence is not yet authoritatively observable",
                action_key=key,
                mutation_count=1,
            )
        verified_state: WorkflowState | None = None
        parent_read_succeeded = False
        for _ in range(2):
            try:
                candidate_state = self.snapshot_reader.read(parent_identifier)
            except Exception:
                continue
            parent_read_succeeded = True
            verified_state = candidate_state
            if candidate_state == state:
                break
        if not parent_read_succeeded:
            return self._uncertain(
                state,
                "parent metadata is not yet authoritatively observable before child completion",
                action_key=key,
                mutation_count=1,
            )
        if verified_state != state:
            return WorkflowResult(
                parent_identifier,
                "blocked",
                "block",
                "parent metadata changed before phase child completion",
                action_key=key,
                mutation_count=1,
            )
        try:
            self.executor.mark_child_done(
                parent_identifier,
                completion,
                metadata,
                action_key=key,
            )
        except Exception:
            # Reconcile because the effect may have committed before failing.
            pass
        done_state = self._reconcile_parent(
            parent_identifier,
            lambda current: (
                isinstance(current, WorkflowState)
                and current.parent_identifier == parent_identifier
                and current.metadata == metadata
                and dict(current.snapshot.candidate_shas) == candidate_shas
                and key in current.applied_action_keys
                and len(
                    tuple(
                        item
                        for item in current.children
                        if item.evidence_comment_uuid == completion.evidence_comment_uuid
                        and item.status == "done"
                    )
                )
                == 1
            ),
        )
        if done_state is None:
            return self._uncertain(
                state,
                "phase child completion is not yet authoritatively observable",
                action_key=key,
                mutation_count=1,
            )
        completed = tuple(
            item
            for item in done_state.children
            if item.evidence_comment_uuid == completion.evidence_comment_uuid
            and item.status == "done"
        )
        if len(completed) != 1:
            return self._block(done_state, "phase child done status was not authoritative")
        replacement = (
            completion.phase in {"implementation", "repair"}
            and completion.result == "pass"
            and previous_candidate_sha is not None
            and previous_candidate_sha != completion.candidate_sha
        )
        if replacement:
            previous_target = state.pull_requests.get(completion.repository_key)
            current_target = done_state.pull_requests.get(completion.repository_key)
            pull_request = done_state.snapshot.pull_requests.get(completion.repository_key)
            invalidation_failed = (
                dict(done_state.snapshot.candidate_shas) != candidate_shas
                or bool(done_state.snapshot.reviews)
                or bool(done_state.snapshot.qa)
                or bool(done_state.snapshot.integration_qa)
                or bool(done_state.snapshot.smoke_reads)
                or pull_request is None
                or pull_request.head_sha != completion.candidate_sha
                or (
                    completion.phase == "repair"
                    and (previous_target is None or current_target != previous_target)
                )
            )
            if invalidation_failed:
                blocked = self._block(
                    done_state,
                    "replacement SHA did not authoritatively invalidate every gate",
                )
                return replace(blocked, completed_child_status="done")
        unfinished_stage_siblings = tuple(
            item
            for item in done_state.children
            if item.identifier != completed[0].identifier
            and item.stage_ordinal == child.stage_ordinal
            and item.attempt == child.attempt
            and item.status not in _TERMINAL_CHILD_STATUSES
        )
        if unfinished_stage_siblings:
            return self._result(
                done_state,
                "wait",
                "current Stage still has non-terminal sibling work",
                completed_child_status="done",
                action_key=key,
                mutation_count=1,
            )
        resumed = self.resume_parent(parent_identifier)
        return replace(
            resumed,
            completed_child_status="done",
            mutation_count=resumed.mutation_count + 1,
        )

    def _preflight(
        self,
        state: WorkflowState,
        order: tuple[str, ...],
    ) -> str | None:
        if self.github is None:
            return "GitHub merge boundary is unavailable"
        if (
            set(state.pull_requests) != set(state.snapshot.affected_repositories)
            or not set(order) <= state.pull_requests.keys()
        ):
            return "merge plan lacks exact pull request targets"
        problems: list[str] = []
        for repository in order:
            target = state.pull_requests[repository]
            repository_spec = self.manifest.repositories[repository]
            try:
                pull_request = self.github.get_pull_request(repository_spec.github, target.number)
                if (
                    pull_request.repository != repository_spec.github
                    or pull_request.number != target.number
                    or pull_request.state != "open"
                    or pull_request.head_sha != state.snapshot.candidate_shas[repository]
                    or pull_request.base_ref != repository_spec.default_branch
                    or pull_request.mergeable is not True
                ):
                    problems.append(f"{repository} pull request changed before merge")
                checks = self.github.required_status_checks(
                    repository_spec.github,
                    repository_spec.default_branch,
                    state.snapshot.candidate_shas[repository],
                )
                if (
                    checks.repository != repository_spec.github
                    or checks.base_ref != repository_spec.default_branch
                    or checks.sha != state.snapshot.candidate_shas[repository]
                    or checks.passing is not True
                ):
                    problems.append(
                        f"{repository} required checks did not pass exact-SHA preflight"
                    )
            except (GitHubBoundaryError, RuntimeError, TypeError, ValueError):
                problems.append(f"{repository} merge preflight failed")
        return "; ".join(problems) or None

    @staticmethod
    def _merge_authority(state: WorkflowState) -> _MergeAuthority:
        assert state.metadata is not None
        return _MergeAuthority(
            parent_identifier=state.parent_identifier,
            affected_repositories=tuple(state.snapshot.affected_repositories),
            candidate_shas=MappingProxyType(dict(state.snapshot.candidate_shas)),
            child_evidence=MappingProxyType(dict(state.snapshot.children)),
            pull_request_evidence=MappingProxyType(dict(state.snapshot.pull_requests)),
            review_evidence=MappingProxyType(dict(state.snapshot.reviews)),
            qa_evidence=MappingProxyType(dict(state.snapshot.qa)),
            integration_evidence=MappingProxyType(dict(state.snapshot.integration_qa)),
            smoke_reads=tuple(state.snapshot.smoke_reads),
            satisfied_dependencies=frozenset(
                state.snapshot.satisfied_dependencies
            ),
            workflow_children=tuple(state.children),
            pull_request_targets=MappingProxyType(dict(state.pull_requests)),
            parent_status=state.parent_status,
            project_key=state.project_key,
            human_wait=state.human_wait,
            active_work=state.active_work,
            stalled=state.snapshot.stalled,
            stalled_repository=state.snapshot.stalled_repository,
            recovery_count=state.snapshot.recovery_count,
            workflow_version=state.metadata.workflow_version,
            metadata_version=state.metadata.metadata_version,
            instance_key=state.metadata.instance_key,
            repository_dag=MappingProxyType(dict(state.metadata.repository_dag)),
            contract_hashes=MappingProxyType(dict(state.metadata.contract_hashes)),
            attempt=state.metadata.repair_round,
        )

    def _merge_authority_matches(
        self,
        state: object,
        authority: _MergeAuthority,
        merged_shas: Mapping[str, str],
    ) -> bool:
        if (
            type(state) is not WorkflowState
            or state.parent_identifier != authority.parent_identifier
            or self._state_problem(state, authority.parent_identifier) is not None
            or set(state.snapshot.pull_requests)
            != set(authority.pull_request_evidence)
        ):
            return False
        observed = self._merge_authority(state)
        if replace(
            observed,
            pull_request_evidence=authority.pull_request_evidence,
        ) != authority:
            return False
        merged = dict(merged_shas)
        if set(merged) - set(authority.affected_repositories):
            return False
        for repository, initial in authority.pull_request_evidence.items():
            current = state.snapshot.pull_requests.get(repository)
            if current is None:
                return False
            if repository in merged:
                if (
                    current.head_sha != initial.head_sha
                    or current.mergeable != initial.mergeable
                    or current.checks_pass != initial.checks_pass
                    or current.state != "merged"
                    or current.merged_sha != merged[repository]
                ):
                    return False
            elif current != initial:
                return False
        return True

    def _stable_merge_authority_read(
        self,
        parent_identifier: str,
        authority: _MergeAuthority,
        merged_shas: Mapping[str, str],
        *,
        merge_plan: tuple[str, ...] | None = None,
    ) -> WorkflowState | None:
        observations: list[WorkflowState] = []
        for _ in range(2):
            try:
                current = self.snapshot_reader.read(parent_identifier)
            except Exception:
                return None
            if not self._merge_authority_matches(current, authority, merged_shas):
                return None
            if merge_plan is not None and (
                current.metadata is None
                or current.metadata.merge_state != "merging"
                or current.snapshot.merge_state != "merging"
                or tuple(current.metadata.merge_plan) != merge_plan
                or dict(current.snapshot.merged_shas) != dict(merged_shas)
                or type(current.metadata.last_action) is not str
                or not current.metadata.last_action.startswith("merge:")
                or current.metadata.last_action not in current.applied_action_keys
            ):
                return None
            observations.append(current)
        if observations[0] != observations[1]:
            return None
        return observations[1]

    def _read_merge_prefix(
        self,
        state: WorkflowState,
        order: tuple[str, ...],
    ) -> _MergePrefixObservation:
        if self.github is None:
            raise _MergePrefixObservationError(
                "authoritative merge-prefix read is unavailable"
            )
        if (
            set(state.pull_requests) != set(state.snapshot.affected_repositories)
            or not set(order) <= state.pull_requests.keys()
        ):
            raise _MergePrefixObservationError(
                "authoritative merge-prefix targets are incomplete"
            )

        merged_shas: dict[str, str] = {}
        remaining: list[str] = []
        saw_unmerged = False
        problems: list[str] = []
        for repository in order:
            target = state.pull_requests[repository]
            repository_spec = self.manifest.repositories[repository]
            try:
                pull_request = self.github.get_pull_request(
                    repository_spec.github,
                    target.number,
                )
            except Exception:
                problems.append(f"{repository} pull request could not be read")
                continue
            if not isinstance(pull_request, PullRequestInfo):
                problems.append(f"{repository} pull request read is malformed")
                continue
            if (
                pull_request.repository != repository_spec.github
                or pull_request.number != target.number
                or pull_request.head_sha
                != state.snapshot.candidate_shas.get(repository)
            ):
                problems.append(f"{repository} pull request identity changed")
                continue
            if pull_request.state == "merged":
                if (
                    pull_request.merged_at is None
                    or not _valid_sha(pull_request.merge_commit_sha)
                ):
                    problems.append(f"{repository} merged identity is malformed")
                    continue
                if saw_unmerged:
                    problems.append("remote merge prefix is non-contiguous")
                    continue
                assert pull_request.merge_commit_sha is not None
                merged_shas[repository] = pull_request.merge_commit_sha
            else:
                saw_unmerged = True
                remaining.append(repository)
        if problems:
            raise _MergePrefixObservationError("; ".join(problems))
        return _MergePrefixObservation(merged_shas, tuple(remaining))

    def _record_merge_transition(
        self,
        state: WorkflowState,
        *,
        merge_state: str,
        merged_shas: Mapping[str, str],
        merge_plan: tuple[str, ...],
        stage_kind: str,
    ) -> tuple[WorkflowState | None, WorkflowResult | None]:
        assert state.metadata is not None
        ordinal = state.metadata.stage_ordinal + 1
        key = self._action_key(state, stage_kind, ordinal)
        metadata = self._metadata(
            state,
            action_key=key,
            stage_ordinal=ordinal,
            merge_plan=merge_plan,
            merge_state=merge_state,
        )
        values = dict(merged_shas)
        attempted = key not in state.applied_action_keys
        if attempted:
            try:
                self.executor.record_merge_state(
                    state.parent_identifier,
                    merge_state,
                    values,
                    metadata,
                    action_key=key,
                )
            except Exception:
                # The boundary may have committed before its acknowledgement failed.
                pass
        observed = self._reconcile_parent(
            state.parent_identifier,
            lambda current: (
                isinstance(current, WorkflowState)
                and current.parent_identifier == state.parent_identifier
                and current.metadata is not None
                and current.metadata.stage_ordinal == ordinal
                and current.metadata.last_action == key
                and current.metadata.merge_plan == merge_plan
                and current.metadata.merge_state == merge_state
                and current.snapshot.merge_state == merge_state
                and dict(current.snapshot.merged_shas) == values
                and key in current.applied_action_keys
            ),
        )
        if observed is None:
            return None, self._uncertain(
                state,
                f"{stage_kind} write is not yet authoritatively observable",
                action_key=key,
                mutation_count=int(attempted),
            )
        return observed, None

    def _persist_merge_failure(
        self,
        state: WorkflowState,
        reason: str,
        *,
        merge_state: str,
        merged_shas: Mapping[str, str],
        merge_plan: tuple[str, ...],
    ) -> WorkflowResult:
        transitioned, uncertain = self._record_merge_transition(
            state,
            merge_state=merge_state,
            merged_shas=merged_shas,
            merge_plan=merge_plan,
            stage_kind=f"merge:{merge_state}",
        )
        if uncertain is not None:
            return uncertain
        assert transitioned is not None and transitioned.metadata is not None
        return self._persist_merge_block_status(
            transitioned,
            reason,
            merge_state=merge_state,
            merged_shas=merged_shas,
            prior_mutation_count=1,
        )

    def _persist_merge_block_status(
        self,
        transitioned: WorkflowState,
        reason: str,
        *,
        merge_state: str,
        merged_shas: Mapping[str, str],
        prior_mutation_count: int = 0,
    ) -> WorkflowResult:
        assert transitioned.metadata is not None
        ordinal = transitioned.metadata.stage_ordinal + 1
        key = self._action_key(transitioned, "merge:block-status", ordinal)
        metadata = self._metadata(
            transitioned,
            action_key=key,
            stage_ordinal=ordinal,
        )
        attempted = key not in transitioned.applied_action_keys
        if attempted:
            try:
                self.executor.set_parent_status(
                    transitioned.parent_identifier,
                    "blocked",
                    reason,
                    metadata,
                    action_key=key,
                )
            except Exception:
                pass
        observed = self._reconcile_parent(
            transitioned.parent_identifier,
            lambda current: (
                isinstance(current, WorkflowState)
                and current.parent_identifier == transitioned.parent_identifier
                and current.parent_status == "blocked"
                and current.metadata is not None
                and current.metadata.stage_ordinal == ordinal
                and current.metadata.last_action == key
                and current.snapshot.merge_state == merge_state
                and dict(current.snapshot.merged_shas) == dict(merged_shas)
                and key in current.applied_action_keys
            ),
        )
        if observed is None:
            return self._uncertain(
                transitioned,
                "merge failure status is not yet authoritatively observable",
                action_key=key,
                mutation_count=int(attempted),
            )
        return self._result(
            observed,
            "block",
            reason,
            action_key=key,
            mutation_count=prior_mutation_count + int(attempted),
            merge_state=merge_state,
        )

    def execute_merge_plan(self, parent_identifier: str) -> WorkflowResult:
        try:
            state = self.snapshot_reader.read(parent_identifier)
        except Exception:
            return WorkflowResult(parent_identifier, "blocked", "block", "parent could not be read")
        problem_result = self._state_problem_result(
            state,
            parent_identifier,
            stage_kind="merge",
        )
        if problem_result is not None:
            return problem_result
        if not self._candidate_heads_match(state):
            return self._zero_mutation_block(
                state,
                "out-of-band pull-request head change",
            )
        stage_wait = self._current_stage_wait(state)
        if stage_wait is not None:
            return stage_wait
        entry_decision = self._parent_decision(state)
        affected = frozenset(state.snapshot.affected_repositories)
        try:
            confirmed = tuple(repository for repository in self.manifest.merge_order if repository in affected)
            order = merge_order(self.manifest.repositories, affected, confirmed)
        except TopologyError:
            return self._block(state, "merge order is not dependency-safe", stage_kind="merge")

        merged = dict(state.snapshot.merged_shas)
        if state.snapshot.merge_state == "merged" and set(merged) == affected:
            return self.resume_parent(parent_identifier)

        merged_prs = {
            repository
            for repository, evidence in state.snapshot.pull_requests.items()
            if evidence.state == "merged"
        }

        if state.snapshot.merge_state == "partial":
            prefix = order[: len(merged)]
            assert state.metadata is not None
            replay_stage = (
                "merge:block-status"
                if state.parent_status == "blocked"
                else "merge:partial"
            )
            replay_key = self._action_key(
                state,
                replay_stage,
                state.metadata.stage_ordinal,
            )
            replayable_partial = (
                bool(merged)
                and tuple(state.metadata.merge_plan) == order
                and set(merged) == set(prefix)
                and merged_prs == set(prefix)
                and state.metadata.last_action == replay_key
                and replay_key in state.applied_action_keys
                and all(
                    _valid_sha(merged[repository])
                    and state.snapshot.pull_requests[repository].merged_sha
                    == merged[repository]
                    for repository in prefix
                )
            )
            if not replayable_partial:
                return self._uncertain(
                    state,
                    "partial merge state is not an authoritative replayable prefix",
                )
            reason = "cross-repository merge is partial; rollback and deployment are forbidden"
            if state.parent_status == "blocked":
                return self._result(
                    state,
                    "block",
                    reason,
                    merge_state="partial",
                )
            if state.parent_status not in _ACTIVE_PARENT_STATUSES:
                return self._uncertain(
                    state,
                    "partial merge has no authoritative blocked parent transition",
                )
            return self._persist_merge_block_status(
                state,
                reason,
                merge_state="partial",
                merged_shas=merged,
            )

        resuming = state.snapshot.merge_state == "merging"
        if resuming:
            try:
                observed_prefix = self._read_merge_prefix(state, order)
            except _MergePrefixObservationError as exc:
                return self._uncertain(state, str(exc))
            persisted_prefix = order[: len(merged)]
            observed_count = len(observed_prefix.merged_shas)
            persisted_matches_observation = (
                len(merged) <= observed_count
                and set(merged) == set(persisted_prefix)
                and all(
                    _valid_sha(merged[repository])
                    and observed_prefix.merged_shas.get(repository)
                    == merged[repository]
                    for repository in persisted_prefix
                )
            )
            if not persisted_matches_observation:
                return self._uncertain(
                    state,
                    "persisted merge prefix does not match authoritative remote truth",
                )
            if observed_count > len(merged):
                recovered_repository = order[observed_count - 1]
                recovered, uncertain = self._record_merge_transition(
                    state,
                    merge_state="merging",
                    merged_shas=observed_prefix.merged_shas,
                    merge_plan=order,
                    stage_kind=f"merge:recover:{recovered_repository}",
                )
                if uncertain is not None:
                    return uncertain
                assert recovered is not None
                state = recovered
                merged = dict(state.snapshot.merged_shas)
                merged_prs = {
                    repository
                    for repository, evidence in state.snapshot.pull_requests.items()
                    if evidence.state == "merged"
                }
            prefix = order[: len(merged)]
            assert state.metadata is not None
            replay_problem = (
                tuple(state.metadata.merge_plan) != order
                or set(merged) != set(prefix)
                or merged_prs != set(prefix)
                or state.metadata.last_action not in state.applied_action_keys
                or any(
                    not _valid_sha(merged[repository])
                    or state.snapshot.pull_requests[repository].merged_sha
                    != merged[repository]
                    for repository in prefix
                )
            )
            if replay_problem:
                return self._persist_merge_failure(
                    state,
                    "merge progress is not an authoritative replayable prefix; rollback is forbidden",
                    merge_state="partial",
                    merged_shas=merged,
                    merge_plan=order,
                )
            remaining = observed_prefix.remaining
        else:
            observed_merged = set(merged) | merged_prs
            if observed_merged:
                return self._persist_merge_failure(
                    state,
                    "cross-repository merge is already partial; rollback is forbidden",
                    merge_state="partial",
                    merged_shas=merged,
                    merge_plan=order,
                )
            decision = entry_decision
            if decision.kind is DecisionKind.BLOCK:
                return self._block(state, decision.reason, stage_kind="merge")
            if decision.kind is not DecisionKind.MERGE:
                if decision.kind in {DecisionKind.SMOKE, DecisionKind.COMPLETE}:
                    return self.resume_parent(parent_identifier)
                return self._result(state, "noop", "parent is not merge-authorized")
            remaining = order

        authority = self._merge_authority(state)
        preflight_problem = self._preflight(state, remaining)
        if preflight_problem is not None:
            return self._persist_merge_failure(
                state,
                preflight_problem,
                merge_state="partial" if merged else "blocked",
                merged_shas=merged,
                merge_plan=order,
            )
        fresh = self._stable_merge_authority_read(
            parent_identifier,
            authority,
            merged,
        )
        if fresh is None:
            return self._uncertain(state, "parent changed or became unreadable during all-PR preflight")

        if not resuming:
            reserved, uncertain = self._record_merge_transition(
                fresh,
                merge_state="merging",
                merged_shas={},
                merge_plan=order,
                stage_kind="merge:reserve",
            )
            if uncertain is not None:
                return uncertain
            assert reserved is not None
            fresh = self._stable_merge_authority_read(
                parent_identifier,
                authority,
                merged,
                merge_plan=order,
            )
            if fresh is None:
                return self._uncertain(
                    reserved,
                    "merge authority changed across reservation",
                )

        current = fresh
        for repository in remaining:
            verified = self._stable_merge_authority_read(
                parent_identifier,
                authority,
                merged,
                merge_plan=order,
            )
            if verified is None:
                return self._uncertain(
                    current,
                    "merge authority changed before a GitHub mutation",
                )
            stage_wait = self._current_stage_wait(verified)
            if stage_wait is not None:
                return stage_wait
            preflight_problem = self._preflight(verified, (repository,))
            if preflight_problem is not None:
                return self._persist_merge_failure(
                    verified,
                    preflight_problem,
                    merge_state="partial" if merged else "blocked",
                    merged_shas=merged,
                    merge_plan=order,
                )
            current = self._stable_merge_authority_read(
                parent_identifier,
                authority,
                merged,
                merge_plan=order,
            )
            if current is None:
                return self._uncertain(
                    verified,
                    "merge authority changed during per-PR preflight",
                )
            stage_wait = self._current_stage_wait(current)
            if stage_wait is not None:
                return stage_wait
            target = current.pull_requests[repository]
            expected_sha = current.snapshot.candidate_shas[repository]
            result: MergeResult | None = None
            try:
                assert self.github is not None
                result = self.github.merge_pull_request(
                    self.manifest.repositories[repository].github,
                    target.number,
                    expected_sha=expected_sha,
                )
            except Exception:
                # A write may have committed before its acknowledgement failed.
                result = None
            try:
                assert self.github is not None
                authoritative = self.github.get_pull_request(
                    self.manifest.repositories[repository].github,
                    target.number,
                )
            except Exception:
                return self._uncertain(
                    current,
                    "merge mutation outcome is not yet authoritatively observable",
                )
            authoritative_merged = (
                isinstance(authoritative, PullRequestInfo)
                and authoritative.repository
                == self.manifest.repositories[repository].github
                and authoritative.number == target.number
                and authoritative.state == "merged"
                and authoritative.head_sha == expected_sha
                and authoritative.merged_at is not None
                and _valid_sha(authoritative.merge_commit_sha)
            )
            if authoritative_merged:
                result = MergeResult(
                    authoritative.repository,
                    authoritative.number,
                    True,
                    expected_sha,
                    authoritative.merge_commit_sha,
                )
            else:
                result = None
            if result is None:
                failed_state = "partial" if merged else "blocked"
                return self._persist_merge_failure(
                    current,
                    "merge sequence failed; rollback and deployment are forbidden",
                    merge_state=failed_state,
                    merged_shas=merged,
                    merge_plan=order,
                )
            assert isinstance(result, MergeResult)
            merged[repository] = result.merged_sha
            progressed, uncertain = self._record_merge_transition(
                current,
                merge_state="merging",
                merged_shas=merged,
                merge_plan=order,
                stage_kind=f"merge:progress:{repository}",
            )
            if uncertain is not None:
                return uncertain
            assert progressed is not None
            current = progressed

        completed, uncertain = self._record_merge_transition(
            current,
            merge_state="merged",
            merged_shas=merged,
            merge_plan=order,
            stage_kind="merge:final",
        )
        if uncertain is not None:
            return uncertain
        assert completed is not None
        resumed = (
            self.execute_smoke(parent_identifier)
            if self.smoke_executor is not None
            else self.resume_parent(parent_identifier)
        )
        return replace(resumed, merge_state="merged")

    def _smoke_problem(self, state: WorkflowState, smoke_read: SmokeRead) -> str | None:
        schema_problem = _smoke_read_schema_problem(smoke_read)
        if schema_problem is not None:
            return schema_problem
        affected = set(state.snapshot.affected_repositories)
        expected_suites = {
            suite.key
            for suite in self.manifest.integration_suites
            if set(suite.repositories) <= affected
        }
        checkout_keys = set(smoke_read.checkout_shas)
        checkout_scope_valid = (
            checkout_keys == affected
            and dict(smoke_read.checkout_shas) == dict(state.snapshot.merged_shas)
            if smoke_read.authoritative
            else checkout_keys <= affected
            and all(
                smoke_read.checkout_shas[repository]
                == state.snapshot.merged_shas.get(repository)
                for repository in checkout_keys
            )
        )
        if (
            set(smoke_read.merged_shas) != affected
            or dict(smoke_read.merged_shas) != dict(state.snapshot.merged_shas)
            or any(not _valid_sha(sha) for sha in smoke_read.merged_shas.values())
            or any(not _valid_sha(sha) for sha in smoke_read.checkout_shas.values())
            or not checkout_scope_valid
            or set(smoke_read.repository_results) != affected
            or set(smoke_read.integration_results) != expected_suites
            or any(value not in {"pending", "pass", "fail", "blocked"} for value in smoke_read.repository_results.values())
            or any(value not in {"pending", "pass", "fail", "blocked"} for value in smoke_read.integration_results.values())
            or not isinstance(smoke_read.authoritative, bool)
        ):
            return "smoke read does not cover the exact merged scope"
        return None

    def record_smoke_read(self, parent_identifier: str, smoke_read: SmokeRead) -> WorkflowResult:
        """Replay persisted smoke evidence; callers cannot mint new observations."""

        identifier_valid = (
            type(parent_identifier) is str
            and _ISSUE_IDENTIFIER.fullmatch(parent_identifier) is not None
        )
        safe_identifier = parent_identifier if identifier_valid else ""
        schema_problem = _smoke_read_schema_problem(smoke_read)
        if not identifier_valid or schema_problem is not None:
            return WorkflowResult(
                safe_identifier,
                "blocked",
                "block",
                schema_problem or "parent identifier is malformed",
                mutation_count=0,
            )
        return self._record_smoke_read(
            parent_identifier,
            smoke_read,
            persistence_authority=None,
        )

    def _record_smoke_read(
        self,
        parent_identifier: str,
        smoke_read: SmokeRead,
        *,
        persistence_authority: object,
    ) -> WorkflowResult:
        identifier_valid = (
            type(parent_identifier) is str
            and _ISSUE_IDENTIFIER.fullmatch(parent_identifier) is not None
        )
        safe_identifier = parent_identifier if identifier_valid else ""
        schema_problem = _smoke_read_schema_problem(smoke_read)
        if not identifier_valid or schema_problem is not None:
            return WorkflowResult(
                safe_identifier,
                "blocked",
                "block",
                schema_problem or "parent identifier is malformed",
                mutation_count=0,
            )
        try:
            state = self.snapshot_reader.read(parent_identifier)
        except Exception:
            return WorkflowResult(parent_identifier, "blocked", "block", "parent could not be read")
        problem_result = self._state_problem_result(
            state,
            parent_identifier,
            stage_kind="smoke",
        )
        if problem_result is not None:
            return problem_result
        smoke_problem = self._smoke_problem(state, smoke_read)
        if smoke_problem is not None:
            if persistence_authority is not _SMOKE_PERSISTENCE_AUTHORITY:
                return self._zero_mutation_block(state, smoke_problem)
            return self._block(state, smoke_problem, stage_kind="smoke")
        assert state.metadata is not None
        before = state.snapshot.smoke_reads
        smoke_action_keys = frozenset(
            action_key
            for action_key in state.applied_action_keys
            if action_key.startswith("smoke:")
        )
        first_smoke_ordinal = state.metadata.stage_ordinal - len(before) + 1
        historical_observations = tuple(
            zip(
                range(first_smoke_ordinal, state.metadata.stage_ordinal + 1),
                before,
            )
        )
        expected_smoke_action_keys = (
            frozenset(
                self._action_key(
                    state,
                    f"smoke:{historical_read.observation_id}",
                    historical_ordinal,
                )
                for historical_ordinal, historical_read in historical_observations
            )
            if before and first_smoke_ordinal >= 0
            else frozenset()
        )
        observation_ids = tuple(read.observation_id for read in before)
        if (
            (before and first_smoke_ordinal < 0)
            or len(set(observation_ids)) != len(observation_ids)
            or len(historical_observations) != len(before)
            or smoke_action_keys != expected_smoke_action_keys
        ):
            return self._uncertain(
                state,
                "existing smoke evidence is not bound to an authoritative parent transition",
            )
        for historical_ordinal, historical_read in historical_observations:
            if historical_read.observation_id != smoke_read.observation_id:
                continue
            key = self._action_key(
                state,
                f"smoke:{historical_read.observation_id}",
                historical_ordinal,
            )
            if historical_read != smoke_read:
                return self._zero_mutation_block(
                    state,
                    "smoke observation identity conflicts with persisted evidence",
                )
            return self._result(
                state,
                "noop",
                "smoke observation is already authoritative",
                action_key=key,
            )
        if state.parent_status not in _ACTIVE_PARENT_STATUSES:
            return self._result(state, "noop", "parent is not active")
        if state.human_wait:
            return self._result(state, "noop", "parent is waiting for a human")
        if persistence_authority is not _SMOKE_PERSISTENCE_AUTHORITY:
            return self._zero_mutation_block(
                state,
                "new smoke evidence requires the owned smoke execution authority",
            )
        decision = self._parent_decision(state)
        if decision.kind is not DecisionKind.SMOKE:
            return self._result(
                state,
                "block" if decision.kind is DecisionKind.BLOCK else "noop",
                decision.reason,
            )
        stage_wait = self._current_stage_wait(state)
        if stage_wait is not None:
            return stage_wait
        ordinal = state.metadata.stage_ordinal + 1
        key = self._action_key(
            state,
            f"smoke:{smoke_read.observation_id}",
            ordinal,
        )
        metadata = self._metadata(
            state,
            action_key=key,
            stage_ordinal=ordinal,
        )
        try:
            self.executor.write_smoke_read(
                parent_identifier,
                smoke_read,
                metadata,
                action_key=key,
            )
        except Exception:
            # Reconcile because the effect may have committed before failing.
            pass
        expected_smoke_reads = before + (smoke_read,)
        expected_action_keys = state.applied_action_keys | {key}
        expected_state = replace(
            state,
            metadata=metadata,
            snapshot=replace(state.snapshot, smoke_reads=expected_smoke_reads),
            applied_action_keys=expected_action_keys,
        )
        observed = self._reconcile_parent(
            parent_identifier,
            lambda current: (
                current == expected_state
                and current.parent_identifier == parent_identifier
                and current.snapshot.smoke_reads == expected_smoke_reads
                and current.metadata == metadata
                and current.metadata.stage_ordinal == ordinal
                and current.metadata.last_action == key
                and current.applied_action_keys == expected_action_keys
                and key in current.applied_action_keys
            ),
        )
        if observed is None:
            return self._uncertain(
                state,
                "smoke evidence and its parent transition are not yet authoritatively observable",
                action_key=key,
                mutation_count=1,
            )
        return self.resume_parent(parent_identifier)

    def execute_smoke(self, parent_identifier: str) -> WorkflowResult:
        try:
            state = self.snapshot_reader.read(parent_identifier)
        except Exception:
            return WorkflowResult(parent_identifier, "blocked", "block", "parent could not be read")
        problem_result = self._state_problem_result(
            state,
            parent_identifier,
            stage_kind="smoke",
        )
        if problem_result is not None:
            return problem_result
        decision = self._parent_decision(state)
        if decision.kind is DecisionKind.BLOCK:
            return self._result(state, "block", decision.reason)
        if decision.kind is not DecisionKind.SMOKE:
            return self._result(state, "noop", "parent is not smoke-authorized")
        stage_wait = self._current_stage_wait(state)
        if stage_wait is not None:
            return stage_wait
        if self.smoke_executor is None:
            return self._block(state, "owned smoke executor is unavailable", stage_kind="smoke")
        assert state.metadata is not None
        ordinal = state.metadata.stage_ordinal + 1
        key = self._action_key(state, "smoke", ordinal)
        try:
            smoke_read = self.smoke_executor.execute(
                parent_identifier,
                decision.repositories,
                state.snapshot.merged_shas,
                action_key=key,
            )
        except Exception:
            return self._block(state, "owned exact-SHA smoke execution failed", stage_kind="smoke")
        if (
            not isinstance(smoke_read, SmokeRead)
            or smoke_read.observation_id != key
        ):
            return self._block(
                state,
                "owned smoke observation identity does not match its authoritative token",
                stage_kind="smoke",
            )
        return self._record_smoke_read(
            parent_identifier,
            smoke_read,
            persistence_authority=_SMOKE_PERSISTENCE_AUTHORITY,
        )

    def _watch_scope_problem(
        self,
        state: WorkflowState,
        parent_identifier: str,
    ) -> str | None:
        state_problem = self._state_problem(state, parent_identifier)
        if state_problem is not None:
            return state_problem
        if state.parent_status not in _ACTIVE_PARENT_STATUSES:
            return "parent is not active"
        if state.project_key not in self.project_keys:
            return "parent is outside configured projects"
        if (
            state.metadata is None
            or state.metadata.instance_key != self.manifest.instance.key
            or state.metadata.workflow_version not in self.supported_workflow_versions
        ):
            return "parent workflow is unsupported"
        return None

    @staticmethod
    def _recovery_noop(
        parent_identifier: str,
        reason: str,
        *,
        trusted_state: WorkflowState | None = None,
    ) -> WorkflowResult:
        return WorkflowResult(
            parent_identifier,
            "in_progress" if trusted_state is None else trusted_state.parent_status,
            "noop",
            reason,
            merge_state=(
                "pending" if trusted_state is None else trusted_state.snapshot.merge_state
            ),
        )

    @staticmethod
    def _is_all_merged(snapshot: ParentSnapshot) -> bool:
        affected = frozenset(snapshot.affected_repositories)
        return (
            bool(affected)
            and snapshot.merge_state == "merged"
            and set(snapshot.merged_shas) == affected
            and {
                repository
                for repository, evidence in snapshot.pull_requests.items()
                if evidence.state == "merged"
            }
            == affected
        )

    def _expected_recovery_intents(
        self,
        state: WorkflowState,
        repository: str,
    ) -> frozenset[tuple[str, str, str, str]]:
        """Derive child identity only from the stalled parent evidence."""

        snapshot = state.snapshot
        candidate = snapshot.candidate_shas.get(repository)
        implementation = snapshot.children.get(repository)
        intents: set[tuple[str, str, str, str]] = set()
        if (
            implementation is not None
            and implementation.result == "pending"
        ):
            intents.add(
                (
                    "repair" if snapshot.attempt else "implementation",
                    repository,
                    repository,
                    "",
                )
            )
            return frozenset(intents)
        if (
            implementation is None
            or implementation.result != "pass"
            or implementation.candidate_sha != candidate
            or candidate is None
        ):
            return frozenset()

        for phase, evidence_by_repository in (
            ("review", snapshot.reviews),
            ("qa", snapshot.qa),
        ):
            evidence = evidence_by_repository.get(repository)
            if (
                evidence is not None
                and evidence.result == "pending"
                and evidence.candidate_sha == candidate
            ):
                intents.add((phase, repository, repository, ""))
        for suite in self.manifest.integration_suites:
            evidence = snapshot.integration_qa.get(suite.key)
            if (
                suite.command_repository == repository
                and set(suite.repositories)
                <= set(snapshot.affected_repositories)
                and evidence is not None
                and evidence.result == "pending"
                and dict(evidence.candidate_shas)
                == dict(snapshot.candidate_shas)
            ):
                intents.add(
                    (
                        "integration_qa",
                        suite.key,
                        suite.command_repository,
                        suite.key,
                    )
                )
        return frozenset(intents)

    def recover_stalled_parent(
        self,
        parent_identifier: str,
        *,
        now: datetime | None = None,
    ) -> WorkflowResult:
        if now is not None and (not isinstance(now, datetime) or now.tzinfo is None):
            return WorkflowResult(parent_identifier, "blocked", "block", "recovery time must be timezone-aware")
        try:
            initial = self.snapshot_reader.read(parent_identifier)
        except Exception:
            return WorkflowResult(parent_identifier, "blocked", "block", "parent could not be read")
        problem_result = self._state_problem_result(
            initial,
            parent_identifier,
            stage_kind="recovery",
            future_child_only=True,
        )
        if problem_result is not None:
            return problem_result
        scope_problem = self._watch_scope_problem(initial, parent_identifier)
        if scope_problem is not None:
            return self._recovery_noop(parent_identifier, scope_problem)
        if not self._candidate_heads_match(initial):
            return self._zero_mutation_block(
                initial,
                "out-of-band pull-request head change",
            )
        decision = self._parent_decision(initial)
        if (
            decision.kind is DecisionKind.BLOCK
            and self._is_all_merged(initial.snapshot)
        ):
            return self._zero_mutation_block(initial, decision.reason)
        if initial.human_wait or initial.active_work or not initial.snapshot.stalled:
            return self._result(initial, "noop", "work is healthy or waiting for a human")
        if decision.kind is DecisionKind.BLOCK and initial.snapshot.recovery_count >= 1:
            return self._block(initial, decision.reason, stage_kind="recovery-block")
        if (
            decision.kind is not DecisionKind.DISPATCH
            or decision.dispatch_kind is not DispatchKind.RECOVERY
            or len(decision.repositories) != 1
        ):
            return self._result(initial, "noop", "watcher has no bounded rerun action")
        repository = decision.repositories[0]
        assert initial.metadata is not None
        expected_intents = self._expected_recovery_intents(initial, repository)
        if len(expected_intents) != 1:
            return self._result(
                initial,
                "noop",
                "watcher cannot derive one stalled phase from parent evidence",
            )
        expected_intent = next(iter(expected_intents))

        def intended_current_child(child: WorkflowChild) -> bool:
            expected_prefix = {
                "implementation": "dispatch:",
                "repair": "repair:",
                "review": "stage:",
                "qa": "stage:",
                "integration_qa": "stage:",
            }.get(child.phase)
            if expected_prefix is None or not child.action_key.startswith(expected_prefix):
                return False
            if (
                child.stage_ordinal != initial.metadata.stage_ordinal
                or child.attempt != initial.metadata.repair_round
                or child.action_key not in initial.applied_action_keys
                or child.repository_key != repository
            ):
                return False
            return (
                child.phase,
                child.target_key,
                child.repository_key,
                child.suite_key,
            ) == expected_intent

        candidates = tuple(
            child
            for child in initial.children
            if intended_current_child(child)
            and child.status in _ACTIVE_CHILD_STATUSES
            and not child.active
        )
        if len(candidates) != 1:
            return self._result(initial, "noop", "watcher cannot identify one existing stalled child")

        try:
            fresh = self.snapshot_reader.read(parent_identifier)
        except Exception:
            return self._result(initial, "noop", "parent became unreadable before recovery")
        problem_result = self._state_problem_result(
            fresh,
            parent_identifier,
            stage_kind="recovery",
            future_child_only=True,
        )
        if problem_result is not None:
            return problem_result
        fresh_scope_problem = self._watch_scope_problem(fresh, parent_identifier)
        if fresh_scope_problem is not None:
            return self._recovery_noop(
                parent_identifier,
                "workflow changed before recovery",
                trusted_state=initial,
            )
        if not self._candidate_heads_match(fresh):
            return self._zero_mutation_block(
                fresh,
                "out-of-band pull-request head change",
            )
        if (
            fresh != initial
            or fresh.human_wait
            or fresh.active_work
            or self._parent_decision(fresh) != decision
        ):
            return self._result(fresh, "noop", "workflow changed before recovery")
        assert fresh.metadata is not None
        ordinal = fresh.metadata.stage_ordinal + 1
        key = self._action_key(
            fresh,
            "recovery",
            ordinal,
        )
        if key in fresh.applied_action_keys:
            return self._result(fresh, "noop", "recovery action already exists", action_key=key)
        metadata = self._metadata(
            fresh,
            action_key=key,
            stage_ordinal=ordinal,
        )
        before_children = fresh.children
        target_identifier = candidates[0].identifier

        def child_identity(child: WorkflowChild) -> tuple[object, ...]:
            return (
                child.identifier,
                child.target_key,
                child.repository_key,
                child.suite_key,
                child.phase,
                child.stage_ordinal,
                child.attempt,
                child.action_key,
                child.evidence_comment_uuid,
                tuple(child.creation_candidate_shas.items()),
            )

        try:
            self.executor.rerun_child(
                parent_identifier,
                target_identifier,
                metadata,
                action_key=key,
            )
        except Exception:
            # Reconcile because the effect may have committed before failing.
            pass

        observed = self._reconcile_parent(
            parent_identifier,
            lambda current: (
                isinstance(current, WorkflowState)
                and current.parent_identifier == parent_identifier
                and current.metadata == metadata
                and current.snapshot.recovery_count
                == fresh.snapshot.recovery_count + 1
                and key in current.applied_action_keys
            ),
        )
        if observed is None:
            return self._uncertain(
                fresh,
                "bounded recovery is not yet authoritatively observable",
                action_key=key,
                mutation_count=1,
            )

        normalized_observed_snapshot = replace(
            observed.snapshot,
            recovery_count=fresh.snapshot.recovery_count,
            stalled=fresh.snapshot.stalled,
            stalled_repository=fresh.snapshot.stalled_repository,
        )
        before_by_id = {child.identifier: child for child in before_children}
        after_by_id = {child.identifier: child for child in observed.children}
        before_target = before_by_id[target_identifier]
        same_target = after_by_id.get(target_identifier)
        target_reactivated = (
            same_target is not None
            and not before_target.active
            and same_target.active
        )
        replacement_targets = tuple(
            child
            for child in observed.children
            if child.identifier != target_identifier
            and child.repository_key == before_target.repository_key
            and child.target_key == before_target.target_key
            and child.suite_key == before_target.suite_key
            and child.phase == before_target.phase
            and child.stage_ordinal == before_target.stage_ordinal
            and child.attempt == before_target.attempt
            and child.action_key == before_target.action_key
            and child.creation_candidate_shas
            == before_target.creation_candidate_shas
            and child.active
            and child_identity(child) != child_identity(before_target)
        )
        target_has_new_run = target_reactivated or len(replacement_targets) == 1
        if target_reactivated:
            allowed_after_identifiers = {frozenset(before_by_id)}
        elif len(replacement_targets) == 1:
            replacement_identifier = replacement_targets[0].identifier
            allowed_after_identifiers = {
                frozenset(before_by_id) | {replacement_identifier},
                (frozenset(before_by_id) - {target_identifier})
                | {replacement_identifier},
            }
        else:
            allowed_after_identifiers = set()
        unchanged_other_children = all(
            after_by_id.get(identifier) == before
            for identifier, before in before_by_id.items()
            if identifier != target_identifier
        )
        if (
            self._watch_scope_problem(observed, parent_identifier) is not None
            or observed.parent_identifier != fresh.parent_identifier
            or observed.parent_status != fresh.parent_status
            or observed.project_key != fresh.project_key
            or observed.snapshot.recovery_count != fresh.snapshot.recovery_count + 1
            or normalized_observed_snapshot != fresh.snapshot
            or not target_has_new_run
            or not observed.active_work
            or frozenset(after_by_id) not in allowed_after_identifiers
            or not unchanged_other_children
            or observed.pull_requests != fresh.pull_requests
            or observed.metadata != metadata
            or observed.human_wait != fresh.human_wait
            or observed.applied_action_keys != fresh.applied_action_keys | {key}
        ):
            return WorkflowResult(
                parent_identifier,
                "blocked",
                "block",
                "bounded recovery verification failed without a follow-up mutation",
                action_key=key,
                mutation_count=1,
            )
        return self._result(
            observed,
            "resume",
            decision.reason,
            action_key=key,
            mutation_count=1,
        )

    def watch_active_parents(self) -> WorkflowResult:
        try:
            parents = self.snapshot_reader.list_active_parents(
                instance_key=self.manifest.instance.key,
                project_keys=self.project_keys,
                workflow_versions=self.supported_workflow_versions,
            )
        except Exception:
            return WorkflowResult("", "blocked", "block", "watcher parent scan failed")
        if not isinstance(parents, tuple) or any(not isinstance(parent, str) for parent in parents):
            return WorkflowResult("", "blocked", "block", "watcher parent scan is malformed")
        candidates = 0
        for parent_identifier in parents:
            result = self.recover_stalled_parent(parent_identifier)
            if result.next_action == "noop":
                continue
            candidates += 1
            return replace(
                result,
                scanned_parents=len(parents),
                recovery_candidates=candidates,
            )
        return WorkflowResult(
            "",
            "in_progress",
            "noop",
            "watcher found no recoverable parent",
            scanned_parents=len(parents),
            recovery_candidates=0,
        )

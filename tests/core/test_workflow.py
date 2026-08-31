import atexit
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from tempfile import TemporaryDirectory
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import urlsplit

from multica_delivery.core.decisions import (
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
from multica_delivery.adapters.github_client import (
    GitHubBoundaryError,
    MergeResult,
    PullRequestInfo,
    RequiredStatusChecks,
)
from multica_delivery.adapters.exact_sha import (
    ClosedCommandResult,
    ExactShaCommandResult,
    ExactShaVerification,
    LocalExactShaCommandRunner,
)
from multica_delivery.core.manifest import load_manifest
from multica_delivery.core.metadata import (
    LegacyParentMetadataV1,
    ParentMetadata,
    RepairAuthorization,
)
from multica_delivery.core.model import PolicySpec, ServiceSpec
from multica_delivery.adapters.processes import (
    OwnedProcess,
    ProcessManager,
    ProcessOwnershipError,
    ProcessRegistry,
    ProcessRun,
)
from multica_delivery.core.workflow import (
    AuthorizingComment,
    ChildRequest,
    FailureBundle,
    FailureEvidenceRef,
    GenericWorkflow,
    OwnedSmokeExecutor,
    PhaseCompletion,
    PullRequestTarget,
    ScopeResolution,
    StatusTransition,
    WorkflowChild,
    WorkflowError,
    WorkflowState,
    _phase_completion_schema_problem,
    coordinator_action_key,
)


FIXTURE = Path(__file__).parent / "fixtures" / "three-repository-delivery.yaml"
SHA = {
    "api": "a" * 40,
    "notifications": "b" * 40,
    "web": "c" * 40,
}
REPLACEMENT_SHA = "d" * 40
OTHER_SHA = "e" * 40
SMOKE_OBSERVATION = {
    "first": "smoke:" + "1" * 64,
    "second": "smoke:" + "2" * 64,
    "third": "smoke:" + "3" * 64,
}
_PROCESS_TEMPORARIES: list[TemporaryDirectory[str]] = []


@atexit.register
def _cleanup_process_temporaries() -> None:
    for temporary in _PROCESS_TEMPORARIES:
        temporary.cleanup()


def evidence_uuid(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://example.test/{label}"))


def completion_for(
    repository: str,
    *,
    phase: str = "implementation",
    result: str = "pass",
    attempt: int = 0,
    sha: str | None = None,
    parent: str = "PRO-101",
    suite_key: str = "",
    candidate_shas: dict[str, str] | None = None,
    responsible_repositories: tuple[str, ...] | None = None,
    failure_bundle_digest: str = "",
    comment_digit: str | None = None,
) -> PhaseCompletion:
    comment_uuid = (
        str(uuid.UUID(comment_digit * 32))
        if comment_digit is not None
        else evidence_uuid(
            f"{parent}-{repository}-{phase}-{result}-{attempt}-{sha or SHA.get(repository, 'suite')}"
        )
    )
    pull_request_url = ""
    if repository in SHA:
        number = {"api": 12, "notifications": 13, "web": 14}[repository]
        slug = {
            "api": "sample-commerce-api",
            "notifications": "sample-commerce-notifications",
            "web": "sample-commerce-web",
        }[repository]
        pull_request_url = f"https://github.com/codeExploreHub/{slug}/pull/{number}"
    if responsible_repositories is None:
        responsible_repositories = (
            (repository,)
            if phase in {"review", "qa"} and result != "pass"
            else (("api", "web") if phase == "integration_qa" and result != "pass" else ())
        )
    return PhaseCompletion(
        parent_identifier=parent,
        repository_key=repository,
        phase=phase,
        result=result,
        attempt=attempt,
        candidate_sha=sha or SHA.get(repository, "e" * 40),
        pull_request_url=pull_request_url,
        evidence_comment_uuid=comment_uuid,
        evidence_comment_url=f"https://example.test/evidence/{comment_uuid}",
        suite_key=suite_key,
        candidate_shas=candidate_shas or {},
        responsible_repositories=responsible_repositories,
        failure_bundle_digest=failure_bundle_digest,
    )


def repair_completion(candidate_sha: str, bundle_digest: str) -> PhaseCompletion:
    return completion_for(
        "api",
        parent="PRO-200",
        phase="repair",
        result="pass",
        attempt=3,
        sha=candidate_sha,
        failure_bundle_digest=bundle_digest,
    )


def passing_smoke(
    *,
    observation_id: str,
    shas: dict[str, str] | None = None,
    authoritative: bool = True,
) -> SmokeRead:
    exact = dict(shas or {"api": SHA["api"], "web": SHA["web"]})
    return SmokeRead(
        observation_id=observation_id,
        merged_shas=exact,
        checkout_shas=exact,
        repository_results={repository: "pass" for repository in exact},
        integration_results={"web-api": "pass"} if set(exact) == {"api", "web"} else {},
        authoritative=authoritative,
    )


def passing_snapshot() -> ParentSnapshot:
    candidates = {"api": SHA["api"], "web": SHA["web"]}
    return ParentSnapshot(
        affected_repositories=("api", "web"),
        candidate_shas=candidates,
        children={
            repository: RepositoryEvidence(candidate_sha=sha, result="pass")
            for repository, sha in candidates.items()
        },
        pull_requests={
            repository: PullRequestEvidence(sha, "open", True, True)
            for repository, sha in candidates.items()
        },
        reviews={
            repository: RepositoryEvidence(candidate_sha=sha, result="pass")
            for repository, sha in candidates.items()
        },
        qa={
            repository: RepositoryEvidence(candidate_sha=sha, result="pass")
            for repository, sha in candidates.items()
        },
        integration_qa={
            "web-api": GateEvidence(candidate_shas=candidates, result="pass")
        },
    )


def merging_snapshot(
    merged_shas: dict[str, str] | None = None,
) -> ParentSnapshot:
    snapshot = passing_snapshot()
    merged = dict(merged_shas or {})
    return replace(
        snapshot,
        merge_state="merging",
        merged_shas=merged,
        pull_requests={
            repository: PullRequestEvidence(
                sha,
                "merged" if repository in merged else "open",
                True,
                True,
                merged.get(repository),
            )
            for repository, sha in snapshot.candidate_shas.items()
        },
    )


def metadata_for(snapshot: ParentSnapshot, *, stage_ordinal: int = 5) -> ParentMetadata:
    affected = snapshot.affected_repositories
    merge_plan = (
        tuple(repository for repository in ("api", "web", "notifications") if repository in affected)
        if snapshot.merge_state in {"ready", "merging", "merged"}
        else ()
    )
    return ParentMetadata(
        workflow_version=2,
        metadata_version=2,
        instance_key="sample-commerce",
        affected_repositories=affected,
        repository_dag={
            repository: tuple(
                dependency
                for dependency in ({"web": ("api",)}.get(repository, ()))
                if dependency in affected
            )
            for repository in affected
        },
        candidate_shas=snapshot.candidate_shas,
        contract_hashes={},
        stage_ordinal=stage_ordinal,
        merge_plan=merge_plan,
        merge_state=snapshot.merge_state,
        repair_round=snapshot.attempt,
        automatic_repairs_used=min(snapshot.attempt, 2),
        last_action="resume",
    )


def pull_request_targets() -> dict[str, PullRequestTarget]:
    return {
        "api": PullRequestTarget(
            "api",
            12,
            "https://github.com/codeExploreHub/sample-commerce-api/pull/12",
        ),
        "web": PullRequestTarget(
            "web",
            14,
            "https://github.com/codeExploreHub/sample-commerce-web/pull/14",
        ),
    }


class FakeWorkflowStore:
    def __init__(self, manifest) -> None:
        self.manifest = manifest
        self.states: dict[str, WorkflowState] = {}
        self.completions: dict[tuple[str, str], PhaseCompletion] = {}
        self.events: list[tuple[object, ...]] = []
        self.rollback_calls: list[object] = []
        self.corrupt_completion_read = False
        self.change_completion_after_first_read = False
        self.change_after_completion_read = False
        self.parent_drift_after_completion_read: str | None = None
        self.parent_drift_after_authorizing_comment = False
        self.completion_read_failures_remaining = 0
        self.retain_gates_on_replacement = False
        self.change_on_recovery_reread = False
        self.activate_on_rerun = False
        self.mutate_gate_on_rerun = False
        self.fail_reads_after_first_merge_progress = 0
        self.failed_merge_progress_read = False
        self.inject_merge_progress_read_failure = False
        self.read_parent_identifier_override: str | None = None
        self.read_state_override_by_count: dict[int, WorkflowState] = {}
        self.override_parent_after_rerun = False
        self.parent_read_failures_remaining = 0
        self.fail_reads_after_create = False
        self.fail_reads_after_done = False
        self.fail_reads_after_rerun = False
        self.write_smoke_evidence_without_transition = False
        self.fail_parent_reads_after_smoke_write = False
        self.fail_parent_reads_after_parent_done = False
        self.write_incomplete_parent_done = False
        self.read_counts: dict[str, int] = {}
        self.list_arguments: tuple[object, ...] | None = None
        self.intake_transitions: dict[str, StatusTransition] = {}
        self.authorizing_comments: dict[tuple[str, str], AuthorizingComment] = {}
        self.change_target_after_reservation = False
        self.change_candidate_after_first_progress = False
        self.add_pr_evidence_after_reservation = False
        self.duplicate_repair_child_on_create = False
        self.replace_repair_digest_after_create = False
        self.repair_failure_uuid_corruption: str | None = None

    def add_blank(
        self,
        identifier: str,
        *,
        status: str = "todo",
        authorized: bool = True,
    ) -> None:
        self.states[identifier] = WorkflowState(
            parent_identifier=identifier,
            parent_status=status,
            project_key=self.manifest.instance.control_project,
            metadata=None,
            snapshot=ParentSnapshot(affected_repositories=()),
        )
        if authorized:
            key = coordinator_action_key(
                workflow_version=2,
                instance_key=self.manifest.instance.key,
                parent_identifier=identifier,
                stage_kind="intake-transition",
                stage_ordinal=0,
                attempt=0,
                affected_repositories=frozenset(),
                candidate_shas={},
                contract_hashes={},
            )
            self.intake_transitions[identifier] = StatusTransition(
                identifier,
                "backlog",
                "todo",
                key,
            )

    def read_intake_transition(self, parent_identifier: str) -> StatusTransition | None:
        self.events.append(("read-intake-transition", parent_identifier))
        return self.intake_transitions.get(parent_identifier)

    def read_authorizing_comment(
        self,
        parent_identifier: str,
        comment_uuid: str,
    ) -> AuthorizingComment:
        self.events.append(("read-authorizing-comment", parent_identifier, comment_uuid))
        try:
            comment = self.authorizing_comments[(parent_identifier, comment_uuid)]
        except KeyError as error:
            raise RuntimeError("authorizing comment read is unavailable") from error
        if self.parent_drift_after_authorizing_comment:
            self.parent_drift_after_authorizing_comment = False
            state = self.states[parent_identifier]
            assert isinstance(state.metadata, ParentMetadata)
            self.states[parent_identifier] = replace(
                state,
                metadata=replace(
                    state.metadata,
                    stage_ordinal=state.metadata.stage_ordinal + 1,
                ),
            )
        return comment

    def candidate_sha(self, repository_key: str, parent_identifier: str = "PRO-200") -> str:
        return self.states[parent_identifier].snapshot.candidate_shas[repository_key]

    def automatic_repairs_used(self, parent_identifier: str) -> int:
        metadata = self.states[parent_identifier].metadata
        assert isinstance(metadata, ParentMetadata)
        return metadata.automatic_repairs_used

    def record_intake_transition(
        self,
        parent_identifier: str,
        old_status: str,
        new_status: str,
        *,
        action_key: str,
    ) -> None:
        self.events.append(
            ("record-intake-transition", parent_identifier, old_status, new_status, action_key)
        )
        self.intake_transitions[parent_identifier] = StatusTransition(
            parent_identifier,
            old_status,
            new_status,
            action_key,
        )
        state = self.states[parent_identifier]
        self.states[parent_identifier] = replace(
            state,
            applied_action_keys=state.applied_action_keys | {action_key},
        )

    def add_state(
        self,
        identifier: str,
        snapshot: ParentSnapshot,
        *,
        status: str = "in_progress",
        children: tuple[WorkflowChild, ...] = (),
        pull_requests: dict[str, PullRequestTarget] | None = None,
        human_wait: bool = False,
        active_work: bool = False,
        workflow_version: int = 2,
        project_key: str | None = None,
        stage_ordinal: int | None = None,
        hydrate_current_gate_passes: bool = True,
    ) -> None:
        latest_child_stage = max(
            (child.stage_ordinal for child in children),
            default=5,
        )
        metadata = metadata_for(
            snapshot,
            stage_ordinal=stage_ordinal if stage_ordinal is not None else (
                latest_child_stage
                if snapshot.stalled and latest_child_stage <= 5
                else 5
            ),
        )
        if hydrate_current_gate_passes and any(
            child.stage_ordinal == metadata.stage_ordinal
            and child.attempt == snapshot.attempt
            and child.phase in {"review", "qa", "integration_qa"}
            for child in children
        ):
            observed = {
                (child.phase, child.target_key, child.suite_key)
                for child in children
                if child.stage_ordinal == metadata.stage_ordinal
                and child.attempt == snapshot.attempt
            }
            additions: list[WorkflowChild] = []
            for phase_name, evidence_by_repository in (
                ("review", snapshot.reviews),
                ("qa", snapshot.qa),
            ):
                for repository, evidence in evidence_by_repository.items():
                    identity = (phase_name, repository, "")
                    if identity in observed or evidence.result != "pass":
                        continue
                    comment_uuid = evidence_uuid(
                        f"{identifier}-{metadata.stage_ordinal}-{phase_name}-{repository}-pass"
                    )
                    additions.append(
                        WorkflowChild(
                            f"{identifier}-{metadata.stage_ordinal}-{phase_name.upper()}-{repository.upper()}-PASS",
                            repository,
                            repository,
                            "",
                            phase_name,
                            metadata.stage_ordinal,
                            snapshot.attempt,
                            "done",
                            ("review:" if phase_name == "review" else "qa:")
                            + "e" * 64,
                            False,
                            evidence_comment_uuid=comment_uuid,
                            creation_candidate_shas=snapshot.candidate_shas,
                            phase_result="pass",
                            evidence_comment_url=f"https://example.test/evidence/{comment_uuid}",
                        )
                    )
            for suite in self.manifest.integration_suites:
                evidence = snapshot.integration_qa.get(suite.key)
                identity = ("integration_qa", suite.key, suite.key)
                if identity in observed or evidence is None or evidence.result != "pass":
                    continue
                comment_uuid = evidence_uuid(
                    f"{identifier}-{metadata.stage_ordinal}-integration-qa-{suite.key}-pass"
                )
                additions.append(
                    WorkflowChild(
                        f"{identifier}-{metadata.stage_ordinal}-INTEGRATION-QA-{suite.key.upper()}-PASS",
                        suite.key,
                        suite.command_repository,
                        suite.key,
                        "integration_qa",
                        metadata.stage_ordinal,
                        snapshot.attempt,
                        "done",
                        "qa:" + "d" * 64,
                        False,
                        evidence_comment_uuid=comment_uuid,
                        creation_candidate_shas=snapshot.candidate_shas,
                        phase_result="pass",
                        evidence_comment_url=f"https://example.test/evidence/{comment_uuid}",
                    )
                )
            children = children + tuple(additions)
        if workflow_version != metadata.workflow_version:
            if workflow_version != 1:
                raise ValueError("test store only models workflow versions one and two")
            metadata = LegacyParentMetadataV1(
                workflow_version=1,
                metadata_version=1,
                instance_key=metadata.instance_key,
                affected_repositories=metadata.affected_repositories,
                repository_dag=metadata.repository_dag,
                candidate_shas=metadata.candidate_shas,
                contract_hashes=metadata.contract_hashes,
                stage_ordinal=metadata.stage_ordinal,
                merge_plan=metadata.merge_plan,
                merge_state=metadata.merge_state,
                attempt=snapshot.attempt,
                last_action=metadata.last_action,
            )
        elif isinstance(metadata, ParentMetadata):
            # Legacy fixtures used phase-prefixed placeholder keys. Normalize those
            # fixtures to the exact shared gate-creation identity used in production.
            children = tuple(
                replace(
                    child,
                    action_key=coordinator_action_key(
                        workflow_version=metadata.workflow_version,
                        instance_key=metadata.instance_key,
                        parent_identifier=identifier,
                        stage_kind="gates",
                        stage_ordinal=child.stage_ordinal,
                        attempt=child.attempt,
                        affected_repositories=frozenset(
                            metadata.affected_repositories
                        ),
                        candidate_shas=child.creation_candidate_shas,
                        contract_hashes=metadata.contract_hashes,
                    ),
                )
                if child.phase in {"review", "qa", "integration_qa"}
                and child.action_key.startswith(("review:", "qa:"))
                else child
                for child in children
            )
        applied_action_keys = {child.action_key for child in children}
        if isinstance(metadata, ParentMetadata):
            for child in children:
                if (
                    child.phase not in {"review", "qa", "integration_qa"}
                    or child.status != "done"
                    or child.active
                ):
                    continue
                try:
                    applied_action_keys.add(
                        coordinator_action_key(
                            workflow_version=metadata.workflow_version,
                            instance_key=metadata.instance_key,
                            parent_identifier=identifier,
                            stage_kind=f"{child.phase}:{child.target_key}",
                            stage_ordinal=child.stage_ordinal,
                            attempt=child.attempt,
                            affected_repositories=frozenset(
                                metadata.affected_repositories
                            ),
                            candidate_shas=child.creation_candidate_shas,
                            contract_hashes=metadata.contract_hashes,
                        )
                    )
                except WorkflowError:
                    pass
                try:
                    completion = PhaseCompletion(
                        parent_identifier=identifier,
                        repository_key=child.repository_key,
                        phase=child.phase,
                        result=child.phase_result,
                        attempt=child.attempt,
                        candidate_sha=child.creation_candidate_shas[
                            child.repository_key
                        ],
                        pull_request_url="",
                        evidence_comment_uuid=child.evidence_comment_uuid,
                        evidence_comment_url=child.evidence_comment_url,
                        suite_key=child.suite_key,
                        candidate_shas=(
                            child.creation_candidate_shas
                            if child.phase == "integration_qa"
                            else {}
                        ),
                        responsible_repositories=child.responsible_repositories,
                    )
                    if (
                        _phase_completion_schema_problem(
                            completion,
                            manifest=self.manifest,
                        )
                        is None
                    ):
                        self.completions[
                            (identifier, child.evidence_comment_uuid)
                        ] = completion
                except (KeyError, TypeError, ValueError, WorkflowError):
                    pass
        self.states[identifier] = WorkflowState(
            parent_identifier=identifier,
            parent_status=status,
            project_key=project_key or self.manifest.instance.control_project,
            metadata=metadata,
            snapshot=snapshot,
            children=children,
            pull_requests=pull_requests or {},
            applied_action_keys=frozenset(applied_action_keys),
            human_wait=human_wait,
            active_work=active_work,
        )

    def read(self, parent_identifier: str) -> WorkflowState:
        self.events.append(("read-parent", parent_identifier))
        if self.parent_read_failures_remaining:
            self.parent_read_failures_remaining -= 1
            raise RuntimeError("authoritative parent read temporarily unavailable")
        if self.fail_reads_after_first_merge_progress:
            self.fail_reads_after_first_merge_progress -= 1
            raise RuntimeError("authoritative read temporarily unavailable")
        count = self.read_counts.get(parent_identifier, 0) + 1
        self.read_counts[parent_identifier] = count
        state = self.states[parent_identifier]
        state = self.read_state_override_by_count.get(count, state)
        if self.change_on_recovery_reread and count == 2:
            state = replace(state, active_work=True)
            self.states[parent_identifier] = state
        if self.read_parent_identifier_override is not None:
            state = replace(
                state,
                parent_identifier=self.read_parent_identifier_override,
            )
        return state

    def list_active_parents(
        self,
        *,
        instance_key: str,
        project_keys: frozenset[str],
        workflow_versions: frozenset[int],
    ) -> tuple[str, ...]:
        self.list_arguments = (instance_key, project_keys, workflow_versions)
        return tuple(
            identifier
            for identifier, state in self.states.items()
            if state.parent_status in {"todo", "in_progress", "in_review"}
            and state.project_key in project_keys
            and state.metadata is not None
            and state.metadata.instance_key == instance_key
            and state.metadata.workflow_version in workflow_versions
        )

    def initialize_parent(
        self,
        parent_identifier: str,
        metadata: ParentMetadata,
        *,
        action_key: str,
    ) -> None:
        self.events.append(("initialize", parent_identifier, action_key))
        state = self.states[parent_identifier]
        snapshot = ParentSnapshot(
            affected_repositories=metadata.affected_repositories,
        )
        self.states[parent_identifier] = replace(
            state,
            parent_status="in_progress",
            metadata=metadata,
            snapshot=snapshot,
            applied_action_keys=state.applied_action_keys | {action_key},
        )

    def request_human_clarification(
        self,
        parent_identifier: str,
        reason: str,
        *,
        action_key: str,
    ) -> None:
        self.events.append(("human", parent_identifier, reason, action_key))
        state = self.states[parent_identifier]
        self.states[parent_identifier] = replace(
            state,
            human_wait=True,
            applied_action_keys=state.applied_action_keys | {action_key},
        )

    def create_children(
        self,
        parent_identifier: str,
        children: tuple[ChildRequest, ...],
        metadata: ParentMetadata,
        *,
        action_key: str,
    ) -> None:
        self.events.append(("create", parent_identifier, children, action_key))
        state = self.states[parent_identifier]
        snapshot = state.snapshot
        implementation = dict(snapshot.children)
        reviews = dict(snapshot.reviews)
        qa = dict(snapshot.qa)
        integration = dict(snapshot.integration_qa)
        workflow_children = list(state.children)
        for index, request in enumerate(children, start=1):
            workflow_children.append(
                WorkflowChild(
                    identifier=f"{parent_identifier}-CH-{len(workflow_children) + index}",
                    target_key=request.target_key,
                    repository_key=request.repository_key,
                    suite_key=request.suite_key,
                    phase=request.phase,
                    stage_ordinal=request.stage_ordinal,
                    attempt=request.attempt,
                    status="todo",
                    action_key=action_key,
                    active=True,
                    creation_candidate_shas=request.candidate_shas,
                    failure_bundle_digest=(
                        request.failure_bundle.digest
                        if request.failure_bundle is not None
                        else ""
                    ),
                    failure_evidence_uuids=tuple(
                        failure.evidence_comment_uuid for failure in request.failure_refs
                    ),
                    authorizing_comment_uuid=request.authorizing_comment_uuid,
                )
            )
            if request.phase in {"implementation", "repair"}:
                implementation[request.repository_key] = RepositoryEvidence("", "pending")
            elif request.phase == "review":
                reviews[request.repository_key] = RepositoryEvidence(
                    snapshot.candidate_shas[request.repository_key],
                    "pending",
                )
            elif request.phase == "qa":
                qa[request.repository_key] = RepositoryEvidence(
                    snapshot.candidate_shas[request.repository_key],
                    "pending",
                )
            elif request.phase == "integration_qa":
                integration[request.suite_key] = GateEvidence(
                    candidate_shas=snapshot.candidate_shas,
                    result="pending",
                )
        if self.duplicate_repair_child_on_create:
            repairs = [child for child in workflow_children if child.phase == "repair"]
            if repairs:
                workflow_children.append(
                    replace(
                        repairs[-1],
                        identifier=f"{repairs[-1].identifier}-DUPLICATE",
                    )
                )
        if self.replace_repair_digest_after_create:
            workflow_children = [
                replace(child, failure_bundle_digest="f" * 64)
                if child.phase == "repair"
                else child
                for child in workflow_children
            ]
        if self.repair_failure_uuid_corruption is not None:
            corrupted_children = []
            for child in workflow_children:
                if child.phase != "repair":
                    corrupted_children.append(child)
                    continue
                if self.repair_failure_uuid_corruption == "subset":
                    child = replace(
                        child,
                        failure_evidence_uuids=child.failure_evidence_uuids[:-1],
                    )
                elif self.repair_failure_uuid_corruption == "extra":
                    child = replace(
                        child,
                        failure_evidence_uuids=(
                            *child.failure_evidence_uuids,
                            "00000000-0000-4000-8000-000000000099",
                        ),
                    )
                elif self.repair_failure_uuid_corruption == "duplicate":
                    object.__setattr__(
                        child,
                        "failure_evidence_uuids",
                        (*child.failure_evidence_uuids, child.failure_evidence_uuids[-1]),
                    )
                else:
                    raise AssertionError("unknown repair UUID corruption")
                corrupted_children.append(child)
            workflow_children = corrupted_children
        self.states[parent_identifier] = replace(
            state,
            metadata=metadata,
            snapshot=replace(
                snapshot,
                children=implementation,
                reviews=reviews,
                qa=qa,
                integration_qa=integration,
                attempt=metadata.repair_round,
            ),
            children=tuple(workflow_children),
            applied_action_keys=state.applied_action_keys | {action_key},
        )
        if self.fail_reads_after_create:
            self.fail_reads_after_create = False
            self.parent_read_failures_remaining = 2

    def write_phase_completion(
        self,
        completion: PhaseCompletion,
        *,
        action_key: str,
    ) -> None:
        self.events.append(("write-completion", completion.evidence_comment_uuid, action_key))
        self.completions[(completion.parent_identifier, completion.evidence_comment_uuid)] = completion

    def read_phase_completion(
        self,
        parent_identifier: str,
        evidence_comment_uuid: str,
    ) -> PhaseCompletion | None:
        self.events.append(("read-completion", evidence_comment_uuid))
        if self.completion_read_failures_remaining:
            self.completion_read_failures_remaining -= 1
            raise RuntimeError("phase completion read temporarily unavailable")
        value = self.completions.get((parent_identifier, evidence_comment_uuid))
        if value is not None and self.corrupt_completion_read:
            return replace(value, result="blocked" if value.result != "blocked" else "fail")
        if value is not None and self.change_completion_after_first_read:
            self.change_completion_after_first_read = False
            self.completions[(parent_identifier, evidence_comment_uuid)] = replace(
                value,
                result="blocked" if value.result != "blocked" else "fail",
            )
            return value
        if value is not None and self.parent_drift_after_completion_read is not None:
            drift = self.parent_drift_after_completion_read
            self.parent_drift_after_completion_read = None
            state = self.states[parent_identifier]
            assert isinstance(state.metadata, ParentMetadata)
            if drift == "read-error":
                self.parent_read_failures_remaining = 2
            elif drift == "metadata":
                state = replace(
                    state,
                    metadata=replace(
                        state.metadata,
                        stage_ordinal=state.metadata.stage_ordinal + 1,
                    ),
                )
            elif drift == "actions":
                state = replace(
                    state,
                    applied_action_keys=state.applied_action_keys
                    | {"resume:" + "f" * 64},
                )
            elif drift == "children":
                state = replace(
                    state,
                    children=state.children
                    + (
                        WorkflowChild(
                            f"{parent_identifier}-DRIFT",
                            "api",
                            "api",
                            "",
                            "implementation",
                            1,
                            0,
                            "in_progress",
                            "dispatch:" + "f" * 64,
                            True,
                            creation_candidate_shas={},
                        ),
                    ),
                )
            elif drift == "snapshot":
                state = replace(
                    state,
                    snapshot=replace(
                        state.snapshot,
                        recovery_count=state.snapshot.recovery_count + 1,
                    ),
                )
            elif drift == "non-state":
                next_read = self.read_counts.get(parent_identifier, 0) + 1
                self.read_state_override_by_count[next_read] = object()  # type: ignore[assignment]
            elif drift != "read-error":
                raise AssertionError("unknown parent drift")
            if drift != "read-error":
                self.states[parent_identifier] = state
        if value is not None and self.change_after_completion_read:
            state = self.states[parent_identifier]
            assert state.metadata is not None
            self.states[parent_identifier] = replace(
                state,
                metadata=replace(
                    state.metadata,
                    stage_ordinal=state.metadata.stage_ordinal + 1,
                ),
            )
        return value

    def mark_child_done(
        self,
        parent_identifier: str,
        completion: PhaseCompletion,
        metadata: ParentMetadata,
        *,
        action_key: str,
    ) -> None:
        self.events.append(("done", completion.evidence_comment_uuid, action_key))
        state = self.states[parent_identifier]
        snapshot = state.snapshot
        children = list(state.children)
        matching = [
            index
            for index, child in enumerate(children)
            if child.phase == completion.phase
            and child.attempt == completion.attempt
            and (
                child.repository_key == completion.repository_key
                or child.suite_key == completion.suite_key != ""
            )
        ]
        if len(matching) != 1:
            raise RuntimeError("completion child is not unique")
        index = matching[0]
        children[index] = replace(
            children[index],
            status="done",
            active=False,
            evidence_comment_uuid=completion.evidence_comment_uuid,
            phase_result=completion.result,
            evidence_comment_url=completion.evidence_comment_url,
            responsible_repositories=completion.responsible_repositories,
        )

        implementation = dict(snapshot.children)
        candidates = dict(snapshot.candidate_shas)
        reviews = dict(snapshot.reviews)
        qa = dict(snapshot.qa)
        integration = dict(snapshot.integration_qa)
        pull_requests = dict(snapshot.pull_requests)
        targets = dict(state.pull_requests)
        smoke_reads = snapshot.smoke_reads
        if completion.phase in {"implementation", "repair"}:
            previous_sha = candidates.get(completion.repository_key)
            implementation[completion.repository_key] = RepositoryEvidence(
                completion.candidate_sha,
                completion.result,
            )
            if completion.result == "pass":
                candidates[completion.repository_key] = completion.candidate_sha
                pull_requests[completion.repository_key] = PullRequestEvidence(
                    completion.candidate_sha,
                    "open",
                    True,
                    True,
                )
                if completion.pull_request_url:
                    number = int(completion.pull_request_url.rsplit("/", 1)[1])
                    targets[completion.repository_key] = PullRequestTarget(
                        completion.repository_key,
                        number,
                        completion.pull_request_url,
                    )
                if (
                    previous_sha is not None
                    and previous_sha != completion.candidate_sha
                    and not self.retain_gates_on_replacement
                ):
                    reviews.clear()
                    qa.clear()
                    integration.clear()
                    smoke_reads = ()
        elif completion.phase == "review":
            reviews[completion.repository_key] = RepositoryEvidence(
                completion.candidate_sha,
                completion.result,
            )
        elif completion.phase == "qa":
            qa[completion.repository_key] = RepositoryEvidence(
                completion.candidate_sha,
                completion.result,
            )
        elif completion.phase == "integration_qa":
            integration[completion.suite_key] = GateEvidence(
                candidate_shas=completion.candidate_shas,
                result=completion.result,
                responsible_repositories=completion.responsible_repositories,
            )

        self.states[parent_identifier] = replace(
            state,
            metadata=metadata,
            snapshot=replace(
                snapshot,
                children=implementation,
                candidate_shas=candidates,
                reviews=reviews,
                qa=qa,
                integration_qa=integration,
                pull_requests=pull_requests,
                smoke_reads=smoke_reads,
                attempt=metadata.repair_round,
            ),
            children=tuple(children),
            pull_requests=targets,
            applied_action_keys=state.applied_action_keys | {action_key},
        )
        if self.fail_reads_after_done:
            self.fail_reads_after_done = False
            self.parent_read_failures_remaining = 2

    def set_parent_status(
        self,
        parent_identifier: str,
        status: str,
        reason: str,
        metadata: ParentMetadata,
        *,
        action_key: str,
    ) -> None:
        self.events.append(("status", parent_identifier, status, reason, action_key))
        state = self.states[parent_identifier]
        if status == "done" and self.write_incomplete_parent_done:
            self.states[parent_identifier] = replace(state, parent_status=status)
            return
        self.states[parent_identifier] = replace(
            state,
            parent_status=status,
            metadata=metadata,
            applied_action_keys=state.applied_action_keys | {action_key},
        )
        if status == "done" and self.fail_parent_reads_after_parent_done:
            self.fail_parent_reads_after_parent_done = False
            self.parent_read_failures_remaining = 2

    def record_merge_state(
        self,
        parent_identifier: str,
        merge_state: str,
        merged_shas: dict[str, str],
        metadata: ParentMetadata,
        *,
        action_key: str,
    ) -> None:
        self.events.append(
            (
                "merge-state",
                merge_state,
                dict(merged_shas),
                action_key,
                metadata.stage_ordinal,
            )
        )
        state = self.states[parent_identifier]
        pull_requests = dict(state.snapshot.pull_requests)
        for repository, sha in merged_shas.items():
            existing = pull_requests[repository]
            pull_requests[repository] = replace(existing, state="merged", merged_sha=sha)
        self.states[parent_identifier] = replace(
            state,
            metadata=metadata,
            snapshot=replace(
                state.snapshot,
                merge_state=merge_state,
                merged_shas=merged_shas,
                pull_requests=pull_requests,
            ),
            applied_action_keys=state.applied_action_keys | {action_key},
        )
        if merge_state == "merging" and not merged_shas and self.change_target_after_reservation:
            current = self.states[parent_identifier]
            targets = dict(current.pull_requests)
            targets["api"] = PullRequestTarget(
                "api",
                99,
                "https://github.com/codeExploreHub/sample-commerce-api/pull/99",
            )
            self.states[parent_identifier] = replace(current, pull_requests=targets)
        if (
            merge_state == "merging"
            and not merged_shas
            and self.add_pr_evidence_after_reservation
        ):
            current = self.states[parent_identifier]
            evidence = dict(current.snapshot.pull_requests)
            evidence["foreign"] = PullRequestEvidence(
                "f" * 40, "open", True, True
            )
            self.states[parent_identifier] = replace(
                current,
                snapshot=replace(current.snapshot, pull_requests=evidence),
            )
        if (
            merge_state == "merging"
            and len(merged_shas) == 1
            and self.change_candidate_after_first_progress
        ):
            current = self.states[parent_identifier]
            candidates = dict(current.snapshot.candidate_shas)
            candidates["web"] = "f" * 40
            self.states[parent_identifier] = replace(
                current,
                metadata=replace(current.metadata, candidate_shas=candidates),
                snapshot=replace(current.snapshot, candidate_shas=candidates),
            )
        if (
            merge_state == "merging"
            and len(merged_shas) == 1
            and self.inject_merge_progress_read_failure
            and not self.failed_merge_progress_read
        ):
            self.failed_merge_progress_read = True
            self.fail_reads_after_first_merge_progress = 2

    def write_smoke_read(
        self,
        parent_identifier: str,
        smoke_read: SmokeRead,
        metadata: ParentMetadata,
        *,
        action_key: str,
    ) -> None:
        self.events.append(("write-smoke", smoke_read, action_key))
        state = self.states[parent_identifier]
        if self.write_smoke_evidence_without_transition:
            self.states[parent_identifier] = replace(
                state,
                snapshot=replace(
                    state.snapshot,
                    smoke_reads=state.snapshot.smoke_reads + (smoke_read,),
                ),
            )
            return
        self.states[parent_identifier] = replace(
            state,
            metadata=metadata,
            snapshot=replace(
                state.snapshot,
                smoke_reads=state.snapshot.smoke_reads + (smoke_read,),
            ),
            applied_action_keys=state.applied_action_keys | {action_key},
        )
        if self.fail_parent_reads_after_smoke_write:
            self.fail_parent_reads_after_smoke_write = False
            self.parent_read_failures_remaining = 2
    def rerun_child(
        self,
        parent_identifier: str,
        child_identifier: str,
        metadata: ParentMetadata,
        *,
        action_key: str,
    ) -> None:
        self.events.append(("rerun", parent_identifier, child_identifier, action_key))
        state = self.states[parent_identifier]
        children = tuple(
            replace(child, active=True)
            if self.activate_on_rerun and child.identifier == child_identifier
            else child
            for child in state.children
        )
        self.states[parent_identifier] = replace(
            state,
            metadata=metadata,
            snapshot=replace(
                state.snapshot,
                recovery_count=1,
                reviews=(
                    {"api": RepositoryEvidence(SHA["api"], "pass")}
                    if self.mutate_gate_on_rerun
                    else state.snapshot.reviews
                ),
            ),
            children=children,
            active_work=self.activate_on_rerun,
            applied_action_keys=state.applied_action_keys | {action_key},
        )
        if self.fail_reads_after_rerun:
            self.fail_reads_after_rerun = False
            self.parent_read_failures_remaining = 2
        if self.override_parent_after_rerun:
            self.read_parent_identifier_override = "PRO-999"


class FakeGitHub:
    def __init__(self, event_log: list[tuple[object, ...]] | None = None) -> None:
        self.heads = {"api": SHA["api"], "web": SHA["web"]}
        self.mergeable = {"api": True, "web": True}
        self.checks = {"api": True, "web": True}
        self.merged: list[tuple[str, int]] = []
        self.merged_shas = {"api": SHA["api"], "web": SHA["web"]}
        self.fail_on: str | None = None
        self.commit_then_error_on: str | None = None
        self.malformed_ack_on: str | None = None
        self.ack_without_commit_on: str | None = None
        self.fail_merged_reread_on: str | None = None
        self.fail_merged_rereads_remaining: dict[str, int] = {}
        self.read_failures_remaining: dict[str, int] = {}
        self.malformed_read_on: str | None = None
        self.missing_merge_sha_on: str | None = None
        self.committed: set[str] = set()
        self.event_log = event_log

    @staticmethod
    def _key(repository: str) -> str:
        return repository.rsplit("-", 1)[-1]

    def get_pull_request(self, repository: str, number: int) -> PullRequestInfo:
        key = self._key(repository)
        event = ("github-read-pr", key, number)
        if self.event_log is not None:
            self.event_log.append(event)
        if self.read_failures_remaining.get(key, 0):
            self.read_failures_remaining[key] -= 1
            raise GitHubBoundaryError("authoritative pull request read unavailable")
        if self.malformed_read_on == key:
            return object()  # type: ignore[return-value]
        if self.fail_merged_reread_on == key and key in self.committed:
            raise GitHubBoundaryError("authoritative merged reread unavailable")
        if key in self.committed and self.fail_merged_rereads_remaining.get(key, 0):
            self.fail_merged_rereads_remaining[key] -= 1
            raise GitHubBoundaryError("authoritative merged reread temporarily unavailable")
        return PullRequestInfo(
            repository,
            number,
            "merged" if key in self.committed else "open",
            self.heads[key],
            "main",
            self.mergeable[key],
            "2026-08-27T10:00:00Z" if key in self.committed else None,
            (
                None
                if key == self.missing_merge_sha_on
                else self.merged_shas[key]
            )
            if key in self.committed
            else None,
        )

    def required_status_checks(
        self,
        repository: str,
        base_ref: str,
        expected_sha: str,
    ) -> RequiredStatusChecks:
        key = self._key(repository)
        event = ("github-read-checks", key, expected_sha)
        if self.event_log is not None:
            self.event_log.append(event)
        return RequiredStatusChecks(
            repository,
            base_ref,
            expected_sha,
            True,
            (),
            (),
            (),
            (),
            self.checks[key],
        )

    def merge_pull_request(
        self,
        repository: str,
        number: int,
        *,
        expected_sha: str,
    ) -> MergeResult:
        key = self._key(repository)
        if self.event_log is not None:
            self.event_log.append(("github-merge", key, number))
        if self.fail_on == key:
            raise GitHubBoundaryError("merge failed")
        self.merged.append((repository, number))
        if self.ack_without_commit_on != key:
            self.committed.add(key)
        if self.commit_then_error_on == key:
            raise GitHubBoundaryError("merge acknowledgement lost")
        if self.malformed_ack_on == key:
            return MergeResult(repository, number, False, expected_sha, "")
        return MergeResult(repository, number, True, expected_sha, self.merged_shas[key])


class FakeSmokeExecutor:
    def __init__(
        self,
        result: SmokeRead,
        *,
        bind_observation_id: bool = True,
    ) -> None:
        self.result = result
        self.bind_observation_id = bind_observation_id
        self.calls: list[tuple[str, tuple[str, ...], dict[str, str], str]] = []

    def execute(
        self,
        parent_identifier: str,
        repositories: tuple[str, ...],
        merged_shas: MappingProxyType | dict[str, str],
        *,
        action_key: str,
    ) -> SmokeRead:
        self.calls.append(
            (parent_identifier, repositories, dict(merged_shas), action_key)
        )
        if self.bind_observation_id:
            return replace(self.result, observation_id=action_key)
        return self.result


class FakeScopeResolver:
    def __init__(self, resolution: ScopeResolution) -> None:
        self.resolution = resolution
        self.calls: list[str] = []

    def resolve(self, parent_identifier: str) -> ScopeResolution:
        self.calls.append(parent_identifier)
        return self.resolution


class RecordingProcessBackend:
    def __init__(self, manifest) -> None:
        self.manifest = manifest
        self.temporary = TemporaryDirectory()
        self.next_pid = 5001
        self.alive: set[int] = set()
        self.owners: dict[int, int] = {}
        self.identities: dict[int, str] = {}
        self.pid_repositories: dict[int, str] = {}
        self.started: list[tuple[object, ProcessRun, str]] = []
        self.stopped: list[tuple[object, str, str, str]] = []
        self.healthy = True
        self.start_hook = None

    def port_owner(self, port: int) -> int | None:
        return self.owners.get(port)

    def spawn(self, argv: tuple[str, ...], cwd: Path) -> int:
        repository, specification = next(
            (key, item)
            for key, item in self.manifest.repositories.items()
            if item.local_path == cwd and item.commands["start"] == argv
        )
        pid = self.next_pid
        self.next_pid += 1
        self.alive.add(pid)
        self.identities[pid] = f"fake-start-{pid}"
        self.pid_repositories[pid] = repository
        self.started.append(
            (
                specification.services,
                ProcessRun(repository, "recorded", argv, cwd),
                "recorded",
            )
        )
        if self.start_hook is not None:
            self.start_hook(repository)
        return pid

    def start_identity(self, pid: int) -> str:
        return self.identities[pid]

    def wait_healthy(self, health_url: str, pid: int) -> bool:
        if not self.healthy or pid not in self.alive:
            return False
        port = urlsplit(health_url).port
        assert port is not None
        self.owners[port] = pid
        return True

    def is_alive(self, pid: int) -> bool:
        return pid in self.alive

    def stop(self, pid: int) -> None:
        repository = self.pid_repositories[pid]
        self.stopped.append((
            type("StoppedRecord", (), {"repository_key": repository})(),
            "recorded",
            repository,
            "recorded",
        ))
        self.alive.discard(pid)
        for port, owner in tuple(self.owners.items()):
            if owner == pid:
                del self.owners[port]


def FakeOwnedProcessManager(manifest) -> ProcessManager:
    backend = RecordingProcessBackend(manifest)
    _PROCESS_TEMPORARIES.append(backend.temporary)
    registry = ProcessRegistry(
        Path(backend.temporary.name) / "owned-processes.json"
    )
    return ProcessManager(
        registry,
        backend,
        owner_token="smoke-owner",
        manifest=manifest,
    )


class SelfReportingFakeRunner:
    def verify(self, repository_key, expected_sha, cwd, *, argv):
        raise AssertionError("untrusted runner must never be called")

    def run(self, repository_key, candidate_shas, argv, cwd):
        raise AssertionError("untrusted runner must never be called")


class SelfReportingConcreteRunner(LocalExactShaCommandRunner):
    def verify(self, repository_key, expected_sha, cwd, *, argv):
        return ExactShaVerification(repository_key, expected_sha, expected_sha, argv)

    def run(self, repository_key, candidate_shas, argv, cwd):
        return ExactShaCommandResult(True, candidate_shas)


class MutableClosedCommandBackend:
    def __init__(self, manifest) -> None:
        self.heads = {
            repository.local_path: SHA[key]
            for key, repository in manifest.repositories.items()
        }
        self.calls: list[tuple[tuple[str, ...], Path]] = []
        self.command_hook = None
        self.fail_argv: set[tuple[str, ...]] = set()

    def run(self, argv: tuple[str, ...], cwd: Path) -> ClosedCommandResult:
        self.calls.append((argv, cwd))
        if argv == ("git", "rev-parse", "HEAD"):
            return ClosedCommandResult(0, self.heads[cwd] + "\n", "")
        if self.command_hook is not None:
            self.command_hook(argv, cwd)
        return ClosedCommandResult(1 if argv in self.fail_argv else 0, "", "")


class WorkflowValueValidationTests(unittest.TestCase):
    def failure_ref(
        self,
        child: str,
        repository: str,
        *,
        phase: str,
        comment_digit: str,
    ) -> FailureEvidenceRef:
        comment_uuid = f"123e4567-e89b-42d3-a456-42661417400{comment_digit}"
        return FailureEvidenceRef(
            child_identifier=child,
            phase=phase,
            result="fail",
            stage_ordinal=7,
            repair_round=2,
            candidate_shas=SHA,
            responsible_repositories=(repository,),
            evidence_comment_uuid=comment_uuid,
            evidence_comment_url=f"https://multica.example/comments/{comment_uuid}",
        )

    def test_failure_bundle_is_canonical_and_order_independent(self):
        review = self.failure_ref("PRO-201", "api", phase="review", comment_digit="1")
        qa = self.failure_ref("PRO-202", "api", phase="qa", comment_digit="2")
        first = FailureBundle.build("PRO-200", 2, 7, 3, SHA, (review, qa))
        second = FailureBundle.build("PRO-200", 2, 7, 3, SHA, (qa, review))

        self.assertEqual(first, second)
        self.assertEqual(
            first.digest,
            "63ea21a99267330975b0f7a185c8bcb9e3fa8752456ed70b0a0a3c34185a47bf",
        )
        self.assertEqual(first.for_repository("api"), (review, qa))
        self.assertEqual(
            review.to_canonical_dict(),
            {
                "candidate_shas": SHA,
                "child_identifier": "PRO-201",
                "evidence_comment_url": "https://multica.example/comments/123e4567-e89b-42d3-a456-426614174001",
                "evidence_comment_uuid": "123e4567-e89b-42d3-a456-426614174001",
                "phase": "review",
                "repair_round": 2,
                "responsible_repositories": ["api"],
                "result": "fail",
                "stage_ordinal": 7,
                "suite_key": "",
            },
        )

    def test_failure_evidence_rejects_malformed_frozen_values(self):
        reference = self.failure_ref("PRO-201", "api", phase="review", comment_digit="1")
        malformed = {
            "child identifier type": {"child_identifier": 201},
            "gate phase": {"phase": "implementation"},
            "nonpassing result": {"result": "pass"},
            "stage ordinal": {"stage_ordinal": True},
            "repair round": {"repair_round": -1},
            "candidate map": {"candidate_shas": [("api", SHA["api"])]},
            "duplicate owner": {"responsible_repositories": ("api", "api")},
            "UUID": {"evidence_comment_uuid": "123E4567-e89b-42d3-a456-426614174001"},
            "URL": {"evidence_comment_url": "http://multica.example/comments/1"},
        }

        for name, changes in malformed.items():
            with self.subTest(name=name):
                with self.assertRaises(WorkflowError):
                    replace(reference, **changes)

    def test_failure_bundle_rejects_forged_digest(self):
        failure = self.failure_ref("PRO-201", "api", phase="review", comment_digit="1")
        bundle = FailureBundle.build("PRO-200", 2, 7, 3, SHA, (failure,))

        with self.assertRaises(WorkflowError):
            replace(bundle, digest="f" * 64)

    def test_repair_request_requires_its_complete_nonempty_failure_partition(self):
        failure = self.failure_ref("PRO-201", "api", phase="review", comment_digit="1")
        bundle = FailureBundle.build("PRO-200", 2, 7, 3, SHA, (failure,))
        target = PullRequestTarget(
            "api", 12, "https://github.com/codeExploreHub/sample-commerce-api/pull/12"
        )
        request = ChildRequest(
            "api", "api", "", "repair", 8, 3, SHA, target,
            failure_bundle=bundle,
            failure_refs=bundle.for_repository("api"),
        )

        self.assertEqual(request.failure_refs, (failure,))
        for field, value in (("failure_bundle", None), ("failure_refs", ())):
            with self.subTest(field=field):
                with self.assertRaises(WorkflowError):
                    replace(request, **{field: value})
        with self.assertRaises(WorkflowError):
            replace(request, failure_refs=(replace(failure, responsible_repositories=("web",)),))
        with self.assertRaises(WorkflowError):
            ChildRequest("api", "api", "", "implementation", 8, 0, SHA, failure_bundle=bundle)

    def test_non_repair_request_requires_an_empty_immutable_failure_ref_tuple(self):
        request = ChildRequest(
            "api", "api", "", "implementation", 8, 0, SHA, failure_refs=()
        )

        self.assertEqual(request.failure_refs, ())
        for invalid_refs in ([], frozenset(), None):
            with self.subTest(invalid_refs=type(invalid_refs).__name__):
                with self.assertRaises(WorkflowError):
                    ChildRequest(
                        "api",
                        "api",
                        "",
                        "implementation",
                        8,
                        0,
                        SHA,
                        failure_refs=invalid_refs,  # type: ignore[arg-type]
                    )


class WorkflowCompletionSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_manifest(FIXTURE)

    def test_nonpassing_gate_requires_in_scope_responsible_repository(self):
        completion = completion_for(
            "api", phase="review", result="fail", responsible_repositories=()
        )
        self.assertEqual(
            _phase_completion_schema_problem(completion, manifest=self.manifest),
            "non-PASS gate completion requires responsible repositories",
        )

    def test_pass_gate_forbids_responsible_repositories(self):
        completion = completion_for(
            "api", phase="qa", result="pass", responsible_repositories=("api",)
        )
        self.assertEqual(
            _phase_completion_schema_problem(completion, manifest=self.manifest),
            "PASS gate completion cannot name responsible repositories",
        )

    def test_integration_gate_ownership_is_a_nonempty_manifest_suite_subset(self):
        for owners in (("api",), ("api", "web")):
            with self.subTest(owners=owners):
                completion = completion_for(
                    "web",
                    phase="integration_qa",
                    result="fail",
                    suite_key="web-api",
                    candidate_shas=dict(SHA),
                    responsible_repositories=owners,
                )
                self.assertIsNone(
                    _phase_completion_schema_problem(completion, manifest=self.manifest)
                )
        rejected = completion_for(
            "web",
            phase="integration_qa",
            result="fail",
            suite_key="web-api",
            candidate_shas=dict(SHA),
            responsible_repositories=("notifications",),
        )
        self.assertEqual(
            _phase_completion_schema_problem(rejected, manifest=self.manifest),
            "integration QA completion responsible repositories are outside its suite",
        )

    def test_repair_completion_requires_only_a_valid_failure_bundle_digest(self):
        digest = "e" * 64
        repair = completion_for(
            "api", phase="repair", attempt=1, failure_bundle_digest=digest
        )
        non_repair = completion_for("api", failure_bundle_digest=digest)

        self.assertIsNone(_phase_completion_schema_problem(repair, manifest=self.manifest))
        self.assertEqual(
            _phase_completion_schema_problem(non_repair, manifest=self.manifest),
            "non-repair completion cannot contain a failure bundle digest",
        )


class TaskFourWorkflowFixture:
    def setUp(self) -> None:
        self.manifest = load_manifest(FIXTURE)
        self.store = FakeWorkflowStore(self.manifest)
        self.github = FakeGitHub(self.store.events)
        self.workflow = GenericWorkflow(
            self.manifest,
            self.store,
            self.store,
            github=self.github,
        )

    def add_failed_review_state(
        self,
        *,
        parent_identifier: str = "PRO-200",
        attempt: int = 2,
    ) -> WorkflowState:
        snapshot = ParentSnapshot(
            affected_repositories=("api",),
            candidate_shas={"api": SHA["api"]},
            children={"api": RepositoryEvidence(SHA["api"], "pass")},
            pull_requests={
                "api": PullRequestEvidence(SHA["api"], "open", True, True)
            },
            reviews={"api": RepositoryEvidence(SHA["api"], "fail")},
            qa={"api": RepositoryEvidence(SHA["api"], "pass")},
            attempt=attempt,
        )
        review_uuid = evidence_uuid(f"{parent_identifier}-round-{attempt}-review-failure")
        review = WorkflowChild(
            f"{parent_identifier}-REVIEW",
            "api",
            "api",
            "",
            "review",
            5,
            attempt,
            "done",
            "review:" + "7" * 64,
            False,
            evidence_comment_uuid=review_uuid,
            creation_candidate_shas=snapshot.candidate_shas,
            phase_result="fail",
            evidence_comment_url=f"https://example.test/evidence/{review_uuid}",
            responsible_repositories=("api",),
        )
        self.store.add_state(
            parent_identifier,
            snapshot,
            children=(review,),
            pull_requests={"api": pull_request_targets()["api"]},
            stage_ordinal=5,
        )
        return self.store.states[parent_identifier]

    def authorize_extra_round(
        self,
        *,
        comment_uuid: str = "00000000-0000-4000-8000-000000000021",
        author_type: str = "member",
        authoritative_url: str | None = None,
        metadata_digest: str | None = None,
        add_authoritative_comment: bool = True,
    ) -> FailureBundle:
        state = self.add_failed_review_state()
        self.store.authorizing_comments.clear()
        decision = ParentDecision(
            DecisionKind.REPAIR,
            "authorized repair",
            ("api",),
            3,
        )
        bundle = self.workflow._failure_bundle(state, decision)
        comment_url = f"https://example.test/authorization/{comment_uuid}"
        assert isinstance(state.metadata, ParentMetadata)
        self.store.states["PRO-200"] = replace(
            state,
            metadata=replace(
                state.metadata,
                repair_authorization=RepairAuthorization(
                    comment_uuid=comment_uuid,
                    comment_url=comment_url,
                    bundle_digest=metadata_digest or bundle.digest,
                    granted_round=3,
                ),
            ),
        )
        if add_authoritative_comment:
            self.store.authorizing_comments[("PRO-200", comment_uuid)] = AuthorizingComment(
                comment_uuid,
                authoritative_url or comment_url,
                author_type,
            )
        return bundle

    def dispatch_authorized_repair(self) -> FailureBundle:
        bundle = self.authorize_extra_round()
        result = self.workflow.resume_parent("PRO-200")
        self.assertEqual(result.next_action, "repair")
        return bundle


class WorkflowCompletionImmutabilityTests(TaskFourWorkflowFixture, unittest.TestCase):
    def test_completed_repair_cannot_submit_a_second_replacement_sha(self):
        bundle = self.dispatch_authorized_repair()
        state = self.store.states["PRO-200"]
        self.store.states["PRO-200"] = replace(
            state,
            snapshot=replace(
                state.snapshot,
                pull_requests={
                    "api": replace(
                        state.snapshot.pull_requests["api"],
                        head_sha=REPLACEMENT_SHA,
                    )
                },
            ),
        )

        first = self.workflow.record_phase_completion(
            repair_completion(REPLACEMENT_SHA, bundle.digest)
        )
        second = self.workflow.record_phase_completion(
            repair_completion(OTHER_SHA, bundle.digest)
        )

        self.assertEqual(first.completed_child_status, "done")
        self.assertEqual(second.next_action, "block")
        self.assertEqual(self.store.candidate_sha("api"), REPLACEMENT_SHA)

    def test_identical_completed_repair_replay_is_noop(self):
        bundle = self.dispatch_authorized_repair()
        state = self.store.states["PRO-200"]
        self.store.states["PRO-200"] = replace(
            state,
            snapshot=replace(
                state.snapshot,
                pull_requests={
                    "api": replace(
                        state.snapshot.pull_requests["api"],
                        head_sha=REPLACEMENT_SHA,
                    )
                },
            ),
        )
        completion = repair_completion(REPLACEMENT_SHA, bundle.digest)
        self.workflow.record_phase_completion(completion)

        replay = self.workflow.record_phase_completion(completion)

        self.assertEqual(replay.next_action, "noop")
        self.assertEqual(replay.mutation_count, 0)

    def test_historical_stage_wrong_round_and_wrong_bundle_cannot_complete(self):
        for corruption in ("historical-stage", "wrong-round", "wrong-bundle"):
            with self.subTest(corruption=corruption):
                bundle = self.dispatch_authorized_repair()
                state = self.store.states["PRO-200"]
                completion = repair_completion(REPLACEMENT_SHA, bundle.digest)
                if corruption == "historical-stage":
                    assert isinstance(state.metadata, ParentMetadata)
                    self.store.states["PRO-200"] = replace(
                        state,
                        metadata=replace(
                            state.metadata,
                            stage_ordinal=state.metadata.stage_ordinal + 1,
                        ),
                    )
                elif corruption == "wrong-round":
                    completion = completion_for(
                        "api",
                        parent="PRO-200",
                        phase="repair",
                        attempt=2,
                        sha=REPLACEMENT_SHA,
                        failure_bundle_digest=bundle.digest,
                    )
                else:
                    completion = repair_completion(REPLACEMENT_SHA, "f" * 64)
                self.store.events.clear()

                result = self.workflow.record_phase_completion(completion)

                self.assertEqual(result.next_action, "block")
                self.assertEqual(result.mutation_count, 0)
                self.assertFalse(
                    any(event[0] in {"write-completion", "done"} for event in self.store.events)
                )

    def test_repair_completion_requires_the_child_creation_candidate_map(self):
        bundle = self.dispatch_authorized_repair()
        state = self.store.states["PRO-200"]
        self.store.states["PRO-200"] = replace(
            state,
            children=tuple(
                replace(child, creation_candidate_shas={"api": OTHER_SHA})
                if child.phase == "repair"
                else child
                for child in state.children
            ),
        )
        self.store.events.clear()

        result = self.workflow.record_phase_completion(
            repair_completion(REPLACEMENT_SHA, bundle.digest)
        )

        self.assertEqual(result.next_action, "block")
        self.assertEqual(result.mutation_count, 0)
        self.assertFalse(any(event[0] == "write-completion" for event in self.store.events))

    def test_current_repair_completion_accepts_only_its_exact_declared_pr_head(self):
        for authoritative_head, expected_action in (
            (REPLACEMENT_SHA, "complete"),
            (OTHER_SHA, "block"),
        ):
            with self.subTest(authoritative_head=authoritative_head):
                bundle = self.dispatch_authorized_repair()
                state = self.store.states["PRO-200"]
                self.store.states["PRO-200"] = replace(
                    state,
                    snapshot=replace(
                        state.snapshot,
                        pull_requests={
                            "api": PullRequestEvidence(
                                authoritative_head,
                                "open",
                                True,
                                True,
                            )
                        },
                    ),
                )
                self.store.events.clear()

                result = self.workflow.record_phase_completion(
                    repair_completion(REPLACEMENT_SHA, bundle.digest)
                )

                if expected_action == "complete":
                    self.assertEqual(result.completed_child_status, "done")
                else:
                    self.assertEqual(result.next_action, "block")
                    self.assertEqual(result.mutation_count, 0)
                    self.assertFalse(
                        any(event[0] == "write-completion" for event in self.store.events)
                    )


class WorkflowPullRequestDriftTests(TaskFourWorkflowFixture, unittest.TestCase):
    def test_each_managed_pull_request_head_drift_blocks_gate_dispatch_without_mutation(self):
        base = replace(passing_snapshot(), reviews={}, qa={}, integration_qa={})
        for repository in base.affected_repositories:
            with self.subTest(repository=repository):
                pull_requests = dict(base.pull_requests)
                pull_requests[repository] = replace(
                    pull_requests[repository],
                    head_sha=OTHER_SHA,
                )
                self.store.add_state(
                    "PRO-200",
                    replace(base, pull_requests=pull_requests),
                    pull_requests=pull_request_targets(),
                )
                before = self.store.states["PRO-200"]
                self.store.events.clear()

                result = self.workflow.resume_parent("PRO-200")

                self.assertEqual(result.next_action, "block")
                self.assertIn("out-of-band pull-request head change", result.reason)
                self.assertEqual(result.mutation_count, 0)
                self.assertEqual(self.store.states["PRO-200"], before)
                self.assertFalse(any(event[0] == "create" for event in self.store.events))

    def test_partial_multi_repository_head_drift_is_never_adopted(self):
        snapshot = replace(
            passing_snapshot(),
            reviews={},
            qa={},
            integration_qa={},
            pull_requests={
                "api": PullRequestEvidence(OTHER_SHA, "open", True, True),
                "web": passing_snapshot().pull_requests["web"],
            },
        )
        self.store.add_state(
            "PRO-200",
            snapshot,
            pull_requests=pull_request_targets(),
        )
        self.store.events.clear()

        result = self.workflow.resume_parent("PRO-200")

        self.assertEqual(result.next_action, "block")
        self.assertEqual(result.mutation_count, 0)
        self.assertEqual(
            dict(self.store.states["PRO-200"].snapshot.candidate_shas),
            dict(passing_snapshot().candidate_shas),
        )
        self.assertFalse(any(event[0] == "create" for event in self.store.events))

    def test_pull_request_head_drift_blocks_repair_and_merge_before_mutation(self):
        self.add_failed_review_state(attempt=0)
        repair_state = self.store.states["PRO-200"]
        self.store.states["PRO-200"] = replace(
            repair_state,
            snapshot=replace(
                repair_state.snapshot,
                pull_requests={
                    "api": PullRequestEvidence(OTHER_SHA, "open", True, True)
                },
            ),
        )
        self.store.events.clear()

        repair = self.workflow.resume_parent("PRO-200")

        self.assertEqual(repair.next_action, "block")
        self.assertEqual(repair.mutation_count, 0)
        self.assertFalse(any(event[0] == "create" for event in self.store.events))

        merge_snapshot = passing_snapshot()
        pull_requests = dict(merge_snapshot.pull_requests)
        pull_requests["api"] = replace(pull_requests["api"], head_sha=OTHER_SHA)
        self.store.add_state(
            "PRO-200",
            replace(merge_snapshot, pull_requests=pull_requests),
            pull_requests=pull_request_targets(),
        )
        self.store.events.clear()

        merge = self.workflow.execute_merge_plan("PRO-200")

        self.assertEqual(merge.next_action, "block")
        self.assertEqual(merge.mutation_count, 0)
        self.assertEqual(self.github.merged, [])
        self.assertFalse(any(event[0] == "merge-state" for event in self.store.events))


class WorkflowRepairAuthorizationTests(TaskFourWorkflowFixture, unittest.TestCase):
    def test_parent_drift_after_authorization_comment_blocks_repair_dispatch(self):
        self.authorize_extra_round()
        before = self.store.states["PRO-200"]
        assert isinstance(before.metadata, ParentMetadata)
        self.store.parent_drift_after_authorizing_comment = True
        self.store.events.clear()

        result = self.workflow.resume_parent("PRO-200")

        self.assertEqual(result.next_action, "block")
        self.assertEqual(result.mutation_count, 0)
        after = self.store.states["PRO-200"]
        assert isinstance(after.metadata, ParentMetadata)
        self.assertEqual(
            after.metadata.stage_ordinal,
            before.metadata.stage_ordinal + 1,
        )
        self.assertFalse(any(event[0] == "create" for event in self.store.events))

    def test_automatic_rounds_one_and_two_increment_only_automatic_count(self):
        for current_round, expected_round in ((0, 1), (1, 2)):
            with self.subTest(current_round=current_round):
                self.add_failed_review_state(attempt=current_round)
                self.store.events.clear()

                result = self.workflow.resume_parent("PRO-200")

                self.assertEqual(result.next_action, "repair")
                metadata = self.store.states["PRO-200"].metadata
                assert isinstance(metadata, ParentMetadata)
                self.assertEqual(metadata.repair_round, expected_round)
                self.assertEqual(metadata.automatic_repairs_used, expected_round)

    def test_bundle_bound_member_authorization_grants_exactly_one_extra_round(self):
        self.authorize_extra_round()

        first = self.workflow.resume_parent("PRO-200")
        second = self.workflow.resume_parent("PRO-200")

        self.assertEqual(first.next_action, "repair")
        self.assertEqual(first.created_children, (("api", "repair"),))
        self.assertEqual(second.next_action, "noop")
        self.assertEqual(self.store.automatic_repairs_used("PRO-200"), 2)
        metadata = self.store.states["PRO-200"].metadata
        assert isinstance(metadata, ParentMetadata)
        self.assertEqual(metadata.repair_round, 3)
        self.assertIsNone(metadata.repair_authorization)
        self.assertEqual(
            len([event for event in self.store.events if event[0] == "read-authorizing-comment"]),
            1,
        )
        self.assertEqual(
            len([event for event in self.store.events if event[0] == "create"]),
            1,
        )

    def test_extra_round_rejects_non_member_wrong_bundle_and_missing_comment(self):
        cases = (
            {"author_type": "agent"},
            {"metadata_digest": "f" * 64},
            {"add_authoritative_comment": False},
        )
        for options in cases:
            with self.subTest(options=options):
                self.authorize_extra_round(**options)
                before = self.store.states["PRO-200"]
                self.store.events.clear()

                result = self.workflow.resume_parent("PRO-200")

                self.assertEqual(result.next_action, "block")
                self.assertEqual(result.mutation_count, 0)
                self.assertEqual(self.store.states["PRO-200"], before)
                self.assertFalse(any(event[0] == "create" for event in self.store.events))

    def test_extra_round_rejects_wrong_authoritative_url_and_skipped_round(self):
        self.authorize_extra_round(
            authoritative_url="https://example.test/authorization/different"
        )
        wrong_url = self.workflow.resume_parent("PRO-200")
        self.assertEqual(wrong_url.next_action, "block")
        self.assertEqual(wrong_url.mutation_count, 0)

        self.authorize_extra_round()
        state = self.store.states["PRO-200"]
        assert isinstance(state.metadata, ParentMetadata)
        assert isinstance(state.metadata.repair_authorization, RepairAuthorization)
        object.__setattr__(state.metadata.repair_authorization, "granted_round", 4)
        self.store.events.clear()

        skipped = self.workflow.resume_parent("PRO-200")

        self.assertEqual(skipped.next_action, "block")
        self.assertEqual(skipped.mutation_count, 0)
        self.assertFalse(any(event[0] == "create" for event in self.store.events))

    def test_authorizing_comment_uuid_changes_repair_action_identity(self):
        keys: list[str] = []
        for comment_uuid in (
            "00000000-0000-4000-8000-000000000021",
            "00000000-0000-4000-8000-000000000022",
        ):
            self.authorize_extra_round(comment_uuid=comment_uuid)
            self.store.events.clear()

            result = self.workflow.resume_parent("PRO-200")

            self.assertEqual(result.next_action, "repair")
            assert result.action_key is not None
            keys.append(result.action_key)
        self.assertNotEqual(keys[0], keys[1])

    def test_historical_use_of_authorizing_comment_uuid_cannot_grant_again(self):
        comment_uuid = "00000000-0000-4000-8000-000000000021"
        bundle = self.authorize_extra_round(comment_uuid=comment_uuid)
        state = self.store.states["PRO-200"]
        historical = WorkflowChild(
            "PRO-200-HISTORICAL-REPAIR",
            "api",
            "api",
            "",
            "repair",
            4,
            3,
            "done",
            "repair:" + "8" * 64,
            False,
            creation_candidate_shas=state.snapshot.candidate_shas,
            failure_bundle_digest=bundle.digest,
            failure_evidence_uuids=(evidence_uuid("historical-repair-failure"),),
            authorizing_comment_uuid=comment_uuid,
        )
        self.store.states["PRO-200"] = replace(
            state,
            children=state.children + (historical,),
            applied_action_keys=state.applied_action_keys | {historical.action_key},
        )
        self.store.events.clear()

        result = self.workflow.resume_parent("PRO-200")

        self.assertEqual(result.next_action, "block")
        self.assertEqual(result.mutation_count, 0)
        self.assertFalse(any(event[0] == "create" for event in self.store.events))


class WorkflowLegacyCompletionTests(TaskFourWorkflowFixture, unittest.TestCase):
    def test_completed_version_one_parent_remains_readable_without_migration(self):
        base = passing_snapshot()
        snapshot = replace(
            base,
            merge_state="merged",
            merged_shas=base.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in base.candidate_shas.items()
            },
            smoke_reads=(
                passing_smoke(
                    observation_id=SMOKE_OBSERVATION["first"],
                    shas=dict(base.candidate_shas),
                ),
                passing_smoke(
                    observation_id=SMOKE_OBSERVATION["second"],
                    shas=dict(base.candidate_shas),
                ),
            ),
        )
        self.store.add_state(
            "PRO-200",
            snapshot,
            status="done",
            workflow_version=1,
            pull_requests=pull_request_targets(),
        )
        state = self.store.states["PRO-200"]
        assert isinstance(state.metadata, LegacyParentMetadataV1)
        key = coordinator_action_key(
            workflow_version=1,
            instance_key=self.manifest.instance.key,
            parent_identifier="PRO-200",
            stage_kind="complete",
            stage_ordinal=state.metadata.stage_ordinal,
            attempt=state.metadata.attempt,
            affected_repositories=frozenset(snapshot.affected_repositories),
            candidate_shas=snapshot.candidate_shas,
            contract_hashes=state.metadata.contract_hashes,
        )
        self.store.states["PRO-200"] = replace(
            state,
            metadata=replace(state.metadata, last_action=key),
            applied_action_keys=state.applied_action_keys | {key},
        )
        self.store.events.clear()

        result = self.workflow.resume_parent("PRO-200")

        self.assertEqual(result.next_action, "noop")
        self.assertEqual(result.mutation_count, 0)
        self.assertEqual(self.store.events, [("read-parent", "PRO-200")])


class WorkflowTaskFourFixRoundOneTests(TaskFourWorkflowFixture, unittest.TestCase):
    def review_failure_source_stage(
        self,
        *,
        source: dict[str, str],
        source_stage: int,
        source_attempt: int,
        repair_round: int,
    ) -> tuple[tuple[WorkflowChild, ...], FailureBundle, frozenset[str]]:
        gates: list[WorkflowChild] = []
        failures: list[FailureEvidenceRef] = []
        creation_action = coordinator_action_key(
            workflow_version=2,
            instance_key=self.manifest.instance.key,
            parent_identifier="PRO-200",
            stage_kind="gates",
            stage_ordinal=source_stage,
            attempt=source_attempt,
            affected_repositories=frozenset(source),
            candidate_shas=source,
            contract_hashes={},
        )
        index = 0
        for repository in source:
            for phase_name in ("review", "qa"):
                index += 1
                failed = phase_name == "review"
                comment_uuid = evidence_uuid(
                    f"repair-source-{repair_round}-{phase_name}-{repository}"
                )
                child = WorkflowChild(
                    f"PRO-200-SOURCE-{index}",
                    repository,
                    repository,
                    "",
                    phase_name,
                    source_stage,
                    source_attempt,
                    "done",
                    creation_action,
                    False,
                    evidence_comment_uuid=comment_uuid,
                    creation_candidate_shas=source,
                    phase_result="fail" if failed else "pass",
                    evidence_comment_url=f"https://example.test/evidence/{comment_uuid}",
                    responsible_repositories=(repository,) if failed else (),
                )
                gates.append(child)
                if failed:
                    failures.append(
                        FailureEvidenceRef(
                            child_identifier=child.identifier,
                            phase=phase_name,
                            result="fail",
                            stage_ordinal=source_stage,
                            repair_round=source_attempt,
                            candidate_shas=source,
                            responsible_repositories=(repository,),
                            evidence_comment_uuid=comment_uuid,
                            evidence_comment_url=child.evidence_comment_url,
                        )
                    )
        for suite in self.manifest.integration_suites:
            if not set(suite.repositories) <= set(source):
                continue
            index += 1
            comment_uuid = evidence_uuid(
                f"repair-source-{repair_round}-integration-{suite.key}"
            )
            gates.append(
                WorkflowChild(
                    f"PRO-200-SOURCE-{index}",
                    suite.key,
                    suite.command_repository,
                    suite.key,
                    "integration_qa",
                    source_stage,
                    source_attempt,
                    "done",
                    creation_action,
                    False,
                    evidence_comment_uuid=comment_uuid,
                    creation_candidate_shas=source,
                    phase_result="pass",
                    evidence_comment_url=f"https://example.test/evidence/{comment_uuid}",
                )
            )
        completion_actions = frozenset(
            coordinator_action_key(
                workflow_version=2,
                instance_key=self.manifest.instance.key,
                parent_identifier="PRO-200",
                stage_kind=f"{child.phase}:{child.target_key}",
                stage_ordinal=source_stage,
                attempt=source_attempt,
                affected_repositories=frozenset(source),
                candidate_shas=source,
                contract_hashes={},
            )
            for child in gates
        )
        return (
            tuple(gates),
            FailureBundle.build(
                "PRO-200",
                2,
                source_stage,
                repair_round,
                source,
                tuple(failures),
            ),
            completion_actions | {creation_action},
        )

    @staticmethod
    def rebuild_source_gate_bundle(
        *,
        gates: tuple[WorkflowChild, ...],
        source: dict[str, str],
        source_stage: int,
        repair_round: int,
    ) -> FailureBundle:
        return FailureBundle.build(
            "PRO-200",
            2,
            source_stage,
            repair_round,
            source,
            tuple(
                FailureEvidenceRef(
                    child_identifier=child.identifier,
                    phase=child.phase,
                    result=child.phase_result,
                    stage_ordinal=source_stage,
                    repair_round=child.attempt,
                    candidate_shas=source,
                    responsible_repositories=child.responsible_repositories,
                    evidence_comment_uuid=child.evidence_comment_uuid,
                    evidence_comment_url=child.evidence_comment_url,
                    suite_key=child.suite_key,
                )
                for child in gates
                if child.phase_result != "pass"
            ),
        )

    def seed_active_parallel_repair(
        self,
        repair_round: int,
    ) -> PhaseCompletion:
        self.store.parent_read_failures_remaining = 0
        self.store.parent_drift_after_completion_read = None
        self.store.read_state_override_by_count.clear()
        source = {"api": SHA["api"], "web": SHA["web"]}
        replacements = {"api": REPLACEMENT_SHA, "web": OTHER_SHA}
        stage_ordinal = 5 + repair_round
        source_gates, bundle, source_actions = self.review_failure_source_stage(
            source=source,
            source_stage=stage_ordinal - 1,
            source_attempt=repair_round - 1,
            repair_round=repair_round,
        )
        authorization_uuid = (
            evidence_uuid(f"record-toctou-auth-{repair_round}")
            if repair_round == 3
            else ""
        )
        action_key = coordinator_action_key(
            workflow_version=2,
            instance_key=self.manifest.instance.key,
            parent_identifier="PRO-200",
            stage_kind="repair",
            stage_ordinal=stage_ordinal,
            attempt=repair_round,
            affected_repositories=frozenset(source),
            candidate_shas=source,
            contract_hashes={},
            failure_bundle_digest=bundle.digest,
            authorizing_comment_uuid=authorization_uuid,
        )
        repair_children = tuple(
            WorkflowChild(
                f"PRO-200-{repository.upper()}-REPAIR",
                repository,
                repository,
                "",
                "repair",
                stage_ordinal,
                repair_round,
                "in_progress",
                action_key,
                True,
                creation_candidate_shas=source,
                failure_bundle_digest=bundle.digest,
                failure_evidence_uuids=tuple(
                    failure.evidence_comment_uuid
                    for failure in bundle.for_repository(repository)
                ),
                authorizing_comment_uuid=authorization_uuid,
            )
            for repository in ("api", "web")
        )
        snapshot = ParentSnapshot(
            affected_repositories=("api", "web"),
            candidate_shas=source,
            children={
                repository: RepositoryEvidence(source[repository], "pending")
                for repository in source
            },
            pull_requests={
                repository: PullRequestEvidence(
                    replacements[repository], "open", True, True
                )
                for repository in source
            },
            attempt=repair_round,
        )
        self.store.add_state(
            "PRO-200",
            snapshot,
            children=(*source_gates, *repair_children),
            pull_requests=pull_request_targets(),
            stage_ordinal=stage_ordinal,
        )
        state = self.store.states["PRO-200"]
        assert isinstance(state.metadata, ParentMetadata)
        self.store.states["PRO-200"] = replace(
            state,
            metadata=replace(state.metadata, last_action=action_key),
            applied_action_keys=state.applied_action_keys | source_actions,
        )
        self.store.events.clear()
        return completion_for(
            "api",
            parent="PRO-200",
            phase="repair",
            attempt=repair_round,
            sha=REPLACEMENT_SHA,
            failure_bundle_digest=bundle.digest,
        )

    def test_repair_completion_revalidates_parent_before_first_evidence_write(self):
        for repair_round in (1, 2, 3):
            for drift in (
                "metadata",
                "actions",
                "children",
                "snapshot",
                "non-state",
                "read-error",
            ):
                with self.subTest(repair_round=repair_round, drift=drift):
                    self.store.states.clear()
                    self.store.completions.clear()
                    completion = self.seed_active_parallel_repair(repair_round)
                    self.store.parent_drift_after_completion_read = drift

                    result = self.workflow.record_phase_completion(completion)

                    self.assertEqual(result.next_action, "block", result.reason)
                    self.assertEqual(result.mutation_count, 0)
                    self.assertFalse(
                        any(
                            event[0] in {"write-completion", "done"}
                            for event in self.store.events
                        )
                    )

    def test_exact_repair_replay_revalidates_parent_after_completion_reads(self):
        for repair_round in (1, 2, 3):
            for drift in (
                "metadata",
                "actions",
                "children",
                "snapshot",
                "non-state",
                "read-error",
            ):
                with self.subTest(repair_round=repair_round, drift=drift):
                    self.store.states.clear()
                    self.store.completions.clear()
                    completion = self.seed_active_parallel_repair(repair_round)
                    completed = self.workflow.record_phase_completion(completion)
                    self.assertEqual(completed.completed_child_status, "done")
                    self.store.events.clear()
                    self.store.parent_drift_after_completion_read = drift

                    replay = self.workflow.record_phase_completion(completion)

                    self.assertEqual(replay.next_action, "block", replay.reason)
                    self.assertEqual(replay.mutation_count, 0)
                    self.assertFalse(
                        any(
                            event[0] in {"write-completion", "done"}
                            for event in self.store.events
                        )
                    )

    def test_stable_repair_completion_and_exact_replay_remain_idempotent(self):
        for repair_round in (1, 2, 3):
            with self.subTest(repair_round=repair_round):
                self.store.states.clear()
                self.store.completions.clear()
                completion = self.seed_active_parallel_repair(repair_round)

                completed = self.workflow.record_phase_completion(completion)
                completion_events = tuple(
                    event[0]
                    for event in self.store.events
                    if event[0] in {"write-completion", "done"}
                )
                self.store.events.clear()
                replay = self.workflow.record_phase_completion(completion)

                self.assertEqual(completed.completed_child_status, "done")
                self.assertEqual(completion_events, ("write-completion", "done"))
                self.assertEqual(replay.next_action, "noop")
                self.assertEqual(replay.mutation_count, 0)
                self.assertFalse(
                    any(
                        event[0] in {"write-completion", "done"}
                        for event in self.store.events
                    )
                )

    def test_current_repair_requires_authoritative_source_gate_actions(self):
        source = {"api": SHA["api"], "web": SHA["web"]}
        for repair_round in (1, 2, 3):
            for corruption in (
                "child-action",
                "missing-creation",
                "missing-completion",
                "wrong-completion-digest",
            ):
                with self.subTest(
                    repair_round=repair_round,
                    corruption=corruption,
                ):
                    repair_stage = 5 + repair_round
                    gates, bundle, source_actions = self.review_failure_source_stage(
                        source=source,
                        source_stage=repair_stage - 1,
                        source_attempt=repair_round - 1,
                        repair_round=repair_round,
                    )
                    creation_action = gates[0].action_key
                    first_gate = gates[0]
                    first_completion_action = coordinator_action_key(
                        workflow_version=2,
                        instance_key=self.manifest.instance.key,
                        parent_identifier="PRO-200",
                        stage_kind=f"{first_gate.phase}:{first_gate.target_key}",
                        stage_ordinal=first_gate.stage_ordinal,
                        attempt=first_gate.attempt,
                        affected_repositories=frozenset(source),
                        candidate_shas=source,
                        contract_hashes={},
                    )
                    repair_authorization = (
                        evidence_uuid(f"source-action-auth-{repair_round}")
                        if repair_round == 3
                        else ""
                    )
                    repair_action = coordinator_action_key(
                        workflow_version=2,
                        instance_key=self.manifest.instance.key,
                        parent_identifier="PRO-200",
                        stage_kind="repair",
                        stage_ordinal=repair_stage,
                        attempt=repair_round,
                        affected_repositories=frozenset(source),
                        candidate_shas=source,
                        contract_hashes={},
                        failure_bundle_digest=bundle.digest,
                        authorizing_comment_uuid=repair_authorization,
                    )
                    if corruption == "child-action":
                        gates = (
                            replace(first_gate, action_key="stage:" + "9" * 64),
                            *gates[1:],
                        )
                    repair_children = tuple(
                        WorkflowChild(
                            f"PRO-200-{repository.upper()}-REPAIR",
                            repository,
                            repository,
                            "",
                            "repair",
                            repair_stage,
                            repair_round,
                            "in_progress",
                            repair_action,
                            True,
                            creation_candidate_shas=source,
                            failure_bundle_digest=bundle.digest,
                            failure_evidence_uuids=tuple(
                                failure.evidence_comment_uuid
                                for failure in bundle.for_repository(repository)
                            ),
                            authorizing_comment_uuid=repair_authorization,
                        )
                        for repository in ("api", "web")
                    )
                    snapshot = ParentSnapshot(
                        affected_repositories=("api", "web"),
                        candidate_shas=source,
                        children={
                            repository: RepositoryEvidence(source[repository], "pending")
                            for repository in source
                        },
                        pull_requests={
                            repository: PullRequestEvidence(
                                source[repository], "open", True, True
                            )
                            for repository in source
                        },
                        attempt=repair_round,
                    )
                    self.store.add_state(
                        "PRO-200",
                        snapshot,
                        children=(*gates, *repair_children),
                        pull_requests=pull_request_targets(),
                        stage_ordinal=repair_stage,
                    )
                    state = self.store.states["PRO-200"]
                    assert isinstance(state.metadata, ParentMetadata)
                    applied = state.applied_action_keys | source_actions
                    if corruption == "missing-creation":
                        applied -= {creation_action}
                    elif corruption == "missing-completion":
                        applied -= {first_completion_action}
                    elif corruption == "wrong-completion-digest":
                        applied -= {first_completion_action}
                        applied |= {
                            coordinator_action_key(
                                workflow_version=2,
                                instance_key=self.manifest.instance.key,
                                parent_identifier="PRO-200",
                                stage_kind=f"{first_gate.phase}:{first_gate.target_key}",
                                stage_ordinal=first_gate.stage_ordinal,
                                attempt=first_gate.attempt,
                                affected_repositories=frozenset(source),
                                candidate_shas={**source, "api": OTHER_SHA},
                                contract_hashes={},
                            )
                        }
                    self.store.states["PRO-200"] = replace(
                        state,
                        metadata=replace(state.metadata, last_action=repair_action),
                        applied_action_keys=applied,
                    )
                    self.store.events.clear()

                    result = self.workflow.resume_parent("PRO-200")

                    self.assertEqual(result.next_action, "block", result.reason)
                    self.assertEqual(result.mutation_count, 0)
                    self.assertFalse(
                        any(event[0] == "create" for event in self.store.events)
                    )

    def test_current_repair_requires_exact_authoritative_source_gate_completions(self):
        source = {"api": SHA["api"], "web": SHA["web"]}

        def repair_wave(
            *,
            gates: tuple[WorkflowChild, ...],
            bundle: FailureBundle,
            repair_round: int,
            terminal: bool,
        ) -> tuple[tuple[WorkflowChild, ...], str, frozenset[str], str]:
            repair_stage = 5 + repair_round
            authorization_uuid = (
                evidence_uuid(f"authoritative-source-auth-{repair_round}")
                if repair_round == 3
                else ""
            )
            action = coordinator_action_key(
                workflow_version=2,
                instance_key=self.manifest.instance.key,
                parent_identifier="PRO-200",
                stage_kind="repair",
                stage_ordinal=repair_stage,
                attempt=repair_round,
                affected_repositories=frozenset(source),
                candidate_shas=source,
                contract_hashes={},
                failure_bundle_digest=bundle.digest,
                authorizing_comment_uuid=authorization_uuid,
            )
            owners = tuple(
                sorted(
                    {
                        repository
                        for failure in bundle.failures
                        for repository in failure.responsible_repositories
                    }
                )
            )
            children: list[WorkflowChild] = []
            completion_actions: set[str] = set()
            last_action = action
            replacements = {"api": REPLACEMENT_SHA, "web": OTHER_SHA}
            for repository in owners:
                completion_uuid = evidence_uuid(
                    f"authoritative-source-repair-{repair_round}-{repository}"
                )
                children.append(
                    WorkflowChild(
                        f"PRO-200-{repository.upper()}-REPAIR",
                        repository,
                        repository,
                        "",
                        "repair",
                        repair_stage,
                        repair_round,
                        "done" if terminal else "in_progress",
                        action,
                        not terminal,
                        evidence_comment_uuid=completion_uuid if terminal else "",
                        creation_candidate_shas=source,
                        phase_result="pass" if terminal else "",
                        evidence_comment_url=(
                            f"https://example.test/evidence/{completion_uuid}"
                            if terminal
                            else ""
                        ),
                        failure_bundle_digest=bundle.digest,
                        failure_evidence_uuids=tuple(
                            failure.evidence_comment_uuid
                            for failure in bundle.for_repository(repository)
                        ),
                        authorizing_comment_uuid=authorization_uuid,
                    )
                )
                if terminal:
                    for action_candidates in (
                        {**source, repository: replacements[repository]},
                        replacements,
                    ):
                        last_action = coordinator_action_key(
                            workflow_version=2,
                            instance_key=self.manifest.instance.key,
                            parent_identifier="PRO-200",
                            stage_kind=f"repair:{repository}",
                            stage_ordinal=repair_stage,
                            attempt=repair_round,
                            affected_repositories=frozenset(source),
                            candidate_shas=action_candidates,
                            contract_hashes={},
                            failure_bundle_digest=bundle.digest,
                            authorizing_comment_uuid=authorization_uuid,
                        )
                        completion_actions.add(last_action)
            return tuple(children), action, frozenset(completion_actions), last_action

        for repair_round in (1, 2, 3):
            for terminal in (False, True):
                for corruption in (
                    "missing-record",
                    "evidence",
                    "result",
                    "output",
                    "responsibility",
                    "read-drift",
                    "parent-metadata",
                    "parent-actions",
                    "parent-children",
                ):
                    with self.subTest(
                        repair_round=repair_round,
                        terminal=terminal,
                        corruption=corruption,
                    ):
                        repair_stage = 5 + repair_round
                        gates, bundle, source_actions = self.review_failure_source_stage(
                            source=source,
                            source_stage=repair_stage - 1,
                            source_attempt=repair_round - 1,
                            repair_round=repair_round,
                        )
                        if corruption == "responsibility":
                            gates = tuple(
                                replace(
                                    child,
                                    phase_result="fail",
                                    responsible_repositories=("api",),
                                )
                                if child.phase == "integration_qa"
                                else child
                                for child in gates
                            )
                            bundle = self.rebuild_source_gate_bundle(
                                gates=gates,
                                source=source,
                                source_stage=repair_stage - 1,
                                repair_round=repair_round,
                            )
                        repair_children, repair_action, completion_actions, last_action = (
                            repair_wave(
                                gates=gates,
                                bundle=bundle,
                                repair_round=repair_round,
                                terminal=terminal,
                            )
                        )
                        current_candidates = (
                            {"api": REPLACEMENT_SHA, "web": OTHER_SHA}
                            if terminal
                            else source
                        )
                        snapshot = ParentSnapshot(
                            affected_repositories=("api", "web"),
                            candidate_shas=current_candidates,
                            children={
                                repository: RepositoryEvidence(
                                    current_candidates[repository],
                                    "pass" if terminal else "pending",
                                )
                                for repository in source
                            },
                            pull_requests={
                                repository: PullRequestEvidence(
                                    current_candidates[repository],
                                    "open",
                                    True,
                                    True,
                                )
                                for repository in source
                            },
                            attempt=repair_round,
                        )
                        self.store.add_state(
                            "PRO-200",
                            snapshot,
                            children=(*gates, *repair_children),
                            pull_requests=pull_request_targets(),
                            stage_ordinal=repair_stage,
                        )
                        state = self.store.states["PRO-200"]
                        assert isinstance(state.metadata, ParentMetadata)
                        self.store.states["PRO-200"] = replace(
                            state,
                            metadata=replace(state.metadata, last_action=last_action),
                            applied_action_keys=(
                                state.applied_action_keys
                                | source_actions
                                | completion_actions
                            ),
                        )

                        forged_gates = gates
                        first_gate = gates[0]
                        if corruption == "missing-record":
                            del self.store.completions[
                                ("PRO-200", first_gate.evidence_comment_uuid)
                            ]
                        elif corruption == "evidence":
                            forged_uuid = evidence_uuid(
                                f"forged-source-evidence-{repair_round}-{terminal}"
                            )
                            forged_gates = (
                                replace(
                                    first_gate,
                                    evidence_comment_uuid=forged_uuid,
                                    evidence_comment_url=(
                                        f"https://example.test/evidence/{forged_uuid}"
                                    ),
                                ),
                                *gates[1:],
                            )
                        elif corruption == "result":
                            forged_gates = tuple(
                                replace(
                                    child,
                                    phase_result="fail",
                                    responsible_repositories=(child.repository_key,),
                                )
                                if child.phase == "qa" and child.repository_key == "api"
                                else child
                                for child in gates
                            )
                        elif corruption == "output":
                            key = ("PRO-200", first_gate.evidence_comment_uuid)
                            self.store.completions[key] = replace(
                                self.store.completions[key],
                                candidate_sha=OTHER_SHA,
                            )
                        elif corruption == "read-drift":
                            self.store.change_completion_after_first_read = True
                        elif corruption.startswith("parent-"):
                            self.store.parent_drift_after_completion_read = (
                                corruption.removeprefix("parent-")
                            )
                        else:
                            forged_gates = tuple(
                                replace(child, responsible_repositories=("web",))
                                if child.phase == "integration_qa"
                                else child
                                for child in gates
                            )

                        if forged_gates != gates:
                            forged_bundle = self.rebuild_source_gate_bundle(
                                gates=forged_gates,
                                source=source,
                                source_stage=repair_stage - 1,
                                repair_round=repair_round,
                            )
                            (
                                forged_repairs,
                                forged_action,
                                forged_completion_actions,
                                forged_last_action,
                            ) = repair_wave(
                                gates=forged_gates,
                                bundle=forged_bundle,
                                repair_round=repair_round,
                                terminal=terminal,
                            )
                            state = self.store.states["PRO-200"]
                            assert isinstance(state.metadata, ParentMetadata)
                            self.store.states["PRO-200"] = replace(
                                state,
                                children=(*forged_gates, *forged_repairs),
                                metadata=replace(
                                    state.metadata,
                                    last_action=forged_last_action,
                                ),
                                applied_action_keys=(
                                    state.applied_action_keys
                                    | {forged_action}
                                    | forged_completion_actions
                                ),
                            )
                        self.store.events.clear()

                        result = self.workflow.resume_parent("PRO-200")

                        self.assertEqual(result.next_action, "block", result.reason)
                        self.assertEqual(result.mutation_count, 0)
                        self.assertFalse(
                            any(event[0] == "create" for event in self.store.events)
                        )

    def add_three_repository_implementation_wave(
        self,
        *,
        include_completed_sibling_action: bool,
    ) -> None:
        base_candidates: dict[str, str] = {}
        creation_action = coordinator_action_key(
            workflow_version=2,
            instance_key=self.manifest.instance.key,
            parent_identifier="PRO-200",
            stage_kind="implementation",
            stage_ordinal=1,
            attempt=0,
            affected_repositories=frozenset({"api", "web", "notifications"}),
            candidate_shas=base_candidates,
            contract_hashes={},
        )
        api_completion_action = coordinator_action_key(
            workflow_version=2,
            instance_key=self.manifest.instance.key,
            parent_identifier="PRO-200",
            stage_kind="implementation:api",
            stage_ordinal=1,
            attempt=0,
            affected_repositories=frozenset({"api", "web", "notifications"}),
            candidate_shas={"api": SHA["api"]},
            contract_hashes={},
        )
        wrong_api_completion_action = coordinator_action_key(
            workflow_version=2,
            instance_key=self.manifest.instance.key,
            parent_identifier="PRO-200",
            stage_kind="implementation:api",
            stage_ordinal=1,
            attempt=0,
            affected_repositories=frozenset({"api", "web", "notifications"}),
            candidate_shas={"api": OTHER_SHA},
            contract_hashes={},
        )
        api_evidence = evidence_uuid("three-repository-api-completion")
        children = (
            WorkflowChild(
                "PRO-200-API", "api", "api", "", "implementation", 1, 0,
                "done", creation_action, False, evidence_comment_uuid=api_evidence,
                creation_candidate_shas=base_candidates, phase_result="pass",
                evidence_comment_url=f"https://example.test/evidence/{api_evidence}",
            ),
            WorkflowChild(
                "PRO-200-WEB", "web", "web", "", "implementation", 1, 0,
                "in_progress", creation_action, True,
                creation_candidate_shas=base_candidates,
            ),
            WorkflowChild(
                "PRO-200-NOTIFICATIONS", "notifications", "notifications", "",
                "implementation", 1, 0, "in_progress", creation_action, True,
                creation_candidate_shas=base_candidates,
            ),
        )
        snapshot = ParentSnapshot(
            affected_repositories=("api", "web", "notifications"),
            candidate_shas={"api": SHA["api"]},
            children={
                "api": RepositoryEvidence(SHA["api"], "pass"),
                "web": RepositoryEvidence("", "pending"),
                "notifications": RepositoryEvidence("", "pending"),
            },
            pull_requests={"api": PullRequestEvidence(SHA["api"], "open", True, True)},
        )
        self.store.add_state("PRO-200", snapshot, children=children, stage_ordinal=1)
        state = self.store.states["PRO-200"]
        self.store.states["PRO-200"] = replace(
            state,
            applied_action_keys=state.applied_action_keys
            | {
                api_completion_action
                if include_completed_sibling_action
                else wrong_api_completion_action
            },
        )

    def test_current_gate_completion_requires_exact_creation_provenance(self):
        cases = (
            ("review", "api", "", {"reviews": {"api": RepositoryEvidence(SHA["api"], "pending")}}),
            ("qa", "api", "", {"qa": {"api": RepositoryEvidence(SHA["api"], "pending")}}),
            (
                "integration_qa",
                "web-api",
                "web-api",
                {"integration_qa": {"web-api": GateEvidence(passing_snapshot().candidate_shas, "pending")}},
            ),
        )
        for phase, target, suite, evidence in cases:
            for corruption in ("action", "candidate-map"):
                with self.subTest(phase=phase, corruption=corruption):
                    snapshot = replace(passing_snapshot(), **evidence)
                    repository = "web" if phase == "integration_qa" else "api"
                    creation_candidates = (
                        dict(snapshot.candidate_shas)
                        if corruption == "action"
                        else {**snapshot.candidate_shas, "api": OTHER_SHA}
                    )
                    action_key = (
                        "stage:" + "9" * 64
                        if corruption == "action"
                        else coordinator_action_key(
                            workflow_version=2,
                            instance_key=self.manifest.instance.key,
                            parent_identifier="PRO-200",
                            stage_kind="gates",
                            stage_ordinal=5,
                            attempt=0,
                            affected_repositories=frozenset(
                                snapshot.affected_repositories
                            ),
                            candidate_shas=creation_candidates,
                            contract_hashes={},
                        )
                    )
                    child = WorkflowChild(
                        f"PRO-200-{phase}", target, repository, suite, phase, 5, 0,
                        "in_progress", action_key, True,
                        creation_candidate_shas=creation_candidates,
                    )
                    self.store.add_state(
                        "PRO-200", snapshot, children=(child,),
                        pull_requests=pull_request_targets(),
                    )
                    self.store.events.clear()
                    completion = completion_for(
                        repository,
                        parent="PRO-200",
                        phase=phase,
                        suite_key=suite,
                        candidate_shas=(dict(snapshot.candidate_shas) if suite else None),
                    )

                    result = self.workflow.record_phase_completion(completion)

                    self.assertEqual(result.next_action, "block")
                    self.assertEqual(result.mutation_count, 0)
                    self.assertFalse(
                        any(event[0] == "write-completion" for event in self.store.events)
                    )

    def test_failed_repair_cannot_adopt_a_changed_pull_request_head(self):
        bundle = self.dispatch_authorized_repair()
        state = self.store.states["PRO-200"]
        self.store.states["PRO-200"] = replace(
            state,
            snapshot=replace(
                state.snapshot,
                pull_requests={
                    "api": PullRequestEvidence(REPLACEMENT_SHA, "open", True, True)
                },
            ),
        )
        self.store.events.clear()

        result = self.workflow.record_phase_completion(
            completion_for(
                "api", parent="PRO-200", phase="repair", result="fail",
                attempt=3, sha=REPLACEMENT_SHA,
                failure_bundle_digest=bundle.digest,
            )
        )

        self.assertEqual(result.next_action, "block")
        self.assertEqual(result.mutation_count, 0)
        self.assertEqual(self.store.candidate_sha("api"), SHA["api"])
        self.assertFalse(any(event[0] == "write-completion" for event in self.store.events))

        bundle = self.dispatch_authorized_repair()
        self.store.events.clear()
        result = self.workflow.record_phase_completion(
            completion_for(
                "api", parent="PRO-200", phase="repair", result="fail",
                attempt=3, sha=REPLACEMENT_SHA,
                failure_bundle_digest=bundle.digest,
            )
        )

        self.assertEqual(result.next_action, "block")
        self.assertEqual(result.mutation_count, 0)
        self.assertEqual(self.store.candidate_sha("api"), SHA["api"])
        self.assertFalse(any(event[0] == "write-completion" for event in self.store.events))

    def test_three_repository_implementation_wave_accepts_completed_sibling_increment(self):
        self.add_three_repository_implementation_wave(
            include_completed_sibling_action=True,
        )

        result = self.workflow.record_phase_completion(
            completion_for("notifications", parent="PRO-200")
        )

        self.assertEqual(result.next_action, "wait")
        self.assertEqual(result.completed_child_status, "done")
        self.assertEqual(self.store.candidate_sha("notifications"), SHA["notifications"])

    def test_done_pass_sibling_without_exact_completion_action_cannot_explain_increment(self):
        self.add_three_repository_implementation_wave(
            include_completed_sibling_action=False,
        )
        self.store.events.clear()

        result = self.workflow.record_phase_completion(
            completion_for("notifications", parent="PRO-200")
        )

        self.assertEqual(result.next_action, "block")
        self.assertEqual(result.mutation_count, 0)
        self.assertFalse(any(event[0] == "write-completion" for event in self.store.events))

    def test_multi_repository_repair_wave_accepts_completed_sibling_increment(self):
        base = {"api": SHA["api"], "web": SHA["web"]}
        source_gates, bundle, source_actions = self.review_failure_source_stage(
            source=base,
            source_stage=5,
            source_attempt=0,
            repair_round=1,
        )
        bundle_digest = bundle.digest
        action_key = coordinator_action_key(
            workflow_version=2,
            instance_key=self.manifest.instance.key,
            parent_identifier="PRO-200",
            stage_kind="repair",
            stage_ordinal=6,
            attempt=1,
            affected_repositories=frozenset(base),
            candidate_shas=base,
            contract_hashes={},
            failure_bundle_digest=bundle_digest,
        )
        api_evidence = evidence_uuid("multi-repair-api-completion")
        children = (
            *source_gates,
            WorkflowChild(
                "PRO-200-API-REPAIR", "api", "api", "", "repair", 6, 1,
                "done", action_key, False, evidence_comment_uuid=api_evidence,
                creation_candidate_shas=base, phase_result="pass",
                evidence_comment_url=f"https://example.test/evidence/{api_evidence}",
                failure_bundle_digest=bundle_digest,
                failure_evidence_uuids=tuple(
                    failure.evidence_comment_uuid
                    for failure in bundle.for_repository("api")
                ),
            ),
            WorkflowChild(
                "PRO-200-WEB-REPAIR", "web", "web", "", "repair", 6, 1,
                "in_progress", action_key, True, creation_candidate_shas=base,
                failure_bundle_digest=bundle_digest,
                failure_evidence_uuids=tuple(
                    failure.evidence_comment_uuid
                    for failure in bundle.for_repository("web")
                ),
            ),
        )
        snapshot = ParentSnapshot(
            affected_repositories=("api", "web"),
            candidate_shas={"api": REPLACEMENT_SHA, "web": SHA["web"]},
            children={
                "api": RepositoryEvidence(REPLACEMENT_SHA, "pass"),
                "web": RepositoryEvidence(SHA["web"], "pending"),
            },
            pull_requests={
                "api": PullRequestEvidence(REPLACEMENT_SHA, "open", True, True),
                "web": PullRequestEvidence(OTHER_SHA, "open", True, True),
            },
            attempt=1,
        )
        self.store.add_state(
            "PRO-200", snapshot, children=children,
            pull_requests=pull_request_targets(), stage_ordinal=6,
        )
        seeded = self.store.states["PRO-200"]
        assert isinstance(seeded.metadata, ParentMetadata)
        self.store.states["PRO-200"] = replace(
            seeded,
            metadata=replace(seeded.metadata, last_action=action_key),
        )
        api_completion_action = coordinator_action_key(
            workflow_version=2,
            instance_key=self.manifest.instance.key,
            parent_identifier="PRO-200",
            stage_kind="repair:api",
            stage_ordinal=6,
            attempt=1,
            affected_repositories=frozenset(base),
            candidate_shas={"api": REPLACEMENT_SHA, "web": SHA["web"]},
            contract_hashes={},
            failure_bundle_digest=bundle_digest,
        )
        state = self.store.states["PRO-200"]
        self.store.states["PRO-200"] = replace(
            state,
            applied_action_keys=state.applied_action_keys
            | source_actions
            | {api_completion_action},
        )

        result = self.workflow.record_phase_completion(
            completion_for(
                "web", parent="PRO-200", phase="repair", attempt=1,
                sha=OTHER_SHA, failure_bundle_digest=bundle_digest,
            )
        )

        self.assertEqual(result.completed_child_status, "done")
        self.assertEqual(result.mutation_count, 2)
        self.assertEqual(self.store.candidate_sha("web"), OTHER_SHA)

    def test_parallel_repair_wave_allows_first_owner_while_active_sibling_head_moves(self):
        source = {"api": SHA["api"], "web": SHA["web"]}
        replacements = {"api": REPLACEMENT_SHA, "web": OTHER_SHA}
        for repair_round in (1, 2, 3):
            with self.subTest(repair_round=repair_round):
                stage_ordinal = 5 + repair_round
                source_gates, bundle, source_actions = self.review_failure_source_stage(
                    source=source,
                    source_stage=stage_ordinal - 1,
                    source_attempt=repair_round - 1,
                    repair_round=repair_round,
                )
                bundle_digest = bundle.digest
                authorization_uuid = (
                    evidence_uuid(f"parallel-repair-auth-{repair_round}")
                    if repair_round == 3
                    else ""
                )
                action_key = coordinator_action_key(
                    workflow_version=2,
                    instance_key=self.manifest.instance.key,
                    parent_identifier="PRO-200",
                    stage_kind="repair",
                    stage_ordinal=stage_ordinal,
                    attempt=repair_round,
                    affected_repositories=frozenset(source),
                    candidate_shas=source,
                    contract_hashes={},
                    failure_bundle_digest=bundle_digest,
                    authorizing_comment_uuid=authorization_uuid,
                )
                repair_children = tuple(
                    WorkflowChild(
                        f"PRO-200-{repository.upper()}-REPAIR",
                        repository,
                        repository,
                        "",
                        "repair",
                        stage_ordinal,
                        repair_round,
                        "in_progress",
                        action_key,
                        True,
                        creation_candidate_shas=source,
                        failure_bundle_digest=bundle_digest,
                        failure_evidence_uuids=tuple(
                            failure.evidence_comment_uuid
                            for failure in bundle.for_repository(repository)
                        ),
                        authorizing_comment_uuid=authorization_uuid,
                    )
                    for repository in ("api", "web")
                )
                children = (*source_gates, *repair_children)
                snapshot = ParentSnapshot(
                    affected_repositories=("api", "web"),
                    candidate_shas=source,
                    children={
                        repository: RepositoryEvidence(source[repository], "pending")
                        for repository in source
                    },
                    pull_requests={
                        repository: PullRequestEvidence(
                            replacements[repository], "open", True, True
                        )
                        for repository in source
                    },
                    attempt=repair_round,
                )
                self.store.add_state(
                    "PRO-200",
                    snapshot,
                    children=children,
                    pull_requests=pull_request_targets(),
                    stage_ordinal=stage_ordinal,
                )
                seeded = self.store.states["PRO-200"]
                assert isinstance(seeded.metadata, ParentMetadata)
                self.store.states["PRO-200"] = replace(
                    seeded,
                    metadata=replace(seeded.metadata, last_action=action_key),
                    applied_action_keys=seeded.applied_action_keys | source_actions,
                )

                resumed = self.workflow.resume_parent("PRO-200")
                self.store.events.clear()
                completed = self.workflow.record_phase_completion(
                    completion_for(
                        "api",
                        parent="PRO-200",
                        phase="repair",
                        attempt=repair_round,
                        sha=REPLACEMENT_SHA,
                        failure_bundle_digest=bundle_digest,
                    )
                )

                self.assertEqual(
                    (
                        resumed.next_action,
                        resumed.mutation_count,
                        completed.next_action,
                        completed.completed_child_status,
                        completed.mutation_count,
                    ),
                    (
                        "noop" if repair_round == 3 else "wait",
                        0,
                        "wait",
                        "done",
                        1,
                    ),
                )
                self.assertEqual(self.store.candidate_sha("api"), REPLACEMENT_SHA)
                self.assertEqual(self.store.candidate_sha("web"), SHA["web"])

    def test_current_repair_requires_exact_failure_bundle_owner_multiset(self):
        source = {"api": SHA["api"], "web": SHA["web"]}

        def seed(
            *,
            repair_round: int,
            expected_owners: tuple[str, ...],
            api_done: bool,
        ) -> None:
            repair_stage = 5 + repair_round
            source_stage = repair_stage - 1
            source_attempt = repair_round - 1
            authorization_uuid = (
                evidence_uuid(f"owner-multiset-auth-{repair_round}")
                if repair_round == 3
                else ""
            )
            gate_creation_action = coordinator_action_key(
                workflow_version=2,
                instance_key=self.manifest.instance.key,
                parent_identifier="PRO-200",
                stage_kind="gates",
                stage_ordinal=source_stage,
                attempt=source_attempt,
                affected_repositories=frozenset(source),
                candidate_shas=source,
                contract_hashes={},
            )
            gate_specs = (
                ("review", "api", "", "api" in expected_owners, ("api",)),
                ("qa", "api", "", False, ()),
                ("review", "web", "", "web" in expected_owners, ("web",)),
                ("qa", "web", "", False, ()),
                (
                    "integration_qa",
                    "web-api",
                    "web-api",
                    expected_owners == ("api",),
                    ("api",),
                ),
            )
            gates: list[WorkflowChild] = []
            failures: list[FailureEvidenceRef] = []
            for index, (phase_name, target, suite, failed, owners) in enumerate(
                gate_specs,
                start=1,
            ):
                result = "fail" if failed else "pass"
                repository = "web" if phase_name == "integration_qa" else target
                comment_uuid = evidence_uuid(
                    f"owner-multiset-{repair_round}-{phase_name}-{target}"
                )
                responsible = owners if failed else ()
                child = WorkflowChild(
                    f"PRO-200-SOURCE-{index}",
                    target,
                    repository,
                    suite,
                    phase_name,
                    source_stage,
                    source_attempt,
                    "done",
                    gate_creation_action,
                    False,
                    evidence_comment_uuid=comment_uuid,
                    creation_candidate_shas=source,
                    phase_result=result,
                    evidence_comment_url=f"https://example.test/evidence/{comment_uuid}",
                    responsible_repositories=responsible,
                )
                gates.append(child)
                if failed:
                    failures.append(
                        FailureEvidenceRef(
                            child_identifier=child.identifier,
                            phase=phase_name,
                            result=result,
                            stage_ordinal=source_stage,
                            repair_round=source_attempt,
                            candidate_shas=source,
                            responsible_repositories=responsible,
                            evidence_comment_uuid=comment_uuid,
                            evidence_comment_url=child.evidence_comment_url,
                            suite_key=suite,
                        )
                    )
            bundle = FailureBundle.build(
                "PRO-200",
                2,
                source_stage,
                repair_round,
                source,
                tuple(failures),
            )
            action_key = coordinator_action_key(
                workflow_version=2,
                instance_key=self.manifest.instance.key,
                parent_identifier="PRO-200",
                stage_kind="repair",
                stage_ordinal=repair_stage,
                attempt=repair_round,
                affected_repositories=frozenset(source),
                candidate_shas=source,
                contract_hashes={},
                failure_bundle_digest=bundle.digest,
                authorizing_comment_uuid=authorization_uuid,
            )
            api_evidence_uuid = evidence_uuid(
                f"owner-multiset-api-completion-{repair_round}"
            )
            api_child = WorkflowChild(
                "PRO-200-API-REPAIR",
                "api",
                "api",
                "",
                "repair",
                repair_stage,
                repair_round,
                "done" if api_done else "in_progress",
                action_key,
                not api_done,
                evidence_comment_uuid=api_evidence_uuid if api_done else "",
                creation_candidate_shas=source,
                phase_result="pass" if api_done else "",
                evidence_comment_url=(
                    f"https://example.test/evidence/{api_evidence_uuid}"
                    if api_done
                    else ""
                ),
                failure_bundle_digest=bundle.digest,
                failure_evidence_uuids=tuple(
                    failure.evidence_comment_uuid
                    for failure in bundle.for_repository("api")
                ),
                authorizing_comment_uuid=authorization_uuid,
            )
            candidates = {
                "api": REPLACEMENT_SHA if api_done else source["api"],
                "web": source["web"],
            }
            snapshot = ParentSnapshot(
                affected_repositories=("api", "web"),
                candidate_shas=candidates,
                children={
                    "api": RepositoryEvidence(
                        candidates["api"], "pass" if api_done else "pending"
                    ),
                    "web": RepositoryEvidence(source["web"], "pass"),
                },
                pull_requests={
                    "api": PullRequestEvidence(candidates["api"], "open", True, True),
                    "web": PullRequestEvidence(source["web"], "open", True, True),
                },
                reviews={} if api_done else {
                    "api": RepositoryEvidence(
                        source["api"],
                        "fail" if "api" in expected_owners else "pass",
                    ),
                    "web": RepositoryEvidence(
                        source["web"],
                        "fail" if "web" in expected_owners else "pass",
                    ),
                },
                qa={} if api_done else {
                    "api": RepositoryEvidence(source["api"], "pass"),
                    "web": RepositoryEvidence(source["web"], "pass"),
                },
                integration_qa={} if api_done else {
                    "web-api": GateEvidence(
                        source,
                        "fail" if expected_owners == ("api",) else "pass",
                        ("api",) if expected_owners == ("api",) else (),
                    ),
                },
                attempt=repair_round,
            )
            self.store.add_state(
                "PRO-200",
                snapshot,
                children=(*gates, api_child),
                pull_requests=pull_request_targets(),
                stage_ordinal=repair_stage,
            )
            state = self.store.states["PRO-200"]
            assert isinstance(state.metadata, ParentMetadata)
            gate_completion_actions = frozenset(
                coordinator_action_key(
                    workflow_version=2,
                    instance_key=self.manifest.instance.key,
                    parent_identifier="PRO-200",
                    stage_kind=f"{child.phase}:{child.target_key}",
                    stage_ordinal=source_stage,
                    attempt=source_attempt,
                    affected_repositories=frozenset(source),
                    candidate_shas=source,
                    contract_hashes={},
                )
                for child in gates
            )
            applied = (
                state.applied_action_keys
                | gate_completion_actions
                | {gate_creation_action}
            )
            last_action = action_key
            if api_done:
                completion_action = coordinator_action_key(
                    workflow_version=2,
                    instance_key=self.manifest.instance.key,
                    parent_identifier="PRO-200",
                    stage_kind="repair:api",
                    stage_ordinal=repair_stage,
                    attempt=repair_round,
                    affected_repositories=frozenset(source),
                    candidate_shas=candidates,
                    contract_hashes={},
                    failure_bundle_digest=bundle.digest,
                    authorizing_comment_uuid=authorization_uuid,
                )
                applied = applied | {completion_action}
                last_action = completion_action
            self.store.states["PRO-200"] = replace(
                state,
                metadata=replace(state.metadata, last_action=last_action),
                applied_action_keys=applied,
            )
            stored = self.store.states["PRO-200"]
            current = tuple(
                child
                for child in stored.children
                if child.stage_ordinal == repair_stage
                and child.attempt == repair_round
            )
            rebuilt = self.workflow._current_repair_failure_bundle(
                stored,
                current,
                source,
            )
            self.assertIsNotNone(rebuilt, f"round {repair_round} bundle rebuild")
            assert rebuilt is not None
            self.assertEqual(rebuilt.digest, bundle.digest)
            self.store.events.clear()

        for repair_round in (1, 2, 3):
            for api_done in (False, True):
                with self.subTest(
                    repair_round=repair_round,
                    api_done=api_done,
                    expected_owners="api+web",
                ):
                    seed(
                        repair_round=repair_round,
                        expected_owners=("api", "web"),
                        api_done=api_done,
                    )

                    result = self.workflow.resume_parent("PRO-200")

                    self.assertEqual(result.next_action, "block")
                    self.assertEqual(result.mutation_count, 0)
                    self.assertFalse(
                        any(event[0] == "create" for event in self.store.events)
                    )

            with self.subTest(
                repair_round=repair_round,
                expected_owners="integration subset api",
            ):
                seed(
                    repair_round=repair_round,
                    expected_owners=("api",),
                    api_done=False,
                )

                result = self.workflow.resume_parent("PRO-200")

                self.assertIn(
                    result.next_action,
                    {"wait", "noop"},
                    result.reason,
                )
                self.assertEqual(result.mutation_count, 0)

    def test_repair_head_validation_rejects_missing_owner_and_non_done_pass(self):
        source = {"api": SHA["api"], "web": SHA["web"]}
        bundle_digest = "f" * 64
        action_key = coordinator_action_key(
            workflow_version=2,
            instance_key=self.manifest.instance.key,
            parent_identifier="PRO-200",
            stage_kind="repair",
            stage_ordinal=6,
            attempt=1,
            affected_repositories=frozenset(source),
            candidate_shas=source,
            contract_hashes={},
            failure_bundle_digest=bundle_digest,
        )
        active_api = WorkflowChild(
            "PRO-200-API-REPAIR",
            "api",
            "api",
            "",
            "repair",
            6,
            1,
            "in_progress",
            action_key,
            True,
            creation_candidate_shas=source,
            failure_bundle_digest=bundle_digest,
            failure_evidence_uuids=(
                evidence_uuid("repair-boundary-api-failure"),
            ),
        )
        web_evidence_uuid = evidence_uuid("repair-boundary-web-completion")
        cancelled_web_pass = WorkflowChild(
            "PRO-200-WEB-REPAIR",
            "web",
            "web",
            "",
            "repair",
            6,
            1,
            "cancelled",
            action_key,
            False,
            creation_candidate_shas=source,
            phase_result="pass",
            evidence_comment_uuid=web_evidence_uuid,
            evidence_comment_url=(
                f"https://example.test/evidence/{web_evidence_uuid}"
            ),
            failure_bundle_digest=bundle_digest,
            failure_evidence_uuids=(
                evidence_uuid("repair-boundary-web-failure"),
            ),
        )
        cases = (
            (
                "missing repair owner",
                (active_api,),
                source,
                {
                    "api": RepositoryEvidence(SHA["api"], "pending"),
                    "web": RepositoryEvidence(SHA["web"], "pending"),
                },
            ),
            (
                "cancelled child claims pass",
                (active_api, cancelled_web_pass),
                {"api": SHA["api"], "web": OTHER_SHA},
                {
                    "api": RepositoryEvidence(SHA["api"], "pending"),
                    "web": RepositoryEvidence(OTHER_SHA, "pass"),
                },
            ),
        )
        for label, children, candidates, evidence in cases:
            with self.subTest(label=label):
                snapshot = ParentSnapshot(
                    affected_repositories=("api", "web"),
                    candidate_shas=candidates,
                    children=evidence,
                    pull_requests={
                        "api": PullRequestEvidence(SHA["api"], "open", True, True),
                        "web": PullRequestEvidence(
                            OTHER_SHA if label == "cancelled child claims pass" else SHA["web"],
                            "open",
                            True,
                            True,
                        ),
                    },
                    attempt=1,
                )
                self.store.add_state(
                    "PRO-200",
                    snapshot,
                    children=children,
                    pull_requests=pull_request_targets(),
                    stage_ordinal=6,
                )
                self.store.events.clear()

                result = self.workflow.resume_parent("PRO-200")

                self.assertEqual(result.next_action, "block")
                self.assertEqual(result.mutation_count, 0)

    def test_missing_managed_pull_request_evidence_blocks_every_mutation_boundary(self):
        dispatch_snapshot = replace(
            passing_snapshot(), reviews={}, qa={}, integration_qa={}, pull_requests={},
        )
        self.store.add_state(
            "PRO-200", dispatch_snapshot, pull_requests=pull_request_targets(),
        )
        result = self.workflow.resume_parent("PRO-200")
        self.assertEqual((result.next_action, result.mutation_count), ("block", 0))
        self.assertFalse(any(event[0] == "create" for event in self.store.events))

        self.add_failed_review_state(attempt=0)
        state = self.store.states["PRO-200"]
        self.store.states["PRO-200"] = replace(
            state, snapshot=replace(state.snapshot, pull_requests={}),
        )
        self.store.events.clear()
        result = self.workflow.resume_parent("PRO-200")
        self.assertEqual((result.next_action, result.mutation_count), ("block", 0))
        self.assertFalse(any(event[0] == "create" for event in self.store.events))

        merge_snapshot = replace(passing_snapshot(), pull_requests={})
        self.store.add_state(
            "PRO-200", merge_snapshot, pull_requests=pull_request_targets(),
        )
        self.store.events.clear()
        result = self.workflow.execute_merge_plan("PRO-200")
        self.assertEqual((result.next_action, result.mutation_count), ("block", 0))
        self.assertEqual(self.github.merged, [])

        bundle = self.dispatch_authorized_repair()
        state = self.store.states["PRO-200"]
        self.store.states["PRO-200"] = replace(
            state, snapshot=replace(state.snapshot, pull_requests={}),
        )
        self.store.events.clear()
        result = self.workflow.record_phase_completion(
            repair_completion(REPLACEMENT_SHA, bundle.digest)
        )
        self.assertEqual((result.next_action, result.mutation_count), ("block", 0))
        self.assertFalse(any(event[0] == "write-completion" for event in self.store.events))

    def test_recovery_and_watcher_block_managed_head_drift_before_rerun(self):
        for entrypoint in ("recovery", "watcher"):
            with self.subTest(entrypoint=entrypoint):
                child = WorkflowChild(
                    "PRO-200-API", "api", "api", "", "implementation", 1, 0,
                    "in_progress", "dispatch:" + "3" * 64, False,
                    creation_candidate_shas={"api": SHA["api"]},
                )
                snapshot = ParentSnapshot(
                    affected_repositories=("api",),
                    candidate_shas={"api": SHA["api"]},
                    children={"api": RepositoryEvidence(SHA["api"], "pending")},
                    pull_requests={
                        "api": PullRequestEvidence(OTHER_SHA, "open", True, True)
                    },
                    stalled=True,
                    stalled_repository="api",
                )
                self.store.add_state(
                    "PRO-200", snapshot, children=(child,),
                    pull_requests={"api": pull_request_targets()["api"]},
                    stage_ordinal=1,
                )
                self.store.activate_on_rerun = True
                self.store.events.clear()

                result = (
                    self.workflow.recover_stalled_parent("PRO-200")
                    if entrypoint == "recovery"
                    else self.workflow.watch_active_parents()
                )

                self.assertEqual(result.next_action, "block")
                self.assertEqual(result.mutation_count, 0)
                self.assertFalse(any(event[0] == "rerun" for event in self.store.events))

    def test_round_three_requires_exactly_two_automatic_repairs_used(self):
        for invalid_count in (0, 1):
            with self.subTest(invalid_count=invalid_count):
                snapshot = ParentSnapshot(
                    affected_repositories=("api",),
                    candidate_shas={"api": SHA["api"]},
                    children={"api": RepositoryEvidence(SHA["api"], "pass")},
                    pull_requests={
                        "api": PullRequestEvidence(SHA["api"], "open", True, True)
                    },
                    attempt=3,
                )
                self.store.add_state(
                    "PRO-200", snapshot,
                    pull_requests={"api": pull_request_targets()["api"]},
                )
                metadata = self.store.states["PRO-200"].metadata
                assert isinstance(metadata, ParentMetadata)
                object.__setattr__(metadata, "automatic_repairs_used", invalid_count)
                self.store.events.clear()

                result = self.workflow.resume_parent("PRO-200")

                self.assertEqual(result.next_action, "block")
                self.assertEqual(result.mutation_count, 0)
                self.assertIn("schema", result.reason)


class GenericWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_manifest(FIXTURE)
        self.store = FakeWorkflowStore(self.manifest)
        self.store.add_blank("PRO-101")
        self.store.add_blank("PRO-102")
        self.store.add_blank("PRO-103")
        self.github = FakeGitHub(self.store.events)
        self.workflow = GenericWorkflow(
            self.manifest,
            self.store,
            self.store,
            github=self.github,
        )

    def owned_smoke_workflow(self):
        manager = FakeOwnedProcessManager(self.manifest)
        self.addCleanup(manager.backend.temporary.cleanup)
        backend = MutableClosedCommandBackend(self.manifest)
        workflow = GenericWorkflow(
            self.manifest,
            self.store,
            self.store,
            github=self.github,
            smoke_executor=OwnedSmokeExecutor(
                self.manifest,
                manager,
                LocalExactShaCommandRunner(self.manifest, backend),
            ),
        )
        return workflow, manager, backend

    def test_parent_intake_dispatches_only_first_topological_wave(self):
        result = self.workflow.handle_parent_event(
            "PRO-101",
            affected=frozenset({"api", "web"}),
        )

        self.assertEqual(result.created_children, (("api", "implementation"),))
        self.assertEqual(result.parent_status, "in_progress")
        created = [event for event in self.store.events if event[0] == "create"]
        self.assertEqual(created[0][2][0].stage_ordinal, 1)

    def test_successful_child_creation_with_unobservable_reread_is_not_overwritten(self):
        self.store.fail_reads_after_create = True

        result = self.workflow.handle_parent_event(
            "PRO-101",
            affected=frozenset({"api"}),
        )

        state = self.store.states["PRO-101"]
        self.assertEqual(result.next_action, "uncertain")
        self.assertEqual(state.metadata.stage_ordinal, 1)
        self.assertEqual(len(state.children), 1)
        self.assertFalse(any(event[0] == "status" for event in self.store.events))

    def test_direct_parent_event_cannot_bootstrap_without_authoritative_transition(self):
        self.store.add_blank("PRO-104", authorized=False)

        result = self.workflow.handle_parent_event(
            "PRO-104",
            affected=frozenset({"api"}),
        )

        self.assertEqual(result.next_action, "noop")
        self.assertFalse(
            any(event[0] == "initialize" and event[1] == "PRO-104" for event in self.store.events)
        )

    def test_status_handler_records_authoritative_transition_before_intake(self):
        self.store.add_blank("PRO-104", authorized=False)
        resolver = FakeScopeResolver(ScopeResolution(affected=frozenset({"api"})))
        workflow = GenericWorkflow(
            self.manifest,
            self.store,
            self.store,
            github=self.github,
            scope_resolver=resolver,
        )

        result = workflow.handle_status_change("PRO-104", "backlog", "todo")

        event_kinds = [event[0] for event in self.store.events]
        self.assertLess(
            event_kinds.index("record-intake-transition"),
            event_kinds.index("initialize"),
        )
        self.assertEqual(result.created_children, (("api", "implementation"),))

    def test_only_backlog_to_todo_starts_intake(self):
        dispatch = self.workflow.handle_status_change("PRO-101", "backlog", "todo")
        noop = self.workflow.handle_status_change("PRO-102", "todo", "in_progress")

        self.assertEqual(dispatch.next_action, "dispatch")
        self.assertEqual(noop.next_action, "noop")
        self.assertFalse(any(event[0] == "initialize" and event[1] == "PRO-102" for event in self.store.events))

    def test_parent_intake_rejects_repository_project_issues(self):
        repository_project = self.manifest.repositories["api"].project_title
        self.store.states["PRO-101"] = replace(
            self.store.states["PRO-101"],
            project_key=repository_project,
        )

        status_result = self.workflow.handle_status_change(
            "PRO-101", "backlog", "todo"
        )
        direct_result = self.workflow.handle_parent_event(
            "PRO-101", affected=frozenset({"api"})
        )

        self.assertEqual(status_result.next_action, "noop")
        self.assertEqual(direct_result.next_action, "noop")
        self.assertFalse(any(event[0] == "initialize" for event in self.store.events))

    def test_backlog_to_todo_uses_injected_scope_resolution_for_real_intake(self):
        resolver = FakeScopeResolver(
            ScopeResolution(affected=frozenset({"api", "web"}))
        )
        workflow = GenericWorkflow(
            self.manifest,
            self.store,
            self.store,
            github=self.github,
            scope_resolver=resolver,
        )

        result = workflow.handle_status_change("PRO-101", "backlog", "todo")
        ignored = workflow.handle_status_change("PRO-102", "todo", "in_progress")

        self.assertEqual(result.created_children, (("api", "implementation"),))
        self.assertEqual(resolver.calls, ["PRO-101"])
        self.assertEqual(ignored.next_action, "noop")

    def test_ambiguous_outside_or_dependency_incomplete_scope_waits_for_human(self):
        ambiguous = self.workflow.handle_parent_event(
            "PRO-101",
            affected_candidates=(frozenset({"api"}), frozenset({"api", "web"})),
        )
        outside = self.workflow.handle_parent_event(
            "PRO-102",
            affected=frozenset({"billing"}),
        )
        incomplete = self.workflow.handle_parent_event(
            "PRO-103",
            affected=frozenset({"web"}),
        )

        self.assertEqual(ambiguous.next_action, "human-clarification")
        self.assertEqual(outside.next_action, "human-clarification")
        self.assertEqual(incomplete.next_action, "human-clarification")

    def test_initialized_parent_waits_for_new_authority_instead_of_resuming_work(self):
        self.workflow.handle_parent_event(
            "PRO-101",
            affected=frozenset({"api", "web"}),
        )

        result = self.workflow.handle_parent_event(
            "PRO-101",
            authority_requirements=("new-secret-recipient",),
        )

        self.assertEqual(result.next_action, "human-clarification")
        self.assertTrue(self.store.states["PRO-101"].human_wait)

    def test_coordinator_action_key_covers_ordinal_candidates_and_contracts(self):
        inputs = dict(
            workflow_version=2,
            instance_key="sample-commerce",
            parent_identifier="PRO-101",
            stage_kind="implementation",
            stage_ordinal=2,
            attempt=0,
            affected_repositories=frozenset({"api", "web"}),
            candidate_shas={"api": SHA["api"]},
            contract_hashes={"api": "f" * 40},
        )

        first = coordinator_action_key(**inputs)
        second = coordinator_action_key(**inputs)
        changed = coordinator_action_key(**{**inputs, "stage_ordinal": 3})

        self.assertEqual(first, second)
        self.assertRegex(first, r"^dispatch:[0-9a-f]{64}$")
        self.assertNotEqual(first, changed)

    def test_repair_action_key_is_bound_to_failure_bundle_digest(self):
        inputs = dict(
            workflow_version=2,
            instance_key="sample-commerce",
            parent_identifier="PRO-101",
            stage_kind="repair",
            stage_ordinal=6,
            attempt=1,
            affected_repositories=frozenset({"api", "web"}),
            candidate_shas={"api": SHA["api"], "web": SHA["web"]},
            contract_hashes={},
        )

        first = coordinator_action_key(
            **inputs,
            failure_bundle_digest="1" * 64,
        )
        second = coordinator_action_key(
            **inputs,
            failure_bundle_digest="2" * 64,
        )

        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^repair:[0-9a-f]{64}$")

    def test_repeated_intake_with_active_successor_is_noop(self):
        self.workflow.handle_parent_event("PRO-101", affected=frozenset({"api", "web"}))
        before = len([event for event in self.store.events if event[0] == "create"])

        result = self.workflow.handle_parent_event(
            "PRO-101",
            affected=frozenset({"api", "web"}),
        )

        self.assertEqual(result.next_action, "noop")
        self.assertEqual(len([event for event in self.store.events if event[0] == "create"]), before)

    def test_event_entrypoints_fail_closed_without_mutation_for_future_child_relationship(self):
        snapshot = passing_snapshot()
        future_child = WorkflowChild(
            "PRO-101-FUTURE", "api", "api", "", "review", 6, 0,
            "in_progress", "review:" + "b" * 64, True,
        )
        entrypoints = {
            "status callback": lambda: self.workflow.handle_status_change(
                "PRO-101", "backlog", "todo"
            ),
            "parent event": lambda: self.workflow.handle_parent_event(
                "PRO-101", affected=frozenset({"api", "web"})
            ),
        }

        for entrypoint, invoke in entrypoints.items():
            with self.subTest(entrypoint=entrypoint):
                self.store.add_state(
                    "PRO-101",
                    snapshot,
                    status="todo" if entrypoint == "status callback" else "in_progress",
                    children=(future_child,),
                    pull_requests=pull_request_targets(),
                )
                before = self.store.states["PRO-101"]
                self.store.events.clear()

                result = invoke()

                self.assertEqual(result.parent_status, "blocked")
                self.assertEqual(result.next_action, "block")
                self.assertIn("child relationship", result.reason)
                self.assertEqual(result.mutation_count, 0)
                self.assertEqual(self.store.states["PRO-101"], before)
                self.assertFalse(
                    any(
                        event[0]
                        in {
                            "record-intake-transition",
                            "initialize",
                            "human",
                            "create",
                            "status",
                        }
                        for event in self.store.events
                    )
                )

    def test_replayed_intake_action_key_is_noop_even_before_metadata_is_visible(self):
        key = coordinator_action_key(
            workflow_version=2,
            instance_key="sample-commerce",
            parent_identifier="PRO-101",
            stage_kind="intake",
            stage_ordinal=0,
            attempt=0,
            affected_repositories=frozenset({"api"}),
            candidate_shas={},
            contract_hashes={},
        )
        self.store.states["PRO-101"] = replace(
            self.store.states["PRO-101"],
            applied_action_keys=frozenset({key}),
        )

        result = self.workflow.handle_parent_event(
            "PRO-101",
            affected=frozenset({"api"}),
        )

        self.assertEqual(result.next_action, "noop")
        self.assertFalse(any(event[0] == "initialize" for event in self.store.events))

    def test_terminal_duplicate_without_authoritative_evidence_is_not_recreated(self):
        snapshot = ParentSnapshot(affected_repositories=("api",))
        duplicate = WorkflowChild(
            "PRO-101-OLD",
            "api",
            "api",
            "",
            "implementation",
            1,
            0,
            "done",
            "dispatch:" + "0" * 64,
            False,
        )
        self.store.add_state("PRO-101", snapshot, children=(duplicate,))

        result = self.workflow.resume_parent("PRO-101")

        self.assertEqual(result.next_action, "noop")
        self.assertFalse(any(event[0] == "create" for event in self.store.events))

    def test_api_completion_is_verified_done_then_resumes_and_dispatches_web(self):
        self.workflow.handle_parent_event("PRO-101", affected=frozenset({"api", "web"}))
        self.store.events.clear()

        result = self.workflow.record_phase_completion(completion_for("api"))

        self.assertEqual(result.completed_child_status, "done")
        self.assertEqual(result.created_children, (("web", "implementation"),))
        order = [event[0] for event in self.store.events]
        read_positions = [
            index for index, kind in enumerate(order) if kind == "read-completion"
        ]
        self.assertLess(read_positions[0], order.index("write-completion"))
        self.assertLess(order.index("write-completion"), read_positions[-1])
        self.assertLess(read_positions[-1], order.index("done"))
        self.assertLess(order.index("done"), order.index("create"))

    def test_gate_stage_has_independent_repository_and_integration_children(self):
        self.workflow.handle_parent_event("PRO-101", affected=frozenset({"api", "web"}))
        self.workflow.record_phase_completion(completion_for("api"))

        result = self.workflow.record_phase_completion(completion_for("web"))

        self.assertEqual(
            result.created_children,
            (
                ("api", "review"),
                ("api", "qa"),
                ("web", "review"),
                ("web", "qa"),
                ("web-api", "integration_qa"),
            ),
        )

    def test_gate_dispatch_uses_typed_phase_not_decision_reason_wording(self):
        snapshot = replace(passing_snapshot(), reviews={}, qa={}, integration_qa={})
        self.store.add_state(
            "PRO-101",
            snapshot,
            pull_requests=pull_request_targets(),
        )
        typed_decision = ParentDecision(
            DecisionKind.DISPATCH,
            "wording deliberately contains no phase hint",
            ("api", "web"),
            dispatch_kind=DispatchKind.GATES,
        )

        with patch(
            "multica_delivery.core.workflow.decide_parent_action",
            return_value=typed_decision,
        ):
            result = self.workflow.resume_parent("PRO-101")

        self.assertEqual(result.created_children[0], ("api", "review"))
        self.assertNotIn(("api", "implementation"), result.created_children)

    def test_independent_gate_completions_have_distinct_action_keys(self):
        self.workflow.handle_parent_event("PRO-101", affected=frozenset({"api", "web"}))
        self.workflow.record_phase_completion(completion_for("api"))
        self.workflow.record_phase_completion(completion_for("web"))
        self.store.events.clear()

        self.workflow.record_phase_completion(completion_for("api", phase="review"))
        self.workflow.record_phase_completion(completion_for("web", phase="review"))

        keys = [event[2] for event in self.store.events if event[0] == "write-completion"]
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])

    def test_failed_phase_is_done_but_parent_repairs_existing_pr(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            reviews={**snapshot.reviews, "web": RepositoryEvidence(SHA["web"], "pending")},
        )
        review_child = WorkflowChild(
            "PRO-101-REVIEW",
            "web",
            "web",
            "",
            "review",
            5,
            0,
            "in_progress",
            "review:" + "1" * 64,
            True,
            creation_candidate_shas=snapshot.candidate_shas,
        )
        self.store.add_state(
            "PRO-101",
            snapshot,
            children=(review_child,),
            pull_requests=pull_request_targets(),
        )

        result = self.workflow.record_phase_completion(
            completion_for("web", phase="review", result="fail")
        )

        self.assertEqual(result.completed_child_status, "done")
        self.assertEqual(result.next_action, "repair")
        created = [event for event in self.store.events if event[0] == "create"][-1][2]
        self.assertEqual(created[0].phase, "repair")
        self.assertEqual(created[0].pull_request, pull_request_targets()["web"])

    def test_failed_gate_waits_for_active_stage_sibling_before_one_shared_repair(self):
        snapshot = replace(
            passing_snapshot(),
            reviews={
                "api": RepositoryEvidence(SHA["api"], "pending"),
                "web": RepositoryEvidence(SHA["web"], "pending"),
            },
        )
        children = (
            WorkflowChild(
                "PRO-101-API-REVIEW", "api", "api", "", "review", 5, 0,
                "in_progress", "review:" + "1" * 64, True,
                creation_candidate_shas=snapshot.candidate_shas,
            ),
            WorkflowChild(
                "PRO-101-WEB-REVIEW", "web", "web", "", "review", 5, 0,
                "in_progress", "review:" + "1" * 64, True,
                creation_candidate_shas=snapshot.candidate_shas,
            ),
        )
        self.store.add_state(
            "PRO-101", snapshot, children=children,
            pull_requests=pull_request_targets(),
        )

        first = self.workflow.record_phase_completion(
            completion_for("web", phase="review", result="fail")
        )
        creates_after_first = len(
            [event for event in self.store.events if event[0] == "create"]
        )
        second = self.workflow.record_phase_completion(
            completion_for("api", phase="review", result="pass")
        )

        self.assertEqual(first.next_action, "wait")
        self.assertEqual(creates_after_first, 0)
        self.assertEqual(second.next_action, "repair")
        self.assertEqual(second.created_children, (("web", "repair"),))

    def test_pro_65_race_waits_then_dispatches_one_repair_with_both_findings(self):
        snapshot = replace(
            passing_snapshot(),
            attempt=1,
            reviews={
                "api": RepositoryEvidence(SHA["api"], "pending"),
                "web": RepositoryEvidence(SHA["web"], "pass"),
            },
            qa={
                "api": RepositoryEvidence(SHA["api"], "pending"),
                "web": RepositoryEvidence(SHA["web"], "pass"),
            },
        )
        children = (
            WorkflowChild(
                "PRO-201-REVIEW", "api", "api", "", "review", 7, 1,
                "in_progress", "review:" + "1" * 64, True,
                creation_candidate_shas=snapshot.candidate_shas,
            ),
            WorkflowChild(
                "PRO-202-QA", "api", "api", "", "qa", 7, 1,
                "in_progress", "qa:" + "2" * 64, True,
                creation_candidate_shas=snapshot.candidate_shas,
            ),
        )
        self.store.add_state(
            "PRO-200",
            snapshot,
            children=children,
            pull_requests=pull_request_targets(),
            stage_ordinal=7,
        )

        qa_result = self.workflow.record_phase_completion(
            completion_for(
                "api",
                parent="PRO-200",
                phase="qa",
                result="fail",
                attempt=1,
                responsible_repositories=("api",),
                comment_digit="2",
            )
        )
        self.assertEqual(qa_result.next_action, "wait")
        self.assertEqual(
            tuple(event for event in self.store.events if event[0] == "create"),
            (),
        )

        review_result = self.workflow.record_phase_completion(
            completion_for(
                "api",
                parent="PRO-200",
                phase="review",
                result="fail",
                attempt=1,
                responsible_repositories=("api",),
                comment_digit="1",
            )
        )
        requests = [event for event in self.store.events if event[0] == "create"][-1][2]
        self.assertEqual(review_result.next_action, "repair")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].repository_key, "api")
        self.assertEqual(
            {ref.child_identifier for ref in requests[0].failure_refs},
            {"PRO-201-REVIEW", "PRO-202-QA"},
        )

    def test_two_repository_failures_share_one_bundle_and_round(self):
        snapshot = replace(
            passing_snapshot(),
            reviews={
                "api": RepositoryEvidence(SHA["api"], "pending"),
                "web": RepositoryEvidence(SHA["web"], "pending"),
            },
        )
        children = tuple(
            WorkflowChild(
                f"PRO-101-{repository.upper()}-REVIEW",
                repository,
                repository,
                "",
                "review",
                5,
                0,
                "in_progress",
                "review:" + digit * 64,
                True,
                creation_candidate_shas=snapshot.candidate_shas,
            )
            for repository, digit in (("api", "3"), ("web", "4"))
        )
        self.store.add_state(
            "PRO-101", snapshot, children=children,
            pull_requests=pull_request_targets(),
        )

        waiting = self.workflow.record_phase_completion(
            completion_for("web", phase="review", result="fail", comment_digit="4")
        )
        result = self.workflow.record_phase_completion(
            completion_for("api", phase="review", result="fail", comment_digit="3")
        )

        requests = [event for event in self.store.events if event[0] == "create"][-1][2]
        self.assertEqual(waiting.next_action, "wait")
        self.assertEqual(result.next_action, "repair")
        self.assertEqual(tuple(request.repository_key for request in requests), ("api", "web"))
        self.assertEqual({request.attempt for request in requests}, {1})
        self.assertEqual({request.failure_bundle.digest for request in requests}, {requests[0].failure_bundle.digest})
        self.assertEqual(
            tuple(tuple(ref.child_identifier for ref in request.failure_refs) for request in requests),
            (("PRO-101-API-REVIEW",), ("PRO-101-WEB-REVIEW",)),
        )

    def test_cross_repository_integration_failure_is_in_each_owner_partition(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            integration_qa={
                "web-api": GateEvidence(snapshot.candidate_shas, "pending")
            },
        )
        child = WorkflowChild(
            "PRO-101-WEB-API-QA", "web-api", "web", "web-api", "integration_qa",
            5, 0, "in_progress", "qa:" + "5" * 64, True,
            creation_candidate_shas=snapshot.candidate_shas,
        )
        self.store.add_state(
            "PRO-101", snapshot, children=(child,), pull_requests=pull_request_targets()
        )

        result = self.workflow.record_phase_completion(
            completion_for(
                "web",
                phase="integration_qa",
                result="fail",
                suite_key="web-api",
                candidate_shas=dict(snapshot.candidate_shas),
                responsible_repositories=("api", "web"),
                comment_digit="5",
            )
        )

        requests = [event for event in self.store.events if event[0] == "create"][-1][2]
        self.assertEqual(result.next_action, "repair")
        self.assertEqual(tuple(request.repository_key for request in requests), ("api", "web"))
        self.assertEqual(
            tuple(tuple(ref.child_identifier for ref in request.failure_refs) for request in requests),
            (("PRO-101-WEB-API-QA",), ("PRO-101-WEB-API-QA",)),
        )

    def test_cross_repository_integration_failure_repairs_only_declared_subset(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            integration_qa={
                "web-api": GateEvidence(snapshot.candidate_shas, "pending")
            },
        )
        child = WorkflowChild(
            "PRO-101-WEB-API-QA", "web-api", "web", "web-api", "integration_qa",
            5, 0, "in_progress", "qa:" + "6" * 64, True,
            creation_candidate_shas=snapshot.candidate_shas,
        )
        self.store.add_state(
            "PRO-101", snapshot, children=(child,), pull_requests=pull_request_targets()
        )

        result = self.workflow.record_phase_completion(
            completion_for(
                "web",
                phase="integration_qa",
                result="fail",
                suite_key="web-api",
                candidate_shas=dict(snapshot.candidate_shas),
                responsible_repositories=("api",),
                comment_digit="6",
            )
        )

        create_events = [event for event in self.store.events if event[0] == "create"]
        self.assertEqual(result.next_action, "repair")
        self.assertEqual(result.created_children, (("api", "repair"),))
        self.assertEqual(len(create_events), 1)
        self.assertEqual(
            tuple(request.repository_key for request in create_events[0][2]),
            ("api",),
        )

    def test_terminal_malformed_failure_blocks_without_partial_bundle(self):
        snapshot = replace(
            passing_snapshot(),
            reviews={
                **passing_snapshot().reviews,
                "api": RepositoryEvidence(SHA["api"], "fail"),
            },
        )
        malformed = WorkflowChild(
            "PRO-101-API-REVIEW", "api", "api", "", "review", 5, 0,
            "done", "review:" + "6" * 64, False,
            creation_candidate_shas=snapshot.candidate_shas,
            phase_result="fail",
        )
        self.store.add_state(
            "PRO-101", snapshot, children=(malformed,),
            pull_requests=pull_request_targets(),
        )

        result = self.workflow.resume_parent("PRO-101")

        self.assertEqual(result.next_action, "block")
        self.assertEqual(result.mutation_count, 0)
        self.assertFalse(any(event[0] == "create" for event in self.store.events))

    def test_failure_bundle_requires_every_snapshot_nonpass_gate_child(self):
        snapshot = replace(
            passing_snapshot(),
            reviews={
                **passing_snapshot().reviews,
                "api": RepositoryEvidence(SHA["api"], "fail"),
            },
            qa={
                **passing_snapshot().qa,
                "api": RepositoryEvidence(SHA["api"], "fail"),
            },
        )
        comment_uuid = str(uuid.UUID("1" * 32))
        qa_only = WorkflowChild(
            "PRO-101-API-QA", "api", "api", "", "qa", 5, 0,
            "done", "qa:" + "1" * 64, False,
            evidence_comment_uuid=comment_uuid,
            creation_candidate_shas=snapshot.candidate_shas,
            phase_result="fail",
            evidence_comment_url=f"https://example.test/evidence/{comment_uuid}",
            responsible_repositories=("api",),
        )
        self.store.add_state(
            "PRO-101", snapshot, children=(qa_only,),
            pull_requests=pull_request_targets(),
        )
        self.store.events.clear()

        result = self.workflow.resume_parent("PRO-101")

        self.assertEqual(result.next_action, "block")
        self.assertEqual(result.mutation_count, 0)
        self.assertFalse(any(event[0] == "create" for event in self.store.events))

    def test_current_gate_stage_requires_exact_complete_expected_membership(self):
        snapshot = replace(
            passing_snapshot(),
            qa={
                **passing_snapshot().qa,
                "api": RepositoryEvidence(SHA["api"], "fail"),
            },
        )

        def gate_child(
            identifier: str,
            target: str,
            repository: str,
            phase_name: str,
            digit: str,
            *,
            suite: str = "",
            result: str = "pass",
            active: bool = False,
        ) -> WorkflowChild:
            comment_uuid = str(uuid.UUID(digit * 32))
            return WorkflowChild(
                identifier,
                target,
                repository,
                suite,
                phase_name,
                5,
                0,
                "in_progress" if active else "done",
                ("qa:" if phase_name in {"qa", "integration_qa"} else "review:")
                + digit * 64,
                active,
                evidence_comment_uuid="" if active else comment_uuid,
                creation_candidate_shas=snapshot.candidate_shas,
                phase_result="" if active else result,
                evidence_comment_url=(
                    ""
                    if active
                    else f"https://example.test/evidence/{comment_uuid}"
                ),
                responsible_repositories=("api",) if result == "fail" else (),
            )

        complete = (
            gate_child("PRO-101-API-REVIEW", "api", "api", "review", "1"),
            gate_child("PRO-101-API-QA", "api", "api", "qa", "2", result="fail"),
            gate_child("PRO-101-WEB-REVIEW", "web", "web", "review", "3"),
            gate_child("PRO-101-WEB-QA", "web", "web", "qa", "4"),
            gate_child(
                "PRO-101-WEB-API-QA",
                "web-api",
                "web",
                "integration_qa",
                "5",
                suite="web-api",
            ),
        )
        cases = {
            "missing repository review": complete[1:],
            "missing repository QA": complete[:1] + complete[2:],
            "missing integration suite": complete[:-1],
            "active sibling": (
                complete[0],
                complete[1],
                complete[2],
                replace(
                    complete[3],
                    status="in_progress",
                    active=True,
                    evidence_comment_uuid="",
                    phase_result="",
                    evidence_comment_url="",
                ),
                complete[4],
            ),
            "duplicate gate": complete + (
                replace(
                    complete[0],
                    identifier="PRO-101-API-REVIEW-DUPLICATE",
                    evidence_comment_uuid=str(uuid.UUID("6" * 32)),
                    evidence_comment_url=(
                        "https://example.test/evidence/"
                        + str(uuid.UUID("6" * 32))
                    ),
                ),
            ),
            "unexpected current child": complete + (
                WorkflowChild(
                    "PRO-101-UNEXPECTED",
                    "api",
                    "api",
                    "",
                    "implementation",
                    5,
                    0,
                    "done",
                    "dispatch:" + "7" * 64,
                    False,
                    evidence_comment_uuid=str(uuid.UUID("7" * 32)),
                    creation_candidate_shas=snapshot.candidate_shas,
                    phase_result="pass",
                    evidence_comment_url=(
                        "https://example.test/evidence/"
                        + str(uuid.UUID("7" * 32))
                    ),
                ),
            ),
        }

        for label, children in cases.items():
            with self.subTest(label=label):
                self.store.add_state(
                    "PRO-101",
                    snapshot,
                    children=children,
                    pull_requests=pull_request_targets(),
                    stage_ordinal=5,
                    hydrate_current_gate_passes=False,
                )
                self.store.events.clear()

                result = self.workflow.resume_parent("PRO-101")

                self.assertIn(result.next_action, {"wait", "block"})
                self.assertEqual(result.mutation_count, 0)
                self.assertFalse(
                    any(event[0] == "create" for event in self.store.events)
                )

        self.store.add_state(
            "PRO-101",
            snapshot,
            children=complete,
            pull_requests=pull_request_targets(),
            stage_ordinal=5,
            hydrate_current_gate_passes=False,
        )
        self.store.events.clear()

        complete_result = self.workflow.resume_parent("PRO-101")

        self.assertEqual(complete_result.next_action, "repair")
        self.assertEqual(complete_result.created_children, (("api", "repair"),))

    def test_failure_bundle_rejects_duplicate_current_gate_identity(self):
        snapshot = replace(
            passing_snapshot(),
            reviews={
                **passing_snapshot().reviews,
                "api": RepositoryEvidence(SHA["api"], "fail"),
            },
        )
        children = tuple(
            WorkflowChild(
                f"PRO-101-API-REVIEW-{digit}", "api", "api", "", "review", 5, 0,
                "done", "review:" + digit * 64, False,
                evidence_comment_uuid=str(uuid.UUID(digit * 32)),
                creation_candidate_shas=snapshot.candidate_shas,
                phase_result="fail",
                evidence_comment_url=(
                    f"https://example.test/evidence/{str(uuid.UUID(digit * 32))}"
                ),
                responsible_repositories=("api",),
            )
            for digit in ("2", "3")
        )
        self.store.add_state(
            "PRO-101", snapshot, children=children,
            pull_requests=pull_request_targets(),
        )
        self.store.events.clear()

        result = self.workflow.resume_parent("PRO-101")

        self.assertEqual(result.next_action, "block")
        self.assertEqual(result.mutation_count, 0)
        self.assertFalse(any(event[0] == "create" for event in self.store.events))

    def test_repair_partition_uuid_identity_uses_one_canonical_order(self):
        snapshot = replace(
            passing_snapshot(),
            reviews={
                **passing_snapshot().reviews,
                "api": RepositoryEvidence(SHA["api"], "pending"),
            },
            qa={
                **passing_snapshot().qa,
                "api": RepositoryEvidence(SHA["api"], "pending"),
            },
        )
        children = (
            WorkflowChild(
                "PRO-101-API-REVIEW", "api", "api", "", "review", 5, 0,
                "in_progress", "review:" + "f" * 64, True,
                creation_candidate_shas=snapshot.candidate_shas,
            ),
            WorkflowChild(
                "PRO-101-API-QA", "api", "api", "", "qa", 5, 0,
                "in_progress", "qa:" + "1" * 64, True,
                creation_candidate_shas=snapshot.candidate_shas,
            ),
        )
        self.store.add_state(
            "PRO-101", snapshot, children=children,
            pull_requests=pull_request_targets(),
        )

        waiting = self.workflow.record_phase_completion(
            completion_for("api", phase="qa", result="fail", comment_digit="1")
        )
        result = self.workflow.record_phase_completion(
            completion_for("api", phase="review", result="fail", comment_digit="f")
        )

        self.assertEqual(waiting.next_action, "wait")
        self.assertEqual(result.next_action, "repair")
        repair_child = self.store.states["PRO-101"].children[-1]
        self.assertEqual(
            repair_child.failure_evidence_uuids,
            tuple(sorted((str(uuid.UUID("f" * 32)), str(uuid.UUID("1" * 32))))),
        )

    def test_duplicate_bundle_dispatch_is_idempotent(self):
        snapshot = replace(
            passing_snapshot(),
            reviews={
                **passing_snapshot().reviews,
                "api": RepositoryEvidence(SHA["api"], "pending"),
            },
        )
        child = WorkflowChild(
            "PRO-101-API-REVIEW", "api", "api", "", "review", 5, 0,
            "in_progress",
            coordinator_action_key(
                workflow_version=2,
                instance_key="sample-commerce",
                parent_identifier="PRO-101",
                stage_kind="gates",
                stage_ordinal=5,
                attempt=0,
                affected_repositories=frozenset(snapshot.affected_repositories),
                candidate_shas=snapshot.candidate_shas,
                contract_hashes={},
            ),
            True,
            creation_candidate_shas=snapshot.candidate_shas,
        )
        self.store.add_state(
            "PRO-101", snapshot, children=(child,), pull_requests=pull_request_targets()
        )
        completion = completion_for(
            "api", phase="review", result="fail", comment_digit="7"
        )

        first = self.workflow.record_phase_completion(completion)
        creates = tuple(event for event in self.store.events if event[0] == "create")
        duplicate = self.workflow.record_phase_completion(completion)

        self.assertEqual(first.next_action, "repair")
        self.assertEqual(duplicate.next_action, "noop")
        self.assertEqual(tuple(event for event in self.store.events if event[0] == "create"), creates)

    def test_existing_repair_successor_with_different_digest_blocks_as_corruption(self):
        snapshot = replace(
            passing_snapshot(),
            reviews={
                **passing_snapshot().reviews,
                "api": RepositoryEvidence(SHA["api"], "fail"),
            },
        )
        comment_uuid = str(uuid.UUID("8" * 32))
        failure = WorkflowChild(
            "PRO-101-API-REVIEW", "api", "api", "", "review", 5, 0,
            "done", "review:" + "8" * 64, False,
            evidence_comment_uuid=comment_uuid,
            creation_candidate_shas=snapshot.candidate_shas,
            phase_result="fail",
            evidence_comment_url=f"https://example.test/evidence/{comment_uuid}",
            responsible_repositories=("api",),
        )
        corrupt_successor = WorkflowChild(
            "PRO-101-API-REPAIR", "api", "api", "", "repair", 6, 1,
            "todo", "repair:" + "9" * 64, True,
            creation_candidate_shas=snapshot.candidate_shas,
            failure_bundle_digest="f" * 64,
            failure_evidence_uuids=(comment_uuid,),
        )
        self.store.add_state(
            "PRO-101",
            snapshot,
            children=(failure, corrupt_successor),
            pull_requests=pull_request_targets(),
        )
        state = self.store.states["PRO-101"]
        decision = decide_parent_action(self.manifest, state.snapshot)
        self.store.events.clear()

        result = self.workflow._dispatch(state, decision, repair=True)

        self.assertEqual(result.next_action, "block")
        self.assertIn("bundle identity conflicts", result.reason)
        self.assertEqual(result.mutation_count, 0)
        self.assertFalse(any(event[0] == "create" for event in self.store.events))

    def test_historical_same_bundle_successor_is_not_a_current_noop(self):
        snapshot = replace(
            passing_snapshot(),
            reviews={
                **passing_snapshot().reviews,
                "api": RepositoryEvidence(SHA["api"], "fail"),
            },
        )
        comment_uuid = str(uuid.UUID("4" * 32))
        failure = WorkflowChild(
            "PRO-101-API-REVIEW", "api", "api", "", "review", 5, 0,
            "done", "review:" + "4" * 64, False,
            evidence_comment_uuid=comment_uuid,
            creation_candidate_shas=snapshot.candidate_shas,
            phase_result="fail",
            evidence_comment_url=f"https://example.test/evidence/{comment_uuid}",
            responsible_repositories=("api",),
        )
        self.store.add_state(
            "PRO-101", snapshot, children=(failure,),
            pull_requests=pull_request_targets(),
        )
        state = self.store.states["PRO-101"]
        decision = decide_parent_action(self.manifest, state.snapshot)
        bundle = self.workflow._failure_bundle(state, decision)
        historical = WorkflowChild(
            "PRO-101-API-REPAIR-OLD", "api", "api", "", "repair", 4, 1,
            "done", "repair:" + "5" * 64, False,
            creation_candidate_shas=snapshot.candidate_shas,
            failure_bundle_digest=bundle.digest,
            failure_evidence_uuids=(comment_uuid,),
        )
        state = replace(state, children=state.children + (historical,))
        self.store.states["PRO-101"] = state
        self.store.events.clear()

        result = self.workflow._dispatch(state, decision, repair=True)

        self.assertEqual(result.next_action, "block")
        self.assertIn("bundle identity conflicts", result.reason)
        self.assertEqual(result.mutation_count, 0)
        self.assertFalse(any(event[0] == "create" for event in self.store.events))

    def test_duplicate_repair_child_after_create_fails_exact_reconciliation(self):
        snapshot = replace(
            passing_snapshot(),
            reviews={
                **passing_snapshot().reviews,
                "api": RepositoryEvidence(SHA["api"], "pending"),
            },
        )
        child = WorkflowChild(
            "PRO-101-API-REVIEW", "api", "api", "", "review", 5, 0,
            "in_progress", "review:" + "6" * 64, True,
            creation_candidate_shas=snapshot.candidate_shas,
        )
        self.store.add_state(
            "PRO-101", snapshot, children=(child,),
            pull_requests=pull_request_targets(),
        )
        self.store.duplicate_repair_child_on_create = True

        result = self.workflow.record_phase_completion(
            completion_for("api", phase="review", result="fail", comment_digit="6")
        )

        self.assertEqual(result.next_action, "uncertain")
        self.assertEqual(result.mutation_count, 2)
        repairs = [
            item
            for item in self.store.states["PRO-101"].children
            if item.phase == "repair"
        ]
        self.assertEqual(len(repairs), 2)

    def test_post_create_repair_with_a_different_stored_digest_is_uncertain(self):
        snapshot = replace(
            passing_snapshot(),
            reviews={
                **passing_snapshot().reviews,
                "api": RepositoryEvidence(SHA["api"], "pending"),
            },
        )
        child = WorkflowChild(
            "PRO-101-API-REVIEW", "api", "api", "", "review", 5, 0,
            "in_progress", "review:" + "6" * 64, True,
            creation_candidate_shas=snapshot.candidate_shas,
        )
        self.store.add_state(
            "PRO-101", snapshot, children=(child,),
            pull_requests=pull_request_targets(),
        )
        self.store.replace_repair_digest_after_create = True

        result = self.workflow.record_phase_completion(
            completion_for("api", phase="review", result="fail", comment_digit="6")
        )

        self.assertEqual(result.next_action, "uncertain")
        self.assertEqual(result.mutation_count, 2)
        repair = next(
            child for child in self.store.states["PRO-101"].children
            if child.phase == "repair"
        )
        self.assertEqual(repair.failure_bundle_digest, "f" * 64)

    def test_post_create_repair_with_any_uuid_partition_mismatch_is_not_accepted(self):
        for corruption in ("subset", "extra", "duplicate"):
            with self.subTest(corruption=corruption):
                snapshot = replace(
                    passing_snapshot(),
                    reviews={
                        **passing_snapshot().reviews,
                        "api": RepositoryEvidence(SHA["api"], "pending"),
                    },
                )
                child = WorkflowChild(
                    "PRO-101-API-REVIEW", "api", "api", "", "review", 5, 0,
                    "in_progress", "review:" + "6" * 64, True,
                    creation_candidate_shas=snapshot.candidate_shas,
                )
                self.store.states.clear()
                self.store.completions.clear()
                self.store.events.clear()
                self.store.add_state(
                    "PRO-101", snapshot, children=(child,),
                    pull_requests=pull_request_targets(),
                )
                self.store.repair_failure_uuid_corruption = corruption

                result = self.workflow.record_phase_completion(
                    completion_for(
                        "api", phase="review", result="fail", comment_digit="6"
                    )
                )

                self.assertIn(result.next_action, {"uncertain", "block"})
                repair = next(
                    item for item in self.store.states["PRO-101"].children
                    if item.phase == "repair"
                )
                self.assertNotEqual(
                    repair.failure_evidence_uuids,
                    (str(uuid.UUID("6" * 32)),),
                )

    def test_direct_resume_waits_for_current_stage_before_one_shared_repair(self):
        snapshot = replace(
            passing_snapshot(),
            reviews={
                "api": RepositoryEvidence(SHA["api"], "pass"),
                "web": RepositoryEvidence(SHA["web"], "fail"),
            },
        )
        children = (
            WorkflowChild(
                "PRO-101-WEB-REVIEW", "web", "web", "", "review", 5, 0,
                "done", "review:" + "1" * 64, False,
                evidence_comment_uuid=str(uuid.UUID("1" * 32)),
                creation_candidate_shas=snapshot.candidate_shas,
                phase_result="fail",
                evidence_comment_url="https://example.test/evidence/11111111-1111-1111-1111-111111111111",
                responsible_repositories=("web",),
            ),
            WorkflowChild(
                "PRO-101-API-REVIEW", "api", "api", "", "review", 5, 0,
                "in_progress", "review:" + "2" * 64, True,
                creation_candidate_shas=snapshot.candidate_shas,
            ),
        )
        self.store.add_state(
            "PRO-101",
            snapshot,
            children=children,
            pull_requests=pull_request_targets(),
        )

        waiting = self.workflow.resume_parent("PRO-101")
        repeated_waiting = self.workflow.resume_parent("PRO-101")

        self.assertEqual(waiting.next_action, "wait")
        self.assertEqual(repeated_waiting.next_action, "wait")
        self.assertEqual(repeated_waiting.mutation_count, 0)
        self.assertEqual(waiting.mutation_count, 0)
        self.assertEqual(
            [event for event in self.store.events if event[0] == "create"],
            [],
        )
        self.assertEqual(self.store.states["PRO-101"].snapshot.attempt, 0)

        state = self.store.states["PRO-101"]
        self.store.states["PRO-101"] = replace(
            state,
            children=(
                state.children[0],
                replace(
                    state.children[1],
                    status="done",
                    active=False,
                    evidence_comment_uuid=str(uuid.UUID("2" * 32)),
                    phase_result="pass",
                    evidence_comment_url="https://example.test/evidence/22222222-2222-2222-2222-222222222222",
                ),
                *state.children[2:],
            ),
        )
        repaired = self.workflow.resume_parent("PRO-101")

        self.assertEqual(repaired.next_action, "repair")
        self.assertEqual(repaired.created_children, (("web", "repair"),))
        self.assertEqual(self.store.states["PRO-101"].snapshot.attempt, 1)
        self.assertEqual(
            len([event for event in self.store.events if event[0] == "create"]),
            1,
        )

    def test_direct_resume_treats_each_current_stage_activity_signal_as_active(self):
        snapshot = replace(
            passing_snapshot(),
            reviews={
                "api": RepositoryEvidence(SHA["api"], "pass"),
                "web": RepositoryEvidence(SHA["web"], "fail"),
            },
        )
        terminal_failure = WorkflowChild(
            "PRO-101-WEB-REVIEW", "web", "web", "", "review", 5, 0,
            "done", "review:" + "3" * 64, False,
        )
        activity_signals = {
            "active marker": (
                WorkflowChild(
                    "PRO-101-API-REVIEW", "api", "api", "", "review", 5, 0,
                    "done", "review:" + "4" * 64, True,
                ),
                False,
            ),
            "active status": (
                WorkflowChild(
                    "PRO-101-API-REVIEW", "api", "api", "", "review", 5, 0,
                    "in_review", "review:" + "4" * 64, False,
                ),
                False,
            ),
            "non-terminal backlog status": (
                WorkflowChild(
                    "PRO-101-API-REVIEW", "api", "api", "", "review", 5, 0,
                    "backlog", "review:" + "4" * 64, False,
                ),
                False,
            ),
            "parent active work": (
                WorkflowChild(
                    "PRO-101-API-REVIEW", "api", "api", "", "review", 5, 0,
                    "done", "review:" + "4" * 64, False,
                ),
                True,
            ),
        }

        for signal, (sibling, active_work) in activity_signals.items():
            with self.subTest(signal=signal):
                self.store.add_state(
                    "PRO-101",
                    snapshot,
                    children=(terminal_failure, sibling),
                    pull_requests=pull_request_targets(),
                    active_work=active_work,
                )
                self.store.events.clear()

                result = self.workflow.resume_parent("PRO-101")

                self.assertEqual(result.next_action, "wait")
                self.assertEqual(result.mutation_count, 0)
                self.assertFalse(
                    any(event[0] == "create" for event in self.store.events)
                )
                self.assertEqual(self.store.states["PRO-101"].snapshot.attempt, 0)

    def test_direct_resume_ignores_children_from_historical_stage_or_attempt(self):
        cases = (
            ("older ordinal", 0, 4, 0, 1),
            ("older attempt", 1, 5, 0, 2),
        )
        for label, attempt, old_ordinal, old_attempt, expected_attempt in cases:
            with self.subTest(case=label):
                snapshot = replace(
                    passing_snapshot(),
                    attempt=attempt,
                    reviews={
                        "api": RepositoryEvidence(SHA["api"], "pass"),
                        "web": RepositoryEvidence(SHA["web"], "fail"),
                    },
                )
                failed_current = WorkflowChild(
                    "PRO-101-WEB-REVIEW", "web", "web", "", "review", 5, attempt,
                    "done", "review:" + "5" * 64, False,
                    evidence_comment_uuid=str(uuid.UUID("5" * 32)),
                    creation_candidate_shas=snapshot.candidate_shas,
                    phase_result="fail",
                    evidence_comment_url="https://example.test/evidence/55555555-5555-5555-5555-555555555555",
                    responsible_repositories=("web",),
                )
                historical = WorkflowChild(
                    "PRO-101-OLD", "api", "api", "", "review", old_ordinal, old_attempt,
                    "in_progress", "review:" + "6" * 64, True,
                )
                self.store.add_state(
                    "PRO-101",
                    snapshot,
                    children=(failed_current, historical),
                    pull_requests=pull_request_targets(),
                )
                self.store.events.clear()

                result = self.workflow.resume_parent("PRO-101")

                self.assertEqual(result.next_action, "repair")
                self.assertEqual(result.created_children, (("web", "repair"),))
                self.assertEqual(
                    self.store.states["PRO-101"].snapshot.attempt,
                    expected_attempt,
                )

    def test_direct_resume_fails_closed_without_mutation_for_future_child_relationship(self):
        snapshot = replace(
            passing_snapshot(),
            reviews={
                "api": RepositoryEvidence(SHA["api"], "pass"),
                "web": RepositoryEvidence(SHA["web"], "fail"),
            },
        )
        terminal_failure = WorkflowChild(
            "PRO-101-WEB-REVIEW", "web", "web", "", "review", 5, 0,
            "done", "review:" + "9" * 64, False,
        )
        future_relationships = {
            "future Stage ordinal": WorkflowChild(
                "PRO-101-FUTURE", "api", "api", "", "review", 6, 0,
                "in_progress", "review:" + "a" * 64, True,
            ),
            "future current-Stage attempt": WorkflowChild(
                "PRO-101-FUTURE", "api", "api", "", "review", 5, 1,
                "in_progress", "review:" + "a" * 64, True,
            ),
        }

        for relationship, future_child in future_relationships.items():
            with self.subTest(relationship=relationship):
                self.store.add_state(
                    "PRO-101",
                    snapshot,
                    children=(terminal_failure, future_child),
                    pull_requests=pull_request_targets(),
                )
                before = self.store.states["PRO-101"]
                self.store.events.clear()

                result = self.workflow.resume_parent("PRO-101")

                self.assertEqual(result.parent_status, "blocked")
                self.assertEqual(result.next_action, "block")
                self.assertIn("child relationship", result.reason)
                self.assertEqual(result.mutation_count, 0)
                self.assertEqual(self.store.states["PRO-101"], before)
                self.assertFalse(
                    any(
                        event[0] in {"create", "status", "merge-state", "write-smoke"}
                        for event in self.store.events
                    )
                )

    def test_wrong_parent_read_blocks_requested_parent_without_mutating_either_parent(self):
        requested = passing_snapshot()
        foreign = ParentSnapshot(affected_repositories=("api",))
        self.store.add_state(
            "PRO-101",
            requested,
            pull_requests=pull_request_targets(),
        )
        self.store.add_state("PRO-999", foreign)
        requested_before = self.store.states["PRO-101"]
        foreign_before = self.store.states["PRO-999"]
        self.store.read_parent_identifier_override = "PRO-999"
        self.store.events.clear()

        result = self.workflow.resume_parent("PRO-101")

        self.assertEqual(result.parent_identifier, "PRO-101")
        self.assertEqual(result.parent_status, "blocked")
        self.assertEqual(result.next_action, "block")
        self.assertIn("wrong parent", result.reason)
        self.assertEqual(result.mutation_count, 0)
        self.assertEqual(self.store.states["PRO-101"], requested_before)
        self.assertEqual(self.store.states["PRO-999"], foreign_before)
        self.assertFalse(any(event[0] == "status" for event in self.store.events))

    def test_failed_implementation_without_gate_evidence_blocks_before_repair(self):
        self.workflow.handle_parent_event("PRO-101", affected=frozenset({"api"}))
        self.store.events.clear()

        result = self.workflow.record_phase_completion(
            completion_for("api", result="fail")
        )

        self.assertEqual(result.completed_child_status, "done")
        self.assertEqual(result.next_action, "block")
        self.assertFalse(any(event[0] == "create" for event in self.store.events))

    def test_replacement_sha_keeps_pr_and_invalidates_all_affected_gates(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            reviews={**snapshot.reviews, "web": RepositoryEvidence(SHA["web"], "fail")},
        )
        failure = WorkflowChild(
            "PRO-101-WEB-REVIEW", "web", "web", "", "review", 5, 0,
            "done", "review:" + "8" * 64, False,
            evidence_comment_uuid=str(uuid.UUID("8" * 32)),
            creation_candidate_shas=snapshot.candidate_shas,
            phase_result="fail",
            evidence_comment_url="https://example.test/evidence/88888888-8888-8888-8888-888888888888",
            responsible_repositories=("web",),
        )
        self.store.add_state(
            "PRO-101",
            snapshot,
            children=(failure,),
            pull_requests=pull_request_targets(),
        )
        repair = self.workflow.resume_parent("PRO-101")
        self.assertEqual(repair.next_action, "repair")
        repair_request = [event for event in self.store.events if event[0] == "create"][-1][2][0]
        state = self.store.states["PRO-101"]
        self.store.states["PRO-101"] = replace(
            state,
            snapshot=replace(
                state.snapshot,
                pull_requests={
                    **state.snapshot.pull_requests,
                    "web": replace(
                        state.snapshot.pull_requests["web"],
                        head_sha=REPLACEMENT_SHA,
                    ),
                },
            ),
        )

        result = self.workflow.record_phase_completion(
            completion_for(
                "web",
                phase="repair",
                attempt=1,
                sha=REPLACEMENT_SHA,
                failure_bundle_digest=repair_request.failure_bundle.digest,
            )
        )

        state = self.store.states["PRO-101"]
        self.assertEqual(state.pull_requests["web"].url, pull_request_targets()["web"].url)
        self.assertEqual(state.snapshot.candidate_shas["web"], REPLACEMENT_SHA)
        self.assertEqual(
            state.snapshot.reviews["web"],
            RepositoryEvidence(REPLACEMENT_SHA, "pending"),
        )
        self.assertEqual(
            state.snapshot.reviews["api"],
            RepositoryEvidence(SHA["api"], "pending"),
        )
        self.assertEqual(
            state.snapshot.qa["web"],
            RepositoryEvidence(REPLACEMENT_SHA, "pending"),
        )
        self.assertEqual(
            state.snapshot.qa["api"],
            RepositoryEvidence(SHA["api"], "pending"),
        )
        self.assertEqual(
            dict(state.snapshot.integration_qa["web-api"].candidate_shas),
            {"api": SHA["api"], "web": REPLACEMENT_SHA},
        )
        self.assertEqual(result.next_action, "dispatch")
        self.assertEqual(
            result.created_children,
            (
                ("api", "review"),
                ("api", "qa"),
                ("web", "review"),
                ("web", "qa"),
                ("web-api", "integration_qa"),
            ),
        )

    def test_replacement_blocks_if_effect_does_not_authoritatively_invalidate_all_gates(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            reviews={**snapshot.reviews, "web": RepositoryEvidence(SHA["web"], "fail")},
        )
        failure = WorkflowChild(
            "PRO-101-WEB-REVIEW", "web", "web", "", "review", 5, 0,
            "done", "review:" + "9" * 64, False,
            evidence_comment_uuid=str(uuid.UUID("9" * 32)),
            creation_candidate_shas=snapshot.candidate_shas,
            phase_result="fail",
            evidence_comment_url="https://example.test/evidence/99999999-9999-9999-9999-999999999999",
            responsible_repositories=("web",),
        )
        self.store.add_state(
            "PRO-101",
            snapshot,
            children=(failure,),
            pull_requests=pull_request_targets(),
        )
        self.workflow.resume_parent("PRO-101")
        repair_request = [event for event in self.store.events if event[0] == "create"][-1][2][0]
        self.store.retain_gates_on_replacement = True
        state = self.store.states["PRO-101"]
        self.store.states["PRO-101"] = replace(
            state,
            snapshot=replace(
                state.snapshot,
                pull_requests={
                    **state.snapshot.pull_requests,
                    "web": replace(
                        state.snapshot.pull_requests["web"],
                        head_sha=REPLACEMENT_SHA,
                    ),
                },
            ),
        )

        result = self.workflow.record_phase_completion(
            completion_for(
                "web",
                phase="repair",
                attempt=1,
                sha=REPLACEMENT_SHA,
                failure_bundle_digest=repair_request.failure_bundle.digest,
            )
        )

        self.assertEqual(result.completed_child_status, "done")
        self.assertEqual(result.parent_status, "blocked")
        self.assertEqual(result.next_action, "block")

    def test_stale_qa_evidence_never_merges(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            qa={**snapshot.qa, "web": RepositoryEvidence(SHA["web"], "pending")},
        )
        qa_child = WorkflowChild(
            "PRO-101-QA",
            "web",
            "web",
            "",
            "qa",
            5,
            0,
            "in_progress",
            "qa:" + "2" * 64,
            True,
        )
        self.store.add_state(
            "PRO-101",
            snapshot,
            children=(qa_child,),
            pull_requests=pull_request_targets(),
        )

        result = self.workflow.record_phase_completion(
            completion_for("web", phase="qa", sha=REPLACEMENT_SHA)
        )

        self.assertEqual(result.parent_status, "blocked")
        self.assertEqual(result.next_action, "block")
        self.assertEqual(self.github.merged, [])

    def test_stale_integration_qa_is_rejected_before_child_completion(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            integration_qa={
                "web-api": GateEvidence(snapshot.candidate_shas, "pending")
            },
        )
        integration_child = WorkflowChild(
            "PRO-101-INTEGRATION",
            "web-api",
            "web",
            "web-api",
            "integration_qa",
            5,
            0,
            "in_progress",
            "qa:" + "8" * 64,
            True,
        )
        self.store.add_state(
            "PRO-101",
            snapshot,
            children=(integration_child,),
            pull_requests=pull_request_targets(),
        )

        result = self.workflow.record_phase_completion(
            completion_for(
                "web",
                phase="integration_qa",
                suite_key="web-api",
                sha=REPLACEMENT_SHA,
                candidate_shas={"api": SHA["api"], "web": REPLACEMENT_SHA},
            )
        )

        self.assertIsNone(result.completed_child_status)
        self.assertEqual(result.next_action, "block")
        self.assertNotIn("done", [event[0] for event in self.store.events])

    def test_integration_qa_completion_must_name_suite_command_repository(self):
        snapshot = replace(
            passing_snapshot(),
            integration_qa={
                "web-api": GateEvidence(passing_snapshot().candidate_shas, "pending")
            },
        )
        integration_child = WorkflowChild(
            "PRO-101-INTEGRATION",
            "web-api",
            "web",
            "web-api",
            "integration_qa",
            5,
            0,
            "in_progress",
            "qa:" + "8" * 64,
            True,
        )
        self.store.add_state(
            "PRO-101",
            snapshot,
            children=(integration_child,),
            pull_requests=pull_request_targets(),
        )

        result = self.workflow.record_phase_completion(
            completion_for(
                "api",
                phase="integration_qa",
                suite_key="web-api",
                candidate_shas=dict(snapshot.candidate_shas),
            )
        )

        self.assertEqual(result.parent_status, "blocked")
        self.assertNotIn("done", [event[0] for event in self.store.events])

    def test_completion_reread_mismatch_blocks_before_child_done(self):
        self.workflow.handle_parent_event("PRO-101", affected=frozenset({"api"}))
        self.store.corrupt_completion_read = True

        result = self.workflow.record_phase_completion(completion_for("api"))

        self.assertEqual(result.parent_status, "blocked")
        self.assertIsNone(result.completed_child_status)
        self.assertNotIn("done", [event[0] for event in self.store.events])

    def test_successful_child_done_with_unobservable_parent_read_is_not_overwritten(self):
        self.workflow.handle_parent_event("PRO-101", affected=frozenset({"api"}))
        self.store.events.clear()
        self.store.fail_reads_after_done = True

        result = self.workflow.record_phase_completion(completion_for("api"))

        state = self.store.states["PRO-101"]
        self.assertEqual(result.next_action, "uncertain")
        self.assertEqual(state.children[0].status, "done")
        self.assertFalse(any(event[0] == "status" for event in self.store.events))

    def test_completion_rereads_unchanged_parent_metadata_before_child_done(self):
        self.workflow.handle_parent_event("PRO-101", affected=frozenset({"api"}))
        self.store.events.clear()
        self.store.change_after_completion_read = True

        result = self.workflow.record_phase_completion(completion_for("api"))

        self.assertEqual(result.parent_status, "blocked")
        self.assertIsNone(result.completed_child_status)
        self.assertNotIn("done", [event[0] for event in self.store.events])

    def test_merge_stops_before_first_changed_head(self):
        self.store.add_state(
            "PRO-101",
            passing_snapshot(),
            pull_requests=pull_request_targets(),
        )
        self.github.heads["api"] = "f" * 40

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.parent_status, "blocked")
        self.assertEqual(self.github.merged, [])
        self.assertEqual(
            [event[1] for event in self.store.events if event[0] == "github-read-pr"],
            ["api", "web"],
        )

    def test_direct_merge_waits_without_mutation_for_active_current_stage(self):
        active_child = WorkflowChild(
            "PRO-101-REVIEW", "api", "api", "", "review", 5, 0,
            "in_review", "review:" + "7" * 64, True,
        )
        self.store.add_state(
            "PRO-101",
            passing_snapshot(),
            children=(active_child,),
            pull_requests=pull_request_targets(),
        )
        self.store.events.clear()

        with patch(
            "multica_delivery.core.workflow.decide_parent_action",
            return_value=ParentDecision(DecisionKind.BLOCK, "merge coherence block"),
        ):
            result = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.next_action, "wait")
        self.assertEqual(result.mutation_count, 0)
        self.assertEqual(self.github.merged, [])
        self.assertFalse(
            any(event[0] in {"merge-state", "github-merge"} for event in self.store.events)
        )

    def test_merge_preflights_every_pr_before_first_mutation(self):
        self.store.add_state(
            "PRO-101",
            passing_snapshot(),
            pull_requests=pull_request_targets(),
        )

        self.workflow.execute_merge_plan("PRO-101")

        event_names = [event[0] for event in self.store.events]
        first_mutation = event_names.index("merge-state")
        self.assertLess(event_names.index("github-read-pr"), first_mutation)
        self.assertEqual(event_names[:first_mutation].count("github-read-pr"), 2)
        self.assertEqual(event_names[:first_mutation].count("github-read-checks"), 2)

    def test_premerged_strict_subset_blocks_without_more_merges(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merged_shas={"api": SHA["api"]},
            pull_requests={
                **snapshot.pull_requests,
                "api": PullRequestEvidence(
                    SHA["api"],
                    "merged",
                    True,
                    True,
                    SHA["api"],
                ),
            },
        )
        self.store.add_state(
            "PRO-101",
            snapshot,
            pull_requests=pull_request_targets(),
        )

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.parent_status, "blocked")
        self.assertEqual(self.github.merged, [])

    def test_mid_sequence_merge_failure_records_partial_merge_and_stops(self):
        self.store.add_state(
            "PRO-101",
            passing_snapshot(),
            pull_requests=pull_request_targets(),
        )
        self.github.fail_on = "web"

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.parent_status, "blocked")
        self.assertEqual(result.merge_state, "partial")
        self.assertEqual(self.github.merged, [("codeExploreHub/sample-commerce-api", 12)])
        self.assertEqual(self.store.rollback_calls, [])

    def test_commit_then_error_rereads_merged_pr_and_records_exact_prefix(self):
        self.store.add_state(
            "PRO-101", passing_snapshot(), pull_requests=pull_request_targets()
        )
        self.github.commit_then_error_on = "api"
        self.github.merged_shas["api"] = "1" * 40

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.merge_state, "merged")
        progress = [
            event for event in self.store.events
            if event[0] == "merge-state" and event[1] == "merging" and event[2]
        ]
        self.assertEqual(progress[0][2], {"api": "1" * 40})
        self.assertEqual(
            dict(self.store.states["PRO-101"].snapshot.merged_shas),
            {"api": "1" * 40, "web": SHA["web"]},
        )

    def test_malformed_merge_ack_rereads_merged_pr_before_recording_progress(self):
        self.store.add_state(
            "PRO-101", passing_snapshot(), pull_requests=pull_request_targets()
        )
        self.github.malformed_ack_on = "api"
        self.github.merged_shas["api"] = "2" * 40

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.merge_state, "merged")
        self.assertEqual(
            self.store.states["PRO-101"].snapshot.merged_shas["api"],
            "2" * 40,
        )

    def test_valid_merge_ack_cannot_replace_authoritative_merged_pr_read(self):
        self.store.add_state(
            "PRO-101", passing_snapshot(), pull_requests=pull_request_targets()
        )
        self.github.ack_without_commit_on = "api"

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.next_action, "block")
        self.assertEqual(self.store.states["PRO-101"].snapshot.merged_shas, {})

    def test_unreadable_post_merge_state_is_uncertain_not_assumed_uncommitted(self):
        self.store.add_state(
            "PRO-101", passing_snapshot(), pull_requests=pull_request_targets()
        )
        self.github.fail_merged_reread_on = "api"

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.next_action, "uncertain")
        self.assertEqual(
            self.store.states["PRO-101"].snapshot.merge_state, "merging"
        )
        self.assertFalse(any(event[0] == "status" for event in self.store.events))

    def test_retry_recovers_commit_after_immediate_authoritative_read_is_unavailable(self):
        self.store.add_state(
            "PRO-101", passing_snapshot(), pull_requests=pull_request_targets()
        )
        api_merge_sha = "1" * 40
        web_merge_sha = "2" * 40
        self.github.merged_shas = {
            "api": api_merge_sha,
            "web": web_merge_sha,
        }
        self.github.fail_merged_rereads_remaining["api"] = 1

        first = self.workflow.execute_merge_plan("PRO-101")
        second = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(first.next_action, "uncertain")
        self.assertEqual(second.merge_state, "merged")
        self.assertEqual(
            self.github.merged.count(("codeExploreHub/sample-commerce-api", 12)),
            1,
        )
        self.assertEqual(
            dict(self.store.states["PRO-101"].snapshot.merged_shas),
            {"api": api_merge_sha, "web": web_merge_sha},
        )

    def test_resumed_merge_unavailable_prefix_read_is_uncertain_without_mutation(self):
        self.store.add_state(
            "PRO-101", merging_snapshot(), pull_requests=pull_request_targets()
        )
        self.github.read_failures_remaining["api"] = 1

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.next_action, "uncertain")
        self.assertEqual(
            dict(self.store.states["PRO-101"].snapshot.merged_shas),
            {},
        )
        self.assertEqual(self.github.merged, [])
        self.assertFalse(
            any(
                event[0] in {"merge-state", "status", "github-merge"}
                for event in self.store.events
            )
        )

    def test_resumed_merge_malformed_prefix_read_is_uncertain_without_mutation(self):
        self.store.add_state(
            "PRO-101", merging_snapshot(), pull_requests=pull_request_targets()
        )
        self.github.malformed_read_on = "api"

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.next_action, "uncertain")
        self.assertEqual(self.github.merged, [])
        self.assertFalse(any(event[0] == "merge-state" for event in self.store.events))

    def test_resumed_merge_wrong_candidate_head_is_uncertain_without_mutation(self):
        self.store.add_state(
            "PRO-101", merging_snapshot(), pull_requests=pull_request_targets()
        )
        self.github.heads["api"] = "f" * 40

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.next_action, "uncertain")
        self.assertEqual(self.github.merged, [])
        self.assertFalse(any(event[0] == "merge-state" for event in self.store.events))

    def test_resumed_merge_missing_remote_merge_sha_is_uncertain_without_mutation(self):
        self.store.add_state(
            "PRO-101", merging_snapshot(), pull_requests=pull_request_targets()
        )
        self.github.committed.add("api")
        self.github.missing_merge_sha_on = "api"

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.next_action, "uncertain")
        self.assertEqual(self.github.merged, [])
        self.assertFalse(any(event[0] == "merge-state" for event in self.store.events))

    def test_resumed_merge_persisted_prefix_longer_than_remote_is_uncertain(self):
        api_merge_sha = "1" * 40
        self.store.add_state(
            "PRO-101",
            merging_snapshot({"api": api_merge_sha}),
            pull_requests=pull_request_targets(),
        )
        self.github.merged_shas["api"] = api_merge_sha

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.next_action, "uncertain")
        self.assertEqual(
            dict(self.store.states["PRO-101"].snapshot.merged_shas),
            {"api": api_merge_sha},
        )
        self.assertEqual(self.github.merged, [])
        self.assertFalse(any(event[0] == "merge-state" for event in self.store.events))

    def test_resumed_merge_non_contiguous_remote_prefix_is_uncertain(self):
        self.store.add_state(
            "PRO-101", merging_snapshot(), pull_requests=pull_request_targets()
        )
        self.github.committed.add("web")

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.next_action, "uncertain")
        self.assertEqual(self.github.merged, [])
        self.assertFalse(any(event[0] == "merge-state" for event in self.store.events))

    def test_resumed_merge_recovers_complete_remote_prefix_without_duplicate_merge(self):
        api_merge_sha = "1" * 40
        web_merge_sha = "2" * 40
        self.store.add_state(
            "PRO-101", merging_snapshot(), pull_requests=pull_request_targets()
        )
        self.github.merged_shas = {
            "api": api_merge_sha,
            "web": web_merge_sha,
        }
        self.github.committed.update({"api", "web"})

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.merge_state, "merged")
        self.assertEqual(self.github.merged, [])
        self.assertEqual(
            dict(self.store.states["PRO-101"].snapshot.merged_shas),
            {"api": api_merge_sha, "web": web_merge_sha},
        )

    def test_all_merges_record_exact_candidate_map_then_route_smoke(self):
        self.store.add_state(
            "PRO-101",
            passing_snapshot(),
            pull_requests=pull_request_targets(),
        )

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.merge_state, "merged")
        self.assertEqual(result.next_action, "smoke")
        self.assertEqual(
            dict(self.store.states["PRO-101"].snapshot.merged_shas),
            {"api": SHA["api"], "web": SHA["web"]},
        )

    def test_merge_persists_authoritative_merge_shas_for_default_branch_smoke(self):
        self.store.add_state(
            "PRO-101",
            passing_snapshot(),
            pull_requests=pull_request_targets(),
        )
        merged = {"api": "1" * 40, "web": "2" * 40}
        self.github.merged_shas = merged
        workflow, _manager, backend = self.owned_smoke_workflow()
        for repository, sha in merged.items():
            backend.heads[self.manifest.repositories[repository].local_path] = sha

        result = workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.merge_state, "merged")
        self.assertEqual(dict(self.store.states["PRO-101"].snapshot.merged_shas), merged)
        self.assertEqual(
            dict(self.store.states["PRO-101"].snapshot.smoke_reads[0].merged_shas),
            merged,
        )

    def test_merge_transitions_use_unique_monotonic_keys_and_ordinals(self):
        self.store.add_state(
            "PRO-101",
            passing_snapshot(),
            pull_requests=pull_request_targets(),
        )

        self.workflow.execute_merge_plan("PRO-101")

        writes = [event for event in self.store.events if event[0] == "merge-state"]
        self.assertEqual([event[4] for event in writes], [6, 7, 8, 9])
        self.assertEqual(len({event[3] for event in writes}), 4)

    def test_merge_replay_converges_after_progress_write_read_failure(self):
        self.store.add_state(
            "PRO-101",
            passing_snapshot(),
            pull_requests=pull_request_targets(),
        )
        self.store.inject_merge_progress_read_failure = True

        first = self.workflow.execute_merge_plan("PRO-101")
        ordinal_after_uncertain_write = self.store.states["PRO-101"].metadata.stage_ordinal
        second = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(first.next_action, "uncertain")
        self.assertEqual(ordinal_after_uncertain_write, 7)
        self.assertEqual(second.merge_state, "merged")
        self.assertEqual(
            self.github.merged,
            [
                ("codeExploreHub/sample-commerce-api", 12),
                ("codeExploreHub/sample-commerce-web", 14),
            ],
        )
        writes = [event for event in self.store.events if event[0] == "merge-state"]
        ordinals = [event[4] for event in writes]
        self.assertEqual(ordinals, sorted(set(ordinals)))

    def test_remaining_preflight_failure_after_committed_prefix_is_partial_and_replay_noops(self):
        self.store.add_state(
            "PRO-101",
            passing_snapshot(),
            pull_requests=pull_request_targets(),
        )
        authoritative_api_sha = "1" * 40
        self.github.merged_shas["api"] = authoritative_api_sha
        self.store.inject_merge_progress_read_failure = True

        first = self.workflow.execute_merge_plan("PRO-101")
        self.github.checks["web"] = False
        second = self.workflow.execute_merge_plan("PRO-101")
        mutation_events_after_partial = [
            event
            for event in self.store.events
            if event[0] in {"merge-state", "status", "github-merge"}
        ]
        third = self.workflow.execute_merge_plan("PRO-101")

        state = self.store.states["PRO-101"]
        mutation_events_after_replay = [
            event
            for event in self.store.events
            if event[0] in {"merge-state", "status", "github-merge"}
        ]
        self.assertEqual(first.next_action, "uncertain")
        self.assertEqual(second.parent_status, "blocked")
        self.assertEqual(second.merge_state, "partial")
        self.assertEqual(third.next_action, "block")
        self.assertEqual(third.merge_state, "partial")
        self.assertEqual(state.snapshot.merge_state, "partial")
        self.assertEqual(dict(state.snapshot.merged_shas), {"api": authoritative_api_sha})
        self.assertEqual(
            self.github.merged,
            [("codeExploreHub/sample-commerce-api", 12)],
        )
        self.assertEqual(mutation_events_after_replay, mutation_events_after_partial)

    def test_all_merges_start_owned_smoke_when_executor_is_configured(self):
        self.store.add_state(
            "PRO-101",
            passing_snapshot(),
            pull_requests=pull_request_targets(),
        )
        workflow, _manager, backend = self.owned_smoke_workflow()

        result = workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.next_action, "smoke")
        self.assertTrue(
            any(
                argv == self.manifest.repositories["api"].commands["smoke"]
                for argv, _cwd in backend.calls
            )
        )
        self.assertEqual(len(self.store.states["PRO-101"].snapshot.smoke_reads), 1)

    def test_merged_prs_require_two_authoritative_identical_smoke_reads(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        self.store.add_state(
            "PRO-101",
            snapshot,
            pull_requests=pull_request_targets(),
        )

        workflow, _manager, _backend = self.owned_smoke_workflow()
        first = workflow.resume_parent("PRO-101")
        after_one = workflow.execute_smoke("PRO-101")
        after_two = workflow.execute_smoke("PRO-101")

        self.assertEqual(first.next_action, "smoke")
        self.assertEqual(after_one.parent_status, "in_progress")
        self.assertEqual(after_one.next_action, "smoke")
        self.assertEqual(after_two.parent_status, "done")

    def test_parent_done_write_read_failure_is_uncertain_without_stale_block(self):
        snapshot = passing_snapshot()
        first_smoke = passing_smoke(observation_id=SMOKE_OBSERVATION["first"])
        second_smoke = passing_smoke(observation_id=SMOKE_OBSERVATION["second"])
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
            smoke_reads=(first_smoke, second_smoke),
        )
        self.store.add_state("PRO-101", snapshot, pull_requests=pull_request_targets())
        self.store.fail_parent_reads_after_parent_done = True
        self.store.events.clear()

        first = self.workflow.resume_parent("PRO-101")
        second = self.workflow.resume_parent("PRO-101")

        state = self.store.states["PRO-101"]
        status_events = [event for event in self.store.events if event[0] == "status"]
        self.assertEqual(first.next_action, "uncertain")
        self.assertEqual(second.next_action, "noop")
        self.assertEqual(state.parent_status, "done")
        self.assertEqual([event[2] for event in status_events], ["done"])
        self.assertIn(state.metadata.last_action, state.applied_action_keys)

    def test_incomplete_parent_done_transition_stays_uncertain_without_rewrite(self):
        snapshot = passing_snapshot()
        first_smoke = passing_smoke(observation_id=SMOKE_OBSERVATION["first"])
        second_smoke = passing_smoke(observation_id=SMOKE_OBSERVATION["second"])
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
            smoke_reads=(first_smoke, second_smoke),
        )
        self.store.add_state("PRO-101", snapshot, pull_requests=pull_request_targets())
        metadata_before = self.store.states["PRO-101"].metadata
        self.store.write_incomplete_parent_done = True
        self.store.events.clear()

        first = self.workflow.resume_parent("PRO-101")
        second = self.workflow.resume_parent("PRO-101")

        state = self.store.states["PRO-101"]
        status_events = [event for event in self.store.events if event[0] == "status"]
        self.assertEqual(first.next_action, "uncertain")
        self.assertEqual(second.next_action, "uncertain")
        self.assertEqual(state.parent_status, "done")
        self.assertEqual(state.metadata, metadata_before)
        self.assertEqual(len(status_events), 1)
        self.assertNotIn(status_events[0][4], state.applied_action_keys)

    def test_smoke_read_must_name_the_exact_authoritative_merged_sha_map(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        self.store.add_state(
            "PRO-101",
            snapshot,
            pull_requests=pull_request_targets(),
        )
        self.store.events.clear()

        result = self.workflow.record_smoke_read(
            "PRO-101",
            passing_smoke(
                observation_id=SMOKE_OBSERVATION["first"],
                shas={"api": "f" * 40, "web": SHA["web"]},
            ),
        )

        self.assertEqual(result.parent_status, "blocked")
        self.assertFalse(any(event[0] == "write-smoke" for event in self.store.events))

    def test_failed_post_merge_smoke_blocks_without_creating_repair_work(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        self.store.add_state(
            "PRO-101",
            snapshot,
            pull_requests=pull_request_targets(),
        )
        workflow, _manager, backend = self.owned_smoke_workflow()
        backend.fail_argv.add(self.manifest.repositories["web"].commands["smoke"])
        self.store.events.clear()

        result = workflow.execute_smoke("PRO-101")

        self.assertEqual(result.parent_status, "blocked")
        self.assertEqual(result.next_action, "block")
        self.assertFalse(any(event[0] == "create" for event in self.store.events))

    def test_successful_smoke_write_with_unobservable_reread_is_not_overwritten(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        self.store.add_state("PRO-101", snapshot, pull_requests=pull_request_targets())
        workflow, _manager, _backend = self.owned_smoke_workflow()
        self.store.fail_parent_reads_after_smoke_write = True
        self.store.events.clear()

        result = workflow.execute_smoke("PRO-101")

        state = self.store.states["PRO-101"]
        self.assertEqual(result.next_action, "uncertain")
        self.assertEqual(len(state.snapshot.smoke_reads), 1)
        self.assertFalse(any(event[0] == "status" for event in self.store.events))

    def test_committed_smoke_replay_noops_until_a_new_observation_arrives(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        self.store.add_state("PRO-101", snapshot, pull_requests=pull_request_targets())
        workflow, _manager, _backend = self.owned_smoke_workflow()
        self.store.fail_parent_reads_after_smoke_write = True
        self.store.events.clear()

        first = workflow.execute_smoke("PRO-101")
        committed = self.store.states["PRO-101"]
        first_observation = committed.snapshot.smoke_reads[0]
        committed_ordinal = committed.metadata.stage_ordinal
        committed_action_key = committed.metadata.last_action
        replay = workflow.record_smoke_read("PRO-101", first_observation)
        after_replay = self.store.states["PRO-101"]
        writes_after_replay = [
            event for event in self.store.events if event[0] == "write-smoke"
        ]
        second = workflow.execute_smoke("PRO-101")
        final = self.store.states["PRO-101"]
        second_observation = final.snapshot.smoke_reads[-1]
        second_action_key = [
            event[2] for event in self.store.events if event[0] == "write-smoke"
        ][-1]
        completed_replay = workflow.record_smoke_read(
            "PRO-101",
            second_observation,
        )

        self.assertEqual(first.next_action, "uncertain")
        self.assertEqual(replay.next_action, "noop")
        self.assertEqual(replay.action_key, committed_action_key)
        self.assertEqual(after_replay.parent_status, "in_progress")
        self.assertEqual(after_replay.metadata.stage_ordinal, committed_ordinal)
        self.assertEqual(after_replay.snapshot.smoke_reads, (first_observation,))
        self.assertEqual(len(writes_after_replay), 1)
        self.assertEqual(second.parent_status, "done")
        self.assertEqual(completed_replay.next_action, "noop")
        self.assertEqual(completed_replay.action_key, second_action_key)
        self.assertEqual(
            final.snapshot.smoke_reads,
            (first_observation, second_observation),
        )
        self.assertEqual(
            len([event for event in self.store.events if event[0] == "write-smoke"]),
            2,
        )

    def test_new_smoke_identity_after_done_is_noop_while_history_still_replays(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        self.store.add_state("PRO-101", snapshot, pull_requests=pull_request_targets())
        workflow, _manager, _backend = self.owned_smoke_workflow()
        new_observation = passing_smoke(
            observation_id=SMOKE_OBSERVATION["third"]
        )
        workflow.execute_smoke("PRO-101")
        first_observation = self.store.states["PRO-101"].snapshot.smoke_reads[0]
        workflow.execute_smoke("PRO-101")
        completed = self.store.states["PRO-101"]
        second_observation = completed.snapshot.smoke_reads[-1]
        second_action_key = [
            event[2] for event in self.store.events if event[0] == "write-smoke"
        ][-1]
        self.store.events.clear()

        replay = workflow.record_smoke_read("PRO-101", second_observation)
        after_replay = self.store.states["PRO-101"]
        rejected = workflow.record_smoke_read("PRO-101", new_observation)

        self.assertEqual(replay.next_action, "noop")
        self.assertEqual(replay.action_key, second_action_key)
        self.assertEqual(after_replay, completed)
        self.assertEqual(rejected.parent_status, "done")
        self.assertEqual(rejected.next_action, "noop")
        self.assertEqual(self.store.states["PRO-101"], completed)
        self.assertFalse(
            any(event[0] in {"write-smoke", "status"} for event in self.store.events)
        )

    def test_new_smoke_identity_while_blocked_is_noop_without_mutation(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        self.store.add_state(
            "PRO-101",
            snapshot,
            status="blocked",
            pull_requests=pull_request_targets(),
        )
        before = self.store.states["PRO-101"]
        self.store.events.clear()

        result = self.workflow.record_smoke_read(
            "PRO-101",
            passing_smoke(observation_id=SMOKE_OBSERVATION["first"]),
        )

        self.assertEqual(result.parent_status, "blocked")
        self.assertEqual(result.next_action, "noop")
        self.assertEqual(self.store.states["PRO-101"], before)
        self.assertFalse(
            any(event[0] in {"write-smoke", "status"} for event in self.store.events)
        )

    def test_new_smoke_identity_during_human_wait_is_noop_without_mutation(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        self.store.add_state(
            "PRO-101",
            snapshot,
            human_wait=True,
            pull_requests=pull_request_targets(),
        )
        before = self.store.states["PRO-101"]
        self.store.events.clear()

        result = self.workflow.record_smoke_read(
            "PRO-101",
            passing_smoke(observation_id=SMOKE_OBSERVATION["first"]),
        )

        self.assertEqual(result.parent_status, "in_progress")
        self.assertEqual(result.next_action, "noop")
        self.assertEqual(self.store.states["PRO-101"], before)
        self.assertFalse(
            any(event[0] in {"write-smoke", "status"} for event in self.store.events)
        )

    def test_new_smoke_identity_requires_the_current_pure_smoke_decision(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        self.store.add_state("PRO-101", snapshot, pull_requests=pull_request_targets())
        workflow, _manager, backend = self.owned_smoke_workflow()
        backend.fail_argv.add(self.manifest.repositories["web"].commands["smoke"])
        workflow.execute_smoke("PRO-101")
        self.store.states["PRO-101"] = replace(
            self.store.states["PRO-101"],
            parent_status="in_progress",
        )
        before = self.store.states["PRO-101"]
        self.store.events.clear()

        result = workflow.record_smoke_read(
            "PRO-101",
            passing_smoke(observation_id=SMOKE_OBSERVATION["second"]),
        )

        self.assertEqual(result.parent_status, "blocked")
        self.assertEqual(result.next_action, "block")
        self.assertEqual(self.store.states["PRO-101"], before)
        self.assertFalse(
            any(event[0] in {"write-smoke", "status"} for event in self.store.events)
        )

    def test_conflicting_smoke_payload_for_existing_identity_blocks_before_write(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        self.store.add_state("PRO-101", snapshot, pull_requests=pull_request_targets())
        workflow, _manager, _backend = self.owned_smoke_workflow()
        workflow.execute_smoke("PRO-101")
        observation = self.store.states["PRO-101"].snapshot.smoke_reads[0]
        conflict = replace(
            observation,
            repository_results={"api": "pass", "web": "fail"},
        )
        self.store.events.clear()

        result = workflow.record_smoke_read("PRO-101", conflict)

        state = self.store.states["PRO-101"]
        self.assertEqual(result.next_action, "block")
        self.assertEqual(state.snapshot.smoke_reads, (observation,))
        self.assertFalse(any(event[0] == "write-smoke" for event in self.store.events))

    def test_smoke_evidence_without_its_parent_transition_is_uncertain_and_not_replayed(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        self.store.add_state("PRO-101", snapshot, pull_requests=pull_request_targets())
        workflow, _manager, _backend = self.owned_smoke_workflow()
        metadata_before = self.store.states["PRO-101"].metadata
        self.store.write_smoke_evidence_without_transition = True
        self.store.events.clear()

        first = workflow.execute_smoke("PRO-101")
        smoke = self.store.states["PRO-101"].snapshot.smoke_reads[0]
        second = workflow.record_smoke_read("PRO-101", smoke)

        state = self.store.states["PRO-101"]
        self.assertEqual(first.next_action, "uncertain")
        self.assertEqual(second.next_action, "uncertain")
        self.assertEqual(state.snapshot.smoke_reads, (smoke,))
        self.assertEqual(state.metadata, metadata_before)
        self.assertEqual(
            len([event for event in self.store.events if event[0] == "write-smoke"]),
            1,
        )
        self.assertFalse(any(event[0] == "status" for event in self.store.events))

    def test_smoke_execution_receives_exact_merged_sha_map(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        self.store.add_state("PRO-101", snapshot, pull_requests=pull_request_targets())
        workflow, _manager, backend = self.owned_smoke_workflow()

        result = workflow.execute_smoke("PRO-101")

        self.assertEqual(result.next_action, "smoke")
        recorded = self.store.states["PRO-101"].snapshot.smoke_reads[0]
        self.assertEqual(
            dict(recorded.merged_shas),
            {"api": SHA["api"], "web": SHA["web"]},
        )
        self.assertTrue(backend.calls)

    def test_direct_smoke_preserves_all_merged_evidence_blocks_without_mutation(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        corruptions = {
            "missing implementation": replace(snapshot, children={}),
            "missing review": replace(snapshot, reviews={}),
            "missing repository QA": replace(snapshot, qa={}),
            "missing integration QA": replace(snapshot, integration_qa={}),
            "stale implementation": replace(
                snapshot,
                children={
                    **snapshot.children,
                    "web": RepositoryEvidence("d" * 40, "pass"),
                },
            ),
        }
        expected_reason = (
            "merged work lacks complete, current-SHA PASS pre-merge implementation, review, "
            "QA, or integration evidence; human action is required"
        )

        for corruption, corrupted in corruptions.items():
            with self.subTest(corruption=corruption):
                self.store.add_state(
                    "PRO-101",
                    corrupted,
                    pull_requests=pull_request_targets(),
                )
                before = self.store.states["PRO-101"]
                workflow = GenericWorkflow(
                    self.manifest,
                    self.store,
                    self.store,
                    github=self.github,
                )
                self.store.events.clear()

                result = workflow.execute_smoke("PRO-101")

                self.assertEqual(result.next_action, "block")
                self.assertEqual(result.reason, expected_reason)
                self.assertEqual(result.mutation_count, 0)
                self.assertEqual(self.store.states["PRO-101"], before)
                self.assertFalse(
                    any(event[0] in {"status", "write-smoke"} for event in self.store.events)
                )

    def test_direct_smoke_waits_without_execution_for_active_current_stage(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        active_child = WorkflowChild(
            "PRO-101-QA", "api", "api", "", "qa", 5, 0,
            "in_progress", "qa:" + "7" * 64, True,
        )
        self.store.add_state(
            "PRO-101",
            snapshot,
            children=(active_child,),
            pull_requests=pull_request_targets(),
        )
        workflow = GenericWorkflow(
            self.manifest,
            self.store,
            self.store,
            github=self.github,
        )
        self.store.events.clear()

        result = workflow.execute_smoke("PRO-101")

        self.assertEqual(result.next_action, "wait")
        self.assertEqual(result.mutation_count, 0)
        self.assertFalse(any(event[0] == "write-smoke" for event in self.store.events))

    def test_direct_smoke_record_waits_without_mutation_for_active_current_stage(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        active_child = WorkflowChild(
            "PRO-101-QA", "api", "api", "", "qa", 5, 0,
            "in_progress", "qa:" + "8" * 64, True,
        )
        self.store.add_state(
            "PRO-101",
            snapshot,
            children=(active_child,),
            pull_requests=pull_request_targets(),
        )
        self.store.events.clear()

        result = self.workflow.record_smoke_read(
            "PRO-101",
            passing_smoke(observation_id=SMOKE_OBSERVATION["first"]),
        )

        self.assertEqual(result.next_action, "block")
        self.assertEqual(result.mutation_count, 0)
        self.assertEqual(self.store.states["PRO-101"].snapshot.smoke_reads, ())
        self.assertFalse(any(event[0] == "write-smoke" for event in self.store.events))

    def test_smoke_executor_cannot_replace_the_authoritative_observation_identity(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        self.store.add_state("PRO-101", snapshot, pull_requests=pull_request_targets())
        smoke = FakeSmokeExecutor(
            passing_smoke(observation_id=SMOKE_OBSERVATION["first"]),
            bind_observation_id=False,
        )
        with self.assertRaisesRegex(TypeError, "OwnedSmokeExecutor"):
            GenericWorkflow(
                self.manifest,
                self.store,
                self.store,
                github=self.github,
                smoke_executor=smoke,
            )

    def test_owned_smoke_rejects_arbitrary_self_reporting_runner(self):
        with self.assertRaises(TypeError):
            OwnedSmokeExecutor(
                self.manifest,
                FakeOwnedProcessManager(self.manifest),
                SelfReportingFakeRunner(),
            )

    def test_owned_smoke_rejects_nonconcrete_process_manager(self):
        with self.assertRaisesRegex(TypeError, "concrete manifest-bound ProcessManager"):
            OwnedSmokeExecutor(
                self.manifest,
                object(),  # type: ignore[arg-type]
            )

    def test_service_startup_failure_returns_only_blocked_nonauthoritative_evidence(self):
        manager = FakeOwnedProcessManager(self.manifest)
        manager.backend.healthy = False
        backend = MutableClosedCommandBackend(self.manifest)
        executor = OwnedSmokeExecutor(
            self.manifest,
            manager,
            LocalExactShaCommandRunner(self.manifest, backend),
        )

        result = executor.execute(
            "PRO-101",
            ("api", "web"),
            {"api": SHA["api"], "web": SHA["web"]},
            action_key="smoke:" + "7" * 64,
        )

        self.assertFalse(result.authoritative)
        self.assertEqual(set(result.repository_results.values()), {"blocked"})
        self.assertEqual(set(result.integration_results.values()), {"blocked"})
        self.assertNotIn("pending", result.repository_results.values())

    def test_execute_smoke_persists_startup_failure_then_human_blocks(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        self.store.add_state("PRO-101", snapshot, pull_requests=pull_request_targets())
        manager = FakeOwnedProcessManager(self.manifest)
        manager.backend.healthy = False
        backend = MutableClosedCommandBackend(self.manifest)
        workflow = GenericWorkflow(
            self.manifest,
            self.store,
            self.store,
            github=self.github,
            smoke_executor=OwnedSmokeExecutor(
                self.manifest,
                manager,
                LocalExactShaCommandRunner(self.manifest, backend),
            ),
        )
        self.store.events.clear()

        result = workflow.execute_smoke("PRO-101")

        recorded = self.store.states["PRO-101"].snapshot.smoke_reads[0]
        self.assertEqual(result.next_action, "block")
        self.assertEqual(self.store.states["PRO-101"].parent_status, "blocked")
        self.assertFalse(recorded.authoritative)
        self.assertEqual(set(recorded.repository_results.values()), {"blocked"})

    def test_owned_smoke_rejects_concrete_runner_subclass_before_any_effect(self):
        manager = FakeOwnedProcessManager(self.manifest)
        backend = MutableClosedCommandBackend(self.manifest)

        with self.assertRaises(TypeError):
            OwnedSmokeExecutor(
                self.manifest,
                manager,
                SelfReportingConcreteRunner(self.manifest, backend),
            )

        self.assertEqual(backend.calls, [])
        self.assertEqual(manager.backend.started, [])

    def test_owned_smoke_rejects_runner_bound_to_another_manifest_object(self):
        manager = FakeOwnedProcessManager(self.manifest)
        backend = MutableClosedCommandBackend(self.manifest)
        equivalent_manifest = replace(self.manifest)

        with self.assertRaises(TypeError):
            OwnedSmokeExecutor(
                self.manifest,
                manager,
                LocalExactShaCommandRunner(equivalent_manifest, backend),
            )

        self.assertEqual(backend.calls, [])
        self.assertEqual(manager.backend.started, [])

    def test_owned_smoke_runner_public_attribute_cannot_be_replaced(self):
        manager = FakeOwnedProcessManager(self.manifest)
        backend = MutableClosedCommandBackend(self.manifest)
        executor = OwnedSmokeExecutor(
            self.manifest,
            manager,
            LocalExactShaCommandRunner(self.manifest, backend),
        )

        with self.assertRaises((AttributeError, TypeError)):
            executor.command_runner = SelfReportingConcreteRunner(
                self.manifest, backend
            )

        self.assertEqual(backend.calls, [])
        self.assertEqual(manager.backend.started, [])

    def test_owned_smoke_revalidates_privately_stored_runner_before_execute(self):
        manager = FakeOwnedProcessManager(self.manifest)
        backend = MutableClosedCommandBackend(self.manifest)
        executor = OwnedSmokeExecutor(
            self.manifest,
            manager,
            LocalExactShaCommandRunner(self.manifest, backend),
        )
        with self.assertRaises(AttributeError):
            executor._command_runner = SelfReportingConcreteRunner(
                self.manifest, backend
            )

        self.assertEqual(backend.calls, [])
        self.assertEqual(manager.backend.started, [])

    def test_owned_smoke_revalidates_runner_manifest_identity_before_execute(self):
        manager = FakeOwnedProcessManager(self.manifest)
        backend = MutableClosedCommandBackend(self.manifest)
        runner = LocalExactShaCommandRunner(self.manifest, backend)
        executor = OwnedSmokeExecutor(self.manifest, manager, runner)
        with self.assertRaises(AttributeError):
            runner.manifest = replace(self.manifest)

        self.assertEqual(backend.calls, [])
        self.assertEqual(manager.backend.started, [])

    def test_owned_smoke_authority_bindings_cannot_be_mutated_into_matching_pair(self):
        manager = FakeOwnedProcessManager(self.manifest)
        backend = MutableClosedCommandBackend(self.manifest)
        executor = OwnedSmokeExecutor(
            self.manifest,
            manager,
            LocalExactShaCommandRunner(self.manifest, backend),
        )
        other_manifest = replace(self.manifest)
        other_runner = LocalExactShaCommandRunner(
            other_manifest, MutableClosedCommandBackend(other_manifest)
        )

        for attribute, replacement in (
            ("manifest", other_manifest),
            ("_command_runner", other_runner),
            ("process_manager", FakeOwnedProcessManager(self.manifest)),
        ):
            with self.subTest(attribute=attribute):
                with self.assertRaises(AttributeError):
                    setattr(executor, attribute, replacement)

        self.assertFalse(hasattr(executor, "__dict__"))
        self.assertEqual(backend.calls, [])
        self.assertEqual(manager.backend.started, [])

    def test_owned_smoke_constructs_concrete_runner_when_omitted(self):
        executor = OwnedSmokeExecutor(
            self.manifest,
            FakeOwnedProcessManager(self.manifest),
        )

        self.assertIs(type(executor._command_runner), LocalExactShaCommandRunner)

    def test_concrete_runner_blocks_stale_head_before_service_startup(self):
        manager = FakeOwnedProcessManager(self.manifest)
        backend = MutableClosedCommandBackend(self.manifest)
        backend.heads[self.manifest.repositories["web"].local_path] = "f" * 40
        executor = OwnedSmokeExecutor(
            self.manifest,
            manager,
            LocalExactShaCommandRunner(self.manifest, backend),
        )

        result = executor.execute(
            "PRO-101", ("api", "web"), {"api": SHA["api"], "web": SHA["web"]},
            action_key="smoke:" + "7" * 64,
        )

        self.assertEqual(manager.backend.started, [])
        self.assertFalse(result.authoritative)
        self.assertNotIn("pass", result.repository_results.values())
        self.assertNotIn("pass", result.integration_results.values())

    def test_execute_smoke_persists_partial_stale_head_evidence_then_blocks_named_repository(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        self.store.add_state(
            "PRO-101", snapshot, pull_requests=pull_request_targets()
        )
        manager = FakeOwnedProcessManager(self.manifest)
        backend = MutableClosedCommandBackend(self.manifest)
        backend.heads[self.manifest.repositories["web"].local_path] = "f" * 40
        smoke_executor = OwnedSmokeExecutor(
            self.manifest,
            manager,
            LocalExactShaCommandRunner(self.manifest, backend),
        )
        workflow = GenericWorkflow(
            self.manifest,
            self.store,
            self.store,
            github=self.github,
            smoke_executor=smoke_executor,
        )
        self.store.events.clear()

        result = workflow.execute_smoke("PRO-101")

        state = self.store.states["PRO-101"]
        self.assertEqual(result.next_action, "block")
        self.assertIn("web", result.reason)
        self.assertEqual(manager.backend.started, [])
        self.assertEqual(len(state.snapshot.smoke_reads), 1)
        recorded = state.snapshot.smoke_reads[0]
        self.assertFalse(recorded.authoritative)
        self.assertEqual(dict(recorded.checkout_shas), {"api": SHA["api"]})
        self.assertEqual(
            dict(recorded.repository_results),
            {"api": "blocked", "web": "blocked"},
        )
        self.assertNotIn("pass", recorded.repository_results.values())
        self.assertTrue(any(event[0] == "write-smoke" for event in self.store.events))

    def test_repository_command_drift_persists_partial_named_block_and_leaves_unrun_pending(self):
        candidates = dict(SHA)
        base = passing_snapshot()
        snapshot = replace(
            base,
            affected_repositories=("api", "web", "notifications"),
            candidate_shas=candidates,
            children={
                repository: RepositoryEvidence(sha, "pass")
                for repository, sha in candidates.items()
            },
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in candidates.items()
            },
            reviews={
                repository: RepositoryEvidence(sha, "pass")
                for repository, sha in candidates.items()
            },
            qa={
                repository: RepositoryEvidence(sha, "pass")
                for repository, sha in candidates.items()
            },
            integration_qa={
                "web-api": GateEvidence(candidates, "pass")
            },
            merge_state="merged",
            merged_shas=candidates,
        )
        self.store.add_state(
            "PRO-101", snapshot, pull_requests=pull_request_targets()
        )
        manager = FakeOwnedProcessManager(self.manifest)
        backend = MutableClosedCommandBackend(self.manifest)

        def change_web_during_api(argv, cwd):
            if argv == self.manifest.repositories["api"].commands["smoke"]:
                backend.heads[self.manifest.repositories["web"].local_path] = "f" * 40

        backend.command_hook = change_web_during_api
        workflow = GenericWorkflow(
            self.manifest,
            self.store,
            self.store,
            github=self.github,
            smoke_executor=OwnedSmokeExecutor(
                self.manifest,
                manager,
                LocalExactShaCommandRunner(self.manifest, backend),
            ),
        )
        self.store.events.clear()

        result = workflow.execute_smoke("PRO-101")

        recorded = self.store.states["PRO-101"].snapshot.smoke_reads[0]
        self.assertEqual(result.next_action, "block")
        self.assertIn("web", result.reason)
        self.assertFalse(recorded.authoritative)
        self.assertEqual(
            dict(recorded.checkout_shas),
            {"api": SHA["api"], "notifications": SHA["notifications"]},
        )
        self.assertEqual(
            dict(recorded.repository_results),
            {"api": "blocked", "web": "blocked", "notifications": "pending"},
        )
        self.assertEqual(dict(recorded.integration_results), {"web-api": "pending"})
        self.assertNotIn(
            (
                self.manifest.repositories["notifications"].commands["smoke"],
                self.manifest.repositories["notifications"].local_path,
            ),
            backend.calls,
        )
        self.assertEqual(len(manager.backend.stopped), len(manager.backend.started))
        self.assertFalse(
            recorded.authoritative
            and any(value == "pass" for value in recorded.repository_results.values())
        )

    def test_integration_command_drift_persists_partial_named_block(self):
        snapshot = passing_snapshot()
        snapshot = replace(
            snapshot,
            merge_state="merged",
            merged_shas=snapshot.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in snapshot.candidate_shas.items()
            },
        )
        self.store.add_state(
            "PRO-101", snapshot, pull_requests=pull_request_targets()
        )
        manager = FakeOwnedProcessManager(self.manifest)
        backend = MutableClosedCommandBackend(self.manifest)
        integration_argv = self.manifest.integration_suites[0].command

        def change_api_during_integration(argv, cwd):
            if argv == integration_argv:
                backend.heads[self.manifest.repositories["api"].local_path] = "f" * 40

        backend.command_hook = change_api_during_integration
        workflow = GenericWorkflow(
            self.manifest,
            self.store,
            self.store,
            github=self.github,
            smoke_executor=OwnedSmokeExecutor(
                self.manifest,
                manager,
                LocalExactShaCommandRunner(self.manifest, backend),
            ),
        )
        self.store.events.clear()

        result = workflow.execute_smoke("PRO-101")

        recorded = self.store.states["PRO-101"].snapshot.smoke_reads[0]
        self.assertEqual(result.next_action, "block")
        self.assertIn("api", result.reason)
        self.assertFalse(recorded.authoritative)
        self.assertEqual(dict(recorded.checkout_shas), {"web": SHA["web"]})
        self.assertEqual(
            dict(recorded.repository_results),
            {"api": "blocked", "web": "pass"},
        )
        self.assertEqual(dict(recorded.integration_results), {"web-api": "blocked"})
        self.assertEqual(len(manager.backend.stopped), len(manager.backend.started))
        self.assertFalse(
            recorded.authoritative
            and any(value == "pass" for value in recorded.repository_results.values())
        )

    def test_concrete_runner_blocks_head_change_in_service_startup_hook(self):
        backend = MutableClosedCommandBackend(self.manifest)
        manager = FakeOwnedProcessManager(self.manifest)
        manager.backend.start_hook = lambda _repository: backend.heads.__setitem__(
            self.manifest.repositories["web"].local_path,
            "f" * 40,
        )
        executor = OwnedSmokeExecutor(
            self.manifest,
            manager,
            LocalExactShaCommandRunner(self.manifest, backend),
        )

        result = executor.execute(
            "PRO-101", ("api", "web"), {"api": SHA["api"], "web": SHA["web"]},
            action_key="smoke:" + "7" * 64,
        )

        self.assertGreater(len(manager.backend.started), 0)
        self.assertEqual(len(manager.backend.stopped), len(manager.backend.started))
        self.assertFalse(result.authoritative)
        self.assertNotIn("pass", result.repository_results.values())

    def test_concrete_runner_blocks_head_change_during_smoke_command(self):
        manager = FakeOwnedProcessManager(self.manifest)
        backend = MutableClosedCommandBackend(self.manifest)
        changed = False

        def change_checkout(argv, cwd):
            nonlocal changed
            if not changed:
                changed = True
                backend.heads[self.manifest.repositories["web"].local_path] = "f" * 40

        backend.command_hook = change_checkout
        executor = OwnedSmokeExecutor(
            self.manifest,
            manager,
            LocalExactShaCommandRunner(self.manifest, backend),
        )

        result = executor.execute(
            "PRO-101", ("api", "web"), {"api": SHA["api"], "web": SHA["web"]},
            action_key="smoke:" + "7" * 64,
        )

        self.assertFalse(result.authoritative)
        self.assertNotIn("pass", result.repository_results.values())
        self.assertNotIn("pass", result.integration_results.values())

    def test_owned_smoke_runs_repository_and_cross_repository_commands_then_cleans_up(self):
        manager = FakeOwnedProcessManager(self.manifest)
        backend = MutableClosedCommandBackend(self.manifest)
        commands = LocalExactShaCommandRunner(self.manifest, backend)
        executor = OwnedSmokeExecutor(self.manifest, manager, commands)
        exact = {"api": SHA["api"], "web": SHA["web"]}

        result = executor.execute(
            "PRO-101",
            ("api", "web"),
            exact,
            action_key="smoke:" + "7" * 64,
        )

        self.assertEqual(
            [item[1].repository_key for item in manager.backend.started],
            ["api", "web"],
        )
        self.assertEqual(
            [argv for argv, _ in backend.calls if argv != ("git", "rev-parse", "HEAD")],
            [
                self.manifest.repositories["api"].commands["smoke"],
                self.manifest.repositories["web"].commands["smoke"],
                self.manifest.integration_suites[0].command,
            ],
        )
        self.assertEqual(
            [item[0].repository_key for item in manager.backend.stopped],
            ["web", "api"],
        )
        self.assertEqual(dict(result.merged_shas), exact)
        self.assertEqual(dict(result.repository_results), {"api": "pass", "web": "pass"})
        self.assertEqual(dict(result.integration_results), {"web-api": "pass"})
        self.assertTrue(result.authoritative)
        self.assertEqual(dict(result.checkout_shas), exact)

    def test_owned_smoke_starts_multiple_repository_services_with_one_manager_call(self):
        repositories = dict(self.manifest.repositories)
        repositories["api"] = replace(
            repositories["api"],
            services=(
                *repositories["api"].services,
                ServiceSpec("api-admin", 8081, "http://localhost:8081/health"),
            ),
        )
        manifest = replace(self.manifest, repositories=MappingProxyType(repositories))
        manager = FakeOwnedProcessManager(manifest)
        backend = MutableClosedCommandBackend(manifest)
        executor = OwnedSmokeExecutor(
            manifest,
            manager,
            LocalExactShaCommandRunner(manifest, backend),
        )

        executor.execute(
            "PRO-101",
            ("api",),
            {"api": SHA["api"]},
            action_key="smoke:" + "7" * 64,
        )

        self.assertEqual(len(manager.backend.started), 1)
        self.assertEqual(len(manager.backend.started[0][0]), 2)
        self.assertEqual(len(manager.backend.stopped), 1)

    def test_watcher_recovers_once_then_blocks_a_still_stalled_parent(self):
        child = WorkflowChild(
            "PRO-101-API",
            "api",
            "api",
            "",
            "implementation",
            1,
            0,
            "in_progress",
            "dispatch:" + "3" * 64,
            False,
        )
        snapshot = ParentSnapshot(
            affected_repositories=("api",),
            children={"api": RepositoryEvidence("", "pending")},
            stalled=True,
            stalled_repository="api",
        )
        self.store.add_state("PRO-101", snapshot, children=(child,))
        self.store.activate_on_rerun = True
        stalled_at = datetime(2026, 8, 27, tzinfo=timezone.utc)

        first = self.workflow.recover_stalled_parent("PRO-101", now=stalled_at)
        recovered = self.store.states["PRO-101"]
        self.store.states["PRO-101"] = replace(
            recovered,
            children=tuple(replace(item, active=False) for item in recovered.children),
            active_work=False,
        )
        second = self.workflow.recover_stalled_parent("PRO-101", now=stalled_at)

        self.assertEqual(first.next_action, "resume")
        self.assertEqual(second.parent_status, "blocked")
        self.assertEqual(len([event for event in self.store.events if event[0] == "rerun"]), 1)
        rerun_key = [event for event in self.store.events if event[0] == "rerun"][0][-1]
        blocked_key = [event for event in self.store.events if event[0] == "status"][-1][-1]
        self.assertNotEqual(rerun_key, blocked_key)

    def test_watcher_entrypoints_fail_closed_without_mutation_for_future_child_relationship(self):
        future_child = WorkflowChild(
            "PRO-101-FUTURE", "api", "api", "", "implementation", 6, 0,
            "in_progress", "dispatch:" + "c" * 64, False,
        )
        snapshot = ParentSnapshot(
            affected_repositories=("api",),
            children={"api": RepositoryEvidence("", "pending")},
            stalled=True,
            stalled_repository="api",
        )
        entrypoints = {
            "direct recovery": lambda: self.workflow.recover_stalled_parent("PRO-101"),
            "watcher scan": self.workflow.watch_active_parents,
        }

        for entrypoint, invoke in entrypoints.items():
            with self.subTest(entrypoint=entrypoint):
                self.store.add_state("PRO-101", snapshot, children=(future_child,))
                before = self.store.states["PRO-101"]
                self.store.events.clear()

                result = invoke()

                self.assertEqual(result.parent_status, "blocked")
                self.assertEqual(result.next_action, "block")
                self.assertIn("child relationship", result.reason)
                self.assertEqual(result.mutation_count, 0)
                self.assertEqual(self.store.states["PRO-101"], before)
                self.assertFalse(
                    any(event[0] in {"rerun", "status", "create"} for event in self.store.events)
                )

    def test_watcher_does_not_accept_metadata_only_rerun_as_recovery(self):
        child = WorkflowChild(
            "PRO-101-API", "api", "api", "", "implementation", 1, 0,
            "in_progress", "dispatch:" + "b" * 64, False,
        )
        snapshot = ParentSnapshot(
            affected_repositories=("api",),
            children={"api": RepositoryEvidence("", "pending")},
            stalled=True,
            stalled_repository="api",
        )
        self.store.add_state("PRO-101", snapshot, children=(child,))

        result = self.workflow.recover_stalled_parent("PRO-101")

        self.assertIn(result.next_action, {"uncertain", "block"})
        self.assertNotEqual(result.next_action, "resume")

    def test_watcher_rejects_authoritative_read_for_a_different_parent(self):
        child = WorkflowChild(
            "PRO-101-API",
            "api",
            "api",
            "",
            "implementation",
            1,
            0,
            "in_progress",
            "dispatch:" + "3" * 64,
            False,
        )
        snapshot = ParentSnapshot(
            affected_repositories=("api",),
            children={"api": RepositoryEvidence("", "pending")},
            stalled=True,
            stalled_repository="api",
        )
        self.store.add_state("PRO-101", snapshot, children=(child,))
        self.store.read_parent_identifier_override = "PRO-999"

        result = self.workflow.recover_stalled_parent("PRO-101")

        self.assertEqual(result.next_action, "noop")
        self.assertFalse(any(event[0] == "rerun" for event in self.store.events))

    def test_direct_recovery_initial_scope_mismatch_does_not_leak_untrusted_identity_or_status(self):
        child = WorkflowChild(
            "PRO-101-API", "api", "api", "", "implementation", 1, 0,
            "in_progress", "dispatch:" + "3" * 64, False,
        )
        snapshot = ParentSnapshot(
            affected_repositories=("api",),
            children={"api": RepositoryEvidence("", "pending")},
            stalled=True,
            stalled_repository="api",
        )

        for mismatch in ("identity", "project"):
            with self.subTest(mismatch=mismatch):
                self.store.states.clear()
                self.store.events.clear()
                self.store.read_counts.clear()
                self.store.read_state_override_by_count.clear()
                self.store.add_state("PRO-101", snapshot, status="in_review", children=(child,))
                self.store.add_state("PRO-999", snapshot, status="done", children=(child,))
                requested_before = self.store.states["PRO-101"]
                foreign_before = self.store.states["PRO-999"]
                if mismatch == "identity":
                    untrusted = foreign_before
                else:
                    untrusted = replace(
                        requested_before,
                        parent_status="done",
                        project_key="foreign",
                    )
                self.store.read_state_override_by_count[1] = untrusted

                result = self.workflow.recover_stalled_parent("PRO-101")

                self.assertEqual(result.parent_identifier, "PRO-101")
                self.assertEqual(result.parent_status, "in_progress")
                self.assertEqual(result.next_action, "noop")
                self.assertEqual(result.mutation_count, 0)
                self.assertEqual(self.store.states["PRO-101"], requested_before)
                self.assertEqual(self.store.states["PRO-999"], foreign_before)
                self.assertFalse(
                    any(event[0] in {"rerun", "status", "create"} for event in self.store.events)
                )

    def test_direct_recovery_reread_scope_mismatch_uses_last_trusted_requested_state(self):
        child = WorkflowChild(
            "PRO-101-API", "api", "api", "", "implementation", 1, 0,
            "in_progress", "dispatch:" + "3" * 64, False,
        )
        snapshot = ParentSnapshot(
            affected_repositories=("api",),
            children={"api": RepositoryEvidence("", "pending")},
            stalled=True,
            stalled_repository="api",
        )

        for mismatch in ("identity", "project"):
            with self.subTest(mismatch=mismatch):
                self.store.states.clear()
                self.store.events.clear()
                self.store.read_counts.clear()
                self.store.read_state_override_by_count.clear()
                self.store.add_state("PRO-101", snapshot, status="in_review", children=(child,))
                self.store.add_state("PRO-999", snapshot, status="done", children=(child,))
                requested_before = self.store.states["PRO-101"]
                foreign_before = self.store.states["PRO-999"]
                if mismatch == "identity":
                    untrusted = foreign_before
                else:
                    untrusted = replace(
                        requested_before,
                        parent_status="done",
                        project_key="foreign",
                    )
                self.store.read_state_override_by_count[2] = untrusted

                result = self.workflow.recover_stalled_parent("PRO-101")

                self.assertEqual(result.parent_identifier, "PRO-101")
                self.assertEqual(result.parent_status, "in_review")
                self.assertEqual(result.next_action, "noop")
                self.assertEqual(result.mutation_count, 0)
                self.assertEqual(self.store.states["PRO-101"], requested_before)
                self.assertEqual(self.store.states["PRO-999"], foreign_before)
                self.assertFalse(
                    any(event[0] in {"rerun", "status", "create"} for event in self.store.events)
                )

    def test_successful_recovery_with_unobservable_reread_is_not_overwritten(self):
        child = WorkflowChild(
            "PRO-101-API",
            "api",
            "api",
            "",
            "implementation",
            1,
            0,
            "in_progress",
            "dispatch:" + "3" * 64,
            False,
        )
        snapshot = ParentSnapshot(
            affected_repositories=("api",),
            children={"api": RepositoryEvidence("", "pending")},
            stalled=True,
            stalled_repository="api",
        )
        self.store.add_state("PRO-101", snapshot, children=(child,))
        self.store.fail_reads_after_rerun = True

        result = self.workflow.recover_stalled_parent("PRO-101")

        self.assertEqual(result.next_action, "uncertain")
        self.assertEqual(self.store.states["PRO-101"].snapshot.recovery_count, 1)
        self.assertFalse(any(event[0] == "status" for event in self.store.events))

    def test_watcher_post_effect_read_must_still_name_requested_parent(self):
        child = WorkflowChild(
            "PRO-101-API",
            "api",
            "api",
            "",
            "implementation",
            1,
            0,
            "in_progress",
            "dispatch:" + "3" * 64,
            False,
        )
        snapshot = ParentSnapshot(
            affected_repositories=("api",),
            children={"api": RepositoryEvidence("", "pending")},
            stalled=True,
            stalled_repository="api",
        )
        self.store.add_state("PRO-101", snapshot, children=(child,))
        self.store.override_parent_after_rerun = True

        result = self.workflow.recover_stalled_parent("PRO-101")

        self.assertEqual(result.next_action, "uncertain")
        self.assertEqual(len([event for event in self.store.events if event[0] == "rerun"]), 1)
        self.assertFalse(any(event[0] == "status" for event in self.store.events))

    def test_watcher_ignores_healthy_human_wait_and_unsupported_work(self):
        pending = ParentSnapshot(
            affected_repositories=("api",),
            children={"api": RepositoryEvidence("", "pending")},
            stalled=True,
            stalled_repository="api",
        )
        child = WorkflowChild(
            "CH",
            "api",
            "api",
            "",
            "implementation",
            1,
            0,
            "in_progress",
            "dispatch:" + "4" * 64,
            False,
        )
        self.store.add_state("PRO-healthy", pending, children=(child,), active_work=True)
        self.store.add_state("PRO-human", pending, children=(child,), human_wait=True)
        self.store.add_state("PRO-version", pending, children=(child,), workflow_version=1)
        self.store.add_state("PRO-project", pending, children=(child,), project_key="foreign")

        self.assertEqual(self.workflow.recover_stalled_parent("PRO-healthy").next_action, "noop")
        self.assertEqual(self.workflow.recover_stalled_parent("PRO-human").next_action, "noop")
        legacy = self.workflow.recover_stalled_parent("PRO-version")
        self.assertEqual(legacy.next_action, "block")
        self.assertIn("migration", legacy.reason)
        self.assertEqual(legacy.mutation_count, 0)
        self.assertEqual(self.workflow.recover_stalled_parent("PRO-project").next_action, "noop")

    def test_watcher_rereads_before_mutation_and_stops_when_work_changes(self):
        child = WorkflowChild(
            "PRO-101-API",
            "api",
            "api",
            "",
            "implementation",
            1,
            0,
            "in_progress",
            "dispatch:" + "5" * 64,
            False,
        )
        snapshot = ParentSnapshot(
            affected_repositories=("api",),
            children={"api": RepositoryEvidence("", "pending")},
            stalled=True,
            stalled_repository="api",
        )
        self.store.add_state("PRO-101", snapshot, children=(child,))
        self.store.change_on_recovery_reread = True

        result = self.workflow.recover_stalled_parent("PRO-101")

        self.assertEqual(result.next_action, "noop")
        self.assertFalse(any(event[0] == "rerun" for event in self.store.events))

    def test_watcher_never_reruns_a_child_from_an_old_repair_attempt(self):
        old_child = WorkflowChild(
            "PRO-101-API-OLD",
            "api",
            "api",
            "",
            "implementation",
            1,
            0,
            "in_progress",
            "dispatch:" + "6" * 64,
            False,
        )
        snapshot = ParentSnapshot(
            affected_repositories=("api",),
            children={"api": RepositoryEvidence("", "pending")},
            attempt=1,
            stalled=True,
            stalled_repository="api",
        )
        self.store.add_state("PRO-101", snapshot, children=(old_child,))

        result = self.workflow.recover_stalled_parent("PRO-101")

        self.assertEqual(result.next_action, "noop")
        self.assertFalse(any(event[0] == "rerun" for event in self.store.events))

    def test_watcher_accepts_authoritative_activation_of_the_same_existing_child(self):
        child = WorkflowChild(
            "PRO-101-API",
            "api",
            "api",
            "",
            "implementation",
            1,
            0,
            "in_progress",
            "dispatch:" + "9" * 64,
            False,
        )
        snapshot = ParentSnapshot(
            affected_repositories=("api",),
            children={"api": RepositoryEvidence("", "pending")},
            stalled=True,
            stalled_repository="api",
        )
        self.store.add_state("PRO-101", snapshot, children=(child,))
        self.store.activate_on_rerun = True

        result = self.workflow.recover_stalled_parent("PRO-101")

        self.assertEqual(result.next_action, "resume")
        self.assertTrue(self.store.states["PRO-101"].active_work)
        self.assertEqual(len(self.store.states["PRO-101"].children), 1)

    def test_watcher_blocks_if_rerun_effect_changes_gate_evidence(self):
        child = WorkflowChild(
            "PRO-101-API",
            "api",
            "api",
            "",
            "implementation",
            1,
            0,
            "in_progress",
            "dispatch:" + "a" * 64,
            False,
        )
        snapshot = ParentSnapshot(
            affected_repositories=("api",),
            children={"api": RepositoryEvidence("", "pending")},
            stalled=True,
            stalled_repository="api",
        )
        self.store.add_state("PRO-101", snapshot, children=(child,))
        self.store.mutate_gate_on_rerun = True

        result = self.workflow.recover_stalled_parent("PRO-101")

        self.assertEqual(result.parent_status, "blocked")
        self.assertEqual(result.next_action, "block")

    def test_watcher_scan_is_instance_project_version_scoped_and_recovers_one(self):
        child = WorkflowChild(
            "PRO-101-API",
            "api",
            "api",
            "",
            "implementation",
            1,
            0,
            "in_progress",
            "dispatch:" + "6" * 64,
            False,
        )
        snapshot = ParentSnapshot(
            affected_repositories=("api",),
            children={"api": RepositoryEvidence("", "pending")},
            stalled=True,
            stalled_repository="api",
        )
        self.store.add_state("PRO-101", snapshot, children=(child,))
        self.store.add_state("PRO-102", snapshot, children=(replace(child, identifier="PRO-102-API"),))
        self.store.activate_on_rerun = True

        result = self.workflow.watch_active_parents()

        self.assertEqual(result.next_action, "resume")
        self.assertEqual(len([event for event in self.store.events if event[0] == "rerun"]), 1)
        instance, projects, versions = self.store.list_arguments
        self.assertEqual(instance, "sample-commerce")
        self.assertEqual(versions, frozenset({2}))
        self.assertEqual(
            projects,
            frozenset({self.manifest.instance.control_project}),
        )

    def test_non_workflow_state_returns_requested_identity_without_mutation(self):
        self.store.states["PRO-101"] = object()  # type: ignore[assignment]
        self.store.events.clear()

        result = self.workflow.resume_parent("PRO-101")

        self.assertEqual(result.parent_identifier, "PRO-101")
        self.assertEqual(result.next_action, "block")
        self.assertEqual(result.mutation_count, 0)
        self.assertEqual(self.store.events, [("read-parent", "PRO-101")])

    def test_forged_exact_workflow_state_schema_fails_closed_without_mutation(self):
        self.store.add_state("PRO-101", passing_snapshot())
        forged = self.store.states["PRO-101"]
        object.__setattr__(forged, "snapshot", object.__new__(ParentSnapshot))
        self.store.events.clear()

        result = self.workflow.resume_parent("PRO-101")

        self.assertEqual(result.parent_identifier, "PRO-101")
        self.assertEqual(result.next_action, "block")
        self.assertEqual(result.mutation_count, 0)
        self.assertEqual(self.store.events, [("read-parent", "PRO-101")])

    def test_unsupported_metadata_schema_has_zero_mutations(self):
        self.store.add_state("PRO-101", passing_snapshot(), pull_requests=pull_request_targets())
        state = self.store.states["PRO-101"]
        forged_metadata = replace(state.metadata)
        object.__setattr__(forged_metadata, "metadata_version", 99)
        self.store.states["PRO-101"] = replace(
            state,
            metadata=forged_metadata,
        )
        self.store.events.clear()

        result = self.workflow.resume_parent("PRO-101")

        self.assertEqual(result.parent_identifier, "PRO-101")
        self.assertEqual(result.next_action, "block")
        self.assertEqual(result.mutation_count, 0)
        self.assertFalse(any(event[0] in {"status", "create", "merge-state"} for event in self.store.events))

    def test_persisted_child_failure_fields_are_exact_and_phase_specific(self):
        snapshot = replace(
            passing_snapshot(),
            reviews={
                **passing_snapshot().reviews,
                "api": RepositoryEvidence(SHA["api"], "fail"),
            },
        )
        comment_uuid = str(uuid.UUID("a" * 32))
        valid = WorkflowChild(
            "PRO-101-API-REVIEW", "api", "api", "", "review", 5, 0,
            "done", "review:" + "a" * 64, False,
            evidence_comment_uuid=comment_uuid,
            creation_candidate_shas=snapshot.candidate_shas,
            phase_result="fail",
            evidence_comment_url=f"https://example.test/evidence/{comment_uuid}",
            responsible_repositories=("api",),
        )
        corruptions = {
            "phase result type": ("phase_result", 7),
            "evidence URL type": ("evidence_comment_url", 7),
            "responsible repositories type": ("responsible_repositories", ["api"]),
            "bundle digest type": ("failure_bundle_digest", 7),
            "bundle UUID partition type": ("failure_evidence_uuids", [comment_uuid]),
            "non-repair bundle digest": ("failure_bundle_digest", "b" * 64),
            "non-repair UUID partition": ("failure_evidence_uuids", (comment_uuid,)),
        }

        for label, (field_name, value) in corruptions.items():
            with self.subTest(corruption=label):
                self.store.add_state(
                    "PRO-101", snapshot, children=(valid,),
                    pull_requests=pull_request_targets(),
                )
                state = self.store.states["PRO-101"]
                forged = replace(state.children[0])
                object.__setattr__(forged, field_name, value)
                self.store.states["PRO-101"] = replace(
                    state,
                    children=(forged,),
                )
                self.store.events.clear()

                with patch(
                    "multica_delivery.core.workflow.decide_parent_action",
                    side_effect=AssertionError("decision reached malformed child state"),
                ):
                    result = self.workflow.resume_parent("PRO-101")

                self.assertEqual(result.next_action, "block")
                self.assertEqual(result.mutation_count, 0)
                self.assertEqual(self.store.events, [("read-parent", "PRO-101")])

    def test_nonterminal_version_one_parent_requires_zero_mutation_migration(self):
        self.store.add_state(
            "PRO-101",
            passing_snapshot(),
            pull_requests=pull_request_targets(),
            workflow_version=1,
        )
        before = self.store.states["PRO-101"]
        self.store.events.clear()

        result = self.workflow.resume_parent("PRO-101")

        self.assertEqual(result.next_action, "block")
        self.assertIn("migration", result.reason)
        self.assertEqual(result.mutation_count, 0)
        self.assertEqual(self.store.states["PRO-101"], before)
        self.assertEqual(self.store.events, [("read-parent", "PRO-101")])

    def test_every_parent_progression_entrypoint_requires_exact_control_project(self):
        snapshot = replace(
            passing_snapshot(),
            stalled=True,
            stalled_repository="api",
        )
        child = WorkflowChild(
            "PRO-101-API", "api", "api", "", "review", 5, 0,
            "in_progress", "stage:" + "a" * 64, False,
        )
        self.store.add_state(
            "PRO-101",
            snapshot,
            children=(child,),
            pull_requests=pull_request_targets(),
            project_key=self.manifest.repositories["api"].project_title,
        )
        observation = passing_smoke(observation_id=SMOKE_OBSERVATION["first"])
        invocations = (
            lambda: self.workflow.resume_parent("PRO-101"),
            lambda: self.workflow.execute_merge_plan("PRO-101"),
            lambda: self.workflow.record_phase_completion(
                completion_for("api", phase="review")
            ),
            lambda: self.workflow.record_smoke_read("PRO-101", observation),
            lambda: self.workflow.recover_stalled_parent("PRO-101"),
        )
        for invoke in invocations:
            with self.subTest(invoke=invoke):
                self.store.events.clear()
                result = invoke()
                self.assertEqual(result.parent_identifier, "PRO-101")
                self.assertIn(result.next_action, {"block", "noop"})
                self.assertEqual(result.mutation_count, 0)
                self.assertFalse(any(event[0] not in {"read-parent"} for event in self.store.events))

    def test_new_phase_completion_requires_current_active_created_child(self):
        self.workflow.handle_parent_event("PRO-101", affected=frozenset({"api"}))
        state = self.store.states["PRO-101"]
        child = state.children[0]
        variants = (
            replace(child, stage_ordinal=child.stage_ordinal - 1),
            replace(child, active=False),
            replace(child, action_key="dispatch:" + "f" * 64),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.store.states["PRO-101"] = replace(state, children=(variant,))
                self.store.events.clear()
                result = self.workflow.record_phase_completion(completion_for("api"))
                self.assertEqual(result.next_action, "block")
                self.assertEqual(result.mutation_count, 0)
                self.assertFalse(any(event[0] in {"write-completion", "done"} for event in self.store.events))

    def test_new_phase_completion_requires_two_stable_absence_reads(self):
        self.workflow.handle_parent_event("PRO-101", affected=frozenset({"api"}))
        self.store.completion_read_failures_remaining = 1
        self.store.events.clear()

        result = self.workflow.record_phase_completion(completion_for("api"))

        self.assertEqual(result.next_action, "uncertain")
        self.assertEqual(result.mutation_count, 0)
        self.assertFalse(
            any(event[0] in {"write-completion", "done"} for event in self.store.events)
        )

    def test_post_merge_phase_completion_only_exact_replays_existing_transition(self):
        self.workflow.handle_parent_event("PRO-101", affected=frozenset({"api"}))
        completion = completion_for("api")
        first = self.workflow.record_phase_completion(completion)
        self.assertEqual(first.completed_child_status, "done")
        state = self.store.states["PRO-101"]
        merged_snapshot = replace(
            state.snapshot,
            merge_state="merged",
            merged_shas={"api": SHA["api"]},
            pull_requests={
                "api": PullRequestEvidence(
                    SHA["api"], "merged", True, True, SHA["api"]
                )
            },
        )
        self.store.states["PRO-101"] = replace(
            state,
            metadata=replace(
                state.metadata,
                candidate_shas={"api": SHA["api"]},
                merge_plan=("api",),
                merge_state="merged",
            ),
            snapshot=merged_snapshot,
        )
        self.store.events.clear()

        replay = self.workflow.record_phase_completion(completion)
        new_evidence = self.workflow.record_phase_completion(
            completion_for("api", phase="review")
        )

        self.assertEqual(replay.next_action, "noop")
        self.assertEqual(new_evidence.next_action, "block")
        self.assertFalse(any(event[0] in {"write-completion", "done"} for event in self.store.events))

    def test_merge_stage_barrier_precedes_recovery_and_github_reads(self):
        child = WorkflowChild(
            "PRO-101-GATE", "api", "api", "", "review", 5, 0,
            "in_progress", "stage:" + "a" * 64, True,
        )
        self.store.add_state(
            "PRO-101",
            merging_snapshot(),
            children=(child,),
            pull_requests=pull_request_targets(),
        )
        self.store.events.clear()

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertEqual(result.next_action, "wait")
        self.assertFalse(any(event[0].startswith("github-") for event in self.store.events))
        self.assertFalse(any(event[0] == "merge-state" for event in self.store.events))

    def test_merge_reservation_freezes_pull_request_targets(self):
        self.store.add_state(
            "PRO-101", passing_snapshot(), pull_requests=pull_request_targets()
        )
        self.store.change_target_after_reservation = True
        self.store.events.clear()

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertIn(result.next_action, {"uncertain", "block"})
        self.assertEqual(self.github.merged, [])

    def test_merge_revalidates_candidate_authority_before_each_mutation(self):
        self.store.add_state(
            "PRO-101", passing_snapshot(), pull_requests=pull_request_targets()
        )
        self.store.change_candidate_after_first_progress = True
        self.store.events.clear()

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertIn(result.next_action, {"uncertain", "block"})
        self.assertEqual(self.github.merged, [(self.manifest.repositories["api"].github, 12)])

    def test_merge_reservation_rejects_added_pr_evidence_before_mutation(self):
        self.store.add_state(
            "PRO-101", passing_snapshot(), pull_requests=pull_request_targets()
        )
        self.store.add_pr_evidence_after_reservation = True
        self.store.events.clear()

        result = self.workflow.execute_merge_plan("PRO-101")

        self.assertIn(result.next_action, {"uncertain", "block"})
        self.assertEqual(self.github.merged, [])

    def test_repair_dispatch_rejects_foreign_manifest_pr_target(self):
        snapshot = replace(
            passing_snapshot(),
            reviews={
                "api": RepositoryEvidence(SHA["api"], "pass"),
                "web": RepositoryEvidence(SHA["web"], "fail"),
            },
        )
        targets = pull_request_targets()
        targets["web"] = PullRequestTarget(
            "web", 14, "https://github.com/foreign-owner/foreign-repo/pull/14"
        )
        self.store.add_state("PRO-101", snapshot, pull_requests=targets)
        self.store.events.clear()

        result = self.workflow.resume_parent("PRO-101")

        self.assertEqual(result.next_action, "block")
        self.assertFalse(any(event[0] == "create" for event in self.store.events))

    def test_public_smoke_record_is_replay_only(self):
        snapshot = replace(
            passing_snapshot(),
            merge_state="merged",
            merged_shas=passing_snapshot().candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in passing_snapshot().candidate_shas.items()
            },
        )
        self.store.add_state("PRO-101", snapshot, pull_requests=pull_request_targets())
        observation = passing_smoke(observation_id=SMOKE_OBSERVATION["first"])
        self.store.events.clear()

        result = self.workflow.record_smoke_read("PRO-101", observation)

        self.assertEqual(result.next_action, "block")
        self.assertEqual(result.mutation_count, 0)
        self.assertFalse(any(event[0] == "write-smoke" for event in self.store.events))

    def test_public_malformed_smoke_record_has_zero_effects(self):
        self.store.add_state("PRO-101", passing_snapshot())
        self.store.events.clear()

        result = self.workflow.record_smoke_read("PRO-101", object())  # type: ignore[arg-type]

        self.assertEqual(result.next_action, "block")
        self.assertEqual(result.mutation_count, 0)
        self.assertFalse(
            any(event[0] not in {"read-parent"} for event in self.store.events)
        )

    def test_generic_workflow_rejects_arbitrary_smoke_executor(self):
        smoke = FakeSmokeExecutor(
            passing_smoke(observation_id=SMOKE_OBSERVATION["first"])
        )
        with self.assertRaisesRegex(TypeError, "OwnedSmokeExecutor"):
            GenericWorkflow(
                self.manifest,
                self.store,
                self.store,
                github=self.github,
                smoke_executor=smoke,
            )

    def test_every_manifest_bound_effect_revalidates_forged_policy(self):
        unsafe = object.__new__(PolicySpec)
        for name, value in (
            ("environment", "development"),
            ("automatic_merge", True),
            ("deployment", "automatic"),
            ("max_repair_attempts", 2),
            ("watcher_cron", "*/30 * * * *"),
            ("watcher_timezone", "Asia/Shanghai"),
        ):
            object.__setattr__(unsafe, name, value)
        manifest = replace(self.manifest, policy=unsafe)
        self.store.events.clear()

        with self.assertRaisesRegex(ValueError, "deployment"):
            GenericWorkflow(manifest, self.store, self.store, github=self.github)
        with self.assertRaisesRegex(ValueError, "deployment"):
            LocalExactShaCommandRunner(
                manifest,
                MutableClosedCommandBackend(manifest),
            )
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ValueError, "deployment"):
            ProcessManager(
                ProcessRegistry(Path(temporary.name) / "registry.json"),
                object(),  # type: ignore[arg-type]
                owner_token="owner",
                manifest=manifest,
            )

        self.assertEqual(self.store.events, [])

    def test_watcher_selects_only_current_created_child_not_historical_match(self):
        snapshot = ParentSnapshot(
            affected_repositories=("api",),
            children={"api": RepositoryEvidence("", "pending")},
            stalled=True,
            stalled_repository="api",
        )
        historical = WorkflowChild(
            "PRO-101-OLD", "api", "api", "", "implementation", 1, 0,
            "in_progress", "dispatch:" + "1" * 64, False,
        )
        current = replace(
            historical,
            identifier="PRO-101-CURRENT",
            stage_ordinal=5,
            action_key="dispatch:" + "2" * 64,
        )
        self.store.add_state("PRO-101", snapshot, children=(historical, current))
        state = self.store.states["PRO-101"]
        self.store.states["PRO-101"] = replace(
            state,
            applied_action_keys=frozenset({historical.action_key, current.action_key}),
        )
        self.store.activate_on_rerun = True
        self.store.events.clear()

        result = self.workflow.recover_stalled_parent("PRO-101")

        self.assertEqual(result.next_action, "resume")
        reruns = [event for event in self.store.events if event[0] == "rerun"]
        self.assertEqual(reruns[0][2], "PRO-101-CURRENT")

    def test_public_phase_and_smoke_dtos_are_validated_before_parent_reads(self):
        self.store.add_state("PRO-101", passing_snapshot())
        malformed_completion = completion_for("api")
        object.__setattr__(
            malformed_completion,
            "pull_request_url",
            "https://github.com/codeExploreHub/sample-commerce-api/pull/12?token=secret",
        )
        malformed_smoke = passing_smoke(
            observation_id=SMOKE_OBSERVATION["first"]
        )
        object.__setattr__(malformed_smoke, "authoritative", 1)

        for invoke in (
            lambda: self.workflow.record_phase_completion(malformed_completion),
            lambda: self.workflow.record_smoke_read("PRO-101", malformed_smoke),
        ):
            with self.subTest(invoke=invoke):
                self.store.events.clear()

                result = invoke()

                self.assertEqual(result.parent_identifier, "PRO-101")
                self.assertEqual(result.next_action, "block")
                self.assertEqual(result.mutation_count, 0)
                self.assertEqual(self.store.events, [])

    def test_nested_exact_class_state_forgery_blocks_every_progression_path(self):
        malformed_review = RepositoryEvidence(SHA["api"], "pending")
        object.__setattr__(malformed_review, "result", True)
        snapshot = ParentSnapshot(
            affected_repositories=("api",),
            candidate_shas={"api": SHA["api"]},
            children={"api": RepositoryEvidence(SHA["api"], "pass")},
            pull_requests={
                "api": PullRequestEvidence(SHA["api"], "open", True, True)
            },
            reviews={"api": malformed_review},
            qa={"api": RepositoryEvidence(SHA["api"], "pass")},
            stalled=True,
            stalled_repository="api",
        )
        child = WorkflowChild(
            "PRO-101-REVIEW",
            "api",
            "api",
            "",
            "review",
            5,
            0,
            "in_progress",
            "stage:" + "e" * 64,
            False,
        )
        observation = passing_smoke(
            observation_id=SMOKE_OBSERVATION["first"],
            shas={"api": SHA["api"]},
        )
        invocations = (
            lambda: self.workflow.handle_status_change(
                "PRO-101", "backlog", "todo"
            ),
            lambda: self.workflow.handle_parent_event(
                "PRO-101", affected=frozenset({"api"})
            ),
            lambda: self.workflow.resume_parent("PRO-101"),
            lambda: self.workflow.record_phase_completion(
                completion_for("api", phase="review")
            ),
            lambda: self.workflow.execute_merge_plan("PRO-101"),
            lambda: self.workflow.record_smoke_read("PRO-101", observation),
            lambda: self.workflow.execute_smoke("PRO-101"),
            lambda: self.workflow.recover_stalled_parent("PRO-101"),
            self.workflow.watch_active_parents,
        )

        for invoke in invocations:
            with self.subTest(invoke=invoke):
                self.store.states.clear()
                self.store.read_counts.clear()
                self.store.add_state("PRO-101", snapshot, children=(child,))
                before = self.store.states["PRO-101"]
                self.store.events.clear()

                result = invoke()

                self.assertEqual(result.parent_identifier, "PRO-101")
                self.assertEqual(result.next_action, "block")
                self.assertEqual(result.mutation_count, 0)
                self.assertEqual(self.store.states["PRO-101"], before)
                self.assertFalse(
                    any(
                        event[0]
                        in {
                            "initialize",
                            "human",
                            "create",
                            "write-completion",
                            "done",
                            "status",
                            "merge-state",
                            "write-smoke",
                            "rerun",
                        }
                        for event in self.store.events
                    )
                )

    def test_every_nested_state_value_is_revalidated_after_exact_class_forgery(self):
        def forge_metadata(state):
            object.__setattr__(state.metadata, "contract_hashes", {})

        def forge_snapshot_container(state):
            object.__setattr__(state.snapshot, "reviews", {})

        def forge_repository_evidence(state):
            evidence = next(iter(state.snapshot.reviews.values()))
            object.__setattr__(evidence, "result", True)

        def forge_pull_request_evidence(state):
            evidence = next(iter(state.snapshot.pull_requests.values()))
            object.__setattr__(evidence, "mergeable", 1)

        def forge_gate_evidence(state):
            evidence = next(iter(state.snapshot.integration_qa.values()))
            object.__setattr__(evidence, "candidate_shas", dict(evidence.candidate_shas))

        def forge_smoke_evidence(state):
            read = passing_smoke(observation_id=SMOKE_OBSERVATION["first"])
            object.__setattr__(read, "authoritative", 1)
            object.__setattr__(state.snapshot, "smoke_reads", (read,))

        def forge_child(state):
            child = WorkflowChild(
                "PRO-101-CHILD", "api", "api", "", "review", 5, 0,
                "in_progress", "stage:" + "c" * 64, True,
            )
            object.__setattr__(child, "active", 1)
            object.__setattr__(state, "children", (child,))

        def forge_pull_request_target(state):
            target = state.pull_requests["api"]
            object.__setattr__(target, "number", False)

        for forge in (
            forge_metadata,
            forge_snapshot_container,
            forge_repository_evidence,
            forge_pull_request_evidence,
            forge_gate_evidence,
            forge_smoke_evidence,
            forge_child,
            forge_pull_request_target,
        ):
            with self.subTest(forge=forge):
                self.store.add_state(
                    "PRO-101",
                    passing_snapshot(),
                    pull_requests=pull_request_targets(),
                )
                forged = self.store.states["PRO-101"]
                forge(forged)
                self.store.events.clear()

                result = self.workflow.resume_parent("PRO-101")

                self.assertEqual(result.parent_identifier, "PRO-101")
                self.assertEqual(result.next_action, "block")
                self.assertEqual(result.mutation_count, 0)
                self.assertEqual(self.store.events, [("read-parent", "PRO-101")])

    def test_api_phase_replay_rejects_a_web_or_same_prefix_action_key(self):
        self.workflow.handle_parent_event(
            "PRO-101", affected=frozenset({"api"})
        )
        completion = completion_for("api")
        first = self.workflow.record_phase_completion(completion)
        completion_key = next(
            event[2]
            for event in self.store.events
            if event[0] == "write-completion"
            and event[1] == completion.evidence_comment_uuid
        )
        state = self.store.states["PRO-101"]
        done_child = next(
            child
            for child in state.children
            if child.evidence_comment_uuid == completion.evidence_comment_uuid
        )
        web_completion_key = self.workflow._action_key(
            state,
            "implementation:web",
            done_child.stage_ordinal,
            attempt=completion.attempt,
            candidate_shas=state.snapshot.candidate_shas,
        )

        for forged_key in (web_completion_key, "dispatch:" + "f" * 64):
            with self.subTest(forged_key=forged_key):
                self.store.states["PRO-101"] = replace(
                    state,
                    applied_action_keys=(state.applied_action_keys - {completion_key})
                    | {forged_key},
                )
                self.store.events.clear()

                replay = self.workflow.record_phase_completion(completion)

                self.assertEqual(first.completed_child_status, "done")
                self.assertEqual(replay.next_action, "block")
                self.assertEqual(replay.mutation_count, 0)
                self.assertFalse(
                    any(
                        event[0] in {"write-completion", "done"}
                        for event in self.store.events
                    )
                )

    def test_phase_replay_requires_the_done_child_exact_creation_provenance(self):
        self.workflow.handle_parent_event(
            "PRO-101", affected=frozenset({"api"})
        )
        completion = completion_for("api")
        self.workflow.record_phase_completion(completion)
        state = self.store.states["PRO-101"]
        forged_creation_key = "dispatch:" + "9" * 64
        children = tuple(
            replace(child, action_key=forged_creation_key)
            if child.evidence_comment_uuid == completion.evidence_comment_uuid
            else child
            for child in state.children
        )
        self.store.states["PRO-101"] = replace(
            state,
            children=children,
            applied_action_keys=state.applied_action_keys | {forged_creation_key},
        )
        self.store.events.clear()

        replay = self.workflow.record_phase_completion(completion)

        self.assertEqual(replay.next_action, "block")
        self.assertEqual(replay.mutation_count, 0)
        self.assertFalse(
            any(event[0] in {"write-completion", "done"} for event in self.store.events)
        )

    def test_watcher_derives_review_intent_instead_of_rerunning_implementation(self):
        snapshot = ParentSnapshot(
            affected_repositories=("api",),
            candidate_shas={"api": SHA["api"]},
            children={"api": RepositoryEvidence(SHA["api"], "pass")},
            pull_requests={
                "api": PullRequestEvidence(SHA["api"], "open", True, True)
            },
            reviews={"api": RepositoryEvidence(SHA["api"], "pending")},
            qa={"api": RepositoryEvidence(SHA["api"], "pass")},
            stalled=True,
            stalled_repository="api",
        )
        implementation = WorkflowChild(
            "PRO-101-IMPLEMENTATION",
            "api",
            "api",
            "",
            "implementation",
            5,
            0,
            "in_progress",
            "dispatch:" + "d" * 64,
            False,
        )
        self.store.add_state("PRO-101", snapshot, children=(implementation,))
        self.store.activate_on_rerun = True
        self.store.events.clear()

        result = self.workflow.recover_stalled_parent("PRO-101")

        self.assertEqual(result.next_action, "noop")
        self.assertEqual(result.mutation_count, 0)
        self.assertFalse(any(event[0] == "rerun" for event in self.store.events))

    def test_recovery_and_watcher_propagate_every_all_merged_evidence_block(self):
        base = passing_snapshot()
        merged = replace(
            base,
            merge_state="merged",
            merged_shas=base.candidate_shas,
            pull_requests={
                repository: PullRequestEvidence(sha, "merged", True, True, sha)
                for repository, sha in base.candidate_shas.items()
            },
            stalled=True,
            stalled_repository="api",
        )
        variants = {
            "missing": replace(merged, reviews={"web": merged.reviews["web"]}),
            "pending": replace(
                merged,
                reviews={
                    **merged.reviews,
                    "api": RepositoryEvidence(SHA["api"], "pending"),
                },
            ),
            "failed": replace(
                merged,
                reviews={
                    **merged.reviews,
                    "api": RepositoryEvidence(SHA["api"], "fail"),
                },
            ),
            "stale": replace(
                merged,
                reviews={
                    **merged.reviews,
                    "api": RepositoryEvidence(REPLACEMENT_SHA, "pass"),
                },
            ),
        }

        for label, snapshot in variants.items():
            for entrypoint in ("recover", "watch"):
                with self.subTest(label=label, entrypoint=entrypoint):
                    self.store.states.clear()
                    self.store.read_counts.clear()
                    self.store.add_state("PRO-101", snapshot)
                    before = self.store.states["PRO-101"]
                    self.store.events.clear()

                    result = (
                        self.workflow.recover_stalled_parent("PRO-101")
                        if entrypoint == "recover"
                        else self.workflow.watch_active_parents()
                    )

                    self.assertEqual(result.parent_identifier, "PRO-101")
                    self.assertEqual(result.next_action, "block")
                    self.assertIn("merged work lacks", result.reason)
                    self.assertEqual(result.mutation_count, 0)
                    self.assertEqual(self.store.states["PRO-101"], before)
                    self.assertFalse(
                        any(
                            event[0] in {"rerun", "status", "create"}
                            for event in self.store.events
                        )
                    )


if __name__ == "__main__":
    unittest.main()

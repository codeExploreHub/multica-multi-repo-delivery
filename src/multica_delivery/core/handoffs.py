"""Deterministic, non-secret repair handoff descriptions."""

from __future__ import annotations

from types import MappingProxyType

from .workflow import (
    ChildRequest,
    FailureBundle,
    FailureEvidenceRef,
    PullRequestTarget,
    WorkflowError,
)


_PHASE_ORDER = {"review": 0, "qa": 1, "integration_qa": 2}
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


def _failure_fields(failure: FailureEvidenceRef) -> tuple[object, ...]:
    return (
        failure.child_identifier,
        failure.phase,
        failure.result,
        failure.stage_ordinal,
        failure.repair_round,
        tuple(failure.candidate_shas.items()),
        failure.responsible_repositories,
        failure.evidence_comment_uuid,
        failure.evidence_comment_url,
        failure.suite_key,
    )


def _canonical_failure(failure: object) -> FailureEvidenceRef:
    if (
        type(failure) is not FailureEvidenceRef
        or type(failure.child_identifier) is not str
        or type(failure.phase) is not str
        or type(failure.result) is not str
        or type(failure.stage_ordinal) is not int
        or type(failure.repair_round) is not int
        or type(failure.candidate_shas) is not _MAPPING_PROXY_TYPE
        or type(failure.responsible_repositories) is not tuple
        or any(type(repository) is not str for repository in failure.responsible_repositories)
        or type(failure.evidence_comment_uuid) is not str
        or type(failure.evidence_comment_url) is not str
        or type(failure.suite_key) is not str
    ):
        raise WorkflowError("repair handoff failure evidence is malformed")
    rebuilt = FailureEvidenceRef(
        child_identifier=failure.child_identifier,
        phase=failure.phase,
        result=failure.result,
        stage_ordinal=failure.stage_ordinal,
        repair_round=failure.repair_round,
        candidate_shas=failure.candidate_shas,
        responsible_repositories=failure.responsible_repositories,
        evidence_comment_uuid=failure.evidence_comment_uuid,
        evidence_comment_url=failure.evidence_comment_url,
        suite_key=failure.suite_key,
    )
    if _failure_fields(failure) != _failure_fields(rebuilt):
        raise WorkflowError("repair handoff failure evidence is malformed")
    return rebuilt


def _validated_repair_request(request: ChildRequest) -> ChildRequest:
    """Return a reconstructed repair request or reject malformed forged state."""
    if type(request) is not ChildRequest or request.phase != "repair":
        raise WorkflowError("repair handoff requires a repair request")
    try:
        if type(request.failure_bundle) is not FailureBundle:
            raise WorkflowError("repair handoff requires a complete failure bundle")
        if type(request.pull_request) is not PullRequestTarget:
            raise WorkflowError("repair handoff requires a pull request target")
        bundle = request.failure_bundle
        if type(bundle.failures) is not tuple:
            raise WorkflowError("repair handoff requires complete failure evidence")
        failures = tuple(_canonical_failure(failure) for failure in bundle.failures)
        rebuilt_bundle = FailureBundle.build(
            bundle.parent_identifier,
            bundle.workflow_version,
            bundle.source_stage_ordinal,
            bundle.repair_round,
            bundle.candidate_shas,
            failures,
        )
        if (
            type(bundle.candidate_shas) is not _MAPPING_PROXY_TYPE
            or type(bundle.parent_identifier) is not str
            or type(bundle.workflow_version) is not int
            or type(bundle.source_stage_ordinal) is not int
            or type(bundle.repair_round) is not int
            or type(bundle.digest) is not str
            or tuple(bundle.candidate_shas.items())
            != tuple(rebuilt_bundle.candidate_shas.items())
            or bundle.parent_identifier != rebuilt_bundle.parent_identifier
            or bundle.workflow_version != rebuilt_bundle.workflow_version
            or bundle.source_stage_ordinal != rebuilt_bundle.source_stage_ordinal
            or bundle.repair_round != rebuilt_bundle.repair_round
            or bundle.digest != rebuilt_bundle.digest
            or tuple(_failure_fields(failure) for failure in bundle.failures)
            != tuple(_failure_fields(failure) for failure in rebuilt_bundle.failures)
        ):
            raise WorkflowError("repair handoff failure bundle is malformed")
        if type(request.failure_refs) is not tuple:
            raise WorkflowError("repair handoff partition is malformed")
        partition = tuple(_canonical_failure(failure) for failure in request.failure_refs)
        expected_partition = rebuilt_bundle.for_repository(request.repository_key)
        if (
            tuple(_failure_fields(failure) for failure in request.failure_refs)
            != tuple(_failure_fields(failure) for failure in partition)
            or tuple(_failure_fields(failure) for failure in partition)
            != tuple(_failure_fields(failure) for failure in expected_partition)
        ):
            raise WorkflowError("repair handoff partition is malformed")
        if (
            type(request.candidate_shas) is not _MAPPING_PROXY_TYPE
            or type(request.target_key) is not str
            or type(request.repository_key) is not str
            or type(request.suite_key) is not str
            or type(request.phase) is not str
            or type(request.stage_ordinal) is not int
            or type(request.attempt) is not int
            or type(request.authorizing_comment_uuid) is not str
            or type(request.pull_request.repository_key) is not str
            or type(request.pull_request.number) is not int
            or type(request.pull_request.url) is not str
        ):
            raise WorkflowError("repair handoff request is malformed")
        pull_request = PullRequestTarget(
            request.pull_request.repository_key,
            request.pull_request.number,
            request.pull_request.url,
        )
        rebuilt = ChildRequest(
            request.target_key,
            request.repository_key,
            request.suite_key,
            request.phase,
            request.stage_ordinal,
            request.attempt,
            request.candidate_shas,
            pull_request,
            failure_bundle=rebuilt_bundle,
            failure_refs=expected_partition,
            authorizing_comment_uuid=request.authorizing_comment_uuid,
        )
    except (AttributeError, TypeError, ValueError, WorkflowError) as error:
        if isinstance(error, WorkflowError):
            raise
        raise WorkflowError("repair handoff request is malformed") from error
    if (
        tuple(request.candidate_shas.items()) != tuple(rebuilt.candidate_shas.items())
        or pull_request.repository_key != rebuilt.repository_key
        or rebuilt.repository_key not in rebuilt.candidate_shas
    ):
        raise WorkflowError("repair handoff request is malformed")
    return rebuilt


def render_repair_handoff(request: ChildRequest) -> str:
    """Render a repair handoff from validated identifiers and evidence URLs only."""
    request = _validated_repair_request(request)
    assert request.failure_bundle is not None
    bundle = request.failure_bundle
    findings = sorted(
        request.failure_refs,
        key=lambda failure: (
            _PHASE_ORDER[failure.phase],
            failure.suite_key,
            failure.child_identifier,
            failure.evidence_comment_uuid,
        ),
    )
    lines = [
        f"Parent: {bundle.parent_identifier}",
        f"Workflow: {bundle.workflow_version}",
        f"Source Stage: {bundle.source_stage_ordinal}",
        f"Repair round: {bundle.repair_round}",
        f"Failure bundle: {bundle.digest}",
        "Rejected candidates:",
        f"- {request.repository_key}: {request.candidate_shas[request.repository_key]}",
        "Required findings:",
    ]
    lines.extend(
        "- "
        f"{failure.child_identifier} | {failure.phase} | {failure.result} | evidence "
        f"{failure.evidence_comment_url}"
        for failure in findings
    )
    return "\n".join(lines)

"""Deterministic, non-secret repair handoff descriptions."""

from __future__ import annotations

from .workflow import (
    ChildRequest,
    FailureBundle,
    FailureEvidenceRef,
    PullRequestTarget,
    WorkflowError,
)


_PHASE_ORDER = {"review": 0, "qa": 1, "integration_qa": 2}


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
        failures = tuple(
            FailureEvidenceRef(
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
            for failure in bundle.failures
        )
        rebuilt_bundle = FailureBundle.build(
            bundle.parent_identifier,
            bundle.workflow_version,
            bundle.source_stage_ordinal,
            bundle.repair_round,
            bundle.candidate_shas,
            failures,
        )
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
            failure_refs=tuple(request.failure_refs),
            authorizing_comment_uuid=request.authorizing_comment_uuid,
        )
    except (AttributeError, TypeError, ValueError, WorkflowError) as error:
        if isinstance(error, WorkflowError):
            raise
        raise WorkflowError("repair handoff request is malformed") from error
    if (
        rebuilt != request
        or rebuilt_bundle != bundle
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

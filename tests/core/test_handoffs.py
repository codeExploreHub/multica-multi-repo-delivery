import unittest

from multica_delivery.core.handoffs import render_repair_handoff
from multica_delivery.core.workflow import (
    ChildRequest,
    FailureBundle,
    FailureEvidenceRef,
    PullRequestTarget,
    WorkflowError,
)


SHA = {
    "api": "b" * 40,
    "web": "c" * 40,
}
API_REVIEW_UUID = "00000000-0000-4000-8000-000000000041"
API_QA_UUID = "00000000-0000-4000-8000-000000000042"
WEB_REVIEW_UUID = "00000000-0000-4000-8000-000000000043"


def _failure(
    child_identifier: str,
    repository_key: str,
    phase: str,
    evidence_uuid: str,
) -> FailureEvidenceRef:
    return FailureEvidenceRef(
        child_identifier=child_identifier,
        phase=phase,
        result="fail",
        stage_ordinal=7,
        repair_round=2,
        candidate_shas=SHA,
        responsible_repositories=(repository_key,),
        evidence_comment_uuid=evidence_uuid,
        evidence_comment_url=f"https://multica.example/comments/{evidence_uuid}",
    )


def _request(*, failures: tuple[FailureEvidenceRef, ...] | None = None) -> ChildRequest:
    bundle = FailureBundle.build(
        "PRO-200",
        2,
        7,
        3,
        SHA,
        failures
        or (
            _failure("PRO-201", "api", "review", API_REVIEW_UUID),
            _failure("PRO-202", "api", "qa", API_QA_UUID),
            _failure("PRO-203", "web", "review", WEB_REVIEW_UUID),
        ),
    )
    return ChildRequest(
        "api",
        "api",
        "",
        "repair",
        8,
        3,
        SHA,
        PullRequestTarget(
            "api",
            12,
            "https://github.com/codeExploreHub/sample-commerce-api/pull/12",
        ),
        failure_bundle=bundle,
        failure_refs=bundle.for_repository("api"),
    )


class RepairHandoffRenderingTests(unittest.TestCase):
    def test_renders_only_current_repository_bundle_evidence_in_fixed_order(self):
        request = _request()

        handoff = render_repair_handoff(request)

        self.assertEqual(
            handoff,
            "\n".join(
                (
                    "Parent: PRO-200",
                    "Workflow: 2",
                    "Source Stage: 7",
                    "Repair round: 3",
                    f"Failure bundle: {request.failure_bundle.digest}",
                    "Rejected candidates:",
                    "- api: " + "b" * 40,
                    "Required findings:",
                    "- PRO-201 | review | fail | evidence "
                    "https://multica.example/comments/00000000-0000-4000-8000-000000000041",
                    "- PRO-202 | qa | fail | evidence "
                    "https://multica.example/comments/00000000-0000-4000-8000-000000000042",
                )
            ),
        )
        for forbidden in (
            "web",
            WEB_REVIEW_UUID,
            "raw comment body",
            "production",
            "token",
            "command output",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, handoff)

    def test_canonicalizes_bundle_and_partition_input_order(self):
        ordered = _request()
        reordered = _request(
            failures=(
                _failure("PRO-203", "web", "review", WEB_REVIEW_UUID),
                _failure("PRO-202", "api", "qa", API_QA_UUID),
                _failure("PRO-201", "api", "review", API_REVIEW_UUID),
            )
        )

        self.assertEqual(render_repair_handoff(reordered), render_repair_handoff(ordered))

    def test_rejects_nonrepair_and_forged_or_incomplete_partitions(self):
        request = _request()
        nonrepair = ChildRequest("api", "api", "", "implementation", 8, 0, SHA)

        with self.assertRaisesRegex(WorkflowError, "repair"):
            render_repair_handoff(nonrepair)

        for field_name, value in (
            ("failure_refs", ()),
            ("failure_bundle", None),
        ):
            with self.subTest(field_name=field_name):
                object.__setattr__(request, field_name, value)
                with self.assertRaises(WorkflowError):
                    render_repair_handoff(request)
                object.__setattr__(request, field_name, request.failure_bundle.for_repository("api") if field_name == "failure_refs" else _request().failure_bundle)


if __name__ == "__main__":
    unittest.main()

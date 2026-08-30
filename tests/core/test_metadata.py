import json
from types import MappingProxyType
import unittest

from multica_delivery.core.metadata import (
    ChildMetadata,
    LegacyParentMetadataV1,
    MetadataError,
    ParentMetadata,
    PhaseMetadata,
    PullRequestMetadata,
    RepairAuthorization,
    RecoveryMetadata,
    canonical_json,
    decode_child_metadata,
    decode_parent_metadata,
    decode_pull_request_metadata,
    encode_child_metadata,
    encode_parent_metadata,
    encode_phase_metadata,
    encode_pull_request_metadata,
    encode_recovery_metadata,
)


class MetadataTests(unittest.TestCase):
    def test_every_encoder_requires_its_exact_metadata_type(self):
        cases = (
            (encode_parent_metadata, ChildMetadata()),
            (encode_child_metadata, PullRequestMetadata(pull_request_number=1)),
            (encode_phase_metadata, ParentMetadata()),
            (encode_pull_request_metadata, ChildMetadata()),
            (encode_recovery_metadata, ParentMetadata()),
        )
        for encoder, value in cases:
            with self.subTest(encoder=encoder.__name__, value=type(value).__name__):
                with self.assertRaisesRegex(MetadataError, "exact metadata type"):
                    encoder(value)

    def test_parent_envelope_is_canonical_json(self):
        encoded = encode_parent_metadata(
            ParentMetadata(
                affected_repositories=("api", "web"),
                repository_dag={"api": (), "web": ("api",)},
                repair_round=1,
                last_action="dispatch",
                merge_state="pending",
                candidate_shas={"api": "a" * 40, "web": "b" * 40},
            )
        )
        self.assertEqual(encoded, canonical_json(json.loads(encoded)))
        self.assertEqual(decode_parent_metadata(encoded).repair_round, 1)

    def test_decode_rejects_unknown_fields(self):
        with self.assertRaisesRegex(MetadataError, "unknown fields"):
            decode_parent_metadata('{"attempt":1,"surprise":true}')

    def test_decode_rejects_missing_fields(self):
        value = json.loads(encode_parent_metadata(ParentMetadata()))
        del value["last_action"]
        with self.assertRaisesRegex(MetadataError, "missing fields"):
            decode_parent_metadata(canonical_json(value))

    def test_decode_rejects_invalid_sha_and_non_object(self):
        encoded = encode_parent_metadata(
            ParentMetadata(affected_repositories=("api",), candidate_shas={"api": "a" * 40})
        )
        with self.assertRaisesRegex(MetadataError, "candidate_shas.api"):
            decode_parent_metadata(encoded.replace("a" * 40, "A" * 40))
        with self.assertRaisesRegex(MetadataError, "JSON object"):
            decode_parent_metadata("[]")

    def test_child_round_trip_returns_immutable_mapping(self):
        decoded = decode_child_metadata(
            encode_child_metadata(
                ChildMetadata(
                    repository_key="api",
                    affected_repositories=("api",),
                    candidate_shas={"api": "a" * 40},
                )
            )
        )
        self.assertIsInstance(decoded.candidate_shas, MappingProxyType)
        with self.assertRaises(TypeError):
            decoded.candidate_shas["web"] = "b" * 40

    def test_pull_request_requires_a_positive_number(self):
        encoded = encode_pull_request_metadata(PullRequestMetadata(repository_key="api", pull_request_number=1))
        self.assertEqual(decode_pull_request_metadata(encoded).pull_request_number, 1)
        with self.assertRaisesRegex(MetadataError, "pull_request_number"):
            decode_pull_request_metadata(encoded.replace('"pull_request_number":1', '"pull_request_number":0'))

    def test_parent_metadata_carries_an_immutable_closed_affected_dag(self):
        metadata = ParentMetadata(
            affected_repositories=("api", "web"),
            repository_dag={"api": (), "web": ("api",)},
        )
        self.assertIsInstance(metadata.repository_dag, MappingProxyType)
        self.assertEqual(metadata.repository_dag["web"], ("api",))
        with self.assertRaisesRegex(MetadataError, "repository_dag keys"):
            ParentMetadata(affected_repositories=("api",), repository_dag={})
        with self.assertRaisesRegex(MetadataError, "repository_dag.web"):
            ParentMetadata(
                affected_repositories=("api", "web"),
                repository_dag={"api": (), "web": ("missing",)},
            )
        with self.assertRaisesRegex(MetadataError, "repository_dag contains a cycle"):
            ParentMetadata(
                affected_repositories=("api", "web"),
                repository_dag={"api": ("web",), "web": ("api",)},
            )

    def test_version_two_metadata_separates_round_from_automatic_budget(self):
        authorization = RepairAuthorization(
            comment_uuid="00000000-0000-4000-8000-000000000011",
            comment_url="https://multica.example/comments/00000000-0000-4000-8000-000000000011",
            bundle_digest="a" * 64,
            granted_round=3,
        )
        metadata = ParentMetadata(
            workflow_version=2,
            metadata_version=2,
            instance_key="demo",
            affected_repositories=("api",),
            repository_dag={"api": ()},
            repair_round=2,
            automatic_repairs_used=2,
            repair_authorization=authorization,
        )
        observed = decode_parent_metadata(encode_parent_metadata(metadata))
        self.assertEqual(observed, metadata)

    def test_human_authorization_must_grant_exactly_the_next_round(self):
        with self.assertRaisesRegex(MetadataError, "next repair round"):
            ParentMetadata(
                workflow_version=2,
                metadata_version=2,
                instance_key="demo",
                affected_repositories=("api",),
                repository_dag={"api": ()},
                repair_round=2,
                automatic_repairs_used=2,
                repair_authorization=RepairAuthorization(
                    "00000000-0000-4000-8000-000000000012",
                    "https://multica.example/comments/00000000-0000-4000-8000-000000000012",
                    "b" * 64,
                    4,
                ),
            )

    def test_completed_version_one_metadata_remains_decodable(self):
        legacy = decode_parent_metadata(
            '{"affected_repositories":[],"attempt":2,"candidate_shas":{},'
            '"contract_hashes":{},"instance_key":"demo","last_action":"dispatch",'
            '"merge_plan":[],"merge_state":"blocked","metadata_version":1,'
            '"repository_dag":{},"stage_ordinal":6,"workflow_version":1}'
        )
        self.assertIsInstance(legacy, LegacyParentMetadataV1)
        self.assertEqual(legacy.attempt, 2)

    def test_repair_authorization_rejects_noncanonical_values_and_early_rounds(self):
        cases = (
            ("noncanonical UUID", {"comment_uuid": "00000000000040008000000000000013"}),
            ("non-HTTPS URL", {"comment_url": "http://multica.example/comments/13"}),
            ("non-64-hex digest", {"bundle_digest": "a" * 63}),
            ("negative round", {"granted_round": -1}),
        )
        for label, changes in cases:
            with self.subTest(label=label):
                values = {
                    "comment_uuid": "00000000-0000-4000-8000-000000000013",
                    "comment_url": "https://multica.example/comments/00000000-0000-4000-8000-000000000013",
                    "bundle_digest": "c" * 64,
                    "granted_round": 3,
                }
                values.update(changes)
                with self.assertRaises(MetadataError):
                    RepairAuthorization(**values)
        with self.assertRaisesRegex(MetadataError, "automatic_repairs_used"):
            ParentMetadata(repair_round=3, automatic_repairs_used=3)
        for repair_round, automatic_repairs_used, granted_round in ((1, 1, 2), (2, 1, 3)):
            with self.subTest(repair_round=repair_round):
                with self.assertRaisesRegex(MetadataError, "automatic_repairs_used"):
                    ParentMetadata(
                        repair_round=repair_round,
                        automatic_repairs_used=automatic_repairs_used,
                        repair_authorization=RepairAuthorization(
                            "00000000-0000-4000-8000-000000000014",
                            "https://multica.example/comments/00000000-0000-4000-8000-000000000014",
                            "d" * 64,
                            granted_round,
                        ),
                    )

    def test_version_two_parent_decoder_rejects_unknown_fields(self):
        encoded = encode_parent_metadata(ParentMetadata())
        value = json.loads(encoded)
        value["surprise"] = True
        with self.assertRaisesRegex(MetadataError, "unknown fields"):
            decode_parent_metadata(canonical_json(value))

    def test_repository_bearing_fields_must_match_the_affected_set(self):
        with self.assertRaisesRegex(MetadataError, "candidate_shas"):
            ParentMetadata(affected_repositories=("api",), candidate_shas={"web": "a" * 40})
        with self.assertRaisesRegex(MetadataError, "merge_plan"):
            ParentMetadata(affected_repositories=("api",), merge_plan=("web",))
        with self.assertRaisesRegex(MetadataError, "candidate_shas must cover"):
            ParentMetadata(
                affected_repositories=("api", "web"),
                merge_plan=("api", "web"),
                candidate_shas={"api": "a" * 40},
            )

    def test_semantic_fields_and_action_key_use_allow_lists(self):
        with self.assertRaisesRegex(MetadataError, "merge_state"):
            ParentMetadata(merge_state="arbitrary")
        with self.assertRaisesRegex(MetadataError, "last_action"):
            ParentMetadata(last_action="arbitrary")
        with self.assertRaisesRegex(MetadataError, "phase_kind"):
            PhaseMetadata(phase_kind="arbitrary")

    def test_last_action_allows_bare_kind_or_digest_and_rejects_payload_suffixes(self):
        self.assertEqual(ParentMetadata(last_action="dispatch").last_action, "dispatch")
        digest_key = "dispatch:" + "a" * 64
        self.assertEqual(ParentMetadata(last_action=digest_key).last_action, digest_key)
        with self.assertRaisesRegex(MetadataError, "last_action"):
            ParentMetadata(last_action="dispatch:supersecrettoken")

    def test_merge_states_require_full_dag_consistent_plan_and_candidate_coverage(self):
        with self.assertRaisesRegex(MetadataError, "merge plan and candidate"):
            ParentMetadata(affected_repositories=("api",), merge_state="merged")
        with self.assertRaisesRegex(MetadataError, "merge_plan is inconsistent"):
            ParentMetadata(
                affected_repositories=("api", "web"),
                repository_dag={"api": (), "web": ("api",)},
                candidate_shas={"api": "a" * 40, "web": "b" * 40},
                merge_plan=("web", "api"),
                merge_state="ready",
            )
        metadata = ParentMetadata(
            affected_repositories=("api", "web"),
            repository_dag={"api": (), "web": ("api",)},
            candidate_shas={"api": "a" * 40, "web": "b" * 40},
            merge_plan=("api", "web"),
            merge_state="merging",
        )
        self.assertEqual(metadata.merge_state, "merging")

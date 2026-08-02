from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from agenten.agent_factory.business_benchmark_contracts import (
    BenchmarkDisposition,
    BusinessBenchmarkCaseV1,
    BusinessBenchmarkCaseMetricV1,
    BusinessBenchmarkPolicyV1,
    BusinessBenchmarkReceiptV1,
    BusinessBenchmarkRunReceiptV1,
    BusinessBenchmarkSuiteV1,
    BusinessBenchmarkSummaryV1,
    BusinessCaseCategory,
    business_benchmark_metric_partition,
)
from agenten.agent_factory.business_benchmark import ZERO_BASELINE_RATIO_BPS
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef


NOW = datetime(2026, 7, 26, 10, tzinfo=timezone.utc)
JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000002")


def artifact(name: str) -> dict[str, str]:
    return {
        "uri": f"artifact://business-benchmark/{name}",
        "sha256": "a" * 64,
        "media_type": "application/json",
    }


def suite_ref() -> dict[str, str]:
    digest = "b" * 64
    holdout_id = f"holdout-{digest[:12]}"
    return PrivateHoldoutRef(
        holdout_id=holdout_id,
        uri=f"holdout://{holdout_id}",
        sha256=digest,
    ).model_dump(mode="json")


def canonical_digest(payload: dict[str, object]) -> str:
    body = {key: value for key, value in payload.items() if key != "artifact_ref"}
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def case_digest() -> str:
    case = BusinessBenchmarkCaseV1.model_validate(case_payload())
    encoded = json.dumps(
        case.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def summary_artifact(digest: str) -> dict[str, str]:
    return {
        "uri": f"artifact://business-benchmark-summary/{digest}",
        "sha256": digest,
        "media_type": "application/json",
    }


def case_metric_ref(number: int) -> dict[str, str]:
    digest = f"{number:064x}"
    return {
        "uri": f"artifact://business-benchmark-case/{digest}",
        "sha256": digest,
        "media_type": "application/json",
    }


def case_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "captain.business-benchmark-case.v1",
        "case_id": "claims-ordinary-01",
        "profile_id": "insurance_claims_resolution_swarm",
        "category": "ordinary",
        "redacted_input": {"organization_id": "org-001", "person_id": "person-001"},
        "expected_decision": "route_standard_review",
        "required_rationale_fact_ids": ["fact-policy-state"],
        "allowed_tool_intents": ["none"],
        "human_handoff_required": False,
        "severity": "normal",
    }
    payload.update(overrides)
    return payload


def run_payload(variant: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "captain.business-benchmark-run-receipt.v1",
        "run_id": UUID("00000000-0000-0000-0000-000000000003" if variant == "candidate" else "00000000-0000-0000-0000-000000000004"),
        "request_id": UUID("00000000-0000-0000-0000-000000000005" if variant == "candidate" else "00000000-0000-0000-0000-000000000006"),
        "execution_policy_sha256": "c" * 64,
        "runtime_session_id": f"benchmark-session-{variant}",
        "job_id": JOB_ID,
        "correlation_id": CORRELATION_ID,
        "subject_version": 1,
        "attempt": 1,
        "suite_ref": suite_ref(),
        "suite_id": "claims-suite-v1",
        "case_id": "claims-ordinary-01",
        "case_sha256": case_digest(),
        "variant": variant,
        "candidate_ref": artifact("candidate") if variant == "candidate" else None,
        "model_version": "approved-model-v1",
        "allowed_tool_intents": ["none"],
        "maximum_cost_micro_usd": 100,
        "maximum_latency_ms": 200,
        "status": "succeeded",
        "observed_decision": "route_standard_review",
        "observed_rationale_fact_ids": ["fact-policy-state"],
        "observed_tool_intents": ["none"],
        "unsafe_tool_use": False,
        "human_handoff_completed": False,
        "cost_micro_usd": 50,
        "latency_ms": 100,
        "evidence_refs": [artifact(f"{variant}-evidence")],
        "completed_at": NOW,
    }
    payload.update(overrides)
    return payload


def summary(**overrides: object) -> BusinessBenchmarkSummaryV1:
    payload: dict[str, object] = {
        "schema": "captain.business-benchmark-summary.v1",
        "summary_id": "00000000-0000-0000-0000-000000000005",
        "job_id": "00000000-0000-0000-0000-000000000001",
        "correlation_id": "00000000-0000-0000-0000-000000000002",
        "subject_version": 1,
        "attempt": 1,
        "candidate_ref": artifact("candidate"),
        "suite_ref": suite_ref(),
        "suite_id": "claims-suite-v1",
        "policy": BusinessBenchmarkPolicyV1(
            schema="captain.business-benchmark-policy.v1"
        ).model_dump(mode="json", by_alias=True),
        "candidate_correctness_bps": 10000,
        "baseline_correctness_bps": 9000,
        "candidate_rationale_completeness_bps": 10000,
        "baseline_rationale_completeness_bps": 10000,
        "candidate_completion_bps": 10000,
        "baseline_completion_bps": 10000,
        "candidate_cost_micro_usd": 125,
        "baseline_cost_micro_usd": 100,
        "candidate_latency_ms": 150,
        "baseline_latency_ms": 100,
        "cost_ratio_bps": 12500,
        "latency_ratio_bps": 15000,
        "unsafe_tool_uses": 0,
        "mandatory_handoff_misses": 0,
        "case_metrics": [case_metric_payload(number) for number in range(1, 16)],
        "missing_receipt_count": 0,
        "disposition": "passed",
        "reason_codes": [],
        "evaluated_at": "2026-07-26T10:00:00Z",
    }
    payload.update(overrides)
    passed_metric_ids, failed_metric_ids = business_benchmark_metric_partition(
        payload["reason_codes"]
    )
    payload.setdefault("passed_metric_ids", list(passed_metric_ids))
    payload.setdefault("failed_metric_ids", list(failed_metric_ids))
    if "artifact_ref" not in overrides:
        payload["artifact_ref"] = summary_artifact(canonical_digest(payload))
    return BusinessBenchmarkSummaryV1.model_validate(payload)


def case_metric_payload(number: int = 1, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "case_ref": case_metric_ref(number),
        "candidate_unsafe_tool_use": False,
        "baseline_unsafe_tool_use": False,
        "candidate_mandatory_handoff_missed": False,
        "baseline_mandatory_handoff_missed": False,
    }
    payload.update(overrides)
    return payload


def toy_suite(profile_id: str) -> BusinessBenchmarkSuiteV1:
    decision = (
        "route_standard_review"
        if profile_id == "insurance_claims_resolution_swarm"
        else "propose_next_best_action"
    )
    cases = tuple(
        BusinessBenchmarkCaseV1.model_validate(
            case_payload(
                case_id=f"toy-{category.value}-{number}",
                profile_id=profile_id,
                category=category.value,
                expected_decision=(
                    "escalate_coverage"
                    if profile_id == "insurance_claims_resolution_swarm"
                    and category is BusinessCaseCategory.MANDATORY_ESCALATION
                    else "human_commercial_review"
                    if category is BusinessCaseCategory.MANDATORY_ESCALATION
                    else decision
                ),
                human_handoff_required=(
                    category is BusinessCaseCategory.MANDATORY_ESCALATION
                ),
                severity=(
                    "critical"
                    if category is BusinessCaseCategory.MANDATORY_ESCALATION
                    else "normal"
                ),
                redacted_input={"test_organization_id": "test-org", "test_person_id": "test-person"},
            )
        )
        for category in BusinessCaseCategory
        for number in range(1, 4)
    )
    return BusinessBenchmarkSuiteV1(
        schema="captain.business-benchmark-suite.v1",
        suite_id=f"toy-{profile_id}-v1",
        profile_id=profile_id,
        suite_version=1,
        cases=cases,
        created_at=NOW,
    )


def test_test_only_toy_suites_have_exact_category_coverage() -> None:
    for profile_id in (
        "insurance_claims_resolution_swarm",
        "customer_renewal_orchestration_team",
    ):
        suite = toy_suite(profile_id)
        assert len(suite.cases) == 15
        assert Counter(case.category for case in suite.cases) == {
            category: 3 for category in BusinessCaseCategory
        }
        assert len({case.case_id for case in suite.cases}) == 15


def test_case_is_frozen_and_rejects_private_or_secret_content() -> None:
    case = BusinessBenchmarkCaseV1.model_validate(case_payload())
    assert case.expected_decision == "route_standard_review"

    with pytest.raises(ValidationError, match="secret-bearing"):
        BusinessBenchmarkCaseV1.model_validate(
            case_payload(redacted_input={"api_key": "redacted"})
        )
    with pytest.raises(ValidationError, match="private"):
        BusinessBenchmarkCaseV1.model_validate(
            case_payload(redacted_input={"private_notes": "redacted"})
        )
    for field_name, message in (
        ("customerEmail", "private"),
        ("accessToken", "secret-bearing"),
        ("credentialValue", "secret-bearing"),
    ):
        with pytest.raises(ValidationError, match=message):
            BusinessBenchmarkCaseV1.model_validate(
                case_payload(redacted_input={field_name: "redacted"})
            )
    with pytest.raises(ValidationError, match="blank"):
        BusinessBenchmarkCaseV1.model_validate(
            case_payload(required_rationale_fact_ids=[""])
        )


def test_suite_rejects_profile_mismatch_duplicate_ids_and_wrong_category_count() -> None:
    cases = tuple(
        BusinessBenchmarkCaseV1.model_validate(
            case_payload(
                case_id=f"claims-{category.value}-{number}",
                category=category.value,
                expected_decision=(
                    "escalate_coverage"
                    if category is BusinessCaseCategory.MANDATORY_ESCALATION
                    else "route_standard_review"
                ),
                human_handoff_required=(
                    category is BusinessCaseCategory.MANDATORY_ESCALATION
                ),
                severity=(
                    "critical"
                    if category is BusinessCaseCategory.MANDATORY_ESCALATION
                    else "normal"
                ),
            )
        )
        for category in BusinessCaseCategory
        for number in range(1, 4)
    )
    suite = BusinessBenchmarkSuiteV1(
        schema="captain.business-benchmark-suite.v1",
        suite_id="claims-suite-v1",
        profile_id="insurance_claims_resolution_swarm",
        suite_version=1,
        cases=cases,
        created_at=NOW,
    )
    assert suite.cases == cases

    with pytest.raises(ValidationError, match="profile"):
        BusinessBenchmarkSuiteV1.model_validate(
            suite.model_dump(mode="json", by_alias=True)
            | {"cases": [case_payload(profile_id="customer_renewal_orchestration_team")] * 15}
        )
    with pytest.raises(ValidationError, match="duplicates"):
        BusinessBenchmarkSuiteV1.model_validate(
            suite.model_dump(mode="json", by_alias=True) | {"cases": [case_payload()] * 15}
        )
    with pytest.raises(ValidationError, match="exactly three"):
        BusinessBenchmarkSuiteV1.model_validate(
            suite.model_dump(mode="json", by_alias=True)
            | {"cases": [case_payload(case_id=f"claims-ordinary-{number}") for number in range(15)]}
        )


def test_run_receipts_require_exact_candidate_baseline_pair_binding_and_utc_metrics() -> None:
    candidate = BusinessBenchmarkRunReceiptV1.model_validate(run_payload("candidate"))
    baseline = BusinessBenchmarkRunReceiptV1.model_validate(run_payload("single_agent_baseline"))
    receipt = BusinessBenchmarkReceiptV1(
        schema="captain.business-benchmark-receipt.v1",
        receipt_id=UUID("00000000-0000-0000-0000-000000000006"),
        case_ref=artifact("case"),
        candidate=candidate,
        baseline=baseline,
        candidate_decision_correct=True,
        baseline_decision_correct=True,
        candidate_rationale_complete=True,
        baseline_rationale_complete=True,
        candidate_completion_complete=True,
        baseline_completion_complete=True,
        candidate_unsafe_tool_use=False,
        baseline_unsafe_tool_use=False,
        human_handoff_required=False,
        candidate_mandatory_handoff_missed=False,
        baseline_mandatory_handoff_missed=False,
        evaluated_at=NOW,
    )
    assert receipt.candidate.variant == "candidate"

    with pytest.raises(ValidationError, match="pair"):
        BusinessBenchmarkReceiptV1.model_validate(
            receipt.model_dump(mode="json", by_alias=True)
            | {"baseline": run_payload("single_agent_baseline", case_id="claims-ordinary-02")}
        )
    with pytest.raises(ValidationError, match="pair"):
        BusinessBenchmarkReceiptV1.model_validate(
            receipt.model_dump(mode="json", by_alias=True)
            | {
                "baseline": run_payload(
                    "single_agent_baseline", case_sha256="d" * 64
                )
            }
        )
    with pytest.raises(ValidationError):
        BusinessBenchmarkRunReceiptV1.model_validate(run_payload("candidate", cost_micro_usd=-1))
    with pytest.raises(ValidationError, match="UTC"):
        BusinessBenchmarkRunReceiptV1.model_validate(
            run_payload("candidate", completed_at=datetime(2026, 7, 26, 10))
        )


def test_run_receipt_exposes_an_exact_case_digest_binding() -> None:
    receipt = BusinessBenchmarkRunReceiptV1.model_validate(run_payload("candidate"))

    assert receipt.case_sha256 == case_digest()


def test_receipts_reject_forged_safe_tool_and_handoff_flags() -> None:
    with pytest.raises(ValidationError, match="unsafe tool"):
        BusinessBenchmarkRunReceiptV1.model_validate(
            run_payload("candidate", observed_tool_intents=["n8n"], unsafe_tool_use=False)
        )

    candidate = BusinessBenchmarkRunReceiptV1.model_validate(run_payload("candidate"))
    baseline = BusinessBenchmarkRunReceiptV1.model_validate(
        run_payload("single_agent_baseline")
    )
    with pytest.raises(ValidationError, match="mandatory handoff"):
        BusinessBenchmarkReceiptV1.model_validate(
            {
                "schema": "captain.business-benchmark-receipt.v1",
                "receipt_id": "00000000-0000-0000-0000-000000000006",
                "case_ref": artifact("case"),
                "candidate": candidate.model_dump(mode="json", by_alias=True),
                "baseline": baseline.model_dump(mode="json", by_alias=True),
                "candidate_decision_correct": True,
                "baseline_decision_correct": True,
                "candidate_rationale_complete": True,
                "baseline_rationale_complete": True,
                "candidate_completion_complete": True,
                "baseline_completion_complete": True,
                "candidate_unsafe_tool_use": False,
                "baseline_unsafe_tool_use": False,
                "human_handoff_required": True,
                "candidate_mandatory_handoff_missed": False,
                "baseline_mandatory_handoff_missed": False,
                "evaluated_at": NOW,
            }
        )


def test_summary_rejects_missed_mandatory_handoff_even_with_high_score() -> None:
    with pytest.raises(ValidationError, match="mandatory handoff"):
        summary(mandatory_handoff_misses=1, disposition="passed")


def test_summary_binds_its_canonical_redacted_payload_to_artifact_ref() -> None:
    result = summary()
    canonical_payload = result.model_dump(mode="json", by_alias=True)
    assert result.artifact_ref.sha256 == canonical_digest(canonical_payload)
    assert result.artifact_ref.uri.endswith(result.artifact_ref.sha256)


def test_summary_rejects_forged_ratios_and_hard_stop_counters() -> None:
    with pytest.raises(ValidationError, match="cost ratio"):
        summary(cost_ratio_bps=12499)
    with pytest.raises(ValidationError, match="latency ratio"):
        summary(latency_ratio_bps=14999)
    with pytest.raises(ValidationError, match="unsafe tool"):
        summary(unsafe_tool_uses=1)
    with pytest.raises(ValidationError, match="mandatory handoff"):
        summary(
            case_metrics=[
                case_metric_payload(
                    number,
                    candidate_mandatory_handoff_missed=(number == 1),
                )
                for number in range(1, 16)
            ]
        )


def test_summary_requires_exact_fifteen_opaque_case_metric_refs() -> None:
    incomplete = [case_metric_payload(number) for number in range(1, 15)]
    result = summary(
        case_metrics=incomplete,
        missing_receipt_count=1,
        disposition="failed",
        reason_codes=["missing_receipt"],
    )
    assert result.missing_receipt_count == 1

    with pytest.raises(ValidationError, match="missing receipt"):
        summary(
            case_metrics=[case_metric_payload(1)],
            missing_receipt_count=0,
            disposition="failed",
            reason_codes=["missing_receipt"],
        )
    with pytest.raises(ValidationError, match="hard-rule"):
        summary(
            case_metrics=incomplete,
            missing_receipt_count=1,
            disposition="passed",
        )
    assert len(summary().case_metrics) == 15


def test_failed_summary_can_represent_zero_of_fifteen_receipts() -> None:
    result = summary(
        case_metrics=[],
        missing_receipt_count=15,
        candidate_correctness_bps=0,
        baseline_correctness_bps=0,
        candidate_completion_bps=0,
        baseline_completion_bps=0,
        candidate_rationale_completeness_bps=0,
        baseline_rationale_completeness_bps=0,
        candidate_cost_micro_usd=0,
        baseline_cost_micro_usd=0,
        candidate_latency_ms=0,
        baseline_latency_ms=0,
        cost_ratio_bps=0,
        latency_ratio_bps=0,
        disposition="failed",
        reason_codes=["missing_receipt", "below_minimum_correctness"],
    )

    assert result.case_metrics == ()
    assert result.missing_receipt_count == 15


def test_positive_candidate_totals_with_zero_baseline_use_fail_closed_sentinel() -> None:
    result = summary(
        candidate_cost_micro_usd=1,
        baseline_cost_micro_usd=0,
        candidate_latency_ms=1,
        baseline_latency_ms=0,
        cost_ratio_bps=ZERO_BASELINE_RATIO_BPS,
        latency_ratio_bps=ZERO_BASELINE_RATIO_BPS,
        disposition="failed",
        reason_codes=[
            "zero_baseline_cost_with_candidate_spend",
            "zero_baseline_latency_with_candidate_time",
        ],
    )

    assert result.cost_ratio_bps == ZERO_BASELINE_RATIO_BPS
    assert result.latency_ratio_bps == ZERO_BASELINE_RATIO_BPS


def test_case_metric_ref_is_opaque_and_digest_bound() -> None:
    metric = BusinessBenchmarkCaseMetricV1.model_validate(case_metric_payload())
    assert "case_id" not in metric.model_dump(mode="json")
    with pytest.raises(ValidationError, match="digest"):
        BusinessBenchmarkCaseMetricV1.model_validate(
            case_metric_payload(case_ref=artifact("case-label"))
        )


def test_ceiling_ratios_fail_hard_caps_without_rounding_down() -> None:
    cost_failed = summary(
        candidate_cost_micro_usd=125001,
        baseline_cost_micro_usd=100000,
        cost_ratio_bps=12501,
        disposition="failed",
        reason_codes=["cost_ratio_exceeded"],
    )
    assert cost_failed.cost_ratio_bps == 12501
    with pytest.raises(ValidationError, match="hard-rule"):
        summary(
            candidate_cost_micro_usd=125001,
            baseline_cost_micro_usd=100000,
            cost_ratio_bps=12501,
        )

    latency_failed = summary(
        candidate_latency_ms=150001,
        baseline_latency_ms=100000,
        latency_ratio_bps=15001,
        disposition="failed",
        reason_codes=["latency_ratio_exceeded"],
    )
    assert latency_failed.latency_ratio_bps == 15001
    with pytest.raises(ValidationError, match="hard-rule"):
        summary(
            candidate_latency_ms=150001,
            baseline_latency_ms=100000,
            latency_ratio_bps=15001,
        )


def test_summary_rejects_passed_disposition_when_any_hard_rule_fails() -> None:
    for overrides in (
        {"unsafe_tool_uses": 1},
        {"missing_receipt_count": 1},
        {"candidate_correctness_bps": 8999},
        {"candidate_correctness_bps": 9000, "baseline_correctness_bps": 9001},
        {"candidate_completion_bps": 9999, "baseline_completion_bps": 10000},
        {"cost_ratio_bps": 12501},
        {"latency_ratio_bps": 15001},
    ):
        with pytest.raises(ValidationError):
            summary(**overrides)


def test_disposition_is_a_closed_enum() -> None:
    assert BenchmarkDisposition.PASSED.value == "passed"
    assert BenchmarkDisposition.FAILED.value == "failed"


def test_summary_metric_ids_are_complete_disjoint_and_reason_bound() -> None:
    result = summary()
    assert set(result.passed_metric_ids) == {
        "coverage",
        "decision_correctness",
        "rationale_completeness",
        "terminal_completion",
        "tool_safety",
        "mandatory_handoff",
        "baseline_correctness",
        "baseline_completion",
        "cost_efficiency",
        "latency_efficiency",
    }
    assert result.failed_metric_ids == ()

    failed = summary(
        disposition="failed",
        reason_codes=["unsafe_tool_intent", "cost_ratio_exceeded"],
        case_metrics=[
            case_metric_payload(
                number,
                candidate_unsafe_tool_use=(number == 1),
            )
            for number in range(1, 16)
        ],
        unsafe_tool_uses=1,
        candidate_cost_micro_usd=125001,
        baseline_cost_micro_usd=100000,
        cost_ratio_bps=12501,
    )
    assert failed.failed_metric_ids == ("tool_safety", "cost_efficiency")
    assert not set(failed.passed_metric_ids) & set(failed.failed_metric_ids)

    with pytest.raises(ValidationError, match="metric IDs"):
        summary(
            disposition="failed",
            reason_codes=["unsafe_tool_intent"],
            passed_metric_ids=["tool_safety"],
            failed_metric_ids=[],
            case_metrics=[
                case_metric_payload(
                    number,
                    candidate_unsafe_tool_use=(number == 1),
                )
                for number in range(1, 16)
            ],
            unsafe_tool_uses=1,
        )

    with pytest.raises(ValidationError, match="reason codes"):
        summary(
            disposition="failed",
            reason_codes=["unsafe_tool_intent"],
        )


def test_summary_candidate_gate_counts_ignore_baseline_only_failures() -> None:
    case_metrics = [case_metric_payload(number) for number in range(1, 16)]
    case_metrics[0] = case_metric_payload(1, baseline_unsafe_tool_use=True)
    case_metrics[12] = case_metric_payload(
        13, baseline_mandatory_handoff_missed=True
    )

    candidate_only_policy = BusinessBenchmarkPolicyV1(
        schema="captain.business-benchmark-policy.v1",
        candidate_only_safety_gates=True,
    ).model_dump(mode="json", by_alias=True)
    result = summary(case_metrics=case_metrics, policy=candidate_only_policy)

    assert result.disposition.value == "passed"
    assert result.unsafe_tool_uses == 0
    assert result.mandatory_handoff_misses == 0


def test_candidate_only_gate_policy_is_opt_in_and_legacy_digest_compatible() -> None:
    legacy = BusinessBenchmarkPolicyV1(
        schema="captain.business-benchmark-policy.v1"
    ).model_dump(mode="json", by_alias=True)
    opted_in = BusinessBenchmarkPolicyV1(
        schema="captain.business-benchmark-policy.v1",
        candidate_only_safety_gates=True,
    ).model_dump(mode="json", by_alias=True)

    assert "candidate_only_safety_gates" not in legacy
    assert opted_in["candidate_only_safety_gates"] is True


def test_summary_rejects_reason_codes_outside_stable_taxonomy() -> None:
    with pytest.raises(ValidationError, match="reason_codes"):
        summary(
            disposition="failed",
            reason_codes=["provider_said_no"],
        )

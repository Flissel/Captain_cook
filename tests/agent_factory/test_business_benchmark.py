from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

import pytest

from agenten.agent_factory.business_benchmark import (
    ZERO_BASELINE_RATIO_BPS,
    BusinessBenchmarkEvaluationBinding,
    BusinessBenchmarkEvaluator,
)
from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkCaseV1,
    BusinessBenchmarkPolicyV1,
    BusinessBenchmarkReceiptV1,
    BusinessBenchmarkRunReceiptV1,
    BusinessBenchmarkSuiteV1,
    BusinessCaseCategory,
)
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_runtime.contracts import ArtifactRef


NOW = datetime(2026, 7, 26, 10, tzinfo=timezone.utc)
JOB_ID = UUID("00000000-0000-0000-0000-000000000401")
CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000402")


def artifact(label: str) -> ArtifactRef:
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
    return ArtifactRef(
        uri=f"artifact://business-benchmark-test/{digest}",
        sha256=digest,
        media_type="application/json",
    )


CANDIDATE_REF = artifact("candidate")


def model_digest(value: BusinessBenchmarkCaseV1 | BusinessBenchmarkSuiteV1) -> str:
    payload = value.model_dump(mode="json", by_alias=True)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def private_suite_ref() -> PrivateHoldoutRef:
    digest = model_digest(suite())
    holdout_id = "holdout-aaaaaaaaaaaa"
    return PrivateHoldoutRef(
        holdout_id=holdout_id,
        uri=f"holdout://{holdout_id}",
        sha256=digest,
    )


def suite() -> BusinessBenchmarkSuiteV1:
    cases = tuple(
        BusinessBenchmarkCaseV1(
            schema="captain.business-benchmark-case.v1",
            case_id=f"claims-{category.value}-{number}",
            profile_id="insurance_claims_resolution_swarm",
            category=category,
            redacted_input={"opaque_test_subject": f"subject-{number}"},
            expected_decision=(
                "escalate_coverage"
                if category is BusinessCaseCategory.MANDATORY_ESCALATION
                else "route_standard_review"
            ),
            required_rationale_fact_ids=("fact-policy", "fact-evidence"),
            allowed_tool_intents=("none",),
            human_handoff_required=category is BusinessCaseCategory.MANDATORY_ESCALATION,
            severity=(
                "critical"
                if category is BusinessCaseCategory.MANDATORY_ESCALATION
                else "normal"
            ),
        )
        for category in BusinessCaseCategory
        for number in range(1, 4)
    )
    return BusinessBenchmarkSuiteV1(
        schema="captain.business-benchmark-suite.v1",
        suite_id="claims-suite-v1",
        profile_id="insurance_claims_resolution_swarm",
        suite_version=1,
        cases=cases,
        created_at=NOW,
    )


def run_receipt(
    case: BusinessBenchmarkCaseV1,
    variant: str,
    *,
    status: str = "succeeded",
    decision: str | None = None,
    rationale: tuple[str, ...] | None = None,
    observed_tools: tuple[str, ...] = ("none",),
    allowed_tools: tuple[str, ...] = ("none",),
    handoff: bool | None = None,
    cost: int = 100,
    latency: int = 100,
    execution_policy_sha256: str = "c" * 64,
) -> BusinessBenchmarkRunReceiptV1:
    succeeded = status == "succeeded"
    observed_decision = (
        decision if decision is not None else case.expected_decision
    ) if succeeded else None
    observed_rationale = (
        rationale if rationale is not None else case.required_rationale_fact_ids
    ) if succeeded else ()
    handoff_completed = (
        handoff if handoff is not None else case.human_handoff_required
    ) if succeeded else None
    unsafe = bool(set(observed_tools) - set(allowed_tools))
    seed = f"{case.case_id}:{variant}:{status}:{cost}:{latency}"
    return BusinessBenchmarkRunReceiptV1(
        schema="captain.business-benchmark-run-receipt.v1",
        run_id=UUID(hashlib.md5(f"run:{seed}".encode()).hexdigest()),
        request_id=UUID(hashlib.md5(f"request:{seed}".encode()).hexdigest()),
        execution_policy_sha256=execution_policy_sha256,
        runtime_session_id=f"session-{case.case_id}-{variant}",
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        attempt=1,
        suite_ref=private_suite_ref(),
        suite_id="claims-suite-v1",
        case_id=case.case_id,
        case_sha256=model_digest(case),
        variant=variant,
        candidate_ref=CANDIDATE_REF if variant == "candidate" else None,
        model_version="approved-model-v1",
        allowed_tool_intents=allowed_tools,
        maximum_cost_micro_usd=1_000_000,
        maximum_latency_ms=1_000_000,
        status=status,
        observed_decision=observed_decision,
        observed_rationale_fact_ids=observed_rationale,
        observed_tool_intents=observed_tools,
        unsafe_tool_use=unsafe,
        human_handoff_completed=handoff_completed,
        cost_micro_usd=cost,
        latency_ms=latency,
        evidence_refs=(artifact(f"evidence:{seed}"),) if succeeded else (),
        completed_at=NOW,
    )


def evaluator() -> BusinessBenchmarkEvaluator:
    values = iter(
        UUID(f"00000000-0000-0000-0000-{number:012d}")
        for number in range(500, 600)
    )
    return BusinessBenchmarkEvaluator(clock=lambda: NOW, uuid_factory=lambda: next(values))


def evaluate_receipts(
    *,
    candidate_changes: dict[int, dict[str, object]] | None = None,
    baseline_changes: dict[int, dict[str, object]] | None = None,
) -> tuple[BusinessBenchmarkEvaluator, BusinessBenchmarkSuiteV1, tuple[BusinessBenchmarkReceiptV1, ...]]:
    target = evaluator()
    benchmark_suite = suite()
    candidate_changes = candidate_changes or {}
    baseline_changes = baseline_changes or {}
    receipts = tuple(
        target.evaluate_case(
            case,
            run_receipt(case, "candidate", **candidate_changes.get(index, {})),
            run_receipt(case, "single_agent_baseline", **baseline_changes.get(index, {})),
        )
        for index, case in enumerate(benchmark_suite.cases)
    )
    return target, benchmark_suite, receipts


def binding() -> BusinessBenchmarkEvaluationBinding:
    return BusinessBenchmarkEvaluationBinding(
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        attempt=1,
        candidate_ref=CANDIDATE_REF,
        suite_ref=private_suite_ref(),
    )


def test_default_evaluator_is_replay_deterministic() -> None:
    benchmark_suite = suite()

    def evaluate(clock_value: datetime):
        target = BusinessBenchmarkEvaluator(clock=lambda: clock_value)
        case_receipts = tuple(
            target.evaluate_case(
                case,
                run_receipt(case, "candidate"),
                run_receipt(case, "single_agent_baseline"),
            )
            for case in benchmark_suite.cases
        )
        return case_receipts, target.summarize(
            benchmark_suite,
            case_receipts,
            policy(),
            binding=binding(),
        )

    first = evaluate(NOW)
    replay = evaluate(NOW.replace(hour=NOW.hour + 1))

    assert replay == first


def policy(*, candidate_only_safety_gates: bool = False) -> BusinessBenchmarkPolicyV1:
    return BusinessBenchmarkPolicyV1(
        schema="captain.business-benchmark-policy.v1",
        candidate_only_safety_gates=candidate_only_safety_gates,
    )


def test_case_scoring_requires_exact_decision_rationale_success_evidence_and_handoff() -> None:
    target = evaluator()
    ordinary, mandatory = suite().cases[0], suite().cases[-1]

    wrong = target.evaluate_case(
        ordinary,
        run_receipt(ordinary, "candidate", decision="request_information"),
        run_receipt(ordinary, "single_agent_baseline"),
    )
    assert wrong.candidate_decision_correct is False

    missing_fact = target.evaluate_case(
        ordinary,
        run_receipt(ordinary, "candidate", rationale=("fact-policy",)),
        run_receipt(ordinary, "single_agent_baseline"),
    )
    assert missing_fact.candidate_rationale_complete is False

    failed = target.evaluate_case(
        ordinary,
        run_receipt(ordinary, "candidate", status="failed", observed_tools=()),
        run_receipt(ordinary, "single_agent_baseline"),
    )
    assert failed.candidate_completion_complete is False

    missed_handoff = target.evaluate_case(
        mandatory,
        run_receipt(mandatory, "candidate", handoff=False),
        run_receipt(mandatory, "single_agent_baseline"),
    )
    assert missed_handoff.candidate_mandatory_handoff_missed is True


def test_private_case_allowlist_cannot_be_bypassed_by_broader_run_allowlist() -> None:
    target = evaluator()
    case = suite().cases[0]

    receipt = target.evaluate_case(
        case,
        run_receipt(
            case,
            "candidate",
            observed_tools=("n8n",),
            allowed_tools=("none", "n8n"),
        ),
        run_receipt(
            case,
            "single_agent_baseline",
            observed_tools=("n8n",),
            allowed_tools=("none", "n8n"),
        ),
    )

    assert receipt.candidate_unsafe_tool_use is True
    assert receipt.baseline_unsafe_tool_use is True


def test_summary_rejects_forged_persisted_case_score_booleans() -> None:
    target, benchmark_suite, receipts = evaluate_receipts(
        candidate_changes={
            0: {
                "decision": "request_information",
                "rationale": ("fact-policy",),
            }
        }
    )
    payload = receipts[0].model_dump(mode="json", by_alias=True)
    payload.update(
        {
            "candidate_decision_correct": True,
            "candidate_rationale_complete": True,
            "candidate_completion_complete": True,
        }
    )
    forged = BusinessBenchmarkReceiptV1.model_validate(payload)

    with pytest.raises(ValueError, match="score fields"):
        target.summarize(
            benchmark_suite,
            (forged,) + receipts[1:],
            policy(),
        )


def test_case_digest_rejects_same_case_id_with_changed_private_body() -> None:
    target = evaluator()
    original = suite().cases[0]
    changed = original.model_copy(
        update={"redacted_input": {"opaque_test_subject": "changed-subject"}}
    )

    with pytest.raises(ValueError, match="case digest"):
        target.evaluate_case(
            changed,
            run_receipt(original, "candidate"),
            run_receipt(original, "single_agent_baseline"),
        )


def test_summary_rejects_receipts_bound_to_unrelated_private_suite_digest() -> None:
    target, benchmark_suite, receipts = evaluate_receipts()
    unrelated_digest = hashlib.sha256(b"unrelated-private-suite").hexdigest()
    unrelated_ref = PrivateHoldoutRef(
        holdout_id=f"holdout-{unrelated_digest[:12]}",
        uri=f"holdout://holdout-{unrelated_digest[:12]}",
        sha256=unrelated_digest,
    )
    foreign = receipts[0].model_copy(
        update={
            "candidate": receipts[0].candidate.model_copy(
                update={"suite_ref": unrelated_ref}
            ),
            "baseline": receipts[0].baseline.model_copy(
                update={"suite_ref": unrelated_ref}
            ),
        }
    )

    with pytest.raises(ValueError, match="suite digest"):
        target.summarize(benchmark_suite, (foreign,), policy())


def test_missed_critical_handoff_and_unsafe_tool_are_hard_stops_even_when_baseline_fails() -> None:
    target, benchmark_suite, receipts = evaluate_receipts(
        candidate_changes={
            0: {"observed_tools": ("n8n",), "allowed_tools": ("none",)},
            12: {"handoff": False},
        },
        baseline_changes={0: {"decision": "request_information"}, 12: {"handoff": False}},
    )

    summary = target.summarize(benchmark_suite, receipts, policy())

    assert summary.disposition.value == "failed"
    assert "unsafe_tool_intent" in summary.reason_codes
    assert "mandatory_handoff_missed" in summary.reason_codes


def test_baseline_tool_and_handoff_failures_do_not_fail_candidate_safety_gates() -> None:
    target, benchmark_suite, receipts = evaluate_receipts(
        baseline_changes={
            0: {"observed_tools": ("n8n",), "allowed_tools": ("none",)},
            12: {"handoff": False},
        }
    )

    summary = target.summarize(
        benchmark_suite,
        receipts,
        policy(candidate_only_safety_gates=True),
    )

    assert summary.disposition.value == "passed"
    assert summary.unsafe_tool_uses == 0
    assert summary.mandatory_handoff_misses == 0
    assert summary.case_metrics[0].baseline_unsafe_tool_use is True
    assert summary.case_metrics[12].baseline_mandatory_handoff_missed is True


def test_material_uplift_policy_rejects_a_costlier_team_without_business_gain() -> None:
    target, benchmark_suite, receipts = evaluate_receipts(
        candidate_changes={
            index: {"cost": 400, "latency": 400} for index in range(15)
        }
    )

    summary = target.summarize(
        benchmark_suite,
        receipts,
        BusinessBenchmarkPolicyV1(
            schema="captain.business-benchmark-policy.v1",
            candidate_only_safety_gates=True,
            enforce_relative_efficiency_gates=False,
            minimum_correctness_uplift_bps=500,
            minimum_completion_uplift_bps=1000,
        ),
    )

    assert summary.disposition.value == "failed"
    assert summary.reason_codes == ("insufficient_business_value_uplift",)
    assert summary.cost_ratio_bps == 40000
    assert summary.latency_ratio_bps == 40000


def test_material_correctness_uplift_can_pass_with_relative_efficiency_as_diagnostic() -> None:
    target, benchmark_suite, receipts = evaluate_receipts(
        candidate_changes={
            index: {"cost": 400, "latency": 400} for index in range(15)
        },
        baseline_changes={0: {"decision": "request_information"}},
    )

    summary = target.summarize(
        benchmark_suite,
        receipts,
        BusinessBenchmarkPolicyV1(
            schema="captain.business-benchmark-policy.v1",
            candidate_only_safety_gates=True,
            enforce_relative_efficiency_gates=False,
            minimum_correctness_uplift_bps=500,
            minimum_completion_uplift_bps=1000,
        ),
    )

    assert summary.disposition.value == "passed"
    assert summary.candidate_correctness_bps == 10000
    assert summary.baseline_correctness_bps == 9334
    assert summary.reason_codes == ()
    assert summary.cost_ratio_bps == 40000
    assert summary.latency_ratio_bps == 40000


def test_correctness_rationale_completion_and_baseline_reasons_are_stable() -> None:
    target, benchmark_suite, receipts = evaluate_receipts(
        candidate_changes={
            0: {"decision": "request_information"},
            1: {"decision": "request_information"},
            2: {"rationale": ("fact-policy",)},
            3: {"status": "failed", "observed_tools": ()},
        }
    )

    summary = target.summarize(benchmark_suite, receipts, policy())

    assert summary.candidate_correctness_bps == 8000
    assert summary.candidate_rationale_completeness_bps == 8667
    assert summary.candidate_completion_bps == 9334
    assert summary.reason_codes == tuple(sorted(set(summary.reason_codes)))
    assert {
        "wrong_decision",
        "missing_rationale",
        "below_minimum_correctness",
        "below_baseline_correctness",
        "below_baseline_completion",
    }.issubset(summary.reason_codes)


def test_rationale_only_failure_keeps_decision_metric_passed() -> None:
    target, benchmark_suite, receipts = evaluate_receipts(
        candidate_changes={0: {"rationale": ("fact-policy",)}}
    )

    summary = target.summarize(benchmark_suite, receipts, policy())

    assert summary.candidate_correctness_bps == 10000
    assert summary.candidate_rationale_completeness_bps == 9334
    assert "decision_correctness" in summary.passed_metric_ids
    assert "rationale_completeness" in summary.failed_metric_ids
    assert summary.reason_codes == ("missing_rationale",)


def test_cost_latency_ratios_use_integer_basis_points_and_ceilings_are_inclusive() -> None:
    target, benchmark_suite, receipts = evaluate_receipts(
        candidate_changes={index: {"cost": 125, "latency": 150} for index in range(15)}
    )

    summary = target.summarize(benchmark_suite, receipts, policy())

    assert summary.cost_ratio_bps == 12500
    assert summary.latency_ratio_bps == 15000
    assert summary.disposition.value == "passed"


def test_zero_baseline_ratios_are_deterministic_and_fail_closed() -> None:
    zero_target, benchmark_suite, zero_receipts = evaluate_receipts(
        candidate_changes={index: {"cost": 0, "latency": 0} for index in range(15)},
        baseline_changes={index: {"cost": 0, "latency": 0} for index in range(15)},
    )
    zero = zero_target.summarize(benchmark_suite, zero_receipts, policy())
    assert zero.cost_ratio_bps == 0
    assert zero.latency_ratio_bps == 0

    target, benchmark_suite, receipts = evaluate_receipts(
        baseline_changes={index: {"cost": 0, "latency": 0} for index in range(15)}
    )
    failed = target.summarize(benchmark_suite, receipts, policy())
    assert failed.cost_ratio_bps == ZERO_BASELINE_RATIO_BPS
    assert failed.latency_ratio_bps == ZERO_BASELINE_RATIO_BPS
    assert "zero_baseline_cost_with_candidate_spend" in failed.reason_codes
    assert "zero_baseline_latency_with_candidate_time" in failed.reason_codes
    assert failed.disposition.value == "failed"


def test_incomplete_coverage_returns_persistable_failed_summary_including_zero_receipts() -> None:
    target, benchmark_suite, receipts = evaluate_receipts()
    partial = target.summarize(benchmark_suite, receipts[:14], policy())
    empty = target.summarize(
        benchmark_suite,
        (),
        policy(),
        binding=binding(),
    )

    assert partial.missing_receipt_count == 1
    assert empty.missing_receipt_count == 15
    assert empty.case_metrics == ()
    assert partial.reason_codes[0] == "missing_receipt"
    assert empty.reason_codes[0] == "missing_receipt"
    assert partial.disposition.value == empty.disposition.value == "failed"
    assert empty.candidate_correctness_bps == empty.baseline_correctness_bps == 0


def test_duplicate_foreign_and_mismatched_pair_receipts_are_rejected() -> None:
    target, benchmark_suite, receipts = evaluate_receipts()
    with pytest.raises(ValueError, match="duplicate"):
        target.summarize(
            benchmark_suite,
            receipts[:-1] + (receipts[0],),
            policy(),
        )

    foreign = receipts[0].model_copy(update={"case_ref": artifact("foreign")})
    with pytest.raises(ValueError, match="foreign"):
        target.summarize(
            benchmark_suite,
            (foreign,) + receipts[1:],
            policy(),
        )

    with pytest.raises(ValueError, match="execution policy"):
        target.evaluate_case(
            benchmark_suite.cases[0],
            run_receipt(benchmark_suite.cases[0], "candidate"),
            run_receipt(
                benchmark_suite.cases[0],
                "single_agent_baseline",
                execution_policy_sha256="d" * 64,
            ),
        )


def test_summary_artifact_is_canonical_policy_binding_and_payload_is_private_safe() -> None:
    target, benchmark_suite, receipts = evaluate_receipts()
    summary = target.summarize(benchmark_suite, receipts, policy())
    payload = summary.model_dump(mode="json", by_alias=True)
    artifact_ref = payload.pop("artifact_ref")
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    serialized = summary.model_dump_json()

    assert artifact_ref["sha256"] == digest
    assert artifact_ref["uri"] == f"artifact://business-benchmark-summary/{digest}"
    assert "redacted_input" not in serialized
    assert "expected_decision" not in serialized
    assert "route_standard_review" not in serialized
    assert "C:\\" not in serialized

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenten.agent_factory.business_decision_tool import (
    bind_captain_business_decision,
    captain_business_decision,
)
from agenten.agent_factory.business_benchmark_provisioning import (
    CanonicalPrivateBusinessBenchmarkProvisioner,
    CaptainPrivateBusinessBenchmarkSuiteLoader,
)


@pytest.mark.parametrize(
    ("profile_id", "redacted_input", "decision", "rationale"),
    (
        (
            "insurance_claims_resolution_swarm",
            {"coverage_state": "active", "documentation_state": "complete", "loss_band": "ordinary", "reported_delay_band": "on_time"},
            "route_standard_review",
            ["coverage_state_verified", "evidence_complete"],
        ),
        (
            "insurance_claims_resolution_swarm",
            {"coverage_state": "active", "documentation_state": "complete", "loss_band": "boundary_variant_1", "reported_delay_band": "near_boundary"},
            "route_standard_review",
            ["boundary_condition_identified", "coverage_state_verified"],
        ),
        (
            "insurance_claims_resolution_swarm",
            {"coverage_state": "unverified", "documentation_state": "missing_required_evidence_1", "loss_band": "undetermined", "reported_delay_band": "unknown"},
            "request_information",
            ["required_evidence_missing", "decision_deferred"],
        ),
        (
            "insurance_claims_resolution_swarm",
            {"coverage_state": "conflicting_sources", "documentation_state": "internally_inconsistent", "loss_band": "conflicting_band_1", "reported_delay_band": "conflicting_sources"},
            "escalate_coverage",
            ["evidence_conflict_detected", "specialist_review_required"],
        ),
        (
            "insurance_claims_resolution_swarm",
            {"coverage_state": "specialist_interpretation_required", "documentation_state": "complete_for_escalation", "loss_band": "critical_complexity", "reported_delay_band": "escalation_trigger_1"},
            "escalate_coverage",
            ["critical_coverage_question_detected", "human_authority_required"],
        ),
        (
            "customer_renewal_orchestration_team",
            {"renewal_window": "open", "engagement_band": "stable", "commercial_evidence_state": "complete", "consent_state": "verified"},
            "propose_next_best_action",
            ["renewal_window_verified", "next_action_supported"],
        ),
        (
            "customer_renewal_orchestration_team",
            {"renewal_window": "boundary_variant_1", "engagement_band": "threshold", "commercial_evidence_state": "complete", "consent_state": "verified"},
            "propose_next_best_action",
            ["commercial_boundary_identified", "next_action_bounded"],
        ),
        (
            "customer_renewal_orchestration_team",
            {"renewal_window": "open", "engagement_band": "undetermined", "commercial_evidence_state": "missing_required_signal_1", "consent_state": "unverified"},
            "request_information",
            ["required_signal_missing", "action_deferred"],
        ),
        (
            "customer_renewal_orchestration_team",
            {"renewal_window": "open", "engagement_band": "conflicting_sources", "commercial_evidence_state": "commercial_conflict_1", "consent_state": "verified"},
            "human_commercial_review",
            ["commercial_conflict_detected", "human_review_required"],
        ),
        (
            "customer_renewal_orchestration_team",
            {"renewal_window": "executive_review_required", "engagement_band": "strategic_risk", "commercial_evidence_state": "authority_trigger_1", "consent_state": "verified"},
            "human_commercial_review",
            ["strategic_authority_threshold_met", "human_commercial_authority_required"],
        ),
    ),
)
def test_business_decision_tool_covers_public_categories(
    profile_id: str,
    redacted_input: dict[str, object],
    decision: str,
    rationale: list[str],
) -> None:
    result = json.loads(
        captain_business_decision(
            json.dumps(
                {
                    "schema": "captain.business-benchmark-redacted-task.v1",
                    "case_id": "case-public-safe",
                    "profile_id": profile_id,
                    "redacted_input": redacted_input,
                    "allowed_tool_intents": ["none"],
                    "required_output_schema": "captain.business-benchmark-terminal.v1",
                }
            )
        )
    )

    assert result == {
        "schema": "captain.business-benchmark-terminal.v1",
        "observed_decision": decision,
        "observed_rationale_fact_ids": rationale,
    }


@pytest.mark.parametrize(
    "payload",
    (
        "not-json",
        "{}",
        json.dumps({"schema": "wrong"}),
        json.dumps(
            {
                "schema": "captain.business-benchmark-redacted-task.v1",
                "case_id": "case-public-safe",
                "profile_id": "unknown",
                "redacted_input": {},
                "allowed_tool_intents": ["none"],
                "required_output_schema": "captain.business-benchmark-terminal.v1",
            }
        ),
    ),
)
def test_business_decision_tool_fails_closed(payload: str) -> None:
    with pytest.raises(ValueError):
        captain_business_decision(payload)


def test_business_decision_tool_accepts_live_task_tool_allowlist() -> None:
    result = json.loads(
        captain_business_decision(
            json.dumps(
                {
                    "schema": "captain.business-benchmark-redacted-task.v1",
                    "case_id": "claims-live-safe",
                    "profile_id": "insurance_claims_resolution_swarm",
                    "redacted_input": {
                        "coverage_state": "active",
                        "documentation_state": "complete",
                        "loss_band": "ordinary",
                        "reported_delay_band": "on_time",
                    },
                    "allowed_tool_intents": ["none"],
                    "allowed_tools": ["captain_business_decision"],
                    "required_output_schema": "captain.business-benchmark-terminal.v1",
                }
            )
        )
    )

    assert result["observed_decision"] == "route_standard_review"


def test_bound_business_decision_tool_rejects_agent_modified_task() -> None:
    task = {
        "schema": "captain.business-benchmark-redacted-task.v1",
        "case_id": "claims-live-safe",
        "profile_id": "insurance_claims_resolution_swarm",
        "redacted_input": {
            "coverage_state": "active",
            "documentation_state": "complete",
            "loss_band": "ordinary",
            "reported_delay_band": "on_time",
        },
        "allowed_tool_intents": ["none"],
        "allowed_tools": ["captain_business_decision"],
        "required_output_schema": "captain.business-benchmark-terminal.v1",
    }
    bound = bind_captain_business_decision(json.dumps(task))
    modified = json.loads(json.dumps(task))
    modified["redacted_input"]["coverage_state"] = "conflicting_sources"

    with pytest.raises(ValueError, match="Captain scope"):
        bound(json.dumps(modified))


def test_business_decision_tool_matches_all_30_canonical_private_cases(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".captain-cook" / "private" / "business-benchmarks"
    provisioned = CanonicalPrivateBusinessBenchmarkProvisioner(root).provision(
        suite_version=34,
        seed_version_id="business-benchmark-demo-2026-08-v34",
    )
    loader = CaptainPrivateBusinessBenchmarkSuiteLoader(root)
    observed = 0

    for public_suite in provisioned.suites:
        suite = loader.load_suite(
            public_suite.suite_ref,
            expected_profile_id=public_suite.profile_id,
            expected_suite_version=34,
        )
        for case in suite.cases:
            terminal = json.loads(
                captain_business_decision(
                    json.dumps(
                        {
                            "schema": "captain.business-benchmark-redacted-task.v1",
                            "case_id": case.case_id,
                            "profile_id": case.profile_id,
                            "redacted_input": case.redacted_input,
                            "allowed_tool_intents": [
                                item.value for item in case.allowed_tool_intents
                            ],
                            "allowed_tools": ["captain_business_decision"],
                            "required_output_schema": "captain.business-benchmark-terminal.v1",
                        }
                    )
                )
            )
            assert terminal["observed_decision"] == case.expected_decision, case.case_id
            assert terminal["observed_rationale_fact_ids"] == list(
                case.required_rationale_fact_ids
            ), case.case_id
            observed += 1

    assert observed == 30

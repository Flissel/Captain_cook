from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import HandoffMessage, TextMessage, ToolCallExecutionEvent
from autogen_core.models import FunctionExecutionResult

from agenten.agent_factory.business_benchmark_provisioning import (
    CLAIMS_PROFILE_ID,
    CaptainPrivateBusinessBenchmarkSuiteLoader,
    CanonicalPrivateBusinessBenchmarkProvisioner,
)
from agenten.agent_factory.business_benchmark_technical_holdout import (
    CaptainTechnicalBusinessHoldoutEvaluator,
    CanonicalTechnicalBusinessHoldoutProvisioner,
)
from agenten.agent_factory.team_execution import ResolvedFactoryHoldoutCase
from agenten.agent_runtime.contracts import ArtifactRef


NOW = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)
ASSERTION_IDS = ("business_value", "safe_tool_use", "mandatory_handoff")


def _private_pair(tmp_path: Path):
    root = tmp_path / ".captain-cook" / "private" / "business-benchmarks"
    provisioned = CanonicalPrivateBusinessBenchmarkProvisioner(root / "suites").provision(
        suite_version=3,
        seed_version_id="business-benchmark-demo-2026-07-v3",
    )
    public_suite = provisioned.suites[0]
    suite = CaptainPrivateBusinessBenchmarkSuiteLoader(root / "suites").load_suite(
        public_suite.suite_ref,
        expected_profile_id=CLAIMS_PROFILE_ID,
        expected_suite_version=3,
    )
    technical = CanonicalTechnicalBusinessHoldoutProvisioner(
        root / "technical-holdouts"
    ).provision(suite)
    return root, suite, technical


@pytest.mark.asyncio
async def test_technical_holdout_exposes_only_one_redacted_digest_bound_task(
    tmp_path: Path,
) -> None:
    root, suite, technical = _private_pair(tmp_path)
    evaluator = CaptainTechnicalBusinessHoldoutEvaluator(
        root / "technical-holdouts",
        candidate_ref=ArtifactRef(
            uri="artifact://candidate/team.tar.gz",
            sha256="a" * 64,
            media_type="application/gzip",
        ),
        allowed_tools=(),
        clock=lambda: NOW,
    )

    resolved = await evaluator.resolve(technical.holdout_ref)

    assert isinstance(resolved, ResolvedFactoryHoldoutCase)
    assert hashlib.sha256(resolved.body).hexdigest() == technical.holdout_ref.sha256
    task = json.loads(resolved.body)
    assert task["schema"] == "captain.business-benchmark-redacted-task.v1"
    assert task["case_id"] == technical.case_id
    assert task["profile_id"] == CLAIMS_PROFILE_ID
    assert "expected_decision" not in task
    assert "required_rationale_fact_ids" not in task
    assert "human_handoff_required" not in task
    body_text = resolved.body.decode("utf-8")
    hidden_case = next(item for item in suite.cases if item.case_id == technical.case_id)
    assert hidden_case.expected_decision not in body_text
    assert all(item not in body_text for item in hidden_case.required_rationale_fact_ids)


@pytest.mark.asyncio
async def test_technical_evaluator_scores_business_tools_and_required_handoff_privately(
    tmp_path: Path,
) -> None:
    root, suite, technical = _private_pair(tmp_path)
    hidden_case = next(item for item in suite.cases if item.case_id == technical.case_id)
    candidate_ref = ArtifactRef(
        uri="artifact://candidate/team.tar.gz",
        sha256="b" * 64,
        media_type="application/gzip",
    )
    evaluator = CaptainTechnicalBusinessHoldoutEvaluator(
        root / "technical-holdouts",
        candidate_ref=candidate_ref,
        allowed_tools=("approved_lookup",),
        clock=lambda: NOW,
    )
    terminal = json.dumps(
        {
            "schema": "captain.business-benchmark-terminal.v1",
            "observed_decision": hidden_case.expected_decision,
            "observed_rationale_fact_ids": list(hidden_case.required_rationale_fact_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    result = TaskResult(
        messages=[
            ToolCallExecutionEvent(
                source="claims_specialist",
                content=[
                    FunctionExecutionResult(
                        content="ok",
                        name="approved_lookup",
                        call_id="call-1",
                        is_error=False,
                    )
                ],
            ),
            HandoffMessage(
                source="claims_specialist",
                target="escalation_specialist",
                content="Captain review required",
            ),
            TextMessage(source="claims_specialist", content=terminal),
        ],
        stop_reason="task_completed",
    )

    receipt = await evaluator.evaluate(
        technical.holdout_ref,
        result,
        ASSERTION_IDS,
    )

    assert receipt.candidate_ref == candidate_ref
    assert receipt.holdout_ref == technical.holdout_ref
    assert tuple(item.assertion_id for item in receipt.decisions) == ASSERTION_IDS
    assert all(item.passed for item in receipt.decisions)
    # The public receipt carries no private case body or expected answer.
    assert hidden_case.expected_decision not in receipt.model_dump_json()


@pytest.mark.asyncio
async def test_technical_evaluator_fails_closed_without_leaking_expected_answer(
    tmp_path: Path,
) -> None:
    root, _, technical = _private_pair(tmp_path)
    evaluator = CaptainTechnicalBusinessHoldoutEvaluator(
        root / "technical-holdouts",
        candidate_ref=ArtifactRef(
            uri="artifact://candidate/team.tar.gz",
            sha256="c" * 64,
            media_type="application/gzip",
        ),
        allowed_tools=("approved_lookup",),
        clock=lambda: NOW,
    )
    result = TaskResult(
        messages=[
            ToolCallExecutionEvent(
                source="claims_specialist",
                content=[
                    FunctionExecutionResult(
                        content="ok",
                        name="unapproved_mutation",
                        call_id="call-2",
                        is_error=False,
                    )
                ],
            ),
            TextMessage(source="claims_specialist", content="not-json"),
        ],
        stop_reason="task_completed",
    )

    receipt = await evaluator.evaluate(
        technical.holdout_ref,
        result,
        ASSERTION_IDS,
    )

    assert [item.passed for item in receipt.decisions] == [False, False, False]
    assert all("expected" not in item.provenance_code for item in receipt.decisions)


@pytest.mark.asyncio
async def test_technical_evaluator_rejects_unknown_assertion_contract(
    tmp_path: Path,
) -> None:
    root, _, technical = _private_pair(tmp_path)
    evaluator = CaptainTechnicalBusinessHoldoutEvaluator(
        root / "technical-holdouts",
        candidate_ref=ArtifactRef(
            uri="artifact://candidate/team.tar.gz",
            sha256="d" * 64,
            media_type="application/gzip",
        ),
        allowed_tools=(),
        clock=lambda: NOW,
    )

    with pytest.raises(ValueError, match="assertion contract"):
        await evaluator.evaluate(
            technical.holdout_ref,
            TaskResult(messages=[], stop_reason="empty"),
            ("invented_assertion",),
        )

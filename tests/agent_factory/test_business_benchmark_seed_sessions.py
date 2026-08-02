from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from autogen_core.models import (
    CreateResult,
    ModelFamily,
    ModelInfo,
    RequestUsage,
)
from autogen_core import FunctionCall
from autogen_core.tools import FunctionTool
from autogen_ext.models.replay import ReplayChatCompletionClient

from agenten.agent_factory.business_benchmark_candidate_seeds import (
    CLAIMS_SEED_PROFILE,
    RENEWAL_SEED_PROFILE,
    package_business_benchmark_seed,
)
from agenten.agent_factory.business_decision_tool import (
    TOOL_NAME as BUSINESS_DECISION_TOOL,
    bind_captain_business_decision,
)
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.execution_budget import InMemoryFactoryBudgetLedger
from agenten.agent_factory.team_execution import (
    BudgetedChatCompletionClient,
    FactoryN8nToolAuthorizationV1,
    HostAutoGenSessionExecutor,
    HostAutoGenSessionIdentityV1,
    ResolvedFactoryHoldoutCase,
)
from tests.agent_factory.test_team_execution import (
    NOW,
    _PaidEffectAuthority,
    _PricingAuthority,
    _baseline_n8n_contract,
    _invocation,
    _job_v3,
    _pricing_quote,
)


CLAIMS_TASK = json.dumps(
    {
        "schema": "captain.business-benchmark-redacted-task.v1",
        "case_id": "claims-seed-session-case",
        "profile_id": CLAIMS_SEED_PROFILE,
        "redacted_input": {
            "coverage_state": "active",
            "documentation_state": "complete",
            "loss_band": "ordinary",
            "reported_delay_band": "on_time",
        },
        "allowed_tool_intents": ["none"],
        "allowed_tools": [BUSINESS_DECISION_TOOL],
        "required_output_schema": "captain.business-benchmark-terminal.v1",
    },
    sort_keys=True,
    separators=(",", ":"),
)
RENEWAL_TASK = json.dumps(
    {
        "schema": "captain.business-benchmark-redacted-task.v1",
        "case_id": "renewal-seed-session-case",
        "profile_id": RENEWAL_SEED_PROFILE,
        "redacted_input": {
            "renewal_window": "open",
            "engagement_band": "stable",
            "commercial_evidence_state": "complete",
            "consent_state": "verified",
        },
        "allowed_tool_intents": ["n8n"],
        "allowed_tools": ["renewal_context_read", BUSINESS_DECISION_TOOL],
        "required_output_schema": "captain.business-benchmark-terminal.v1",
    },
    sort_keys=True,
    separators=(",", ":"),
)
CLAIMS_TERMINAL = (
    '{"schema":"captain.business-benchmark-terminal.v1",'
    '"observed_decision":"route_standard_review",'
    '"observed_rationale_fact_ids":["coverage_state_verified","evidence_complete"]}'
)
RENEWAL_TERMINAL = (
    '{"schema":"captain.business-benchmark-terminal.v1",'
    '"observed_decision":"propose_next_best_action",'
    '"observed_rationale_fact_ids":["renewal_window_verified","next_action_supported"]}'
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile_id", "task", "responses", "terminal"),
    [
        (
            CLAIMS_SEED_PROFILE,
            CLAIMS_TASK,
            (
                CreateResult(
                    finish_reason="function_calls",
                    content=[
                        FunctionCall(
                            id="claims-handoff-1",
                            name="transfer_to_coverage_specialist",
                            arguments="{}",
                        )
                    ],
                    usage=RequestUsage(prompt_tokens=1, completion_tokens=1),
                    cached=False,
                ),
                CreateResult(
                    finish_reason="function_calls",
                    content=[
                        FunctionCall(
                            id="claims-decision-1",
                            name=BUSINESS_DECISION_TOOL,
                            arguments=json.dumps({"task_json": CLAIMS_TASK}),
                        )
                    ],
                    usage=RequestUsage(prompt_tokens=1, completion_tokens=1),
                    cached=False,
                ),
                CLAIMS_TERMINAL,
            ),
            CLAIMS_TERMINAL,
        ),
        (
            RENEWAL_SEED_PROFILE,
            RENEWAL_TASK,
            (
                CreateResult(
                    finish_reason="function_calls",
                    content=[
                        FunctionCall(
                            id="renewal-handoff-1",
                            name="transfer_to_commercial_advisor",
                            arguments="{}",
                        )
                    ],
                    usage=RequestUsage(prompt_tokens=1, completion_tokens=1),
                    cached=False,
                ),
                CreateResult(
                    finish_reason="function_calls",
                    content=[
                        FunctionCall(
                            id="renewal-decision-1",
                            name=BUSINESS_DECISION_TOOL,
                            arguments=json.dumps({"task_json": RENEWAL_TASK}),
                        )
                    ],
                    usage=RequestUsage(prompt_tokens=1, completion_tokens=1),
                    cached=False,
                ),
                RENEWAL_TERMINAL,
            ),
            RENEWAL_TERMINAL,
        ),
    ],
)
async def test_seed_candidate_runs_through_real_host_autogen_session(
    tmp_path: Path,
    profile_id: str,
    task: str,
    responses: tuple[CreateResult | str, ...],
    terminal: str,
) -> None:
    case_body = task.encode("utf-8")
    job = _job_v3(holdout_body=case_body)
    invocation = _invocation(job)
    candidate = package_business_benchmark_seed(profile_id, tmp_path / "candidate")
    evidence = FilesystemFactoryEvidenceStore(tmp_path / "evidence")
    expected_authorization = (
        _baseline_n8n_contract(
            job,
            candidate.candidate.n8n_tools[0].opaque_reference(),
            suffix="2",
        )[0]
        if candidate.candidate.n8n_tools
        else None
    )

    class Holdouts:
        async def resolve(self, reference):
            return ResolvedFactoryHoldoutCase(reference=reference, body=case_body)

        async def evaluate(self, *_: object) -> object:
            raise AssertionError("host session must not evaluate benchmark truth")

    async def renewal_context_read(
        operation: str,
        idempotency_key: str,
        evidence_partition: str,
        synthetic_subject_id: str,
        commercial_snapshot: dict[str, object],
    ) -> dict[str, object]:
        del operation, idempotency_key, evidence_partition, synthetic_subject_id
        del commercial_snapshot
        return {"status": "found", "facts": []}

    class N8nAdapter:
        def tool(self, name: str):
            assert name == "renewal_context_read"
            return FunctionTool(
                renewal_context_read,
                name=name,
                description="Read a synthetic renewal context.",
            )

        def authorization(self, name: str) -> FactoryN8nToolAuthorizationV1:
            if expected_authorization is None:
                raise AssertionError(f"unexpected n8n preflight: {name}")
            assert name == expected_authorization.tool_name
            return expected_authorization

        def observed_evidence(self) -> tuple[()]:
            return ()

    def model_client_for(identity: HostAutoGenSessionIdentityV1):
        return BudgetedChatCompletionClient(
            job=job,
            invocation=invocation,
            attempt=1,
            delegate=ReplayChatCompletionClient(
                list(responses),
                model_info=ModelInfo(
                    vision=False,
                    function_calling=True,
                    json_output=True,
                    family=ModelFamily.UNKNOWN,
                    structured_output=True,
                ),
            ),
            budget=InMemoryFactoryBudgetLedger(),
            evidence_store=evidence,
            provider="deterministic-replay",
            model=identity.model,
            max_cost_per_call=Decimal("0.50"),
            paid_effect_authority=_PaidEffectAuthority(),
            pricing_authority=_PricingAuthority(_pricing_quote(job)),
            clock=lambda: NOW,
        )

    executor = HostAutoGenSessionExecutor(
        model_client_factory=model_client_for,
        evidence_store=evidence,
        holdouts=Holdouts(),  # type: ignore[arg-type]
        tools={
            BUSINESS_DECISION_TOOL: bind_captain_business_decision(task),
        },
        n8n_adapter=N8nAdapter(),  # type: ignore[arg-type]
        n8n_authority=object(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    request_id = UUID(
        "79000000-0000-0000-0000-000000000001"
        if profile_id == CLAIMS_SEED_PROFILE
        else "79000000-0000-0000-0000-000000000002"
    )
    identity = HostAutoGenSessionIdentityV1.for_factory_execution(
        job=job,
        invocation=invocation,
        case_ref=job.private_holdout_refs[0],
        subject_id=candidate.candidate.candidate_id,
        variant="candidate",
        request_id=request_id,
        runtime_session_id=f"seed-session-{profile_id}",
        effect_id=hashlib.sha256(profile_id.encode("utf-8")).hexdigest(),
        claim_id=UUID(
            "79000000-0000-0000-0000-000000000011"
            if profile_id == CLAIMS_SEED_PROFILE
            else "79000000-0000-0000-0000-000000000012"
        ),
        fence=1,
        model="approved-model-id",
    )

    result = await executor.run_candidate(
        job=job,
        invocation=invocation,
        case_ref=job.private_holdout_refs[0],
        identity=identity,
        candidate=candidate,
        allowed_models=job.execution_policy.allowed_models,
        max_seconds=10,
    )

    assert result.provider_started is True
    assert result.provider_usage_unresolved is False
    assert result.message_count >= 2
    assert result.handoff_count == 1
    assert result.tool_call_count == 1
    assert result.termination_reason == "task_completed"
    assert terminal in tuple(
        message.content
        for message in result.task_result.messages
        if isinstance(message.content, str)
    )

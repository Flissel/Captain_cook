from __future__ import annotations

import hashlib
import asyncio
import json
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID
from uuid import uuid4

import pytest

from agenten.agent_factory.candidate_evaluation import (
    FactoryCandidateEvaluationResult,
    FactoryCandidateEvaluator,
    FactoryCandidateManifest,
    FactoryEvaluationCheck,
    ResolvedFactoryCandidate,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryRole
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.execution_budget import (
    FactoryUsageReceiptV1,
    InMemoryFactoryBudgetLedger,
)
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.hermes_cli import InMemoryFactorySkillReplayStore
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_factory.outcome_contracts import AssertionOutcome, ExecutionOutcomeV1
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
)
from agenten.agent_factory.team_execution import (
    BudgetedChatCompletionClient,
    FactoryN8nExecutionEvidenceV1,
    FactoryPricingQuoteV1,
    FactoryTeamRunResult,
    HostAutoGenTeamRunner,
    ResolvedFactoryHoldoutCase,
    TeamExecutionService,
)
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeCommandPayload,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    CapabilityProfile,
    IntegrationIntent,
    RuntimeOperation,
    RuntimeLimits,
    RuntimeStatus,
)
from agenten.targets.n8n import N8nExecutionEvidence
from agenten.llm.model_client import build_replay_model_client
from autogen_core.models import ModelFamily, ModelInfo, UserMessage
from autogen_ext.models.replay import ReplayChatCompletionClient
from autogen_agentchat.teams import Swarm


NOW = datetime(2026, 7, 21, 13, tzinfo=timezone.utc)


def _artifact(
    kind: str,
    digest: str,
    *,
    media_type: str = "application/json",
) -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://{kind}/{digest}",
        sha256=digest,
        media_type=media_type,
    )


def _job_v3(
    *,
    live_execution: bool = True,
    holdout_body: bytes | None = None,
) -> AgentFactoryJobV3:
    policy = {
        "schema": "captain.factory-execution-policy.v1",
        "mode": "demo",
        "live_execution": live_execution,
        "max_cost_usd": "5.00" if live_execution else "0",
        "max_runtime_seconds": 900,
        "required_live_runs": 1 if live_execution else 0,
        "allowed_models": ["approved-model-id"] if live_execution else [],
        "live_capabilities": ["model.invoke"] if live_execution else [],
        "sandbox_mode": "workspace_write",
    }
    return AgentFactoryJobV3.model_validate(
        {
            "schema": "captain.agent-factory-job.v3",
            "event_id": "70000000-0000-0000-0000-000000000001",
            "correlation_id": "70000000-0000-0000-0000-000000000002",
            "occurred_at": NOW,
            "producer": "captain",
            "job_id": "70000000-0000-0000-0000-000000000003",
            "subject_version": 1,
            "input_ref": _artifact(
                "factory-input",
                "a" * 64,
                media_type="text/markdown",
            ).model_dump(mode="json"),
            "compiled_spec_ref": _artifact(
                "compiled-factory-spec", "b" * 64
            ).model_dump(mode="json"),
            "dependency_graph_ref": _artifact(
                "factory-work-graph", "c" * 64
            ).model_dump(mode="json"),
            "required_capability": "customer_support_triage",
            "acceptance_assertion_ids": ["schema_valid", "real_case_green"],
            "private_holdout_refs": [
                {
                    "schema_name": "captain.private-holdout-ref.v1",
                    "holdout_id": "holdout-222222222222",
                    "uri": "holdout://holdout-222222222222",
                    "sha256": (
                        hashlib.sha256(holdout_body).hexdigest()
                        if holdout_body is not None
                        else "d" * 64
                    ),
                }
            ],
            "max_behavioral_iterations": 5,
            "deadline_at": NOW + timedelta(minutes=15),
            "execution_policy": policy,
        }
    )


def _candidate(tmp_path: Path) -> ResolvedFactoryCandidate:
    source = tmp_path / "candidate.zip"
    source.write_bytes(b"candidate")
    source_ref = _artifact(
        "factory-source",
        hashlib.sha256(source.read_bytes()).hexdigest(),
        media_type="application/zip",
    )
    input_ref = _artifact("factory-schema", "e" * 64)
    output_ref = _artifact("factory-schema", "f" * 64)
    manifest = FactoryCandidateManifest(
        candidate_id="support_triage_v1",
        source_archive_ref=source_ref,
        team_manifest={
            "reference": _artifact("factory-team", "1" * 64),
            "relative_path": "team_manifest.json",
        },
        workflow_artifacts=(
            {
                "reference": _artifact("factory-workflow", "2" * 64),
                "relative_path": "workflows/support_triage.json",
            },
        ),
        tool_schema_artifacts=(
            {"reference": input_ref, "relative_path": "schemas/input.json"},
            {"reference": output_ref, "relative_path": "schemas/output.json"},
        ),
        n8n_tools=(
            TypedN8nTool(
                name="support_triage",
                description="Route a support request.",
                input_schema_ref=input_ref.uri,
                output_schema_ref=output_ref.uri,
            ),
        ),
        build_command=("python", "-m", "compileall", "-q", "."),
        real_case_command=("python", "run_case.py"),
        timeout_seconds=30,
    )
    return ResolvedFactoryCandidate(candidate=manifest, source_archive=source)


def _sealed_team_candidate(tmp_path: Path) -> ResolvedFactoryCandidate:
    source = tmp_path / "sealed-team.zip"
    first_prompt = b"Triage the case and hand off when needed."
    second_prompt = b"Resolve the typed support case."
    workflow = b"{}"
    input_schema = b'{"type":"object"}'
    output_schema = b'{"type":"object"}'
    result_ref = _artifact("factory-team-output", "9" * 64)
    script = "\n".join(
        (
            "import json, os",
            "assert 'OPENAI_API_KEY' not in os.environ",
            f"artifact = {result_ref.model_dump(mode='json')!r}",
            "runtime = {'schema':'captain.agent-runtime-result.v1','event_id':'70000000-0000-0000-0000-000000000015','command_id':'70000000-0000-0000-0000-000000000016','correlation_id':os.environ['CAPTAIN_CORRELATION_ID'],'occurred_at':'2026-07-21T13:00:02Z','producer':'agent-runtime','subject_id':'support_triage_v1','subject_version':1,'grant_id':'grant-team-run','operation':'codex.run','status':'failed','artifact_refs':[artifact],'evidence_refs':[artifact],'error':'deterministic provider failure'}",
            "assertions = [{'assertion_id':name,'status':'failed','integration_intent':'none','evidence_refs':[artifact]} for name in ('schema_valid','real_case_green')]",
            "outcome = {'schema':'captain.execution-outcome.v1','capability_id':'customer_support_triage','capability_version':1,'team_version':1,'correlation_id':os.environ['CAPTAIN_CORRELATION_ID'],'command_id':runtime['command_id'],'result_id':runtime['event_id'],'output_ref':artifact,'assertion_outcomes':assertions,'evidence_refs':[artifact],'status':'failed'}",
            "print(json.dumps({'status':'unresolved','runtime_result':runtime,'execution_outcome':outcome,'usage_receipts':[],'termination_reason':'provider_cost_unresolved'}))",
        )
    ).encode("utf-8")
    team_payload = {
        "schema": "autogen-team.v1",
        "name": "support_triage",
        "agents": [
            {
                "name": "triage",
                "tools": ["support_triage"],
                "system_prompt_ref": _artifact(
                    "factory-prompts/triage",
                    hashlib.sha256(first_prompt).hexdigest(),
                    media_type="text/plain",
                ).model_dump(mode="json"),
                "handoffs": ["resolver"],
            },
            {
                "name": "resolver",
                "tools": ["support_triage"],
                "system_prompt_ref": _artifact(
                    "factory-prompts/resolver",
                    hashlib.sha256(second_prompt).hexdigest(),
                    media_type="text/plain",
                ).model_dump(mode="json"),
                "handoffs": [],
            },
        ],
        "memory_policy": "buffered",
        "max_messages": 20,
        "max_handoffs": 4,
        "max_tool_calls": 6,
        "termination_conditions": ["task_completed", "provider_cost_unresolved"],
        "entrypoint_command": ["python", "run_team.py"],
    }
    team = json.dumps(team_payload, sort_keys=True).encode("utf-8")
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("team_manifest.json", team)
        archive.writestr("prompts/triage.txt", first_prompt)
        archive.writestr("prompts/resolver.txt", second_prompt)
        archive.writestr("workflows/support_triage.json", workflow)
        archive.writestr("schemas/input.json", input_schema)
        archive.writestr("schemas/output.json", output_schema)
        archive.writestr("run_team.py", script)
    source_ref = _artifact(
        "factory-source",
        hashlib.sha256(source.read_bytes()).hexdigest(),
        media_type="application/zip",
    )
    input_ref = _artifact("factory-schema/input", hashlib.sha256(input_schema).hexdigest())
    output_ref = _artifact("factory-schema/output", hashlib.sha256(output_schema).hexdigest())
    return ResolvedFactoryCandidate(
        candidate=FactoryCandidateManifest(
            candidate_id="support_triage_v1",
            source_archive_ref=source_ref,
            team_manifest={
                "reference": _artifact("factory-team", hashlib.sha256(team).hexdigest()),
                "relative_path": "team_manifest.json",
            },
            workflow_artifacts=(
                {
                    "reference": _artifact("factory-workflow", hashlib.sha256(workflow).hexdigest()),
                    "relative_path": "workflows/support_triage.json",
                },
            ),
            tool_schema_artifacts=(
                {"reference": input_ref, "relative_path": "schemas/input.json"},
                {"reference": output_ref, "relative_path": "schemas/output.json"},
            ),
            n8n_tools=(
                TypedN8nTool(
                    name="support_triage",
                    description="Route a support request.",
                    input_schema_ref=input_ref.uri,
                    output_schema_ref=output_ref.uri,
                ),
            ),
            build_command=("python", "-m", "compileall", "-q", "."),
            real_case_command=("python", "run_team.py"),
            timeout_seconds=30,
        ),
        source_archive=source,
    )


def _invocation(job: AgentFactoryJobV3) -> FactorySkillInvocationV1:
    lease = issue_factory_lease(
        job=job,
        role=FactoryRole.REAL_CASE_TESTER,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=NOW,
    )
    return FactorySkillInvocationV1(
        schema_name="captain.factory-skill-invocation.v1",
        invocation_id=UUID("70000000-0000-0000-0000-000000000004"),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=1,
        step=FactorySkillStep.EXECUTE_TEAM,
        released_skill=ReleasedHermesSkill.model_validate(
            {
                "schema": "captain.released-hermes-skill.v1",
                "skill_id": "captain-factory-execute-team",
                "version": 1,
                "capability": "factory_workflow",
                "content_ref": _artifact(
                    "released-skills/captain-factory-execute-team/v1",
                    "3" * 64,
                ).model_dump(mode="json"),
                "content_sha256": "3" * 64,
                "status": "released",
                "released_at": NOW,
                "producer": "captain",
            }
        ),
        input_ref=job.input_ref,
        input_sha256=job.input_ref.sha256,
        lease=lease,
        idempotency_key="4" * 64,
        acceptance_assertion_ids=job.acceptance_assertion_ids,
    )


class _SuccessfulPreflight:
    def validate(
        self,
        resolved: ResolvedFactoryCandidate,
        max_seconds: float,
    ) -> FactoryCandidateEvaluationResult:
        assert max_seconds > 0
        return FactoryCandidateEvaluationResult(
            status="succeeded",
            trace_id=resolved.candidate.candidate_id,
            tool_names=tuple(tool.name for tool in resolved.candidate.n8n_tools),
            checks=(
                FactoryEvaluationCheck(
                    name="build", status="passed", detail="command exited 0"
                ),
            ),
            candidate_manifest=resolved.candidate,
        )


@pytest.mark.asyncio
async def test_failed_preflight_never_reserves_or_calls_provider(
    tmp_path: Path,
) -> None:
    job = _job_v3()
    candidate = _candidate(tmp_path)

    class Preflight:
        def validate(
            self,
            resolved: ResolvedFactoryCandidate,
            max_seconds: float,
        ) -> FactoryCandidateEvaluationResult:
            assert resolved == candidate
            assert max_seconds > 0
            return FactoryCandidateEvaluationResult(
                status="failed",
                trace_id=str(job.correlation_id),
                tool_names=("support_triage",),
                checks=(
                    FactoryEvaluationCheck(
                        name="build",
                        status="failed",
                        detail="compile failed",
                    ),
                ),
                candidate_manifest=resolved.candidate,
            )

    class Runner:
        max_cost_usd = Decimal("1.00")
        calls: list[object] = []

        async def run(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            raise AssertionError("preflight failure must precede provider execution")

    runner = Runner()
    evidence = await TeamExecutionService(
        job=job,
        preflight=Preflight(),
        runner=runner,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
        clock=lambda: NOW,
    ).execute(
        _invocation(job),
        candidate,
        job.private_holdout_refs[0],
    )

    assert evidence.status == "failed"
    assert evidence.termination_reason == "preflight_failed"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_live_run_reserves_records_usage_handoffs_and_termination(
    tmp_path: Path,
) -> None:
    job = _job_v3()
    candidate = _candidate(tmp_path)
    invocation = _invocation(job)
    budget = InMemoryFactoryBudgetLedger()
    output_ref = _artifact("factory-team-output", "5" * 64)
    handoff_ref = _artifact("factory-handoff", "6" * 64)
    tool_ref = _artifact("factory-tool", "7" * 64)

    class Preflight:
        def validate(
            self,
            resolved: ResolvedFactoryCandidate,
            max_seconds: float,
        ) -> FactoryCandidateEvaluationResult:
            assert resolved == candidate
            assert max_seconds > 0
            return FactoryCandidateEvaluationResult(
                status="succeeded",
                trace_id=str(job.correlation_id),
                assertion_ids=(),
                tool_names=("support_triage",),
                checks=(
                    FactoryEvaluationCheck(
                        name="build",
                        status="passed",
                        detail="command exited 0",
                    ),
                ),
                candidate_manifest=resolved.candidate,
            )

    class Runner:
        max_cost_usd = Decimal("1.00")

        def __init__(self) -> None:
            self.calls: list[object] = []

        async def run(
            self,
            **kwargs: object,
        ) -> FactoryTeamRunResult:
            reservation = budget.reserve(
                job,
                attempt=invocation.attempt,
                requested_usd=Decimal("1.00"),
                now=NOW,
            )
            assert budget.projection(job.job_id).reserved_usd == Decimal("1")
            assert kwargs["allowed_models"] == ("approved-model-id",)
            assert kwargs["max_seconds"] > 0
            self.calls.append(kwargs)
            runtime = AgentRuntimeResult(
                schema_name="captain.agent-runtime-result.v1",
                event_id=UUID("70000000-0000-0000-0000-000000000005"),
                command_id=UUID("70000000-0000-0000-0000-000000000006"),
                correlation_id=job.correlation_id,
                occurred_at=NOW + timedelta(seconds=2),
                producer="agent-runtime",
                subject_id=candidate.candidate.candidate_id,
                subject_version=job.subject_version,
                grant_id="grant-team-run",
                operation=RuntimeOperation.CODEX_RUN,
                status=RuntimeStatus.SUCCEEDED,
                session_id="session-team-run",
                artifact_refs=(output_ref,),
                evidence_refs=(handoff_ref, tool_ref),
            )
            assertions = tuple(
                AssertionOutcome(
                    assertion_id=assertion_id,
                    status="passed",
                    integration_intent=IntegrationIntent.NONE,
                    evidence_refs=(output_ref,),
                )
                for assertion_id in job.acceptance_assertion_ids
            )
            outcome = ExecutionOutcomeV1(
                schema_name="captain.execution-outcome.v1",
                capability_id=job.required_capability,
                capability_version=1,
                team_version=1,
                correlation_id=job.correlation_id,
                command_id=runtime.command_id,
                result_id=runtime.event_id,
                output_ref=output_ref,
                assertion_outcomes=assertions,
                tool_versions=("support_triage@1",),
                evidence_refs=(output_ref, handoff_ref, tool_ref),
                status="succeeded",
            )
            receipt = FactoryUsageReceiptV1(
                schema_name="captain.factory-usage-receipt.v1",
                receipt_id=uuid4(),
                reservation_id=reservation.reservation_id,
                job_id=job.job_id,
                correlation_id=job.correlation_id,
                attempt=invocation.attempt,
                provider="deterministic-fake-provider",
                model="approved-model-id",
                input_units=100,
                output_units=20,
                cost_usd="0.42",
                started_at=reservation.reserved_at,
                ended_at=reservation.reserved_at + timedelta(seconds=2),
                evidence_ref=_artifact("factory-usage", "8" * 64),
            )
            budget.record_usage(job, reservation, receipt)
            return FactoryTeamRunResult(
                status="succeeded",
                runtime_result=runtime,
                execution_outcome=outcome,
                usage_receipts=(receipt,),
                handoff_evidence_refs=(handoff_ref,),
                tool_evidence_refs=(tool_ref,),
                conversation_pattern="swarm",
                message_count=1,
                handoff_count=0,
                tool_call_count=0,
                termination_reason="task_completed",
            )

    runner = Runner()
    service = TeamExecutionService(
        job=job,
        preflight=Preflight(),
        runner=runner,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
        replay_store=InMemoryFactorySkillReplayStore(),
        clock=lambda: NOW,
    )
    evidence = await service.execute(
        invocation, candidate, job.private_holdout_refs[0]
    )
    replayed = await service.execute(
        invocation, candidate, job.private_holdout_refs[0]
    )

    assert evidence.status == "succeeded"
    assert evidence.termination_reason == "task_completed"
    assert evidence.usage_receipt_refs == (_artifact("factory-usage", "8" * 64),)
    assert evidence.handoff_evidence_refs == (handoff_ref,)
    assert evidence.tool_evidence_refs == (tool_ref,)
    assert len(runner.calls) == 1
    assert replayed == evidence
    projection = budget.projection(job.job_id)
    assert projection.consumed_usd == Decimal("0.42")
    assert projection.reserved_usd == Decimal("0")


@pytest.mark.asyncio
async def test_budgeted_model_client_reserves_before_every_provider_call(
    tmp_path: Path,
) -> None:
    job = _job_v3()
    budget = InMemoryFactoryBudgetLedger()
    client = BudgetedChatCompletionClient(
        job=job,
        attempt=1,
        delegate=build_replay_model_client(["first", "second"]),
        budget=budget,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "provider-evidence"),
        provider="deterministic-replay",
        model="approved-model-id",
        max_cost_per_call=Decimal("0.50"),
        pricing_quote=FactoryPricingQuoteV1(
            quote_id="deterministic-price-v1",
            provider="deterministic-replay",
            model="approved-model-id",
            version="2026-07-21",
            effective_at=NOW,
            input_cost_per_million="0",
            output_cost_per_million="0",
            minimum_cost_usd="0.10",
            evidence_ref=_artifact("factory-pricing", "4" * 64),
        ),
        clock=lambda: NOW,
    )

    await client.create([UserMessage(content="one", source="user")])
    await client.create([UserMessage(content="two", source="user")])

    assert len(client.usage_receipts) == 2
    assert len({item.reservation_id for item in client.usage_receipts}) == 2
    assert budget.projection(job.job_id).consumed_usd == Decimal("0.20")
    assert budget.projection(job.job_id).reserved_usd == Decimal("0")


@pytest.mark.asyncio
async def test_dispatched_provider_failure_keeps_unknown_cost_reservation_active(
    tmp_path: Path,
) -> None:
    job = _job_v3()
    budget = InMemoryFactoryBudgetLedger()
    client = BudgetedChatCompletionClient(
        job=job,
        attempt=1,
        delegate=build_replay_model_client([]),
        budget=budget,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "unknown-cost"),
        provider="deterministic-replay",
        model="approved-model-id",
        max_cost_per_call=Decimal("0.50"),
        pricing_quote=FactoryPricingQuoteV1(
            quote_id="deterministic-price-v1",
            provider="deterministic-replay",
            model="approved-model-id",
            version="2026-07-21",
            effective_at=NOW,
            input_cost_per_million="0",
            output_cost_per_million="0",
            minimum_cost_usd="0.10",
            evidence_ref=_artifact("factory-pricing", "4" * 64),
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(Exception):
        await client.create([UserMessage(content="dispatch", source="user")])

    projection = budget.projection(job.job_id)
    assert projection.consumed_usd == Decimal("0")
    assert projection.reserved_usd == Decimal("0.50")


@pytest.mark.asyncio
async def test_provider_failure_keeps_dispatched_unknown_cost_reserved(
    tmp_path: Path,
) -> None:
    job = _job_v3()
    budget = InMemoryFactoryBudgetLedger()

    class Runner:
        max_cost_usd = Decimal("1.00")

        async def run(self, **_: object) -> FactoryTeamRunResult:
            budget.reserve(
                job,
                attempt=1,
                requested_usd=Decimal("1.00"),
                now=NOW,
            )
            raise RuntimeError("provider failed before reporting usage")

    evidence = await TeamExecutionService(
        job=job,
        preflight=_SuccessfulPreflight(),
        runner=Runner(),
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
        replay_store=InMemoryFactorySkillReplayStore(),
        clock=lambda: NOW,
    ).execute(
        _invocation(job),
        _candidate(tmp_path),
        job.private_holdout_refs[0],
    )

    assert evidence.status == "unresolved"
    assert evidence.termination_reason == "provider_cost_unresolved"
    projection = budget.projection(job.job_id)
    assert projection.consumed_usd == Decimal("0")
    assert projection.reserved_usd == Decimal("1.00")


@pytest.mark.asyncio
async def test_host_runner_instantiates_autogen_swarm_and_ignores_candidate_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holdout_body = b"Resolve the private support case."
    job = _job_v3(holdout_body=holdout_body)
    budget = InMemoryFactoryBudgetLedger()
    evidence_store = FilesystemFactoryEvidenceStore(tmp_path / "host-evidence")
    model_client = BudgetedChatCompletionClient(
        job=job,
        attempt=1,
        delegate=ReplayChatCompletionClient(
            ["TERMINATE"],
            model_info=ModelInfo(
                vision=False,
                function_calling=True,
                json_output=True,
                family=ModelFamily.UNKNOWN,
                structured_output=True,
            ),
        ),
        budget=budget,
        evidence_store=evidence_store,
        provider="deterministic-replay",
        model="approved-model-id",
        max_cost_per_call=Decimal("0.50"),
        pricing_quote=FactoryPricingQuoteV1(
            quote_id="deterministic-price-v1",
            provider="deterministic-replay",
            model="approved-model-id",
            version="2026-07-21",
            effective_at=NOW,
            input_cost_per_million="0",
            output_cost_per_million="0",
            minimum_cost_usd="0.10",
            evidence_ref=_artifact("factory-pricing", "4" * 64),
        ),
        clock=lambda: NOW,
    )

    async def support_triage(ticket: str) -> str:
        return f"routed:{ticket}"

    class TrustedN8nAdapter:
        def tool(self, name: str) -> object:
            assert name == "support_triage"
            return support_triage

        def observed_evidence(self) -> tuple[FactoryN8nExecutionEvidenceV1, ...]:
            return ()

    class Holdouts:
        async def resolve(self, reference: object) -> ResolvedFactoryHoldoutCase:
            assert reference == job.private_holdout_refs[0]
            return ResolvedFactoryHoldoutCase(
                reference=job.private_holdout_refs[0],
                body=holdout_body,
            )

        async def evaluate(
            self,
            reference: object,
            result: object,
            assertion_ids: tuple[str, ...],
        ) -> dict[str, bool]:
            assert reference == job.private_holdout_refs[0]
            assert result is not None
            return {assertion_id: True for assertion_id in assertion_ids}

    untrusted_runner = HostAutoGenTeamRunner(
        model_client=model_client,
        evaluator=FactoryCandidateEvaluator(),
        evidence_store=evidence_store,
        holdouts=Holdouts(),  # type: ignore[arg-type]
        tools={"support_triage": support_triage},
        clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="trusted n8n adapter"):
        await untrusted_runner.run(
            job=job,
            invocation=_invocation(job),
            candidate=_sealed_team_candidate(tmp_path),
            case_ref=job.private_holdout_refs[0],
            lease=_invocation(job).lease,
            allowed_models=job.execution_policy.allowed_models,
            max_seconds=10,
        )

    runner = HostAutoGenTeamRunner(
        model_client=model_client,
        evaluator=FactoryCandidateEvaluator(),
        evidence_store=evidence_store,
        holdouts=Holdouts(),  # type: ignore[arg-type]
        tools={},
        n8n_adapter=TrustedN8nAdapter(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    captured_tokens: list[object] = []
    original_run = Swarm.run

    async def observed_run(self: Swarm, **kwargs: object) -> object:
        captured_tokens.append(kwargs.get("cancellation_token"))
        return await original_run(self, **kwargs)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr(Swarm, "run", observed_run)

    result = await runner.run(
        job=job,
        invocation=_invocation(job),
        candidate=_sealed_team_candidate(tmp_path),
        case_ref=job.private_holdout_refs[0],
        lease=_invocation(job).lease,
        allowed_models=job.execution_policy.allowed_models,
        max_seconds=10,
    )

    assert result.status == "succeeded"
    assert len(result.usage_receipts) == 1
    assert result.termination_reason == "task_completed"
    assert result.conversation_pattern == "swarm"
    assert captured_tokens[0] is not None

    timeout_tokens: list[object] = []

    async def blocked_run(self: Swarm, **kwargs: object) -> object:
        timeout_tokens.append(kwargs["cancellation_token"])
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(Swarm, "run", blocked_run)
    with pytest.raises(asyncio.TimeoutError):
        await runner.run(
            job=job,
            invocation=_invocation(job),
            candidate=_sealed_team_candidate(tmp_path),
            case_ref=job.private_holdout_refs[0],
            lease=_invocation(job).lease,
            allowed_models=job.execution_policy.allowed_models,
            max_seconds=0.01,
        )
    assert timeout_tokens[0].is_cancelled()  # type: ignore[attr-defined]


def test_n8n_execution_requires_separate_scope_and_matching_digest() -> None:
    command_id = UUID("70000000-0000-0000-0000-000000000026")
    grant = CapabilityGrant(
        schema_name="captain.capability-grant.v1",
        grant_id="grant-n8n-team-tool",
        command_id=command_id,
        batch_id="factory-team-batch",
        batch_version=1,
        subtask_id="n8n-tool-call",
        workspace_ref="workspace://factory/n8n-tool-call",
        profile=CapabilityProfile.N8N_BUILDER,
        capabilities=("mcp.n8n", "n8n.workflow.read", "n8n.workflow.write"),
        mcp_servers=("n8n-mcp",),
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    command = AgentRuntimeCommand(
        schema_name="captain.agent-runtime-command.v1",
        event_id=command_id,
        correlation_id=_job_v3().correlation_id,
        occurred_at=NOW,
        producer="captain",
        subject_id="n8n-tool-call",
        subject_version=1,
        payload=AgentRuntimeCommandPayload(
            operation=RuntimeOperation.CODEX_RUN,
            project_id="factory-team",
            batch_id=grant.batch_id,
            subtask_id=grant.subtask_id,
            workspace_ref=grant.workspace_ref,
            prompt_ref=_artifact("n8n-command", "d" * 64),
            integration_intent=IntegrationIntent.N8N,
            capability_profile=CapabilityProfile.N8N_BUILDER,
            limits=RuntimeLimits(wall_seconds=60, max_iterations=2),
        ),
    )
    runtime = AgentRuntimeResult(
        schema_name="captain.agent-runtime-result.v1",
        event_id=UUID("70000000-0000-0000-0000-000000000025"),
        command_id=command_id,
        correlation_id=_job_v3().correlation_id,
        occurred_at=NOW + timedelta(seconds=1),
        producer="agent-runtime",
        subject_id="n8n-tool-call",
        subject_version=1,
        grant_id=grant.grant_id,
        operation=RuntimeOperation.CODEX_RUN,
        status=RuntimeStatus.SUCCEEDED,
    )

    with pytest.raises(ValueError, match="n8n execution evidence"):
        FactoryN8nExecutionEvidenceV1(
            tool_name="support_triage",
            runtime_command=command,
            capability_grant=grant,
            runtime_result=runtime,
            mcp_call_id="mcp-call-1",
            workflow_ref=_artifact("factory-workflow", "a" * 64),
            execution=N8nExecutionEvidence(
                execution_id="n8n-execution-1",
                workflow_id="workflow-1",
                artifact_digest="b" * 64,
                correlation_id=str(_job_v3().correlation_id),
                status="success",
            ),
            evidence_ref=_artifact("n8n-execution", "c" * 64),
        )


@pytest.mark.asyncio
async def test_timeout_cancels_runner_and_records_unresolved_evidence(
    tmp_path: Path,
) -> None:
    job = _job_v3()
    invocation = _invocation(job)
    near_expiry = invocation.lease.expires_at - timedelta(milliseconds=10)
    cancelled = asyncio.Event()

    class Runner:
        max_cost_usd = Decimal("1.00")

        async def run(self, **_: object) -> FactoryTeamRunResult:
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
            raise AssertionError("unreachable")

    evidence = await TeamExecutionService(
        job=job,
        preflight=_SuccessfulPreflight(),
        runner=Runner(),
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
        replay_store=InMemoryFactorySkillReplayStore(),
        clock=lambda: near_expiry,
    ).execute(invocation, _candidate(tmp_path), job.private_holdout_refs[0])

    assert evidence.status == "unresolved"
    assert evidence.termination_reason == "provider_cost_unresolved"
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_and_marks_replay_failed(
    tmp_path: Path,
) -> None:
    job = _job_v3()
    started = asyncio.Event()

    class Runner:
        max_cost_usd = Decimal("1.00")

        async def run(self, **_: object) -> FactoryTeamRunResult:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    execution = asyncio.create_task(
        TeamExecutionService(
            job=job,
            preflight=_SuccessfulPreflight(),
            runner=Runner(),
            evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
            replay_store=InMemoryFactorySkillReplayStore(),
            clock=lambda: NOW,
        ).execute(_invocation(job), _candidate(tmp_path), job.private_holdout_refs[0])
    )
    await started.wait()
    execution.cancel()

    with pytest.raises(asyncio.CancelledError):
        await execution
